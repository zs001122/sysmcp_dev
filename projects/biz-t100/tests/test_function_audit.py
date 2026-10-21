"""四接口操作审计：合成身份、跨动作占用与旧库只读兼容。"""

import json
import os
import sqlite3
import stat
from uuid import uuid4

import anyio
import pytest

from biz_t100.account_write_store import SQLiteAccountWriteStore
from biz_t100.function_audit import FunctionAudit, LegacyEmployeeUpdateRecord
from biz_t100.journal import SQLiteQueryJournal
from biz_t100.models import BusinessError, Caller
from biz_t100.reset_store import SQLiteResetStore

ENTERPRISE = "synthetic-enterprise"
CALLER = Caller(issuer="https://synthetic.invalid", subject="operator-a", client_id="test-client")
ACTIONS = ("employee_query", "employee_create", "employee_update", "account_maintain")
DIGEST = "a" * 64


def audit(path, legacy=None, enterprise=ENTERPRISE):
    return FunctionAudit(path, enterprise_id=enterprise, legacy_write_path=legacy)


async def start(db, action="account_maintain", **changes):
    values = dict(
        request_id=str(uuid4()),
        caller=CALLER,
        action=action,
        employee_ids=("E001",),
        account_ids=("A001",),
        binding_version="synthetic-policy",
        request_digest=DIGEST,
        write=action != "employee_query",
    )
    values.update(changes)
    await db.start(**values)
    return values["request_id"]


async def legacy_store(path, phase="unknown", action="disable_account"):
    db = SQLiteAccountWriteStore(path, enterprise_id=ENTERPRISE, action=action)
    await db.prepare()
    identifier = str(uuid4())
    await db.reserve(
        operation_id=identifier,
        caller=CALLER,
        employee_id="E001",
        account_id="A001",
        binding_version="historical-policy",
    )
    if phase != "started":
        await db.mark_dispatched(operation_id=identifier)
    if phase not in {"started", "dispatched"}:
        await db.finish(
            operation_id=identifier,
            outcome=phase,
            code="reset_outcome_unknown" if phase == "unknown" else None,
        )
    return identifier


