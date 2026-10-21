"""合成身份与捕获上游报文验证账号维护；禁止依赖真实运行资料。"""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import httpx2
import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from pydantic import SecretStr, ValidationError

import biz_t100.reset_client as transport_module
from biz_t100.account_write import (
    AccountWritePlan,
    T100AccountWriteClient,
    action_profile,
    create_account_write_app,
)
from biz_t100.account_write_store import SQLiteAccountWriteStore
from biz_t100.client import T100Config
from biz_t100.models import BusinessError, PasswordResetGrant
from biz_t100.server import IdentityProfile

ISSUER = "https://synthetic-account-write.example.invalid"
AUDIENCE = "synthetic-account-write"
SECRET = "synthetic-account-write-key"
ACTIONS = ("reset_password", "disable_account")


@pytest.fixture
def config():
    return T100Config(
        url="https://t100.example.invalid/api",
        enterprise_id="DEMO-ENT",
        host_product="TEST",
        host_ip="127.0.0.1",
        host_account="synthetic-admin",
        service_ip="127.0.0.1",
        service_id="synthetic-service",
    )


@pytest.fixture
def grant():
    return PasswordResetGrant(
        employee_id="E001",
        account_id="LOGIN-A",
        binding_version="synthetic-v1",
        credential=SecretStr(SECRET),
    )


def plan(action="disable_account", **changes):
    return AccountWritePlan(
        **{
            "action": action,
            "employee_id": "E001",
            "account_id": "LOGIN-A",
            "account_status": "N",
            **changes,
        }
    )


def ack():
    return {"payload": {"std_data": {"execution": {"code": "0"}}}}


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "enable_account"},
        {"action": "disable_account", "account_status": "Y"},
        {"account_status": "n"},
        {"account_status": None},
        {"employee_id": "E 001"},
        {"account_id": ""},
        {"password": "synthetic-password"},
        {"key": SECRET},
        {"detail": []},
    ],
)
def test_plan_rejects_ambiguous_status_target_and_extra_fields(changes):
    with pytest.raises(ValidationError):
        plan(**changes)


@pytest.mark.parametrize("action", ACTIONS)
def test_status_never_has_a_default(action):
    with pytest.raises(ValidationError):
        AccountWritePlan(action=action, employee_id="E001", account_id="LOGIN-A")


@pytest.mark.parametrize(
    "action,status", [("reset_password", "Y"), ("reset_password", "N"), ("disable_account", "N")]
)
async def test_exact_single_account_master_and_ack(config, grant, action, status):
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json=ack())

    selected = plan(action, account_status=status)
    receipt = await T100AccountWriteClient(
        config, selected, transport=httpx.MockTransport(upstream)
    ).reset(grant)
    assert receipt.model_dump() == {"acknowledged": True}
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["datakey"] == {"EntId": config.enterprise_id}
    assert body["service"]["name"] == "user.account.create"
    assert body["key"] == SECRET
    assert body["payload"]["std_data"]["parameter"] == {
        "master": [
            {
                "gzxa001": "LOGIN-A",
                "gzxa003": "E001",
                "gzxastus": status,
                "is_reproduce": "N",
                "password_reset": "Y" if action == "reset_password" else "N",
            }
        ]
    }
    assert SECRET not in selected.model_dump_json()
    assert SECRET not in receipt.model_dump_json()


async def test_default_transport_is_real_capable_without_retry_proxy_or_redirect(
    config, grant, monkeypatch
):
    # 捕获默认构造参数，然后仅在测试边界替换网络，不能真正访问远端。
    original = httpx.AsyncClient
    options = []
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://other.example.invalid/"})

    def observed(**kwargs):
        options.append(kwargs.copy())
        kwargs["transport"] = httpx.MockTransport(upstream)
        return original(**kwargs)

    monkeypatch.setattr(transport_module.httpx, "AsyncClient", observed)
    with pytest.raises(BusinessError, match="^upstream_rejected$"):
        await T100AccountWriteClient(config, plan()).reset(grant)
    assert len(requests) == 1
    assert options[0]["transport"] is None
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert requests[0].extensions["timeout"] == {
        "connect": 3.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }


@pytest.mark.parametrize("field,value", [("employee_id", "E002"), ("account_id", "LOGIN-B")])
async def test_plan_must_match_both_authorized_targets_before_http(config, grant, field, value):
    requests = []
    writer = T100AccountWriteClient(
        config, plan(), transport=httpx.MockTransport(lambda request: requests.append(request))
    )
    with pytest.raises(BusinessError, match="^authorization_changed$"):
        await writer.reset(grant.model_copy(update={field: value}))
    assert requests == []


@pytest.mark.parametrize(
    "field,value", [("employee_id", ""), ("account_id", " "), ("binding_version", "")]
)
async def test_bypassed_grant_cannot_dispatch(config, grant, field, value):
    writer = T100AccountWriteClient(
        config, plan(), transport=httpx.MockTransport(lambda request: pytest.fail("no HTTP"))
    )
    with pytest.raises(BusinessError, match="^upstream_contract_error$"):
        await writer.reset(grant.model_copy(update={field: value}))


