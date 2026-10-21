"""显式的单进程装配与本地就绪检查；无真实身份默认值。"""

import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastmcp.server.auth import AuthProvider
from pydantic import ConfigDict, Field, PrivateAttr, field_validator, model_validator
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .access import (
    CREDENTIAL_MAX_BYTES,
    FileServiceAccountAccess,
    ServiceAccountPolicy,
    _private_bytes,
)
from .client import T100Config, T100EmployeeClient
from .journal import SQLiteQueryJournal
from .models import BusinessError, Model
from .server import IdentityProfile, create_app

_CONFIGURATION_MAX_BYTES = 1_000_000
_CONFIGURATION_ERROR = "runtime_configuration_invalid"
_SIDECARS = ("-journal", "-wal", "-shm")


def _absolute_path(path: Path) -> Path:
    if not path.is_absolute() or path.anchor != "/" or ".." in path.parts:
        raise ValueError("absolute paths without parent traversal required")
    return path


def _journal_files(path: Path) -> tuple[Path, ...]:
    return (path, *(path.with_name(path.name + suffix) for suffix in _SIDECARS))


def _separate_paths(config, source: Path | None = None) -> None:
    private = [config.policy_file, config.credential_file]
    if source is not None:
        private.append(_absolute_path(source))
    paths = [*private, *_journal_files(config.journal_path)]
    if len(set(paths)) != len(paths):
        raise ValueError("configuration, credentials and journal require separate paths")
    if any(left in right.parents for left in paths for right in paths if left != right):
        raise ValueError("a file path cannot be another file's directory")


class RuntimeConfig(Model):
    """非敏感的装配设置；JSON 中的路径必须显式给出，身份配置为必填。"""

    model_config = ConfigDict(hide_input_in_errors=True)

    identity: IdentityProfile
    t100: T100Config
    policy_file: Path
    credential_file: Path
    journal_path: Path
    budget_seconds: float = Field(default=10.0, gt=0, le=10)
    max_in_flight: int = Field(default=4, ge=1, le=32)
    _source_path: Path | None = PrivateAttr(default=None)

    @field_validator("policy_file", "credential_file", "journal_path")
    @classmethod
    def explicit_path(cls, value: Path) -> Path:
        return _absolute_path(value)

    @model_validator(mode="after")
    def nonconflicting_paths(self):
        _separate_paths(self)
        return self


def _validated(config: RuntimeConfig) -> RuntimeConfig:
    failed = False
    try:
        if not isinstance(config, RuntimeConfig):
            raise ValueError()
        # 从取值重建：model_copy/model_construct 会绕过模型校验。
        result = RuntimeConfig.model_validate_json(config.model_dump_json(warnings=False))
        source = config._source_path
        _separate_paths(result, source)
        result._source_path = source
    except Exception:
        failed = True
    if failed:
        raise BusinessError(_CONFIGURATION_ERROR)
    return result


def load_runtime_config(path: Path) -> RuntimeConfig:
    """读取一个私有的、有大小限制的配置文件；绝不读取服务凭据。"""
    failed = False
    try:
        source = _absolute_path(Path(path))
        raw = _private_bytes(source, _CONFIGURATION_MAX_BYTES)
        result = RuntimeConfig.model_validate_json(raw)
        _separate_paths(result, source)
        result._source_path = source
    except Exception:
        failed = True
    if failed:
        raise BusinessError(_CONFIGURATION_ERROR)
    return result


def _ancestors(path: Path) -> None:
    for parent in path.parents:
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError()


def _private_metadata(path: Path, *, maximum: int | None = None, exact_mode=None) -> None:
    _ancestors(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or (exact_mode is not None and stat.S_IMODE(info.st_mode) != exact_mode)
            or (maximum is not None and not 0 < info.st_size <= maximum)
        ):
            raise ValueError()
    finally:
        os.close(descriptor)


