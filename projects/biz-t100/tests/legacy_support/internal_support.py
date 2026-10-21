# Historical regression support relocated from scripts; not a supported command.
"""内部实验环境的私有合成身份状态；绝不包含进 wheel 制品。

服务方调用只加载公开的验签材料与凭据摘要。只有 issue_token 读取签名密钥。
任何端点、企业或主体都不能通过本辅助模块接入真实业务系统。
"""

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from pydantic import SecretStr

from biz_t100.access import ServiceAccountPolicy
from biz_t100.client import T100Config
from biz_t100.models import BusinessError
from biz_t100.reset_server import RESET_SCOPE
from biz_t100.server import SCOPE, IdentityProfile

Service = Literal["read", "reset"]
ISSUER = "https://internal-lab.example.invalid"
USERS = frozenset({"user-a", "user-b", "disabled", "unmapped"})
_TARGETS = {"user-a": ("E001", "LOGIN-A"), "user-b": ("E002", "LOGIN-B")}
_SERVICES = {
    "read": {
        "audience": "internal-t100-read",
        "client_id": "internal-read-client",
        "enterprise_id": "INTERNAL-READ",
        "url": "https://internal-read.example.invalid/api",
    },
    "reset": {
        "audience": "internal-t100-reset",
        "client_id": "internal-reset-client",
        "enterprise_id": "INTERNAL-RESET",
        "url": "https://internal-reset.example.invalid/api",
    },
}
_STATE_ERROR = "internal_state_invalid"
_TOKEN_ERROR = "internal_token_invalid"
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _service(value: Service) -> dict[str, str]:
    if type(value) is not str or value not in _SERVICES:
        raise BusinessError(_STATE_ERROR)
    return _SERVICES[value]


def _absolute(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.anchor != "/" or ".." in path.parts:
        raise ValueError()
    if path == Path("/"):
        raise ValueError()
    return path


def _directory(path: Path, *, private: bool = True) -> int:
    """使用目录描述符逐级打开；任何祖先符号链接都不会被跟随。"""
    if not path.is_absolute() or path.anchor != "/" or ".." in path.parts:
        raise ValueError()
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError()
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _regular(info, maximum: int, *, allow_empty: bool = False) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_size > maximum
        or (not allow_empty and info.st_size == 0)
    ):
        raise ValueError()


def _read_private(path: Path, maximum: int) -> bytes:
    parent = _directory(path.parent)
    descriptor = None
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        _regular(os.fstat(descriptor), maximum)
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            data = source.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError()
        return data
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _metadata(path: Path, maximum: int, *, optional: bool = False) -> None:
    parent = _directory(path.parent)
    descriptor = None
    try:
        try:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        except FileNotFoundError:
            if optional:
                return
            raise
        _regular(os.fstat(descriptor), maximum, allow_empty=optional)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _write_new(path: Path, content: bytes) -> None:
    parent = _directory(path.parent)
    descriptor = None
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as destination:
            destination.write(content)
            destination.flush()
            os.fsync(descriptor)
        os.fsync(parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _mkdir(path: Path, *, parent_private: bool = True) -> bool:
    parent = _directory(path.parent, private=parent_private)
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent)
        except FileExistsError:
            return False
        os.fsync(parent)
    finally:
        os.close(parent)
    descriptor = _directory(path)
    os.close(descriptor)
    return True


def _policy(service: Service) -> dict:
    info = _service(service)
    rows = []
    for subject, (employee, account) in {
        **_TARGETS,
        "disabled": ("E003", "LOGIN-DISABLED"),
    }.items():
        enabled = subject != "disabled"
        rows.append(
            {
                "issuer": ISSUER,
                "subject": subject,
                "employee_id": employee,
                "t100_account_id": account if service == "reset" else None,
                "allowed_clients": [info["client_id"]],
                "enabled": enabled,
                "allow_employee_read": enabled and service == "read",
                "allow_password_reset": enabled and service == "reset",
            }
        )
    return {"auth_mode": "service-account", "revision": "internal-lab-v1", "principals": rows}


