"""四个原始接口的独立离线契约：合成资料，捕获真实请求，无网络。"""

import asyncio
import copy
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

import biz_t100.interface_client as module
from biz_t100.client import T100Config
from biz_t100.interface_client import InterfaceUpstreamError, T100InterfaceClient
from biz_t100.interfaces import (
    ACTIONS,
    REQUEST_MODELS,
    SERVICES,
    AccountMaintainRequest,
    EmployeeCreateRequest,
    EmployeeQueryRequest,
    EmployeeUpdateRequest,
)
from biz_t100.models import BusinessError

SECRET = "synthetic-interface-key"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Interface tests must never access the network")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@pytest.fixture
def config():
    return T100Config(
        url="https://synthetic-t100.example.invalid/api",
        enterprise_id="SYNTHETIC-ENT",
        host_product="SYNTHETIC",
        host_ip="127.0.0.1",
        host_account="synthetic-service",
        service_ip="127.0.0.1",
        service_id="synthetic",
    )


def master(**changes):
    return {
        "gzxastus": "N",
        "gzxa001": "LOGIN-A",
        "gzxa003": "E001",
        "is_reproduce": "N",
        "password_reset": "N",
        **changes,
    }


def row(**changes):
    return {
        "employee_no": "E001",
        "full_name": "Synthetic Person",
        "department_no": "DEMO",
        "status": "Y",
        **changes,
    }


def request_for(action):
    return REQUEST_MODELS[action].model_validate(
        {
            "employee_query": {"employee_name": "Synthetic"},
            "employee_create": {"employee": [row()]},
            "employee_update": {"employee": [{"employee_no": "E001", "status": "N"}]},
            "account_maintain": {"master": [master()]},
        }[action]
    )


def response_for(action, code="0"):
    std = {"execution": {"code": code}}
    if action == "employee_query":
        std["parameter"] = {"employee": [row()]}
    elif action != "account_maintain":
        std["parameter"] = {
            "employee": [{"source_no": "E001", "erp_no": "E001", "execute_description": ""}]
        }
    return {"payload": {"std_data": std}}


@pytest.mark.parametrize("action", ACTIONS)
async def test_each_document_service_sends_only_parameters_and_fixed_runtime_metadata(
    config, action
):
    requests, attempts = [], []

    def upstream(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json=response_for(action))

    client = T100InterfaceClient(config, transport=httpx.MockTransport(upstream))
    request = request_for(action)
    result = await client.execute(
        action, request, SecretStr(SECRET), on_request=lambda: attempts.append(action)
    )
    assert attempts == [action]
    assert len(requests) == 1
    sent = requests[0]
    assert sent["key"] == SECRET
    assert sent["service"]["name"] == SERVICES[action]
    assert sent["host"]["acct"] == config.host_account
    assert sent["datakey"] == {"EntId": config.enterprise_id}
    assert sent["payload"]["std_data"]["parameter"] == request.parameter()
    assert SECRET not in json.dumps(result)
    if action != "employee_query":
        assert result["acknowledged"] is True
        assert result["business_effect_verified"] is False


def test_create_full_document_fields_preserved_but_update_sends_only_supplied_fields():
    fields = row(
        last_name="Synthetic",
        middle_name="",
        name="Person",
        job_title="JOB-A",
        bank_account="BANK-A",
        account_no="DOCUMENT-ACCOUNT",
        email="a@example.invalid",
        telephone="000-000",
        mobile_phone="000",
        remark="Synthetic note",
        modify_no="EDITOR",
        create_date="20260102030405",
    )
    request = EmployeeCreateRequest(employee=[fields], enterprise_no="SYNTHETIC-ENT", site_no="S1")
    assert request.parameter()["employee"] == [fields]
    update = EmployeeUpdateRequest(employee=[{"employee_no": "E001", "status": "N"}])
    assert update.parameter() == {
        "is_digiwin_product": "N",
        "employee": [{"employee_no": "E001", "status": "N"}],
    }
    assert "full_name" not in update.parameter()["employee"][0]


