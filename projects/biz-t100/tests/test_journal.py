"""只使用私有临时数据库；不访问来源数据库，也不发起业务调用。"""

import os
import sqlite3
import stat
import threading
from pathlib import Path

import anyio
import pytest

from biz_t100.journal import SQLiteQueryJournal
from biz_t100.models import BusinessError, Caller

CALLER = Caller(issuer="https://identity.invalid", subject="test-user", client_id="test-client")


async def append(
    journal, request_id="request-1", event="query_started", code=None, upstream_attempted=False
):
    await journal.record(
        request_id=request_id,
        caller=CALLER,
        employee_id="E001",
        event=event,
        code=code,
        upstream_attempted=upstream_attempted,
    )


async def test_private_query_only_schema_and_reopen(tmp_path):
    path = tmp_path / "private" / "queries.sqlite"
    journal = SQLiteQueryJournal(path)
    assert not path.exists()  # 构造时绝不执行 I/O。
    await append(journal)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    reopened = SQLiteQueryJournal(path)
    assert await reopened.pending_request_ids() == ["request-1"]
    with sqlite3.connect(path) as db:
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert tables == [("query_events",), ("journal_context",)]
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
        columns = [row[1] for row in db.execute("PRAGMA table_info(query_events)")]
        assert columns == [
            "request_id",
            "event",
            "occurred_at",
            "issuer",
            "subject",
            "client_id",
            "employee_id",
            "code",
            "upstream_attempted",
            "enterprise_id",
            "tool_name",
        ]


@pytest.mark.parametrize("event", ["query_succeeded", "query_failed", "query_cancelled"])
async def test_terminal_stops_pending_and_inspection_does_not_mutate(tmp_path, event):
    path = tmp_path / "queries.sqlite"
    journal = SQLiteQueryJournal(path)
    await append(journal)
    await append(
        journal,
        event=event,
        code=None if event == "query_succeeded" else event,
        upstream_attempted=event == "query_succeeded",
    )
    before = path.read_bytes()
    assert await SQLiteQueryJournal(path).pending_request_ids() == []
    assert path.read_bytes() == before


async def test_failed_terminal_preserves_pending_and_safe_error(tmp_path, monkeypatch):
    path = tmp_path / "queries.sqlite"
    journal = SQLiteQueryJournal(path)
    await append(journal)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("private-path SELECT secret-token")

    with monkeypatch.context() as scoped:
        scoped.setattr("biz_t100.journal.sqlite3.connect", fail)
        with pytest.raises(BusinessError, match="^journal_unavailable$") as error:
            await append(journal, event="query_succeeded", upstream_attempted=True)
        assert error.value.__context__ is None
    assert await journal.pending_request_ids() == ["request-1"]


@pytest.mark.parametrize("kind", ["foreign", "old-account", "empty"])
async def test_rejects_other_or_uninitialized_database_without_changes(tmp_path, kind):
    path = tmp_path / "old.sqlite"
    if kind == "empty":
        path.touch(mode=0o600)
    else:
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE old_data(value TEXT)")
            db.execute("INSERT INTO old_data VALUES ('preserve')")
            if kind == "old-account":
                db.execute("PRAGMA application_id=1094927188")
                db.execute("PRAGMA user_version=2")
        path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await append(SQLiteQueryJournal(path))
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["file", "parent", "sidecar", "permissions", "directory"])
async def test_rejects_symlinks_or_shared_permissions(tmp_path, kind):
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    path = folder / "queries.sqlite"
    await append(SQLiteQueryJournal(path))
    if kind == "file":
        alias = folder / "alias.sqlite"
        alias.symlink_to(path)
        path = alias
    elif kind == "parent":
        alias = tmp_path / "alias"
        alias.symlink_to(folder, target_is_directory=True)
        path = alias / path.name
    elif kind == "sidecar":
        (folder / "queries.sqlite-journal").symlink_to(tmp_path / "elsewhere")
    elif kind == "permissions":
        path.chmod(0o640)
    else:
        folder.chmod(0o750)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await append(SQLiteQueryJournal(path), request_id="request-2")


@pytest.mark.parametrize(
    "field,value",
    [
        ("event", "token=private"),
        ("code", "private-token"),
        ("employee_id", "E001\nsecret"),
        ("request_id", "sql\nsecret"),
    ],
)
async def test_unapproved_metadata_never_creates_journal(tmp_path, field, value):
    path = tmp_path / "queries.sqlite"
    kwargs = dict(
        request_id="request-1",
        caller=CALLER,
        employee_id="E001",
        event="query_failed",
        code="query_failed",
    )
    kwargs[field] = value
    with pytest.raises(BusinessError, match="^journal_unavailable$") as error:
        await SQLiteQueryJournal(path).record(**kwargs)
    assert error.value.__context__ is None
    assert not path.exists()


