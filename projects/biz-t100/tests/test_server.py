"""仅使用已签名的 HTTP 请求与合成端口；绝不接触真实 IdP 或业务系统。"""

import asyncio
import json
import time
from contextlib import asynccontextmanager

import anyio
import httpx
import httpx2
import pytest
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from pydantic import SecretStr

from biz_t100 import IdentityProfile, create_app, create_server
from biz_t100.models import BusinessError, EmployeeGrant, EmployeeRecord
from biz_t100.server import SCOPE, TOOL_NAME

ISSUER = "https://identity.example.invalid"
AUDIENCE = "t100-test-resource"
PROFILE = IdentityProfile(issuer=ISSUER, audience=AUDIENCE, allowed_clients=frozenset({"agent-a"}))
HEADERS = {"Accept": "application/json, text/event-stream"}


class SyntheticAccess:
    def __init__(self):
        self.version = "1"
        self.disabled = False
        self.calls = 0

    async def authorize(self, caller):
        self.calls += 1
        if self.disabled or caller.subject not in {"employee-a", "employee-b"}:
            raise BusinessError("identity_not_enabled")
        return EmployeeGrant(
            employee_id={"employee-a": "E001", "employee-b": "E002"}[caller.subject],
            binding_version=self.version,
            credential=SecretStr("synthetic-credential-never-return"),
        )


class SyntheticReader:
    def __init__(self):
        self.calls = []
        self.error = None
        self.delay = 0

    async def read(self, grant):
        self.calls.append(grant.employee_id)
        await anyio.sleep(self.delay)
        if self.error:
            raise self.error
        return EmployeeRecord(employee_id=grant.employee_id, display_name="Synthetic employee")


class TestJournal:
    __test__ = False

    def __init__(self):
        self.events = []
        self.fail_event = None
        self.on_start = None

    async def record(self, **event):
        if event["event"] == self.fail_event:
            raise OSError("synthetic-secret-path")
        self.events.append(event)
        if event["event"] == "query_started" and self.on_start:
            self.on_start()


@pytest.fixture
def harness():
    key = RSAKeyPair.generate()
    access, reader, journal = SyntheticAccess(), SyntheticReader(), TestJournal()
    deps = dict(
        auth=JWTVerifier(
            public_key=key.public_key, issuer=ISSUER, audience=AUDIENCE, required_scopes=[SCOPE]
        ),
        identity=PROFILE,
        access=access,
        reader=reader,
        journal=journal,
    )
    return key, deps


def token(key, **overrides):
    values = dict(
        subject="employee-a",
        issuer=ISSUER,
        audience=AUDIENCE,
        scopes=[SCOPE],
        additional_claims={"azp": "agent-a"},
    )
    values.update(overrides)
    return key.create_token(**values)


@asynccontextmanager
async def connection(deps):
    app = create_app(**deps)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            yield client


async def rpc(client, bearer, method, params, protocol="2025-06-18"):
    headers = {**HEADERS, "MCP-Protocol-Version": protocol}
    if protocol == "2026-07-28":
        headers["MCP-Method"] = method
        if "name" in params:
            headers["MCP-Name"] = params["name"]
        params = {
            **params,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": protocol,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        }
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        },
    )


async def call(client, bearer, arguments=None, name=TOOL_NAME):
    response = await rpc(
        client,
        bearer,
        "tools/call",
        {
            "name": name,
            "arguments": {} if arguments is None else arguments,
        },
    )
    assert response.status_code == 200
    return response.json()


def error_text(response):
    return json.dumps(response, ensure_ascii=False)


