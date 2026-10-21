"""合成的 HTTP 契约检查；绝不连接 T100，也不读取凭据文件。"""

import asyncio
import json
import traceback

import anyio
import httpx
import pytest
from pydantic import SecretStr, ValidationError

import biz_t100.client as client_module
from biz_t100.client import T100Config, T100EmployeeClient
from biz_t100.models import BusinessError, EmployeeGrant

SECRET = "synthetic-credential-never-echo"
PRIVATE = "synthetic-sensitive-upstream-data"


@pytest.fixture
def config():
    return T100Config(
        url="https://t100.example.invalid/employee-api",
        enterprise_id="synthetic-enterprise",
        host_product="synthetic-product",
        host_ip="127.0.0.1",
        host_account="synthetic-authorized-account",
        service_ip="127.0.0.1",
        service_id="synthetic-service",
    )


@pytest.fixture
def grant():
    return EmployeeGrant(
        employee_id="E001", binding_version="synthetic-v1", credential=SecretStr(SECRET)
    )


def response_body(employees, code="0"):
    return {
        "payload": {"std_data": {"execution": {"code": code}, "parameter": {"employee": employees}}}
    }


class TrackingTransport(httpx.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)
        self.requests = []
        self.closed = 0

    async def handle_async_request(self, request):
        self.requests.append(request)
        return await super().handle_async_request(request)

    async def aclose(self):
        self.closed += 1
        await super().aclose()


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, wait=False):
        self.chunks = chunks
        self.wait = wait
        self.closed = False
        self.reads = 0
        self.started = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.wait:
            self.started.set()
            await anyio.sleep_forever()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("code", [0, "0"])
async def test_exact_query_and_output_whitelist(config, grant, code, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://unusable-proxy.example.invalid:1")
    employee = {
        "employee_no": "E001",
        "full_name": "Synthetic Employee",
        "department_no": "D001",
        "status": "Y",
        "salary": PRIVATE,
        "account_id": PRIVATE,
        "password": SECRET,
    }
    transport = TrackingTransport(
        lambda _: httpx.Response(200, json=response_body([employee], code))
    )
    result = await T100EmployeeClient(config, transport=transport).read(grant)
    assert result.model_dump() == {
        "system": "t100",
        "employee_id": "E001",
        "display_name": "Synthetic Employee",
        "department": "D001",
        "employee_status": "Y",
        "observation": "employee_record",
    }
    assert len(transport.requests) == 1
    request = transport.requests[0]
    envelope = json.loads(request.content)
    assert request.method == "POST"
    assert str(request.url) == config.url
    assert "authorization" not in request.headers
    assert envelope["key"] == SECRET
    assert envelope["host"]["acct"] == config.host_account
    assert envelope["service"]["name"] == "employee.list.data.query.get"
    assert envelope["datakey"] == {"EntId": config.enterprise_id}
    assert envelope["payload"]["std_data"]["parameter"] == {
        "is_digiwin_product": "N",
        "use_has_next": False,
        "employee_no": "E001",
    }
    assert len(envelope["host"]["timestamp"]) == 17
    assert request.extensions["timeout"]["connect"] == 3.0
    assert PRIVATE not in result.model_dump_json()
    assert SECRET not in result.model_dump_json()
    assert transport.closed == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://t100.example.invalid/api",
        "https:///api",
        "https://user@t100.example.invalid/api",
        "https://:password@t100.example.invalid/api",
        "https://@t100.example.invalid/api",
        "https://t100.example.invalid/api?key=x",
        "https://t100.example.invalid/api?",
        "https://t100.example.invalid/api#fragment",
        "https://t100.example.invalid/api#",
        " https://t100.example.invalid/api",
        "https://t100.example.invalid/\napi",
        "https://t100.example.invalid:0/api",
        "https://t100.example.invalid:65536/api",
    ],
)
def test_endpoint_rejects_unapproved_components(config, url):
    with pytest.raises(ValidationError):
        T100Config.model_validate({**config.model_dump(), "url": url})