@pytest.mark.parametrize(
    "filters",
    [
        {"employee_no": "00"},
        {"employee_name": "Synthetic"},
        {"department_no": "D1"},
        {"date_s": "20260101", "date_e": "20260131"},
        {"data_time": "20260101000000"},
        {"key_condition": "ENT=101 AND KEY1='E001'"},
        {"conditions": [{"field_name": "ooag001", "operator": "like", "value": "E%"}]},
    ],
)
def test_document_query_filters_are_business_criteria_not_personal_identity(filters):
    request = EmployeeQueryRequest.model_validate(filters)
    for key, value in filters.items():
        assert request.parameter()[key] == value
    assert request.parameter()["use_has_next"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"employee_name": " "},
        {"employee_no": None},
        {"employee_no": 1},
        {"employee_no": "E", "page_size": 501},
        {"employee_no": "E", "page_no": 0},
        {"employee_no": "E", "page_size": True},
        {"employee_no": "E", "use_has_next": True, "page_size": 0},
        {"employee_no": "E", "date_s": "20260101"},
        {"employee_no": "E", "date_s": "20260230", "date_e": "20260301"},
        {"employee_no": "E", "date_s": "20260301", "date_e": "20260201"},
        {
            "employee_no": "E",
            "conditions": [{"field_name": "name;DROP", "operator": "=", "value": "A"}],
        },
        {"employee_no": "E", "orders": [{"field_name": "ooag001", "order_type": "random"}]},
    ],
)
def test_invalid_query_input_fails_without_network(changes):
    with pytest.raises(ValidationError):
        EmployeeQueryRequest.model_validate(changes)


@pytest.mark.parametrize(
    "data",
    [
        {"employee": []},
        {"employee": [{"employee_no": "E001"}]},
        {"employee": [{"employee_no": "E001", "full_name": None}]},
        {"employee": [{"employee_no": "E001", "password_reset": "Y"}]},
        {"employee": [{"employee_no": "E001", "status": "N", "modify_date": "20260230000000"}]},
        {"employee": [{"employee_no": "E001", "status": "N"}] * 101},
        {"employee": [{"employee_no": "E001", "status": "N"}] * 2},
    ],
)
def test_update_rejects_ambiguous_null_empty_changes_unknown_fields_and_duplicate_targets(data):
    with pytest.raises(ValidationError):
        EmployeeUpdateRequest.model_validate(data)


@pytest.mark.parametrize("missing", ["employee_no", "full_name", "department_no", "status"])
def test_create_requires_document_required_fields(missing):
    value = row()
    value.pop(missing)
    with pytest.raises(ValidationError):
        EmployeeCreateRequest(employee=[value])


@pytest.mark.parametrize(
    "missing", ["gzxastus", "gzxa001", "gzxa003", "is_reproduce", "password_reset"]
)
def test_account_status_copy_and_reset_are_explicit_not_silent_defaults(missing):
    value = master()
    value.pop(missing)
    with pytest.raises(ValidationError):
        AccountMaintainRequest(master=[value])


def test_copy_requires_explicit_source_and_never_infers_account_from_employee():
    copied = AccountMaintainRequest(master=[master(is_reproduce="Y", reproduce_acct="SOURCE")])
    assert copied.parameter()["master"][0]["reproduce_acct"] == "SOURCE"
    assert copied.master[0].gzxa001 != copied.master[0].gzxa003
    for value in [master(is_reproduce="Y"), master(reproduce_acct="SOURCE")]:
        with pytest.raises(ValidationError):
            AccountMaintainRequest(master=[value])


def test_omitted_detail_empty_detail_and_nested_sites_are_not_silently_rewritten():
    absent = AccountMaintainRequest(master=[master()]).parameter()["master"][0]
    empty = AccountMaintainRequest(master=[master(detail=[])]).parameter()["master"][0]
    assert "detail" not in absent
    assert empty["detail"] == []
    detail = {
        "gzxbstus": "Y",
        "gzxb002": "1",
        "gzxb003": "ROLE-A",
        "gzxb004": "2026-01-01",
        "gzxb005": "2026-12-31",
        "detail1": [
            {
                "gzxcstus": "N",
                "gzxc004": "SITE-A",
                "gzxc005": "2026-01-01",
                "gzxc006": "2026-12-31",
                "gzxc007": "N",
            }
        ],
    }
    request = AccountMaintainRequest(master=[master(detail=[detail])])
    assert request.parameter()["master"][0]["detail"] == [detail]
    detail.pop("detail1")
    assert (
        "detail1"
        not in AccountMaintainRequest(master=[master(detail=[detail])]).parameter()["master"][0][
            "detail"
        ][0]
    )
    detail["detail1"] = []
    assert (
        AccountMaintainRequest(master=[master(detail=[detail])]).parameter()["master"][0]["detail"][
            0
        ]["detail1"]
        == []
    )


