# Historical regression support relocated from scripts; not a supported command.
"""内部实验环境的运维侧客户端；产品绝不导入。"""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from internal_support import issue_token

from biz_t100.access import _private_bytes
from biz_t100.models import BusinessError
from biz_t100.reset_server import RESET_ERRORS, RESET_TOOL_NAME
from biz_t100.server import ERROR_CODES, TOOL_NAME

_NAMES = {"read": TOOL_NAME, "reset": RESET_TOOL_NAME}
_CODES = (
    ERROR_CODES
    | RESET_ERRORS
    | {
        "invalid_arguments",
        "confirmation_required",
        "tool_not_available",
    }
)


def _error(result):
    for item in result.content:
        text = getattr(item, "text", "")
        if text in _CODES:
            return text
        try:
            value = json.loads(text)
            if isinstance(value, dict) and value.get("error") in _CODES:
                return value["error"]
        except (ValueError, TypeError):
            pass
    return "internal_call_failed"


def _http_client(**kwargs):
    kwargs.update(trust_env=False, follow_redirects=False)
    return httpx2.AsyncClient(**kwargs)


@asynccontextmanager
async def connection(state, service, url, *, user="user-a", token_file=None):
    temporary = None
    if token_file is None:
        temporary = state.root / "tokens" / (str(uuid4()) + ".jwt")
        issue_token(state.root, user=user, service=service, output=temporary)
        token_file = temporary
    try:
        token = _private_bytes(token_file, 16_384).decode().strip()
        async with Client(
            StreamableHttpTransport(url, auth=token, httpx_client_factory=_http_client),
            mode="legacy",
            timeout=15,
            init_timeout=5,
        ) as client:
            yield client
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def call_once(state, service, url, *, user, token_file, confirm, operation_id):
    if service == "read" and (confirm or operation_id is not None):
        raise BusinessError("internal_arguments_invalid")
    args = (
        {}
        if service == "read"
        else {
            "confirm": confirm,
            "operation_id": operation_id or str(uuid4()),
        }
    )
    async with connection(state, service, url, user=user, token_file=token_file) as client:
        result = await client.call_tool(_NAMES[service], args, raise_on_error=False)
    value = {"mode": "internal-simulated", "service": service, "real_business_called": False}
    if result.is_error:
        return {**value, "status": "failed", "error": _error(result)}
    return {**value, "status": "passed", "result": result.structured_content}


def _require(value, code):
    if not value:
        raise BusinessError("internal_check_" + code)


def _captures(state, service):
    path = state.capture_path(service)
    try:
        raw = _private_bytes(path, 8_000_000)
    except FileNotFoundError:
        return []
    rows = [json.loads(line) for line in raw.splitlines() if line]
    _require(all(type(row) is dict for row in rows), "capture_invalid")
    return rows


def _audit(state, service, correlation):
    path = state.journal_path(service)
    # 实验环境的状态加载器已检查所有路径与私有目录。此处使用
    # 只读 SQLite；检查过程绝不创建日志，也不改变锁状态。
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        database.row_factory = sqlite3.Row
        table, key = (
            ("query_events", "request_id")
            if service == "read"
            else ("reset_events", "operation_id")
        )
        return [
            dict(row)
            for row in database.execute(
                f"SELECT * FROM {table} WHERE {key}=?",
                (correlation,),
            )
        ]


