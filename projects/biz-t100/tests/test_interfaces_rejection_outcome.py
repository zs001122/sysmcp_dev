"""完整失败回执与结果未知的区别；仅合成上游，不连接真实业务系统。"""

import copy
import json

import httpx
import pytest
from pydantic import SecretStr

from biz_t100.client import T100Config
from biz_t100.interface_client import InterfaceUpstreamError, T100InterfaceClient
from biz_t100.interfaces import REQUEST_MODELS
from biz_t100.models import BusinessError

SECRET = "synthetic-service-key"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Rejection outcome tests must never access the network")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@pytest.fixture
def config():
    return T100Config(
        url="https://synthetic.invalid/t100",
        enterprise_id="SYNTHETIC",
        host_product="SYNTHETIC",
        host_ip="127.0.0.1",
        host_account="synthetic-service",
        service_ip="127.0.0.1",
        service_id="synthetic",
    )


def request(action, targets=("E001",)):
    if action == "employee_query":
        parameter = {"employee_no": targets[0]}
    elif action == "account_maintain":
        parameter = {
            "master": [
                {
                    "gzxa001": "LOGIN-A",
                    "gzxa003": targets[0],
                    "gzxastus": "Y",
                    "password_reset": "N",
                    "is_reproduce": "N",
                }
            ]
        }
    else:
        parameter = {
            "employee": [
                {
                    "employee_no": target,
                    "full_name": "Synthetic Employee",
                    "department_no": "D001",
                    "status": "N",
                }
                for target in targets
            ]
        }
    return REQUEST_MODELS[action].model_validate(parameter)


def failed_receipt(target="E001", **changes):
    return {
        "source_no": target,
        "erp_no": " ",
        "execute_description": "full_name输入的资料无效！",
        **changes,
    }


def response(rows=None, code="aws-00102"):
    return {
        "payload": {
            "std_data": {
                "execution": {"code": code},
                "parameter": {"employee": rows if rows is not None else [failed_receipt()]},
            }
        }
    }


async def failure(config, body, *, action="employee_update", targets=("E001",), observer=None):
    captured = []

    def upstream(req):
        captured.append(json.loads(req.content))
        return httpx.Response(200, json=body)

    client = T100InterfaceClient(
        config, transport=httpx.MockTransport(upstream), on_write_response=observer
    )
    with pytest.raises(InterfaceUpstreamError) as raised:
        await client.execute(action, request(action, targets), SecretStr(SECRET))
    assert len(captured) == 1
    assert captured[0]["payload"]["std_data"]["parameter"] == request(action, targets).parameter()
    return raised.value


@pytest.mark.parametrize("action", ["employee_create", "employee_update"])
@pytest.mark.parametrize("code", ["0", 0, "aws-00102", -1, "future-error-code"])
@pytest.mark.parametrize("erp_no", ["", " ", " \t\r\n"])
async def test_complete_all_failed_receipts_prove_rejection(config, action, code, erp_no):
    rows = [failed_receipt(target, erp_no=erp_no) for target in ("E002", "E001")]
    error = await failure(config, response(rows, code), action=action, targets=("E001", "E002"))
    assert error.code == "upstream_rejected"
    assert error.write_rejected is True
    assert "write_rejected" not in error.diagnostics  # Internal evidence, not free text output.
    assert error.diagnostics["http_status"] == 200


@pytest.mark.parametrize("code", ["0", "aws-00102"])
@pytest.mark.parametrize(
    "receipt",
    [
        {"source_no": "E002", "erp_no": "E002", "execute_description": ""},
        {"source_no": "E002", "erp_no": "E002", "execute_description": "rejected text"},
        {"source_no": "E002", "erp_no": "OTHER", "execute_description": "rejected text"},
    ],
)
async def test_mixed_or_populated_erp_receipt_never_proves_whole_batch_rejected(
    config, code, receipt
):
    error = await failure(
        config, response([failed_receipt(), receipt], code), targets=("E001", "E002")
    )
    assert error.code == "upstream_rejected"
    assert error.write_rejected is False


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [failed_receipt(), failed_receipt()],
        [failed_receipt("E002")],
        [failed_receipt(source_no=" E001")],
        [failed_receipt(source_no=None)],
        [failed_receipt(source_no=1)],
        [failed_receipt(erp_no=None)],
        [failed_receipt(erp_no=0)],
        [failed_receipt(execute_description=None)],
        [failed_receipt(execute_description=1)],
        [failed_receipt(execute_description="")],
        [failed_receipt(execute_description=" \t\n")],
        [{"source_no": "E001", "erp_no": ""}],
        [{"source_no": "E001", "execute_description": "rejected"}],
        [{"erp_no": "", "execute_description": "rejected"}],
        [None],
        "employee",
        {},
    ],
)
async def test_incomplete_mismatched_or_ambiguous_receipts_stay_unknown(config, rows):
    error = await failure(config, response(rows))
    assert error.code == "upstream_rejected"
    assert error.write_rejected is False