@pytest.mark.parametrize("extra", ["key", "url", "host", "service", "credential", "employee_id"])
def test_business_input_cannot_override_server_identity_transport_or_secret(extra):
    with pytest.raises(ValidationError):
        EmployeeQueryRequest.model_validate({"employee_no": "E001", extra: "synthetic-value"})


async def test_enterprise_mismatch_and_mutated_model_are_rejected_before_dispatch(config):
    calls = []

    def unexpected(req):
        pytest.fail("invalid model must be rejected before HTTP")

    client = T100InterfaceClient(config, transport=httpx.MockTransport(unexpected))
    mismatch = EmployeeQueryRequest(employee_no="E001", enterprise_no="OTHER")
    bad = request_for("employee_query").model_copy(update={"page_size": "unsafe"})
    for request, code in [(mismatch, "enterprise_mismatch"), (bad, "invalid_request")]:
        with pytest.raises(BusinessError) as error:
            await client.execute(
                "employee_query", request, SecretStr(SECRET), on_request=lambda: calls.append(1)
            )
        assert error.value.code == code
    assert calls == []


@pytest.mark.parametrize("rows", [[], [row()], [row(), row(employee_no="E002")]])
async def test_query_accepts_real_empty_and_multiple_records_with_pagination(config, rows):
    for item in rows:
        item["internal_credential"] = SECRET
    body = response_for("employee_query")
    body["payload"]["std_data"]["parameter"] = {
        "employee": rows,
        "total_results": len(rows),
        "has_next": False,
        "data_time": "20260101000000",
        "key_condition": "KEY1='E002'",
        "unknown_secret": SECRET,
    }
    client = T100InterfaceClient(
        config, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    )
    result = await client.execute(
        "employee_query", request_for("employee_query"), SecretStr(SECRET)
    )
    assert len(result["employee"]) == len(rows)
    assert result["total_results"] == len(rows)
    assert result["has_next"] is False
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize(
    "mutation",
    [
        "boolean_code",
        "missing_code",
        "employee_scalar",
        "employee_id_number",
        "wrong_status",
        "boolean_total",
        "numeric_has_next",
    ],
)
async def test_malformed_response_cannot_become_valid_document_data(config, mutation):
    body = response_for("employee_query")
    std = body["payload"]["std_data"]
    if mutation == "boolean_code":
        std["execution"]["code"] = False
    elif mutation == "missing_code":
        std["execution"].pop("code")
    elif mutation == "employee_scalar":
        std["parameter"]["employee"] = {}
    elif mutation == "employee_id_number":
        std["parameter"]["employee"][0]["employee_no"] = 1
    elif mutation == "wrong_status":
        std["parameter"]["employee"][0]["status"] = "enabled"
    elif mutation == "boolean_total":
        std["parameter"]["total_results"] = True
    else:
        std["parameter"]["has_next"] = 1
    client = T100InterfaceClient(
        config, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    )
    with pytest.raises(InterfaceUpstreamError) as error:
        await client.execute("employee_query", request_for("employee_query"), SecretStr(SECRET))
    assert error.value.code == "upstream_contract_error"


@pytest.mark.parametrize("action", ["employee_create", "employee_update"])
@pytest.mark.parametrize(
    "mutation,code",
    [
        ("wrong_source", "upstream_contract_error"),
        ("wrong_erp", "upstream_contract_error"),
        ("duplicate", "upstream_contract_error"),
        ("missing", "upstream_contract_error"),
        ("failed", "upstream_rejected"),
        ("description", "upstream_rejected"),
    ],
)
async def test_employee_write_ack_requires_exact_per_record_success(config, action, mutation, code):
    body = response_for(action)
    rows = body["payload"]["std_data"]["parameter"]["employee"]
    if mutation == "wrong_source":
        rows[0]["source_no"] = "E002"
    elif mutation == "wrong_erp":
        rows[0]["erp_no"] = "E002"
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif mutation == "missing":
        rows.clear()
    elif mutation == "failed":
        rows[0]["erp_no"] = ""
    else:
        rows[0]["execute_description"] = SECRET
    calls = []

    def upstream(req):
        calls.append(req)
        return httpx.Response(200, json=body)

    client = T100InterfaceClient(config, transport=httpx.MockTransport(upstream))
    with pytest.raises(InterfaceUpstreamError) as error:
        await client.execute(action, request_for(action), SecretStr(SECRET))
    assert error.value.code == code
    assert SECRET not in str(error.value)
    assert len(calls) == 1


