# Historical regression support relocated from scripts; not a supported command.
"""私有的已批准员工资料、模拟身份与只读上游。

本开发入口没有真实上游传输，也没有密码重置路径。
"""

import argparse
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx2
from employee_case_support import (
    MODE,
    init_case,
    issue_case_token,
    load_case,
)
from internal_client import _audit, _captures, _error, _require, call_once, connection
from internal_test import create_lab_app, serve, service_status
from internal_upstream import SyntheticUpstream

from biz_t100.models import BusinessError
from biz_t100.reset_server import RESET_TOOL_NAME
from biz_t100.server import TOOL_NAME

DEFAULT_STATE = Path(__file__).resolve().parents[2] / ".runtime" / "approved-read-case"
USER = "simulated-approved-user"


def create_case_app(state, service="read", *, scenario="success", instance_id=None):
    state.identity_profile(service)  # 在构造任何组件之前先拒绝 reset。
    capture = SyntheticUpstream(
        "read",
        enterprise_id=state.t100_config("read").enterprise_id,
        credential_sha256=state.credential_digest("read"),
        request_log=state.capture_path("read"),
        scenario=scenario,
        read_fixture=(state.employee_no, state.display_name),
    )
    return create_lab_app(
        state,
        "read",
        scenario=scenario,
        instance_id=instance_id,
        state_loader=load_case,
        capture=capture,
    )


@asynccontextmanager
async def case_connection(state, url, *, user=USER):
    output = state.root / "tokens" / (str(uuid4()) + ".jwt")
    try:
        issue_case_token(state.root, user=user, service="read", output=output)
        async with connection(state, "read", url, token_file=output) as client:
            yield client
    finally:
        output.unlink(missing_ok=True)


async def check_case(state, url):
    before = len(_captures(state, "read"))
    async with case_connection(state, url) as client:
        names = [tool.name for tool in await client.list_tools()]
        _require(names == [TOOL_NAME], "case_tools")
        result = await client.call_tool(TOOL_NAME, {})
        data = result.structured_content
        _require(
            data["data"]["employee_id"] == state.employee_no
            and data["data"]["display_name"] == state.display_name,
            "case_result",
        )
        correlation = data["request_id"]
        protocol = client.protocol_version
        injected = await client.call_tool(
            TOOL_NAME, {"employee_no": "OTHER-EMPLOYEE"}, raise_on_error=False
        )
        _require(injected.is_error and _error(injected) == "invalid_arguments", "case_injection")
        reset = await client.call_tool(
            RESET_TOOL_NAME,
            {"confirm": True, "operation_id": str(uuid4())},
            raise_on_error=False,
        )
        _require(reset.is_error, "case_reset_not_exposed")
    async with case_connection(state, url, user="unmapped") as client:
        rejected = await client.call_tool(TOOL_NAME, {}, raise_on_error=False)
        _require(rejected.is_error and _error(rejected) == "identity_not_enabled", "case_unmapped")
    async with httpx2.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        missing = await client.post(
            url,
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    _require(missing.status_code in {401, 403}, "case_missing_token")
    captures = _captures(state, "read")[before:]
    _require(len(captures) == 1, "case_request_count")
    enterprise = state.t100_config("read").enterprise_id
    _require(
        (captures[0]["enterprise_id"], captures[0]["employee_id"], captures[0]["account_id"])
        == (enterprise, state.employee_no, None),
        "case_captured_target",
    )
    events = _audit(state, "read", correlation)
    _require(
        len(events) == 2
        and {event["event"] for event in events} == {"query_started", "query_succeeded"},
        "case_audit_events",
    )
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
                state.issuer,
                USER,
                state.client_id("read"),
                enterprise,
                state.employee_no,
                TOOL_NAME,
            )
            for row in events
        ),
        "case_audit_identity",
    )
    return {
        "status": "passed",
        "mode": MODE,
        "upstream_requests": 1,
        "denied_extra_requests": 0,
        "employee_exact_match": True,
        "audit_events": len(events),
        "protocol": protocol,
        "checks": [
            "本人查询",
            "工号参数拒绝",
            "重置工具不暴露",
            "未映射拒绝",
            "缺令牌拒绝",
            "精确目标及审计",
        ],
        "identity": "locally_signed_simulated_subject",
        "upstream": "mock_only",
        "real_identity_verified": False,
        "real_business_called": False,
        "services_left_running": True,
    }


async def _client_command(state, args):
    status = await service_status(state, "read")
    if args.command == "status":
        return status
    if status["status"] != "ready":
        raise BusinessError("internal_case_service_not_ready")
    if args.command == "check":
        return await check_case(state, status["url"])
    output = None
    try:
        token_file = args.token_file
        if token_file is None:
            output = state.root / "tokens" / (str(uuid4()) + ".jwt")
            issue_case_token(state.root, user=args.user or USER, service="read", output=output)
            token_file = output
        result = await call_once(
            state,
            "read",
            status["url"],
            user=USER,
            token_file=token_file,
            confirm=False,
            operation_id=None,
        )
        return {
            **result,
            "mode": MODE,
            "identity": "locally_signed_simulated_subject",
            "upstream": "mock_only",
            "real_identity_verified": False,
        }
    finally:
        if output is not None:
            output.unlink(missing_ok=True)


def parser():
    result = argparse.ArgumentParser(description="Approved employee case; mock upstream, read only")
    commands = result.add_subparsers(dest="command", required=True)
    for command in ("init", "serve", "status", "call", "check", "token"):
        sub = commands.add_parser(command)
        sub.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
        if command == "init":
            sub.add_argument("--input-file", type=Path, required=True)
        elif command == "serve":
            sub.add_argument("--port", type=int, default=8768)
            sub.add_argument(
                "--scenario",
                choices=("success", "mismatch", "timeout", "unauthenticated"),
                default="success",
            )
        elif command == "token":
            sub.add_argument("--user", choices=(USER, "unmapped"), default=USER)
            sub.add_argument("--output", type=Path, required=True)
            sub.add_argument("--ttl", type=int, default=300)
        elif command == "call":
            who = sub.add_mutually_exclusive_group()
            who.add_argument("--user", choices=(USER, "unmapped"))
            who.add_argument("--token-file", type=Path)
    return result


def run(argv=None):
    args = parser().parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        if args.command == "init":
            if args.state_dir == DEFAULT_STATE and not DEFAULT_STATE.parent.exists():
                DEFAULT_STATE.parent.mkdir(mode=0o700)
            value = init_case(args.state_dir, args.input_file)
        elif args.command == "token":
            value = issue_case_token(
                args.state_dir,
                user=args.user,
                service="read",
                output=args.output,
                ttl_seconds=args.ttl,
            )
        else:
            state = load_case(args.state_dir)
            if args.command == "serve":
                asyncio.run(
                    serve(
                        state,
                        "read",
                        port=args.port,
                        scenario=args.scenario,
                        app_factory=create_case_app,
                    )
                )
                return 0
            value = asyncio.run(_client_command(state, args))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, BusinessError) and exc.code.startswith("internal_")
            else "internal_case_failed"
        )
        print(json.dumps({"status": "failed", "error": code, "mode": MODE}))
        return 1
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 1 if value.get("status") == "failed" else 0
