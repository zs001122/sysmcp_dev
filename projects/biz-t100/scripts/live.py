"""Two-entry test workflow: this entry calls real T100 through authenticated FastMCP."""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
import mcp_types
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from fastmcp import Client as MCPClient
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from pydantic import SecretStr

from biz_t100.access import _private_bytes
from biz_t100.errors import ERROR_MESSAGES, error_message
from biz_t100.function_audit import (
    FunctionAudit,
    LegacyEmployeeUpdateRecord,
    LegacyStatusRetry,
    ReviewedStatusContinuation,
)
from biz_t100.functions import (
    SCOPES,
    TOOLS,
    FunctionPermission,
    FunctionService,
    GuardedFunctionAccess,
    create_function_app,
    request_digest,
    request_targets,
)
from biz_t100.interface_client import T100InterfaceClient
from biz_t100.interfaces import REQUEST_MODELS
from biz_t100.models import BusinessError
from biz_t100.runtime import load_runtime_config
from biz_t100.server import IdentityProfile

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT / ".runtime/live-read/runtime.json"
STATE = PROJECT / ".runtime/functions"
LEGACY_AUDIT = PROJECT / ".runtime/live-write/audit/account-write.sqlite3"
ISSUER = "https://local-t100-maintenance.example.invalid"
AUDIENCE = "t100-maintenance-test"
CLIENT = "local-operator"
SUBJECT = "temporary-local-operator"
PROTOCOL = "2025-11-25"
REMOTE_PROTOCOLS = (PROTOCOL, "2025-06-18")
CLIENT_VERSION = version("sysmcp-biz-t100")


_OPERATION_LABELS = {
    "employee_query": "员工资料查询",
    "employee_create": "员工资料新建",
    "employee_update": "员工资料修改",
    "account_maintain": "用户账号维护",
}
_SUMMARY_FIELDS = ("操作说明", "状态说明", "操作对象", "失败原因", "处理建议", "匹配员工数")
_EMPLOYEE_FIELD_LABELS = {
    "employee_no": "员工工号",
    "last_name": "姓",
    "middle_name": "中间名",
    "name": "名字",
    "full_name": "员工全名",
    "department_no": "部门编号",
    "job_title": "职称",
    "bank_account": "银行行号或邮局局号",
    "account_no": "员工资料账号",
    "email": "电子邮箱",
    "telephone": "电话",
    "mobile_phone": "手机号码",
    "remark": "说明",
    "status": "员工资料状态",
    "modify_no": "资料修改者",
    "create_date": "资料创建时间",
    "modify_date": "资料修改时间",
}
_ERROR_ADVICE = {
    "unauthenticated": "检查客户端签名资料、令牌有效期及目标服务的 issuer、audience 和客户端配置。",
    "mcp_call_failed": "核对服务地址、工具名称和服务端审计；远程调用失败后不要自动重试写操作。",
    "protocol_mismatch": "目标服务未按指定协议响应；分别验证直连和网关 Profile，不自动降级。",
    "tool_not_available": "先用 tools 查看该服务开放的工具；网关调用需使用实际暴露的工具名。",
    "write_permission_not_configured": (
        "私有运行许可尚未开启真实写入；检查 .runtime/functions/permissions.json 中的 "
        "writes_enabled 及动作、目标范围。--confirm 不能替代维护许可。"
    ),
    "confirmation_required": "确认本次写入目标和内容后添加 --confirm；仍需有效的私有写许可。",
    "reset_pending_review": "先核查历史未决写入及审计；不要通过换命令或新编号重复写入。",
    "reset_already_recorded": "该请求已有记录；请查看原结果，本次不会再次发送。",
    "reset_outcome_unknown": "先核对原操作的最终执行结果；当前目标仍被占用，不自动重试。",
    "permission_denied": (
        "核对 .runtime/functions/permissions.json 中允许的动作、员工及账号范围。旧配置如仍有 "
        "request_digest，会额外限定整份报文；内部功能测试应使用目标范围许可。"
    ),
    "policy_unavailable": "检查私有许可、文件权限及企业和运行配置摘要是否一致。",
    "runtime_configuration_invalid": "检查指定的私有运行配置及文件权限。",
    "invalid_arguments": "核对命令参数或完整 parameter JSON；字段文件不能与快捷参数混用。",
    "empty_identifier": (
        "请填写实际员工工号和登录账号；若使用 $EMPLOYEE_NO、$ACCOUNT_ID，先在当前终端赋值。"
    ),
    "invalid_request": "核对所选接口的字段、类型及必填项。",
    "employee_not_found": "先核对真实员工工号；文档中的 E001 等标识是合成示例。",
    "employee_not_unique": "缩小查询条件并核对唯一工号，不能自动选择第一条记录写入。",
    "upstream_rejected": "核对业务码和审计，确认上游最终结果后再处理；不要自动重试。",
    "upstream_verification_failed": "接口可能已执行写入；请按请求编号核查回读差异，不自动重试。",
    "upstream_timeout": "核对审计中的派发事实及上游最终结果；程序没有自动重试。",
    "journal_unavailable": "先恢复并核对审计和结果记录；当前不能确认操作成功。",
}


