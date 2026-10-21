# Historical regression support relocated from scripts; not a supported command.
"""私有的、经运维方批准的员工装置数据，配合模拟的只读身份。

员工数据在运行时提供，绝不嵌入本模块。本辅助模块不能选择真实上游，
也不能签发写令牌。产品模块不导入开发脚本。
"""

import hashlib
import json
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from internal_support import (
    _absolute,
    _directory,
    _metadata,
    _mkdir,
    _read_private,
    _write_new,
)
from pydantic import Field, SecretStr, model_validator

from biz_t100.access import ServiceAccountPolicy
from biz_t100.client import T100Config
from biz_t100.models import BusinessError, Identifier, Model
from biz_t100.server import SCOPE, IdentityProfile

MODE = "approved-employee-simulated"
ISSUER = "https://approved-read-case.example.invalid"
AUDIENCE = "approved-t100-read"
CLIENT_ID = "approved-read-client"
SUBJECT = "simulated-approved-user"
USER = SUBJECT
USERS = frozenset({SUBJECT, "unmapped"})
_URL = "https://internal-read.example.invalid/api"
_ENTERPRISE = "INTERNAL-READ"
_STATE_ERROR = "internal_case_state_invalid"
_INPUT_ERROR = "internal_case_input_invalid"
_TOKEN_ERROR = "internal_case_token_invalid"
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class _Fixture(Model):
    employee_no: Identifier
    display_name: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def display_only(self):
        if not self.display_name.strip() or any(
            unicodedata.category(char).startswith("C") for char in self.display_name
        ):
            raise ValueError()
        return self


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique_object)


def _fixture(raw):
    return _Fixture.model_validate(_json(raw))


def _read(service):
    if type(service) is not str or service != "read":
        raise BusinessError(_STATE_ERROR)


def _policy(employee_no):
    return {
        "auth_mode": "service-account",
        "revision": "approved-read-case-v1",
        "principals": [
            {
                "issuer": ISSUER,
                "subject": SUBJECT,
                "employee_id": employee_no,
                "t100_account_id": None,
                "allowed_clients": [CLIENT_ID],
                "enabled": True,
                "allow_employee_read": True,
                "allow_password_reset": False,
            }
        ],
    }


def _check_policy(path, employee_no):
    policy = ServiceAccountPolicy.model_validate(_json(_read_private(path, 1_000_000)))
    if len(policy.principals) != 1:
        raise ValueError()
    actual = policy.principals[0].model_dump()
    expected = _policy(employee_no)["principals"][0]
    mutable = {"enabled", "allow_employee_read"}
    if any(actual[key] != value for key, value in expected.items() if key not in mutable):
        raise ValueError()


@dataclass(frozen=True)
class EmployeeCaseState:
    root: Path
    public_key: str = field(repr=False)
    employee_no: str = field(repr=False)
    display_name: str = field(repr=False)
    case_digest: str = field(repr=False)
    _credential_digest: str = field(repr=False)

    @property
    def mode(self):
        return MODE

    @property
    def issuer(self):
        return ISSUER

    @property
    def publickey_file(self):
        return self.root / "issuer-public.pem"

    def identity_profile(self, service):
        _read(service)
        return IdentityProfile(
            issuer=ISSUER, audience=AUDIENCE, allowed_clients=frozenset({CLIENT_ID})
        )

    def client_id(self, service):
        _read(service)
        return CLIENT_ID

    def policy_path(self, service):
        _read(service)
        return self.root / "read" / "access.json"

    def credential_path(self, service):
        _read(service)
        return self.root / "read" / "service.key"

    def journal_path(self, service):
        _read(service)
        return self.root / "read" / "audit" / "query.sqlite3"

    def capture_path(self, service):
        _read(service)
        return self.root / "read" / "upstream.jsonl"

    def credential_digest(self, service):
        _read(service)
        return self._credential_digest

    def t100_config(self, service):
        _read(service)
        return T100Config(
            url=_URL,
            enterprise_id=_ENTERPRISE,
            host_product="APPROVED-READ-CASE",
            host_ip="127.0.0.1",
            host_account="approved-read-service",
            service_ip="127.0.0.1",
            service_id="approved-read",
        )