def test_client_rejects_https_bypass_from_copied_config(config):
    copied = config.model_copy(update={"url": "http://t100.example.invalid/api"})
    transport = TrackingTransport(lambda _: pytest.fail("must not send a request"))
    with pytest.raises(ValidationError):
        T100EmployeeClient(copied, transport=transport)
    assert transport.requests == []


@pytest.mark.parametrize("field", ["key_file", "secret", "credential", "verify"])
def test_config_has_no_secret_or_tls_bypass_setting(config, field):
    with pytest.raises(ValidationError):
        T100Config.model_validate({**config.model_dump(), field: "synthetic-only"})


async def test_empty_credential_has_zero_requests(config, grant):
    transport = TrackingTransport(lambda _: pytest.fail("must not send a request"))
    empty = grant.model_copy(update={"credential": SecretStr(" ")})
    with pytest.raises(BusinessError, match="^upstream_unauthenticated$"):
        await T100EmployeeClient(config, transport=transport).read(empty)
    assert transport.requests == []


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "upstream_unauthenticated"),
        (403, "business_forbidden"),
        (429, "upstream_unavailable"),
        (500, "upstream_unavailable"),
        (503, "upstream_unavailable"),
        (400, "upstream_rejected"),
        (404, "upstream_rejected"),
        (302, "upstream_rejected"),
        (307, "upstream_rejected"),
        (600, "upstream_rejected"),
    ],
)
async def test_http_errors_are_fixed_safe_and_never_retried(config, grant, status, code):
    transport = TrackingTransport(
        lambda _: httpx.Response(
            status, text=PRIVATE + SECRET, headers={"Location": "https://other.example.invalid/"}
        )
    )
    with pytest.raises(BusinessError) as failure:
        await T100EmployeeClient(config, transport=transport).read(grant)
    assert failure.value.code == code
    rendered = "".join(traceback.format_exception(failure.value))
    assert PRIVATE not in str(failure.value)
    assert SECRET not in repr(failure.value)
    assert PRIVATE not in rendered
    assert len(transport.requests) == 1
    assert str(transport.requests[0].url) == config.url
    assert transport.closed == 1


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"payload": {"std_data": []}},
        response_body(None),
        response_body({}),
        response_body([None]),
        response_body([{}]),
        response_body([{"employee_no": "E001", "full_name": {"secret": PRIVATE}}]),
        response_body([{"employee_no": "E001", "department_no": 1}]),
        response_body([{"employee_no": "E001", "status": "X" * 65}]),
        response_body([{"employee_no": "E001"}], code=True),
        response_body([{"employee_no": "E001"}], code=0.0),
        response_body([{"employee_no": "E001"}], code=None),
    ],
)
async def test_bad_contract_rejected_with_one_request(config, grant, body):
    transport = TrackingTransport(lambda _: httpx.Response(200, json=body))
    with pytest.raises(BusinessError, match="^upstream_contract_error$"):
        await T100EmployeeClient(config, transport=transport).read(grant)
    assert len(transport.requests) == 1
    assert transport.closed == 1


@pytest.mark.parametrize(
    ("employees", "expected"),
    [
        ([], "employee_not_found"),
        ([{"employee_no": "E001"}, {"employee_no": "E001"}], "employee_not_unique"),
        ([{"employee_no": "E001"}, {"employee_no": "E002"}], "employee_not_unique"),
        ([{"employee_no": "E002", "full_name": PRIVATE}], "upstream_employee_mismatch"),
        ([{"employee_no": "E001 "}], "upstream_employee_mismatch"),
        ([{"employee_no": "e001"}], "upstream_employee_mismatch"),
        ([{"employee_no": 1}], "upstream_employee_mismatch"),
        ([{"employee_no": None}], "upstream_employee_mismatch"),
        ([{"employee_no": ["E001"]}], "upstream_employee_mismatch"),
    ],
)
async def test_employee_result_failures_are_distinct_and_safe(config, grant, employees, expected):
    body = response_body(employees)
    body["payload"]["std_data"]["execution"]["description"] = PRIVATE + SECRET
    transport = TrackingTransport(lambda _: httpx.Response(200, json=body))
    with pytest.raises(BusinessError) as failure:
        await T100EmployeeClient(config, transport=transport).read(grant)
    assert failure.value.code == expected
    rendered = "".join(traceback.format_exception(failure.value))
    assert PRIVATE not in rendered and SECRET not in rendered
    assert len(transport.requests) == 1
    assert transport.closed == 1


