"""离线密码重置候选契约；所有目标与凭据均为合成数据。"""

import asyncio
import json
import traceback

import anyio
import httpx
import pytest
from pydantic import SecretStr, ValidationError

import biz_t100.reset_client as reset_module
from biz_t100.client import T100Config
from biz_t100.models import BusinessError, PasswordResetGrant
from biz_t100.reset_client import ResetReceipt, T100PasswordResetClient

SECRET = "synthetic-reset-credential-never-echo"
PRIVATE = "synthetic-sensitive-upstream-message"


@pytest.fixture
def config():
    return T100Config(
        url="https://t100.example.invalid/reset-api",
        enterprise_id="synthetic-enterprise",
        host_product="synthetic-product",
        host_ip="127.0.0.1",
        host_account="synthetic-service-account",
        service_ip="127.0.0.1",
        service_id="synthetic-service",
    )


@pytest.fixture
def grant():
    return PasswordResetGrant(
        employee_id="E001",
        account_id="LOGIN-OTHER-001",
        binding_version="synthetic-v1",
        credential=SecretStr(SECRET),
    )


def ack(code="0"):
    return {"payload": {"std_data": {"execution": {"code": code}}}}


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
    def __init__(self, chunks=(), *, wait=False):
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


def assert_safe_error(failure, code):
    assert failure.value.code == code
    assert str(failure.value) == code
    rendered = "".join(traceback.format_exception(failure.value))
    assert SECRET not in rendered
    assert PRIVATE not in rendered


def test_live_transport_is_unconditionally_unavailable(config, monkeypatch):
    def forbidden_client(**kwargs):
        pytest.fail("construction must not create a network client")

    monkeypatch.setattr(reset_module.httpx, "AsyncClient", forbidden_client)
    with pytest.raises(BusinessError) as failure:
        T100PasswordResetClient(config)
    assert_safe_error(failure, "business_adapter_not_ready")


@pytest.mark.parametrize("code", [0, "0"])
@pytest.mark.parametrize("empty_parameter", [False, True])
async def test_only_password_patch_and_bare_ack(
    config, grant, code, empty_parameter, monkeypatch, capsys
):
    monkeypatch.setenv("HTTPS_PROXY", "http://unusable-proxy.example.invalid:1")
    reply = ack(code)
    reply["payload"]["std_data"]["execution"]["message"] = PRIVATE + SECRET
    if empty_parameter:
        reply["payload"]["std_data"]["parameter"] = {}
    transport = TrackingTransport(lambda _: httpx.Response(200, json=reply))
    actual_client = httpx.AsyncClient
    options = []

    def observed_client(**kwargs):
        options.append(kwargs)
        return actual_client(**kwargs)

    monkeypatch.setattr(reset_module.httpx, "AsyncClient", observed_client)
    result = await T100PasswordResetClient(config, transport=transport).reset(grant)
    assert type(result) is ResetReceipt
    assert result.model_dump() == {"acknowledged": True}
    assert SECRET not in repr(result)
    assert PRIVATE not in result.model_dump_json()
    assert len(transport.requests) == 1
    request = transport.requests[0]
    body = json.loads(request.content)
    assert request.method == "POST"
    assert str(request.url) == config.url
    assert "authorization" not in request.headers
    assert body["key"] == SECRET
    assert body["service"]["name"] == "user.account.create"
    assert body["datakey"] == {"EntId": config.enterprise_id}
    assert body["payload"]["std_data"]["parameter"] == {
        "master": [
            {
                "gzxa001": "LOGIN-OTHER-001",
                "gzxa003": "E001",
                "password_reset": "Y",
            }
        ]
    }
    assert body["host"]["acct"] == config.host_account
    assert len(body["host"]["timestamp"]) == 17
    assert len(options) == 1
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert options[0].get("verify", True) is True
    assert request.extensions["timeout"] == {
        "connect": 3.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }
    assert transport.closed == 1
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert PRIVATE not in output.out + output.err


def test_config_copy_cannot_bypass_https_validation(config):
    bypass = config.model_copy(update={"url": "http://t100.example.invalid/reset"})
    transport = TrackingTransport(lambda _: pytest.fail("must not send"))
    with pytest.raises(ValidationError):
        T100PasswordResetClient(bypass, transport=transport)
    assert transport.requests == []


