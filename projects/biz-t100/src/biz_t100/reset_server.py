"""仅限本人的密码重置，与员工查询服务隔离。

本工厂没有真实环境默认值。当前上游适配器只是离线契约候选；
旧的 upsert 端点尚未证明只有重置副作用。
"""

import json
import time
from typing import Literal, Protocol
from uuid import UUID

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from fastmcp.server.middleware import Middleware
from sysmcp_core import create_service

from .errors import EMPLOYEE_RESULT_ERRORS
from .models import BusinessError, Caller, Model, PasswordResetGrant
from .reset_client import ResetReceipt
from .server import ERROR_CODES, IdentityProfile, current_caller

RESET_TOOL_NAME = "reset_my_t100_password"
RESET_SCOPE = "t100:password:reset:self"
# 员工查询的结果码不并入独立的重置/审计契约。
RESET_ERRORS = (ERROR_CODES - EMPLOYEE_RESULT_ERRORS) | {
    "reset_already_recorded",
    "reset_pending_review",
    "reset_outcome_unknown",
    "reset_timeout",
    "reset_cancelled",
    "reset_failed",
}


class PasswordResetAccess(Protocol):
    async def authorize(self, caller: Caller) -> PasswordResetGrant: ...


class PasswordResetter(Protocol):
    async def reset(self, grant: PasswordResetGrant) -> ResetReceipt: ...


class ResetStore(Protocol):
    async def reserve(
        self,
        *,
        operation_id: str,
        caller: Caller,
        employee_id: str,
        account_id: str,
        binding_version: str,
    ) -> None: ...

    async def mark_dispatched(self, *, operation_id: str) -> None: ...

    async def finish(
        self,
        *,
        operation_id: str,
        outcome: Literal["succeeded", "rejected", "not_sent", "unknown"],
        code: str | None = None,
    ) -> None: ...


class ResetResult(Model):
    operation_id: str
    status: Literal["upstream_acknowledged", "business_verified"] = "upstream_acknowledged"


def _arguments(value):
    if type(value) is not dict or set(value) != {"confirm", "operation_id"}:
        raise ToolError("invalid_arguments")
    operation_id = value["operation_id"]
    if type(value["confirm"]) is not bool or type(operation_id) is not str:
        raise ToolError("invalid_arguments")
    try:
        if str(UUID(operation_id)) != operation_id:
            raise ValueError()
    except ValueError:
        raise ToolError("invalid_arguments") from None
    if not value["confirm"]:
        raise ToolError("confirmation_required")


class ResetBoundary(Middleware):
    def __init__(self, identity, *, tool_name=RESET_TOOL_NAME, required_scope=RESET_SCOPE):
        self.identity = identity
        self.tool_name = tool_name
        self.required_scope = required_scope

    async def on_list_tools(self, context, call_next):
        try:
            current_caller(self.identity, required_scope=self.required_scope)
        except (BusinessError, ValueError):
            raise ToolError("unauthenticated") from None
        return await call_next(context)

    async def on_call_tool(self, context, call_next):
        if context.message.name != self.tool_name:
            raise ToolError("tool_not_available")
        # 在模式校验错误回显之前，先拒绝目标/凭据/密码覆盖参数。
        _arguments(context.message.arguments)
        return await call_next(context)


def _same_reset_binding(before, after):
    if (before.employee_id, before.account_id, before.binding_version) != (
        after.employee_id,
        after.account_id,
        after.binding_version,
    ):
        raise BusinessError("authorization_changed")


