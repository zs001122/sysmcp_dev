"""重置证据测试只使用合成身份与私有临时数据库。"""

import os
import sqlite3
import stat
import threading
from uuid import uuid4

import anyio
import pytest

from biz_t100.journal import SQLiteQueryJournal
from biz_t100.models import BusinessError, Caller
from biz_t100.reset_store import SQLiteResetStore

CALLER = Caller(issuer="https://identity.invalid", subject="user-one", client_id="client-one")
OTHER = Caller(issuer="https://other-identity.invalid", subject="user-one", client_id="client-two")
ENTERPRISE = "synthetic-enterprise"
TOOL = "reset_my_t100_password"


def store(path, enterprise=ENTERPRISE):
    return SQLiteResetStore(path, enterprise_id=enterprise)


async def reserve(db, operation_id=None, *, caller=CALLER, employee="E001", account="A001"):
    operation_id = operation_id or str(uuid4())
    await db.reserve(
        operation_id=operation_id,
        caller=caller,
        employee_id=employee,
        account_id=account,
        binding_version="synthetic-policy-version",
    )
    return operation_id


def state(path, operation_id):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT state,code FROM reset_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()


def events(path, operation_id):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT event FROM reset_events WHERE operation_id=? ORDER BY rowid", (operation_id,)
        ).fetchall()


async def test_prepare_private_schema_and_reopen_preserves_complete_identity(tmp_path):
    path = tmp_path / "private" / "reset.sqlite"
    db = store(path)
    assert not path.exists()
    await db.prepare()
    await db.check_ready()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (0x53595231,)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("SELECT * FROM reset_context").fetchall() == [
            (1, ENTERPRISE, TOOL)
        ]
        assert connection.execute("SELECT COUNT(*) FROM reset_operations").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM reset_events").fetchone() == (0,)
        names = {row[1] for row in connection.execute("PRAGMA table_info(reset_events)")}
        assert not names & {"key", "credential", "token", "payload", "response", "reason"}
    operation_id = await reserve(db)
    with sqlite3.connect(path) as connection:
        identity = connection.execute(
            "SELECT issuer,subject,client_id,enterprise_id,employee_id,account_id,tool_name,"
            "binding_version FROM reset_events WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    assert identity == (
        CALLER.issuer,
        CALLER.subject,
        CALLER.client_id,
        ENTERPRISE,
        "E001",
        "A001",
        TOOL,
        "synthetic-policy-version",
    )
    before = path.read_bytes()
    assert await store(path).pending_operation_ids() == [operation_id]
    await store(path).check_ready()
    assert path.read_bytes() == before


@pytest.mark.parametrize("outcome", ["succeeded", "rejected", "not_sent"])
async def test_known_outcome_releases_target_but_never_reuses_operation_id(tmp_path, outcome):
    path = tmp_path / "reset.sqlite"
    db = store(path)
    operation_id = await reserve(db)
    if outcome != "not_sent":
        await db.mark_dispatched(operation_id=operation_id)
    await db.finish(operation_id=operation_id, outcome=outcome)
    assert state(path, operation_id) == (outcome, None)
    expected = [("reset_started",)]
    if outcome != "not_sent":
        expected.append(("reset_dispatched",))
    assert events(path, operation_id) == [*expected, ("reset_" + outcome,)]
    assert await store(path).pending_operation_ids() == []
    with pytest.raises(BusinessError, match="^reset_already_recorded$"):
        await reserve(store(path), operation_id, caller=OTHER, employee="E999", account="A999")
    new_id = await reserve(store(path))
    assert await db.pending_operation_ids() == [new_id]


@pytest.mark.parametrize("phase", ["started", "dispatched", "unknown"])
@pytest.mark.parametrize("target", ["employee", "account"])
async def test_pending_blocks_changed_id_identity_or_binding_after_restart(tmp_path, phase, target):
    path = tmp_path / "reset.sqlite"
    db = store(path)
    operation_id = await reserve(db)
    if phase != "started":
        await db.mark_dispatched(operation_id=operation_id)
    if phase == "unknown":
        await db.finish(operation_id=operation_id, outcome="unknown", code="reset_outcome_unknown")
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await store(path).reserve(
            operation_id=str(uuid4()),
            caller=OTHER,
            employee_id="E001" if target == "employee" else "E999",
            account_id="A001" if target == "account" else "A999",
            binding_version="changed-policy-version",
        )
    assert await store(path).pending_operation_ids() == [operation_id]
    assert state(path, operation_id)[0] == phase


async def test_same_subject_different_issuer_keeps_distinct_evidence(tmp_path):
    path = tmp_path / "reset.sqlite"
    db = store(path)
    first = await reserve(db)
    second = await reserve(db, caller=OTHER, employee="E002", account="A002")
    await db.mark_dispatched(operation_id=second)
    await db.finish(operation_id=second, outcome="unknown", code="upstream_contract_error")
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT operation_id,issuer,subject,client_id,employee_id,account_id "
            "FROM reset_operations"
        ).fetchall()
    assert set(rows) == {
        (first, CALLER.issuer, CALLER.subject, CALLER.client_id, "E001", "A001"),
        (second, OTHER.issuer, OTHER.subject, OTHER.client_id, "E002", "A002"),
    }
    assert await db.pending_operation_ids() == sorted([first, second])


