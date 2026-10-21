"""使用合成签名、策略、HTTP 与 SQLite 的原生 FastMCP 重置链路。"""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from uuid import uuid4

import anyio
import httpx
import httpx2
import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

import biz_t100.access as access_module
from biz_t100.access import FileServiceAccountAccess
from biz_t100.client import T100Config
from biz_t100.models import BusinessError, Caller
from biz_t100.reset_client import T100PasswordResetClient
from biz_t100.reset_server import RESET_SCOPE, RESET_TOOL_NAME, create_reset_app
from biz_t100.reset_store import SQLiteResetStore
from biz_t100.server import SCOPE, TOOL_NAME, IdentityProfile

ISSUER = "https://reset-identity.example.invalid"
AUDIENCE = "synthetic-password-reset"
SECRET = "synthetic-reset-service-key"
AUTO = object()
PEOPLE = {"user-a": ("E001", "LOGIN-A"), "user-b": ("E002", "LOGIN-B")}


def private_file(path, content):
    pending = path.with_name(path.name + ".new")
    pending.write_text(content)
    pending.chmod(0o600)
    pending.replace(path)


class FaultStore(SQLiteResetStore):
    fail = None
    after_reserve = None
    after_dispatch = None

    async def reserve(self, **values):
        if self.fail == "reserve":
            raise OSError(SECRET)
        await super().reserve(**values)
        if self.after_reserve:
            self.after_reserve()

    async def mark_dispatched(self, **values):
        if self.fail == "dispatch":
            raise OSError(SECRET)
        await super().mark_dispatched(**values)
        if self.after_dispatch:
            self.after_dispatch()

    async def finish(self, **values):
        if self.fail == "finish":
            raise OSError(SECRET)
        await super().finish(**values)