def present_result(result, *, action=None, history=False):
    """增加固定中文摘要；英文协议字段和请求事实保持原样。"""
    value = {key: item for key, item in result.items() if key not in _SUMMARY_FIELDS}
    selected = action if action in _OPERATION_LABELS else result.get("action")
    label = _OPERATION_LABELS.get(selected, "T100 请求")
    value["操作说明"] = "查看本地操作记录（" + label + "）" if history else label
    if selected in _OPERATION_LABELS:
        value["操作对象"] = "T100 登录账号" if selected == "account_maintain" else "员工档案"
    status = value.get("status")
    operation_state = value.get("operation_state")
    code = value.get("error") or value.get("code")
    if operation_state == "unknown" or status == "unknown" or code == "reset_outcome_unknown":
        summary = "写入结果未知，需核查原操作；不能判定成功或重复写入"
    elif history and status != "failed":
        summary = {
            "started": "原操作已开始，尚无终态记录",
            "dispatched": "原操作记录为可能已派发，结果尚未确认",
            "succeeded": "原操作已记录成功；具体业务效果以原调用结果为准",
            "failed": "原操作已记录失败；请查看错误码及审计",
        }.get(operation_state, "已读取本地操作记录；请查看原操作状态")
    elif status == "passed" and selected == "employee_query":
        data = value.get("data")
        rows = data.get("employee") if type(data) is dict else None
        if type(rows) is list:
            value["匹配员工数"] = len(rows)
            more = data.get("has_next") is True
            if rows:
                summary = f"查询完成，本次返回 {len(rows)} 条员工资料"
            elif more:
                summary = "查询完成，本次未返回匹配员工"
            else:
                summary = "查询完成，未找到匹配员工"
            if more:
                summary += "（还有下一页）"
        else:
            summary = "查询请求已完成，请查看返回资料"
    elif status == "verified" and value.get("business_effect_verified") is True:
        summary = "资料写入成功，提交字段已回读核对一致"
    elif status in {"acknowledged", "upstream_acknowledged"}:
        summary = (
            "账号维护接口返回成功回执，账号状态、密码或权限效果尚未验证"
            if selected == "account_maintain"
            else "接口返回成功回执，业务效果尚未验证"
        )
    elif status == "blocked":
        summary = "操作被阻止，需先处理阻塞原因"
    elif status == "failed":
        if value.get("upstream_attempted") is False:
            summary = "请求未完成，未调用 T100"
        elif value.get("write_outcome") == "rejected" and code == "upstream_rejected":
            summary = "上游明确拒绝本次写入；已记录失败，不作为结果未知占用"
        elif value.get("write_upstream_attempted") is True:
            summary = "请求失败，已尝试业务写入，最终效果需核查"
        else:
            summary = "请求失败，请按请求编号核查"
    else:
        summary = "请求状态尚未确认，请查看英文状态及审计记录"
    value["状态说明"] = summary
    failed = status in {"failed", "blocked", "unknown"} or operation_state in {"failed", "unknown"}
    if failed and type(code) is str:
        value["失败原因"] = error_message(code)
        value["处理建议"] = _ERROR_ADVICE.get(code, "请结合请求编号、固定错误码和审计记录核查。")
        diagnostics = value.get("diagnostics")
        fields = diagnostics.get("invalid_fields") if type(diagnostics) is dict else None
        if (
            code == "upstream_rejected"
            and type(fields) is list
            and 0 < len(fields) <= len(_EMPLOYEE_FIELD_LABELS)
            and all(type(field) is str and field in _EMPLOYEE_FIELD_LABELS for field in fields)
        ):
            labels = "、".join(
                f"{_EMPLOYEE_FIELD_LABELS[field]}（{field}）" for field in sorted(set(fields))
            )
            value["失败原因"] = f"上游提示{labels}缺失或无效。"
            value["处理建议"] = (
                "按接口文档核对并提供有效字段，先核查本次写入最终结果；不要自动重试。"
            )
    elif operation_state == "unknown" or status == "unknown":
        value["处理建议"] = _ERROR_ADVICE["reset_pending_review"]
    elif selected == "employee_query" and value.get("匹配员工数") == 0:
        value["处理建议"] = "核对实际工号或调整检索条件；E001 等文档示例不代表真实员工。"
    elif status in {"acknowledged", "upstream_acknowledged"}:
        value["处理建议"] = (
            "通过账号资料或实际登录确认账号操作效果；员工查询的 status 不能验证账号启停或密码。"
            if selected == "account_maintain"
            else "根据原调用结果继续验证实际业务效果；成功回执不等于已生效。"
        )
    return {**{key: value[key] for key in _SUMMARY_FIELDS if key in value}, **value}


