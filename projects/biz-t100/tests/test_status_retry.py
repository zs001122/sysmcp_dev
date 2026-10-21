"""显式复核只允许一次精确状态写入，不改变旧 unknown 事实。"""

import sqlite3
from uuid import uuid4

import anyio
import pytest
from pydantic import ValidationError

from biz_t100.account_write_store import SQLiteAccountWriteStore
from biz_t100.function_audit import FunctionAudit, LegacyStatusRetry
from biz_t100.functions import request_digest
from biz_t100.interfaces import EmployeeUpdateRequest
from biz_t100.models import BusinessError, Caller

ENTERPRISE = "SYNTHETIC-ENTERPRISE"
CALLER = Caller(issuer="https://synthetic.invalid", subject="operator", client_id="test-client")


def audit(path, old_path, review=None):
    return FunctionAudit(
        path, enterprise_id=ENTERPRISE, legacy_write_path=old_path, legacy_status_retry=review
    )


async def old_operation(path, *, phase="unknown", action="disable_account", employee="E001"):
    store = SQLiteAccountWriteStore(path, enterprise_id=ENTERPRISE, action=action)
    await store.prepare()
    identifier = str(uuid4())
    await store.reserve(
        operation_id=identifier,
        caller=CALLER,
        employee_id=employee,
        account_id="LOGIN-" + employee,
        binding_version="synthetic-old-policy",
    )
    if phase != "started":
        await store.mark_dispatched(operation_id=identifier)
    if phase not in {"started", "dispatched"}:
        await store.finish(
            operation_id=identifier,
            outcome=phase,
            code="upstream_timeout" if phase == "unknown" else None,
        )
    return identifier


async def review_for(path, old_path, identifier, status="N"):
    prior = await audit(path, old_path).legacy_review_subject(identifier)
    return LegacyStatusRetry(
        operation_id=identifier,
        retry_request_id=str(uuid4()),
        enterprise_id=ENTERPRISE,
        employee_id=prior["employee_id"],
        account_id=prior["account_id"],
        status=status,
        legacy_fingerprint=prior["fingerprint"],
        approved_by="synthetic-workspace-user",
        reason="Synthetic explicit approval for one status assignment; old result stays unknown.",
    )


async def start(db, review, **changes):
    values = dict(
        request_id=review.retry_request_id,
        caller=CALLER,
        action="employee_update",
        employee_ids=(review.employee_id,),
        account_ids=(),
        binding_version="synthetic-review-policy",
        request_digest=review.request_digest,
        write=True,
    )
    values.update(changes)
    await db.start(**values)


@pytest.mark.parametrize("status", ["Y", "N"])
@pytest.mark.parametrize("action", ["disable_account", "enable_account"])
async def test_precise_review_allows_once_and_preserves_old_database(tmp_path, status, action):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path, action=action)
    before = old_path.read_bytes()
    review = await review_for(path, old_path, identifier, status)
    assert not path.exists()  # Forming the review is purely read-only.
    expected = EmployeeUpdateRequest.model_validate(
        {"employee": [{"employee_no": "E001", "status": status}]}
    )
    assert review.request_digest == request_digest("employee_update", expected)
    db = audit(path, old_path, review)
    await db.prepare()
    await start(db, review)
    await db.dispatched(review.retry_request_id)
    await db.finish(review.retry_request_id, "succeeded")
    current = await db.status(review.retry_request_id)
    assert current["request_digest"] == review.request_digest
    assert current["operation_state"] == "succeeded"
    assert current["employee_ids"] == ["E001"]
    assert current["account_ids"] == []
    prior = await db.legacy_review_subject(identifier)
    assert prior["state"] == "unknown"
    assert prior["fingerprint"] == review.legacy_fingerprint
    assert old_path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } == {"function_context", "function_operations", "function_events"}


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "00000000-0000-0000-0000-000000000001"},
        {"action": "employee_create"},
        {"action": "account_maintain"},
        {"employee_ids": ("E002",)},
        {"employee_ids": ("E001", "E002")},
        {"account_ids": ("LOGIN-E001",)},
        {"request_digest": "f" * 64},
    ],
)
async def test_review_cannot_be_used_with_different_request_context(tmp_path, change):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path)
    review = await review_for(path, old_path, identifier)
    db = audit(path, old_path, review)
    await db.prepare()
    before = path.read_bytes(), old_path.read_bytes()
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(db, review, **change)
    assert (path.read_bytes(), old_path.read_bytes()) == before


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "Y"},
        {"email": "synthetic@example.invalid"},
        {"modify_no": "SYNTHETIC-EDITOR"},
    ],
)
async def test_review_digest_excludes_opposite_status_and_additional_modifications(
    tmp_path, fields
):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path)
    review = await review_for(path, old_path, identifier)
    request = EmployeeUpdateRequest.model_validate(
        {"employee": [{"employee_no": "E001", "status": "N", **fields}]}
    )
    db = audit(path, old_path, review)
    await db.prepare()
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(db, review, request_digest=request_digest("employee_update", request))