def _check_policy(path: Path, service: Service) -> None:
    policy = ServiceAccountPolicy.model_validate_json(_read_private(path, 1_000_000))
    actual = {row.subject: row.model_dump() for row in policy.principals}
    expected = {row["subject"]: row for row in _policy(service)["principals"]}
    if set(actual) != set(expected):
        raise ValueError()
    mutable = {"enabled", "allow_employee_read" if service == "read" else "allow_password_reset"}
    for subject, row in actual.items():
        wanted = expected[subject]
        if subject == "disabled":
            if row != wanted:
                raise ValueError()
        elif any(row[key] != value for key, value in wanted.items() if key not in mutable):
            raise ValueError()


@dataclass(frozen=True)
class LabState:
    root: Path
    public_key: str = field(repr=False)
    _digests: tuple[str, str] = field(repr=False)

    @property
    def issuer(self) -> str:
        return ISSUER

    @property
    def publickey_file(self) -> Path:
        return self.root / "issuer-public.pem"

    def client_id(self, service: Service) -> str:
        return _service(service)["client_id"]

    def identity_profile(self, service: Service) -> IdentityProfile:
        info = _service(service)
        return IdentityProfile(
            issuer=ISSUER,
            audience=info["audience"],
            allowed_clients=frozenset({info["client_id"]}),
        )

    def policy_path(self, service: Service) -> Path:
        _service(service)
        return self.root / service / "access.json"

    def credential_path(self, service: Service) -> Path:
        _service(service)
        return self.root / service / "service.key"

    def journal_path(self, service: Service) -> Path:
        _service(service)
        filename = "query.sqlite3" if service == "read" else "reset.sqlite3"
        return self.root / service / "audit" / filename

    def capture_path(self, service: Service) -> Path:
        _service(service)
        return self.root / service / "upstream.jsonl"

    def credential_digest(self, service: Service) -> str:
        _service(service)
        return self._digests[0 if service == "read" else 1]

    def t100_config(self, service: Service) -> T100Config:
        info = _service(service)
        return T100Config(
            url=info["url"],
            enterprise_id=info["enterprise_id"],
            host_product="INTERNAL-LAB",
            host_ip="127.0.0.1",
            host_account=f"internal-{service}-service",
            service_ip="127.0.0.1",
            service_id=f"internal-{service}",
        )


def _load_state(root: Path) -> LabState:
    root = _absolute(root)
    descriptor = _directory(root)
    os.close(descriptor)
    manifest = json.loads(_read_private(root / "state.json", 16_384))
    if type(manifest) is not dict or type(manifest.get("schema_version")) is not int:
        raise ValueError()
    public = _read_private(root / "issuer-public.pem", 16_384)
    public_key = serialization.load_pem_public_key(public)
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size != 2048:
        raise ValueError()
    canonical_public = public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if public != canonical_public:
        raise ValueError()
    digest_values = []
    expected_services = {}
    for service in ("read", "reset"):
        digest = manifest["services"][service]["credential_sha256"]
        if type(digest) is not str or not _HEX_DIGEST.fullmatch(digest):
            raise ValueError()
        digest_values.append(digest)
        expected_services[service] = {**_service(service), "credential_sha256": digest}
    if digest_values[0] == digest_values[1]:
        raise ValueError()
    expected = {
        "schema_version": 1,
        "mode": "internal-lab",
        "issuer": ISSUER,
        "public_key_sha256": hashlib.sha256(public).hexdigest(),
        "services": expected_services,
    }
    if manifest != expected:
        raise ValueError()
    state = LabState(root, public.decode("ascii"), tuple(digest_values))
    # 检查签名密钥的元数据，但服务路径绝不读取其内容。
    _metadata(root / "issuer-private.pem", 16_384)
    for directory in (root / "tokens", root / "read", root / "reset"):
        descriptor = _directory(directory)
        os.close(descriptor)
    for service in ("read", "reset"):
        _check_policy(state.policy_path(service), service)
        _metadata(state.credential_path(service), 16_384)
        journal = state.journal_path(service)
        descriptor = _directory(journal.parent)
        os.close(descriptor)
        for suffix in ("", "-journal", "-wal", "-shm"):
            _metadata(journal.with_name(journal.name + suffix), 1 << 40, optional=True)
        _metadata(state.capture_path(service), 1 << 40, optional=True)
    return state