@pytest.mark.parametrize("action", ACTIONS)
async def test_durable_identity_events_and_private_files(tmp_path, action):
    path = tmp_path / "private" / "functions.sqlite3"
    db = audit(path)
    await db.prepare()
    await db.check_ready()
    identifier = await start(db, action)
    await db.dispatched(identifier)
    await db.finish(identifier, "succeeded", None)
    result = await audit(path).status(identifier)
    assert result["request_id"] == identifier
    assert result["action"] == action
    assert result["issuer"] == CALLER.issuer
    assert result["subject"] == CALLER.subject
    assert result["client_id"] == CALLER.client_id
    assert result["enterprise_id"] == ENTERPRISE
    assert result["employee_ids"] == ["E001"]
    assert result["account_ids"] == ["A001"]
    assert result["binding_version"] == "synthetic-policy"
    assert result["request_digest"] == DIGEST
    assert result["operation_state"] == "succeeded"
    assert result["upstream_attempted"] is True
    assert result["recorded_possible_dispatch"] is True
    assert [e["event"] for e in result["events"]] == ["started", "dispatched", "succeeded"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (0x53594631,)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        for table in ("function_operations", "function_events"):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert not columns & {
                "key",
                "token",
                "credential",
                "payload",
                "response",
                "name",
                "email",
            }


@pytest.mark.parametrize("phase", ["started", "dispatched", "unknown"])
@pytest.mark.parametrize("action", ACTIONS[1:])
@pytest.mark.parametrize("target", ["employee", "account"])
async def test_batch_target_intersection_blocks_cross_action(tmp_path, phase, action, target):
    db = audit(tmp_path / "functions.sqlite3")
    await db.prepare()
    identifier = await start(
        db, action, employee_ids=("E001", "E002"), account_ids=("A001", "A002")
    )
    if phase != "started":
        await db.dispatched(identifier)
    if phase == "unknown":
        await db.finish(identifier, "unknown", "reset_outcome_unknown")
    for other in ACTIONS[1:]:
        with pytest.raises(BusinessError, match="^reset_pending_review$"):
            await start(
                db,
                other,
                employee_ids=("E002",) if target == "employee" else ("E003",),
                account_ids=("A002",) if target == "account" else ("A003",),
                binding_version="changed-binding",
                request_digest="b" * 64,
                caller=CALLER.model_copy(update={"subject": "other-operator"}),
            )
    assert (await db.status(identifier))["operation_state"] == phase
    await start(db, employee_ids=("E003",), account_ids=("A003",))


@pytest.mark.parametrize("targets", [(), ("E001",)])
async def test_queries_do_not_lock_or_obey_write_locks(tmp_path, targets):
    db = audit(tmp_path / "functions.sqlite3")
    await db.prepare()
    first = await start(db, "employee_query", employee_ids=targets, account_ids=())
    second = await start(db)
    await db.dispatched(second)
    await db.finish(second, "unknown", "upstream_timeout")
    third = await start(db, "employee_query", employee_ids=targets, account_ids=())
    await db.dispatched(first)
    await db.finish(first, "succeeded")
    await db.dispatched(third)
    await db.finish(third, "succeeded")


async def test_unknown_employee_update_does_not_block_account_object(tmp_path):
    path = tmp_path / "functions.sqlite3"
    db = audit(path)
    await db.prepare()
    prior = await start(
        db,
        "employee_update",
        employee_ids=("E001",),
        account_ids=(),
    )
    await db.dispatched(prior)
    await db.finish(prior, "unknown", "upstream_verification_failed")

    await start(
        db,
        "account_maintain",
        employee_ids=("E001",),
        account_ids=("A001",),
        request_digest="b" * 64,
    )
    assert (await db.status(prior))["operation_state"] == "unknown"


@pytest.mark.parametrize("phase", ["started", "dispatched"])
async def test_inflight_employee_update_still_blocks_account_object(tmp_path, phase):
    db = audit(tmp_path / "functions.sqlite3")
    await db.prepare()
    prior = await start(
        db,
        "employee_update",
        employee_ids=("E001",),
        account_ids=(),
    )
    if phase == "dispatched":
        await db.dispatched(prior)
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(
            db,
            "account_maintain",
            employee_ids=("E001",),
            account_ids=("A001",),
            request_digest="b" * 64,
        )


@pytest.mark.parametrize("same_id", [False, True])
async def test_independent_connections_reserve_atomically(tmp_path, same_id):
    path = tmp_path / "functions.sqlite3"
    await audit(path).prepare()
    fixed_id = str(uuid4())
    results = []

    async def contender(action):
        try:
            await start(audit(path), action, request_id=fixed_id if same_id else str(uuid4()))
            results.append("started")
        except BusinessError as exc:
            results.append(exc.code)

    async with anyio.create_task_group() as group:
        for action in ACTIONS[1:]:
            group.start_soon(contender, action)
    conflict = "reset_already_recorded" if same_id else "reset_pending_review"
    assert sorted(results) == sorted(["started", conflict, conflict])


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
async def test_known_terminal_releases_target_but_never_uuid(tmp_path, outcome):
    db = audit(tmp_path / "functions.sqlite3")
    await db.prepare()
    identifier = await start(db)
    await db.dispatched(identifier)
    await db.finish(identifier, outcome)
    await start(db, "employee_update")
    with pytest.raises(BusinessError, match="^reset_already_recorded$"):
        await start(db, "employee_query", request_id=identifier)


async def test_failed_before_dispatch_records_zero_attempt(tmp_path):
    db = audit(tmp_path / "functions.sqlite3")
    await db.prepare()
    identifier = await start(db)
    await db.finish(identifier, "failed", "authorization_changed")
    result = await db.status(identifier)
    assert result["upstream_attempted"] is False
    assert result["code"] == "authorization_changed"
    assert [e["event"] for e in result["events"]] == ["started", "failed"]
    await start(db)


@pytest.mark.parametrize(
    ("phase", "outcome"),
    [
        (phase, outcome)
        for phase in ("started", "dispatched", "unknown", "succeeded", "failed")
        for outcome in ("dispatched", "succeeded", "failed", "unknown")
        if (phase, outcome)
        not in {
            ("started", "dispatched"),
            ("started", "failed"),
            ("dispatched", "succeeded"),
            ("dispatched", "failed"),
            ("dispatched", "unknown"),
        }
    ],
)
async def test_state_machine_refuses_invalid_transition_without_mutation(tmp_path, phase, outcome):
    path = tmp_path / "functions.sqlite3"
    db = audit(path)
    await db.prepare()
    identifier = await start(db)
    if phase != "started":
        await db.dispatched(identifier)
    if phase in {"unknown", "succeeded", "failed"}:
        await db.finish(identifier, phase)
    before = path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$") as exc:
        if outcome == "dispatched":
            await db.dispatched(identifier)
        else:
            await db.finish(identifier, outcome)
    assert exc.value.__context__ is None
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "not-uuid"},
        {"action": "reset_password"},
        {"write": False},
        {"write": 1},
        {"employee_ids": ["E001"]},
        {"account_ids": ("../secret",)},
        {"employee_ids": ("E001", "E001")},
        {"employee_ids": (), "account_ids": ()},
        {"request_digest": "arbitrary-secret"},
        {"request_digest": "A" * 64},
        {"binding_version": "bad\nbinding"},
        {"caller": {"subject": "not-Caller"}},
    ],
)
async def test_input_validation_is_fail_closed(tmp_path, change):
    path = tmp_path / "functions.sqlite3"
    db = audit(path)
    await db.prepare()
    before = path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await start(db, **change)
    assert path.read_bytes() == before


