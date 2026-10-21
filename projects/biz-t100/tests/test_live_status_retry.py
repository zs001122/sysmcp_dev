"""One user-reviewed status request, native MCP, and unchanged historical evidence."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_live_cli import LiveCase

from biz_t100.account_write_store import SQLiteAccountWriteStore
from biz_t100.function_audit import FunctionAudit, LegacyStatusRetry
from biz_t100.functions import request_digest
from biz_t100.interfaces import EmployeeUpdateRequest
from biz_t100.models import BusinessError, Caller


@pytest.fixture
async def reviewed_case(tmp_path, monkeypatch):
    case = LiveCase(tmp_path, monkeypatch)
    old = SQLiteAccountWriteStore(
        case.legacy, enterprise_id=case.config.t100.enterprise_id, action="disable_account"
    )
    await old.prepare()
    old_id = str(uuid4())
    await old.reserve(
        operation_id=old_id,
        caller=Caller(
            issuer="https://synthetic.invalid", subject="prior", client_id="prior-client"
        ),
        employee_id="E001",
        account_id="LOGIN-A",
        binding_version="prior-approved-status",
    )
    await old.mark_dispatched(operation_id=old_id)
    await old.finish(operation_id=old_id, outcome="unknown", code="upstream_rejected")
    audit = FunctionAudit(
        case.state / "audit/operations.sqlite3",
        enterprise_id=case.config.t100.enterprise_id,
        legacy_write_path=case.legacy,
    )
    subject = await audit.legacy_review_subject(old_id)
    review = LegacyStatusRetry(
        operation_id=old_id,
        retry_request_id=str(uuid4()),
        enterprise_id=case.config.t100.enterprise_id,
        employee_id="E001",
        account_id="LOGIN-A",
        status="N",
        legacy_fingerprint=subject["fingerprint"],
        approved_by="synthetic-test-operator",
        reason="Operator explicitly permits this single synthetic status request.",
    )
    request = EmployeeUpdateRequest(employee=[{"employee_no": "E001", "status": "N"}])
    assert review.request_digest == request_digest("employee_update", request)
    case.permissions(
        writes_enabled=True,
        write_targets=[
            {
                "action": "employee_update",
                "employee_no": "E001",
                "request_digest": review.request_digest,
            }
        ],
        legacy_status_retry=review.model_dump(mode="json"),
    )
    return case, review, request


async def test_reviewed_live_status_runs_once_and_preserves_original_unknown(reviewed_case):
    case, review, request = reviewed_case
    before = case.legacy.read_bytes()
    result = await case.call("employee_update", request)
    assert result["status"] == "verified", result
    assert result["request_id"] == review.retry_request_id
    assert result["reviewed_legacy_operation_id"] == review.operation_id
    assert result["real_identity_verified"] is False
    assert (result["read_requests"], result["write_requests"]) == (2, 1)
    folder = Path(result["evidence_path"]).parent
    evidence = json.loads((folder / "legacy-status-retry.json").read_text())
    assert evidence["approval"] == review.model_dump(mode="json")
    assert evidence["request_digest"] == review.request_digest
    assert case.legacy.read_bytes() == before
    assert (await case.audit(result))["operation_state"] == "succeeded"
    attempts = len(case.business.requests)
    duplicate = await case.call("employee_update", request)
    assert duplicate["error"] == "reset_already_recorded"
    assert (duplicate["read_requests"], duplicate["write_requests"]) == (0, 0)
    assert len(case.business.requests) == attempts
    assert case.legacy.read_bytes() == before


@pytest.mark.parametrize(
    "row",
    [
        {"employee_no": "E001", "status": "Y"},
        {"employee_no": "E001", "status": "N", "email": "changed@example.invalid"},
        {"employee_no": "E002", "status": "N"},
    ],
)
async def test_request_bound_permission_refuses_other_status_fields_or_employee(reviewed_case, row):
    case, _, _ = reviewed_case
    with pytest.raises(BusinessError, match="^permission_denied$"):
        await case.call("employee_update", EmployeeUpdateRequest(employee=[row]))
    assert case.business.requests == case.business.key_reads == []


async def test_review_cannot_be_used_after_request_scope_is_widened(reviewed_case):
    case, _, _ = reviewed_case
    case.permissions(write_targets=[{"action": "employee_update", "employee_no": "E001"}])
    with pytest.raises(BusinessError, match="^permission_denied$"):
        await case.call(
            "employee_update",
            EmployeeUpdateRequest(employee=[{"employee_no": "E001", "status": "Y"}]),
        )
    assert case.business.requests == case.business.key_reads == []


async def test_removed_review_between_loading_and_first_auth_is_rejected(
    reviewed_case, monkeypatch
):
    case, _, request = reviewed_case
    select = case.host.selected_status_retry

    def revoke(*args):
        review = select(*args)
        case.permissions(legacy_status_retry=None)
        return review

    monkeypatch.setattr(case.host, "selected_status_retry", revoke)
    with pytest.raises(BusinessError, match="^authorization_changed$"):
        await case.call("employee_update", request)
    assert case.business.requests == case.business.key_reads == []


async def test_reviewed_failed_dispatch_is_consumed_and_never_repeated(reviewed_case):
    case, review, request = reviewed_case
    case.business.fail_write = True
    result = await case.call("employee_update", request)
    assert result["request_id"] == review.retry_request_id
    assert result["error"] == "upstream_timeout"
    assert (result["read_requests"], result["write_requests"]) == (1, 1)
    assert (await case.audit(result))["operation_state"] == "unknown"
    attempts = len(case.business.requests)
    duplicate = await case.call("employee_update", request)
    assert duplicate["error"] == "reset_already_recorded"
    assert len(case.business.requests) == attempts


async def test_unreviewed_read_remains_available_with_review_configuration(reviewed_case):
    case, _, _ = reviewed_case
    result = await case.call("employee_query")
    assert result["status"] == "passed"
    assert result["read_requests"] == 1
    assert result["write_requests"] == 0
    assert "reviewed_legacy_operation_id" not in result