def load_state(root: Path) -> LabState:
    """校验固定的实验环境状态，不读取签名/服务秘密内容。"""
    try:
        return _load_state(root)
    except Exception:
        raise BusinessError(_STATE_ERROR) from None


def init_state(root: Path) -> dict:
    """初始化缺失的私有根目录，或仅校验已有状态而不做修改。"""
    try:
        root = _absolute(root)
        created = _mkdir(root, parent_private=False)
        if not created:
            state = _load_state(root)
            return {
                "status": "already_initialized",
                "mode": "internal-simulated",
                "root": str(state.root),
            }
        for directory in (root / "read", root / "reset", root / "tokens"):
            _mkdir(directory)
        for service in ("read", "reset"):
            _mkdir(root / service / "audit")
        pair = RSAKeyPair.generate()
        _write_new(root / "issuer-public.pem", pair.public_key.encode("ascii"))
        _write_new(root / "issuer-private.pem", pair.private_key.get_secret_value().encode("ascii"))
        services = {}
        for service in ("read", "reset"):
            credential = f"internal-lab-{service}-{secrets.token_hex(32)}".encode("ascii")
            _write_new(root / service / "service.key", credential)
            _write_new(root / service / "access.json", json.dumps(_policy(service)).encode())
            services[service] = {
                **_service(service),
                "credential_sha256": hashlib.sha256(credential).hexdigest(),
            }
        manifest = {
            "schema_version": 1,
            "mode": "internal-lab",
            "issuer": ISSUER,
            "public_key_sha256": hashlib.sha256(pair.public_key.encode("ascii")).hexdigest(),
            "services": services,
        }
        # 标记最后写入：部分失败的初始化绝不会被误认为有效状态，
        # 也不会通过覆盖已有材料来"修复"。
        _write_new(root / "state.json", json.dumps(manifest).encode())
        _load_state(root)
        return {"status": "initialized", "mode": "internal-simulated", "root": str(root)}
    except Exception:
        raise BusinessError(_STATE_ERROR) from None


def issue_token(
    root: Path, *, user: str, service: Service, output: Path, ttl_seconds: int = 300
) -> dict:
    """把一个有限生命周期的合成令牌写入独占的私有文件。"""
    try:
        if type(user) is not str or user not in USERS:
            raise ValueError()
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise ValueError()
        info = _service(service)
        state = _load_state(root)
        output = _absolute(output)
        if output == state.root or state.root in output.parents:
            if output.parent != state.root / "tokens":
                raise ValueError()
        parent = _directory(output.parent)
        os.close(parent)
        private = _read_private(state.root / "issuer-private.pem", 16_384)
        key = serialization.load_pem_private_key(private, password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048:
            raise ValueError()
        public = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode("ascii")
        )
        if public != state.public_key:
            raise ValueError()
        pair = RSAKeyPair(private_key=SecretStr(private.decode("ascii")), public_key=public)
        token = pair.create_token(
            subject=user,
            issuer=ISSUER,
            audience=info["audience"],
            scopes=[SCOPE if service == "read" else RESET_SCOPE],
            expires_in_seconds=ttl_seconds,
            additional_claims={"azp": info["client_id"]},
        )
        _write_new(output, (token + "\n").encode("ascii"))
        return {
            "status": "issued",
            "mode": "internal-simulated",
            "user": user,
            "service": service,
            "output": str(output),
            "ttl_seconds": ttl_seconds,
        }
    except Exception:
        raise BusinessError(_TOKEN_ERROR) from None