@pytest.mark.parametrize(
    "change",
    [
        {"operation_id": "00000000-0000-0000-0000-000000000001"},
        {"account_id": "DIFFERENT-LOGIN"},
        {"legacy_fingerprint": "f" * 64},
    ],
)
async def test_mismatched_legacy_review_is_rejected(tmp_path, change):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path)
    original = await review_for(path, old_path, identifier)
    review = LegacyStatusRetry.model_validate(original.model_dump() | change)
    db = audit(path, old_path, review)
    await db.prepare()
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(db, review)


@pytest.mark.parametrize("phase", ["started", "dispatched", "succeeded", "rejected"])
async def test_review_only_applies_to_unknown_legacy_state(tmp_path, phase):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path, phase=phase)
    review = await review_for(path, old_path, identifier)
    db = audit(path, old_path, review)
    await db.prepare()
    before = old_path.read_bytes()
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(db, review)
    assert old_path.read_bytes() == before


async def test_unknown_password_reset_is_never_covered_by_status_review(tmp_path):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path, action="reset_password")
    review = await review_for(path, old_path, identifier)
    db = audit(path, old_path, review)
    await db.prepare()
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(db, review)


@pytest.mark.parametrize("target", ["operation", "event"])
async def test_complete_legacy_history_fingerprint_detects_changes(tmp_path, target):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path)
    review = await review_for(path, old_path, identifier)
    with sqlite3.connect(old_path) as connection:
        if target == "operation":
            connection.execute("UPDATE reset_operations SET updated_at=updated_at+1")
        else:
            connection.execute(
                "UPDATE reset_events SET code='upstream_rejected' WHERE event='reset_unknown'"
            )
    before = old_path.read_bytes()
    db = audit(path, old_path, review)
    await db.prepare()
    assert (await db.legacy_review_subject(identifier))["fingerprint"] != review.legacy_fingerprint
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(db, review)
    assert old_path.read_bytes() == before


@pytest.mark.parametrize("phase", ["started", "dispatched", "unknown"])
async def test_existing_new_pending_is_never_overridden(tmp_path, phase):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    db = audit(path, old_path)
    await db.prepare()
    pending = str(uuid4())
    await db.start(
        request_id=pending,
        caller=CALLER,
        action="employee_update",
        employee_ids=("E001",),
        account_ids=(),
        binding_version="synthetic-policy",
        request_digest="a" * 64,
        write=True,
    )
    if phase != "started":
        await db.dispatched(pending)
    if phase == "unknown":
        await db.finish(pending, "unknown", "upstream_timeout")
    identifier = await old_operation(old_path)
    review = await review_for(path, old_path, identifier)
    reviewed = audit(path, old_path, review)
    await reviewed.prepare()
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(reviewed, review)
    assert (await reviewed.status(pending))["operation_state"] == phase