async def test_compat_initialize_list_schema_and_self_call(harness):
    key, deps = harness
    bearer = token(key)
    async with connection(deps) as client:
        initial = await rpc(
            client,
            bearer,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "offline-contract", "version": "1"},
            },
        )
        assert initial.status_code == 200
        assert initial.json()["result"]["protocolVersion"] == "2025-06-18"
        listed = await rpc(client, bearer, "tools/list", {})
        tools = listed.json()["result"]["tools"]
        assert [tool["name"] for tool in tools] == [TOOL_NAME]
        assert tools[0]["inputSchema"]["properties"] == {}
        assert tools[0]["annotations"]["readOnlyHint"] is True
        assert "outputSchema" in tools[0]
        result = (await call(client, bearer))["result"]
        assert result["isError"] is False
        value = result["structuredContent"]
        assert value["request_id"]
        assert value["data"]["employee_id"] == "E001"
        assert value["data"]["observation"] == "employee_record"
        assert "account_id" not in value["data"]
        assert "account_status" not in value["data"]
    assert deps["access"].calls == 3
    assert deps["reader"].calls == ["E001"]
    assert [e["event"] for e in deps["journal"].events] == ["query_started", "query_succeeded"]


async def test_modern_native_http_protocol(harness):
    key, deps = harness
    async with connection(deps) as client:
        listed = await rpc(client, token(key), "tools/list", {}, protocol="2026-07-28")
        assert listed.status_code == 200
        assert [x["name"] for x in listed.json()["result"]["tools"]] == [TOOL_NAME]
        called = await rpc(
            client,
            token(key),
            "tools/call",
            {"name": TOOL_NAME, "arguments": {}},
            protocol="2026-07-28",
        )
        assert called.status_code == 200
        assert called.json()["result"]["structuredContent"]["data"]["employee_id"] == "E001"


@pytest.mark.parametrize(
    "kind", ["missing", "bad-signature", "expired", "issuer", "audience", "scope"]
)
async def test_native_auth_rejects_before_business(harness, kind):
    key, deps = harness
    bearer = {
        "missing": None,
        "bad-signature": token(RSAKeyPair.generate()),
        "expired": token(key, expires_in_seconds=-60),
        "issuer": token(key, issuer="https://other.invalid"),
        "audience": token(key, audience="gateway-resource"),
        "scope": token(key, scopes=["unrelated"]),
    }[kind]
    async with connection(deps) as client:
        response = await rpc(client, bearer, "tools/list", {})
    assert response.status_code in {401, 403}
    assert deps["access"].calls == 0
    assert deps["reader"].calls == []


@pytest.mark.parametrize(
    "claims", [{}, {"azp": "other-agent"}, {"azp": "agent-a", "client_id": "spoofed"}]
)
async def test_invalid_client_profile_is_not_an_employee(harness, claims):
    key, deps = harness
    async with connection(deps) as client:
        result = await call(client, token(key, additional_claims=claims))
    assert "unauthenticated" in error_text(result)
    assert deps["reader"].calls == []


async def test_multi_audience_rejected(harness):
    key, deps = harness
    async with connection(deps) as client:
        result = await call(client, token(key, audience=[AUDIENCE, "gateway"]))
    assert "unauthenticated" in error_text(result)
    assert deps["reader"].calls == []


@pytest.mark.parametrize("argument", ["employee_id", "account_id", "token", "url", "subject"])
async def test_injected_arguments_rejected_without_echo(harness, argument):
    key, deps = harness
    async with connection(deps) as client:
        result = await call(client, token(key), {argument: "secret-injected-value"})
    assert "invalid_arguments" in error_text(result)
    assert "secret-injected-value" not in error_text(result)
    assert deps["access"].calls == 0
    assert deps["reader"].calls == []


async def test_unknown_or_write_tool_not_exposed(harness):
    key, deps = harness
    async with connection(deps) as client:
        result = await call(client, token(key), name="reset_password")
    assert "tool_not_available" in error_text(result)
    assert deps["reader"].calls == []


async def test_unmapped_employee_rejected(harness):
    key, deps = harness
    async with connection(deps) as client:
        result = await call(client, token(key, subject="unmapped-machine"))
    assert "identity_not_enabled" in error_text(result)
    assert deps["reader"].calls == []