def directory(path):
    if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in path.parents):
        raise BusinessError("runtime_configuration_invalid")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        path.is_symlink()
        or not path.is_dir()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise BusinessError("runtime_configuration_invalid")


def save(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2).encode())
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def config_digest(config):
    return hashlib.sha256(config.t100.model_dump_json().encode()).hexdigest()


def initialize_permissions(config, folder):
    path = folder / "permissions.json"
    if not path.exists() and not path.is_symlink():
        save(
            path,
            {
                "enterprise_id": config.t100.enterprise_id,
                "upstream_config_digest": config_digest(config),
                "queries_enabled": True,
                "writes_enabled": False,
                "write_targets": [],
            },
        )
    return path


def permission_guard(config, path, action, request):
    try:
        raw = _private_bytes(path, 1_000_000)
        policy = json.loads(raw)
        if policy["enterprise_id"] != config.t100.enterprise_id or policy[
            "upstream_config_digest"
        ] != config_digest(config):
            raise ValueError()
        if action == "employee_query":
            if policy.get("queries_enabled") is not True:
                raise BusinessError("permission_denied")
        else:
            if policy.get("writes_enabled") is not True:
                raise BusinessError("write_permission_not_configured")
            targets = policy["write_targets"]
            if type(targets) is not list or any(type(row) is not dict for row in targets):
                raise ValueError()
            rows = request.parameter()["master" if action == "account_maintain" else "employee"]
            for row in rows:
                employee = row["gzxa003" if action == "account_maintain" else "employee_no"]
                if not any(
                    target.get("action") == action
                    and target.get("employee_no") == employee
                    and (
                        "request_digest" not in target
                        or target["request_digest"] == request_digest(action, request)
                    )
                    and (action != "account_maintain" or target.get("account_id") == row["gzxa001"])
                    for target in targets
                ):
                    raise BusinessError("permission_denied")
                if action == "account_maintain" and row["is_reproduce"] == "Y":
                    if not any(
                        target.get("action") == action
                        and row["reproduce_acct"] == target.get("employee_no")
                        for target in targets
                    ):
                        raise BusinessError("permission_denied")
        return hashlib.sha256(raw).hexdigest()
    except BusinessError:
        raise
    except Exception:
        raise BusinessError("policy_unavailable") from None


def selected_status_retry(config, path, action, request):
    """Load only an explicitly reviewed, exact employee-status request."""
    if action != "employee_update":
        return None
    try:
        policy = json.loads(_private_bytes(path, 1_000_000))
        value = policy.get("legacy_status_retry")
        if value is None:
            return None
        review = LegacyStatusRetry.model_validate(value)
        employees, _ = request_targets(action, request)
        if review.employee_id not in employees:
            return None
        if (
            review.enterprise_id != config.t100.enterprise_id
            or review.request_digest != request_digest(action, request)
        ):
            raise BusinessError("permission_denied")
        return review
    except BusinessError:
        raise
    except Exception:
        raise BusinessError("policy_unavailable") from None


def selected_status_continuation(config, path, action, request):
    """Only apply a recorded review to an explicitly approved status payload."""
    if action != "employee_update":
        return None
    try:
        policy = json.loads(_private_bytes(path, 1_000_000))
        value = policy.get("status_continuation")
        if value is None:
            return None
        review = ReviewedStatusContinuation.model_validate_json(json.dumps(value))
        employees, accounts = request_targets(action, request)
        if review.employee_id not in employees:
            return None
        rows = request.parameter()["employee"]
        if (
            policy.get("legacy_status_retry") is not None
            or review.enterprise_id != config.t100.enterprise_id
            or employees != (review.employee_id,)
            or accounts != ()
            or len(rows) != 1
            or rows[0].get("status") not in {"Y", "N"}
            or request_digest(action, request) not in review.request_digests
        ):
            raise BusinessError("permission_denied")
        return review
    except BusinessError:
        raise
    except Exception:
        raise BusinessError("policy_unavailable") from None