@pytest.mark.parametrize("code", ["403", "NO_EMPLOYEE", 1, " 0", "000"])
async def test_unknown_business_code_not_guessed(config, grant, code):
    transport = TrackingTransport(
        lambda _: httpx.Response(200, json=response_body([{"employee_no": "E001"}], code=code))
    )
    with pytest.raises(BusinessError, match="^upstream_rejected$"):
        await T100EmployeeClient(config, transport=transport).read(grant)
    assert len(transport.requests) == 1


async def test_invalid_json_never_exposes_upstream_body(config, grant):
    transport = TrackingTransport(lambda _: httpx.Response(200, text=PRIVATE))
    with pytest.raises(BusinessError) as failure:
        await T100EmployeeClient(config, transport=transport).read(grant)
    assert failure.value.code == "upstream_contract_error"
    assert PRIVATE not in "".join(traceback.format_exception(failure.value))
    assert len(transport.requests) == 1


async def test_stream_size_limit_stops_reading_and_closes(config, grant):
    stream = TrackingStream([b" " * 1_000_000, b" " * 1_000_000, b"x", b"never-read"])
    transport = TrackingTransport(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(BusinessError, match="^upstream_contract_error$"):
        await T100EmployeeClient(config, transport=transport).read(grant)
    assert stream.reads == 3
    assert stream.closed
    assert transport.closed == 1
    assert len(transport.requests) == 1


async def test_exact_size_limit_is_accepted(config, grant):
    body = json.dumps(response_body([{"employee_no": "E001"}])).encode()
    stream = TrackingStream([body, b" " * (2_000_000 - len(body))])
    transport = TrackingTransport(lambda _: httpx.Response(200, stream=stream))
    result = await T100EmployeeClient(config, transport=transport).read(grant)
    assert result.employee_id == "E001"
    assert stream.closed
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ReadTimeout(PRIVATE), "upstream_timeout"),
        (httpx.ConnectTimeout(PRIVATE), "upstream_timeout"),
        (httpx.ConnectError(PRIVATE), "upstream_unavailable"),
        (httpx.ReadError(PRIVATE), "upstream_unavailable"),
        (httpx.DecodingError(PRIVATE), "upstream_contract_error"),
    ],
)
async def test_network_failures_do_not_leak_or_retry(config, grant, error, code):
    def fail(_):
        raise error

    transport = TrackingTransport(fail)
    with pytest.raises(BusinessError) as failure:
        await T100EmployeeClient(config, transport=transport).read(grant)
    assert failure.value.code == code
    assert PRIVATE not in "".join(traceback.format_exception(failure.value))
    assert len(transport.requests) == 1
    assert transport.closed == 1


async def test_total_budget_covers_slow_stream_and_cleanup(config, grant, monkeypatch):
    monkeypatch.setattr(client_module, "REQUEST_BUDGET_SECONDS", 0.02)
    stream = TrackingStream([b"{"], wait=True)
    transport = TrackingTransport(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(BusinessError, match="^upstream_timeout$"):
        await T100EmployeeClient(config, transport=transport).read(grant)
    assert stream.started.is_set()
    assert stream.closed
    assert transport.closed == 1
    assert len(transport.requests) == 1


async def test_cancellation_propagates_and_closes_without_retry(config, grant):
    stream = TrackingStream([b"{"], wait=True)
    transport = TrackingTransport(lambda _: httpx.Response(200, stream=stream))
    task = asyncio.create_task(T100EmployeeClient(config, transport=transport).read(grant))
    await asyncio.wait_for(stream.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    assert transport.closed == 1
    assert len(transport.requests) == 1
