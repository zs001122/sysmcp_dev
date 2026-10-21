"""Employee patch intent preserves existing fields against a replacement upstream."""

import copy
import json

import httpx
import pytest
from test_functions import employee, request_for
from test_functions import harness as harness

from biz_t100.functions import (
    EMPLOYEE_UPDATE_PRESERVE_FIELDS,
    FunctionService,
    materialize_employee_update,
    request_digest,
)
from biz_t100.interface_client import T100InterfaceClient
from biz_t100.interfaces import SERVICES, EmployeeUpdateRequest
from biz_t100.models import BusinessError


@pytest.fixture
async def replacement(harness):
    harness.rows["E001"].update(
        email="synthetic-existing@example.invalid",
        telephone="000",
        mobile_phone="111",
        remark="Unchanged server-only remark",
        modify_no="SYSTEM",
    )

    async def upstream(request):
        body = json.loads(request.content)
        if body["service"]["name"] == SERVICES["employee_update"] and not harness.fail_write:
            for row in body["payload"]["std_data"]["parameter"]["employee"]:
                for field in EMPLOYEE_UPDATE_PRESERVE_FIELDS:
                    harness.rows[row["employee_no"]][field] = ""
        return await harness.upstream(request)

    harness.service.client = T100InterfaceClient(
        harness.config, transport=httpx.MockTransport(upstream)
    )
    return harness


def writes(case):
    return [r for r in case.requests if r["service"]["name"] != SERVICES["employee_query"]]


def observer(case, callback):
    case.service = FunctionService(
        enterprise_id=case.config.enterprise_id,
        access=case.access,
        client=case.service.client,
        audit=case.audit,
        on_prepared_write=callback,
    )


async def test_status_intent_preserves_all_known_values_and_binds_effective_evidence(replacement):
    case = replacement
    baseline = copy.deepcopy(case.rows["E001"])
    request = request_for("employee_update")
    intent = request.parameter()
    case.approve("employee_update", request)
    prepared = []
    observer(
        case,
        lambda identifier, action, effective: prepared.append(
            (identifier, action, effective.parameter())
        ),
    )
    result = await case.execute("employee_update", request)
    assert result["status"] == "verified", result
    assert case.rows["E001"] == baseline | {"status": "N"}
    assert request.parameter() == intent
    actual = writes(case)[0]["payload"]["std_data"]["parameter"]
    assert prepared == [(result["request_id"], "employee_update", actual)]
    actual_request = EmployeeUpdateRequest.model_validate(actual)
    assert result["effective_request_digest"] == request_digest("employee_update", actual_request)
    assert result["effective_request_digest"] != request_digest("employee_update", request)
    assert (await case.audit.status(result["request_id"]))["request_digest"] == request_digest(
        "employee_update", request
    )
    empty_preserved = {field for field in EMPLOYEE_UPDATE_PRESERVE_FIELDS if baseline[field] == ""}
    assert (
        set(actual["employee"][0])
        == {"employee_no", *EMPLOYEE_UPDATE_PRESERVE_FIELDS} - empty_preserved
    )
    verified = result["data"]["verification"][0]["verified_fields"]
    assert set(verified) == {"employee_no", *EMPLOYEE_UPDATE_PRESERVE_FIELDS}


async def test_explicit_empty_optional_field_clears_without_restoring_old_value(replacement):
    case = replacement
    request = EmployeeUpdateRequest(employee=[{"employee_no": "E001", "email": ""}])
    case.approve("employee_update", request)
    result = await case.execute("employee_update", request)
    assert result["status"] == "verified", result
    assert case.rows["E001"]["email"] == ""
    assert writes(case)[0]["payload"]["std_data"]["parameter"]["employee"][0]["email"] == ""
    assert case.rows["E001"]["mobile_phone"] == "111"
    assert case.rows["E001"]["status"] == "Y"


@pytest.mark.parametrize("field", EMPLOYEE_UPDATE_PRESERVE_FIELDS)
async def test_unknown_omitted_preservation_field_rejects_before_write(replacement, field):
    case = replacement
    del case.rows["E001"][field]
    request = EmployeeUpdateRequest(employee=[{"employee_no": "E001", "remark": "Explicit"}])
    case.approve("employee_update", request)
    result = await case.execute("employee_update", request)
    assert result["error"] == "upstream_contract_error", result
    assert result["write_requests"] == 0
    assert writes(case) == []


@pytest.mark.parametrize("field,value", [("full_name", ""), ("department_no", " "), ("status", "")])
async def test_invalid_current_core_value_rejects_before_write(replacement, field, value):
    case = replacement
    case.rows["E001"][field] = value
    request = EmployeeUpdateRequest(
        employee=[{"employee_no": "E001", "email": "new@example.invalid"}]
    )
    case.approve("employee_update", request)
    result = await case.execute("employee_update", request)
    assert result["error"] == "upstream_contract_error", result
    assert result["write_requests"] == 0
    assert writes(case) == []


@pytest.mark.parametrize(
    "field,value", [("email", "new@example.invalid"), ("full_name", "New Name")]
)
async def test_explicit_change_can_supply_field_absent_from_baseline(replacement, field, value):
    case = replacement
    del case.rows["E001"][field]
    request = EmployeeUpdateRequest(employee=[{"employee_no": "E001", field: value}])
    case.approve("employee_update", request)
    result = await case.execute("employee_update", request)
    assert result["status"] == "verified", result
    assert case.rows["E001"][field] == value