@pytest.mark.parametrize(
    "initial,next_state",
    [
        ("started", "succeeded"),
        ("started", "rejected"),
        ("started", "unknown"),
        ("dispatched", "not_sent"),
        ("dispatched", "dispatched"),
        ("unknown", "succeeded"),
        ("unknown", "rejected"),
        ("unknown", "not_sent"),
        ("unknown", "dispatched"),
        ("succeeded", "unknown"),
        ("not_sent", "dispatched"),
    ],
)
async def test_invalid_transition_is_fail_closed_and_preserves_events(
    tmp_path, initial, next_state
):
    path = tmp_path / "reset.sqlite"
    db = store(path)
    operation_id = await reserve(db)
    if initial in {"dispatched", "unknown", "succeeded"}:
        await db.mark_dispatched(operation_id=operation_id)
    if initial in {"unknown", "succeeded", "not_sent"}:
        await db.finish(operation_id=operation_id, outcome=initial)
    before = path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$") as error:
        if next_state == "dispatched":
            await db.mark_dispatched(operation_id=operation_id)
        else:
            await db.finish(operation_id=operation_id, outcome=next_state)
    assert error.value.__context__ is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("same_operation", [True, False])
@pytest.mark.parametrize("same_target", ["employee", "account"])
async def test_concurrent_reservations_across_instances_have_only_one_winner(
    tmp_path, same_operation, same_target
):
    path = tmp_path / "reset.sqlite"
    await store(path).prepare()
    operation_id = str(uuid4())
    results = []

    async def contender(second):
        try:
            await reserve(
                store(path),
                operation_id if same_operation or not second else str(uuid4()),
                caller=OTHER if second else CALLER,
                employee="E002" if second and same_target == "account" else "E001",
                account="A002" if second and same_target == "employee" else "A001",
            )
            results.append("reserved")
        except BusinessError as exc:
            results.append(exc.code)

    async with anyio.create_task_group() as group:
        group.start_soon(contender, False)
        group.start_soon(contender, True)
    expected = "reset_already_recorded" if same_operation else "reset_pending_review"
    assert sorted(results) == sorted(["reserved", expected])
    assert len(await store(path).pending_operation_ids()) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reset_events").fetchone() == (1,)


@pytest.mark.parametrize("stage", ["reserve", "dispatch", "finish"])
async def test_commit_failure_never_reports_success_or_releases_target(
    tmp_path, monkeypatch, stage
):
    path = tmp_path / "reset.sqlite"
    db = store(path)
    await db.prepare()
    operation_id = str(uuid4())
    if stage != "reserve":
        await reserve(db, operation_id)
    if stage == "finish":
        await db.mark_dispatched(operation_id=operation_id)
    previous = state(path, operation_id)
    prior_events = events(path, operation_id)
    connect = sqlite3.connect

    class CommitFailure:
        def __init__(self, *args, **kwargs):
            self.inner = connect(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def commit(self):
            raise sqlite3.OperationalError("private SQL key and path must not escape")

    with monkeypatch.context() as scoped:
        scoped.setattr("biz_t100.reset_store.sqlite3.connect", CommitFailure)
        with pytest.raises(BusinessError, match="^journal_unavailable$") as error:
            if stage == "reserve":
                await reserve(db, operation_id)
            elif stage == "dispatch":
                await db.mark_dispatched(operation_id=operation_id)
            else:
                await db.finish(operation_id=operation_id, outcome="succeeded")
        assert error.value.__context__ is None
    assert state(path, operation_id) == previous
    assert events(path, operation_id) == prior_events
    assert await store(path).pending_operation_ids() == (
        [] if stage == "reserve" else [operation_id]
    )


async def test_event_insert_failure_rolls_back_state_change(tmp_path, monkeypatch):
    path = tmp_path / "reset.sqlite"
    db = store(path)
    operation_id = await reserve(db)
    connect = sqlite3.connect

    class EventFailure:
        def __init__(self, *args, **kwargs):
            self.inner = connect(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO reset_events"):
                raise sqlite3.OperationalError("private payload")
            return self.inner.execute(sql, *args)

    with monkeypatch.context() as scoped:
        scoped.setattr("biz_t100.reset_store.sqlite3.connect", EventFailure)
        with pytest.raises(BusinessError, match="^journal_unavailable$"):
            await db.mark_dispatched(operation_id=operation_id)
    assert state(path, operation_id) == ("started", None)
    assert events(path, operation_id) == [("reset_started",)]


@pytest.mark.parametrize("operation", ["prepare", "reserve", "check_ready", "pending"])
async def test_query_database_rejected_before_sqlite_open_and_remains_unchanged(
    tmp_path, monkeypatch, operation
):
    path = tmp_path / "query.sqlite"
    await SQLiteQueryJournal(path, enterprise_id=ENTERPRISE).prepare()
    before = path.read_bytes()

    def never_open(*args, **kwargs):
        pytest.fail("foreign application must be rejected before sqlite3.connect")

    monkeypatch.setattr("biz_t100.reset_store.sqlite3.connect", never_open)
    db = store(path)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        if operation == "reserve":
            await reserve(db)
        elif operation == "pending":
            await db.pending_operation_ids()
        else:
            await getattr(db, operation)()
    assert path.read_bytes() == before


async def test_query_journal_cannot_write_reset_database(tmp_path):
    path = tmp_path / "reset.sqlite"
    await store(path).prepare()
    before = path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await SQLiteQueryJournal(path, enterprise_id=ENTERPRISE).prepare()
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["prepare", "reserve", "check_ready", "pending"])
async def test_wrong_enterprise_context_cannot_read_or_write_store(tmp_path, operation):
    path = tmp_path / "reset.sqlite"
    await reserve(store(path))
    before = path.read_bytes()
    db = store(path, enterprise="other-enterprise")
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        if operation == "reserve":
            await reserve(db)
        elif operation == "pending":
            await db.pending_operation_ids()
        else:
            await getattr(db, operation)()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "not-a-uuid"),
        ("operation_id", str(uuid4()).upper()),
        ("employee_id", "employee\nsecret"),
        ("account_id", "account secret"),
        ("binding_version", "policy\nsecret"),
        ("binding_version", ""),
    ],
)
async def test_invalid_reservation_metadata_never_creates_database(tmp_path, field, value):
    path = tmp_path / "reset.sqlite"
    values = dict(
        operation_id=str(uuid4()),
        caller=CALLER,
        employee_id="E001",
        account_id="A001",
        binding_version="policy-version",
    )
    values[field] = value
    with pytest.raises(BusinessError, match="^journal_unavailable$") as error:
        await store(path).reserve(**values)
    assert error.value.__context__ is None
    assert not path.exists()