def selected_legacy_employee_updates(config, path, action, request):
    """Load exact old records already verified as employee updates, not account operations."""
    if action != "account_maintain":
        return ()
    try:
        policy = json.loads(_private_bytes(path, 1_000_000))
        values = policy.get("legacy_employee_updates", [])
        if type(values) is not list:
            raise ValueError()
        records = tuple(LegacyEmployeeUpdateRecord.model_validate(value) for value in values)
        employees, _ = request_targets(action, request)
        if any(
            record.enterprise_id != config.t100.enterprise_id or record.employee_id not in employees
            for record in records
        ):
            raise BusinessError("permission_denied")
        return records
    except BusinessError:
        raise
    except Exception:
        raise BusinessError("policy_unavailable") from None


def make_request(args):
    action = args.command.replace("-", "_")
    fields = (
        "employee_no",
        "employee_name",
        "department_no",
        "full_name",
        "status",
        "email",
        "mobile_phone",
        "account_id",
        "account_status",
        "password_reset",
        "is_reproduce",
        "reproduce_acct",
        "page_size",
        "page_no",
        "date_s",
        "date_e",
    )
    values = {key: getattr(args, key) for key in fields if getattr(args, key, None) is not None}
    identifiers = (
        ("employee_no", "account_id") if action == "account_maintain" else ("employee_no",)
    )
    if any(key in values and not values[key].strip() for key in identifiers):
        raise BusinessError("empty_identifier")
    if args.data_file:
        if values:
            raise BusinessError("invalid_arguments")
        # Files with real personal data belong in a private runtime folder.
        body = json.loads(_private_bytes(args.data_file.absolute(), 1_000_000))
    elif action == "employee_query":
        body = values
        body["use_has_next"] = "page_size" in values or "page_no" in values
    elif action in {"employee_create", "employee_update"}:
        body = {"employee": [values]}
    else:
        aliases = {"employee_no": "gzxa003", "account_id": "gzxa001", "account_status": "gzxastus"}
        body = {"master": [{aliases.get(key, key): value for key, value in values.items()}]}
    try:
        return action, REQUEST_MODELS[action].model_validate(body)
    except Exception:
        raise BusinessError("invalid_arguments") from None


def remote_headers(client_config, action=None):
    """只读取客户端签名资料；上游配置、服务 key 和业务许可留在服务端。"""
    try:
        value = json.loads(_private_bytes(client_config.absolute(), 100_000))
        allowed = {
            "identity", "signing_key_file", "scopes", "token_lifetime_seconds",
            "gateway_api_key_file",
            "endpoint", "allow_private_http",
        }
        if type(value) is not dict or set(value) - allowed:
            raise ValueError()
        if "allow_private_http" in value and type(value["allow_private_http"]) is not bool:
            raise ValueError()
        identity = value["identity"]
        if type(identity) is not dict or set(identity) != {
            "issuer", "subject", "audience", "client_id"
        }:
            raise ValueError()
        if any(
            type(item) is not str or not item.strip() or len(item) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in item)
            for item in identity.values()
        ):
            raise ValueError()
        scopes = value["scopes"]
        if (
            type(scopes) is not list or not scopes
            or any(type(scope) is not str or scope not in SCOPES.values() for scope in scopes)
            or len(set(scopes)) != len(scopes)
        ):
            raise ValueError()
        if action is not None and SCOPES[action] not in scopes:
            raise BusinessError("permission_denied")
        lifetime = value.get("token_lifetime_seconds", 120)
        if type(lifetime) is not int or not 1 <= lifetime <= 300:
            raise ValueError()
        path = Path(value["signing_key_file"])
        private = _private_bytes(path, 16_384)
        key = serialization.load_pem_private_key(private, password=None)
        if not isinstance(key, RSAPrivateKey) or key.key_size < 2048:
            raise ValueError()
        pair = RSAKeyPair(
            private_key=SecretStr(private.decode()),
            public_key=key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            ).decode(),
        )
        token = pair.create_token(
            subject=identity["subject"], issuer=identity["issuer"], audience=identity["audience"],
            scopes=scopes, additional_claims={"azp": identity["client_id"]},
            expires_in_seconds=lifetime,
        )
        headers = {"Authorization": "Bearer " + token}
        if "gateway_api_key_file" in value:
            gateway_key = (
                _private_bytes(Path(value["gateway_api_key_file"]), 16_384).decode().strip()
            )
            if not gateway_key or any(char.isspace() or ord(char) < 32 or ord(char) == 127
                                      for char in gateway_key):
                raise ValueError()
            headers["X-API-Key"] = gateway_key
        return headers
    except BusinessError:
        raise
    except Exception:
        raise BusinessError("runtime_configuration_invalid") from None


