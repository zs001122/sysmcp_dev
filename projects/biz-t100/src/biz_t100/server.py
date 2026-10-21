"""原生 FastMCP 传输、一个仅限本人的用例和显式的集成端口。"""

import json
import time
from uuid import uuid4

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from pydantic import Field
from sysmcp_core import create_service

from .errors import EMPLOYEE_RESULT_ERRORS, error_message
from .models import BusinessError, Caller, EmployeeGrant, EmployeeRecord, Model, QueryResult
from .ports import EmployeeAccess, EmployeeReader, QueryJournal

TOOL_NAME = "get_my_t100_employee_record"
SCOPE = "t100:employee:read:self"
ERROR_CODES = (
    frozenset(
        {
            "unauthenticated",
            "identity_not_enabled",
            "account_not_linked",
            "permission_denied",
            "policy_unavailable",
            "credential_unavailable",
            "authorization_changed",
            "business_adapter_not_ready",
            "upstream_unauthenticated",
            "business_forbidden",
            "upstream_unavailable",
            "upstream_timeout",
            "upstream_rejected",
            "upstream_contract_error",
            "journal_unavailable",
            "query_timeout",
            "query_cancelled",
            "query_failed",
        }
    )
    | EMPLOYEE_RESULT_ERRORS
)


class IdentityProfile(Model):
    """资源身份契约；签名与令牌类型校验由 auth 负责。

    必须使用面向本资源的员工令牌。仅有 MetaMCP 端点密钥不满足本契约。
    EmployeeAccess 还会额外检查当前有效性。
    """

    issuer: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    allowed_clients: frozenset[str] = Field(min_length=1)


def current_caller(profile: IdentityProfile, *, required_scope: str = SCOPE) -> Caller:
    token = get_access_token()
    if token is None:
        raise BusinessError("unauthenticated")
    claims = token.claims
    subject, client = claims.get("sub"), claims.get("azp")
    if (
        claims.get("iss") != profile.issuer
        or claims.get("aud") not in (profile.audience, [profile.audience])
        or not isinstance(subject, str)
        or not subject
        or token.subject != subject
        or not isinstance(client, str)
        or client not in profile.allowed_clients
        or token.client_id != client
        or token.expires_at is None
        or token.expires_at <= time.time()
        or required_scope not in token.scopes
    ):
        raise BusinessError("unauthenticated")
    return Caller(issuer=profile.issuer, subject=subject, client_id=client)


class ToolBoundary(Middleware):
    def __init__(self, profile: IdentityProfile):
        self.profile = profile

    async def on_list_tools(self, context, call_next):
        try:
            current_caller(self.profile)
        except (BusinessError, ValueError):
            raise ToolError("unauthenticated") from None
        return await call_next(context)

    async def on_call_tool(self, context, call_next):
        # 在模式校验回显用户提交的身份或秘密之前先行拒绝。
        if context.message.name != TOOL_NAME:
            raise ToolError("tool_not_available")
        if context.message.arguments not in (None, {}):
            raise ToolError("invalid_arguments")
        return await call_next(context)


def _same_binding(before: EmployeeGrant, after: EmployeeGrant) -> None:
    if (before.employee_id, before.binding_version) != (after.employee_id, after.binding_version):
        raise BusinessError("authorization_changed")


def create_server(
    *,
    auth: AuthProvider,
    identity: IdentityProfile,
    access: EmployeeAccess,
    reader: EmployeeReader,
    journal: QueryJournal,
    budget_seconds: float = 10.0,
) -> FastMCP:
    """构建但不启动服务。所有运行依赖都必须显式提供。

    T100 服务账号访问必须显式配置（ADR-003）。
    其管理员凭据绝不能替代调用者本人的授权。
    """
    if access is None or reader is None or journal is None:
        raise ValueError("Access, reader and durable journal must be explicitly supplied")
    if not 0 < budget_seconds <= 10:
        raise ValueError("Query budget must be greater than zero and at most 10 seconds")
    server = create_service("t100-employee-readonly", auth=auth)
    server.add_middleware(ToolBoundary(identity))

    @server.tool(
        name=TOOL_NAME,
        annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True},
    )
    async def get_my_t100_employee_record() -> QueryResult:
        """查询当前受信员工本人的 T100 资料；status 表示员工档案状态，不接受代查参数。"""
        request_id = str(uuid4())
        caller: Caller | None = None
        employee_id: str | None = None
        terminal_recorded = False
        upstream_attempted = False
        deadline = time.monotonic() + budget_seconds

        def check_current():
            # 受线程屏蔽的提交可能在取消/超时之后才完成。
            if time.monotonic() >= deadline:
                raise TimeoutError()
            current_caller(identity)

        async def record(event, code=None):
            if caller is None:
                return
            try:
                await journal.record(
                    request_id=request_id,
                    caller=caller,
                    employee_id=employee_id,
                    event=event,
                    code=code,
                    upstream_attempted=upstream_attempted,
                )
            except Exception:
                raise BusinessError("journal_unavailable") from None

        async def rejected(code: str):
            # 成功提交后不再写第二个终态记录；结果投递仍可能超时。
            if not terminal_recorded:
                try:
                    with anyio.move_on_after(1.0, shield=True) as cleanup:
                        await record("query_failed", code)
                    if cleanup.cancel_called:
                        code = "journal_unavailable"
                except BusinessError:
                    code = "journal_unavailable"
            raise ToolError(
                json.dumps(
                    {"error": code, "message": error_message(code), "request_id": request_id},
                    ensure_ascii=False,
                )
            ) from None

        try:
            with anyio.fail_after(budget_seconds):
                caller = current_caller(identity)
                grant = EmployeeGrant.model_validate(await access.authorize(caller))
                check_current()
                employee_id = grant.employee_id
                await record("query_started")
                check_current()
                fresh = EmployeeGrant.model_validate(await access.authorize(caller))
                check_current()
                _same_binding(grant, fresh)
                await anyio.lowlevel.checkpoint_if_cancelled()
                # 即使之后授权发生变化，也要把这一事实持久化到终态事件中。
                # 它证明已进入读取器，而非远端服务器已收到请求。
                upstream_attempted = True
                result = EmployeeRecord.model_validate(await reader.read(fresh))
                check_current()
                if result.employee_id != employee_id:
                    raise BusinessError("upstream_employee_mismatch")
                _same_binding(grant, EmployeeGrant.model_validate(await access.authorize(caller)))
                check_current()
                await record("query_succeeded")
                terminal_recorded = True
                check_current()
                await anyio.lowlevel.checkpoint_if_cancelled()
                return QueryResult(request_id=request_id, data=result)
        except TimeoutError:
            await rejected("query_timeout")
        except anyio.get_cancelled_exc_class():
            # 已持久化的开始记录若没有终态，会保留可见以便核查。
            if not terminal_recorded:
                with anyio.move_on_after(1.0, shield=True):
                    try:
                        await record("query_cancelled", "query_cancelled")
                    except BusinessError:
                        pass
            raise
        except BusinessError as exc:
            await rejected(exc.code if exc.code in ERROR_CODES else "query_failed")
        except Exception:
            await rejected("query_failed")

    return server


def create_app(**dependencies):
    """用于显式组合的 ASGI 应用工厂；不启动监听器。"""
    return create_server(**dependencies).http_app(
        path="/mcp", stateless_http=True, json_response=True
    )
