# Historical regression support relocated from scripts; not a supported command.
"""仅供运维方使用的真实 T100 读取装配，配合临时签名身份。

只有身份是临时的。正常 HTTP 客户端由产品运行时持有；本入口没有模拟传输、
重置工具、旧源码导入，也没有业务配置默认值。私有配置单独准备。
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import anyio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from internal_client import _error, connection
from internal_support import _absolute, _directory, _metadata, _read_private, _write_new
from internal_test import serve, service_status
from pydantic import SecretStr, TypeAdapter
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from biz_t100.access import ServiceAccountPolicy
from biz_t100.errors import ERROR_MESSAGES, error_message
from biz_t100.journal import SQLiteQueryJournal
from biz_t100.models import BusinessError, Identifier
from biz_t100.runtime import (
    RuntimeConfig,
    check_configuration,
    create_runtime_app,
    load_runtime_config,
)
from biz_t100.server import SCOPE, TOOL_NAME

MODE = "live-t100-read-with-temporary-identity"
USER = "simulated-live-user"
DEFAULT_STATE = Path(__file__).resolve().parents[2] / ".runtime" / "live-read"


def _read_only(service):
    if service != "read":
        raise BusinessError("live_read_service_invalid")


@dataclass(frozen=True)
class LiveState:
    root: Path
    config: RuntimeConfig = field(repr=False)
    public_key: str = field(repr=False)
    mode: str = MODE

    def policy_path(self, service):
        _read_only(service)
        return self.config.policy_file

    def credential_path(self, service):
        _read_only(service)
        return self.config.credential_file

    def journal_path(self, service):
        _read_only(service)
        return self.config.journal_path

    def identity_profile(self, service):
        _read_only(service)
        return self.config.identity

    def client_id(self, service):
        return next(iter(self.identity_profile(service).allowed_clients))

    def t100_config(self, service):
        _read_only(service)
        return self.config.t100

    @property
    def issuer(self):
        return self.config.identity.issuer


def load_live(root: Path) -> LiveState:
    """检查固定的私有路径、策略与公钥；绝不读取秘密字节内容。"""
    try:
        root = _absolute(root)
        for directory in (root, root / "tokens", root / "read", root / "read" / "audit"):
            os.close(_directory(directory))
        _metadata(root / "runtime.json", 1_000_000)
        config = load_runtime_config(root / "runtime.json")
        if (
            config.policy_file != root / "read" / "access.json"
            or config.credential_file != root / "read" / "service.key"
            or config.journal_path != root / "read" / "audit" / "query.sqlite3"
            or len(config.identity.allowed_clients) != 1
        ):
            raise ValueError()
        policy = ServiceAccountPolicy.model_validate_json(
            _read_private(config.policy_file, 1_000_000)
        )
        if len(policy.principals) > 1 or any(
            row.issuer != config.identity.issuer
            or row.subject != USER
            or set(row.allowed_clients) != config.identity.allowed_clients
            or row.allow_password_reset
            for row in policy.principals
        ):
            raise ValueError()
        _metadata(config.credential_file, 16_384)
        for suffix in ("", "-journal", "-wal", "-shm"):
            _metadata(
                config.journal_path.with_name(config.journal_path.name + suffix),
                1_000_000_000,
                optional=True,
            )
        public = _read_private(root / "issuer-public.pem", 16_384)
        key = serialization.load_pem_public_key(public)
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError()
        canonical = key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        if canonical != public:
            raise ValueError()
        return LiveState(root, config, public.decode("ascii"))
    except Exception:
        raise BusinessError("live_read_state_invalid") from None


def issue_live_token(root, *, user=USER, output, ttl_seconds=300):
    """只为选定的读资源签名；令牌绝不包含员工号。"""
    try:
        if user not in {USER, "unmapped"} or type(ttl_seconds) is not int:
            raise ValueError()
        if not 1 <= ttl_seconds <= 3600:
            raise ValueError()
        state = load_live(root)
        output = _absolute(output)
        if output.parent != state.root / "tokens":
            raise ValueError()
        private = _read_private(state.root / "issuer-private.pem", 16_384)
        key = serialization.load_pem_private_key(private, password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError()
        public = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode("ascii")
        )
        if public != state.public_key:
            raise ValueError()
        pair = RSAKeyPair(private_key=SecretStr(private.decode("ascii")), public_key=public)
        token = pair.create_token(
            subject=user,
            issuer=state.issuer,
            audience=state.config.identity.audience,
            scopes=[SCOPE],
            expires_in_seconds=ttl_seconds,
            additional_claims={"azp": state.client_id("read")},
        )
        _write_new(output, (token + "\n").encode("ascii"))
        return {"status": "issued", "mode": MODE, "output": str(output), "ttl_seconds": ttl_seconds}
    except Exception:
        raise BusinessError("live_read_token_invalid") from None


def create_live_app(state, service="read", *, scenario="live", instance_id=None):
    """复用产品运行时及其默认真实传输，绝不注入 I/O。"""
    _read_only(service)
    if scenario != "live":
        raise BusinessError("live_read_scenario_invalid")
    profile = state.config.identity
    auth = JWTVerifier(
        public_key=state.public_key,
        issuer=profile.issuer,
        audience=profile.audience,
        required_scopes=[SCOPE],
    )
    native = create_runtime_app(state.config, auth=auth)
    journal = SQLiteQueryJournal(
        state.config.journal_path, enterprise_id=state.config.t100.enterprise_id
    )
    snapshot = state.config.model_dump(mode="json")
    instance_id = instance_id or str(uuid4())
    accepting = False

    async def ready():
        if not accepting:
            return False
        try:
            current = await anyio.to_thread.run_sync(load_live, state.root)
            if (
                current.public_key != state.public_key
                or current.config.model_dump(mode="json") != snapshot
            ):
                return False
            await check_configuration(state.config)
            await journal.check_ready()
        except Exception:
            return False
        return accepting

    @asynccontextmanager
    async def lifespan(app):
        nonlocal accepting
        current = await anyio.to_thread.run_sync(load_live, state.root)
        if (
            current.public_key != state.public_key
            or current.config.model_dump(mode="json") != snapshot
        ):
            raise BusinessError("live_read_state_changed")
        async with native.router.lifespan_context(native):
            accepting = True
            try:
                yield
            finally:
                accepting = False

    async def health(request):
        live = request.url.path.endswith("/live")
        available = accepting if live else await ready()
        return JSONResponse(
            {
                "status": ("live" if live else "ready") if available else "not_ready",
                "mode": MODE,
                "service": "read",
                "instance_id": instance_id,
                "scope": "local_configuration_only",
                "real_identity_verified": False,
            },
            status_code=200 if available else 503,
        )

    async def admitted(scope, receive, send):
        if scope["type"] == "http" and not await ready():
            await JSONResponse({"error": "service_unavailable"}, status_code=503)(
                scope, receive, send
            )
            return
        await native(scope, receive, send)

    return Starlette(
        routes=[
            Route("/health/live", health),
            Route("/health/ready", health),
            Mount("/", admitted),
        ],
        lifespan=lifespan,
    )


def _request_id(result):
    if not result.is_error and isinstance(result.structured_content, dict):
        candidates = [result.structured_content.get("request_id")]
    else:
        candidates = []
        for item in result.content:
            try:
                value = json.loads(getattr(item, "text", ""))
                if isinstance(value, dict):
                    candidates.append(value.get("request_id"))
            except (TypeError, ValueError):
                continue
    for value in candidates:
        try:
            if isinstance(value, str) and str(UUID(value)) == value:
                return value
        except ValueError:
            pass
    return None


def _attempted(state, request_id):
    """只有相互关联的终态证据才能证明本次调用是否已发起。"""
    if request_id is None:
        return None
    try:
        _metadata(state.config.journal_path, 1_000_000_000)
        with sqlite3.connect(state.config.journal_path.as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT upstream_attempted FROM query_events WHERE request_id=? "
                "AND event<>'query_started'",
                (request_id,),
            ).fetchall()
        return bool(rows[0][0]) if len(rows) == 1 else None
    except Exception:
        return None


def _audit_employee(state, request_id):
    if request_id is None:
        return None
    try:
        _metadata(state.config.journal_path, 1_000_000_000)
        with sqlite3.connect(state.config.journal_path.as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT DISTINCT employee_id FROM query_events WHERE request_id=? "
                "AND employee_id IS NOT NULL",
                (request_id,),
            ).fetchall()
        return rows[0][0] if len(rows) == 1 else None
    except Exception:
        return None


def _message(code):
    messages = {
        "live_read_service_invalid": "当前入口只支持员工资料查询。",
        "live_read_state_invalid": "私有运行配置、映射或验签公钥无效，请检查配置文件和权限。",
        "live_read_token_invalid": "临时令牌无法签发或加载，请检查签名材料、有效期和令牌文件路径。",
        "live_read_scenario_invalid": "真实查询入口不接受模拟上游场景。",
        "live_read_state_changed": "运行配置或验签公钥已变更，需要重新启动服务。",
        "live_read_service_not_ready": "常驻查询服务尚未就绪，请先查看status或启动服务。",
        "live_read_failed": "本地查询入口执行失败，请检查运行状态及关联审计。",
    }
    return messages.get(code, error_message(code))


def _employee_argument(value):
    try:
        return TypeAdapter(Identifier).validate_python(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "工号须为1至128个字符，以字母或数字开头，且仅包含字母、数字、_.:@-。"
        ) from None


async def call_live(state, url, *, user=USER, token_file=None):
    temporary = None
    try:
        if token_file is None:
            temporary = state.root / "tokens" / (str(uuid4()) + ".jwt")
            issue_live_token(state.root, user=user, output=temporary)
            token_file = temporary
        else:
            token_file = _absolute(token_file)
            if token_file.parent != state.root / "tokens":
                raise BusinessError("live_read_token_invalid")
            _metadata(token_file, 16_384)
        async with connection(state, "read", url, token_file=token_file) as client:
            result = await client.call_tool(TOOL_NAME, {}, raise_on_error=False)
            protocol = client.protocol_version
        request_id = _request_id(result)
        attempted = await anyio.to_thread.run_sync(_attempted, state, request_id)
        value = {
            "mode": MODE,
            "service": "read",
            "test_mode": "mapped-caller",
            "mcp_transport": "streamable-http",
            "requested_employee_no": await anyio.to_thread.run_sync(
                _audit_employee, state, request_id
            ),
            "identity": "locally_signed_temporary_subject",
            "real_identity_verified": False,
            "upstream": "real_t100",
            "upstream_attempted": attempted,
            "real_business_called": True if not result.is_error else attempted,
            "remote_response_validated": not result.is_error,
            "request_id": request_id,
            "protocol": protocol,
        }
        if result.is_error:
            code = _error(result)
            return {**value, "status": "failed", "error": code, "message": _message(code)}
        return {**value, "status": "passed", "result": result.structured_content}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parser():
    result = argparse.ArgumentParser(description="Live T100 read; temporary identity only")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("serve", "status", "call", "token"):
        sub = commands.add_parser(name)
        sub.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
        if name == "serve":
            sub.add_argument("--port", type=int, default=8769)
        if name == "token":
            sub.add_argument("--output", type=Path, required=True)
            sub.add_argument("--ttl", type=int, default=300)
            sub.add_argument("--user", choices=(USER, "unmapped"), default=USER)
        if name == "call":
            who = sub.add_mutually_exclusive_group()
            who.add_argument("--user", choices=(USER, "unmapped"), default=USER)
            who.add_argument("--token-file", type=Path)
            who.add_argument(
                "--employee-no",
                type=_employee_argument,
                help="本地只读测试工号；创建本次独立映射，不修改默认映射",
            )
    return result


async def _client_command(state, args):
    if args.command == "call" and getattr(args, "employee_no", None) is not None:
        from live_employee_call import call_employee

        return await call_employee(state, args.employee_no)
    status = await service_status(state, "read")
    if args.command == "status":
        return status
    if status["status"] != "ready":
        raise BusinessError("live_read_service_not_ready")
    return await call_live(state, status["url"], user=args.user, token_file=args.token_file)


def run(argv=None):
    args = parser().parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        if args.command == "token":
            value = issue_live_token(
                args.state_dir, user=args.user, output=args.output, ttl_seconds=args.ttl
            )
        else:
            state = load_live(args.state_dir)
            if args.command == "serve":
                asyncio.run(
                    serve(
                        state, "read", port=args.port, scenario="live", app_factory=create_live_app
                    )
                )
                return 0
            value = asyncio.run(_client_command(state, args))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, BusinessError)
            and (exc.code.startswith("live_read_") or exc.code in ERROR_MESSAGES)
            else "live_read_failed"
        )
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": code,
                    "message": _message(code),
                    "mode": MODE,
                    "real_business_called": None,
                }
            )
        )
        return 1
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 1 if value.get("status") == "failed" else 0
