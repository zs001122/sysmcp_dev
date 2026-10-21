"""Status separates configuration permission from durable execution history."""

import asyncio
import importlib
import json
import sqlite3
import sys
from uuid import uuid4

import pytest
from test_live_write import case as case
from test_live_write import live as live

from biz_t100.account_write_store import SQLiteAccountWriteStore
from biz_t100.models import Caller


async def inspect(case, operation_id):
    return await importlib.import_module("live_write_status").inspect_status(
        case.state, case.root, operation_id
    )


async def record(case, operation_id, phase="unknown", *, employee="E001", account="LOGIN-A"):
    saved, _, binding = case.host._load(case.state, case.root, operation_id)
    store = SQLiteAccountWriteStore(
        case.root / "audit" / "account-write.sqlite3",
        enterprise_id=saved.enterprise_id,
        action=saved.plan.action,
    )
    await store.prepare()
    await store.reserve(
        operation_id=operation_id,
        caller=Caller(issuer=case.host.ISSUER, subject=operation_id, client_id=case.host.CLIENT),
        employee_id=employee,
        account_id=account,
        binding_version=binding,
    )
    if phase == "not_sent":
        await store.finish(operation_id=operation_id, outcome=phase, code="permission_denied")
    elif phase != "started":
        await store.mark_dispatched(operation_id=operation_id)
        if phase != "dispatched":
            await store.finish(
                operation_id=operation_id,
                outcome=phase,
                code="upstream_rejected" if phase != "succeeded" else None,
            )
    return store


def snapshot(case):
    return {
        str(path.relative_to(case.root)): path.read_bytes()
        for path in case.root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("approved", [False, True])
async def test_fresh_status_creates_nothing_and_reads_no_credentials(case, monkeypatch, approved):
    operation_id = case.plan()
    if approved:
        case.confirm_contract()
    original = case.host._read_private

    def no_secrets(path, *args):
        assert path.name not in {"service.key", "issuer-private.pem"}
        return original(path, *args)

    monkeypatch.setattr(case.host, "_read_private", no_secrets)
    before = snapshot(case)
    result = await inspect(case, operation_id)
    assert result["status"] == ("planned" if approved else "blocked")
    assert result["ready_for_explicit_call"] is approved
    assert result["operation_state"] == "not_recorded"
    assert result["execution_blocker"] is None
    assert result["configuration_blocker"] == (None if approved else "write_contract_unconfirmed")
    assert result["recorded_possible_dispatch"] is False
    assert result["upstream_attempted"] is False
    assert result["blocking_operation_ids"] == []
    assert case.requests == case.key_reads == []
    assert snapshot(case) == before
    assert not (case.root / "audit" / "account-write.sqlite3").exists()


@pytest.mark.parametrize(
    "phase", ["started", "dispatched", "unknown", "succeeded", "rejected", "not_sent"]
)
async def test_recorded_operation_never_claims_ready_or_business_effect(case, phase):
    operation_id = case.plan()
    case.confirm_contract()
    await record(case, operation_id, phase)
    before = snapshot(case)
    result = await inspect(case, operation_id)
    pending = phase in {"started", "dispatched", "unknown"}
    assert result["status"] == ("blocked" if pending else "completed")
    assert result["blocker"] == ("reset_pending_review" if pending else "reset_already_recorded")
    assert result["execution_blocker"] == result["blocker"]
    assert result["configuration_blocker"] is None
    assert result["operation_state"] == phase
    assert result["ready_for_explicit_call"] is False
    assert result["business_effect_verified"] is False
    assert result["recorded_possible_dispatch"] is (phase not in {"started", "not_sent"})
    assert result["blocking_operation_ids"] == ([operation_id] if pending else [])
    assert result["authorization_binding_matches"] is True
    assert result["upstream_attempted"] is False
    assert case.requests == case.key_reads == []
    assert snapshot(case) == before


@pytest.mark.parametrize("action", ["reset_password", "disable_account", "enable_account"])
@pytest.mark.parametrize("match", ["employee", "account", "neither"])
async def test_pending_lookup_uses_same_employee_or_account_across_actions(case, action, match):
    blocker = case.plan("reset_password")
    operation_id = case.plan(
        action,
        employee_no="E001" if match == "employee" else "E002",
        account_id="LOGIN-A" if match == "account" else "LOGIN-B",
        account_status="Y" if action == "enable_account" else "N",
        upstream_profile=case.host.EMPLOYEE_PROFILE
        if action == "enable_account"
        else case.host.LEGACY_PROFILE,
    )
    case.confirm_contract(
        employee_update_omissions_preserved=True, employee_status_updates_enabled=True
    )
    await record(case, blocker)
    result = await inspect(case, operation_id)
    blocked = match != "neither"
    assert result["status"] == ("blocked" if blocked else "planned")
    assert result["blocking_operation_ids"] == ([blocker] if blocked else [])
    assert result["operation_state"] == "not_recorded"
    assert result["recorded_possible_dispatch"] is False
    assert result["ready_for_explicit_call"] is not blocked
    assert case.requests == case.key_reads == []


async def test_permission_revocation_preserves_historical_pending_evidence(case):
    operation_id = case.plan()
    case.confirm_contract()
    await record(case, operation_id)
    case.change_plan(operation_id, enabled=False)
    result = await inspect(case, operation_id)
    assert result["status"] == "blocked"
    assert result["configuration_blocker"] == result["blocker"] == "permission_denied"
    assert result["execution_blocker"] == "reset_pending_review"
    assert result["blocking_operation_ids"] == [operation_id]
    assert result["operation_state"] == "unknown"
    assert result["upstream_error"] == "upstream_rejected"
    assert result["authorization_binding_matches"] is False
    assert result["ready_for_explicit_call"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("enterprise_id", "OTHER"),
        ("employee_id", "E002"),
        ("account_id", "LOGIN-B"),
        ("tool_name", "reset_my_t100_password"),
        ("issuer", "https://other.example.invalid"),
        ("subject", "other-subject"),
        ("client_id", "other-client"),
        ("code", "private-diagnostic-must-not-escape"),
    ],
)
async def test_status_rejects_inconsistent_operation_identity_or_arbitrary_error(
    case, field, value
):
    operation_id = case.plan()
    case.confirm_contract()
    store = await record(case, operation_id)
    with sqlite3.connect(store._path) as connection:
        connection.execute(
            f"UPDATE reset_operations SET {field}=? WHERE operation_id=?", (value, operation_id)
        )
    result = await inspect(case, operation_id)
    assert result["status"] == "failed"
    assert result["error"] == "journal_unavailable"
    assert result["ready_for_explicit_call"] is False
    assert result["operation_state"] == "unavailable"
    assert result["recorded_possible_dispatch"] is None
    assert result["upstream_error"] is None
    assert "private-diagnostic-must-not-escape" not in str(result)


