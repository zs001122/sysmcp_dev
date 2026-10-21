"""显式账号维护计划与独立写工厂。

user.account.create 是维护方提供的 upsert 服务。这里仅定义精确单账号
报文及受控的执行机制，不证明省略角色/站点时上游会保留这些资料。
装配方必须先取得对应的副作用契约；仅有 ACK 不代表业务状态已生效。
"""

from typing import Literal

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from pydantic import ValidationError, model_validator

from .client import T100Config
from .models import BusinessError, Identifier, Model, PasswordResetGrant
from .reset_client import ResetReceipt, _send_master
from .reset_server import PasswordResetAccess, PasswordResetter, ResetStore, _create_write_server
from .server import IdentityProfile

Action = Literal["reset_password", "disable_account"]
DISABLE_TOOL_NAME = "disable_my_t100_account"
DISABLE_SCOPE = "t100:account:disable:self"


class AccountWritePlan(Model):
    """无凭据的受控计划；工号和账号分别绑定，状态不得靠默认值推定。"""

    action: Action
    employee_id: Identifier
    account_id: Identifier
    account_status: Literal["Y", "N"]

    @model_validator(mode="after")
    def check_action_status(self):
        if self.action == "disable_account" and self.account_status != "N":
            raise ValueError("disable_account requires account_status N")
        return self

    def master(self) -> dict[str, str]:
        validated = AccountWritePlan.model_validate(self.model_dump())
        return {
            "gzxa001": validated.account_id,
            "gzxa003": validated.employee_id,
            "gzxastus": validated.account_status,
            "is_reproduce": "N",
            "password_reset": "Y" if validated.action == "reset_password" else "N",
        }


def action_profile(action: Action) -> tuple[str, str]:
    """返回固定的工具名、scope，不允许动态注册任意写工具。"""
    if action == "reset_password":
        return "reset_my_t100_password", "t100:password:reset:self"
    if action == "disable_account":
        return DISABLE_TOOL_NAME, DISABLE_SCOPE
    raise ValueError("Unsupported account maintenance action")


class T100AccountWriteClient:
    """按显式计划尝试一次维护请求；不会自动重试、跟随跳转或读取代理环境。"""

    def __init__(
        self,
        config: T100Config,
        plan: AccountWritePlan,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = T100Config.model_validate(config.model_dump())
        self.plan = AccountWritePlan.model_validate(plan.model_dump())
        self._transport = transport

    async def reset(self, grant: PasswordResetGrant) -> ResetReceipt:
        """沿现有写端口执行计划；目标必须与本次可信授权完全一致。"""
        if type(grant) is not PasswordResetGrant:
            raise BusinessError("upstream_contract_error")
        try:
            grant = PasswordResetGrant(
                employee_id=grant.employee_id,
                account_id=grant.account_id,
                binding_version=grant.binding_version,
                credential=grant.credential,
            )
            plan = AccountWritePlan.model_validate(self.plan.model_dump())
            config = T100Config.model_validate(self.config.model_dump())
        except (ValidationError, AttributeError):
            raise BusinessError("upstream_contract_error") from None
        if (grant.employee_id, grant.account_id) != (plan.employee_id, plan.account_id):
            raise BusinessError("authorization_changed")
        credential = grant.credential.get_secret_value()
        if not credential.strip():
            raise BusinessError("upstream_unauthenticated")
        return await _send_master(
            config, plan.master(), credential=credential, transport=self._transport
        )


def create_account_write_server(
    *,
    auth: AuthProvider,
    identity: IdentityProfile,
    access: PasswordResetAccess,
    writer: PasswordResetter,
    store: ResetStore,
    action: Action,
    budget_seconds: float = 10.0,
) -> FastMCP:
    """每个实例只暴露一个动作；派发后任何错误均保持结果未知与双目标锁。"""
    tool_name, required_scope = action_profile(action)
    if isinstance(writer, T100AccountWriteClient) and writer.plan.action != action:
        raise ValueError("Writer action does not match the exposed tool")
    description = (
        "重置调用者本人受控绑定的T100账号密码。"
        if action == "reset_password"
        else "将调用者本人受控绑定的T100登录账号status设为N；不修改员工档案。"
    ) + (
        "须明确确认并复用同一操作UUID。仅报告上游应答，不证明业务状态已生效。"
        "结果未知时禁止自动重试；不接受工号、账号、状态、密码或服务key参数。"
    )
    return _create_write_server(
        auth=auth,
        identity=identity,
        access=access,
        resetter=writer,
        store=store,
        budget_seconds=budget_seconds,
        tool_name=tool_name,
        required_scope=required_scope,
        service_name=f"t100-{action.replace('_', '-')}",
        description=description,
        # HTTP 401/403 也没有已验证的“未发生写入”契约。
        rejected_error_codes=frozenset(),
    )


def create_account_write_app(**dependencies):
    """构建原生 /mcp ASGI 应用；真实入口的契约准入由装配方明确校验。"""
    return create_account_write_server(**dependencies).http_app(
        path="/mcp", stateless_http=True, json_response=True
    )
