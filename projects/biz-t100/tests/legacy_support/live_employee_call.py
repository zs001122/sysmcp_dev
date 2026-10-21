# Historical regression support relocated from scripts; not a supported command.
"""私有的、由运维方选定的读取用例，走原生 MCP 与真实 T100 HTTP。

员工号只是本地编写策略的输入，绝不是工具参数。每次调用都拥有独立的
临时主体、策略与持久化日志；不复制共享监听方的策略、服务凭据和签名密钥。
"""

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import httpx2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from internal_support import _directory, _metadata, _mkdir, _read_private, _write_new
from pydantic import SecretStr, TypeAdapter

from biz_t100.access import ServiceAccountPolicy
from biz_t100.errors import error_message
from biz_t100.models import BusinessError, Identifier, QueryResult
from biz_t100.runtime import RuntimeConfig, create_runtime_app
from biz_t100.server import ERROR_CODES, SCOPE, TOOL_NAME

_MODE = "live-t100-read-with-temporary-identity"
_PROTOCOL = "2025-11-25"
_ERRORS = ERROR_CODES | {"invalid_arguments", "tool_not_available"}


@dataclass(frozen=True)
class _Case:
    path: Path
    config: RuntimeConfig
    subject: str


def _base_permission(state) -> str:
    """在签名或读取任何管理员密钥字节之前先检查权限。"""
    try:
        raw = _read_private(state.config.policy_file, 1_000_000)
        policy = ServiceAccountPolicy.model_validate_json(raw)
    except Exception:
        raise BusinessError("policy_unavailable") from None
    if len(policy.principals) != 1 or not policy.principals[0].enabled:
        raise BusinessError("identity_not_enabled")
    row = policy.principals[0]
    if (
        row.issuer != state.config.identity.issuer
        or set(row.allowed_clients) != state.config.identity.allowed_clients
        or state.client_id("read") not in row.allowed_clients
        or not row.allow_employee_read
        or row.allow_password_reset
    ):
        raise BusinessError("permission_denied")
    return hashlib.sha256(raw).hexdigest()


def _prepare(state, employee_no: str) -> _Case:
    cases = state.root / "operator-cases"
    _mkdir(cases)
    os.close(_directory(cases))
    identifier = str(uuid4())
    case = cases / identifier
    if not _mkdir(case):
        raise BusinessError("live_read_failed")
    audit = case / "audit"
    if not _mkdir(audit):
        raise BusinessError("live_read_failed")
    subject = "simulated-cli-" + identifier
    policy = ServiceAccountPolicy.model_validate(
        {
            "auth_mode": "service-account",
            "revision": "operator-case-" + identifier,
            "principals": [
                {
                    "issuer": state.config.identity.issuer,
                    "subject": subject,
                    "employee_id": employee_no,
                    "allowed_clients": [state.client_id("read")],
                    "enabled": True,
                    "allow_employee_read": True,
                    "allow_password_reset": False,
                }
            ],
        }
    )
    policy_path = case / "access.json"
    _write_new(policy_path, policy.model_dump_json().encode())
    raw = json.loads(state.config.model_dump_json())
    raw.update(policy_file=str(policy_path), journal_path=str(audit / "query.sqlite3"))
    config = RuntimeConfig.model_validate_json(json.dumps(raw))
    _write_new(
        case / "case.json",
        json.dumps(
            {
                "mode": _MODE,
                "test_mode": "operator-selected-employee",
                "mcp_transport": "in-process-asgi",
                "issuer": state.config.identity.issuer,
                "subject": subject,
                "enterprise_id": state.config.t100.enterprise_id,
                "employee_no": employee_no,
                "tool_name": TOOL_NAME,
                "real_identity_verified": False,
                "credential_copied": False,
            }
        ).encode(),
    )
    return _Case(case, config, subject)


