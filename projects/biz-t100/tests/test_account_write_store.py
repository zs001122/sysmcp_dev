"""共享账号维护占用：合成目标，跨动作并发与不可解锁的未知结果。"""

import os
import sqlite3
import stat
from uuid import uuid4

import anyio
import pytest

from biz_t100.account_write_store import SQLiteAccountWriteStore
from biz_t100.journal import SQLiteQueryJournal
from biz_t100.models import BusinessError, Caller
from biz_t100.reset_store import SQLiteResetStore

ACTIONS = ("reset_password", "disable_account", "enable_account")
TOOLS = {
    "reset_password": "reset_my_t100_password",
    "disable_account": "disable_my_t100_account",
    "enable_account": "enable_my_t100_account",
}
ENTERPRISE = "synthetic-enterprise"
CALLER = Caller(issuer="https://identity.invalid", subject="user-a", client_id="client-a")
OTHER = Caller(issuer="https://other.invalid", subject="user-b", client_id="client-b")


def store(path, action="reset_password", enterprise=ENTERPRISE):
    return SQLiteAccountWriteStore(path, enterprise_id=enterprise, action=action)


async def reserve(db, operation_id=None, *, employee="E001", account="A001", caller=CALLER):
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
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT state,code FROM reset_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()


@pytest.mark.parametrize("action", ACTIONS)
async def test_action_evidence_and_private_shared_schema(tmp_path, action):
    path = tmp_path / "private" / "writes.sqlite3"
    db = store(path, action)
    await db.prepare()
    await db.check_ready()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    operation_id = await reserve(db)
    await db.mark_dispatched(operation_id=operation_id)
    await db.finish(operation_id=operation_id, outcome="succeeded")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (0x53594131,)
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("SELECT * FROM reset_context").fetchall() == [
            (1, ENTERPRISE, "t100_account_maintenance")
        ]
        rows = connection.execute(
            "SELECT issuer,subject,client_id,enterprise_id,employee_id,account_id,"
            "tool_name,binding_version,event FROM reset_events ORDER BY rowid"
        ).fetchall()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(reset_events)")}
        assert not columns & {"key", "token", "credential", "payload", "response"}
    identity = (
        CALLER.issuer,
        CALLER.subject,
        CALLER.client_id,
        ENTERPRISE,
        "E001",
        "A001",
        TOOLS[action],
        "synthetic-policy-version",
    )
    assert rows == [
        (*identity, "reset_started"),
        (*identity, "reset_dispatched"),
        (*identity, "reset_succeeded"),
    ]
    for other_action in ACTIONS:
        await store(path, other_action).check_ready()
        assert await store(path, other_action).pending_operation_ids() == []


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("phase", ["started", "dispatched", "unknown"])
@pytest.mark.parametrize("target", ["employee", "account"])
async def test_pending_lock_crosses_action_caller_and_binding_change(
    tmp_path, action, phase, target
):
    path = tmp_path / "writes.sqlite3"
    db = store(path, action)
    operation_id = await reserve(db)
    if phase != "started":
        await db.mark_dispatched(operation_id=operation_id)
    if phase == "unknown":
        await db.finish(operation_id=operation_id, outcome="unknown", code="reset_outcome_unknown")
    for other_action in ACTIONS:
        if other_action == action:
            continue
        other = store(path, other_action)
        with pytest.raises(BusinessError, match="^reset_pending_review$"):
            await other.reserve(
                operation_id=str(uuid4()),
                caller=OTHER,
                employee_id="E001" if target == "employee" else "E002",
                account_id="A001" if target == "account" else "A002",
                binding_version="changed-policy-version",
            )
        assert await other.pending_operation_ids() == [operation_id]
    assert state(path, operation_id)[0] == phase


@pytest.mark.parametrize("same_operation", [False, True])
@pytest.mark.parametrize("target", ["employee", "account"])
async def test_concurrent_actions_have_one_winner(tmp_path, same_operation, target):
    path = tmp_path / "writes.sqlite3"
    await store(path).prepare()
    operation_id = str(uuid4())
    results = []

    async def contender(index):
        try:
            await reserve(
                store(path, ACTIONS[index]),
                operation_id if not index or same_operation else str(uuid4()),
                employee="E002" if index and target == "account" else "E001",
                account="A002" if index and target == "employee" else "A001",
                caller=OTHER if index else CALLER,
            )
            results.append("reserved")
        except BusinessError as exc:
            results.append(exc.code)

    async with anyio.create_task_group() as group:
        for index in range(len(ACTIONS)):
            group.start_soon(contender, index)
    conflict = "reset_already_recorded" if same_operation else "reset_pending_review"
    assert sorted(results) == sorted(["reserved", conflict, conflict])
    assert len(await store(path).pending_operation_ids()) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reset_events").fetchone() == (1,)