class Chain:
    def __init__(self, folder, key_reads, enterprise="DEMO-ENT"):
        folder.mkdir(mode=0o700, exist_ok=True)
        self.enterprise = enterprise
        self.secret = SECRET + enterprise
        self.key = RSAKeyPair.generate()
        self.policy_file = folder / "access.json"
        self.key_file = folder / "service.key"
        self.db_file = folder / "audit" / "reset.sqlite3"
        self.policy = {
            "auth_mode": "service-account",
            "revision": "synthetic-v1",
            "principals": [
                {
                    "issuer": ISSUER,
                    "subject": sub,
                    "employee_id": employee,
                    "t100_account_id": account,
                    "allowed_clients": ["reset-client"],
                    "enabled": True,
                    "allow_employee_read": False,
                    "allow_password_reset": True,
                }
                for sub, (employee, account) in PEOPLE.items()
            ],
        }
        self.save()
        private_file(self.key_file, self.secret)
        self.key_reads = key_reads
        self.requests = []
        self.mode = "ack"
        self.after_upstream = None
        self.gate = None
        self.arrived = 0
        self.store = FaultStore(self.db_file, enterprise_id=enterprise)
        self.dependencies = dict(
            auth=JWTVerifier(
                public_key=self.key.public_key,
                issuer=ISSUER,
                audience=AUDIENCE,
                required_scopes=[RESET_SCOPE],
            ),
            identity=IdentityProfile(
                issuer=ISSUER,
                audience=AUDIENCE,
                allowed_clients=frozenset({"reset-client"}),
            ),
            access=FileServiceAccountAccess(
                self.policy_file,
                self.key_file,
                permission="password_reset",
            ),
            resetter=T100PasswordResetClient(
                T100Config(
                    url="https://reset.example.invalid/api",
                    enterprise_id=enterprise,
                    host_product="TEST",
                    host_ip="127.0.0.1",
                    host_account="synthetic",
                    service_ip="127.0.0.1",
                    service_id="offline-only",
                ),
                transport=httpx.MockTransport(self.upstream),
            ),
            store=self.store,
        )

    def save(self):
        private_file(self.policy_file, json.dumps(self.policy))

    def revoke(self):
        self.policy["principals"][0]["allow_password_reset"] = False
        self.save()

    def rebind(self):
        self.policy["principals"][0]["t100_account_id"] = "LOGIN-CHANGED"
        self.save()

    def token(self, user="user-a", *, signer=None, claims=None, **changes):
        options = dict(
            issuer=ISSUER,
            audience=AUDIENCE,
            subject=user,
            scopes=[RESET_SCOPE],
            additional_claims={"azp": "reset-client", **(claims or {})},
        )
        options.update(changes)
        return (signer or self.key).create_token(**options)

    @asynccontextmanager
    async def connection(self):
        app = create_reset_app(**self.dependencies)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://127.0.0.1",
            ) as client:
                yield client

    async def call(
        self,
        client,
        *,
        user="user-a",
        token=AUTO,
        operation_id=None,
        arguments=None,
        headers=None,
        name=RESET_TOOL_NAME,
    ):
        if token is AUTO:
            token = self.token(user)
        request_headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
            **(headers or {}),
        }
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        return await client.post(
            "/mcp",
            headers=request_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": arguments
                    if arguments is not None
                    else {
                        "confirm": True,
                        "operation_id": operation_id or str(uuid4()),
                    },
                },
            },
        )

    async def upstream(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        assert body["key"] == self.secret
        assert body["datakey"] == {"EntId": self.enterprise}
        assert body["service"]["name"] == "user.account.create"
        master = body["payload"]["std_data"]["parameter"]["master"]
        assert len(master) == 1
        assert set(master[0]) == {"gzxa001", "gzxa003", "password_reset"}
        assert (master[0]["gzxa003"], master[0]["gzxa001"]) in PEOPLE.values()
        assert master[0]["password_reset"] == "Y"
        if self.gate is not None:
            self.arrived += 1
            if self.arrived == 2:
                self.gate.set()
            await self.gate.wait()
        if self.after_upstream:
            self.after_upstream()
        if self.mode == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if self.mode == "tool-timeout":
            await anyio.sleep_forever()
        if self.mode in {401, 403, 500}:
            return httpx.Response(self.mode, text=SECRET)
        std = {"execution": {"code": "0"}}
        if self.mode == "business-error":
            std["execution"]["code"] = "UNDEFINED"
        if self.mode == "unexpected-data":
            std["parameter"] = {"employee": [{"employee_no": "OTHER"}]}
        return httpx.Response(200, json={"payload": {"std_data": std}})

    def rows(self, table="reset_operations"):
        if not self.db_file.exists():
            return []
        with sqlite3.connect(self.db_file.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def reads(self):
        return self.key_reads.count(self.key_file)


@pytest.fixture
def chain(tmp_path, monkeypatch):
    reads = []
    original = access_module._private_bytes

    def counted(path, maximum):
        if path.name == "service.key":
            reads.append(path)
        return original(path, maximum)

    monkeypatch.setattr(access_module, "_private_bytes", counted)
    return Chain(tmp_path, reads)


def succeeded(response):
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def denied(response, code):
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"], result
    assert code in result["content"][0]["text"], result
    assert SECRET not in response.text


async def test_a_b_are_bound_to_separate_accounts_and_durable_audit(chain):
    async with chain.connection() as client:
        for user in PEOPLE:
            result = succeeded(await chain.call(client, user=user))
            assert result["status"] == "upstream_acknowledged"
    assert len(chain.requests) == 2
    rows = chain.rows()
    assert {
        (row["enterprise_id"], row["subject"], row["employee_id"], row["account_id"])
        for row in rows
    } == {("DEMO-ENT", user, *target) for user, target in PEOPLE.items()}
    assert all(row["state"] == "succeeded" for row in rows)
    assert all(row["tool_name"] == RESET_TOOL_NAME for row in rows)
    assert len(chain.rows("reset_events")) == 6
    assert SECRET not in json.dumps(chain.rows("reset_events"))


async def test_actual_concurrent_a_b_never_cross_targets(chain):
    chain.gate = anyio.Event()
    async with chain.connection() as client:
        with anyio.fail_after(5):
            results = await asyncio.gather(
                chain.call(client, user="user-a"),
                chain.call(client, user="user-b"),
            )
    assert chain.arrived == 2
    for result in results:
        succeeded(result)
    assert len(chain.requests) == 2


@pytest.mark.parametrize(
    "case,code",
    [
        ("unmapped", "identity_not_enabled"),
        ("disabled", "identity_not_enabled"),
        ("read-only", "permission_denied"),
        ("no-account", "account_not_linked"),
        ("client-denied", "permission_denied"),
        ("duplicate", "policy_unavailable"),
        ("shared-account", "policy_unavailable"),
    ],
)
async def test_policy_denials_read_no_service_key_and_send_nothing(chain, case, code):
    user = "user-a"
    row = chain.policy["principals"][0]
    if case == "unmapped":
        user = "unmapped"
    elif case == "disabled":
        row["enabled"] = False
    elif case == "read-only":
        row["allow_employee_read"] = True
        del row["allow_password_reset"]
    elif case == "no-account":
        del row["t100_account_id"]
    elif case == "client-denied":
        row["allowed_clients"] = ["another-client"]
    elif case == "duplicate":
        chain.policy["principals"].append({**row, "employee_id": "E003"})
    elif case == "shared-account":
        chain.policy["principals"][1]["t100_account_id"] = row["t100_account_id"]
    chain.save()
    async with chain.connection() as client:
        denied(await chain.call(client, user=user), code)
    assert chain.requests == [] and chain.reads() == 0 and chain.rows() == []


@pytest.mark.parametrize(
    "case", ["missing", "expired", "forged", "read-scope", "audience", "client"]
)
async def test_token_denials_do_not_read_key_or_dispatch(chain, case):
    token = {
        "missing": lambda: None,
        "expired": lambda: chain.token(expires_in_seconds=-60),
        "forged": lambda: chain.token(signer=RSAKeyPair.generate()),
        "read-scope": lambda: chain.token(scopes=[SCOPE]),
        "audience": lambda: chain.token(audience="wrong-resource"),
        "client": lambda: chain.token(claims={"azp": "unknown-client"}),
    }[case]()
    async with chain.connection() as client:
        response = await chain.call(client, token=token)
    if case == "client":
        denied(response, "unauthenticated")
    else:
        assert response.status_code in {401, 403}
    assert chain.requests == [] and chain.reads() == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"employee_no": "E002"},
        {"account_id": "LOGIN-B"},
        {"password": SECRET},
        {"key": SECRET},
        {"name": "Another"},
    ],
)
async def test_target_and_password_arguments_are_rejected_without_echo(chain, extra):
    args = {"confirm": True, "operation_id": str(uuid4()), **extra}
    async with chain.connection() as client:
        denied(await chain.call(client, arguments=args), "invalid_arguments")
    assert chain.requests == [] and chain.reads() == 0