def remote_endpoint(endpoint, client_config):
    try:
        value = json.loads(_private_bytes(client_config.absolute(), 100_000))
        selected = endpoint if endpoint is not None else value.get("endpoint")
        private_http = value.get("allow_private_http", False)
        if type(selected) is not str or type(private_http) is not bool:
            raise ValueError()
        validate_endpoint(selected, allow_private_http=private_http)
        return selected
    except BusinessError:
        raise
    except Exception:
        raise BusinessError("runtime_configuration_invalid") from None


def validate_endpoint(endpoint, *, allow_private_http=False):
    try:
        url = urlsplit(endpoint)
        if (
            url.scheme not in {"http", "https"} or not url.hostname
            or url.username is not None or url.password is not None
            or url.query or url.fragment or not url.path
        ):
            raise ValueError()
        _ = url.port
        if url.scheme == "http":
            try:
                address = ipaddress.ip_address(url.hostname)
                local = address.is_loopback
                private = address.is_private and not address.is_unspecified
            except ValueError:
                local, private = url.hostname.lower() == "localhost", False
            if not local and not (allow_private_http and private):
                raise ValueError()
    except Exception:
        raise BusinessError("invalid_arguments") from None


def remote_failure(exc):
    """只映射固定错误，不展示 HTTP 响应、令牌或服务端自由文本。"""
    if isinstance(exc, BusinessError):
        return exc.code
    if isinstance(exc, BaseExceptionGroup):
        codes = [remote_failure(child) for child in exc.exceptions]
        return next((code for code in codes if code != "mcp_call_failed"), "mcp_call_failed")
    if isinstance(exc, httpx2.HTTPStatusError):
        if exc.response.status_code == 401:
            return "unauthenticated"
        if exc.response.status_code == 403:
            return "permission_denied"
    return "mcp_call_failed"


def remote_tool_name(action, selected):
    """A namespace may qualify a tool; it cannot change the selected action."""
    expected = TOOLS[action]
    if selected == expected:
        return selected
    prefix, separator, suffix = selected.rpartition("__")
    if (
        separator and prefix and suffix == expected and len(prefix) <= 256
        and all(not char.isspace() and ord(char) >= 32 and ord(char) != 127 for char in prefix)
    ):
        return selected
    raise BusinessError("tool_not_available")


