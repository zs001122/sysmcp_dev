"""仅包含 T100 员工事实；来源与语义差异记录在 RU-001 中。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Caller(Model):
    issuer: str = Field(min_length=1, max_length=512)
    subject: str = Field(min_length=1, max_length=256)
    client_id: str = Field(min_length=1, max_length=256)


class EmployeeGrant(Model):
    """由配置的授权提供方签发，绝不来自工具入参。

    提供方先对调用者完成授权，再提供配置的 T100 服务凭据。
    该授权不是员工原生令牌，也不等同于 ERP 原生 ACL。
    """

    employee_id: Identifier
    binding_version: str = Field(min_length=1, max_length=256)
    credential: SecretStr = Field(repr=False, exclude=True)


class PasswordResetGrant(EmployeeGrant):
    """单独授权的登录账号，绝不从 employee_id 推导。"""

    account_id: Identifier


class EmployeeRecord(Model):
    system: Literal["t100"] = "t100"
    employee_id: Identifier
    display_name: str | None = Field(default=None, max_length=256)
    department: str | None = Field(default=None, max_length=128)
    # 保留公开字段名兼容性；对应上游 status，当前部署约定 Y 有效、N 无效。
    employee_status: str | None = Field(default=None, max_length=64)
    observation: Literal["employee_record"] = "employee_record"


class QueryResult(Model):
    request_id: str
    data: EmployeeRecord


class BusinessError(Exception):
    """只允许暴露固定错误码；绝不附带上游文本或报文。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