@pytest.mark.parametrize("failure", ["query_started", "query_succeeded"])
async def test_journal_failure_never_returns_business_success(harness, failure):
    key, deps = harness
    deps["journal"].fail_event = failure
    async with connection(deps) as client:
        result = await call(client, token(key))
    assert "journal_unavailable" in error_text(result)
    assert "synthetic-secret-path" not in error_text(result)
    assert "Synthetic employee" not in error_text(result)
    assert len(deps["reader"].calls) == (0 if failure == "query_started" else 1)


@pytest.mark.parametrize("change", ["revoke", "rebind"])
async def test_recheck_after_durable_admission_before_dispatch(harness, change):
    key, deps = harness

    def mutate():
        if change == "revoke":
            deps["access"].disabled = True
        else:
            deps["access"].version = "2"

    deps["journal"].on_start = mutate
    async with connection(deps) as client:
        result = await call(client, token(key))
    assert (
        "identity_not_enabled" if change == "revoke" else "authorization_changed"
    ) in error_text(result)
    assert deps["reader"].calls == []


async def test_tool_total_budget_and_no_retry(harness):
    key, deps = harness
    deps["budget_seconds"] = 0.02
    deps["reader"].delay = 1
    async with connection(deps) as client:
        result = await call(client, token(key))
    assert "query_timeout" in error_text(result)
    assert deps["reader"].calls == ["E001"]
    assert deps["journal"].events[-1]["event"] == "query_failed"


async def test_untrusted_upstream_error_is_masked(harness):
    key, deps = harness
    deps["reader"].error = BusinessError("a-secret-from-upstream")
    async with connection(deps) as client:
        result = await call(client, token(key))
    assert "query_failed" in error_text(result)
    assert "a-secret-from-upstream" not in error_text(result)
    assert deps["journal"].events[-1]["code"] == "query_failed"


async def test_concurrent_employees_keep_separate_identity_and_trace(harness):
    key, deps = harness
    deps["reader"].delay = 0.01
    async with connection(deps) as client:
        values = await asyncio.gather(
            call(client, token(key)), call(client, token(key, subject="employee-b"))
        )
    outputs = [v["result"]["structuredContent"] for v in values]
    assert [v["data"]["employee_id"] for v in outputs] == ["E001", "E002"]
    assert outputs[0]["request_id"] != outputs[1]["request_id"]
    for output in outputs:
        events = [e for e in deps["journal"].events if e["request_id"] == output["request_id"]]
        assert len(events) == 2
        assert {e["employee_id"] for e in events} == {output["data"]["employee_id"]}


async def test_inprocess_without_http_identity_cannot_call_business(harness):
    _, deps = harness
    async with Client(create_server(**deps)) as client:
        with pytest.raises(Exception, match="unauthenticated"):
            await client.call_tool(TOOL_NAME)
    assert deps["reader"].calls == []


async def test_complete_offline_http_to_t100_contract_and_durable_journal(harness, tmp_path):
    from biz_t100.client import T100Config, T100EmployeeClient
    from biz_t100.journal import SQLiteQueryJournal

    key, deps = harness
    requests = []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        employee = body["payload"]["std_data"]["parameter"]["employee_no"]
        return httpx.Response(
            200,
            json={
                "payload": {
                    "std_data": {
                        "execution": {"code": "0"},
                        "parameter": {
                            "employee": [
                                {
                                    "employee_no": employee,
                                    "full_name": "Offline User",
                                    "salary": "never-return",
                                }
                            ]
                        },
                    }
                }
            },
        )

    cfg = T100Config(
        url="https://t100.example.invalid/api",
        enterprise_id="example",
        host_product="TEST",
        host_ip="127.0.0.1",
        host_account="test",
        service_ip="127.0.0.1",
        service_id="test",
    )
    deps["reader"] = T100EmployeeClient(cfg, transport=httpx.MockTransport(upstream))
    deps["journal"] = SQLiteQueryJournal(tmp_path / "journal" / "queries.sqlite3")
    async with connection(deps) as client:
        result = await call(client, token(key))
    assert result["result"]["structuredContent"]["data"]["employee_id"] == "E001"
    assert len(requests) == 1
    assert requests[0]["service"]["name"] == "employee.list.data.query.get"
    assert "never-return" not in error_text(result)
    assert "synthetic-credential" not in error_text(result)
    assert await deps["journal"].pending_request_ids() == []