@pytest.mark.parametrize(
    "outcome,code",
    [
        ("unknown", "secret-token"),
        ("unknown", {}),
        ("replay", None),
        ("succeeded", "reset_failed"),
    ],
)
async def test_invalid_outcome_or_code_preserves_dispatched(tmp_path, outcome, code):
    path = tmp_path / "reset.sqlite"
    db = store(path)
    operation_id = await reserve(db)
    await db.mark_dispatched(operation_id=operation_id)
    before = path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await db.finish(operation_id=operation_id, outcome=outcome, code=code)
    assert path.read_bytes() == before
    assert await db.pending_operation_ids() == [operation_id]


@pytest.mark.parametrize(
    "kind", ["file", "parent", "sidecar", "hardlink", "permissions", "directory"]
)
async def test_symlinks_hardlinks_and_shared_permissions_rejected(tmp_path, kind):
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    path = folder / "reset.sqlite"
    await store(path).prepare()
    if kind == "file":
        alias = folder / "alias.sqlite"
        alias.symlink_to(path)
        path = alias
    elif kind == "parent":
        alias = tmp_path / "alias"
        alias.symlink_to(folder, target_is_directory=True)
        path = alias / path.name
    elif kind == "sidecar":
        path.with_name(path.name + "-journal").symlink_to(tmp_path / "elsewhere")
    elif kind == "hardlink":
        os.link(path, folder / "other-name")
    elif kind == "permissions":
        path.chmod(0o644)
    else:
        folder.chmod(0o755)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await reserve(store(path))


@pytest.mark.parametrize("operation", ["check_ready", "pending"])
async def test_missing_read_only_inspection_does_not_create_store(tmp_path, operation):
    path = tmp_path / "missing" / "reset.sqlite"
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        if operation == "pending":
            await store(path).pending_operation_ids()
        else:
            await store(path).check_ready()
    assert not path.parent.exists()


async def test_cancelled_worker_finishes_commit_without_blocking_loop(tmp_path, monkeypatch):
    path = tmp_path / "reset.sqlite"
    db = store(path)
    entered, release = threading.Event(), threading.Event()
    original = db._reserve
    operation_id = str(uuid4())

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        original(*args)

    monkeypatch.setattr(db, "_reserve", delayed)
    finished = anyio.Event()
    with anyio.CancelScope() as scope:
        async with anyio.create_task_group() as group:

            async def writer():
                await reserve(db, operation_id)
                finished.set()

            group.start_soon(writer)
            with anyio.fail_after(2):
                while not entered.is_set():
                    await anyio.sleep(0.001)
            assert not finished.is_set()
            scope.cancel()
            release.set()
    assert finished.is_set()
    assert await store(path).pending_operation_ids() == [operation_id]


async def test_new_directory_parents_and_database_entry_are_synced(tmp_path, monkeypatch):
    path = tmp_path / "parent" / "leaf" / "reset.sqlite"
    observed = []
    original = os.fsync

    def synced(descriptor):
        metadata = os.fstat(descriptor)
        assert stat.S_ISDIR(metadata.st_mode)
        observed.append(metadata.st_ino)
        return original(descriptor)

    monkeypatch.setattr("biz_t100.reset_store.os.fsync", synced)
    await store(path).prepare()
    assert observed == [
        tmp_path.stat().st_ino,
        path.parent.parent.stat().st_ino,
        path.parent.stat().st_ino,
    ]