@pytest.mark.parametrize(
    "kind,error",
    [
        ("none", "employee_not_found"),
        ("duplicate", "employee_not_unique"),
        ("wrong", "upstream_employee_mismatch"),
    ],
)
async def test_invalid_identity_baseline_cannot_materialize_or_write(harness, kind, error):
    case = harness

    async def upstream(request):
        response = await case.upstream(request)
        body = response.json()
        rows = {"none": [], "duplicate": [employee(), employee()], "wrong": [employee("E002")]}
        body["payload"]["std_data"]["parameter"]["employee"] = rows[kind]
        return httpx.Response(200, json=body)

    case.service.client = T100InterfaceClient(case.config, transport=httpx.MockTransport(upstream))
    request = request_for("employee_update")
    case.approve("employee_update", request)
    result = await case.execute("employee_update", request)
    assert result["error"] == error, result
    assert result["write_requests"] == 0
    assert writes(case) == []


async def test_preserved_field_mismatch_after_write_is_not_success(replacement):
    case = replacement
    case.after_write = lambda: case.rows["E001"].update(email="unexpected@example.invalid")
    request = request_for("employee_update")
    case.approve("employee_update", request)
    result = await case.execute("employee_update", request)
    assert result["error"] == "upstream_verification_failed"
    assert result["business_effect_verified"] is False
    assert result["write_requests"] == 1
    assert (await case.audit.status(result["request_id"]))["operation_state"] == "unknown"


async def test_prepared_observer_receives_detached_nested_models(replacement):
    case = replacement
    request = request_for("employee_update")
    case.approve("employee_update", request)
    observer(case, lambda identifier, action, effective: effective.employee.clear())
    result = await case.execute("employee_update", request)
    assert result["status"] == "verified", result
    assert (
        writes(case)[0]["payload"]["std_data"]["parameter"]["employee"][0]["employee_no"] == "E001"
    )
    assert len(request.employee) == 1


@pytest.mark.parametrize("error", [OSError, TimeoutError])
async def test_prepared_evidence_failure_prevents_write(replacement, error):
    case = replacement
    request = request_for("employee_update")
    case.approve("employee_update", request)

    def fail(*_):
        raise error("synthetic-private-path")

    observer(case, fail)
    result = await case.execute("employee_update", request)
    assert result["error"] == "journal_unavailable"
    assert result["write_requests"] == 0
    state = await case.audit.status(result["request_id"])
    assert state["operation_state"] == "failed"
    assert state["recorded_possible_dispatch"] is False
    assert writes(case) == []


async def test_revocation_after_preparation_still_prevents_dispatch(replacement):
    case = replacement
    request = request_for("employee_update")
    case.approve("employee_update", request)
    observer(case, lambda *_: case.approve("employee_update", request, enabled=False))
    result = await case.execute("employee_update", request)
    assert result["error"] == "identity_not_enabled"
    assert result["write_requests"] == 0
    assert writes(case) == []
    assert (await case.audit.status(result["request_id"]))["recorded_possible_dispatch"] is False


async def test_definite_rejection_is_failed_and_does_not_block_next_explicit_operation(harness):
    case = harness

    async def upstream(request):
        body = json.loads(request.content)
        if body["service"]["name"] == SERVICES["employee_query"]:
            return await case.upstream(request)
        case.requests.append(body)
        return httpx.Response(
            200,
            json={
                "payload": {
                    "std_data": {
                        "execution": {"code": "0"},
                        "parameter": {
                            "employee": [
                                {
                                    "source_no": "E001",
                                    "erp_no": "",
                                    "execute_description": "Synthetic rejected",
                                }
                            ]
                        },
                    }
                }
            },
        )

    case.service.client = T100InterfaceClient(case.config, transport=httpx.MockTransport(upstream))
    request = request_for("employee_update")
    case.approve("employee_update", request)
    identifiers = []
    for _ in range(2):
        result = await case.execute("employee_update", request)
        assert result["error"] == "upstream_rejected", result
        assert result["write_outcome"] == "rejected"
        assert result["write_requests"] == 1
        assert (await case.audit.status(result["request_id"]))["operation_state"] == "failed"
        identifiers.append(result["request_id"])
    assert identifiers[0] != identifiers[1]
    assert len(writes(case)) == 2


async def test_timeout_still_keeps_unknown_and_prevents_another_write(replacement):
    case = replacement
    case.fail_write = True
    request = request_for("employee_update")
    case.approve("employee_update", request)
    result = await case.execute("employee_update", request)
    assert result["error"] == "upstream_timeout"
    assert result["write_requests"] == 1
    assert "write_outcome" not in result
    assert (await case.audit.status(result["request_id"]))["operation_state"] == "unknown"
    other = await case.execute("employee_update", request)
    assert other["error"] == "reset_pending_review"
    assert other["write_requests"] == 0
    assert len(writes(case)) == 1


def test_materialization_rejects_cross_employee_snapshot():
    request = request_for("employee_update")
    with pytest.raises(BusinessError, match="^upstream_employee_mismatch$"):
        materialize_employee_update(request, {"E001": employee("E002")})


async def test_known_empty_omitted_wire_field_is_still_verified_after_write(replacement):
    case = replacement
    assert case.rows["E001"]["job_title"] == ""
    case.after_write = lambda: case.rows["E001"].update(job_title="Unexpected title")
    request = request_for("employee_update")
    case.approve("employee_update", request)
    result = await case.execute("employee_update", request)
    actual = writes(case)[0]["payload"]["std_data"]["parameter"]["employee"][0]
    assert "job_title" not in actual
    assert result["error"] == "upstream_verification_failed"
    assert result["business_effect_verified"] is False
    assert result["write_requests"] == 1
    assert (await case.audit.status(result["request_id"]))["operation_state"] == "unknown"