@pytest.mark.parametrize("phase", ["started", "dispatched", "unknown"])
@pytest.mark.parametrize("target", ["employee", "account"])
async def test_legacy_pending_preserved_readonly_across_actions(tmp_path, phase, target):
    old_path = tmp_path / "legacy.sqlite3"
    await legacy_store(old_path, phase)
    before = old_path.read_bytes()
    db = audit(tmp_path / "functions.sqlite3", old_path)
    await db.prepare()
    await db.check_ready()
    for action in ACTIONS[1:]:
        with pytest.raises(BusinessError, match="^reset_pending_review$"):
            await start(
                db,
                action,
                employee_ids=("E001",) if target == "employee" else ("E002",),
                account_ids=("A001",) if target == "account" else ("A002",),
            )
    await start(db, "employee_query")
    await start(db, employee_ids=("E002",), account_ids=("A002",))
    assert old_path.read_bytes() == before


async def test_legacy_known_result_and_uuid_check(tmp_path):
    old_path = tmp_path / "legacy.sqlite3"
    prior_id = await legacy_store(old_path, "succeeded")
    before = old_path.read_bytes()
    db = audit(tmp_path / "functions.sqlite3", old_path)
    await db.prepare()
    with pytest.raises(BusinessError, match="^reset_already_recorded$"):
        await start(db, request_id=prior_id)
    await start(db)
    assert old_path.read_bytes() == before


async def test_classified_legacy_employee_update_does_not_block_account_object(tmp_path):
    old_path = tmp_path / "legacy.sqlite3"
    prior_id = await legacy_store(old_path, "unknown", action="disable_account")
    path = tmp_path / "functions.sqlite3"
    inspector = audit(path, old_path)
    await inspector.prepare()
    subject = await inspector.legacy_review_subject(prior_id)
    classification = LegacyEmployeeUpdateRecord(
        request_id=prior_id,
        enterprise_id=ENTERPRISE,
        employee_id="E001",
        fingerprint=subject["fingerprint"],
    )
    before = old_path.read_bytes()
    db = FunctionAudit(
        path,
        enterprise_id=ENTERPRISE,
        legacy_write_path=old_path,
        legacy_employee_updates=(classification,),
    )
    await start(
        db,
        "account_maintain",
        employee_ids=("E001",),
        account_ids=("A001",),
    )
    assert old_path.read_bytes() == before


async def test_invalid_legacy_employee_classification_fails_closed(tmp_path):
    old_path = tmp_path / "legacy.sqlite3"
    prior_id = await legacy_store(old_path, "unknown", action="disable_account")
    db = FunctionAudit(
        tmp_path / "functions.sqlite3",
        enterprise_id=ENTERPRISE,
        legacy_write_path=old_path,
        legacy_employee_updates=(
            LegacyEmployeeUpdateRecord(
                request_id=prior_id,
                enterprise_id=ENTERPRISE,
                employee_id="E001",
                fingerprint="0" * 64,
            ),
        ),
    )
    await db.prepare()
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(db, "account_maintain")


async def test_fresh_installation_legacy_absent_does_not_create_old_paths(tmp_path):
    old_path = tmp_path / "absent" / "legacy.sqlite3"
    db = audit(tmp_path / "functions.sqlite3", old_path)
    await db.prepare()
    await db.check_ready()
    await start(db)
    assert not old_path.parent.exists()


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
async def test_absent_legacy_with_sidecar_rejected(tmp_path, suffix):
    old_path = tmp_path / "legacy.sqlite3"
    old_path.with_name(old_path.name + suffix).touch(mode=0o600)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await audit(tmp_path / "functions.sqlite3", old_path).prepare()


async def test_legacy_deletion_or_omission_after_prepare_cannot_drop_unknown_locks(tmp_path):
    old_path = tmp_path / "legacy.sqlite3"
    await legacy_store(old_path)
    path = tmp_path / "functions.sqlite3"
    await audit(path, old_path).prepare()
    old_path.unlink()
    for old in (old_path, None):
        with pytest.raises(BusinessError, match="^journal_unavailable$"):
            await audit(path, old).prepare()
        with pytest.raises(BusinessError, match="^journal_unavailable$"):
            await start(audit(path, old))