@pytest.mark.parametrize("outcome", ["succeeded", "rejected", "not_sent"])
async def test_known_result_releases_target_but_uuid_is_unique_across_actions(tmp_path, outcome):
    path = tmp_path / "writes.sqlite3"
    reset = store(path)
    disable = store(path, "disable_account")
    operation_id = await reserve(reset)
    if outcome != "not_sent":
        await reset.mark_dispatched(operation_id=operation_id)
    await reset.finish(operation_id=operation_id, outcome=outcome)
    with pytest.raises(BusinessError, match="^reset_already_recorded$"):
        await reserve(disable, operation_id, employee="E002", account="A002", caller=OTHER)
    new_id = await reserve(disable)
    assert await reset.pending_operation_ids() == [new_id]


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("transition", ["dispatch", "not_sent", "succeeded", "unknown"])
async def test_one_action_cannot_advance_other_action_operation(tmp_path, action, transition):
    path = tmp_path / "writes.sqlite3"
    db = store(path, action)
    operation_id = await reserve(db)
    if transition in {"succeeded", "unknown"}:
        await db.mark_dispatched(operation_id=operation_id)
    before = path.read_bytes()
    for other_action in ACTIONS:
        if other_action == action:
            continue
        with pytest.raises(BusinessError, match="^journal_unavailable$") as error:
            if transition == "dispatch":
                await store(path, other_action).mark_dispatched(operation_id=operation_id)
            else:
                await store(path, other_action).finish(
                    operation_id=operation_id, outcome=transition
                )
        assert error.value.__context__ is None
    assert path.read_bytes() == before
    assert await db.pending_operation_ids() == [operation_id]


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("outcome", ["succeeded", "not_sent", "rejected", "unknown"])
async def test_unknown_cannot_be_reclassified_or_unlocked(tmp_path, action, outcome):
    path = tmp_path / "writes.sqlite3"
    db = store(path, action)
    operation_id = await reserve(db)
    await db.mark_dispatched(operation_id=operation_id)
    await db.finish(operation_id=operation_id, outcome="unknown", code="upstream_timeout")
    before = path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await store(path, action).finish(operation_id=operation_id, outcome=outcome)
    assert path.read_bytes() == before
    assert await db.pending_operation_ids() == [operation_id]


async def test_pending_lists_three_actions_and_unrelated_targets_can_run(tmp_path):
    path = tmp_path / "writes.sqlite3"
    reset_id = await reserve(store(path))
    disable_id = await reserve(
        store(path, "disable_account"), employee="E002", account="A002", caller=OTHER
    )
    enable_id = await reserve(store(path, "enable_account"), employee="E003", account="A003")
    for action in ACTIONS:
        assert await store(path, action).pending_operation_ids() == sorted(
            [reset_id, disable_id, enable_id]
        )


@pytest.mark.parametrize("factory", [SQLiteQueryJournal, SQLiteResetStore])
@pytest.mark.parametrize("operation", ["prepare", "reserve", "check_ready", "pending"])
async def test_old_reset_or_read_database_rejected_before_sqlite_open(
    tmp_path, monkeypatch, factory, operation
):
    path = tmp_path / "foreign.sqlite3"
    await factory(path, enterprise_id=ENTERPRISE).prepare()
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


@pytest.mark.parametrize("factory", [SQLiteQueryJournal, SQLiteResetStore])
async def test_original_stores_cannot_read_or_write_account_store(tmp_path, factory):
    path = tmp_path / "writes.sqlite3"
    await reserve(store(path, "disable_account"))
    before = path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await factory(path, enterprise_id=ENTERPRISE).prepare()
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["enterprise", "schema", "context"])
async def test_context_and_schema_are_fixed(tmp_path, change):
    path = tmp_path / "writes.sqlite3"
    await reserve(store(path))
    if change != "enterprise":
        with sqlite3.connect(path) as connection:
            if change == "schema":
                connection.execute("CREATE TABLE unexpected (value TEXT)")
            else:
                connection.execute("UPDATE reset_context SET enterprise_id='other'")
    before = path.read_bytes()
    db = store(path, enterprise="other" if change == "enterprise" else ENTERPRISE)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await db.prepare()
    assert path.read_bytes() == before


