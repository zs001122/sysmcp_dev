"""未知状态的关联只读核查：合成身份、零重写、原占用与历史保持不变。"""

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from test_live_employee_status_write import PROFILE, QUERY, UPDATE
from test_live_employee_status_write import status_case as status_case
from test_live_write import live as live
from test_live_write_readback import (
    change_base_permission,
    change_operation,
    db_snapshot,
    install_native_reader,
    install_reader,
    read_result,
)


async def unknown_status(case, action="disable_account", target="N"):
    case.enable()
    case.write_timeout = True
    result = await case.direct(action=action, account_status=target)
    assert result["status"] == "failed", result
    assert result["operation_state"] == "unknown"
    assert [body["service"]["name"] for body in case.requests] == [QUERY, UPDATE]
    return result["operation_id"]


def assert_retained(case, before, result):
    assert result["write_upstream_attempted"] is False
    assert result["business_effect_verified"] is False
    assert len(case.requests) == 2, "核查不得再次调用员工资料更新接口"
    assert db_snapshot(case) == before, "核查不得改写原操作、终态或审计事件"
    assert case.rows()[0]["state"] == "unknown"
    assert "synthetic-private-key" not in json.dumps(result)
    assert "PRIVATE KEY" not in json.dumps(result)


@pytest.mark.parametrize(
    "action,target,observed",
    [
        ("disable_account", "N", "Y"),
        ("disable_account", "N", "N"),
        ("enable_account", "Y", "Y"),
        ("enable_account", "Y", "N"),
    ],
)
async def test_review_records_current_state_but_cannot_prove_original_write(
    status_case, monkeypatch, action, target, observed
):
    case = status_case
    operation_id = await unknown_status(case, action, target)
    before = db_snapshot(case)
    reply = read_result(observed)
    calls = install_reader(case, monkeypatch, reply)
    result = await case.host.review_plan(case.state, case.root, operation_id)
    assert result["status"] == "reviewed", result
    assert result["operation_id"] == operation_id
    assert result["observed_account_status"] == observed
    assert result["expected_account_status"] == target
    assert result["account_state_matches"] is (observed == target)
    assert result["pending_review_retained"] is True
    assert result["operation_state"] == "unknown"
    assert result["real_identity_verified"] is False
    assert result["read_request_id"] == reply["request_id"]
    assert calls == ["E001"]
    evidence = Path(result["evidence_path"])
    assert evidence.parent == case.root / "reviews"
    assert evidence.stat().st_mode & 0o777 == 0o600
    assert evidence.parent.stat().st_mode & 0o777 == 0o700
    started = evidence.with_name(evidence.stem + ".started.json")
    assert started.is_file()
    assert started.stat().st_mode & 0o777 == 0o600
    recorded = json.loads(evidence.read_text())
    assert recorded["operation_id"] == operation_id
    assert recorded["read_request_id"] == reply["request_id"]
    assert recorded["pending_review_retained"] is True
    assert recorded["business_effect_verified"] is False
    assert_retained(case, before, result)


async def test_matching_review_retains_cross_action_employee_and_account_lock(
    status_case, monkeypatch
):
    case = status_case
    operation_id = await unknown_status(case)
    before = db_snapshot(case)
    install_reader(case, monkeypatch, read_result("N"))
    review = await case.host.review_plan(case.state, case.root, operation_id)
    assert review["status"] == "reviewed", review
    assert review["account_state_matches"] is True
    for action, target in (
        ("disable_account", "N"),
        ("enable_account", "Y"),
        ("reset_password", "N"),
    ):
        result = await case.direct(
            action=action,
            account_status=target,
            upstream_profile="legacy-account-v1" if action == "reset_password" else PROFILE,
        )
        assert result["status"] == "failed", result
        assert result["error"] == "reset_pending_review"
        assert result["upstream_attempted"] is False
    assert_retained(case, before, review)


@pytest.mark.parametrize("stored_state", ["started", "dispatched", "succeeded", "not_sent"])
async def test_review_rejects_operations_outside_unknown_state(
    status_case, monkeypatch, stored_state
):
    case = status_case
    operation_id = await unknown_status(case)
    change_operation(case, operation_id, "state", stored_state)
    before = db_snapshot(case)
    key_reads = len(case.key_reads)
    calls = install_reader(case, monkeypatch, read_result())
    result = await case.host.review_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["upstream_attempted"] is False
    assert result["write_upstream_attempted"] is False
    assert result["business_effect_verified"] is False
    assert result.get("observed_account_status") is None
    assert calls == []
    assert len(case.key_reads) == key_reads
    assert len(case.requests) == 2
    assert db_snapshot(case) == before