async def test_explicit_legacy_test_profile_still_uses_injected_transport(config, grant):
    legacy = T100Config.model_validate(
        {**config.model_dump(), "transport_profile": "http-test", "url": "http://10.77.0.1/api"}
    )
    with pytest.raises(BusinessError, match="^business_adapter_not_ready$"):
        T100PasswordResetClient(legacy)
    transport = TrackingTransport(lambda _: httpx.Response(200, json=ack()))
    assert (await T100PasswordResetClient(legacy, transport=transport).reset(grant)).acknowledged
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "updates",
    [
        {"employee_id": ""},
        {"account_id": ""},
        {"account_id": "with spaces"},
        {"account_id": 123},
        {"binding_version": ""},
    ],
)
async def test_invalid_copied_grant_rejected_before_dispatch(config, grant, updates):
    transport = TrackingTransport(lambda _: pytest.fail("must not send"))
    with pytest.raises(BusinessError) as failure:
        await T100PasswordResetClient(config, transport=transport).reset(
            grant.model_copy(update=updates)
        )
    assert_safe_error(failure, "upstream_contract_error")
    assert transport.requests == []


async def test_empty_credential_rejected_before_dispatch(config, grant):
    transport = TrackingTransport(lambda _: pytest.fail("must not send"))
    with pytest.raises(BusinessError) as failure:
        await T100PasswordResetClient(config, transport=transport).reset(
            grant.model_copy(update={"credential": SecretStr(" ")})
        )
    assert_safe_error(failure, "upstream_unauthenticated")
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
async def test_non_success_http_never_reads_body_redirects_or_retries(config, grant, status, code):
    stream = TrackingStream([(SECRET + PRIVATE).encode()])
    transport = TrackingTransport(
        lambda _: httpx.Response(
            status,
            stream=stream,
            headers={"Location": "https://other.example.invalid/"},
        )
    )
    with pytest.raises(BusinessError) as failure:
        await T100PasswordResetClient(config, transport=transport).reset(grant)
    assert_safe_error(failure, code)
    assert len(transport.requests) == 1
    assert str(transport.requests[0].url) == config.url
    assert stream.reads == 0
    assert stream.closed
    assert transport.closed == 1


@pytest.mark.parametrize("code", [True, False, 0.0, None, [], {}])
async def test_execution_code_requires_exact_type(config, grant, code):
    transport = TrackingTransport(lambda _: httpx.Response(200, json=ack(code)))
    with pytest.raises(BusinessError) as failure:
        await T100PasswordResetClient(config, transport=transport).reset(grant)
    assert_safe_error(failure, "upstream_contract_error")
    assert len(transport.requests) == 1


@pytest.mark.parametrize("code", [1, "401", "403", "UNKNOWN", " 0", "000"])
async def test_business_codes_are_not_guessed_or_retried(config, grant, code):
    transport = TrackingTransport(lambda _: httpx.Response(200, json=ack(code)))
    with pytest.raises(BusinessError) as failure:
        await T100PasswordResetClient(config, transport=transport).reset(grant)
    assert_safe_error(failure, "upstream_rejected")
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"payload": None},
        {"payload": {"std_data": []}},
        {"payload": {"std_data": {"execution": {}}}},
    ],
)
async def test_invalid_response_shape_is_unknown_contract(config, grant, body):
    transport = TrackingTransport(lambda _: httpx.Response(200, json=body))
    with pytest.raises(BusinessError) as failure:
        await T100PasswordResetClient(config, transport=transport).reset(grant)
    assert_safe_error(failure, "upstream_contract_error")
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "parameter",
    [
        None,
        [],
        {"master": []},
        {"master": [{"gzxa001": "OTHER", "gzxa003": "E002"}]},
        {"master": [{"gzxa001": "LOGIN-OTHER-001", "gzxa003": "E001"}]},
        {"master": [{"gzxa001": "A"}, {"gzxa001": "B"}]},
        {"password": SECRET},
        {"employee": [{"employee_no": "E001"}]},
    ],
)
async def test_no_target_echo_schema_is_invented(config, grant, parameter):
    # 即使表面匹配的记录，也不能证明它就是重置响应。
    body = ack()
    body["payload"]["std_data"]["parameter"] = parameter
    transport = TrackingTransport(lambda _: httpx.Response(200, json=body))
    with pytest.raises(BusinessError) as failure:
        await T100PasswordResetClient(config, transport=transport).reset(grant)
    assert_safe_error(failure, "upstream_contract_error")
    assert len(transport.requests) == 1


