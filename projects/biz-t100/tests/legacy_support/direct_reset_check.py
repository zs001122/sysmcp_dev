# Historical regression support relocated from scripts; not a supported command.
"""一次性回环（loopback）重置候选检查；无真实凭据，无业务 I/O。

使用项目解释器运行，不带参数。本开发脚本不被产品导入。
签名材料只留在内存；私有的合成配置与 SQLite 存储仅在本次调用中存在。
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import socket
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import httpx2
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from biz_t100.access import FileServiceAccountAccess
from biz_t100.client import T100Config
from biz_t100.reset_client import T100PasswordResetClient
from biz_t100.reset_server import RESET_SCOPE, RESET_TOOL_NAME, create_reset_app
from biz_t100.reset_store import SQLiteResetStore
from biz_t100.server import SCOPE, IdentityProfile

ISSUER = "https://reset-check.example.invalid"
AUDIENCE = "synthetic-t100-reset-check"
ENTERPRISE = "DEMO-ENT"
CLIENT = "direct-reset-check"
PEOPLE = {"user-a": ("E001", "LOGIN-A"), "user-b": ("E002", "LOGIN-B")}


class CheckFailed(Exception):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def private_file(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)


async def verify(folder):
    signing_key = RSAKeyPair.generate()  # 绝不持久化或打印。
    service_key = secrets.token_urlsafe(32)
    policy_path, key_path = folder / "policy.json", folder / "service.key"
    store_path = folder / "reset" / "operations.sqlite3"
    private_file(key_path, service_key)
    private_file(
        policy_path,
        json.dumps(
            {
                "auth_mode": "service-account",
                "revision": "direct-reset-check-only",
                "principals": [
                    {
                        "issuer": ISSUER,
                        "subject": subject,
                        "employee_id": employee,
                        "t100_account_id": account,
                        "allowed_clients": [CLIENT],
                        "enabled": True,
                        "allow_password_reset": True,
                    }
                    for subject, (employee, account) in PEOPLE.items()
                ],
            }
        ),
    )
    binding_version = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    requests, operations, checks = [], {}, []
    mode, gate, active, peak = "success", None, 0, 0

    async def upstream(request):
        nonlocal active, peak
        body = json.loads(request.content)
        require(request.method == "POST", "unexpected_method")
        require(body["key"] == service_key, "service_key_mismatch")
        require(body["datakey"]["EntId"] == ENTERPRISE, "enterprise_mismatch")
        require(body["service"]["name"] == "user.account.create", "service_mismatch")
        parameter = body["payload"]["std_data"]["parameter"]
        require(set(parameter) == {"master"} and len(parameter["master"]) == 1, "wrong_patch")
        master = parameter["master"][0]
        require(set(master) == {"gzxa001", "gzxa003", "password_reset"}, "extra_write_fields")
        target = (master["gzxa003"], master["gzxa001"])
        require(target in PEOPLE.values() and master["password_reset"] == "Y", "wrong_target")
        requests.append(target)
        active += 1
        peak = max(peak, active)
        try:
            if gate is not None:
                if active == 2:
                    gate.set()
                await gate.wait()
            if mode == "401":
                return httpx.Response(401)
            if mode == "timeout":
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return httpx.Response(200, json={"payload": {"std_data": {"execution": {"code": "0"}}}})
        finally:
            active -= 1

    resetter = T100PasswordResetClient(
        T100Config(
            url="https://synthetic-t100.example.invalid/api",
            enterprise_id=ENTERPRISE,
            host_product="TEST",
            host_ip="127.0.0.1",
            host_account="synthetic-service",
            service_ip="127.0.0.1",
            service_id=CLIENT,
        ),
        transport=httpx.MockTransport(upstream),
    )
    store = SQLiteResetStore(store_path, enterprise_id=ENTERPRISE)
    await store.prepare()
    app = create_reset_app(
        auth=JWTVerifier(
            public_key=signing_key.public_key,
            issuer=ISSUER,
            audience=AUDIENCE,
            required_scopes=[RESET_SCOPE],
        ),
        identity=IdentityProfile(
            issuer=ISSUER, audience=AUDIENCE, allowed_clients=frozenset({CLIENT})
        ),
        access=FileServiceAccountAccess(policy_path, key_path, permission="password_reset"),
        resetter=resetter,
        store=store,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server, task = None, None

    def token_for(user, scope=RESET_SCOPE):
        return signing_key.create_token(
            issuer=ISSUER,
            audience=AUDIENCE,
            subject=user,
            scopes=[scope],
            expires_in_seconds=120,
            additional_claims={"azp": CLIENT},
        )

    def http_client(**kwargs):
        kwargs.update(trust_env=False, follow_redirects=False)
        return httpx2.AsyncClient(**kwargs)

    def client_for(user):
        return Client(
            StreamableHttpTransport(url, auth=token_for(user), httpx_client_factory=http_client),
            mode="legacy",
            timeout=5,
            init_timeout=5,
        )

    async def call(client, arguments):
        result = await client.call_tool(RESET_TOOL_NAME, arguments, raise_on_error=False)
        text = " ".join(getattr(item, "text", "") for item in result.content)
        require(service_key not in text, "response_exposes_secret")
        return result, text

    async def success(client, user):
        operation_id = str(uuid4())
        result, _ = await call(client, {"confirm": True, "operation_id": operation_id})
        require(not result.is_error, "reset_not_acknowledged")
        require(
            result.structured_content
            == {"operation_id": operation_id, "status": "upstream_acknowledged"},
            "wrong_receipt",
        )
        operations[operation_id] = (user, "succeeded")
        return operation_id

    async def denied(client, arguments, expected, *, dispatched=False):
        before = len(requests)
        result, text = await call(client, arguments)
        require(result.is_error and expected in text, "expected_rejection_missing")
        require(len(requests) - before == int(dispatched), "rejection_request_count")

    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                access_log=False,
                log_level="critical",
                log_config=None,
                lifespan="on",
                timeout_graceful_shutdown=5,
                proxy_headers=False,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        async with asyncio.timeout(30):
            while not server.started:
                if task.done():
                    await task
                    raise CheckFailed("server_not_started")
                await asyncio.sleep(0.01)
            async with client_for("user-a") as a, client_for("user-b") as b:
                tools = await a.list_tools()
                require([tool.name for tool in tools] == [RESET_TOOL_NAME], "unexpected_tool_list")
                require(not requests, "discovery_dispatched")
                protocol = a.protocol_version
                checks.append({"项目": "仅暴露本人密码重置工具", "结果": "通过"})
                for user, client in [("user-a", a), ("user-b", b)]:
                    before = len(requests)
                    await success(client, user)
                    require(requests[before:] == [PEOPLE[user]], "self_target_mismatch")
                checks.append({"项目": "A/E001/LOGIN-A 与 B/E002/LOGIN-B 本人目标", "结果": "通过"})
                gate = asyncio.Event()
                before = len(requests)
                ids = await asyncio.gather(success(a, "user-a"), success(b, "user-b"))
                require(peak == 2 and len(set(ids)) == 2, "concurrency_not_exercised")
                require(sorted(requests[before:]) == sorted(PEOPLE.values()), "concurrent_targets")
                gate = None
                checks.append({"项目": "两用户真实并发且目标隔离", "结果": "通过"})
                await denied(a, {"confirm": True, "operation_id": ids[0]}, "reset_already_recorded")
                for field, value in [("employee_no", "E002"), ("account_id", "LOGIN-B")]:
                    await denied(
                        a,
                        {"confirm": True, "operation_id": str(uuid4()), field: value},
                        "invalid_arguments",
                    )
                await denied(a, {"operation_id": str(uuid4())}, "invalid_arguments")
                await denied(
                    a, {"confirm": False, "operation_id": str(uuid4())}, "confirmation_required"
                )
                checks.append({"项目": "重复操作、目标注入及缺少确认均零新请求", "结果": "通过"})
                async with httpx.AsyncClient(
                    trust_env=False, follow_redirects=False, timeout=5
                ) as raw:
                    before = len(requests)
                    response = await raw.post(
                        url,
                        headers={
                            "Authorization": "Bearer " + token_for("user-a", SCOPE),
                            "Accept": "application/json, text/event-stream",
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": RESET_TOOL_NAME,
                                "arguments": {"confirm": True, "operation_id": str(uuid4())},
                            },
                        },
                    )
                    require(response.status_code in {401, 403}, "readonly_scope_not_rejected")
                    require(len(requests) == before, "readonly_scope_dispatched")
                checks.append({"项目": "只读 scope 不能重置密码", "结果": "通过"})
                mode = "401"
                rejected_id = str(uuid4())
                await denied(
                    a,
                    {"confirm": True, "operation_id": rejected_id},
                    "upstream_unauthenticated",
                    dispatched=True,
                )
                operations[rejected_id] = ("user-a", "rejected")
                await denied(
                    a, {"confirm": True, "operation_id": rejected_id}, "reset_already_recorded"
                )
                checks.append({"项目": "401 明确拒绝，无自动重试及同操作重放", "结果": "通过"})
                mode = "timeout"
                unknown_id = str(uuid4())
                await denied(
                    b,
                    {"confirm": True, "operation_id": unknown_id},
                    "reset_outcome_unknown",
                    dispatched=True,
                )
                operations[unknown_id] = ("user-b", "unknown")
                await denied(
                    b, {"confirm": True, "operation_id": unknown_id}, "reset_already_recorded"
                )
                await denied(
                    b, {"confirm": True, "operation_id": str(uuid4())}, "reset_pending_review"
                )
                checks.append({"项目": "超时未知不重试，更换操作号仍锁定目标", "结果": "通过"})
            require(len(requests) == 6, "total_request_count")
            require(await store.pending_operation_ids() == [unknown_id], "pending_state_mismatch")
            with sqlite3.connect(store_path.as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                rows = [
                    dict(row) for row in db.execute("SELECT * FROM reset_events ORDER BY rowid")
                ]
                states = {
                    row["operation_id"]: row["state"]
                    for row in db.execute("SELECT operation_id,state FROM reset_operations")
                }
            require(len(rows) == 18 and len(states) == 6, "audit_count_mismatch")
            for operation_id, (user, outcome) in operations.items():
                facts = [row for row in rows if row["operation_id"] == operation_id]
                require(
                    [row["event"] for row in facts]
                    == ["reset_started", "reset_dispatched", "reset_" + outcome],
                    "audit_events",
                )
                require(states[operation_id] == outcome, "audit_state")
                terminal_code = {
                    "succeeded": None,
                    "rejected": "upstream_unauthenticated",
                    "unknown": "upstream_timeout",
                }[outcome]
                require([row["code"] for row in facts] == [None, None, terminal_code], "audit_code")
                require(
                    all(
                        (
                            row["issuer"],
                            row["subject"],
                            row["client_id"],
                            row["enterprise_id"],
                            row["employee_id"],
                            row["account_id"],
                            row["tool_name"],
                        )
                        == (ISSUER, user, CLIENT, ENTERPRISE, *PEOPLE[user], RESET_TOOL_NAME)
                        and row["binding_version"] == binding_version
                        for row in facts
                    ),
                    "audit_identity",
                )
            require(service_key not in json.dumps(rows), "audit_exposes_secret")
            checks.append({"项目": "6 项操作、18 条事件及未知目标占用可核对", "结果": "通过"})
            return {
                "status": "passed",
                "scope": "synthetic-reset-candidate",
                "protocol": protocol,
                "说明": "合成候选检查；成功仅表示 upstream_acknowledged，不证明密码已经生效。",
                "checks": checks,
                "upstream_requests": len(requests),
                "pending_unknown": 1,
                "real_business_called": False,
                "persistent_service": False,
            }
    finally:
        if server is not None:
            server.should_exit = True
        try:
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=8)
                except TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise CheckFailed("server_shutdown_timeout") from None
        finally:
            sock.close()


def main():
    logging.disable(logging.CRITICAL)
    try:
        with TemporaryDirectory(prefix="biz-t100-reset-check-") as tmp:
            folder = Path(tmp)
            folder.chmod(0o700)
            result = asyncio.run(verify(folder))
    except CheckFailed as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "scope": "synthetic-reset-candidate",
                    "check": str(exc),
                    "说明": "合成本机重置检查未通过。",
                    "real_business_called": False,
                },
                ensure_ascii=False,
            )
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "scope": "synthetic-reset-candidate",
                    "check": "direct_reset_check_failed",
                    "说明": "合成本机重置检查未通过。",
                    "real_business_called": False,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