def _load_case(root):
    root = _absolute(root)
    descriptor = _directory(root)
    os.close(descriptor)
    if (root / "reset").exists() or (root / "reset").is_symlink():
        raise ValueError()
    manifest = _json(_read_private(root / "state.json", 16_384))
    if type(manifest) is not dict or type(manifest.get("schema_version")) is not int:
        raise ValueError()
    raw_fixture = _read_private(root / "fixture.json", 16_384)
    fixture = _fixture(raw_fixture)
    case_digest = hashlib.sha256(raw_fixture).hexdigest()
    public = _read_private(root / "issuer-public.pem", 16_384)
    key = serialization.load_pem_public_key(public)
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 2048:
        raise ValueError()
    canonical = key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if public != canonical:
        raise ValueError()
    digest = manifest["services"]["read"]["credential_sha256"]
    if type(digest) is not str or not _DIGEST.fullmatch(digest):
        raise ValueError()
    expected = {
        "schema_version": 1,
        "mode": "approved-read-case",
        "issuer": ISSUER,
        "public_key_sha256": hashlib.sha256(public).hexdigest(),
        "fixture_sha256": case_digest,
        "services": {
            "read": {
                "audience": AUDIENCE,
                "client_id": CLIENT_ID,
                "enterprise_id": _ENTERPRISE,
                "url": _URL,
                "credential_sha256": digest,
            }
        },
    }
    if manifest != expected:
        raise ValueError()
    state = EmployeeCaseState(
        root, public.decode("ascii"), fixture.employee_no, fixture.display_name, case_digest, digest
    )
    # 服务路径只检查私有文件元数据；绝不读取秘密内容。
    _metadata(root / "issuer-private.pem", 16_384)
    for directory in (root / "tokens", root / "read", root / "read" / "audit"):
        descriptor = _directory(directory)
        os.close(descriptor)
    _check_policy(state.policy_path("read"), state.employee_no)
    _metadata(state.credential_path("read"), 16_384)
    journal = state.journal_path("read")
    for suffix in ("", "-journal", "-wal", "-shm"):
        _metadata(journal.with_name(journal.name + suffix), 1 << 40, optional=True)
    _metadata(state.capture_path("read"), 1 << 40, optional=True)
    return state


def load_case(root: Path) -> EmployeeCaseState:
    """校验私有的只读用例，不加载签名/服务秘密。"""
    try:
        return _load_case(root)
    except Exception:
        raise BusinessError(_STATE_ERROR) from None


def init_case(root: Path, input_file: Path) -> dict:
    """从私有的已批准输入一次性创建；已存在的用例不能重新绑定。"""
    try:
        supplied = _fixture(_read_private(_absolute(input_file), 16_384))
    except Exception:
        raise BusinessError(_INPUT_ERROR) from None
    try:
        root = _absolute(root)
        if not _mkdir(root, parent_private=False):
            state = _load_case(root)
            if (state.employee_no, state.display_name) != (
                supplied.employee_no,
                supplied.display_name,
            ):
                raise ValueError()
            return {"status": "already_initialized", "mode": MODE, "root": str(root)}
        for directory in (root / "read", root / "tokens", root / "read" / "audit"):
            _mkdir(directory)
        pair = RSAKeyPair.generate()
        _write_new(root / "issuer-public.pem", pair.public_key.encode("ascii"))
        _write_new(root / "issuer-private.pem", pair.private_key.get_secret_value().encode("ascii"))
        raw_fixture = json.dumps(supplied.model_dump(), ensure_ascii=False).encode("utf-8")
        _write_new(root / "fixture.json", raw_fixture)
        credential = f"approved-read-case-{secrets.token_hex(32)}".encode("ascii")
        _write_new(root / "read" / "service.key", credential)
        _write_new(
            root / "read" / "access.json", json.dumps(_policy(supplied.employee_no)).encode()
        )
        manifest = {
            "schema_version": 1,
            "mode": "approved-read-case",
            "issuer": ISSUER,
            "public_key_sha256": hashlib.sha256(pair.public_key.encode("ascii")).hexdigest(),
            "fixture_sha256": hashlib.sha256(raw_fixture).hexdigest(),
            "services": {
                "read": {
                    "audience": AUDIENCE,
                    "client_id": CLIENT_ID,
                    "enterprise_id": _ENTERPRISE,
                    "url": _URL,
                    "credential_sha256": hashlib.sha256(credential).hexdigest(),
                }
            },
        }
        # 标记最后写入：未完成的初始化必须在下次运行时以失败关闭。
        _write_new(root / "state.json", json.dumps(manifest).encode())
        _load_case(root)
        return {"status": "initialized", "mode": MODE, "root": str(root)}
    except Exception:
        raise BusinessError(_STATE_ERROR) from None


def issue_case_token(
    root: Path, *, user: str, service: str, output: Path, ttl_seconds: int = 300
) -> dict:
    """签发只读的模拟主体令牌；员工数据不出现在声明（claim）中。"""
    try:
        _read(service)
        if type(user) is not str or user not in USERS:
            raise ValueError()
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise ValueError()
        state = _load_case(root)
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
            audience=AUDIENCE,
            scopes=[SCOPE],
            expires_in_seconds=ttl_seconds,
            additional_claims={"azp": CLIENT_ID},
        )
        _write_new(output, (token + "\n").encode("ascii"))
        return {
            "status": "issued",
            "mode": MODE,
            "user": user,
            "service": "read",
            "output": str(output),
            "ttl_seconds": ttl_seconds,
        }
    except Exception:
        raise BusinessError(_TOKEN_ERROR) from None