def _inspect_configuration(config: RuntimeConfig) -> None:
    policy = ServiceAccountPolicy.model_validate_json(
        _private_bytes(config.policy_file, _CONFIGURATION_MAX_BYTES)
    )
    for row in policy.principals:
        if row.issuer != config.identity.issuer or not set(row.allowed_clients).issubset(
            config.identity.allowed_clients
        ):
            raise ValueError()
    # 仅检查元数据，就绪探针也不例外：凭据内容只在 authorize() 中读取。
    _private_metadata(config.credential_file, maximum=CREDENTIAL_MAX_BYTES)
    if config._source_path is not None:
        _private_metadata(config._source_path, maximum=_CONFIGURATION_MAX_BYTES)
    _ancestors(config.journal_path)
    parent = config.journal_path.parent
    try:
        info = parent.lstat()
    except FileNotFoundError:
        pass  # 启动时的 prepare() 可能创建私有目录；此检查绝不创建。
    else:
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError()
    for path in _journal_files(config.journal_path):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        _private_metadata(path, exact_mode=0o600)


async def check_configuration(config: RuntimeConfig) -> dict[str, str]:
    """只读的本地检查；不读取密钥内容，不做授权，也不探测上游健康。"""
    checked = _validated(config)
    failed = False
    try:
        await anyio.to_thread.run_sync(_inspect_configuration, checked, abandon_on_cancel=False)
    except Exception:
        failed = True
    if failed:
        raise BusinessError(_CONFIGURATION_ERROR)
    return {"status": "configuration_valid"}


class _Admission:
    """仅对单个事件循环有效的准入控制；此上限要求只部署一个 worker。"""

    def __init__(self, app, maximum, ready):
        self.app = app
        self.maximum = maximum
        self.ready = ready
        self.accepting = False
        self.active = 0

    async def __call__(self, scope, receive, send):
        route_path = scope.get("path", "")
        root_path = scope.get("root_path", "")
        if root_path and route_path.startswith(root_path):
            remainder = route_path[len(root_path) :]
            if not remainder or remainder.startswith("/"):
                route_path = remainder
        if scope["type"] != "http" or route_path.rstrip("/") != "/mcp":
            await self.app(scope, receive, send)
            return
        if not self.accepting or self.active >= self.maximum:
            await JSONResponse({"error": "service_unavailable"}, status_code=503)(
                scope, receive, send
            )
            return
        self.active += 1
        try:
            if not await self.ready():
                await JSONResponse({"error": "service_unavailable"}, status_code=503)(
                    scope, receive, send
                )
                return
            await self.app(scope, receive, send)
        finally:
            self.active -= 1


def create_runtime_app(config: RuntimeConfig, *, auth: AuthProvider) -> Starlette:
    """无 I/O 地装配；监听、TLS 与优雅退出（draining）由宿主负责。"""
    checked = _validated(config)
    if not isinstance(auth, AuthProvider):
        raise BusinessError(_CONFIGURATION_ERROR)
    access = FileServiceAccountAccess(checked.policy_file, checked.credential_file)
    reader = T100EmployeeClient(checked.t100)
    journal = SQLiteQueryJournal(checked.journal_path, enterprise_id=checked.t100.enterprise_id)
    native = create_app(
        auth=auth,
        identity=checked.identity,
        access=access,
        reader=reader,
        journal=journal,
        budget_seconds=checked.budget_seconds,
    )

    async def local_ready():
        if not admission.accepting:
            return False
        try:
            await check_configuration(checked)
            await journal.check_ready()
        except Exception:
            return False
        return admission.accepting

    admission = _Admission(native, checked.max_in_flight, local_ready)

    @asynccontextmanager
    async def lifespan(app):
        failed = False
        try:
            await check_configuration(checked)
            await journal.prepare()
        except Exception:
            failed = True
        if failed:
            raise BusinessError(_CONFIGURATION_ERROR)
        try:
            async with native.router.lifespan_context(native):
                admission.accepting = True
                try:
                    yield
                finally:
                    admission.accepting = False
        except Exception:
            failed = True
        if failed:
            raise BusinessError(_CONFIGURATION_ERROR)

    async def live(request):
        return JSONResponse({"status": "live"})

    async def ready(request):
        available = await local_ready()
        return JSONResponse(
            {"status": "ready" if available else "not_ready", "scope": "local"},
            status_code=200 if available else 503,
        )

    return Starlette(
        routes=[
            Route("/health/live", live, methods=["GET"]),
            Route("/health/ready", ready, methods=["GET"]),
            Mount("/", app=admission),
        ],
        lifespan=lifespan,
    )