@pytest.mark.parametrize(
    "args,code",
    [
        ({}, "invalid_arguments"),
        ({"confirm": False, "operation_id": str(uuid4())}, "confirmation_required"),
        ({"confirm": "true", "operation_id": str(uuid4())}, "invalid_arguments"),
        ({"confirm": 1, "operation_id": str(uuid4())}, "invalid_arguments"),
        ({"confirm": True, "operation_id": "not-a-uuid"}, "invalid_arguments"),
    ],
)
async def test_confirmation_and_operation_id_are_strict(chain, args, code):
    async with chain.connection() as client:
        denied(await chain.call(client, arguments=args), code)
    assert chain.requests == [] and chain.reads() == 0


async def test_claims_and_headers_cannot_change_target(chain):
    async with chain.connection() as client:
        succeeded(
            await chain.call(
                client,
                token=chain.token(
                    claims={
                        "employee_no": "E002",
                        "account_id": "LOGIN-B",
                        "email": "other@example.invalid",
                    }
                ),
                headers={"X-Employee-No": "E002", "X-API-Key": "MetaMCP-synthetic"},
            )
        )
    target = chain.requests[0]["payload"]["std_data"]["parameter"]["master"][0]
    assert (target["gzxa003"], target["gzxa001"]) == PEOPLE["user-a"]


async def test_same_operation_replay_even_after_reopen_never_dispatches(chain):
    operation = str(uuid4())
    async with chain.connection() as client:
        succeeded(await chain.call(client, operation_id=operation))
        denied(await chain.call(client, operation_id=operation), "reset_already_recorded")
    chain.dependencies["store"] = SQLiteResetStore(chain.db_file, enterprise_id=chain.enterprise)
    async with chain.connection() as client:
        denied(await chain.call(client, operation_id=operation), "reset_already_recorded")
        denied(
            await chain.call(client, operation_id=operation, user="user-b"),
            "reset_already_recorded",
        )
    assert len(chain.requests) == 1


async def test_concurrent_duplicate_operation_sends_at_most_one(chain):
    operation = str(uuid4())
    async with chain.connection() as client:
        responses = await asyncio.gather(
            chain.call(client, operation_id=operation),
            chain.call(client, operation_id=operation),
        )
    assert sum(not response.json()["result"].get("isError") for response in responses) == 1
    assert len(chain.requests) == 1