@pytest.mark.parametrize("factory", [SQLiteQueryJournal, SQLiteResetStore])
async def test_wrong_legacy_schema_rejected_without_changes(tmp_path, factory):
    old_path = tmp_path / "legacy.sqlite3"
    await factory(old_path, enterprise_id=ENTERPRISE).prepare()
    before = old_path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await audit(tmp_path / "functions.sqlite3", old_path).prepare()
    assert old_path.read_bytes() == before


async def test_legacy_version_one_not_automatically_migrated(tmp_path):
    old_path = tmp_path / "legacy.sqlite3"
    await legacy_store(old_path)
    with sqlite3.connect(old_path) as connection:
        connection.execute("PRAGMA user_version=1")
    before = old_path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await audit(tmp_path / "functions.sqlite3", old_path).prepare()
    assert old_path.read_bytes() == before


@pytest.mark.parametrize("location", ["main", "ancestor", "sidecar", "legacy"])
async def test_symlinks_rejected(tmp_path, location):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "functions.sqlite3"
    old_path = tmp_path / "legacy.sqlite3"
    if location == "main":
        target = tmp_path / "target.sqlite3"
        await audit(target).prepare()
        path.symlink_to(target)
    elif location == "ancestor":
        linked = tmp_path / "linked"
        linked.symlink_to(private, target_is_directory=True)
        path = linked / "functions.sqlite3"
    elif location == "sidecar":
        await audit(path).prepare()
        target = tmp_path / "target"
        target.touch(mode=0o600)
        path.with_name(path.name + "-journal").symlink_to(target)
    else:
        target = tmp_path / "old.sqlite3"
        await legacy_store(target)
        old_path.symlink_to(target)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await audit(path, old_path if location == "legacy" else None).prepare()


@pytest.mark.parametrize("location", ["main", "parent", "legacy"])
async def test_nonprivate_modes_rejected(tmp_path, location):
    path = tmp_path / "private" / "functions.sqlite3"
    old_path = tmp_path / "legacy.sqlite3"
    if location == "legacy":
        await legacy_store(old_path)
        old_path.chmod(0o644)
        db = audit(path, old_path)
    else:
        db = audit(path)
        await db.prepare()
        (path if location == "main" else path.parent).chmod(0o644 if location == "main" else 0o755)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await db.check_ready() if location != "legacy" else await db.prepare()


async def test_hardlinked_file_and_enterprise_mismatch_rejected(tmp_path):
    path = tmp_path / "functions.sqlite3"
    await audit(path).prepare()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await audit(path, enterprise="another-enterprise").check_ready()
    os.link(path, tmp_path / "hardlink.sqlite3")
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await audit(path).check_ready()


async def test_audit_failure_does_not_reclassify_or_leak_raw_code(tmp_path):
    path = tmp_path / "functions.sqlite3"
    db = audit(path)
    await db.prepare()
    identifier = await start(db)
    await db.dispatched(identifier)
    before = path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$") as exc:
        await db.finish(identifier, "failed", "secret-upstream-message")
    assert exc.value.__context__ is None
    assert path.read_bytes() == before
    assert "secret-upstream-message" not in json.dumps(await db.status(identifier))
    path.chmod(0o644)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await db.finish(identifier, "succeeded")
    path.chmod(0o600)
    assert (await db.status(identifier))["operation_state"] == "dispatched"


async def test_check_ready_does_not_create_or_status_unknown_record(tmp_path):
    path = tmp_path / "missing" / "functions.sqlite3"
    db = audit(path)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await db.check_ready()
    assert not path.parent.exists()
    await db.prepare()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await db.status(str(uuid4()))


@pytest.mark.parametrize("mutation", ["enterprise", "write_flag", "orphan", "extra_table"])
async def test_corrupt_audit_context_or_schema_fails_closed(tmp_path, mutation):
    path = tmp_path / "functions.sqlite3"
    db = audit(path)
    await db.prepare()
    identifier = await start(db)
    with sqlite3.connect(path) as connection:
        if mutation == "enterprise":
            connection.execute("UPDATE function_operations SET enterprise_id='other-enterprise'")
        elif mutation == "write_flag":
            connection.execute("UPDATE function_operations SET is_write=0")
        elif mutation == "orphan":
            connection.execute(
                "INSERT INTO function_events VALUES (?,'started',1,NULL)", (str(uuid4()),)
            )
        else:
            connection.execute("CREATE TABLE extra(secret TEXT)")
    before = path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await db.status(identifier)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await db.check_ready()
    assert path.read_bytes() == before