async def remote_call(
    endpoint, client_config, action=None, request=None, *, confirm=False, protocol=PROTOCOL,
    tool_name=None,
):
    """通过真实 HTTP MCP 连接调用，服务端统一执行授权和业务审计。"""
    if action is not None and action != "employee_query" and not confirm:
        raise BusinessError("confirmation_required")
    if protocol not in REMOTE_PROTOCOLS or client_config is None:
        raise BusinessError("invalid_arguments")
    endpoint = remote_endpoint(endpoint, client_config)
    headers = remote_headers(client_config, action)
    headers["MCP-Protocol-Version"] = protocol
    gateway = "X-API-Key" in headers
    metadata = {
        "mode": "live", "entrypoint": "metamcp" if gateway else "direct-http",
        "mcp_transport": "streamable-http", "protocol": protocol,
        "identity": "temporary-signed-client", "real_identity_verified": False,
        "caller_identity_forwarded": False if gateway else True,
        "identity_path": "gateway-static-service-principal" if gateway else "direct-jwt",
    }
    attempted = False
    try:
        selected = (
            remote_tool_name(action, tool_name or TOOLS[action]) if action is not None else None
        )

        def http_client(**options):
            # Credentials stay on this exact endpoint, even for same-origin redirects.
            options["follow_redirects"] = False
            # The MCP SDK also has its own within-origin redirect loop.
            options["max_redirects"] = 0
            # This controlled endpoint must not silently send JWTs through an
            # HTTP_PROXY / ALL_PROXY supplied by the shell environment.
            options["trust_env"] = False
            return httpx2.AsyncClient(**options)

        transport = StreamableHttpTransport(
            endpoint, headers=headers, httpx_client_factory=http_client,
        )
        async with MCPClient(
            transport, mode="legacy", auto_initialize=False, timeout=60, init_timeout=15,
        ) as client:
            # SDK initialize() fixes the newest handshake version. Its public typed
            # session API lets us validate the requested legacy Profile exactly.
            init = await client.session.send_request(
                mcp_types.InitializeRequest(
                    params=mcp_types.InitializeRequestParams(
                        protocolVersion=protocol, capabilities=mcp_types.ClientCapabilities(),
                        clientInfo=mcp_types.Implementation(
                            name="t100-live", version=CLIENT_VERSION
                        ),
                    )
                ),
                mcp_types.InitializeResult,
            )
            if init.protocol_version != protocol:
                raise BusinessError("protocol_mismatch")
            client.session.adopt(init)
            await client.session.send_notification(mcp_types.InitializedNotification())
            tools = await client.list_tools(cache_mode="bypass")
            if action is None:
                return {
                    "操作说明": "查看 MCP 服务工具", "状态说明": f"发现 {len(tools)} 个工具",
                    **metadata, "status": "passed", "upstream_attempted": False,
                    "read_requests": 0, "write_requests": 0,
                    "tools": [tool.model_dump(mode="json", by_alias=True) for tool in tools],
                }
            if selected not in {tool.name for tool in tools}:
                raise BusinessError("tool_not_available")
            arguments = {"request": request.model_dump(mode="json", exclude_unset=True)}
            if action != "employee_query":
                arguments["confirm"] = confirm
            attempted = True
            response = await client.call_tool(selected, arguments, raise_on_error=False)
            if response.is_error:
                fixed = [item.text for item in response.content
                         if isinstance(item, mcp_types.TextContent) and item.text in ERROR_MESSAGES]
                raise BusinessError(fixed[0] if len(fixed) == 1 else "mcp_call_failed")
            if (
                not isinstance(response.structured_content, dict)
                or response.structured_content.get("action") != action
                or response.structured_content.get("status") not in {
                    "passed", "verified", "acknowledged", "failed",
                }
            ):
                raise BusinessError("mcp_call_failed")
            result = dict(response.structured_content)
            result.update(metadata, mcp_tool_call_attempted=True)
            return present_result(result, action=action)
    except Exception as exc:
        result = {
            "status": "failed", "error": remote_failure(exc), "business_effect_verified": False,
            **metadata,
            "mcp_tool_call_attempted": attempted,
        }
        # A lost tool response cannot prove whether the server called T100.
        if not attempted:
            result.update(upstream_attempted=False, read_requests=0, write_requests=0)
        return present_result(result, action=action)


async def rpc(client, method, params, identifier):
    packet = {"jsonrpc": "2.0", "method": method, "params": params}
    if identifier is not None:
        packet["id"] = identifier
    response = await client.post("/mcp", json=packet)
    if identifier is None and response.status_code == 202:
        return None
    if response.status_code != 200:
        raise BusinessError("mcp_call_failed")
    packet = response.json()
    if packet.get("id") != identifier or not isinstance(packet.get("result"), dict):
        raise BusinessError("mcp_call_failed")
    return packet["result"]