async def test_non_json_response_never_exposes_upstream_text(config, grant):
    transport = TrackingTransport(lambda _: httpx.Response(200, text=SECRET + PRIVATE))
    with pytest.raises(BusinessError) as failure:
        await T100PasswordResetClient(config, transport=transport).reset(grant)
    assert_safe_error(failure, "upstream_contract_error")
    assert len(transport.requests) == 1


async def test_stream_limit_closes_without_extra_reads_or_calls(config, grant):
    stream = TrackingStream([b" " * 1_000_000, b" " * 1_000_000, b"x", b"never-read"])
    transport = TrackingTransport(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(BusinessError) as failure:
        await T100PasswordResetClient(config, transport=transport).reset(grant)
    assert_safe_error(failure, "upstream_contract_error")
    assert stream.reads == 3
    assert stream.closed
    assert transport.closed == 1
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (httpx.ConnectError, "upstream_unavailable"),
        (httpx.RemoteProtocolError, "upstream_unavailable"),
        (httpx.ReadTimeout, "upstream_timeout"),
        (httpx.ConnectTimeout, "upstream_timeout"),
        (httpx.DecodingError, "upstream_contract_error"),
    ],
)
async def test_network_failures_have_one_attempt_and_fixed_error(config, grant, exception, code):
    def failed(request):
        raise exception(SECRET + PRIVATE, request=request)

    transport = TrackingTransport(failed)
    with pytest.raises(BusinessError) as failure:
        await T100PasswordResetClient(config, transport=transport).reset(grant)
    assert_safe_error(failure, code)
    assert len(transport.requests) == 1
    assert transport.closed == 1


async def test_total_budget_closes_stream_without_retry(config, grant, monkeypatch):
    monkeypatch.setattr(reset_module, "REQUEST_BUDGET_SECONDS", 0.01)
    stream = TrackingStream(wait=True)
    transport = TrackingTransport(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(BusinessError) as failure:
        await T100PasswordResetClient(config, transport=transport).reset(grant)
    assert_safe_error(failure, "upstream_timeout")
    assert stream.closed
    assert transport.closed == 1
    assert len(transport.requests) == 1


async def test_cancel_propagates_and_closes_without_retry(config, grant):
    stream = TrackingStream(wait=True)
    transport = TrackingTransport(lambda _: httpx.Response(200, stream=stream))
    task = asyncio.create_task(T100PasswordResetClient(config, transport=transport).reset(grant))
    await asyncio.wait_for(stream.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    assert transport.closed == 1
    assert len(transport.requests) == 1


async def test_concurrent_enterprises_keep_account_employee_and_key_isolated(config, grant):
    second_config = config.model_copy(update={"enterprise_id": "synthetic-other-enterprise"})
    second_grant = grant.model_copy(
        update={"account_id": "LOGIN-SECOND", "credential": SecretStr("synthetic-other-key")}
    )
    arrived = 0
    both_arrived = asyncio.Event()

    async def responder(request):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), timeout=1)
        return httpx.Response(200, json=ack())

    transports = [TrackingTransport(responder), TrackingTransport(responder)]
    configs = [config, second_config]
    grants = [grant, second_grant]
    results = await asyncio.gather(
        *(
            T100PasswordResetClient(cfg, transport=transport).reset(authorized)
            for cfg, transport, authorized in zip(configs, transports, grants, strict=True)
        )
    )
    assert all(result.acknowledged for result in results)
    for cfg, transport, authorized in zip(configs, transports, grants, strict=True):
        assert len(transport.requests) == 1
        body = json.loads(transport.requests[0].content)
        assert body["datakey"] == {"EntId": cfg.enterprise_id}
        assert body["key"] == authorized.credential.get_secret_value()
        assert body["payload"]["std_data"]["parameter"]["master"] == [
            {
                "gzxa001": authorized.account_id,
                "gzxa003": authorized.employee_id,
                "password_reset": "Y",
            }
        ]
