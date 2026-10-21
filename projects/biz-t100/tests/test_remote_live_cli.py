"""Remote daily CLI: native HTTP MCP, synthetic identity, no external network."""

import asyncio
import importlib
import json
import socket
from pathlib import Path

import httpx2
import pytest
import uvicorn
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.jwt import JWTVerifier
from test_functions import AUDIENCE, CALLER, ISSUER, Harness, request_for

from biz_t100.functions import SCOPES, TOOLS, create_function_app
from biz_t100.interfaces import EmployeeQueryRequest
from biz_t100.models import BusinessError
from biz_t100.server import IdentityProfile

ENDPOINT = "http://127.0.0.1:8991/mcp"
_NETWORK_HTTP = httpx2.AsyncHTTPTransport.handle_async_request


class RemoteCase:
    def __init__(self, folder, monkeypatch):
        folder.chmod(0o700)
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
        self.cli = importlib.import_module("live")
        self.business = Harness(folder, monkeypatch)
        self.path = folder / "client.json"
        self.key_path = folder / "signing.pem"
        self.key_path.write_text(self.business.key.private_key.get_secret_value())
        self.key_path.chmod(0o600)
        self.configuration = {
            "identity": {
                "issuer": ISSUER, "subject": CALLER.subject, "audience": AUDIENCE,
                "client_id": CALLER.client_id,
            },
            "signing_key_file": str(self.key_path),
            "scopes": list(SCOPES.values()),
        }
        self.save()
        self.messages = []
        self.headers = []
        self.app = create_function_app(
            auth=JWTVerifier(
                public_key=self.business.key.public_key, issuer=ISSUER, audience=AUDIENCE
            ),
            identity=IdentityProfile(
                issuer=ISSUER, audience=AUDIENCE, allowed_clients=frozenset({CALLER.client_id})
            ),
            service=self.business.service,
        )

        async def capture(request):
            if request.method == "POST":
                self.messages.append(json.loads(await request.aread()))
                self.headers.append(dict(request.headers))

        def transport(endpoint, **options):
            self.http_factory = options.pop("httpx_client_factory", None)
            def factory(**settings):
                return httpx2.AsyncClient(
                    **settings, transport=httpx2.ASGITransport(app=self.app),
                    event_hooks={"request": [capture]},
                )

            return StreamableHttpTransport(endpoint, httpx_client_factory=factory, **options)

        monkeypatch.setattr(self.cli, "StreamableHttpTransport", transport)

    def save(self):
        self.path.write_text(json.dumps(self.configuration))
        self.path.chmod(0o600)

    async def call(self, action="employee_query", request=None, **options):
        endpoint = options.pop("endpoint", ENDPOINT)
        async with self.app.router.lifespan_context(self.app):
            return await self.cli.remote_call(
                endpoint, self.path, action,
                request or (request_for(action) if action is not None else None), **options,
            )


@pytest.fixture
async def remote_case(tmp_path, monkeypatch):
    case = RemoteCase(tmp_path, monkeypatch)
    await case.business.audit.prepare()
    return case


@pytest.mark.parametrize("protocol", ["2025-11-25", "2025-06-18"])
async def test_native_remote_query_pins_profile_and_audit_identity(remote_case, protocol):
    case = remote_case
    request = EmployeeQueryRequest(employee_no="E001")
    case.business.approve("employee_query", request)
    result = await case.call(request=request, protocol=protocol)
    assert result["status"] == "passed", result
    assert result["data"]["employee"][0]["employee_no"] == "E001"
    assert result["entrypoint"] == "direct-http"
    assert result["protocol"] == protocol
    assert result["identity_path"] == "direct-jwt"
    assert result["caller_identity_forwarded"] is True
    assert result["real_identity_verified"] is False
    assert (result["read_requests"], result["write_requests"]) == (1, 0)
    assert [row["method"] for row in case.messages] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call",
    ]
    assert case.messages[0]["params"]["protocolVersion"] == protocol
    assert case.messages[-1]["params"]["arguments"]["request"] == {"employee_no": "E001"}
    assert all(row["mcp-protocol-version"] == protocol for row in case.headers)
    assert all(row["authorization"].startswith("Bearer ") for row in case.headers)
    audit = await case.business.audit.status(result["request_id"])
    assert audit["operation_state"] == "succeeded"
    assert len(case.business.requests) == 1
    assert set(case.business.key_reads) == {case.business.key_path}
    assert "audit_path" not in result and "evidence_path" not in result


