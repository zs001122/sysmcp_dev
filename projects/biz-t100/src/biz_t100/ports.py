"""显式的集成边界；不隐式连接任何旧环境。"""

from typing import Literal, Protocol

from .models import Caller, EmployeeGrant, EmployeeRecord

Event = Literal["query_started", "query_succeeded", "query_failed", "query_cancelled"]


class EmployeeAccess(Protocol):
    async def authorize(self, caller: Caller) -> EmployeeGrant:
        """重新核对有效调用者、员工映射和显式的执行策略。

        T100 在调用者授权通过后使用服务凭据（ADR-003）。
        拒绝缺失或已撤销的授权与凭据；绝不根据服务账号的管理员权限
        推导调用者权限。服务端在持久化准入之后和查询之后会再次调用本方法。
        """
        ...


class EmployeeReader(Protocol):
    async def read(self, grant: EmployeeGrant) -> EmployeeRecord: ...


class QueryJournal(Protocol):
    async def record(
        self,
        *,
        request_id: str,
        caller: Caller,
        employee_id: str | None,
        event: Event,
        code: str | None = None,
        upstream_attempted: bool = False,
    ) -> None:
        """在持久化提交后返回；绝不记录凭据或业务报文。

        upstream_attempted 表示用例已进入 EmployeeReader.read，
        不代表远端系统已收到或完成请求。开始时为 False，成功时为 True，
        失败或取消时保留已观察到的取值。
        """
        ...