@pytest.mark.parametrize("action", [None, {}, "", "reset", "DISABLE_ACCOUNT"])
def test_invalid_action_has_no_filesystem_effect(tmp_path, action):
    path = tmp_path / "writes.sqlite3"
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        store(path, action)
    assert not path.exists()


@pytest.mark.parametrize("kind", ["file", "parent", "sidecar", "hardlink", "mode", "directory"])
async def test_private_paths_inherited_for_both_actions(tmp_path, kind):
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    path = folder / "writes.sqlite3"
    await store(path).prepare()
    if kind == "file":
        alias = folder / "alias.sqlite3"
        alias.symlink_to(path)
        path = alias
    elif kind == "parent":
        alias = tmp_path / "alias"
        alias.symlink_to(folder, target_is_directory=True)
        path = alias / path.name
    elif kind == "sidecar":
        path.with_name(path.name + "-journal").symlink_to(tmp_path / "elsewhere")
    elif kind == "hardlink":
        os.link(path, folder / "alias.sqlite3")
    elif kind == "mode":
        path.chmod(0o644)
    else:
        folder.chmod(0o755)
    for action in ACTIONS:
        with pytest.raises(BusinessError, match="^journal_unavailable$"):
            await reserve(store(path, action))


async def test_terminal_commit_failure_preserves_cross_action_lock(tmp_path, monkeypatch):
    path = tmp_path / "writes.sqlite3"
    db = store(path, "disable_account")
    operation_id = await reserve(db)
    await db.mark_dispatched(operation_id=operation_id)
    connect = sqlite3.connect

    class CommitFailure:
        def __init__(self, *args, **kwargs):
            self.inner = connect(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def commit(self):
            raise sqlite3.OperationalError("private details must not escape")

    with monkeypatch.context() as scoped:
        scoped.setattr("biz_t100.reset_store.sqlite3.connect", CommitFailure)
        with pytest.raises(BusinessError, match="^journal_unavailable$") as error:
            await db.finish(operation_id=operation_id, outcome="succeeded")
        assert error.value.__context__ is None
    assert state(path, operation_id) == ("dispatched", None)
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await reserve(store(path))


# 固定的已交付 SYA1 v1 结构；故意不从当前实现的迁移常量拼接历史库。
_LEGACY_CONTEXT = """CREATE TABLE reset_context (
 id INTEGER PRIMARY KEY CHECK(id=1),
 enterprise_id TEXT NOT NULL,
 tool_name TEXT NOT NULL CHECK(tool_name='t100_account_maintenance')
)"""
_LEGACY_OPERATIONS = """CREATE TABLE reset_operations (
 operation_id TEXT PRIMARY KEY,
 issuer TEXT NOT NULL,
 subject TEXT NOT NULL,
 client_id TEXT NOT NULL,
 enterprise_id TEXT NOT NULL,
 employee_id TEXT NOT NULL,
 account_id TEXT NOT NULL,
 tool_name TEXT NOT NULL CHECK(tool_name IN
 ('reset_my_t100_password','disable_my_t100_account')),
 binding_version TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN
 ('started','dispatched','succeeded','rejected','not_sent','unknown')),
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 code TEXT
)"""
_LEGACY_EVENTS = """CREATE TABLE reset_events (
 operation_id TEXT NOT NULL REFERENCES reset_operations(operation_id),
 issuer TEXT NOT NULL,
 subject TEXT NOT NULL,
 client_id TEXT NOT NULL,
 enterprise_id TEXT NOT NULL,
 employee_id TEXT NOT NULL,
 account_id TEXT NOT NULL,
 tool_name TEXT NOT NULL CHECK(tool_name IN
 ('reset_my_t100_password','disable_my_t100_account')),
 binding_version TEXT NOT NULL,
 event TEXT NOT NULL CHECK(event IN
 ('reset_started','reset_dispatched','reset_succeeded','reset_rejected','reset_not_sent','reset_unknown')),
 occurred_at REAL NOT NULL,
 code TEXT,
 PRIMARY KEY(operation_id,event)
)"""
_LEGACY_EMPLOYEE_INDEX = """CREATE UNIQUE INDEX one_pending_employee
 ON reset_operations(enterprise_id,employee_id)
 WHERE state IN ('started','dispatched','unknown')"""
_LEGACY_ACCOUNT_INDEX = """CREATE UNIQUE INDEX one_pending_account
 ON reset_operations(enterprise_id,account_id)
 WHERE state IN ('started','dispatched','unknown')"""


class LegacyAccountStore(SQLiteResetStore):
    _application_id = 0x53594131
    _version = 1
    _context_tool_name = "t100_account_maintenance"
    _schema_statements = (
        _LEGACY_CONTEXT,
        _LEGACY_OPERATIONS,
        _LEGACY_EVENTS,
        _LEGACY_EMPLOYEE_INDEX,
        _LEGACY_ACCOUNT_INDEX,
    )
    _schema_definition = {
        ("table", "reset_context", _LEGACY_CONTEXT),
        ("table", "reset_operations", _LEGACY_OPERATIONS),
        ("table", "reset_events", _LEGACY_EVENTS),
        ("index", "one_pending_employee", _LEGACY_EMPLOYEE_INDEX),
        ("index", "one_pending_account", _LEGACY_ACCOUNT_INDEX),
    }

    def __init__(self, path, *, action="reset_password"):
        assert action in {"reset_password", "disable_account"}
        self._tool_name = TOOLS[action]
        super().__init__(path, enterprise_id=ENTERPRISE)


def all_rows(path):
    with sqlite3.connect(path) as connection:
        return tuple(
            tuple(connection.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid"))
            for table in ("reset_context", "reset_operations", "reset_events")
        )


async def legacy_history(path):
    reset = LegacyAccountStore(path)
    ack = await reserve(reset)
    await reset.mark_dispatched(operation_id=ack)
    await reset.finish(operation_id=ack, outcome="succeeded")
    disable = LegacyAccountStore(path, action="disable_account")
    unknown = await reserve(disable, employee="E002", account="A002")
    await disable.mark_dispatched(operation_id=unknown)
    await disable.finish(operation_id=unknown, outcome="unknown", code="upstream_timeout")
    return ack, unknown


async def test_explicit_migration_preserves_ack_unknown_and_all_audit_rows(tmp_path):
    path = tmp_path / "writes.sqlite3"
    ack, unknown = await legacy_history(path)
    before = all_rows(path)
    inode = path.stat().st_ino
    upgraded = store(path, "enable_account")
    await upgraded.prepare()
    assert path.stat().st_ino == inode
    assert all_rows(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (0x53594131,)
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert state(path, ack) == ("succeeded", None)
    assert state(path, unknown) == ("unknown", "upstream_timeout")
    for action in ACTIONS:
        current = store(path, action)
        await current.check_ready()
        assert await current.pending_operation_ids() == [unknown]
        with pytest.raises(BusinessError, match="^reset_already_recorded$"):
            await reserve(current, ack, employee="E003", account="A003")
        with pytest.raises(BusinessError, match="^reset_pending_review$"):
            await reserve(current, employee="E002", account="A003")
        with pytest.raises(BusinessError, match="^reset_pending_review$"):
            await reserve(current, employee="E003", account="A002")
    enabled = await reserve(upgraded, employee="E003", account="A003")
    await upgraded.mark_dispatched(operation_id=enabled)
    await upgraded.finish(operation_id=enabled, outcome="succeeded")
    assert state(path, enabled) == ("succeeded", None)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("operation", ["check_ready", "pending", "reserve", "finish"])
async def test_v1_is_only_migrated_by_explicit_prepare(tmp_path, operation):
    path = tmp_path / "writes.sqlite3"
    _, unknown = await legacy_history(path)
    before = path.read_bytes()
    upgraded = store(path, "enable_account")
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        if operation == "pending":
            await upgraded.pending_operation_ids()
        elif operation == "reserve":
            await reserve(upgraded, employee="E003", account="A003")
        elif operation == "finish":
            await upgraded.finish(operation_id=unknown, outcome="succeeded")
        else:
            await upgraded.check_ready()
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["prepare", "check_ready", "pending", "reserve"])
async def test_old_binary_rejects_v2_instead_of_ignoring_enable_locks(tmp_path, operation):
    path = tmp_path / "writes.sqlite3"
    await legacy_history(path)
    await store(path, "enable_account").prepare()
    await reserve(store(path, "enable_account"), employee="E003", account="A003")
    before = path.read_bytes()
    legacy = LegacyAccountStore(path)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        if operation == "reserve":
            await reserve(legacy, employee="E003", account="A003")
        elif operation == "pending":
            await legacy.pending_operation_ids()
        else:
            await getattr(legacy, operation)()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    ["enterprise", "context", "extra-table", "index", "version", "row-enterprise", "event-binding"],
)
async def test_migration_refuses_foreign_or_nonexact_legacy_database(tmp_path, damage):
    path = tmp_path / "writes.sqlite3"
    await legacy_history(path)
    if damage != "enterprise":
        with sqlite3.connect(path) as connection:
            if damage == "context":
                connection.execute("UPDATE reset_context SET enterprise_id='foreign'")
            elif damage == "extra-table":
                connection.execute("CREATE TABLE unexpected (value TEXT)")
            elif damage == "index":
                connection.execute("DROP INDEX one_pending_employee")
                connection.execute(
                    "CREATE INDEX one_pending_employee ON reset_operations(employee_id)"
                )
            elif damage == "version":
                connection.execute("PRAGMA user_version=77")
            elif damage == "row-enterprise":
                connection.execute("UPDATE reset_operations SET enterprise_id='foreign'")
            elif damage == "event-binding":
                connection.execute("UPDATE reset_events SET binding_version='wrong-binding'")
    before = path.read_bytes()
    db = store(path, "enable_account", "other-enterprise" if damage == "enterprise" else ENTERPRISE)
    with pytest.raises(BusinessError, match="^journal_unavailable$") as error:
        await db.prepare()
    assert error.value.__context__ is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["copy", "index", "commit", "directory-fsync"])
