"""显式的服务账号执行方式，配合独立的、仅限本人的调用者策略。

上游服务以其管理员权限运行（ADR-003）。这不会把管理员权限授予 MCP 调用者，
也不继承调用者的 ERP 原生 ACL。只有运维方控制的策略才能启用调用者本人的
查询或密码重置。读权限不隐含重置权限；重置要求显式的账号绑定。
"""

import hashlib
import os
import stat
from pathlib import Path
from typing import Annotated, Literal

import anyio
from pydantic import Field, SecretStr, model_validator

from .models import BusinessError, Caller, EmployeeGrant, Identifier, Model, PasswordResetGrant

BoundedName = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]
POLICY_MAX_BYTES = 1_000_000
CREDENTIAL_MAX_BYTES = 16_384


class ServicePrincipal(Model):
    issuer: str = Field(min_length=1, max_length=512, pattern=r"\S")
    subject: BoundedName
    employee_id: Identifier
    allowed_clients: list[BoundedName] = Field(min_length=1)
    enabled: bool = False
    allow_employee_read: bool = False
    allow_password_reset: bool = False
    t100_account_id: Identifier | None = None


class ServiceAccountPolicy(Model):
    # 无默认值：选择此授权模型必须通过显式配置。
    auth_mode: Literal["service-account"]
    revision: BoundedName
    principals: list[ServicePrincipal]

    @model_validator(mode="after")
    def unique_subjects(self):
        identities = [(row.issuer, row.subject) for row in self.principals]
        if len(identities) != len(set(identities)):
            raise ValueError("each issuer and subject must have one policy entry")
        accounts = {}
        for row in self.principals:
            if row.t100_account_id is not None:
                previous = accounts.setdefault(row.t100_account_id, row.employee_id)
                if previous != row.employee_id:
                    raise ValueError("one T100 account cannot belong to different employees")
        return self


def _private_bytes(path: Path, maximum: int) -> bytes:
    # 配置的路径由运维方控制，绝不来自工具调用者。
    if not path.is_absolute() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("an explicit non-symlinked private path is required")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise ValueError("private regular file required")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            value = source.read(maximum + 1)
        if len(value) > maximum:
            raise ValueError("private file exceeds limit")
        return value
    finally:
        os.close(descriptor)


class FileServiceAccountAccess:
    """运维方策略加专用服务密钥；无默认用户，无回退机制。

    构造时不访问文件系统。变更授权需原子替换配置文件；每次授权都重新读取。
    访问令牌的校验与吊销语义仍由 AuthProvider 负责。
    """

    def __init__(
        self,
        policy_file: Path,
        credential_file: Path,
        *,
        permission: Literal["employee_read", "password_reset"] = "employee_read",
    ):
        if permission not in {"employee_read", "password_reset"}:
            raise ValueError("explicit supported permission required")
        self._permission = permission
        self._policy_file = Path(policy_file)
        self._credential_file = Path(credential_file)
        if not self._policy_file.is_absolute() or not self._credential_file.is_absolute():
            raise ValueError("explicit absolute paths are required")
        if self._policy_file == self._credential_file:
            raise ValueError("policy and credential must be separate files")

    def _authorize(self, caller: Caller) -> EmployeeGrant:
        try:
            raw = _private_bytes(self._policy_file, POLICY_MAX_BYTES)
            policy = ServiceAccountPolicy.model_validate_json(raw)
        except (OSError, ValueError):
            raise BusinessError("policy_unavailable") from None

        principal = next(
            (
                row
                for row in policy.principals
                if (row.issuer, row.subject) == (caller.issuer, caller.subject)
            ),
            None,
        )
        if principal is None or not principal.enabled:
            raise BusinessError("identity_not_enabled")
        allowed = (
            principal.allow_employee_read
            if self._permission == "employee_read"
            else principal.allow_password_reset
        )
        if caller.client_id not in principal.allowed_clients or not allowed:
            raise BusinessError("permission_denied")
        if self._permission == "password_reset" and principal.t100_account_id is None:
            raise BusinessError("account_not_linked")

        # 密钥可用不代表调用者已获授权：必须在策略放行后才读取。
        try:
            value = _private_bytes(self._credential_file, CREDENTIAL_MAX_BYTES).decode().strip()
            if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("invalid service credential")
        except (OSError, ValueError):
            raise BusinessError("credential_unavailable") from None
        values = dict(
            employee_id=principal.employee_id,
            binding_version=hashlib.sha256(raw).hexdigest(),
            credential=SecretStr(value),
        )
        if self._permission == "password_reset":
            return PasswordResetGrant(account_id=principal.t100_account_id, **values)
        return EmployeeGrant(**values)

    async def authorize(self, caller: Caller) -> EmployeeGrant:
        return await anyio.to_thread.run_sync(self._authorize, caller, abandon_on_cancel=False)