async def test_partial_employee_batch_does_not_claim_rollback_or_retry(config):
    request = EmployeeUpdateRequest(
        employee=[
            {"employee_no": "E001", "status": "N"},
            {"employee_no": "E002", "status": "N"},
        ]
    )
    body = response_for("employee_update")
    body["payload"]["std_data"]["parameter"]["employee"].append(
        {
            "source_no": "E002",
            "erp_no": "",
            "execute_description": "synthetic rejection",
        }
    )
    requests = []

    def upstream(req):
        requests.append(req)
        return httpx.Response(200, json=body)

    client = T100InterfaceClient(config, transport=httpx.MockTransport(upstream))
    with pytest.raises(InterfaceUpstreamError) as error:
        await client.execute("employee_update", request, SecretStr(SECRET))
    assert error.value.code == "upstream_rejected"
    assert len(requests) == 1
    assert "synthetic rejection" not in str(error.value)


@pytest.mark.parametrize(
    "status,code",
    [
        (302, "upstream_rejected"),
        (401, "upstream_unauthenticated"),
        (403, "business_forbidden"),
        (429, "upstream_unavailable"),
        (500, "upstream_unavailable"),
    ],
)
async def test_http_failure_fixed_codes_no_redirect_or_response_text(config, status, code):
    calls = []

    def upstream(req):
        calls.append(req)
        return httpx.Response(
            status, text=SECRET, headers={"Location": "https://other.example.invalid/"}
        )

    client = T100InterfaceClient(config, transport=httpx.MockTransport(upstream))
    with pytest.raises(InterfaceUpstreamError) as error:
        await client.execute("account_maintain", request_for("account_maintain"), SecretStr(SECRET))
    assert error.value.code == code
    assert error.value.diagnostics == {"http_status": status}
    assert len(calls) == 1
    assert SECRET not in str(error.value)


async def test_timeout_is_one_attempt_and_never_retried(config):
    calls, attempts = [], []

    def upstream(req):
        calls.append(req)
        raise httpx.ReadTimeout(SECRET, request=req)

    client = T100InterfaceClient(config, transport=httpx.MockTransport(upstream))
    with pytest.raises(InterfaceUpstreamError) as error:
        await client.execute(
            "account_maintain",
            request_for("account_maintain"),
            SecretStr(SECRET),
            on_request=lambda: attempts.append(1),
        )
    assert error.value.code == "upstream_timeout"
    assert len(calls) == len(attempts) == 1
    assert SECRET not in str(error.value)


async def test_response_size_is_bounded(config, monkeypatch):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 20)
    client = T100InterfaceClient(
        config, transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"x" * 21))
    )
    with pytest.raises(InterfaceUpstreamError) as error:
        await client.execute("employee_query", request_for("employee_query"), SecretStr(SECRET))
    assert error.value.code == "upstream_contract_error"


async def test_concurrent_calls_keep_diagnostics_and_credentials_separate(config):
    calls = []

    async def upstream(req):
        key = json.loads(req.content)["key"]
        calls.append(key)
        await asyncio.sleep(0)
        return httpx.Response(
            200, json=response_for("account_maintain", code="1" if key == "synthetic-A" else "2")
        )

    client = T100InterfaceClient(config, transport=httpx.MockTransport(upstream))

    async def call(secret):
        with pytest.raises(InterfaceUpstreamError) as error:
            await client.execute(
                "account_maintain", request_for("account_maintain"), SecretStr(secret)
            )
        return error.value.diagnostics

    results = await asyncio.gather(call("synthetic-A"), call("synthetic-B"))
    assert results == [
        {"http_status": 200, "execution_code": "1"},
        {"http_status": 200, "execution_code": "2"},
    ]
    assert calls == ["synthetic-A", "synthetic-B"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("aws-00102", "aws-00102"),
        ("SQL_004:2", "SQL_004:2"),
        (12, 12),
        ("synthetic-interface-key", None),
        ("prefix-synthetic-interface-key", None),
        ("free text containing details", None),
        ("x" * 33, None),
    ],
)
async def test_diagnostics_preserve_bounded_service_codes_without_secret_or_free_text(
    config, value, expected
):
    body = response_for("account_maintain", code=value)
    body["payload"]["std_data"]["execution"].update(sql_code=value, description=SECRET)
    client = T100InterfaceClient(
        config, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    )
    with pytest.raises(InterfaceUpstreamError) as error:
        await client.execute("account_maintain", request_for("account_maintain"), SecretStr(SECRET))
    assert error.value.code == "upstream_rejected"
    expected_diagnostics = {"http_status": 200}
    if expected is not None:
        expected_diagnostics.update(execution_code=expected, sql_code=expected)
    assert error.value.diagnostics == expected_diagnostics
    assert SECRET not in json.dumps(error.value.diagnostics)