@pytest.mark.parametrize("action", ["reset_password", "disable_account"])
async def test_review_rejects_legacy_account_interface_and_password_write(
    status_case, monkeypatch, action
):
    case = status_case
    case.enable()
    result = await case.direct(action=action, upstream_profile="legacy-account-v1")
    assert result["status"] == "upstream_acknowledged", result
    operation_id = result["operation_id"]
    change_operation(case, operation_id, "state", "unknown")
    change_operation(case, operation_id, "code", "upstream_timeout")
    before = db_snapshot(case)
    calls = install_reader(case, monkeypatch, read_result())
    result = await case.host.review_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["upstream_attempted"] is False
    assert result["business_effect_verified"] is False
    assert calls == []
    assert len(case.requests) == 1
    assert db_snapshot(case) == before


@pytest.mark.parametrize(
    "column,value",
    [
        ("employee_id", "E002"),
        ("account_id", "LOGIN-B"),
        ("enterprise_id", "another-synthetic-enterprise"),
        ("tool_name", "reset_my_t100_password"),
        ("issuer", "https://other-issuer.example.invalid"),
        ("subject", "other-subject"),
        ("client_id", "other-client"),
        ("binding_version", "0" * 64),
    ],
)
async def test_review_checks_entire_original_identity_and_binding_before_read(
    status_case, monkeypatch, column, value
):
    case = status_case
    operation_id = await unknown_status(case)
    change_operation(case, operation_id, column, value)
    before = db_snapshot(case)
    key_reads = len(case.key_reads)
    calls = install_reader(case, monkeypatch, read_result())
    result = await case.host.review_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["upstream_attempted"] is False
    assert result.get("observed_account_status") is None
    assert calls == []
    assert len(case.key_reads) == key_reads
    assert_retained(case, before, result)


@pytest.mark.parametrize("phase", ["before", "after_read"])
@pytest.mark.parametrize("change", ["disabled", "revision"])
async def test_review_rechecks_read_authority_and_withholds_revoked_data(
    status_case, monkeypatch, phase, change
):
    case = status_case
    operation_id = await unknown_status(case)
    before = db_snapshot(case)
    if phase == "before":
        # A changed revision alone is valid at the start; deny the actual permission here.
        change_base_permission(case, "disabled" if change == "disabled" else "permission")
    calls = install_reader(
        case,
        monkeypatch,
        read_result(),
        (lambda: change_base_permission(case, change)) if phase == "after_read" else None,
    )
    result = await case.host.review_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result.get("observed_account_status") is None
    assert result["upstream_attempted"] is (phase == "after_read")
    assert calls == (["E001"] if phase == "after_read" else [])
    assert_retained(case, before, result)


@pytest.mark.parametrize("phase", ["before", "after_read"])
async def test_revoked_write_plan_cannot_deliver_associated_observation(
    status_case, monkeypatch, phase
):
    case = status_case
    operation_id = await unknown_status(case)
    before = db_snapshot(case)

    def revoke():
        case.change_plan(operation_id, enabled=False)

    if phase == "before":
        revoke()
    calls = install_reader(
        case, monkeypatch, read_result(), revoke if phase == "after_read" else None
    )
    result = await case.host.review_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result.get("observed_account_status") is None
    assert result["upstream_attempted"] is (phase == "after_read")
    assert calls == (["E001"] if phase == "after_read" else [])
    assert_retained(case, before, result)


@pytest.mark.parametrize("failure", ["employee_not_found", "upstream_timeout", "audit_unavailable"])
async def test_failed_read_does_not_become_reviewed_account_state(
    status_case, monkeypatch, failure
):
    case = status_case
    operation_id = await unknown_status(case)
    before = db_snapshot(case)
    reply = read_result(status="Y")
    reply.pop("result")
    reply.update(status="failed", error=failure, remote_response_validated=False)
    calls = install_reader(case, monkeypatch, reply)
    result = await case.host.review_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == failure
    assert result.get("observed_account_status") is None
    assert result["upstream_attempted"] is True
    assert calls == ["E001"]
    assert_retained(case, before, result)