@pytest.mark.parametrize("hook", ["revoke", "rebind"])
async def test_revoke_or_rebind_after_start_is_not_sent(chain, hook):
    chain.store.after_reserve = getattr(chain, hook)
    async with chain.connection() as client:
        denied(
            await chain.call(client),
            "permission_denied" if hook == "revoke" else "authorization_changed",
        )
    assert chain.requests == []
    assert chain.rows()[0]["state"] == "not_sent"


async def test_revoke_during_dispatch_commit_is_rechecked_before_http(chain):
    chain.store.after_dispatch = chain.revoke
    async with chain.connection() as client:
        denied(await chain.call(client), "reset_outcome_unknown")
    assert chain.requests == []
    assert chain.rows()[0]["state"] == "unknown"


@pytest.mark.parametrize("hook", ["revoke", "rebind"])
async def test_after_dispatch_authorization_change_never_returns_success(chain, hook):
    chain.after_upstream = getattr(chain, hook)
    async with chain.connection() as client:
        denied(await chain.call(client), "reset_outcome_unknown")
    assert len(chain.requests) == 1
    assert chain.rows()[0]["state"] == "unknown"


@pytest.mark.parametrize(
    "mode", ["timeout", "tool-timeout", 500, "business-error", "unexpected-data"]
)
async def test_uncertain_write_blocks_new_operation_and_never_retries(chain, mode):
    chain.mode = mode
    if mode == "tool-timeout":
        chain.dependencies["budget_seconds"] = 0.2
    async with chain.connection() as client:
        denied(await chain.call(client), "reset_outcome_unknown")
        denied(await chain.call(client), "reset_pending_review")
    assert len(chain.requests) == 1
    assert chain.rows()[0]["state"] == "unknown"


@pytest.mark.parametrize(
    "status,code", [(401, "upstream_unauthenticated"), (403, "business_forbidden")]
)
async def test_service_rejection_is_one_call(chain, status, code):
    chain.mode = status
    async with chain.connection() as client:
        denied(await chain.call(client), code)
    assert len(chain.requests) == 1
    assert chain.rows()[0]["state"] == "rejected"


@pytest.mark.parametrize("stage", ["reserve", "dispatch", "finish"])
async def test_audit_failure_never_reports_success(chain, stage):
    chain.store.fail = stage
    async with chain.connection() as client:
        response = await chain.call(client)
        denied(response, "journal_unavailable")
    assert len(chain.requests) == (1 if stage == "finish" else 0)
    if stage == "finish":
        assert chain.rows()[0]["state"] == "dispatched"
        chain.store.fail = None
        async with chain.connection() as client:
            denied(await chain.call(client), "reset_pending_review")
        assert len(chain.requests) == 1


async def test_write_grant_does_not_implicitly_enable_read_permission(chain):
    caller = Caller(
        issuer=ISSUER,
        subject="user-a",
        client_id="reset-client",
    )
    with pytest.raises(BusinessError, match="permission_denied"):
        await FileServiceAccountAccess(chain.policy_file, chain.key_file).authorize(caller)
    assert chain.reads() == 0


async def test_reset_endpoint_exposes_only_destructive_reset_tool(chain):
    async with chain.connection() as client:
        response = await client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {chain.token()}",
                "MCP-Protocol-Version": "2025-06-18",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        tools = response.json()["result"]["tools"]
        assert [tool["name"] for tool in tools] == [RESET_TOOL_NAME]
        assert tools[0]["annotations"]["destructiveHint"] is True
        assert set(tools[0]["inputSchema"]["properties"]) == {"confirm", "operation_id"}
        denied(await chain.call(client, name=TOOL_NAME), "tool_not_available")
    assert chain.requests == [] and chain.reads() == 0


async def test_same_employee_across_enterprises_has_separate_credential_and_audit(chain, tmp_path):
    other = Chain(tmp_path / "other", chain.key_reads, enterprise="DEMO-OTHER")
    async with chain.connection() as left, other.connection() as right:
        results = await asyncio.gather(chain.call(left), other.call(right))
    for response in results:
        succeeded(response)
    assert chain.rows()[0]["enterprise_id"] == "DEMO-ENT"
    assert other.rows()[0]["enterprise_id"] == "DEMO-OTHER"
    assert chain.requests[0]["key"] != other.requests[0]["key"]


