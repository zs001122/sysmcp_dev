"""历史员工档案状态更新兼容实现，写入 employee.status 并精确回读。

只允许 employee_no/status；未传字段保持的语义由运行装配方按部署契约准入。
登录账号仍作为可信授权与操作占用边界，不把它发送到员工资料接口。
旧工具名称为历史兼容保留；员工档案状态不能证明登录账号启停。
"""

import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol

import anyio
import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from pydantic import ValidationError, model_validator

from .client import (
    MAX_RESPONSE_BYTES,
    REQUEST_BUDGET_SECONDS,
    T100Config,
    T100EmployeeClient,
    _check_status,
)
from .errors import EMPLOYEE_RESULT_ERRORS
from .models import BusinessError, Identifier, Model, PasswordResetGrant
from .reset_client import ResetReceipt
from .reset_server import PasswordResetAccess, ResetStore, _create_write_server
from .server import IdentityProfile, current_caller

StatusAction = Literal["enable_account", "disable_account"]
ENABLE_TOOL_NAME = "enable_my_t100_account"
ENABLE_SCOPE = "t100:account:enable:self"
DISABLE_TOOL_NAME = "disable_my_t100_account"
DISABLE_SCOPE = "t100:account:disable:self"


class EmployeeStatusPlan(Model):
    """员工及账号均来自受控绑定；启停动作与 Y/N 状态严格对应。"""

    action: StatusAction
    employee_id: Identifier
    account_id: Identifier
    account_status: Literal["Y", "N"]

    @model_validator(mode="after")
    def check_action_status(self):
        expected = "Y" if self.action == "enable_account" else "N"
        if self.account_status != expected:
            raise ValueError("Account action and status do not match")
        return self

    def parameter(self) -> dict:
        selected = EmployeeStatusPlan.model_validate(self.model_dump())
        return {
            "is_digiwin_product": "N",
            "employee": [{"employee_no": selected.employee_id, "status": selected.account_status}],
        }


class EmployeeStatusReceipt(Model):
    """明确关联目标的状态回读证明；不能用账号写入 ACK 代替。"""

    acknowledged: Literal[True] = True
    employee_id: Identifier
    account_id: Identifier
    observed_status: Literal["Y", "N"]
    effect_verified: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def require_true_booleans(cls, value):
        if isinstance(value, dict) and (
            value.get("acknowledged", True) is not True or value.get("effect_verified") is not True
        ):
            raise ValueError("Status receipt requires explicit true verification")
        return value


class EmployeeStatusWriter(Protocol):
    async def reset(
        self,
        grant: PasswordResetGrant,
        *,
        before_write: Callable[[PasswordResetGrant], Awaitable[None]] | None = None,
    ) -> EmployeeStatusReceipt: ...


def action_profile(action: StatusAction) -> tuple[str, str]:
    if action == "enable_account":
        return ENABLE_TOOL_NAME, ENABLE_SCOPE
    if action == "disable_account":
        return DISABLE_TOOL_NAME, DISABLE_SCOPE
    raise ValueError("Unsupported employee status action")


def _update_receipt(body: bytes, employee_id: str) -> ResetReceipt:
    """总状态和逐条目标/执行描述都必须明确成功，拒绝 ACK-only 响应。"""
    try:
        std = json.loads(body)["payload"]["std_data"]
        code = std["execution"]["code"]
        if type(code) not in (str, int):
            raise ValueError()
        if code not in ("0", 0):
            raise BusinessError("upstream_rejected")
        rows = std["parameter"]["employee"]
        if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
            raise ValueError()
        row = rows[0]
        if any(
            type(row[key]) is not str or row[key] != employee_id for key in ("source_no", "erp_no")
        ):
            raise ValueError()
        if type(row["execute_description"]) is not str or row["execute_description"] != "":
            raise ValueError()
        return ResetReceipt()
    except (ValueError, KeyError, TypeError, RecursionError):
        raise BusinessError("upstream_contract_error") from None


