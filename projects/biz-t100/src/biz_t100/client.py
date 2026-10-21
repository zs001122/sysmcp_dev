"""有边界的 T100 员工查询；按 RU-001 选择性适配，无真实环境默认值。"""

import json
from datetime import datetime
from ipaddress import IPv4Address, IPv4Network
from typing import Literal
from urllib.parse import urlsplit

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import BusinessError, EmployeeGrant, EmployeeRecord

REQUEST_BUDGET_SECONDS = 10.0
MAX_RESPONSE_BYTES = 2_000_000


class T100Config(BaseModel):
    """运维方提供的端点元数据；凭据属于已授权的授权对象。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    transport_profile: Literal["https", "http-test"] = "https"
    url: str = Field(min_length=1, max_length=2048)
    enterprise_id: str = Field(min_length=1, max_length=256)
    host_product: str = Field(min_length=1, max_length=256)
    host_ip: str = Field(min_length=1, max_length=256)
    host_account: str = Field(min_length=1, max_length=256)
    service_ip: str = Field(min_length=1, max_length=256)
    service_id: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def fixed_endpoint(self):
        value = self.url
        try:
            parts = urlsplit(value)
            if self.transport_profile == "http-test":
                # 显式的旧式测试 Profile；不做 DNS 解析、重定向或自动回退。
                address = IPv4Address(parts.hostname or "")
                transport_allowed = parts.scheme == "http" and any(
                    address in IPv4Network(network)
                    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
                )
            else:
                transport_allowed = parts.scheme == "https"
            valid = (
                transport_allowed
                and bool(parts.hostname)
                and parts.username is None
                and parts.password is None
                and "?" not in value
                and "#" not in value
                and not any(ord(char) <= 32 or ord(char) == 127 for char in value)
                and (parts.port is None or 0 < parts.port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("fixed endpoint must match its explicit transport profile")
        return self


def _check_status(status: int) -> None:
    if 200 <= status < 300:
        return
    if status == 401:
        code = "upstream_unauthenticated"
    elif status == 403:
        code = "business_forbidden"
    elif status == 429 or 500 <= status < 600:
        code = "upstream_unavailable"
    else:
        code = "upstream_rejected"
    raise BusinessError(code)


def _employee_record(body: bytes, employee_id: str) -> EmployeeRecord:
    try:
        std = json.loads(body)["payload"]["std_data"]
        code = std["execution"]["code"]
        if type(code) not in (str, int):
            raise ValueError("invalid execution code type")
        if code not in ("0", 0):
            raise BusinessError("upstream_rejected")
        employees = std["parameter"]["employee"]
        if type(employees) is not list:
            raise ValueError("employee list required")
        if not employees:
            raise BusinessError("employee_not_found")
        if len(employees) != 1:
            raise BusinessError("employee_not_unique")
        employee = employees[0]
        if type(employee) is not dict or "employee_no" not in employee:
            raise ValueError("employee identifier field required")
        if type(employee["employee_no"]) is not str or employee["employee_no"] != employee_id:
            raise BusinessError("upstream_employee_mismatch")
        return EmployeeRecord(
            employee_id=employee_id,
            display_name=employee.get("full_name"),
            department=employee.get("department_no"),
            employee_status=employee.get("status"),
        )
    except (ValueError, KeyError, TypeError, RecursionError):
        raise BusinessError("upstream_contract_error") from None


class T100EmployeeClient:
    def __init__(
        self, config: T100Config, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        # model_copy/model_construct 即使在冻结模型上也能绕过校验。
        self.config = T100Config.model_validate(config.model_dump())
        self._transport = transport

    async def read(self, grant: EmployeeGrant) -> EmployeeRecord:
        credential = grant.credential.get_secret_value()
        if not credential.strip():
            raise BusinessError("upstream_unauthenticated")
        cfg = self.config
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
                "name": "employee.list.data.query.get",
                "ip": cfg.service_ip,
                "id": cfg.service_id,
            },
            "datakey": {"EntId": cfg.enterprise_id},
            "payload": {
                "std_data": {
                    "parameter": {
                        "is_digiwin_product": "N",
                        "use_has_next": False,
                        "employee_no": grant.employee_id,
                    }
                }
            },
        }
        try:
            with anyio.fail_after(REQUEST_BUDGET_SECONDS):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(REQUEST_BUDGET_SECONDS, connect=3.0),
                    trust_env=False,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    async with client.stream("POST", cfg.url, json=envelope) as response:
                        _check_status(response.status_code)
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise BusinessError("upstream_contract_error")
                            body.extend(chunk)
                return _employee_record(bytes(body), grant.employee_id)
        except (TimeoutError, httpx.TimeoutException):
            raise BusinessError("upstream_timeout") from None
        except httpx.DecodingError:
            raise BusinessError("upstream_contract_error") from None
        except httpx.RequestError:
            raise BusinessError("upstream_unavailable") from None