async def test_migration_failure_rolls_back_history_schema_and_locks(
    tmp_path, monkeypatch, failure
):
    path = tmp_path / "writes.sqlite3"
    _, unknown = await legacy_history(path)
    before = path.read_bytes()
    before_rows = all_rows(path)
    original_connect = sqlite3.connect

    class FailingMigration:
        def __init__(self, *args, **kwargs):
            self.inner = original_connect(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def execute(self, statement, *args):
            if failure == "copy" and statement.startswith("INSERT INTO reset_events("):
                raise sqlite3.OperationalError("synthetic-private-copy-failure")
            if failure == "index" and statement.startswith("CREATE UNIQUE INDEX"):
                raise sqlite3.OperationalError("synthetic-private-index-failure")
            return self.inner.execute(statement, *args)

        def commit(self):
            if failure == "commit":
                raise sqlite3.OperationalError("synthetic-private-commit-failure")
            return self.inner.commit()

    def fail_sync(path):
        raise OSError("synthetic-private-directory-sync-failure")

    with monkeypatch.context() as scoped:
        scoped.setattr("biz_t100.account_write_store.sqlite3.connect", FailingMigration)
        if failure == "directory-fsync":
            scoped.setattr(SQLiteAccountWriteStore, "_sync_directory", staticmethod(fail_sync))
        with pytest.raises(BusinessError, match="^journal_unavailable$") as error:
            await store(path, "enable_account").prepare()
        assert error.value.__context__ is None
    assert path.read_bytes() == before
    assert all_rows(path) == before_rows
    legacy = LegacyAccountStore(path)
    await legacy.check_ready()
    assert await legacy.pending_operation_ids() == [unknown]
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await reserve(legacy, employee="E002", account="A002")
    await store(path, "enable_account").prepare()
    assert all_rows(path) == before_rows


async def test_concurrent_prepare_migrates_once_without_changing_history(tmp_path):
    path = tmp_path / "writes.sqlite3"
    _, unknown = await legacy_history(path)
    before = all_rows(path)
    prepared = []

    async def prepare(action):
        db = store(path, action)
        await db.prepare()
        assert await db.pending_operation_ids() == [unknown]
        prepared.append(action)

    async with anyio.create_task_group() as group:
        for action in ACTIONS:
            group.start_soon(prepare, action)
    assert sorted(prepared) == sorted(ACTIONS)
    assert all_rows(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
