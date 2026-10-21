"""四个文档接口的单次有界调用；授权、重试决策与业务闭环由调用方管理。"""

import json
import re
from collections.abc import Callable
from datetime import datetime

import anyio
import httpx
from pydantic import SecretStr, ValidationError

from .client import MAX_RESPONSE_BYTES, REQUEST_BUDGET_SECONDS, T100Config, _check_status
from .interfaces import REQUEST_MODELS, SERVICES, EmployeeFields, Parameters
from .models import BusinessError

MAX_REQUEST_BYTES = 2_000_000
MAX_QUERY_ROWS = 10_000
_INVALID_EMPLOYEE_FIELDS = frozenset((*EmployeeFields.model_fields, "create_date", "modify_date"))
DiagnosticValue = str | int | list[str]


class InterfaceUpstreamError(BusinessError):
    """固定错误码加有界代码诊断；不保存请求、凭据或上游自由文本。"""

    def __init__(
        self,
        code: str,
        diagnostics: dict[str, DiagnosticValue],
        *,
        write_rejected: bool = False,
    ):
        super().__init__(code)
        self.diagnostics = dict(diagnostics)
        # 仅供操作库判定终态；不把上游自由文本或回执复制到异常对象。
        self.write_rejected = write_rejected is True and code == "upstream_rejected"


def _diagnostics(std: dict, credential: str) -> dict[str, DiagnosticValue]:
    execution = std.get("execution")
    if type(execution) is not dict:
        return {}
    result = {}
    for key, target in (("code", "execution_code"), ("sql_code", "sql_code")):
        value = execution.get(key)
        if type(value) not in {str, int}:
            continue
        text = str(value)
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", text) and credential not in text:
            result[target] = value
    parameter = std.get("parameter")
    rows = parameter.get("employee") if type(parameter) is dict else None
    invalid_fields = set()
    suffix = "输入的资料无效！"
    if type(rows) is list:
        for row in rows:
            description = row.get("execute_description") if type(row) is dict else None
            if (
                type(description) is str
                and description.endswith(suffix)
                and credential not in description
            ):
                # 只接受完整固定模板，绝不输出原描述、目标或未知字段。
                field = description[: -len(suffix)]
                if field in _INVALID_EMPLOYEE_FIELDS:
                    invalid_fields.add(field)
    if invalid_fields:
        result["invalid_fields"] = sorted(invalid_fields)
    return result


def _write_response_observation(std: dict, credential: str) -> dict:
    """私有写回执观察端口：只提取有界字段，不含请求或查询员工资料。"""

    def fields(source, names):
        if type(source) is not dict:
            return {}
        result = {}
        for name in names:
            value = source.get(name)
            if type(value) not in {str, int}:
                continue
            text = str(value)
            if credential in text:
                # 整个值遮蔽，避免截断或拼接留下凭据片段。
                result[name] = "[redacted]" if credential not in "[redacted]" else ""
            else:
                result[name] = text[:2048]
        return result

    parameter = std.get("parameter")
    rows = parameter.get("employee") if type(parameter) is dict else None
    return {
        "execution": fields(std.get("execution"), ("code", "sql_code", "description")),
        "employee": [
            fields(row, ("source_no", "erp_no", "execute_description"))
            for row in rows[:100]
            if type(row) is dict
        ]
        if type(rows) is list
        else [],
    }


def _employee_query(parameter: dict) -> dict:
    employees = parameter.get("employee")
    if type(employees) is not list or len(employees) > MAX_QUERY_ROWS:
        raise ValueError()
    selected = []
    fields = EmployeeFields.model_fields
    for row in employees:
        if type(row) is not dict:
            raise ValueError()
        # 白名单字段返回；查询可多笔、空结果，不承担“本人”授权判断。
        known = {key: value for key, value in row.items() if key in fields}
        # 上游可返回未填资料的 null；不能让更新入参的省略约束误用为响应约束。
        known = {key: value for key, value in known.items() if value is not None}
        parsed = EmployeeFields.model_validate(known)
        record = parsed.model_dump(mode="json", exclude_unset=True)
        if "create_date" in row:
            value = row["create_date"]
            if value is not None:
                if type(value) is not str or len(value) > 32:
                    raise ValueError()
                record["create_date"] = value
        selected.append(record)
    result = {"employee": selected}
    for key in ("data_time", "key_condition", "total_results", "has_next"):
        if key not in parameter:
            continue
        value = parameter[key]
        if key == "has_next":
            if type(value) is not bool:
                raise ValueError()
        elif key == "total_results":
            if type(value) is not int or value < 0:
                raise ValueError()
        elif type(value) is not str or len(value) > (32 if key == "data_time" else 2048):
            raise ValueError()
        result[key] = value
    return result


def _all_employee_receipts_rejected(parameter: dict, request: Parameters) -> bool:
    """完整逐笔失败回执才证明本批未写入；混合、缺失和含糊回执均不推断。"""
    rows = parameter.get("employee")
    targets = {item.employee_no for item in request.employee}
    if type(rows) is not list or len(rows) != len(targets):
        return False
    seen = set()
    for row in rows:
        if type(row) is not dict:
            return False
        source, erp, description = (
            row.get(key) for key in ("source_no", "erp_no", "execute_description")
        )
        if (
            type(source) is not str
            or source not in targets
            or source in seen
            or type(erp) is not str
            or erp.strip() != ""
            or type(description) is not str
            or description.strip() == ""
        ):
            return False
        seen.add(source)
    return seen == targets