async def check_services(state, endpoints):
    """检验已在运行的服务进程。所有上游仍保持为合成实现。"""
    before = {service: len(_captures(state, service)) for service in endpoints}
    checks = []
    accepted = []

    async def positive(service, user):
        async with connection(state, service, endpoints[service], user=user) as client:
            tools = await client.list_tools()
            _require([item.name for item in tools] == [_NAMES[service]], "tool_isolation")
            operation = str(uuid4())
            args = {} if service == "read" else {"confirm": True, "operation_id": operation}
            result = await client.call_tool(_NAMES[service], args)
            data = result.structured_content
            employee, account = ("E001", "LOGIN-A") if user == "user-a" else ("E002", "LOGIN-B")
            if service == "read":
                _require(data["data"]["employee_id"] == employee, "employee_mismatch")
                correlation = data["request_id"]
            else:
                _require(
                    data == {"operation_id": operation, "status": "upstream_acknowledged"},
                    "ack_mismatch",
                )
                correlation = operation
                replay = await client.call_tool(_NAMES[service], args, raise_on_error=False)
                _require(replay.is_error and _error(replay) == "reset_already_recorded", "replay")
                missing = await client.call_tool(
                    _NAMES[service],
                    {"confirm": False, "operation_id": str(uuid4())},
                    raise_on_error=False,
                )
                _require(
                    missing.is_error and _error(missing) == "confirmation_required", "confirmation"
                )
            injected = await client.call_tool(
                _NAMES[service],
                {**args, "employee_no": "E999"},
                raise_on_error=False,
            )
            _require(
                injected.is_error and _error(injected) == "invalid_arguments", "target_injection"
            )
            accepted.append((service, user, employee, account, correlation))
            return client.protocol_version

    protocols = await asyncio.gather(
        *(positive(service, user) for service in endpoints for user in ("user-a", "user-b"))
    )
    checks.append("A/B分别使用本人档案号与账号；两服务工具隔离")
    checks.append("并发调用、重置同操作拒绝重放、缺确认与目标注入前置拒绝")

    for service in endpoints:
        for user in ("disabled", "unmapped"):
            async with connection(state, service, endpoints[service], user=user) as client:
                args = {} if service == "read" else {"confirm": True, "operation_id": str(uuid4())}
                result = await client.call_tool(_NAMES[service], args, raise_on_error=False)
                _require(
                    result.is_error and _error(result) == "identity_not_enabled", "denied_subject"
                )
    checks.append("停用和未映射主体均在业务调用前拒绝")

    # 读资源的令牌绝不能通过重置资源的认证。
    temporary = state.root / "tokens" / (str(uuid4()) + ".jwt")
    issue_token(state.root, user="user-a", service="read", output=temporary)
    try:
        token = _private_bytes(temporary, 16_384).decode().strip()
        async with httpx2.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=5,
        ) as raw:
            response = await raw.post(
                endpoints["reset"],
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        _require(response.status_code in {401, 403}, "resource_token_crossed")
    finally:
        temporary.unlink(missing_ok=True)
    checks.append("查询令牌不能认证到重置入口")

    request_counts = {}
    for service in endpoints:
        rows = _captures(state, service)[before[service] :]
        _require(len(rows) == 2, "request_count")
        cfg = state.t100_config(service)
        _require(
            sorted((row["enterprise_id"], row["employee_id"]) for row in rows)
            == [(cfg.enterprise_id, "E001"), (cfg.enterprise_id, "E002")],
            "captured_employee",
        )
        if service == "reset":
            _require(
                sorted((row["employee_id"], row["account_id"]) for row in rows)
                == [("E001", "LOGIN-A"), ("E002", "LOGIN-B")],
                "captured_account",
            )
        request_counts[service] = len(rows)

    for service, user, employee, account, correlation in accepted:
        rows = _audit(state, service, correlation)
        events = {row["event"] for row in rows}
        _require(
            events
            == (
                {"query_started", "query_succeeded"}
                if service == "read"
                else {"reset_started", "reset_dispatched", "reset_succeeded"}
            ),
            "audit_events",
        )
        profile = state.identity_profile(service)
        _require(
            all(
                (
                    row["issuer"],
                    row["subject"],
                    row["client_id"],
                    row["enterprise_id"],
                    row["employee_id"],
                    row["tool_name"],
                )
                == (
                    profile.issuer,
                    user,
                    state.client_id(service),
                    state.t100_config(service).enterprise_id,
                    employee,
                    _NAMES[service],
                )
                for row in rows
            ),
            "audit_identity",
        )
        if service == "reset":
            _require(all(row["account_id"] == account for row in rows), "audit_account")
    checks.append("捕获报文与独立SQLite审计逐请求对应，拒绝场景零额外上游请求")

    return {
        "status": "passed",
        "mode": "internal-simulated",
        "checks": checks,
        "protocols": sorted(set(protocols)),
        "upstream_requests": request_counts,
        "audited_successful_calls": len(accepted),
        "reset_result": "upstream_acknowledged",
        "real_business_called": False,
        "services_left_running": True,
    }
