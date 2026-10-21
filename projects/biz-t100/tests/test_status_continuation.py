"""合成员工启停续测：原 unknown 保持、复核记录精确绑定、未来未决仍阻断。"""

import sqlite3
from uuid import uuid4

import anyio
import pytest
from pydantic import ValidationError
from test_status_retry import CALLER, ENTERPRISE, old_operation, review_for

from biz_t100.function_audit import (
    FunctionAudit,
    ReviewedStatusContinuation,
    ReviewedStatusRecord,
)
from biz_t100.functions import request_digest
from biz_t100.interfaces import EmployeeUpdateRequest
from biz_t100.models import BusinessError


def digest(status):
    return request_digest(
        "employee_update",
        EmployeeUpdateRequest.model_validate(
            {
                "employee": [
                    {
                        "employee_no": "E001",
                        "full_name": "Synthetic Employee",
                        "department_no": "D001",
                        "status": status,
                    }
                ]
            }
        ),
    )


async def start(db, **changes):
    values = dict(
        request_id=str(uuid4()),
        caller=CALLER,
        action="employee_update",
        employee_ids=("E001",),
        account_ids=(),
        binding_version="synthetic-reviewed-policy",
        request_digest=digest("N"),
        write=True,
    )
    values.update(changes)
    await db.start(**values)
    return values["request_id"]


class Case:
    def __init__(self, tmp_path):
        self.path = tmp_path / "function.sqlite3"
        self.legacy_path = tmp_path / "legacy.sqlite3"

    def audit(self, review=None, **changes):
        values = dict(
            enterprise_id=ENTERPRISE,
            legacy_write_path=self.legacy_path,
            status_continuation=review,
        )
        values.update(changes)
        return FunctionAudit(self.path, **values)

    async def prepare(
        self,
        *,
        current_phase="unknown",
        current_action="employee_update",
        current_employees=("E001",),
        current_accounts=(),
        legacy_phase="unknown",
        legacy_action="disable_account",
        legacy_employee="E001",
    ):
        db = self.audit()
        await db.prepare()
        self.current_id = await start(
            db,
            action=current_action,
            employee_ids=current_employees,
            account_ids=current_accounts,
        )
        if current_phase != "started":
            await db.dispatched(self.current_id)
        if current_phase not in {"started", "dispatched"}:
            await db.finish(
                self.current_id,
                current_phase,
                "upstream_rejected" if current_phase == "unknown" else None,
            )
        self.legacy_id = await old_operation(
            self.legacy_path,
            phase=legacy_phase,
            action=legacy_action,
            employee=legacy_employee,
        )
        current = await db.review_subject(self.current_id)
        legacy = await db.legacy_review_subject(self.legacy_id)
        self.review = ReviewedStatusContinuation(
            enterprise_id=ENTERPRISE,
            employee_id="E001",
            approved_by="synthetic-operator",
            reason="Explicit reviewed full-field N/Y cases; original outcomes remain unknown.",
            request_digests=(digest("N"), digest("Y")),
            records=(
                ReviewedStatusRecord(
                    source="legacy", request_id=self.legacy_id, fingerprint=legacy["fingerprint"]
                ),
                ReviewedStatusRecord(
                    source="function",
                    request_id=self.current_id,
                    fingerprint=current["fingerprint"],
                ),
            ),
        )
        return self