def test_copied_plan_or_config_cannot_bypass_constructor_validation(config):
    with pytest.raises(ValidationError):
        T100AccountWriteClient(config, plan().model_copy(update={"account_status": "Y"}))
    with pytest.raises(ValidationError):
        T100AccountWriteClient(config.model_copy(update={"url": "http://example.invalid"}), plan())


async def test_concurrent_enterprises_keep_credentials_and_targets_separate(config, grant):
    captured = []
    arrived = asyncio.Event()

    async def upstream(request):
        captured.append(json.loads(request.content))
        if len(captured) == 2:
            arrived.set()
        await asyncio.wait_for(arrived.wait(), timeout=2)
        return httpx.Response(200, json=ack())

    other_grant = grant.model_copy(
        update={
            "employee_id": "E002",
            "account_id": "LOGIN-B",
            "credential": SecretStr("synthetic-other-key"),
        }
    )
    writers = [
        T100AccountWriteClient(
            config, plan("reset_password"), transport=httpx.MockTransport(upstream)
        ),
        T100AccountWriteClient(
            config.model_copy(update={"enterprise_id": "OTHER-ENT"}),
            plan(employee_id="E002", account_id="LOGIN-B"),
            transport=httpx.MockTransport(upstream),
        ),
    ]
    await asyncio.gather(writers[0].reset(grant), writers[1].reset(other_grant))
    assert {
        (
            row["datakey"]["EntId"],
            row["key"],
            row["payload"]["std_data"]["parameter"]["master"][0]["gzxa003"],
            row["payload"]["std_data"]["parameter"]["master"][0]["gzxa001"],
        )
        for row in captured
    } == {
        ("DEMO-ENT", SECRET, "E001", "LOGIN-A"),
        ("OTHER-ENT", "synthetic-other-key", "E002", "LOGIN-B"),
    }


class GrantedAccess:
    def __init__(self, grant):
        self.grant = grant
        self.enabled = True
        self.calls = 0

    async def authorize(self, caller):
        self.calls += 1
        if not self.enabled or caller.subject != "user-a":
            raise BusinessError("permission_denied")
        return self.grant


class Chain:
    def __init__(self, folder, config, grant, action):
        self.action = action
        self.tool, self.scope = action_profile(action)
        self.key = RSAKeyPair.generate()
        self.access = GrantedAccess(grant)
        self.requests = []
        self.mode = 200
        self.after_upstream = None
        self.database = folder / "audit" / "write.sqlite3"
        self.store = SQLiteAccountWriteStore(
            self.database, enterprise_id=config.enterprise_id, action=action
        )
        self.writer = T100AccountWriteClient(
            config, plan(action), transport=httpx.MockTransport(self.upstream)
        )
        self.dependencies = dict(
            auth=JWTVerifier(
                public_key=self.key.public_key,
                issuer=ISSUER,
                audience=AUDIENCE,
                required_scopes=[self.scope],
            ),
            identity=IdentityProfile(
                issuer=ISSUER, audience=AUDIENCE, allowed_clients=frozenset({"write-client"})
            ),
            access=self.access,
            writer=self.writer,
            store=self.store,
            action=action,
        )

    async def upstream(self, request):
        self.requests.append(json.loads(request.content))
        if self.after_upstream:
            self.after_upstream()
        if self.mode == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        return httpx.Response(self.mode, json=ack())

    @asynccontextmanager
    async def connection(self):
        app = create_account_write_app(**self.dependencies)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                yield client

    async def rpc(
        self,
        client,
        *,
        method="tools/call",
        token=True,
        scope=None,
        signer=None,
        arguments=None,
        name=None,
        operation=None,
    ):
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        }
        if token:
            value = (signer or self.key).create_token(
                issuer=ISSUER,
                audience=AUDIENCE,
                subject="user-a",
                scopes=[scope or self.scope],
                additional_claims={
                    "azp": "write-client",
                    "employee_no": "E002",
                    "account_id": "LOGIN-B",
                },
            )
            headers["Authorization"] = f"Bearer {value}"
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if method == "tools/call":
            message["params"] = {
                "name": name or self.tool,
                "arguments": arguments
                if arguments is not None
                else {"confirm": True, "operation_id": operation or str(uuid4())},
            }
        return await client.post("/mcp", headers=headers, json=message)

    def rows(self):
        with sqlite3.connect(self.database) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM reset_operations")]


def error(response, code):
    assert response.status_code == 200, response.text
    payload = response.json()["result"]
    assert payload["isError"]
    assert code in payload["content"][0]["text"]
    assert SECRET not in response.text


@pytest.fixture(params=ACTIONS)
def chain(tmp_path, config, grant, request):
    return Chain(tmp_path, config, grant, request.param)