def _sign(state, subject: str) -> str:
    try:
        private = _read_private(state.root / "issuer-private.pem", 16_384)
        key = serialization.load_pem_private_key(private, password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError()
        public = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        if public.decode("ascii") != state.public_key:
            raise ValueError()
        pair = RSAKeyPair(
            private_key=SecretStr(private.decode("ascii")), public_key=state.public_key
        )
        return pair.create_token(
            subject=subject,
            issuer=state.config.identity.issuer,
            audience=state.config.identity.audience,
            scopes=[SCOPE],
            expires_in_seconds=300,
            additional_claims={"azp": state.client_id("read")},
        )
    except Exception:
        raise BusinessError("live_read_token_invalid") from None


def _request_id(result: dict) -> str | None:
    if result.get("isError"):
        candidates = []
        for item in result.get("content", []):
            try:
                content = json.loads(item.get("text", ""))
                if isinstance(content, dict):
                    candidates.append(content.get("request_id"))
            except (ValueError, TypeError, AttributeError):
                pass
    else:
        content = result.get("structuredContent")
        candidates = [content.get("request_id")] if isinstance(content, dict) else []
    for candidate in candidates:
        try:
            if isinstance(candidate, str) and str(UUID(candidate)) == candidate:
                return candidate
        except ValueError:
            pass
    return None


def _error(result: dict) -> str:
    for item in result.get("content", []):
        if not isinstance(item, dict):
            continue
        text = item.get("text", "")
        if isinstance(text, str) and text in _ERRORS:
            return text
        try:
            value = json.loads(text)
            if isinstance(value, dict) and value.get("error") in _ERRORS:
                return value["error"]
        except (ValueError, TypeError):
            pass
    return "live_read_failed"


def _attempted(case: _Case, request_id: str | None) -> bool | None:
    if request_id is None:
        return None
    try:
        _metadata(case.config.journal_path, 1_000_000_000)
        with sqlite3.connect(case.config.journal_path.as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT upstream_attempted FROM query_events WHERE request_id=? "
                "AND event<>'query_started'",
                (request_id,),
            ).fetchall()
        return bool(rows[0][0]) if len(rows) == 1 else None
    except Exception:
        return None


async def _rpc(client, method: str, params: dict, identifier: int | None):
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if identifier is not None:
        body["id"] = identifier
    response = await client.post("/mcp", json=body)
    if response.status_code in {401, 403}:
        raise BusinessError("unauthenticated")
    if identifier is None and response.status_code == 202:
        return None
    if response.status_code != 200:
        raise BusinessError("live_read_failed")
    payload = response.json()
    if (
        not isinstance(payload, dict)
        or payload.get("jsonrpc") != "2.0"
        or payload.get("id") != identifier
        or not isinstance(payload.get("result"), dict)
    ):
        raise BusinessError("live_read_failed")
    return payload["result"]


async def call_employee(state, employee_no: str) -> dict:
    """执行一次隔离的真实读取；不需要监听器，也不修改共享策略。"""
    value = {
        "mode": _MODE,
        "test_mode": "operator-selected-employee",
        "mcp_transport": "in-process-asgi",
        "service": "read",
        "identity": "locally_signed_temporary_subject",
        "real_identity_verified": False,
        "upstream": "real_t100",
        "upstream_attempted": False,
        "real_business_called": False,
        "remote_response_validated": False,
        "request_id": None,
        "protocol": None,
    }
    case = None
    try:
        try:
            selected = TypeAdapter(Identifier).validate_python(employee_no, strict=True)
        except Exception:
            raise BusinessError("invalid_arguments") from None
        value["requested_employee_no"] = selected
        await anyio.to_thread.run_sync(_base_permission, state)
        case = await anyio.to_thread.run_sync(_prepare, state, selected)
        value.update(case_path=str(case.path), audit_path=str(case.config.journal_path))
        token = await anyio.to_thread.run_sync(_sign, state, case.subject)
        profile = case.config.identity
        auth = JWTVerifier(
            public_key=state.public_key,
            issuer=profile.issuer,
            audience=profile.audience,
            required_scopes=[SCOPE],
        )
        app = create_runtime_app(case.config, auth=auth)
        failure = None
        async with app.router.lifespan_context(app):
            try:
                async with httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    base_url="http://127.0.0.1",
                    trust_env=False,
                    follow_redirects=False,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept": "application/json, text/event-stream",
                    },
                    timeout=15,
                ) as client:
                    initialized = await _rpc(
                        client,
                        "initialize",
                        {
                            "protocolVersion": _PROTOCOL,
                            "capabilities": {},
                            "clientInfo": {"name": "t100-private-operator-case", "version": "1"},
                        },
                        1,
                    )
                    if initialized.get("protocolVersion") != _PROTOCOL:
                        raise BusinessError("live_read_failed")
                    value["protocol"] = _PROTOCOL
                    client.headers["MCP-Protocol-Version"] = _PROTOCOL
                    await _rpc(client, "notifications/initialized", {}, None)
                    tools = await _rpc(client, "tools/list", {}, 2)
                    if [item.get("name") for item in tools.get("tools", [])] != [TOOL_NAME]:
                        raise BusinessError("live_read_failed")
                    value.update(upstream_attempted=None, real_business_called=None)
                    result = await _rpc(
                        client, "tools/call", {"name": TOOL_NAME, "arguments": {}}, 3
                    )
            except Exception as exc:
                # 不能让客户端错误被改写成宿主生命周期（lifespan）失败。
                failure = (
                    exc if isinstance(exc, BusinessError) else BusinessError("live_read_failed")
                )
        if failure is not None:
            raise failure
        request_id = _request_id(result)
        value["request_id"] = request_id
        attempted = await anyio.to_thread.run_sync(_attempted, case, request_id)
        value.update(upstream_attempted=attempted, real_business_called=attempted)
        if result.get("isError"):
            raise BusinessError(_error(result))
        parsed = QueryResult.model_validate(result.get("structuredContent"))
        if parsed.data.employee_id != selected:
            raise BusinessError("upstream_employee_mismatch")
        if request_id != parsed.request_id:
            raise BusinessError("upstream_contract_error")
        return {
            **value,
            "status": "passed",
            "real_business_called": True,
            "remote_response_validated": True,
            "result": parsed.model_dump(mode="json"),
        }
    except Exception as exc:
        code = exc.code if isinstance(exc, BusinessError) else "live_read_failed"
        return {**value, "status": "failed", "error": code, "message": error_message(code)}