@pytest.mark.parametrize("stage", ["query_started", "query_succeeded"])
async def test_shielded_journal_cannot_extend_query_deadline(harness, stage):
    key, deps = harness
    deps["budget_seconds"] = 0.02
    journal = deps["journal"]
    original_record = journal.record

    async def slow_record(**event):
        if event["event"] == stage:
            await anyio.to_thread.run_sync(time.sleep, 0.06, abandon_on_cancel=False)
        await original_record(**event)

    journal.record = slow_record
    async with connection(deps) as client:
        result = await call(client, token(key))
    assert "query_timeout" in error_text(result)
    assert "Synthetic employee" not in error_text(result)
    assert len(deps["reader"].calls) == (0 if stage == "query_started" else 1)
    terminals = [e for e in journal.events if e["event"] != "query_started"]
    assert len(terminals) == 1


async def test_token_expiring_during_authorization_never_dispatches(harness, monkeypatch):
    from biz_t100 import server as module

    key, deps = harness
    clock = [time.time()]
    bearer = token(key, expires_in_seconds=30)
    original = deps["access"].authorize

    async def expires(caller):
        grant = await original(caller)
        if deps["access"].calls == 2:
            clock[0] += 60
        return grant

    deps["access"].authorize = expires
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    async with connection(deps) as client:
        result = await call(client, bearer)
    assert "unauthenticated" in error_text(result)
    assert deps["reader"].calls == []


async def test_revocation_after_upstream_withholds_employee_data(harness):
    key, deps = harness
    original = deps["reader"].read

    async def revokes(grant):
        result = await original(grant)
        deps["access"].disabled = True
        return result

    deps["reader"].read = revokes
    async with connection(deps) as client:
        result = await call(client, token(key))
    assert "identity_not_enabled" in error_text(result)
    assert "Synthetic employee" not in error_text(result)
    assert deps["reader"].calls == ["E001"]
    assert deps["journal"].events[-1]["event"] == "query_failed"


async def test_service_admin_key_keeps_two_callers_self_only(harness, tmp_path):
    from biz_t100.access import FileServiceAccountAccess
    from biz_t100.client import T100Config, T100EmployeeClient
    from biz_t100.journal import SQLiteQueryJournal

    key, deps = harness
    policy_file, secret_file = tmp_path / "policy.json", tmp_path / "service.secret"
    policy_file.write_text(
        json.dumps(
            {
                "auth_mode": "service-account",
                "revision": "1",
                "principals": [
                    {
                        "issuer": ISSUER,
                        "subject": subject,
                        "employee_id": employee,
                        "allowed_clients": ["agent-a"],
                        "enabled": True,
                        "allow_employee_read": True,
                    }
                    for subject, employee in [("employee-a", "E001"), ("employee-b", "E002")]
                ],
            }
        )
    )
    secret_file.write_text("synthetic-admin-service-key")
    policy_file.chmod(0o600)
    secret_file.chmod(0o600)
    deps["access"] = FileServiceAccountAccess(policy_file, secret_file)
    deps["journal"] = SQLiteQueryJournal(tmp_path / "journal" / "queries.sqlite3")
    requests = []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        employee = body["payload"]["std_data"]["parameter"]["employee_no"]
        return httpx.Response(
            200,
            json={
                "payload": {
                    "std_data": {
                        "execution": {"code": "0"},
                        "parameter": {
                            "employee": [
                                {
                                    "employee_no": employee,
                                    "full_name": "Synthetic only",
                                }
                            ]
                        },
                    }
                }
            },
        )

    config = T100Config(
        url="https://t100.example.invalid/api",
        enterprise_id="example",
        host_product="TEST",
        host_ip="127.0.0.1",
        host_account="service",
        service_ip="127.0.0.1",
        service_id="test",
    )
    deps["reader"] = T100EmployeeClient(config, transport=httpx.MockTransport(upstream))
    async with connection(deps) as client:
        outputs = await asyncio.gather(
            call(client, token(key)), call(client, token(key, subject="employee-b"))
        )
        # 上游管理员权限不能让未映射的调用者获得访问权。
        denied = await call(client, token(key, subject="unmapped"))
    assert [v["result"]["structuredContent"]["data"]["employee_id"] for v in outputs] == [
        "E001",
        "E002",
    ]
    assert len(requests) == 2
    assert {r["key"] for r in requests} == {"synthetic-admin-service-key"}
    assert {r["payload"]["std_data"]["parameter"]["employee_no"] for r in requests} == {
        "E001",
        "E002",
    }
    assert "identity_not_enabled" in error_text(denied)
    assert "synthetic-admin-service-key" not in error_text(outputs)
    assert await deps["journal"].pending_request_ids() == []


