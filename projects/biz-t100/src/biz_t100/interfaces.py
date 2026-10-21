"""四份 T100 原始接口文档的业务参数契约；不包含身份或服务凭据。

集合与字符串上限是本适配器的资源边界，不声称是 T100 数据库字段长度。
员工更新表达变更意图；执行层从精确前查补齐已知保留字段后发出完整报文。
显式空字符串与省略字段保持可区分；空明细数组不等于省略明细。
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import Identifier, Model

ACTIONS = ("employee_query", "employee_create", "employee_update", "account_maintain")
SERVICES = {
    "employee_query": "employee.list.data.query.get",
    "employee_create": "employee.data.create",
    "employee_update": "employee.data.update",
    "account_maintain": "user.account.create",
}
MAX_BATCH = 100
Text = Annotated[str, Field(max_length=512)]
ShortText = Annotated[str, Field(max_length=128)]
NonemptyText = Annotated[str, Field(min_length=1, max_length=512)]
Flag = Literal["Y", "N"]
FieldName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]


def _date(value: str | None, pattern: str) -> None:
    if value is None:
        return
    try:
        parsed = datetime.strptime(value, pattern)
    except ValueError:
        raise ValueError("invalid calendar date") from None
    if parsed.strftime(pattern) != value:
        raise ValueError("date must use the documented format")


class Parameters(Model):
    @model_validator(mode="before")
    @classmethod
    def no_explicit_null(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("omit an optional field instead of sending null")
        return value

    def parameter(self) -> dict:
        # exclude_unset preserves [] while never inventing absent optional fields.
        result = self.model_dump(mode="json", exclude_unset=True, warnings="error")
        if "is_digiwin_product" in type(self).model_fields:
            result["is_digiwin_product"] = self.is_digiwin_product
        if "use_has_next" in type(self).model_fields:
            result["use_has_next"] = self.use_has_next
        return result


class QueryCondition(Parameters):
    sequence: Annotated[str, Field(pattern=r"^[0-9]{1,5}$")] | None = None
    bracket: Literal["(", "((", ")", "))"] | None = None
    field_name: FieldName
    value: Text
    operator: Literal[">", ">=", "=", "<", "<=", "like", "in", "<>"]
    logical: Literal["AND", "OR"] | None = None


class QueryOrder(Parameters):
    field_name: FieldName
    order_type: Literal["asc", "desc"]


class EmployeeQueryRequest(Parameters):
    enterprise_no: Identifier | None = None
    site_no: Identifier | None = None
    is_digiwin_product: Flag = "N"
    data_time: Annotated[str, Field(pattern=r"^[0-9]{14}$")] | None = None
    key_condition: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    use_has_next: bool = False
    page_size: Annotated[int, Field(ge=0, le=500)] | None = None
    page_no: Annotated[int, Field(ge=1, le=1_000_000)] | None = None
    employee_no: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    employee_name: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    department_no: Identifier | None = None
    date_s: Annotated[str, Field(pattern=r"^[0-9]{8}$")] | None = None
    date_e: Annotated[str, Field(pattern=r"^[0-9]{8}$")] | None = None
    conditions: Annotated[list[QueryCondition], Field(max_length=100)] | None = None
    orders: Annotated[list[QueryOrder], Field(max_length=100)] | None = None

    @model_validator(mode="after")
    def bounded_query(self):
        _date(self.data_time, "%Y%m%d%H%M%S")
        _date(self.date_s, "%Y%m%d")
        _date(self.date_e, "%Y%m%d")
        if (self.date_s is None) != (self.date_e is None):
            raise ValueError("date range requires both endpoints")
        if self.date_s is not None and self.date_s > self.date_e:
            raise ValueError("date range is reversed")
        if self.use_has_next and self.page_size == 0:
            raise ValueError("paged query requires positive page_size")
        if not any(
            (
                self.employee_no,
                self.employee_name,
                self.department_no,
                self.date_s,
                self.data_time,
                self.key_condition,
                self.conditions,
            )
        ):
            raise ValueError("query requires an explicit business filter")
        if any(
            value is not None and not value.strip()
            for value in (self.employee_no, self.employee_name, self.key_condition)
        ):
            raise ValueError("query filter cannot be whitespace")
        return self


class EmployeeFields(Parameters):
    employee_no: Identifier
    last_name: ShortText | None = None
    middle_name: ShortText | None = None
    name: ShortText | None = None
    full_name: Text | None = None
    department_no: ShortText | None = None
    job_title: ShortText | None = None
    bank_account: ShortText | None = None
    account_no: ShortText | None = None
    email: Text | None = None
    telephone: ShortText | None = None
    mobile_phone: ShortText | None = None
    remark: Annotated[str, Field(max_length=2048)] | None = None
    status: Flag | None = Field(default=None, description="员工档案状态，不代表登录账号状态")
    modify_no: ShortText | None = None


class EmployeeCreate(EmployeeFields):
    full_name: NonemptyText
    department_no: Identifier
    status: Flag = Field(description="员工档案状态，不代表登录账号状态")
    create_date: Annotated[str, Field(pattern=r"^[0-9]{14}$")] | None = None

    @model_validator(mode="after")
    def created_at(self):
        _date(self.create_date, "%Y%m%d%H%M%S")
        if not self.full_name.strip():
            raise ValueError("full_name cannot be whitespace")
        return self


class EmployeeUpdate(EmployeeFields):
    modify_date: Annotated[str, Field(pattern=r"^[0-9]{14}$")] | None = None

    @model_validator(mode="after")
    def changed_fields(self):
        _date(self.modify_date, "%Y%m%d%H%M%S")
        if not self.model_fields_set - {"employee_no"}:
            raise ValueError("employee update requires at least one changed field")
        return self


class EmployeeWriteRequest(Parameters):
    enterprise_no: Identifier | None = None
    site_no: Identifier | None = None
    is_digiwin_product: Flag = "N"

    @model_validator(mode="after")
    def unique_targets(self):
        targets = [item.employee_no for item in self.employee]
        if len(targets) != len(set(targets)):
            raise ValueError("duplicate employee targets")
        return self


class EmployeeCreateRequest(EmployeeWriteRequest):
    employee: Annotated[list[EmployeeCreate], Field(min_length=1, max_length=MAX_BATCH)]


class EmployeeUpdateRequest(EmployeeWriteRequest):
    employee: Annotated[list[EmployeeUpdate], Field(min_length=1, max_length=MAX_BATCH)]


class AccountSite(Parameters):
    gzxcstus: Flag
    gzxc004: Identifier
    gzxc005: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")] | None = None
    gzxc006: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")] | None = None
    gzxc007: Flag  # 文档标必填并另注明默认 Y；适配器不隐式扩张据点范围。

    @model_validator(mode="after")
    def dates(self):
        _date(self.gzxc005, "%Y-%m-%d")
        _date(self.gzxc006, "%Y-%m-%d")
        if self.gzxc005 and self.gzxc006 and self.gzxc005 > self.gzxc006:
            raise ValueError("site validity range is reversed")
        return self


class AccountDetail(Parameters):
    gzxbstus: Flag
    gzxb002: Literal["0", "1"]
    gzxb003: Identifier
    gzxb004: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")] | None = None
    gzxb005: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")] | None = None
    detail1: Annotated[list[AccountSite], Field(max_length=100)] | None = None

    @model_validator(mode="after")
    def dates(self):
        _date(self.gzxb004, "%Y-%m-%d")
        _date(self.gzxb005, "%Y-%m-%d")
        if self.gzxb004 and self.gzxb005 and self.gzxb004 > self.gzxb005:
            raise ValueError("role validity range is reversed")
        return self


class AccountMaster(Parameters):
    gzxastus: Flag = Field(description="登录账号状态：Y 启用，N 禁用")
    gzxa001: Identifier
    gzxa003: Identifier
    is_reproduce: Flag
    reproduce_acct: Identifier | None = None
    password_reset: Flag = Field(description="登录账号密码重置：Y 重置，N 不重置")
    detail: Annotated[list[AccountDetail], Field(max_length=100)] | None = None

    @model_validator(mode="after")
    def copy_source(self):
        if (self.is_reproduce == "Y") != (self.reproduce_acct is not None):
            raise ValueError("copy source is required exactly when copying is enabled")
        return self


class AccountMaintainRequest(Parameters):
    master: Annotated[list[AccountMaster], Field(min_length=1, max_length=MAX_BATCH)]

    @model_validator(mode="after")
    def unique_accounts(self):
        accounts = [item.gzxa001 for item in self.master]
        if len(accounts) != len(set(accounts)):
            raise ValueError("duplicate account targets")
        return self


REQUEST_MODELS = {
    "employee_query": EmployeeQueryRequest,
    "employee_create": EmployeeCreateRequest,
    "employee_update": EmployeeUpdateRequest,
    "account_maintain": AccountMaintainRequest,
}