async def test_action_has_only_its_tool_and_cannot_accept_target_override(chain):
    async with chain.connection() as client:
        response = await chain.rpc(client, method="tools/list")
        tools = response.json()["result"]["tools"]
        assert [tool["name"] for tool in tools] == [chain.tool]
        assert set(tools[0]["inputSchema"]["properties"]) == {"confirm", "operation_id"}
        assert tools[0]["annotations"]["destructiveHint"] is True
        other = (
            "reset_my_t100_password"
            if chain.action == "disable_account"
            else "disable_my_t100_account"
        )
        error(await chain.rpc(client, name=other), "tool_not_available")
        for field in ("employee_no", "account_id", "status", "key", "password"):
            error(
                await chain.rpc(
                    client,
                    arguments={
                        "confirm": True,
                        "operation_id": str(uuid4()),
                        field: "synthetic-other",
                    },
                ),
                "invalid_arguments",
            )
    assert chain.requests == []
    assert chain.access.calls == 0


@pytest.mark.parametrize("case", ["missing", "forged", "wrong-scope", "other-action"])
async def test_native_identity_rejection_is_before_access_and_http(chain, case):
    options = {
        "missing": {"token": False},
        "forged": {"signer": RSAKeyPair.generate()},
        "wrong-scope": {"scope": "t100:employee:read:self"},
        "other-action": {
            "scope": action_profile(
                "reset_password" if chain.action == "disable_account" else "disable_account"
            )[1]
        },
    }[case]
    async with chain.connection() as client:
        response = await chain.rpc(client, **options)
    assert response.status_code in {401, 403}
    assert chain.access.calls == 0
    assert chain.requests == []


async def test_success_is_ack_only_and_repeated_operation_never_replays(chain):
    operation = str(uuid4())
    async with chain.connection() as client:
        response = await chain.rpc(client, operation=operation)
        result = response.json()["result"]
        assert not result.get("isError"), response.text
        assert result["structuredContent"] == {
            "operation_id": operation,
            "status": "upstream_acknowledged",
        }
        error(await chain.rpc(client, operation=operation), "reset_already_recorded")
    assert len(chain.requests) == 1
    master = chain.requests[0]["payload"]["std_data"]["parameter"]["master"][0]
    assert master["gzxa003"] == "E001" and master["gzxa001"] == "LOGIN-A"
    assert chain.rows()[0]["state"] == "succeeded"
    assert chain.rows()[0]["tool_name"] == chain.tool


@pytest.mark.parametrize("mode", [401, 403, 500, "timeout"])
async def test_all_post_dispatch_errors_keep_target_locked(chain, mode):
    chain.mode = mode
    async with chain.connection() as client:
        error(await chain.rpc(client), "reset_outcome_unknown")
        error(await chain.rpc(client), "reset_pending_review")
    assert len(chain.requests) == 1
    assert chain.rows()[0]["state"] == "unknown"


async def test_revocation_after_ack_never_returns_success(chain):
    chain.after_upstream = lambda: setattr(chain.access, "enabled", False)
    async with chain.connection() as client:
        error(await chain.rpc(client), "reset_outcome_unknown")
    assert len(chain.requests) == 1
    assert chain.rows()[0]["state"] == "unknown"


def test_wrong_writer_action_cannot_be_exposed(chain):
    chain.dependencies["action"] = (
        "reset_password" if chain.action == "disable_account" else "disable_account"
    )
    with pytest.raises(ValueError, match="Writer action"):
        create_account_write_app(**chain.dependencies)


async def test_unknown_reset_blocks_disable_on_the_same_durable_target(tmp_path, config, grant):
    reset = Chain(tmp_path, config, grant, "reset_password")
    disable = Chain(tmp_path, config, grant, "disable_account")
    reset.mode = 401
    async with reset.connection() as client:
        error(await reset.rpc(client), "reset_outcome_unknown")
    async with disable.connection() as client:
        error(await disable.rpc(client), "reset_pending_review")
    assert len(reset.requests) == 1
    assert disable.requests == []
    assert len(reset.rows()) == 1
    assert reset.rows()[0]["state"] == "unknown"
    assert reset.rows()[0]["tool_name"] == "reset_my_t100_password"


async def test_successful_reset_then_disable_share_history_and_audit_action(
    tmp_path, config, grant
):
    reset = Chain(tmp_path, config, grant, "reset_password")
    disable = Chain(tmp_path, config, grant, "disable_account")
    same_operation = str(uuid4())
    async with reset.connection() as client:
        result = await reset.rpc(client, operation=same_operation)
        assert not result.json()["result"].get("isError")
    async with disable.connection() as client:
        error(await disable.rpc(client, operation=same_operation), "reset_already_recorded")
        result = await disable.rpc(client)
        assert not result.json()["result"].get("isError")
    assert len(reset.requests) == len(disable.requests) == 1
    assert {row["tool_name"] for row in reset.rows()} == {
        "reset_my_t100_password",
        "disable_my_t100_account",
    }
    assert all(row["state"] == "succeeded" for row in reset.rows())