@pytest.mark.parametrize(
    "fault, expected",
    [
        ("policy", "policy_unavailable"),
        ("credential", "credential_unavailable"),
        ("revoke_after_start", "identity_not_enabled"),
    ],
)
async def test_service_policy_failures_stop_before_business(harness, tmp_path, fault, expected):
    from biz_t100.access import FileServiceAccountAccess
    from biz_t100.journal import SQLiteQueryJournal

    key, deps = harness
    policy_file, secret_file = tmp_path / "policy.json", tmp_path / "service.secret"
    policy = {
        "auth_mode": "service-account",
        "revision": "1",
        "principals": [
            {
                "issuer": ISSUER,
                "subject": "employee-a",
                "employee_id": "E001",
                "allowed_clients": ["agent-a"],
                "enabled": True,
                "allow_employee_read": True,
            }
        ],
    }
    policy_file.write_text(json.dumps(policy))
    policy_file.chmod(0o600)
    if fault != "credential":
        secret_file.write_text("synthetic-service-key")
        secret_file.chmod(0o600)
    if fault == "policy":
        policy_file.write_text("invalid policy text")
    deps["access"] = FileServiceAccountAccess(policy_file, secret_file)
    if fault == "revoke_after_start":

        def revoke():
            policy["principals"][0]["enabled"] = False
            policy_file.write_text(json.dumps(policy))

        deps["journal"].on_start = revoke
    else:
        deps["journal"] = SQLiteQueryJournal(tmp_path / "journal" / "queries.sqlite3")
    async with connection(deps) as client:
        result = await call(client, token(key))
    assert expected in error_text(result)
    assert deps["reader"].calls == []
    assert "synthetic-service-key" not in error_text(result)


async def test_injected_reader_cannot_bypass_exact_target_check(harness):
    from biz_t100.errors import error_message

    key, deps = harness

    async def different_employee(grant):
        deps["reader"].calls.append(grant.employee_id)
        return EmployeeRecord(employee_id="E002", display_name="Private Other Employee")

    deps["reader"].read = different_employee
    async with connection(deps) as client:
        response = await call(client, token(key))
    result = response["result"]
    assert result["isError"] and not result.get("structuredContent")
    error = json.loads(result["content"][0]["text"])
    assert error["error"] == "upstream_employee_mismatch"
    assert error["message"] == error_message("upstream_employee_mismatch")
    assert "Private Other Employee" not in error_text(response)
    assert "E002" not in error_text(response)
    assert deps["reader"].calls == ["E001"]
    assert deps["journal"].events[-1]["code"] == "upstream_employee_mismatch"
    assert deps["journal"].events[-1]["upstream_attempted"] is True