async def test_empty_credential_never_reads_transport_or_dispatch_callback(config):
    callbacks = []
    client = T100InterfaceClient(config)
    with pytest.raises(BusinessError) as error:
        await client.execute(
            "employee_query",
            request_for("employee_query"),
            SecretStr(" "),
            on_request=lambda: callbacks.append(1),
        )
    assert error.value.code == "upstream_unauthenticated"
    assert callbacks == []


async def test_total_deadline_includes_slow_body_without_second_request(config, monkeypatch):
    monkeypatch.setattr(module, "REQUEST_BUDGET_SECONDS", 0.01)
    calls = []

    async def upstream(req):
        calls.append(req)
        await asyncio.sleep(1)
        return httpx.Response(200, json=response_for("account_maintain"))

    client = T100InterfaceClient(config, transport=httpx.MockTransport(upstream))
    with pytest.raises(InterfaceUpstreamError) as error:
        await client.execute("account_maintain", request_for("account_maintain"), SecretStr(SECRET))
    assert error.value.code == "upstream_timeout"
    assert len(calls) == 1


async def test_failed_total_code_still_extracts_only_safe_employee_field_diagnostics(config):
    body = response_for("employee_update", code="aws-00102")
    std = body["payload"]["std_data"]
    std["execution"].update(sql_code="0", description=SECRET)
    std["parameter"]["employee"][0].update(
        erp_no=" ", execute_description="full_name输入的资料无效！"
    )
    requests = []

    def upstream(req):
        requests.append(req)
        return httpx.Response(200, json=body)

    client = T100InterfaceClient(config, transport=httpx.MockTransport(upstream))
    with pytest.raises(InterfaceUpstreamError) as error:
        await client.execute("employee_update", request_for("employee_update"), SecretStr(SECRET))
    assert error.value.code == "upstream_rejected"
    assert error.value.diagnostics == {
        "http_status": 200,
        "execution_code": "aws-00102",
        "sql_code": "0",
        "invalid_fields": ["full_name"],
    }
    assert len(requests) == 1
    safe = json.dumps(error.value.diagnostics, ensure_ascii=False)
    assert SECRET not in safe
    assert "E001" not in safe
    assert "输入的资料无效" not in safe
    assert "execute_description" not in safe


@pytest.mark.parametrize(
    "description",
    [
        "full_name输入的资料无效！\n",
        " full_name输入的资料无效！",
        "full_name输入的资料无效！ synthetic-private-detail",
        "unknown_field输入的资料无效！",
        "password输入的资料无效！",
        "full_name输入的资料无效!",
        "full_name输入的资料无效！department_no输入的资料无效！",
        "employee E001 full_name输入的资料无效！",
        "full_name=Synthetic Person输入的资料无效！",
        None,
        {"full_name": "输入的资料无效！"},
    ],
)
def test_invalid_field_diagnostics_require_exact_whitelisted_template(description):
    value = module._diagnostics(
        {
            "execution": {"code": "aws-00102", "description": SECRET},
            "parameter": {"employee": [{"execute_description": description}]},
        },
        SECRET,
    )
    assert value == {"execution_code": "aws-00102"}


def test_invalid_fields_are_deduplicated_document_fields_only():
    descriptions = [
        "full_name输入的资料无效！",
        "department_no输入的资料无效！",
        "full_name输入的资料无效！",
        "create_date输入的资料无效！",
        "modify_date输入的资料无效！",
        "arbitrary-private-description",
    ]
    value = module._diagnostics(
        {
            "execution": {"code": "aws-00102"},
            "parameter": {"employee": [{"execute_description": text} for text in descriptions]},
        },
        SECRET,
    )
    assert value["invalid_fields"] == ["create_date", "department_no", "full_name", "modify_date"]
    assert "arbitrary-private-description" not in json.dumps(value)


@pytest.mark.parametrize("credential", ["full_name", "name", "full_name输入的资料无效！"])
def test_credential_collision_with_field_template_never_becomes_diagnostic(credential):
    value = module._diagnostics(
        {
            "execution": {"code": "aws-00102"},
            "parameter": {"employee": [{"execute_description": "full_name输入的资料无效！"}]},
        },
        credential,
    )
    assert value == {"execution_code": "aws-00102"}
    assert credential not in json.dumps(value, ensure_ascii=False)