@pytest.mark.parametrize("corruption", ["employee", "request_id", "unvalidated", "invalid_status"])
async def test_review_rejects_unvalidated_or_misbound_query_result(
    status_case, monkeypatch, corruption
):
    case = status_case
    operation_id = await unknown_status(case)
    before = db_snapshot(case)
    reply = read_result()
    if corruption == "employee":
        reply["result"]["data"]["employee_id"] = "E002"
    elif corruption == "request_id":
        reply["request_id"] = str(uuid4())
    elif corruption == "unvalidated":
        reply["remote_response_validated"] = False
    else:
        reply["result"]["data"]["employee_status"] = "unknown"
    calls = install_reader(case, monkeypatch, reply)
    result = await case.host.review_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result.get("observed_account_status") is None
    assert result["upstream_attempted"] is True
    assert calls == ["E001"]
    assert_retained(case, before, result)


@pytest.mark.parametrize("phase", ["started", "terminal"])
async def test_failed_review_evidence_never_delivers_account_observation(
    status_case, monkeypatch, phase
):
    case = status_case
    operation_id = await unknown_status(case)
    before = db_snapshot(case)
    calls = install_reader(case, monkeypatch, read_result())
    original_write = case.host._write_new

    def fail_evidence(path, value):
        if path.parent.name == "reviews":
            if path.name.endswith(".started.json") == (phase == "started"):
                raise OSError("synthetic-review-storage-error")
        return original_write(path, value)

    monkeypatch.setattr(case.host, "_write_new", fail_evidence)
    result = await case.host.review_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == "journal_unavailable"
    assert result.get("observed_account_status") is None
    assert result["upstream_attempted"] is (phase == "terminal")
    assert calls == (["E001"] if phase == "terminal" else [])
    assert "synthetic-review-storage-error" not in json.dumps(result)
    assert_retained(case, before, result)


async def test_review_native_query_is_signed_exactly_targeted_and_independently_audited(
    status_case, monkeypatch
):
    case = status_case
    operation_id = await unknown_status(case)
    before = db_snapshot(case)
    queries = install_native_reader(case, monkeypatch)
    arguments = []
    module = importlib.import_module("live_employee_call")
    original_rpc = module._rpc

    async def capture(client, method, params, identifier):
        if method == "tools/call":
            arguments.append(params["arguments"])
        return await original_rpc(client, method, params, identifier)

    monkeypatch.setattr(module, "_rpc", capture)
    result = await case.host.review_plan(case.state, case.root, operation_id)
    assert result["status"] == "reviewed", result
    assert result["observed_account_status"] == "N"
    assert result["account_state_matches"] is True
    assert result["real_identity_verified"] is False
    assert len(queries) == 1
    assert arguments == [{}], "员工号仅来自受控映射，不成为远程工具参数"
    journals = list((case.state.root / "operator-cases").glob("*/audit/query.sqlite3"))
    assert len(journals) == 1
    with sqlite3.connect(journals[0]) as db:
        rows = db.execute(
            "SELECT event,request_id,enterprise_id,employee_id FROM query_events ORDER BY rowid"
        ).fetchall()
    assert [row[0] for row in rows] == ["query_started", "query_succeeded"]
    assert all(
        row[1:] == (result["read_request_id"], "synthetic-enterprise", "E001") for row in rows
    )
    assert_retained(case, before, result)


@pytest.mark.parametrize("status,exit_code", [("reviewed", 0), ("failed", 1)])
def test_review_command_routes_to_readonly_review(
    status_case, monkeypatch, capsys, status, exit_code
):
    case = status_case
    operation_id = str(uuid4())
    calls = []

    async def review(state, root, identifier):
        calls.append((state, root, identifier))
        return {"status": status, "business_effect_verified": False, "operation_id": identifier}

    monkeypatch.setattr(case.host, "review_plan", review)
    monkeypatch.setattr(case.host, "load_live", lambda root: case.state)
    monkeypatch.setattr(sys, "argv", ["live_write.py", "review", "--operation-id", operation_id])
    assert case.host.main() == exit_code
    assert calls == [(case.state, case.host.ROOT, operation_id)]
    assert json.loads(capsys.readouterr().out)["status"] == status
