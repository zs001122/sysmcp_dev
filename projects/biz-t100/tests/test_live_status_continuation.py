"""Reviewed full status payloads through native MCP; synthetic upstream only."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_functions import CALLER, employee
from test_live_cli import LiveCase

from biz_t100.account_write_store import SQLiteAccountWriteStore
from biz_t100.function_audit import FunctionAudit, ReviewedStatusContinuation
from biz_t100.functions import request_digest
from biz_t100.interfaces import EmployeeUpdateRequest
from biz_t100.models import BusinessError


@pytest.fixture
async def continuation_case(tmp_path, monkeypatch):
    case = LiveCase(tmp_path, monkeypatch)
    audit = FunctionAudit(
        case.state / "audit/operations.sqlite3",
        enterprise_id=case.config.t100.enterprise_id,
        legacy_write_path=case.legacy,
    )
    await audit.prepare()
    previous_id = str(uuid4())
    await audit.start(
        request_id=previous_id,
        caller=CALLER,
        action="employee_update",
        employee_ids=("E001",),
        account_ids=(),
        binding_version="synthetic-prior-permit",
        request_digest="a" * 64,
        write=True,
    )
    await audit.dispatched(previous_id)
    await audit.finish(previous_id, "unknown", "upstream_rejected")
    old = SQLiteAccountWriteStore(
        case.legacy, enterprise_id=case.config.t100.enterprise_id, action="disable_account"
    )
    await old.prepare()
    old_id = str(uuid4())
    await old.reserve(
        operation_id=old_id,
        caller=CALLER,
        employee_id="E001",
        account_id="LOGIN-A",
        binding_version="synthetic-old-permit",
    )
    await old.mark_dispatched(operation_id=old_id)
    await old.finish(operation_id=old_id, outcome="unknown", code="upstream_rejected")
    before = (
        await audit.legacy_review_subject(old_id),
        await audit.review_subject(previous_id),
    )
    requests = {
        status: EmployeeUpdateRequest(employee=[employee(status=status)]) for status in ("N", "Y")
    }
    review = ReviewedStatusContinuation(
        enterprise_id=case.config.t100.enterprise_id,
        employee_id="E001",
        approved_by="synthetic-operator",
        reason="Explicit full-field disable and enable cases; prior outcomes remain unknown.",
        request_digests=tuple(request_digest("employee_update", r) for r in requests.values()),
        records=tuple(
            {"source": source, "request_id": identifier, "fingerprint": subject["fingerprint"]}
            for source, identifier, subject in (
                ("legacy", old_id, before[0]),
                ("function", previous_id, before[1]),
            )
        ),
    )
    case.permissions(
        writes_enabled=True,
        write_targets=[
            {"action": "employee_update", "employee_no": "E001", "request_digest": digest}
            for digest in review.request_digests
        ],
        status_continuation=review.model_dump(mode="json"),
    )
    return case, audit, review, requests, before


async def test_full_disable_enable_readback_preserves_both_unknowns(continuation_case):
    case, audit, review, requests, before = continuation_case
    old_bytes = case.legacy.read_bytes()
    identifiers = []
    for status in ("N", "Y"):
        result = await case.call("employee_update", requests[status])
        assert result["status"] == "verified", result
        assert result["business_effect_verified"] is True
        assert result["real_identity_verified"] is False
        assert (result["read_requests"], result["write_requests"]) == (2, 1)
        assert case.business.rows["E001"]["status"] == status
        identifiers.append(result["request_id"])
        snapshot = json.loads(
            (Path(result["evidence_path"]).parent / "status-continuation.json").read_text()
        )
        assert snapshot["approval"] == review.model_dump(mode="json")
        assert snapshot["request_digest"] == request_digest("employee_update", requests[status])
        assert len(result["reviewed_status_operations"]) == 2
    assert identifiers[0] != identifiers[1]
    assert case.legacy.read_bytes() == old_bytes
    assert await audit.legacy_review_subject(review.records[0].request_id) == before[0]
    assert await audit.review_subject(review.records[1].request_id) == before[1]


@pytest.mark.parametrize(
    "change",
    [
        {"full_name": "Changed"},
        {"department_no": "OTHER"},
        {"email": "changed@example.invalid"},
        {"employee_no": "E002"},
    ],
)
async def test_unapproved_payload_has_no_key_read_or_request(continuation_case, change):
    case, _, _, _, _ = continuation_case
    request = EmployeeUpdateRequest(employee=[employee(status="N", **change)])
    with pytest.raises(BusinessError, match="^permission_denied$"):
        await case.call("employee_update", request)
    assert case.business.requests == case.business.key_reads == []


async def test_new_unknown_still_blocks_enable(continuation_case):
    case, _, _, requests, _ = continuation_case
    case.business.fail_write = True
    result = await case.call("employee_update", requests["N"])
    assert result["error"] == "upstream_timeout"
    assert result["write_requests"] == 1
    attempts = len(case.business.requests)
    case.business.fail_write = False
    blocked = await case.call("employee_update", requests["Y"])
    assert blocked["error"] == "reset_pending_review"
    assert blocked["read_requests"] == blocked["write_requests"] == 0
    assert len(case.business.requests) == attempts


async def test_revoked_continuation_before_auth_is_denied(continuation_case, monkeypatch):
    case, _, _, requests, _ = continuation_case
    select = case.host.selected_status_continuation

    def revoke(*args):
        value = select(*args)
        case.permissions(status_continuation=None)
        return value

    monkeypatch.setattr(case.host, "selected_status_continuation", revoke)
    with pytest.raises(BusinessError, match="^authorization_changed$"):
        await case.call("employee_update", requests["N"])
    assert case.business.requests == case.business.key_reads == []


async def test_cannot_dispatch_without_saved_review(continuation_case, monkeypatch):
    case, _, _, requests, _ = continuation_case
    save = case.host.save

    def refuse(path, value):
        if path.name == "status-continuation.json":
            raise OSError("synthetic audit unavailable")
        return save(path, value)

    monkeypatch.setattr(case.host, "save", refuse)
    with pytest.raises(OSError):
        await case.call("employee_update", requests["N"])
    assert case.business.requests == case.business.key_reads == []


async def test_query_remains_available_and_does_not_claim_review(continuation_case):
    case, _, _, _, _ = continuation_case
    result = await case.call("employee_query")
    assert result["status"] == "passed"
    assert "reviewed_status_operations" not in result
    assert result["read_requests"] == 1
    assert result["write_requests"] == 0


async def test_widened_target_permission_does_not_widen_review(continuation_case):
    case, _, _, _, _ = continuation_case
    case.permissions(write_targets=[{"action": "employee_update", "employee_no": "E001"}])
    request = EmployeeUpdateRequest(employee=[employee(status="N", remark="not approved")])
    with pytest.raises(BusinessError, match="^permission_denied$"):
        await case.call("employee_update", request)
    assert case.business.requests == case.business.key_reads == []


async def test_batch_cannot_reuse_single_target_review(continuation_case):
    case, _, _, _, _ = continuation_case
    case.permissions(
        write_targets=[
            {"action": "employee_update", "employee_no": number} for number in ("E001", "E002")
        ]
    )
    request = EmployeeUpdateRequest(employee=[employee(status="N"), employee("E002", status="N")])
    with pytest.raises(BusinessError, match="^permission_denied$"):
        await case.call("employee_update", request)
    assert case.business.requests == case.business.key_reads == []


async def test_old_and_current_review_cannot_coexist(continuation_case):
    from biz_t100.function_audit import LegacyStatusRetry

    case, _, continuation, requests, _ = continuation_case
    old = LegacyStatusRetry(
        operation_id=continuation.records[0].request_id,
        retry_request_id=str(uuid4()),
        enterprise_id=continuation.enterprise_id,
        employee_id="E001",
        account_id="LOGIN-A",
        status="N",
        legacy_fingerprint=continuation.records[0].fingerprint,
        approved_by="synthetic-operator",
        reason="Consumed historical exception.",
    )
    case.permissions(legacy_status_retry=old.model_dump(mode="json"))
    with pytest.raises(BusinessError, match="^permission_denied$"):
        await case.call("employee_update", requests["N"])
    assert case.business.requests == case.business.key_reads == []