@pytest.mark.parametrize(
    "damage",
    ["symlink", "dangling-symlink", "directory", "empty", "permissions", "old-version", "schema"],
)
async def test_unavailable_database_never_becomes_fresh_plan(case, damage):
    operation_id = case.plan()
    case.confirm_contract()
    store = await record(case, operation_id)
    path = store._path
    if damage in {"symlink", "dangling-symlink"}:
        target = path.with_name("original.sqlite3")
        path.rename(target)
        path.symlink_to(target if damage == "symlink" else path.with_name("missing.sqlite3"))
    elif damage == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    elif damage == "empty":
        path.write_bytes(b"")
    elif damage == "permissions":
        path.chmod(0o644)
    else:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "PRAGMA user_version=1" if damage == "old-version" else "CREATE TABLE extra(x)"
            )
    result = await inspect(case, operation_id)
    assert result["status"] == "failed"
    assert result["error"] == "journal_unavailable"
    assert result["ready_for_explicit_call"] is False
    if damage == "old-version":
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (1,)


@pytest.mark.parametrize("damage", ["missing-parent", "public-parent", "orphan-journal"])
async def test_missing_database_requires_intact_private_directory(case, damage):
    operation_id = case.plan()
    case.confirm_contract()
    audit = case.root / "audit"
    if damage == "missing-parent":
        audit.rmdir()
    elif damage == "public-parent":
        audit.chmod(0o755)
    else:
        orphan = audit / "account-write.sqlite3-journal"
        orphan.write_bytes(b"synthetic-orphan-journal")
        orphan.chmod(0o600)
    result = await inspect(case, operation_id)
    assert result["status"] == "failed"
    assert result["ready_for_explicit_call"] is False
    assert not (audit / "account-write.sqlite3").exists()


async def test_snapshot_revalidates_schema_after_initial_ready_check(case, monkeypatch):
    operation_id = case.plan()
    case.confirm_contract()
    store = await record(case, operation_id)
    original = SQLiteAccountWriteStore.check_ready

    async def change_schema(instance):
        await original(instance)
        with sqlite3.connect(store._path) as connection:
            connection.execute("CREATE TABLE unrelated(x)")

    monkeypatch.setattr(SQLiteAccountWriteStore, "check_ready", change_schema)
    result = await inspect(case, operation_id)
    assert result["status"] == "failed"
    assert result["error"] == "journal_unavailable"


async def test_malformed_pending_identifier_is_not_exposed(case):
    first = case.plan("reset_password")
    operation_id = case.plan()
    case.confirm_contract()
    store = await record(case, first)
    with sqlite3.connect(store._path) as connection:
        connection.execute(
            "UPDATE reset_operations SET operation_id=? WHERE operation_id=?",
            ("private-corrupt-identifier-" + str(uuid4()), first),
        )
    result = await inspect(case, operation_id)
    assert result["status"] == "failed"
    assert result["blocking_operation_ids"] == []
    assert "private-corrupt-identifier" not in str(result)


def test_status_cli_reports_blocked_and_nonzero_without_calling_upstream(case, monkeypatch, capsys):
    operation_id = case.plan()
    case.confirm_contract()
    asyncio.run(record(case, operation_id))
    before = snapshot(case)
    monkeypatch.setattr(case.host, "ROOT", case.root)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_write.py",
            "--read-state-dir",
            str(case.state.root),
            "status",
            "--operation-id",
            operation_id,
        ],
    )
    assert case.host.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "blocked"
    assert result["ready_for_explicit_call"] is False
    assert result["blocker"] == "reset_pending_review"
    assert result["blocking_operation_ids"] == [operation_id]
    assert result["upstream_attempted"] is False
    assert case.requests == case.key_reads == []
    assert snapshot(case) == before