async def test_tools_discovery_has_no_business_request_or_service_key_read(remote_case):
    case = remote_case
    result = await case.call(action=None)
    assert result["status"] == "passed"
    assert {tool["name"] for tool in result["tools"]} == set(TOOLS.values())
    assert (result["read_requests"], result["write_requests"]) == (0, 0)
    assert case.business.requests == case.business.key_reads == []


@pytest.mark.parametrize("action", ["employee_create", "employee_update", "account_maintain"])
async def test_confirmed_remote_write_uses_existing_business_semantics_and_server_audit(
    remote_case, action,
):
    case = remote_case
    request = request_for(action)
    case.business.approve(action, request)
    result = await case.call(action=action, request=request, confirm=True)
    assert result["status"] == ("acknowledged" if action == "account_maintain" else "verified")
    assert result["write_requests"] == 1
    assert result["read_requests"] == (2 if action == "employee_update" else 1)
    assert case.messages[-1]["params"]["arguments"]["confirm"] is True
    assert result["business_effect_verified"] is (action != "account_maintain")
    recorded = await case.business.audit.status(result["request_id"])
    assert recorded["operation_state"] == "succeeded"


async def test_client_preconfigured_endpoint_and_explicit_override(remote_case):
    case = remote_case
    case.configuration["endpoint"] = ENDPOINT
    case.save()
    result = await case.call(action=None, endpoint=None)
    assert result["status"] == "passed"
    case.configuration["endpoint"] = "http://8.8.8.8/mcp"
    case.save()
    assert case.cli.remote_endpoint(ENDPOINT, case.path) == ENDPOINT


@pytest.mark.parametrize("url,enabled,accepted", [
    ("http://172.17.0.1:8781/mcp", False, False),
    ("http://172.17.0.1:8781/mcp", True, True),
    ("http://8.8.8.8/mcp", True, False),
    ("http://0.0.0.0:8781/mcp", True, False),
    ("http://localhost:8782/mcp", False, True),
    ("https://explicit-service.example.invalid/mcp", False, True),
])
def test_cleartext_remote_credentials_require_local_or_explicit_private_ip(
    remote_case, url, enabled, accepted,
):
    if accepted:
        remote_case.cli.validate_endpoint(url, allow_private_http=enabled)
    else:
        with pytest.raises(BusinessError, match="^invalid_arguments$"):
            remote_case.cli.validate_endpoint(url, allow_private_http=enabled)


async def test_remote_transport_will_not_forward_credentials_on_redirect(remote_case):
    case = remote_case
    assert (await case.call(action=None))["status"] == "passed"
    async with case.http_factory(headers={}, follow_redirects=True) as client:
        assert client.follow_redirects is False
        assert client.max_redirects == 0
        assert client.trust_env is False