async def call(
    config, action, request, *, confirm=False, state_dir=STATE, legacy_path=LEGACY_AUDIT
):
    if action != "employee_query" and not confirm:
        raise BusinessError("confirmation_required")
    directory(state_dir)
    directory(state_dir / "cases")
    permissions = initialize_permissions(config, state_dir)

    expected_policy_version = None

    def guard():
        version = permission_guard(config, permissions, action, request)
        if expected_policy_version is not None and version != expected_policy_version:
            raise BusinessError("authorization_changed")
        return version

    expected_policy_version = guard()
    review = selected_status_retry(config, permissions, action, request)
    continuation = selected_status_continuation(config, permissions, action, request)
    legacy_employee_updates = selected_legacy_employee_updates(config, permissions, action, request)
    guard()
    audit = FunctionAudit(
        state_dir / "audit/operations.sqlite3",
        enterprise_id=config.t100.enterprise_id,
        legacy_write_path=legacy_path,
        legacy_status_retry=review,
        status_continuation=continuation,
        legacy_employee_updates=legacy_employee_updates,
    )
    await audit.prepare()
    folder = state_dir / "cases" / str(uuid4())
    directory(folder)
    subject = SUBJECT
    employees, accounts = request_targets(action, request)
    permission = FunctionPermission(
        issuer=ISSUER,
        subject=subject,
        client_id=CLIENT,
        enterprise_id=config.t100.enterprise_id,
        enabled=True,
        action=action,
        request_digest=request_digest(action, request),
        employee_ids=employees,
        account_ids=accounts,
    )
    policy_path = folder / "permission.json"
    save(policy_path, permission.model_dump(mode="json"))
    save(folder / "request.json", {"action": action, "parameter": request.parameter()})
    if review is not None:
        save(
            folder / "legacy-status-retry.json",
            {"approval": review.model_dump(mode="json"), "request_digest": review.request_digest},
        )
    if continuation is not None:
        save(
            folder / "status-continuation.json",
            {
                "approval": continuation.model_dump(mode="json"),
                "request_digest": request_digest(action, request),
            },
        )
    pair = RSAKeyPair.generate()
    token = pair.create_token(
        subject=subject,
        issuer=ISSUER,
        audience=AUDIENCE,
        scopes=[SCOPES[action]],
        additional_claims={"azp": CLIENT},
        expires_in_seconds=120,
    )
    identity = IdentityProfile(
        issuer=ISSUER, audience=AUDIENCE, allowed_clients=frozenset({CLIENT})
    )

    def capture_write_response(evidence):
        try:
            save(folder / "upstream-write-response.json", evidence)
        except Exception:
            raise BusinessError("journal_unavailable") from None

    def capture_prepared_write(identifier, selected_action, effective_request):
        try:
            save(
                folder / "effective-request.json",
                {
                    "request_id": identifier,
                    "action": selected_action,
                    "intent_digest": request_digest(action, request),
                    "effective_request_digest": request_digest(selected_action, effective_request),
                    "parameter": effective_request.parameter(),
                },
            )
        except Exception:
            raise BusinessError("journal_unavailable") from None

    service = FunctionService(
        enterprise_id=config.t100.enterprise_id,
        access=GuardedFunctionAccess(
            policy_path,
            config.credential_file,
            enterprise_id=config.t100.enterprise_id,
            guard=guard,
        ),
        client=T100InterfaceClient(config.t100, on_write_response=capture_write_response),
        audit=audit,
        request_id=review.retry_request_id if review is not None else None,
        on_prepared_write=capture_prepared_write,
    )
    app = create_function_app(
        auth=JWTVerifier(public_key=pair.public_key, issuer=ISSUER, audience=AUDIENCE),
        identity=identity,
        service=service,
    )
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://localhost",
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL,
            },
        ) as client:
            init = await rpc(
                client,
                "initialize",
                {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "t100-live", "version": CLIENT_VERSION},
                },
                1,
            )
            if init.get("protocolVersion") != PROTOCOL:
                raise BusinessError("protocol_mismatch")
            await rpc(client, "notifications/initialized", {}, None)
            arguments = {"request": request.model_dump(mode="json", exclude_unset=True)}
            if action != "employee_query":
                arguments["confirm"] = confirm
            response = await rpc(
                client, "tools/call", {"name": TOOLS[action], "arguments": arguments}, 2
            )
    if response.get("isError") or not isinstance(response.get("structuredContent"), dict):
        raise BusinessError("mcp_call_failed")
    result = response["structuredContent"]
    result.update(
        mode="live",
        identity="temporary-local-operator",
        real_identity_verified=False,
        protocol=PROTOCOL,
        audit_path=str(state_dir / "audit/operations.sqlite3"),
        evidence_path=str(folder / "result.json"),
    )
    if (folder / "effective-request.json").is_file():
        result["effective_request_path"] = str(folder / "effective-request.json")
    if (folder / "upstream-write-response.json").is_file():
        result["upstream_response_path"] = str(folder / "upstream-write-response.json")
    if review is not None:
        result["reviewed_legacy_operation_id"] = review.operation_id
    if continuation is not None:
        result["reviewed_status_operations"] = [
            {"source": row.source, "request_id": row.request_id} for row in continuation.records
        ]
    result = present_result(result, action=action)
    try:
        save(folder / "result.json", result)
    except Exception:
        (folder / "result.json").unlink(missing_ok=True)
        result.update(status="failed", error="journal_unavailable", business_effect_verified=False)
        result.pop("data", None)
        result.pop("evidence_path", None)
    return present_result(result, action=action)


