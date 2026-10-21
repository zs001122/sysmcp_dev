"""离线候选的密码重置契约，按 RU-003 选择性适配。

旧服务是 upsert（有则更新、无则插入）接口，且文档要求 status 必填。
它对这种仅密码补丁的支持、以及无账号/权限副作用这两点仍未验证。
原候选类有意不提供默认网络传输；共用发送函数由明确的调用方装配。
"""

import json
from datetime import datetime
from typing import Literal

import anyio
import httpx
from pydantic import ValidationError

from .client import MAX_RESPONSE_BYTES, REQUEST_BUDGET_SECONDS, T100Config
from .models import BusinessError, Model, PasswordResetGrant


class ResetReceipt(Model):
    """上游的应答确认（ACK），不代表密码已交付或登录已生效。"""

    acknowledged: Literal[True] = True


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


def _receipt(body: bytes) -> ResetReceipt:
    try:
        std = json.loads(body)["payload"]["std_data"]
        code = std["execution"]["code"]
        if type(code) not in (str, int):
            raise ValueError("invalid execution code type")
        if code not in ("0", 0):
            raise BusinessError("upstream_rejected")
        # 旧代码中没有重置响应目标结构的证据。
        # 只接受纯粹的 ACK；未知、错误或多条记录都不算 ACK。
        if "parameter" in std and (type(std["parameter"]) is not dict or std["parameter"]):
            raise ValueError("only an acknowledgement is supported")
        return ResetReceipt()
    except (ValueError, KeyError, TypeError, RecursionError):
        raise BusinessError("upstream_contract_error") from None


class T100PasswordResetClient:
    """使用显式提供的离线测试传输进行一次有边界的尝试。

    派发后未成功并不能证明没有发生写入。除非有文档化的拒绝证据，
    写用例必须保留结果未知状态；本客户端既不重试，也不伪造目标回显。
    """

    def __init__(
        self, config: T100Config, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if transport is None:
            raise BusinessError("business_adapter_not_ready")
        self.config = T100Config.model_validate(config.model_dump())
        self._transport = transport

    async def reset(self, grant: PasswordResetGrant) -> ResetReceipt:
        if not isinstance(grant, PasswordResetGrant):
            raise BusinessError("upstream_contract_error")
        try:
            # model_copy/model_construct 即使在冻结模型上也能绕过校验。
            grant = PasswordResetGrant(
                employee_id=grant.employee_id,
                account_id=grant.account_id,
                binding_version=grant.binding_version,
                credential=grant.credential,
            )
        except (ValidationError, AttributeError):
            raise BusinessError("upstream_contract_error") from None
        credential = grant.credential.get_secret_value()
        if not credential.strip():
            raise BusinessError("upstream_unauthenticated")
        return await _send_master(
            self.config,
            {
                "gzxa001": grant.account_id,
                "gzxa003": grant.employee_id,
                "password_reset": "Y",
            },
            credential=credential,
            transport=self._transport,
        )


async def _send_master(
    cfg: T100Config,
    master: dict[str, str],
    *,
    credential: str,
    transport: httpx.AsyncBaseTransport | None,
) -> ResetReceipt:
    """仅发送已校验的单账号报文；调用方负责动作契约与派发许可。"""
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
            "name": "user.account.create",
            "ip": cfg.service_ip,
            "id": cfg.service_id,
        },
        "datakey": {"EntId": cfg.enterprise_id},
        "payload": {"std_data": {"parameter": {"master": [master]}}},
    }
    try:
        with anyio.fail_after(REQUEST_BUDGET_SECONDS):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(REQUEST_BUDGET_SECONDS, connect=3.0),
                trust_env=False,
                follow_redirects=False,
                transport=transport,
            ) as client:
                async with client.stream("POST", cfg.url, json=envelope) as response:
                    _check_status(response.status_code)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise BusinessError("upstream_contract_error")
                        body.extend(chunk)
            return _receipt(bytes(body))
    except (TimeoutError, httpx.TimeoutException):
        raise BusinessError("upstream_timeout") from None
    except httpx.DecodingError:
        raise BusinessError("upstream_contract_error") from None
    except httpx.RequestError:
        raise BusinessError("upstream_unavailable") from None