async def test_cancelled_asgi_request_after_upstream_keeps_target_reserved(chain):
    entered = asyncio.Event()
    chain.after_upstream = entered.set
    chain.mode = "tool-timeout"
    # 分离的传输工作也要一并约束：仅 HTTP 取消绝不能
    # 被解释为回滚，也不能授权替代性写入。
    chain.dependencies["budget_seconds"] = 1.0
    operation = str(uuid4())
    async with chain.connection() as client:
        request = asyncio.create_task(chain.call(client, operation_id=operation))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        finally:
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    rows = chain.rows()
    assert len(rows) == 1
    assert rows[0]["operation_id"] == operation
    assert rows[0]["state"] in {"unknown", "dispatched"}
    assert (rows[0]["employee_id"], rows[0]["account_id"]) == PEOPLE["user-a"]
    assert len(chain.requests) == 1
    events = chain.rows("reset_events")
    assert [row["event"] for row in events[:2]] == ["reset_started", "reset_dispatched"]
    assert all(row["operation_id"] == operation for row in events)
    assert all(row["event"] not in {"reset_succeeded", "reset_not_sent"} for row in events)
    async with chain.connection() as client:
        denied(await chain.call(client, operation_id=str(uuid4())), "reset_pending_review")
    assert len(chain.requests) == 1


async def test_deadline_after_success_commit_does_not_return_success_or_add_terminal(
    chain, monkeypatch
):
    from types import SimpleNamespace

    import biz_t100.reset_server as reset_server_module

    original_monotonic = reset_server_module.time.monotonic
    elapsed = 0.0

    class CommitThenExpire(FaultStore):
        async def finish(self, **values):
            nonlocal elapsed
            await super().finish(**values)
            if values["outcome"] == "succeeded":
                # 只在真实的持久化提交之后推进用例的超时时钟；
                # 不干扰事件循环的时钟。
                elapsed = 20.0

    monkeypatch.setattr(
        reset_server_module,
        "time",
        SimpleNamespace(monotonic=lambda: original_monotonic() + elapsed),
    )
    chain.dependencies["store"] = CommitThenExpire(chain.db_file, enterprise_id=chain.enterprise)
    operation = str(uuid4())
    async with chain.connection() as client:
        denied(await chain.call(client, operation_id=operation), "reset_timeout")
        denied(await chain.call(client, operation_id=operation), "reset_already_recorded")
    assert len(chain.requests) == 1
    rows = chain.rows()
    assert len(rows) == 1
    assert rows[0]["operation_id"] == operation
    assert rows[0]["state"] == "succeeded"
    events = chain.rows("reset_events")
    assert [row["event"] for row in events] == [
        "reset_started",
        "reset_dispatched",
        "reset_succeeded",
    ]
    assert all(row["operation_id"] == operation for row in events)
    assert (rows[0]["employee_id"], rows[0]["account_id"]) == PEOPLE["user-a"]


async def test_real_read_grant_cannot_authorize_reset_when_injected(chain):
    for row in chain.policy["principals"]:
        row["allow_employee_read"] = True
        row["allow_password_reset"] = False
    chain.save()
    # 真实的读授权器会合法地返回 EmployeeGrant 并读取其配置的密钥；
    # 重置用例必须拒绝该授权的权限。
    chain.dependencies["access"] = FileServiceAccountAccess(
        chain.policy_file, chain.key_file, permission="employee_read"
    )
    async with chain.connection() as client:
        denied(await chain.call(client), "permission_denied")
    assert chain.reads() == 1
    assert chain.requests == []
    assert chain.rows() == []
    assert chain.rows("reset_events") == []


@pytest.mark.parametrize(
    "read_code", ["employee_not_found", "employee_not_unique", "upstream_employee_mismatch"]
)
async def test_read_result_codes_do_not_extend_reset_audit_contract(chain, read_code):
    original = chain.dependencies["resetter"].reset

    async def unexpected_read_error(grant):
        await original(grant)
        raise BusinessError(read_code)

    chain.dependencies["resetter"].reset = unexpected_read_error
    async with chain.connection() as client:
        denied(await chain.call(client), "reset_outcome_unknown")
        denied(await chain.call(client), "reset_pending_review")
    assert len(chain.requests) == 1
    assert chain.rows()[0]["state"] == "unknown"
    assert chain.rows()[0]["code"] == "reset_failed"
    assert chain.rows("reset_events")[-1]["code"] == "reset_failed"