def parser():
    root = argparse.ArgumentParser(description="真实T100：三个员工档案接口＋用户账号维护")
    root.add_argument("--config", type=Path, default=CONFIG, help="现有私有运行配置")
    root.add_argument("--endpoint", help="覆盖客户端配置中的常驻 MCP HTTP 地址")
    root.add_argument(
        "--client-config", type=Path,
        help="私有客户端签名与地址配置；指定即连接常驻服务，省略时沿用本机 ASGI",
    )
    root.add_argument("--protocol", choices=REMOTE_PROTOCOLS, default=PROTOCOL,
                      help="远程连接的明确协议 Profile；不自动回退")
    root.add_argument("--tool-name", help="目标服务实际暴露的工具名，供网关 namespace 使用")
    sub = root.add_subparsers(dest="command", required=True)
    for action in ("employee-query", "employee-create", "employee-update", "account-maintain"):
        item = sub.add_parser(action)
        item.add_argument("--data-file", type=Path, help="完整parameter JSON；文件权限0600")
        item.add_argument("--employee-no")
        if action == "employee-query":
            for name in ("employee-name", "department-no", "date-s", "date-e"):
                item.add_argument("--" + name)
            item.add_argument("--page-size", type=int)
            item.add_argument("--page-no", type=int)
        elif action in {"employee-create", "employee-update"}:
            for name in ("full-name", "department-no", "email", "mobile-phone"):
                item.add_argument("--" + name)
            item.add_argument(
                "--status", choices=["Y", "N"], help="员工档案状态；不控制登录账号启停"
            )
        else:
            item.add_argument("--account-id")
            item.add_argument(
                "--account-status", choices=["Y", "N"], help="登录账号状态 gzxastus：Y 启用，N 禁用"
            )
            item.add_argument(
                "--password-reset",
                choices=["Y", "N"],
                help="是否重置登录账号密码：Y 重置，N 不重置",
            )
            item.add_argument("--is-reproduce", choices=["Y", "N"])
            item.add_argument("--reproduce-acct")
        if action != "employee-query":
            item.add_argument("--confirm", action="store_true")
    status = sub.add_parser("status", help="只读查看统一操作日志")
    status.add_argument("--request-id", required=True)
    sub.add_parser("tools", help="查看指定常驻 MCP 服务开放的工具，不调用 T100")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    logging.disable(logging.CRITICAL)
    entered_call = False
    try:
        if args.endpoint or args.client_config:
            if args.command == "status":
                raise BusinessError("invalid_arguments")
            if args.command == "tools":
                action, request = None, None
            else:
                action, request = make_request(args)
            entered_call = True
            result = asyncio.run(remote_call(
                args.endpoint, args.client_config, action, request,
                confirm=getattr(args, "confirm", False), protocol=args.protocol,
                tool_name=args.tool_name,
            ))
        elif args.tool_name or args.command == "tools" or args.protocol != PROTOCOL:
            raise BusinessError("invalid_arguments")
        elif args.command == "status":
            config = load_runtime_config(args.config)
            audit = FunctionAudit(
                STATE / "audit/operations.sqlite3",
                enterprise_id=config.t100.enterprise_id,
                legacy_write_path=LEGACY_AUDIT,
            )
            result = asyncio.run(audit.status(args.request_id))
            result["upstream_attempted"] = False
        else:
            config = load_runtime_config(args.config)
            action, request = make_request(args)
            entered_call = True
            result = asyncio.run(
                call(config, action, request, confirm=getattr(args, "confirm", False))
            )
    except BusinessError as exc:
        result = {"status": "failed", "error": exc.code, "business_effect_verified": False}
        # 这些异常来自本入口的配置、参数或初始许可检查，发生在 MCP 工具调用前。
        if not entered_call or exc.code in {
            "confirmation_required",
            "write_permission_not_configured",
            "permission_denied",
            "policy_unavailable",
            "runtime_configuration_invalid",
        } or ((args.endpoint or args.client_config) and exc.code == "invalid_arguments"):
            result["upstream_attempted"] = False
    except Exception:
        result = {
            "status": "failed",
            "error": "invalid_arguments",
            "business_effect_verified": False,
        }
        if not entered_call:
            result["upstream_attempted"] = False
    if args.command != "tools" or result.get("status") == "failed":
        result = present_result(
            result, action=args.command.replace("-", "_"), history=args.command == "status"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
