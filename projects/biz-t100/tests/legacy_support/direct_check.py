# Historical regression support relocated from scripts; not a supported command.
"""一次性回环（loopback）MCP 检查。仅使用合成身份与捕获式模拟上游。

使用 BIZ 项目解释器运行。不使用输入配置、真实系统或常驻服务。
本开发工具不属于 wheel 制品。
"""

import asyncio
import json
import logging
import secrets
import socket
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import httpx2
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from biz_t100 import IdentityProfile, create_app
from biz_t100.access import FileServiceAccountAccess
from biz_t100.client import T100Config, T100EmployeeClient
from biz_t100.journal import SQLiteQueryJournal
from biz_t100.server import SCOPE, TOOL_NAME

ISSUER = "https://direct-check.example.invalid"
AUDIENCE = "synthetic-t100-direct-check"
ENTERPRISE = "DEMO-ENT"


class CheckFailed(Exception):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def private_file(path, value):
    path.write_text(value)
    path.chmod(0o600)


async def verify(folder):
    signing_key = RSAKeyPair.generate()  # 绝不持久化或打印。
    service_key = secrets.token_urlsafe(32)  # 合成数据，只写入临时私有文件。
    policy_path, key_path = folder / "policy.json", folder / "service.key"
    journal_path = folder / "journal" / "queries.sqlite3"
    people = {"user-a": "E001", "user-b": "E002"}
    private_file(
        policy_path,
        json.dumps(
            {
                "auth_mode": "service-account",
                "revision": "direct-check-only",
                "principals": [
                    {
                        "issuer": ISSUER,
                        "subject": user,
                        "employee_id": employee,
                        "allowed_clients": ["direct-check"],
                        "enabled": True,
                        "allow_employee_read": True,
                    }
                    for user, employee in people.items()
                ],
            }
        ),
    )
    private_file(key_path, service_key)
    requests = []
    active = 0
    peak = 0
    concurrent_gate = None

    async def upstream(request):
        nonlocal active, peak
        body = json.loads(request.content)
        require(body["key"] == service_key, "service_key_mismatch")
        require(body["datakey"]["EntId"] == ENTERPRISE, "enterprise_mismatch")
        employee = body["payload"]["std_data"]["parameter"]["employee_no"]
        require(employee in people.values(), "unexpected_employee")
        requests.append((ENTERPRISE, employee))
        active += 1
        peak = max(peak, active)
        try:
            if concurrent_gate is not None:
                if active == 2:
                    concurrent_gate.set()
                await concurrent_gate.wait()
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "std_data": {
                            "execution": {"code": "0"},
                            "parameter": {
                                "employee": [{"employee_no": employee, "full_name": "Synthetic"}]
                            },
                        }
                    }
                },
            )
        finally:
            active -= 1

    reader = T100EmployeeClient(
        T100Config(
            url="https://synthetic-t100.example.invalid/api",
            enterprise_id=ENTERPRISE,
            host_product="TEST",
            host_ip="127.0.0.1",
            host_account="synthetic-service",
            service_ip="127.0.0.1",
            service_id="direct-check",
        ),
        transport=httpx.MockTransport(upstream),
    )
    journal = SQLiteQueryJournal(journal_path, enterprise_id=ENTERPRISE)
    app = create_app(
        auth=JWTVerifier(
            public_key=signing_key.public_key,
            issuer=ISSUER,
            audience=AUDIENCE,
            required_scopes=[SCOPE],
        ),
        identity=IdentityProfile(
            issuer=ISSUER,
            audience=AUDIENCE,
            allowed_clients=frozenset({"direct-check"}),
        ),
        access=FileServiceAccountAccess(policy_path, key_path),
        reader=reader,
        journal=journal,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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

    def http_client(**kwargs):
        kwargs.update(trust_env=False, follow_redirects=False)
        return httpx2.AsyncClient(**kwargs)

    def client_for(user):
        token = signing_key.create_token(
            issuer=ISSUER,
            audience=AUDIENCE,
            subject=user,
            scopes=[SCOPE],
            expires_in_seconds=120,
            additional_claims={"azp": "direct-check"},
        )
        return Client(
            StreamableHttpTransport(url, auth=token, httpx_client_factory=http_client),
            mode="legacy",
            timeout=5,
            init_timeout=5,
        )

    checks = []
    successes = []

    async def own_record(client, user):
        before = len(requests)
        result = await client.call_tool(TOOL_NAME, {})
        value = result.structured_content
        require(value["data"]["employee_id"] == people[user], "self_record_mismatch")
        require(service_key not in json.dumps(value), "secret_exposed")
        successes.append((user, people[user], value["request_id"]))
        return {
            "user": user,
            "enterprise_id": ENTERPRISE,
            "employee_no": value["data"]["employee_id"],
            "request_id": value["request_id"],
            "before": before,
        }

    try:
        async with asyncio.timeout(20):
            while not server.started:
                if task.done():
                    await task
                    raise CheckFailed("server_not_started")
                await asyncio.sleep(0.01)
            async with client_for("user-a") as a, client_for("user-b") as b:
                tools = await a.list_tools()
                require([item.name for item in tools] == [TOOL_NAME], "unexpected_tool_list")
                require(not requests, "tool_discovery_dispatched")
                protocol = a.protocol_version
                checks.append({"case": "tools_list", "result": "passed"})
                for user, client in [("user-a", a), ("user-b", b)]:
                    value = await own_record(client, user)
                    require(len(requests) - value.pop("before") == 1, "wrong_request_count")
                    checks.append(
                        {"case": user, "result": "passed", **value, "upstream_requests": 1}
                    )
                concurrent_gate = asyncio.Event()
                before = len(requests)
                outputs = await asyncio.gather(own_record(a, "user-a"), own_record(b, "user-b"))
                require(peak == 2, "concurrency_not_exercised")
                require(
                    sorted(requests[before:]) == [(ENTERPRISE, "E001"), (ENTERPRISE, "E002")],
                    "concurrent_targets_mismatch",
                )
                require(len({row["request_id"] for row in outputs}) == 2, "shared_request_id")
                concurrent_gate = None
                checks.append(
                    {"case": "concurrent_a_b", "result": "passed", "upstream_requests": 2}
                )
                before = len(requests)
                denied = await a.call_tool(TOOL_NAME, {"employee_no": "E002"}, raise_on_error=False)
                require(
                    denied.is_error and "invalid_arguments" in denied.content[0].text,
                    "employee_argument_not_rejected",
                )
                require(len(requests) == before, "argument_rejection_dispatched")
                checks.append(
                    {
                        "case": "other_employee_argument",
                        "result": "rejected",
                        "upstream_requests": 0,
                    }
                )
            async with client_for("unmapped") as unknown:
                before = len(requests)
                denied = await unknown.call_tool(TOOL_NAME, {}, raise_on_error=False)
                require(
                    denied.is_error and "identity_not_enabled" in denied.content[0].text,
                    "unmapped_user_not_rejected",
                )
                require(len(requests) == before, "unmapped_user_dispatched")
                checks.append(
                    {"case": "unmapped_user", "result": "rejected", "upstream_requests": 0}
                )
            async with httpx.AsyncClient(trust_env=False, timeout=5) as raw:
                before = len(requests)
                response = await raw.post(
                    url,
                    headers={"Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                require(response.status_code in {401, 403}, "missing_token_not_rejected")
                require(len(requests) == before, "missing_token_dispatched")
                checks.append(
                    {"case": "missing_token", "result": "rejected", "upstream_requests": 0}
                )
            require(len(requests) == 4, "total_request_count_mismatch")
            with sqlite3.connect(journal_path.as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                rows = [dict(row) for row in db.execute("SELECT * FROM query_events")]
            for user, employee, request_id in successes:
                facts = [row for row in rows if row["request_id"] == request_id]
                require(
                    {row["event"] for row in facts} == {"query_started", "query_succeeded"},
                    "audit_events_mismatch",
                )
                require(
                    all(
                        (
                            row["issuer"],
                            row["subject"],
                            row["client_id"],
                            row["enterprise_id"],
                            row["employee_id"],
                            row["tool_name"],
                        )
                        == (ISSUER, user, "direct-check", ENTERPRISE, employee, TOOL_NAME)
                        for row in facts
                    ),
                    "audit_identity_mismatch",
                )
                require(
                    all(
                        row["upstream_attempted"] == (row["event"] == "query_succeeded")
                        for row in facts
                    ),
                    "audit_attempt_mismatch",
                )
            require(service_key not in json.dumps(rows), "audit_exposes_secret")
            require(await journal.pending_request_ids() == [], "pending_audit")
            checks.append({"case": "audit", "result": "passed", "successful_calls": 4})
            return {
                "status": "passed",
                "scope": "direct-loopback-synthetic",
                "protocol": protocol,
                "checks": checks,
                "upstream_requests": len(requests),
                "real_business_called": False,
                "persistent_service": False,
            }
    finally:
        server.should_exit = True
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
        with TemporaryDirectory(prefix="biz-t100-direct-") as tmp:
            result = asyncio.run(verify(Path(tmp)))
    except CheckFailed as exc:
        print(json.dumps({"status": "failed", "check": str(exc)}))
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "check": "direct_check_failed"}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