def _employee_receipts(parameter: dict, request: Parameters) -> dict:
    rows = parameter.get("employee")
    targets = {item.employee_no for item in request.employee}
    if type(rows) is not list or len(rows) != len(targets):
        raise ValueError()
    seen = set()
    selected = []
    rejected = False
    for row in rows:
        if type(row) is not dict:
            raise ValueError()
        source, erp, description = (
            row.get(key) for key in ("source_no", "erp_no", "execute_description")
        )
        if (
            type(source) is not str
            or source not in targets
            or source in seen
            or type(erp) is not str
            or type(description) is not str
        ):
            raise ValueError()
        seen.add(source)
        # 当前上游的成功回执使用空白字符串；只规范化描述，绝不裁剪编号。
        if erp == "" or description.strip() != "":
            rejected = True
        elif erp != source:
            raise ValueError()
        selected.append({"source_no": source, "erp_no": erp, "execute_description": ""})
    if rejected:
        # 任一项失败不返回整批成功；不声称其它项回滚，不自动重试。
        raise BusinessError("upstream_rejected")
    return {"employee": selected, "acknowledged": True, "business_effect_verified": False}


class T100InterfaceClient:
    def __init__(
        self,
        config: T100Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        on_write_response: Callable[[dict], None] | None = None,
    ):
        self.config = T100Config.model_validate(config.model_dump(warnings="error"))
        self._transport = transport
        self._on_write_response = on_write_response

    async def execute(
        self,
        action: str,
        request: Parameters,
        credential: SecretStr,
        *,
        on_request: Callable[[], None] | None = None,
    ) -> dict:
        try:
            model = REQUEST_MODELS[action]
            if type(request) is not model or type(credential) is not SecretStr:
                raise ValueError()
            # 冻结模型及嵌套集合仍可能被 model_copy 或外部引用绕过，调用前重新验收。
            request = model.model_validate(request.parameter())
            cfg = T100Config.model_validate(self.config.model_dump(warnings="error"))
            parameter = request.parameter()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise BusinessError("invalid_request") from None
        if parameter.get("enterprise_no", cfg.enterprise_id) != cfg.enterprise_id:
            raise BusinessError("enterprise_mismatch")
        secret = credential.get_secret_value()
        if not secret.strip():
            raise BusinessError("upstream_unauthenticated")
        envelope = {
            "key": secret,
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
                "name": SERVICES[action],
                "ip": cfg.service_ip,
                "id": cfg.service_id,
            },
            "datakey": {"EntId": cfg.enterprise_id},
            "payload": {"std_data": {"parameter": parameter}},
        }
        encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > MAX_REQUEST_BYTES:
            raise BusinessError("invalid_request")
        diagnostics = {}
        write_rejected = False
        try:
            with anyio.fail_after(REQUEST_BUDGET_SECONDS):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(REQUEST_BUDGET_SECONDS, connect=3.0),
                    trust_env=False,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    if on_request is not None:
                        on_request()
                    async with client.stream(
                        "POST",
                        cfg.url,
                        content=encoded,
                        headers={"Content-Type": "application/json"},
                    ) as response:
                        diagnostics["http_status"] = response.status_code
                        _check_status(response.status_code)
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise BusinessError("upstream_contract_error")
                            body.extend(chunk)
                try:
                    std = json.loads(body)["payload"]["std_data"]
                    if type(std) is not dict:
                        raise ValueError()
                    diagnostics.update(_diagnostics(std, secret))
                    if action != "employee_query" and self._on_write_response is not None:
                        observation = _write_response_observation(std, secret)
                        try:
                            self._on_write_response(observation)
                        except Exception:
                            raise BusinessError("journal_unavailable") from None
                    code = std["execution"]["code"]
                    if type(code) not in {str, int}:
                        raise ValueError()
                    # 未知业务码本身不说明是否执行；只有符合文档的完整失败
                    # 回执才证明本批全拒绝。观察端口已成功后才能形成此证据。
                    if (
                        action in {"employee_create", "employee_update"}
                        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", str(code))
                        and type(std.get("parameter")) is dict
                    ):
                        write_rejected = _all_employee_receipts_rejected(std["parameter"], request)
                    if code not in ("0", 0):
                        raise BusinessError("upstream_rejected")
                    if action == "account_maintain":
                        return {"acknowledged": True, "business_effect_verified": False}
                    output = std["parameter"]
                    if type(output) is not dict:
                        raise ValueError()
                    if action == "employee_query":
                        return _employee_query(output)
                    return _employee_receipts(output, request)
                except (ValueError, KeyError, TypeError, RecursionError, ValidationError):
                    raise BusinessError("upstream_contract_error") from None
        except (TimeoutError, httpx.TimeoutException):
            raise InterfaceUpstreamError("upstream_timeout", diagnostics) from None
        except httpx.DecodingError:
            raise InterfaceUpstreamError("upstream_contract_error", diagnostics) from None
        except httpx.RequestError:
            raise InterfaceUpstreamError("upstream_unavailable", diagnostics) from None
        except BusinessError as exc:
            raise InterfaceUpstreamError(
                exc.code, diagnostics, write_rejected=write_rejected
            ) from None