@pytest.mark.parametrize("code", [None, True, False, 1.5, {}, [], "", " ", "X" * 33, "error\n"])
async def test_invalid_execution_code_cannot_supply_definitive_evidence(config, code):
    error = await failure(config, response(code=code))
    assert error.write_rejected is False
    assert error.code in {"upstream_rejected", "upstream_contract_error"}


@pytest.mark.parametrize("missing", ["execution", "code", "parameter"])
async def test_missing_envelope_fields_keep_outcome_unknown(config, missing):
    body = response()
    std = body["payload"]["std_data"]
    if missing == "code":
        del std["execution"]["code"]
    else:
        del std[missing]
    error = await failure(config, body)
    assert error.write_rejected is False


@pytest.mark.parametrize("action", ["employee_query", "account_maintain"])
async def test_other_actions_cannot_inherit_employee_rejection_evidence(config, action):
    error = await failure(config, response(), action=action)
    assert error.code == "upstream_rejected"
    assert error.write_rejected is False


async def test_successful_receipts_and_zero_execution_code_still_return_ack(config):
    body = response([{"source_no": "E001", "erp_no": "E001", "execute_description": ""}], code="0")
    client = T100InterfaceClient(
        config, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    result = await client.execute("employee_update", request("employee_update"), SecretStr(SECRET))
    assert result["acknowledged"] is True
    assert result["business_effect_verified"] is False


async def test_no_rejection_inference_from_nonzero_code_with_success_receipts(config):
    error = await failure(
        config,
        response([{"source_no": "E001", "erp_no": "E001", "execute_description": ""}]),
    )
    assert error.code == "upstream_rejected"
    assert error.write_rejected is False


@pytest.mark.parametrize("status", [401, 403, 500, 302])
async def test_http_failure_does_not_trust_a_rejection_body(config, status):
    client = T100InterfaceClient(
        config, transport=httpx.MockTransport(lambda _: httpx.Response(status, json=response()))
    )
    with pytest.raises(InterfaceUpstreamError) as raised:
        await client.execute("employee_update", request("employee_update"), SecretStr(SECRET))
    assert raised.value.write_rejected is False


@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.ConnectError])
async def test_transport_failure_stays_unknown_without_retry(config, exception):
    attempts = []

    def upstream(req):
        attempts.append(req)
        raise exception("synthetic failure")

    client = T100InterfaceClient(config, transport=httpx.MockTransport(upstream))
    with pytest.raises(InterfaceUpstreamError) as raised:
        await client.execute("employee_update", request("employee_update"), SecretStr(SECRET))
    assert raised.value.write_rejected is False
    assert len(attempts) == 1


@pytest.mark.parametrize("failure_type", [ValueError, BusinessError])
async def test_response_observer_failure_never_marks_rejection_definitive(config, failure_type):
    observed = []

    def observer(observation):
        observed.append(observation)
        raise failure_type("synthetic-observation-error")

    error = await failure(config, response(), observer=observer)
    assert len(observed) == 1
    assert error.code == "journal_unavailable"
    assert error.write_rejected is False


async def test_rejection_metadata_contains_no_credential_or_raw_receipt(config):
    observed = []
    body = response([failed_receipt(execute_description=SECRET)], code=SECRET)
    original = copy.deepcopy(body)
    error = await failure(config, body, observer=observed.append)
    assert error.code == "upstream_rejected"
    assert error.write_rejected is True
    assert SECRET not in str(error)
    assert SECRET not in repr(error.__dict__)
    assert SECRET not in json.dumps(observed)
    assert "source_no" not in error.diagnostics
    assert "execute_description" not in error.diagnostics
    assert body == original


@pytest.mark.parametrize(
    "code", ["upstream_timeout", "journal_unavailable", "upstream_contract_error"]
)
def test_internal_rejection_flag_is_only_valid_for_upstream_rejected(code):
    assert InterfaceUpstreamError(code, {}, write_rejected=True).write_rejected is False
    assert InterfaceUpstreamError("upstream_rejected", {}).write_rejected is False