def _write_codes(body: bytes, *, credential: str) -> dict[str, str | int]:
    """只取长度有界的数字错误码；未知原文和响应其余字段一律丢弃。"""
    try:
        execution = json.loads(body)["payload"]["std_data"]["execution"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return {}
    if type(execution) is not dict:
        return {}
    result: dict[str, str | int] = {}
    types = {
        str: "string",
        int: "integer",
        bool: "boolean",
        float: "number",
        dict: "object",
        list: "array",
        type(None): "null",
    }
    for field, target in (("code", "execution_code"), ("sql_code", "sql_code")):
        if field not in execution:
            continue
        value = execution[field]
        permitted = (
            type(value) is str and len(value) <= 32 and re.fullmatch(r"-?[0-9]+", value) is not None
        ) or (type(value) is int and len(str(value)) <= 32)
        if permitted and str(value) != credential:
            result[target] = value
        else:
            result[target + "_type"] = types[type(value)]
    return result


class T100EmployeeStatusClient:
    """先证明员工存在，再只更新一次，最后读回；三个请求共用十秒总预算。"""

    def __init__(
        self,
        config: T100Config,
        plan: EmployeeStatusPlan,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = T100Config.model_validate(config.model_dump())
        self.plan = EmployeeStatusPlan.model_validate(plan.model_dump())
        self._transport = transport
        self.write_attempted = False
        self.read_attempts = 0
        self.observed_status: Literal["Y", "N"] | None = None
        self.effect_verified = False
        self.write_diagnostics: dict[str, str | int] = {}

    async def reset(
        self,
        grant: PasswordResetGrant,
        *,
        before_write: Callable[[PasswordResetGrant], Awaitable[None]] | None = None,
    ) -> EmployeeStatusReceipt:
        self.write_attempted = False
        self.read_attempts = 0
        self.observed_status = None
        self.effect_verified = False
        self.write_diagnostics = {}
        if type(grant) is not PasswordResetGrant:
            raise BusinessError("upstream_contract_error")
        try:
            grant = PasswordResetGrant(
                employee_id=grant.employee_id,
                account_id=grant.account_id,
                binding_version=grant.binding_version,
                credential=grant.credential,
            )
            selected = EmployeeStatusPlan.model_validate(self.plan.model_dump())
            cfg = T100Config.model_validate(self.config.model_dump())
        except (ValidationError, AttributeError):
            raise BusinessError("upstream_contract_error") from None
        if (grant.employee_id, grant.account_id) != (selected.employee_id, selected.account_id):
            raise BusinessError("authorization_changed")
        credential = grant.credential.get_secret_value()
        if not credential.strip():
            raise BusinessError("upstream_unauthenticated")
        reader = T100EmployeeClient(cfg, transport=self._transport)
        try:
            with anyio.fail_after(REQUEST_BUDGET_SECONDS):
                self.read_attempts += 1
                await reader.read(grant)
                if before_write is not None:
                    await before_write(grant)
                await self._update(cfg, selected, credential)
                self.read_attempts += 1
                record = await reader.read(grant)
                if record.employee_id != selected.employee_id:
                    raise BusinessError("upstream_contract_error")
                if record.employee_status in {"Y", "N"}:
                    self.observed_status = record.employee_status
                if record.employee_status != selected.account_status:
                    raise BusinessError("upstream_contract_error")
                self.effect_verified = True
                return EmployeeStatusReceipt(
                    employee_id=selected.employee_id,
                    account_id=selected.account_id,
                    observed_status=record.employee_status,
                    effect_verified=True,
                )
        except BusinessError as exc:
            if exc.code in EMPLOYEE_RESULT_ERRORS:
                raise BusinessError("upstream_contract_error") from None
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise BusinessError("upstream_timeout") from None
        except httpx.DecodingError:
            raise BusinessError("upstream_contract_error") from None
        except httpx.RequestError:
            raise BusinessError("upstream_unavailable") from None

    async def _update(
        self, cfg: T100Config, selected: EmployeeStatusPlan, credential: str
    ) -> ResetReceipt:
        envelope = {
            "key": credential,
            "type": "sync",
            "host": {
                "prod": cfg.host_product,
                "ip": cfg.host_ip,
                "lang": "zh_CN",
                "acct": cfg.host_account,
                "timestamp": datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3],
            },
            "service": {
                "prod": "T100",
                "name": "employee.data.update",
                "ip": cfg.service_ip,
                "id": cfg.service_id,
            },
            "datakey": {"EntId": cfg.enterprise_id},
            "payload": {"std_data": {"parameter": selected.parameter()}},
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_BUDGET_SECONDS, connect=3.0),
            trust_env=False,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            self.write_attempted = True
            async with client.stream("POST", cfg.url, json=envelope) as response:
                self.write_diagnostics["http_status"] = response.status_code
                _check_status(response.status_code)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise BusinessError("upstream_contract_error")
                    body.extend(chunk)
        self.write_diagnostics.update(_write_codes(bytes(body), credential=credential))
        return _update_receipt(bytes(body), selected.employee_id)


def create_employee_status_server(
    *,
    auth: AuthProvider,
    identity: IdentityProfile,
    access: PasswordResetAccess,
    writer: EmployeeStatusWriter,
    store: ResetStore,
    action: StatusAction,
    budget_seconds: float = 10.0,
) -> FastMCP:
    if writer is None or access is None or store is None:
        raise ValueError("Status access, writer and durable store are required")
    tool_name, required_scope = action_profile(action)
    if isinstance(writer, T100EmployeeStatusClient) and writer.plan.action != action:
        raise ValueError("Writer action does not match the exposed tool")

    async def before_write(grant: PasswordResetGrant) -> None:
        caller = current_caller(identity, required_scope=required_scope)
        current = await access.authorize(caller)
        fresh_caller = current_caller(identity, required_scope=required_scope)
        if fresh_caller != caller:
            raise BusinessError("authorization_changed")
        if type(current) is not PasswordResetGrant:
            raise BusinessError("authorization_changed")
        try:
            current = PasswordResetGrant(
                employee_id=current.employee_id,
                account_id=current.account_id,
                binding_version=current.binding_version,
                credential=current.credential,
            )
        except (ValidationError, AttributeError):
            raise BusinessError("authorization_changed") from None
        if (grant.employee_id, grant.account_id, grant.binding_version) != (
            current.employee_id,
            current.account_id,
            current.binding_version,
        ):
            raise BusinessError("authorization_changed")

    class GuardedWriter:
        async def reset(self, grant: PasswordResetGrant) -> ResetReceipt:
            receipt = await writer.reset(grant, before_write=before_write)
            if type(receipt) is not EmployeeStatusReceipt:
                raise BusinessError("upstream_contract_error")
            try:
                receipt = EmployeeStatusReceipt.model_validate(receipt.model_dump())
            except (ValidationError, AttributeError):
                raise BusinessError("upstream_contract_error") from None
            expected_status = "Y" if action == "enable_account" else "N"
            if (receipt.employee_id, receipt.account_id, receipt.observed_status) != (
                grant.employee_id,
                grant.account_id,
                expected_status,
            ):
                raise BusinessError("upstream_contract_error")
            return ResetReceipt()

    return _create_write_server(
        auth=auth,
        identity=identity,
        access=access,
        resetter=GuardedWriter(),
        store=store,
        budget_seconds=budget_seconds,
        tool_name=tool_name,
        required_scope=required_scope,
        service_name=f"t100-{action.replace('_', '-')}",
        description=(
            "历史员工档案状态工具，仅更新员工资料status并查询验证结果；不能验证登录账号启停。"
            "须明确确认并复用同一操作UUID；不接受工号、账号、状态或服务key参数。"
            "任何未确认结果不得自动重试。"
        ),
        rejected_error_codes=frozenset(),
        success_status="business_verified",
    )


def create_employee_status_app(**dependencies):
    return create_employee_status_server(**dependencies).http_app(
        path="/mcp", stateless_http=True, json_response=True
    )