async def test_approved_n_then_y_preserves_both_unknown_records_and_schema(tmp_path):
    case = await Case(tmp_path).prepare()
    db = case.audit(case.review)
    old_bytes = case.legacy_path.read_bytes()
    prior = await db.review_subject(case.current_id)
    current_bytes = case.path.read_bytes()
    assert (await db.review_subject(case.current_id)) == prior
    assert case.path.read_bytes() == current_bytes
    assert prior["employee_ids"] == ["E001"]
    assert prior["account_ids"] == []
    assert prior["action"] == "employee_update"
    for status in ("N", "Y"):
        identifier = await start(db, request_digest=digest(status))
        await db.dispatched(identifier)
        await db.finish(identifier, "succeeded")
        assert (await db.status(identifier))["operation_state"] == "succeeded"
    assert (await db.review_subject(case.current_id)) == prior
    assert (await db.legacy_review_subject(case.legacy_id))["state"] == "unknown"
    assert case.legacy_path.read_bytes() == old_bytes
    with sqlite3.connect(case.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM function_operations").fetchone() == (3,)
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(case.audit())  # Approval is not a permanent or global unlock.


@pytest.mark.parametrize(
    "change",
    [
        {"action": "employee_create"},
        {"action": "account_maintain"},
        {"employee_ids": ("E002",)},
        {"employee_ids": ("E001", "E002")},
        {"account_ids": ("LOGIN-E001",)},
        {"request_digest": "a" * 64},
    ],
)
async def test_continuation_is_bound_to_exact_action_targets_and_request(tmp_path, change):
    case = await Case(tmp_path).prepare()
    before = case.path.read_bytes(), case.legacy_path.read_bytes()
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(case.audit(case.review), **change)
    assert (case.path.read_bytes(), case.legacy_path.read_bytes()) == before


@pytest.mark.parametrize("source", ["legacy", "function"])
@pytest.mark.parametrize("mutation", ["operation", "event", "missing", "fingerprint"])
async def test_all_reviewed_records_require_unchanged_complete_history(tmp_path, source, mutation):
    case = await Case(tmp_path).prepare()
    review = case.review
    index = 0 if source == "legacy" else 1
    if mutation == "fingerprint":
        records = list(review.records)
        records[index] = records[index].model_copy(update={"fingerprint": "f" * 64})
        review = review.model_copy(update={"records": tuple(records)})
    elif mutation == "missing":
        records = list(review.records)
        records[index] = records[index].model_copy(update={"request_id": str(uuid4())})
        review = review.model_copy(update={"records": tuple(records)})
    else:
        path = case.legacy_path if source == "legacy" else case.path
        prefix = "reset" if source == "legacy" else "function"
        with sqlite3.connect(path) as connection:
            if mutation == "operation":
                connection.execute(f"UPDATE {prefix}_operations SET updated_at=updated_at+1")
            else:
                connection.execute(f"UPDATE {prefix}_events SET code='upstream_timeout'")
    before = case.path.read_bytes(), case.legacy_path.read_bytes()
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(case.audit(review))
    assert (case.path.read_bytes(), case.legacy_path.read_bytes()) == before


@pytest.mark.parametrize("source", ["legacy", "function"])
@pytest.mark.parametrize("phase", ["started", "dispatched", "succeeded", "failed"])
async def test_only_unknown_records_can_be_reviewed(tmp_path, source, phase):
    options = {("legacy_phase" if source == "legacy" else "current_phase"): phase}
    if source == "legacy" and phase == "failed":
        options["legacy_phase"] = "rejected"
    case = await Case(tmp_path).prepare(**options)
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(case.audit(case.review))


@pytest.mark.parametrize(
    "options",
    [
        {"current_action": "account_maintain"},
        {"current_action": "employee_create"},
        {"current_employees": ("E002",)},
        {"current_employees": ("E001", "E002")},
        {"current_accounts": ("LOGIN-E001",)},
        {"legacy_action": "reset_password"},
        {"legacy_employee": "E002"},
    ],
)
async def test_review_never_covers_other_actions_or_other_employee(tmp_path, options):
    case = await Case(tmp_path).prepare(**options)
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(case.audit(case.review))


@pytest.mark.parametrize("phase", ["started", "dispatched", "unknown"])
async def test_new_unreviewed_pending_blocks_next_status_case(tmp_path, phase):
    case = await Case(tmp_path).prepare()
    db = case.audit(case.review)
    identifier = await start(db)
    if phase != "started":
        await db.dispatched(identifier)
    if phase == "unknown":
        await db.finish(identifier, "unknown", "upstream_timeout")
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(case.audit(case.review), request_digest=digest("Y"))
    assert (await db.status(identifier))["operation_state"] == phase


@pytest.mark.parametrize("omitted", ["legacy", "function"])
async def test_any_omitted_existing_unknown_continues_to_block(tmp_path, omitted):
    case = await Case(tmp_path).prepare()
    review = case.review.model_copy(
        update={"records": tuple(r for r in case.review.records if r.source != omitted)}
    )
    with pytest.raises(BusinessError, match="^reset_pending_review$"):
        await start(case.audit(review))


async def test_query_stays_available_without_consuming_or_mutating_review(tmp_path):
    case = await Case(tmp_path).prepare()
    db = case.audit(case.review)
    query_id = await start(db, action="employee_query", employee_ids=(), write=False)
    await db.dispatched(query_id)
    await db.finish(query_id, "succeeded")
    await start(db)


async def test_parallel_status_calls_have_one_winner_and_no_implicit_retry(tmp_path):
    case = await Case(tmp_path).prepare()
    results = []

    async def contender():
        try:
            await start(case.audit(case.review))
            results.append("started")
        except BusinessError as exc:
            results.append(exc.code)

    async with anyio.create_task_group() as group:
        for _ in range(3):
            group.start_soon(contender)
    assert sorted(results) == ["reset_pending_review", "reset_pending_review", "started"]


async def test_completed_request_id_still_cannot_be_reused(tmp_path):
    case = await Case(tmp_path).prepare()
    db = case.audit(case.review)
    identifier = await start(db)
    await db.dispatched(identifier)
    await db.finish(identifier, "succeeded")
    with pytest.raises(BusinessError, match="^reset_already_recorded$"):
        await start(db, request_id=identifier)


@pytest.mark.parametrize(
    "change",
    [
        {"enterprise_id": "  "},
        {"employee_id": "../E001"},
        {"approved_by": "  "},
        {"reason": "  "},
        {"request_digests": ()},
        {"request_digests": ("not-a-digest",)},
        {"request_digests": ("f" * 64, "f" * 64)},
        {"records": ()},
        {"extra": True},
    ],
)
async def test_review_model_is_strict_bounded_and_nonempty(tmp_path, change):
    case = await Case(tmp_path).prepare()
    with pytest.raises(ValidationError):
        ReviewedStatusContinuation.model_validate(case.review.model_dump() | change)


async def test_records_are_unique_valid_uuids_with_known_source(tmp_path):
    case = await Case(tmp_path).prepare()
    with pytest.raises(ValidationError):
        ReviewedStatusContinuation.model_validate(
            case.review.model_dump() | {"records": (case.review.records[0],) * 2}
        )
    for change in ({"request_id": "bad"}, {"source": "unknown"}, {"fingerprint": "bad"}):
        with pytest.raises(ValidationError):
            ReviewedStatusRecord.model_validate(case.review.records[0].model_dump() | change)
    assert (
        ReviewedStatusContinuation.model_validate_json(case.review.model_dump_json()) == case.review
    )


async def test_constructor_revalidates_enterprise_and_requires_legacy_when_referenced(tmp_path):
    case = await Case(tmp_path).prepare()
    for review in (
        case.review.model_copy(update={"enterprise_id": "OTHER"}),
        case.review.model_copy(update={"request_digests": ("bad",)}),
        case.review.model_dump(),
    ):
        with pytest.raises(BusinessError, match="^journal_unavailable$"):
            case.audit(review)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        case.audit(case.review, legacy_write_path=None)
    old_review = await review_for(case.path, case.legacy_path, case.legacy_id)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        case.audit(case.review, legacy_status_retry=old_review)


async def test_read_subject_never_creates_database_and_missing_record_fails_closed(tmp_path):
    case = Case(tmp_path)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await case.audit().review_subject(str(uuid4()))
    assert not case.path.exists()
    await case.prepare()
    before = case.path.read_bytes()
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await case.audit().review_subject(str(uuid4()))
    assert case.path.read_bytes() == before