async def test_real_tcp_client_ignores_environment_proxies_and_keeps_credentials_direct(
    remote_case, monkeypatch,
):
    case = remote_case
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}/mcp"

    async def loopback_only(transport, request):
        assert request.url.host == "127.0.0.1" and request.url.port == port
        return await _NETWORK_HTTP(transport, request)

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", loopback_only)
    monkeypatch.setattr(case.cli, "StreamableHttpTransport", StreamableHttpTransport)
    request = EmployeeQueryRequest(employee_no="E001")
    case.business.approve("employee_query", request)
    server = uvicorn.Server(uvicorn.Config(case.app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        async with asyncio.timeout(10):
            discovery = await case.cli.remote_call(endpoint, case.path)
            assert discovery["status"] == "passed", discovery
            assert case.business.requests == case.business.key_reads == []
            result = await case.cli.remote_call(endpoint, case.path, "employee_query", request)
            assert result["status"] == "passed", result
            assert result["data"]["employee"][0]["employee_no"] == "E001"
            assert result["entrypoint"] == "direct-http"
            assert (result["read_requests"], result["write_requests"]) == (1, 0)
            assert len(case.business.requests) == 1
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("location", ["/redirected/mcp", "https://127.0.0.1/redirected/mcp"])
@pytest.mark.parametrize("phase", ["initialize", "tools/call"])
async def test_sdk_redirect_loop_never_replays_token_gateway_key_or_tool_body(
    remote_case, monkeypatch, status, location, phase,
):
    case = remote_case
    gateway_key = case.path.parent / "gateway.key"
    gateway_key.write_text("synthetic-private-gateway-key")
    gateway_key.chmod(0o600)
    case.configuration["gateway_api_key_file"] = str(gateway_key)
    case.save()
    captured = []

    async def handler(request):
        packet = json.loads(await request.aread())
        captured.append((str(request.url), packet))
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["x-api-key"] == "synthetic-private-gateway-key"
        method = packet["method"]
        if method == phase:
            return httpx2.Response(status, headers={"Location": location})
        if method == "notifications/initialized":
            return httpx2.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": packet["params"]["protocolVersion"], "capabilities": {},
                "serverInfo": {"name": "synthetic-redirect-case", "version": "1"},
            }
        else:
            assert method == "tools/list"
            result = {
                "tools": [{"name": TOOLS["employee_query"], "inputSchema": {"type": "object"}}]
            }
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": packet["id"], "result": result})

    def transport(endpoint, **options):
        original_factory = options.pop("httpx_client_factory")

        def factory(**settings):
            return original_factory(**settings, transport=httpx2.MockTransport(handler))

        return StreamableHttpTransport(endpoint, httpx_client_factory=factory, **options)

    monkeypatch.setattr(case.cli, "StreamableHttpTransport", transport)
    endpoint = "http://127.0.0.1/mcp"
    result = await case.cli.remote_call(
        endpoint, case.path, "employee_query", EmployeeQueryRequest(employee_no="E001"),
    )
    assert result["status"] == "failed" and result["error"] == "mcp_call_failed"
    assert all(url == endpoint for url, _ in captured)
    assert sum(packet["method"] == phase for _, packet in captured) == 1
    assert len(captured) == (1 if phase == "initialize" else 4)
    if phase == "tools/call":
        assert captured[-1][1]["params"]["arguments"]["request"] == {"employee_no": "E001"}
        assert "upstream_attempted" not in result
    else:
        assert result["upstream_attempted"] is False
    assert "synthetic-private-gateway-key" not in json.dumps(result)


@pytest.mark.parametrize("action", ["employee_create", "employee_update", "account_maintain"])
async def test_remote_write_requires_confirm_before_key_or_http(remote_case, action):
    case = remote_case
    with pytest.raises(BusinessError, match="^confirmation_required$"):
        await case.cli.remote_call(ENDPOINT, case.path, action, request_for(action))
    assert case.messages == case.business.requests == case.business.key_reads == []


@pytest.mark.parametrize("change", ["wrong-client", "wrong-audience", "wrong-issuer"])
async def test_native_verifier_rejects_untrusted_client_before_business(remote_case, change):
    case = remote_case
    field = {
        "wrong-client": "client_id", "wrong-audience": "audience", "wrong-issuer": "issuer"
    }[change]
    case.configuration["identity"][field] = "synthetic-unapproved"
    case.save()
    request = EmployeeQueryRequest(employee_no="E001")
    case.business.approve("employee_query", request)
    result = await case.call(request=request)
    assert result["status"] == "failed", result
    assert result["error"] in {"unauthenticated", "mcp_call_failed"}
    assert case.business.requests == case.business.key_reads == []
    if not result["mcp_tool_call_attempted"]:
        assert result["upstream_attempted"] is False


async def test_missing_action_scope_rejected_before_signing_key_and_http(remote_case):
    case = remote_case
    case.configuration["scopes"] = [SCOPES["employee_query"]]
    case.save()
    case.key_path.unlink()
    with pytest.raises(BusinessError, match="^permission_denied$"):
        await case.cli.remote_call(
            ENDPOINT, case.path, "account_maintain", request_for("account_maintain"), confirm=True
        )
    assert case.messages == case.business.requests == case.business.key_reads == []


async def test_missing_tool_is_refused_before_tool_dispatch(remote_case):
    case = remote_case
    result = await case.call(tool_name="not-exposed-t100-tool")
    assert result["error"] == "tool_not_available"
    assert result["mcp_tool_call_attempted"] is False
    assert result["upstream_attempted"] is False
    assert case.business.requests == case.business.key_reads == []


async def test_create_request_cannot_choose_update_tool_even_if_it_is_discovered(remote_case):
    case = remote_case
    request = request_for("employee_create")
    case.business.approve("employee_create", request)
    result = await case.call(
        action="employee_create", request=request, confirm=True,
        tool_name=TOOLS["employee_update"],
    )
    assert result["error"] == "tool_not_available"
    assert result["mcp_tool_call_attempted"] is False
    assert (result["read_requests"], result["write_requests"]) == (0, 0)
    assert case.messages == case.business.requests == case.business.key_reads == []
    assert "写入成功" not in result["状态说明"]


@pytest.mark.parametrize("name,accepted", [
    (TOOLS["employee_create"], True),
    ("t100-write__" + TOOLS["employee_create"], True),
    ("t100-write__" + TOOLS["employee_update"], False),
    ("__" + TOOLS["employee_create"], False),
    ("t100-write_" + TOOLS["employee_create"], False),
    ("t100-write__" + TOOLS["employee_create"] + "_other", False),
])
def test_remote_tool_namespace_keeps_exact_action_suffix(remote_case, name, accepted):
    if accepted:
        assert remote_case.cli.remote_tool_name("employee_create", name) == name
    else:
        with pytest.raises(BusinessError, match="^tool_not_available$"):
            remote_case.cli.remote_tool_name("employee_create", name)


async def test_gateway_key_stays_separate_from_signed_client_and_identity_claim(remote_case):
    case = remote_case
    key_path = case.path.parent / "gateway.key"
    key_path.write_text("synthetic-gateway-key")
    key_path.chmod(0o600)
    case.configuration["gateway_api_key_file"] = str(key_path)
    case.save()
    result = await case.call(action=None, protocol="2025-06-18")
    assert result["entrypoint"] == "metamcp"
    assert result["identity_path"] == "gateway-static-service-principal"
    assert result["caller_identity_forwarded"] is False
    assert all(row["x-api-key"] == "synthetic-gateway-key" for row in case.headers)
    assert all(row["authorization"].startswith("Bearer ") for row in case.headers)
    assert "synthetic-gateway-key" not in json.dumps(result)
    assert "Bearer " not in json.dumps(result)
    assert case.business.requests == case.business.key_reads == []


@pytest.mark.parametrize("url", [
    "https://user:secret@example.invalid/mcp", "http://example.invalid/mcp?api_key=secret",
    "http://example.invalid/mcp#fragment", "file:///mcp", "http://example.invalid",
])
async def test_endpoint_rejects_embedded_auth_or_ambiguous_address(remote_case, url):
    with pytest.raises(BusinessError, match="^invalid_arguments$"):
        await remote_case.cli.remote_call(url, remote_case.path)
    assert remote_case.messages == []


def test_remote_main_never_reads_t100_config_or_initializes_local_service(
    remote_case, monkeypatch, capsys,
):
    case = remote_case

    def forbidden(*args, **kwargs):
        pytest.fail("Remote client must not load server config, service key or local permissions")

    monkeypatch.setattr(case.cli, "load_runtime_config", forbidden)
    monkeypatch.setattr(case.cli, "initialize_permissions", forbidden)
    monkeypatch.setattr(case.cli, "FunctionService", forbidden)

    async def remote(endpoint, client_config, action, request, **options):
        assert endpoint == ENDPOINT and client_config == case.path
        assert action == "employee_query" and request.employee_no == "E001"
        assert options["protocol"] == "2025-06-18"
        return {"status": "passed", "action": action, "data": {"employee": []}}

    monkeypatch.setattr(case.cli, "remote_call", remote)
    assert case.cli.main([
        "--endpoint", ENDPOINT, "--client-config", str(case.path), "--protocol", "2025-06-18",
        "employee-query", "--employee-no", "E001",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["状态说明"] == "查询完成，未找到匹配员工"


async def test_lost_tool_response_does_not_claim_zero_calls_or_repeat(remote_case, monkeypatch):
    case = remote_case
    request = EmployeeQueryRequest(employee_no="E001")
    case.business.approve("employee_query", request)
    original = case.cli.MCPClient.call_tool
    calls = []

    async def lost(client, name, arguments, **options):
        calls.append(name)
        await original(client, name, arguments, **options)
        raise RuntimeError("synthetic-private-response-error")

    monkeypatch.setattr(case.cli.MCPClient, "call_tool", lost)
    result = await case.call(request=request)
    assert result["status"] == "failed" and result["error"] == "mcp_call_failed"
    assert result["mcp_tool_call_attempted"] is True
    assert "upstream_attempted" not in result
    assert "read_requests" not in result and "write_requests" not in result
    assert calls == [TOOLS["employee_query"]]
    assert len(case.business.requests) == 1
    assert "synthetic-private-response-error" not in json.dumps(result)


async def test_profile_mismatch_blocks_discovery_and_business_without_fallback(
    remote_case, monkeypatch,
):
    case = remote_case
    original = case.cli.MCPClient.session.fget

    class WrongProfile:
        def __init__(self, session):
            self.session = session

        async def send_request(self, request, expected):
            reply = await self.session.send_request(request, expected)
            if request.method == "initialize":
                reply.protocol_version = "2025-06-18"
            return reply

        def __getattr__(self, name):
            return getattr(self.session, name)

    monkeypatch.setattr(
        case.cli.MCPClient, "session", property(lambda client: WrongProfile(original(client)))
    )
    result = await case.call(protocol="2025-11-25")
    assert result["error"] == "protocol_mismatch"
    assert result["mcp_tool_call_attempted"] is False
    assert [row["method"] for row in case.messages] == ["initialize"]
    assert case.business.requests == case.business.key_reads == []