async def test_same_request_cannot_start_again_or_get_conflicting_terminal(tmp_path):
    journal = SQLiteQueryJournal(tmp_path / "queries.sqlite")
    await append(journal)
    await append(journal, event="query_succeeded", upstream_attempted=True)
    for event in ("query_started", "query_failed"):
        with pytest.raises(BusinessError, match="^journal_unavailable$"):
            await append(journal, event=event)
    assert await journal.pending_request_ids() == []


async def test_worker_does_not_block_loop_or_abandon_commit_on_cancel(tmp_path, monkeypatch):
    journal = SQLiteQueryJournal(tmp_path / "queries.sqlite")
    entered, release = threading.Event(), threading.Event()
    original = journal._record

    def delayed(values):
        entered.set()
        assert release.wait(3)
        original(values)

    monkeypatch.setattr(journal, "_record", delayed)
    finished = anyio.Event()
    with anyio.CancelScope() as scope:
        async with anyio.create_task_group() as group:

            async def writer():
                await append(journal)
                finished.set()

            group.start_soon(writer)
            with anyio.fail_after(2):
                while not entered.is_set():
                    await anyio.sleep(0.001)
            assert not finished.is_set()
            scope.cancel()
            release.set()
    assert finished.is_set()
    assert await SQLiteQueryJournal(tmp_path / "queries.sqlite").pending_request_ids() == [
        "request-1"
    ]