def _create_write_server(
    *,
    auth: AuthProvider,
    identity: IdentityProfile,
    access: PasswordResetAccess,
    resetter: PasswordResetter,
    store: ResetStore,
    budget_seconds: float = 10.0,
    tool_name: str,
    required_scope: str,
    service_name: str,
    description: str,
    rejected_error_codes: frozenset[str],
    success_status: Literal["upstream_acknowledged", "business_verified"] = "upstream_acknowledged",
) -> FastMCP:
    """独立的写部署单元；绝不向读工厂添加写能力。

    调用者 scope 与运维方控制的重置权限必须同时放行。
    confirm 只是显式的协议确认，不证明已获得人的批准。
    """
    if access is None or resetter is None or store is None:
        raise ValueError("Reset access, writer and durable store are required")
    if not 0 < budget_seconds <= 10:
        raise ValueError("Reset budget must be greater than zero and at most 10 seconds")
    server = create_service(service_name, auth=auth)
    server.add_middleware(
        ResetBoundary(identity, tool_name=tool_name, required_scope=required_scope)
    )

    @server.tool(
        name=tool_name,
        description=description,
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def write_account(confirm: bool, operation_id: str) -> ResetResult:
        """执行工厂指定的唯一账号维护动作，复用授权与持久化写状态机。"""
        _arguments({"confirm": confirm, "operation_id": operation_id})
        reserved = False
        marked = False
        attempted = False
        terminal = False
        deadline = time.monotonic() + budget_seconds

        def check_current():
            if time.monotonic() >= deadline:
                raise TimeoutError()
            return current_caller(identity, required_scope=required_scope)

        async def authorized():
            caller = check_current()
            grant = await access.authorize(caller)
            # 读授权绝不能静默变成写权限。
            if type(grant) is not PasswordResetGrant:
                raise BusinessError("permission_denied")
            grant = PasswordResetGrant.model_validate(
                {
                    **grant.model_dump(),
                    "credential": grant.credential,
                }
            )
            check_current()
            return caller, grant

        async def durable(method, **values):
            try:
                return await method(**values)
            except BusinessError as exc:
                if exc.code in {"reset_already_recorded", "reset_pending_review"}:
                    raise
                raise BusinessError("journal_unavailable") from None
            except Exception:
                raise BusinessError("journal_unavailable") from None

        async def save_failure(code):
            if not reserved or terminal:
                return code
            # 持久化的派发标记可能在实际请求前的崩溃中保留下来。
            # 必须保守处理；绝不释放状态不明的目标。
            if not marked:
                outcome = "not_sent"
            elif attempted and code in rejected_error_codes:
                outcome = "rejected"
            else:
                outcome = "unknown"
            try:
                with anyio.move_on_after(1.0, shield=True) as cleanup:
                    await store.finish(operation_id=operation_id, outcome=outcome, code=code)
                if cleanup.cancel_called:
                    return "journal_unavailable"
            except Exception:
                return "journal_unavailable"
            return "reset_outcome_unknown" if outcome == "unknown" else code

        try:
            with anyio.fail_after(budget_seconds):
                caller, grant = await authorized()
                await durable(
                    store.reserve,
                    operation_id=operation_id,
                    caller=caller,
                    employee_id=grant.employee_id,
                    account_id=grant.account_id,
                    binding_version=grant.binding_version,
                )
                reserved = True
                check_current()
                _, fresh = await authorized()
                _same_reset_binding(grant, fresh)
                await durable(store.mark_dispatched, operation_id=operation_id)
                marked = True
                # 磁盘等待期间可能发生吊销/重新绑定；I/O 前再核对一次。
                _, fresh = await authorized()
                _same_reset_binding(grant, fresh)
                await anyio.lowlevel.checkpoint_if_cancelled()
                check_current()
                attempted = True
                receipt = await resetter.reset(fresh)
                if type(receipt) is not ResetReceipt:
                    raise BusinessError("upstream_contract_error")
                ResetReceipt.model_validate(receipt.model_dump())
                _, final_grant = await authorized()
                _same_reset_binding(grant, final_grant)
                await durable(store.finish, operation_id=operation_id, outcome="succeeded")
                terminal = True
                check_current()
                await anyio.lowlevel.checkpoint_if_cancelled()
                return ResetResult(operation_id=operation_id, status=success_status)
        except TimeoutError:
            code = await save_failure("reset_timeout")
        except anyio.get_cancelled_exc_class():
            await save_failure("reset_cancelled")
            raise
        except BusinessError as exc:
            code = await save_failure(exc.code if exc.code in RESET_ERRORS else "reset_failed")
        except Exception:
            code = await save_failure("reset_failed")
        raise ToolError(json.dumps({"error": code, "operation_id": operation_id})) from None

    return server


def create_reset_server(
    *,
    auth: AuthProvider,
    identity: IdentityProfile,
    access: PasswordResetAccess,
    resetter: PasswordResetter,
    store: ResetStore,
    budget_seconds: float = 10.0,
) -> FastMCP:
    """保持离线候选工厂的工具、scope 与既有拒绝语义。"""
    return _create_write_server(
        auth=auth,
        identity=identity,
        access=access,
        resetter=resetter,
        store=store,
        budget_seconds=budget_seconds,
        tool_name=RESET_TOOL_NAME,
        required_scope=RESET_SCOPE,
        service_name="t100-password-reset",
        description=(
            "重置调用者本人受控绑定的T100账号密码。须明确确认并复用同一操作UUID。"
            "只报告上游已应答，不返回密码，不证明新密码已交付或登录生效。"
            "结果未知时禁止自动重试；不接受工号、账号、密码、姓名或服务key。"
        ),
        rejected_error_codes=frozenset({"upstream_unauthenticated", "business_forbidden"}),
    )


def create_reset_app(**dependencies):
    """只构建原生 /mcp ASGI 应用；无监听、上游或身份默认值。"""
    return create_reset_server(**dependencies).http_app(
        path="/mcp", stateless_http=True, json_response=True
    )