async def test_review_does_not_clear_other_legacy_pending(tmp_path):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path)
    other_id = await old_operation(old_path, employee="E002")
    before = old_path.read_bytes()
    review = await review_for(path, old_path, identifier)
    db = audit(path, old_path, review)
    await db.prepare()
    await start(db, review)
    await db.dispatched(review.retry_request_id)
    await db.finish(review.retry_request_id, "succeeded")
    plain = audit(path, old_path)
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await plain.start(
            request_id=str(uuid4()),
            caller=CALLER,
            action="employee_update",
            employee_ids=("E002",),
            account_ids=(),
            binding_version="synthetic-policy",
            request_digest="a" * 64,
            write=True,
        )
    assert (await db.legacy_review_subject(other_id))["state"] == "unknown"
    assert old_path.read_bytes() == before


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "unknown"])
async def test_review_is_consumed_even_when_retry_fails(tmp_path, outcome):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path)
    review = await review_for(path, old_path, identifier)
    db = audit(path, old_path, review)
    await db.prepare()
    await start(db, review)
    if outcome != "failed":
        await db.dispatched(review.retry_request_id)
    await db.finish(review.retry_request_id, outcome)
    with pytest.raises(BusinessError, match="^reset_already_recorded$"):
        await start(audit(path, old_path, review), review)
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(audit(path, old_path, review), review, request_id=str(uuid4()))


async def test_concurrent_use_of_same_approval_has_one_winner(tmp_path):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path)
    review = await review_for(path, old_path, identifier)
    await audit(path, old_path, review).prepare()
    results = []

    async def reserve():
        try:
            await start(audit(path, old_path, review), review)
            results.append("started")
        except BusinessError as exc:
            results.append(exc.code)

    async with anyio.create_task_group() as group:
        for _ in range(3):
            group.start_soon(reserve)
    assert sorted(results) == ["reset_already_recorded", "reset_already_recorded", "started"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM function_operations").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM function_events").fetchone() == (1,)


@pytest.mark.parametrize(
    "change",
    [
        {"operation_id": "not-a-uuid"},
        {"retry_request_id": "NOT-A-UUID"},
        {"enterprise_id": "  "},
        {"employee_id": "../E001"},
        {"account_id": "../LOGIN"},
        {"status": "disabled"},
        {"status": True},
        {"legacy_fingerprint": "not-a-digest"},
        {"approved_by": "  "},
        {"reason": "  "},
        {"reason": "unsafe\\nnewline".replace("\\n", "\n")},
        {"reason": "x" * 1025},
        {"extra": True},
    ],
)
async def test_review_schema_is_strict_and_bounded(tmp_path, change):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path)
    review = await review_for(path, old_path, identifier)
    with pytest.raises(ValidationError):
        LegacyStatusRetry.model_validate(review.model_dump() | change)


async def test_retry_uuid_must_differ_and_digest_cannot_be_set(tmp_path):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path)
    review = await review_for(path, old_path, identifier)
    with pytest.raises(ValidationError):
        LegacyStatusRetry.model_validate(review.model_dump() | {"retry_request_id": identifier})
    with pytest.raises(ValidationError):
        review.request_digest = "f" * 64


async def test_constructor_revalidates_review_and_enterprise_and_requires_legacy(tmp_path):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path)
    review = await review_for(path, old_path, identifier)
    for changed in (
        review.model_copy(update={"status": "INVALID"}),
        review.model_copy(update={"enterprise_id": "OTHER"}),
        review.model_dump(),
    ):
        with pytest.raises(BusinessError, match="^journal_unavailable$"):
            audit(path, old_path, changed)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        audit(path, None, review)


async def test_query_does_not_consume_status_approval(tmp_path):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    identifier = await old_operation(old_path)
    review = await review_for(path, old_path, identifier)
    db = audit(path, old_path, review)
    await db.prepare()
    query_id = str(uuid4())
    await db.start(
        request_id=query_id,
        caller=CALLER,
        action="employee_query",
        employee_ids=(),
        account_ids=(),
        binding_version="synthetic-policy",
        request_digest="a" * 64,
        write=False,
    )
    await db.dispatched(query_id)
    await db.finish(query_id, "succeeded")
    await start(db, review)


async def test_legacy_review_read_missing_record_is_fail_closed(tmp_path):
    old_path, path = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    await old_operation(old_path)
    before = old_path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await audit(path, old_path).legacy_review_subject(str(uuid4()))
    assert old_path.read_bytes() == before
    assert not path.exists()