async def test_missing_inspection_does_not_create_database(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await SQLiteQueryJournal(path).pending_request_ids()
    assert not path.exists()


async def test_commit_failure_rolls_back_terminal_and_preserves_pending(tmp_path, monkeypatch):
    path = tmp_path / "queries.sqlite"
    journal = SQLiteQueryJournal(path)
    await append(journal)
    connect = sqlite3.connect

    class CommitFailure:
        def __init__(self, *args, **kwargs):
            self.inner = connect(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def commit(self):
            raise sqlite3.OperationalError("private SQL and credentials must stay hidden")

    with monkeypatch.context() as scoped:
        scoped.setattr("biz_t100.journal.sqlite3.connect", CommitFailure)
        with pytest.raises(BusinessError, match="^journal_unavailable$") as error:
            await append(journal, event="query_succeeded", upstream_attempted=True)
        assert error.value.__context__ is None
    assert await SQLiteQueryJournal(path).pending_request_ids() == ["request-1"]


async def test_each_new_directory_entry_is_synced_before_descending(tmp_path, monkeypatch):
    path = tmp_path / "new-parent" / "new-leaf" / "queries.sqlite"
    events = []
    original_mkdir = Path.mkdir
    original_fsync = os.fsync

    def mkdir(directory, *args, **kwargs):
        result = original_mkdir(directory, *args, **kwargs)
        events.append(("mkdir", directory.stat().st_ino))
        return result

    def fsync(descriptor):
        info = os.fstat(descriptor)
        assert stat.S_ISDIR(info.st_mode)
        events.append(("fsync", info.st_ino))
        return original_fsync(descriptor)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr("biz_t100.journal.os.fsync", fsync)
    await append(SQLiteQueryJournal(path))
    parent = path.parent.parent
    leaf = path.parent
    assert events == [
        ("mkdir", parent.stat().st_ino),
        ("fsync", tmp_path.stat().st_ino),
        ("mkdir", leaf.stat().st_ino),
        ("fsync", parent.stat().st_ino),
        ("fsync", leaf.stat().st_ino),
    ]
    assert await SQLiteQueryJournal(path).pending_request_ids() == ["request-1"]


async def test_parent_directory_sync_failure_prevents_database_creation(tmp_path, monkeypatch):
    path = tmp_path / "new-parent" / "new-leaf" / "queries.sqlite"

    def fail(descriptor):
        raise OSError("private directory synchronization detail")

    monkeypatch.setattr("biz_t100.journal.os.fsync", fail)
    with pytest.raises(BusinessError, match="^journal_unavailable$") as error:
        await append(SQLiteQueryJournal(path))
    assert error.value.__context__ is None
    assert not path.parent.exists()
    assert not path.exists()


@pytest.mark.parametrize("event", ["query_failed", "query_cancelled"])
@pytest.mark.parametrize("attempted", [False, True])
async def test_terminal_preserves_attempt_fact_and_fixed_context(tmp_path, event, attempted):
    path = tmp_path / "queries.sqlite"
    journal = SQLiteQueryJournal(path, enterprise_id="test-enterprise")
    await append(journal)
    await append(journal, event=event, code=event, upstream_attempted=attempted)
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT event,upstream_attempted,enterprise_id,tool_name "
            "FROM query_events ORDER BY occurred_at"
        ).fetchall()
    assert rows == [
        ("query_started", 0, "test-enterprise", "get_my_t100_employee_record"),
        (event, int(attempted), "test-enterprise", "get_my_t100_employee_record"),
    ]
    assert await SQLiteQueryJournal(path).pending_request_ids() == []


@pytest.mark.parametrize(
    "event,attempted",
    [
        ("query_started", True),
        ("query_succeeded", False),
        ("query_failed", 0),
        ("query_failed", 1),
        ("query_failed", "false"),
        ("query_failed", None),
    ],
)
async def test_invalid_attempt_fact_never_creates_a_database(tmp_path, event, attempted):
    path = tmp_path / "queries.sqlite"
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await append(SQLiteQueryJournal(path), event=event, upstream_attempted=attempted)
    assert not path.exists()


async def test_prepare_binds_context_without_creating_business_events(tmp_path):
    path = tmp_path / "private" / "queries.sqlite"
    journal = SQLiteQueryJournal(path, enterprise_id="test-enterprise")
    await journal.prepare()
    await journal.check_ready()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM query_events").fetchone() == (0,)
        assert db.execute("SELECT * FROM journal_context").fetchall() == [
            (1, "test-enterprise", "get_my_t100_employee_record")
        ]
    assert await SQLiteQueryJournal(path).pending_request_ids() == []
    await append(journal)
    before = path.read_bytes()
    inspector = SQLiteQueryJournal(path)
    assert await inspector.pending_request_ids() == ["request-1"]
    await inspector.check_ready()
    assert path.read_bytes() == before
    reopened = SQLiteQueryJournal(path, enterprise_id="test-enterprise")
    await append(reopened, event="query_succeeded", upstream_attempted=True)
    assert await reopened.pending_request_ids() == []


@pytest.mark.parametrize("enterprise_id", [None, "other-enterprise"])
@pytest.mark.parametrize("operation", ["prepare", "record"])
async def test_existing_context_cannot_be_adopted_or_changed(tmp_path, enterprise_id, operation):
    path = tmp_path / "queries.sqlite"
    await SQLiteQueryJournal(path, enterprise_id="test-enterprise").prepare()
    before = path.read_bytes()
    other = SQLiteQueryJournal(path, enterprise_id=enterprise_id)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        if operation == "prepare":
            await other.prepare()
        else:
            await append(other)
    assert path.read_bytes() == before


async def test_inspection_with_wrong_explicit_enterprise_is_rejected(tmp_path):
    path = tmp_path / "queries.sqlite"
    await append(SQLiteQueryJournal(path, enterprise_id="test-enterprise"))
    before = path.read_bytes()
    other = SQLiteQueryJournal(path, enterprise_id="other-enterprise")
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await other.pending_request_ids()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await other.check_ready()
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", [True, "", "  ", "test\nenterprise", "x" * 257])
def test_invalid_enterprise_context_is_rejected_without_io(tmp_path, value):
    path = tmp_path / "queries.sqlite"
    with pytest.raises(BusinessError, match="^journal_unavailable$") as error:
        SQLiteQueryJournal(path, enterprise_id=value)
    assert error.value.__context__ is None
    assert not path.exists()


@pytest.mark.parametrize("operation", ["prepare", "record", "check_ready", "pending"])
async def test_v1_is_rejected_before_sqlite_open_without_modification(
    tmp_path, monkeypatch, operation
):
    path = tmp_path / "v1.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE query_events(request_id TEXT,event TEXT)")
        db.execute("INSERT INTO query_events VALUES ('old-request','query_started')")
        db.execute("PRAGMA application_id=1398363185")
        db.execute("PRAGMA user_version=1")
    path.chmod(0o600)
    before = path.read_bytes()
    assert int.from_bytes(before[68:72], "big") == 0x53595431

    def never_open(*args, **kwargs):
        pytest.fail("schema v1 must be rejected before sqlite3.connect")

    monkeypatch.setattr("biz_t100.journal.sqlite3.connect", never_open)
    journal = SQLiteQueryJournal(path, enterprise_id="test-enterprise")
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        if operation == "record":
            await append(journal)
        elif operation == "pending":
            await journal.pending_request_ids()
        else:
            await getattr(journal, operation)()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "code", ["employee_not_found", "employee_not_unique", "upstream_employee_mismatch"]
)
async def test_precise_result_codes_append_without_rewriting_historical_errors(tmp_path, code):
    path = tmp_path / "queries.sqlite"
    journal = SQLiteQueryJournal(path, enterprise_id="ENT-SYNTHETIC")
    await append(journal, request_id="historical")
    await append(
        journal,
        request_id="historical",
        event="query_failed",
        code="upstream_contract_error",
        upstream_attempted=True,
    )
    with sqlite3.connect(path) as db:
        original = db.execute("SELECT * FROM query_events ORDER BY rowid").fetchall()
        schema = db.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall()
    reopened = SQLiteQueryJournal(path, enterprise_id="ENT-SYNTHETIC")
    await append(reopened, request_id="current")
    await append(
        reopened, request_id="current", event="query_failed", code=code, upstream_attempted=True
    )
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT * FROM query_events WHERE request_id='historical' ORDER BY rowid"
            ).fetchall()
            == original
        )
        assert db.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall() == schema
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
        assert db.execute(
            "SELECT code,upstream_attempted,enterprise_id FROM query_events "
            "WHERE request_id='current' AND event='query_failed'"
        ).fetchone() == (code, 1, "ENT-SYNTHETIC")
    assert await reopened.pending_request_ids() == []
