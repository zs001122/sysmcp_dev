"""Persistent, authenticated four-interface deployment without per-request grants.

The controlled service policy owns caller/action/target authorization. A client
holds only its signing material; the server holds only its verification key and
the existing T100 credential/audit references. Deployment can be split or scoped
to one maintenance service without changing the business authorization boundary.
"""

import base64
import hashlib
import ipaddress
import json
import os
import stat
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import anyio
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from fastmcp.server.middleware import Middleware
from pydantic import ConfigDict, Field, PrivateAttr, SecretStr, field_validator, model_validator
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .access import CREDENTIAL_MAX_BYTES, _private_bytes
from .client import T100Config
from .function_audit import FunctionAudit, LegacyEmployeeUpdateRecord
from .functions import (
    SCOPES,
    TOOLS,
    FunctionGrant,
    FunctionService,
    create_function_server,
    request_digest,
)
from .interface_client import T100InterfaceClient
from .models import BusinessError, Caller, Identifier, Model
from .runtime import _absolute_path, _Admission, _ancestors, _journal_files, _private_metadata
from .server import IdentityProfile, current_caller

CONFIGURATION_ERROR = "runtime_configuration_invalid"
MAX_CONFIGURATION_BYTES = 1_000_000
WRITE_ACTIONS = frozenset(SCOPES) - {"employee_query"}


class AccountTarget(Model):
    employee_no: Identifier
    account_id: Identifier


class ServicePrincipal(Model):
    issuer: str = Field(min_length=1, max_length=512)
    subject: str = Field(min_length=1, max_length=256)
    client_id: str = Field(min_length=1, max_length=256)
    enabled: bool = False
    actions: frozenset[str] = Field(min_length=1)
    employee_ids: frozenset[Identifier] = frozenset()
    account_targets: tuple[AccountTarget, ...] = ()
    allow_search: bool = False
    copy_sources: frozenset[Identifier] = frozenset()
    account_profile: Literal["full", "status-password"] = "full"

    @model_validator(mode="after")
    def valid_capability(self):
        if not self.actions.issubset(SCOPES):
            raise ValueError("unknown action")
        pairs = {(target.employee_no, target.account_id) for target in self.account_targets}
        if len(pairs) != len(self.account_targets):
            raise ValueError("duplicate account target")
        return self


class ServicePermissions(Model):
    enterprise_id: str = Field(min_length=1, max_length=256)
    upstream_config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    principals: tuple[ServicePrincipal, ...] = Field(min_length=1, max_length=100)
    legacy_employee_updates: tuple[LegacyEmployeeUpdateRecord, ...] = ()

    @model_validator(mode="after")
    def unique_principals(self):
        ids = {(row.issuer, row.subject, row.client_id) for row in self.principals}
        if len(ids) != len(self.principals):
            raise ValueError("a caller must have a single capability")
        if any(row.enterprise_id != self.enterprise_id for row in self.legacy_employee_updates):
            raise ValueError("wrong historical enterprise")
        return self


class ServiceRuntimeConfig(Model):
    """Private explicit assembly; no signing private key or upstream secret values."""

    model_config = ConfigDict(hide_input_in_errors=True)
    mode: Literal["read", "write", "maintenance"]
    identity: IdentityProfile
    public_key_file: Path
    t100: T100Config
    permissions_file: Path
    deployment_permissions_file: Path | None = None
    credential_file: Path
    audit_path: Path
    legacy_write_path: Path | None = None
    evidence_dir: Path
    host: str = "127.0.0.1"
    port: int = Field(ge=1024, le=65535)
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    max_in_flight: int = Field(default=4, ge=1, le=32)
    _source_path: Path | None = PrivateAttr(default=None)

    @field_validator(
        "public_key_file",
        "permissions_file",
        "deployment_permissions_file",
        "credential_file",
        "audit_path",
        "legacy_write_path",
        "evidence_dir",
    )
    @classmethod
    def private_absolute_path(cls, value):
        return None if value is None else _absolute_path(value)

    @field_validator("host")
    @classmethod
    def explicit_bind_address(cls, value):
        ipaddress.ip_address(value)
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def explicit_host_aliases(cls, values):
        for value in values:
            if not value or any(char.isspace() or char in "*?/\\#@" for char in value):
                raise ValueError("exact host aliases required")
            try:
                ipaddress.ip_address(value)
                continue
            except ValueError:
                pass
            parsed = urlsplit("//" + value)
            if (
                not parsed.hostname
                or parsed.path
                or (parsed.port is not None and not 0 < parsed.port <= 65535)
            ):
                raise ValueError("exact hostname or hostname:port required")
        return values

    @field_validator("allowed_origins")
    @classmethod
    def explicit_origin_aliases(cls, values):
        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or any(char.isspace() or char in "*?" for char in value)
            ):
                raise ValueError("exact HTTP origins required")
        return values

    @model_validator(mode="after")
    def distinct_paths(self):
        files = [
            self.public_key_file,
            self.permissions_file,
            self.credential_file,
            *_journal_files(self.audit_path),
        ]
        if self.deployment_permissions_file is not None:
            files.append(self.deployment_permissions_file)
        if self.legacy_write_path is not None:
            files.extend(_journal_files(self.legacy_write_path))
        if len(set(files)) != len(files) or self.evidence_dir in files:
            raise ValueError("private files and evidence require separate paths")
        if any(a in b.parents for a in files for b in files if a != b):
            raise ValueError("a private file cannot contain another file")
        if any(path in self.evidence_dir.parents for path in files):
            raise ValueError("evidence cannot be inside a private file")
        if any(self.evidence_dir in path.parents for path in files):
            raise ValueError("evidence cannot contain configuration or databases")
        return self

    @property
    def actions(self):
        if self.mode == "maintenance":
            return frozenset(SCOPES)
        return frozenset({"employee_query"}) if self.mode == "read" else WRITE_ACTIONS


def upstream_digest(config):
    return hashlib.sha256(config.t100.model_dump_json().encode()).hexdigest()


def load_service_runtime_config(path: Path) -> ServiceRuntimeConfig:
    try:
        source = _absolute_path(Path(path))
        config = ServiceRuntimeConfig.model_validate_json(
            _private_bytes(source, MAX_CONFIGURATION_BYTES)
        )
        config._source_path = source
        protected = {
            config.public_key_file,
            config.permissions_file,
            config.credential_file,
            config.deployment_permissions_file,
            config.legacy_write_path,
            *_journal_files(config.audit_path),
        }
        if source in protected or source == config.evidence_dir:
            raise ValueError()
        return config
    except Exception:
        raise BusinessError(CONFIGURATION_ERROR) from None


def _checked(config):
    try:
        if type(config) is not ServiceRuntimeConfig:
            raise ValueError()
        result = ServiceRuntimeConfig.model_validate_json(config.model_dump_json(warnings=False))
        result._source_path = config._source_path
        return result
    except Exception:
        raise BusinessError(CONFIGURATION_ERROR) from None


def _read_permissions(config):
    try:
        raw = _private_bytes(config.permissions_file, MAX_CONFIGURATION_BYTES)
        policy = ServicePermissions.model_validate_json(raw)
        if (
            policy.enterprise_id != config.t100.enterprise_id
            or policy.upstream_config_digest != upstream_digest(config)
            or any(
                row.issuer != config.identity.issuer
                or row.client_id not in config.identity.allowed_clients
                for row in policy.principals
            )
        ):
            raise ValueError()
        if config.deployment_permissions_file is not None:
            deployment, _ = _read_deployment_permissions(config)
            classified = tuple(
                LegacyEmployeeUpdateRecord.model_validate_json(json.dumps(value))
                for value in deployment.get("legacy_employee_updates", [])
            )
            if classified != policy.legacy_employee_updates:
                raise ValueError("historical classification changed; explicit restart required")
        return policy, raw
    except Exception:
        raise BusinessError("policy_unavailable") from None


def _read_deployment_permissions(config):
    try:
        raw = _private_bytes(config.deployment_permissions_file, MAX_CONFIGURATION_BYTES)
        policy = json.loads(raw)
        if policy["enterprise_id"] != config.t100.enterprise_id or policy[
            "upstream_config_digest"
        ] != upstream_digest(config):
            raise ValueError()
        if type(policy.get("queries_enabled")) is not bool:
            raise ValueError()
        if type(policy.get("writes_enabled")) is not bool:
            raise ValueError()
        if type(policy.get("write_targets")) is not list or any(
            type(row) is not dict for row in policy["write_targets"]
        ):
            raise ValueError()
        if type(policy.get("legacy_employee_updates", [])) is not list:
            raise ValueError()
        for value in policy.get("legacy_employee_updates", []):
            LegacyEmployeeUpdateRecord.model_validate_json(json.dumps(value))
        return policy, raw
    except Exception:
        raise BusinessError("policy_unavailable") from None


def _deployment_guard(config, action, request):
    """Retain the existing local deployment switches/targets when configured."""
    if config.deployment_permissions_file is None:
        return b""
    try:
        policy, raw = _read_deployment_permissions(config)
        if action == "employee_query":
            if policy.get("queries_enabled") is not True:
                raise BusinessError("permission_denied")
        else:
            if policy.get("writes_enabled") is not True:
                raise BusinessError("write_permission_not_configured")
            targets = policy["write_targets"]
            if type(targets) is not list or any(type(row) is not dict for row in targets):
                raise ValueError()
            rows = request.parameter()["master" if action == "account_maintain" else "employee"]
            for row in rows:
                employee = row["gzxa003" if action == "account_maintain" else "employee_no"]
                if not any(
                    target.get("action") == action
                    and target.get("employee_no") == employee
                    and (
                        "request_digest" not in target
                        or target["request_digest"] == request_digest(action, request)
                    )
                    and (action != "account_maintain" or target.get("account_id") == row["gzxa001"])
                    for target in targets
                ):
                    raise BusinessError("permission_denied")
                if (
                    action == "account_maintain"
                    and row["is_reproduce"] == "Y"
                    and not any(
                        target.get("action") == action
                        and target.get("employee_no") == row["reproduce_acct"]
                        for target in targets
                    )
                ):
                    raise BusinessError("permission_denied")
        return raw
    except BusinessError:
        raise
    except Exception:
        raise BusinessError("policy_unavailable") from None


class ScopedFunctionAccess:
    """Caller/target checks always precede the upstream service-key read."""

    def __init__(self, config):
        self.config = config
        self._legacy_classification = _read_permissions(config)[0].legacy_employee_updates

    def principal(self, caller):
        policy, raw = _read_permissions(self.config)
        if policy.legacy_employee_updates != self._legacy_classification:
            raise BusinessError("policy_unavailable")
        rows = [
            row
            for row in policy.principals
            if (row.issuer, row.subject, row.client_id)
            == (caller.issuer, caller.subject, caller.client_id)
        ]
        if len(rows) != 1:
            raise BusinessError("permission_denied")
        if not rows[0].enabled:
            raise BusinessError("identity_not_enabled")
        return rows[0], raw

    def _check_authorization(self, caller, action, request):
        principal, raw = self.principal(caller)
        if action not in self.config.actions or action not in principal.actions:
            raise BusinessError("permission_denied")
        if action == "employee_query":
            employee = request.parameter().get("employee_no")
            if employee:
                if employee not in principal.employee_ids:
                    raise BusinessError("permission_denied")
            elif not principal.allow_search:
                raise BusinessError("permission_denied")
        elif action == "account_maintain":
            targets = {(row.employee_no, row.account_id) for row in principal.account_targets}
            for row in request.parameter()["master"]:
                if (row["gzxa003"], row["gzxa001"]) not in targets:
                    raise BusinessError("permission_denied")
                if principal.account_profile == "status-password" and (
                    set(row) != {"gzxastus", "gzxa001", "gzxa003", "is_reproduce", "password_reset"}
                    or row["is_reproduce"] != "N"
                    or (row["gzxastus"], row["password_reset"])
                    not in {("N", "N"), ("Y", "N"), ("Y", "Y")}
                ):
                    raise BusinessError("permission_denied")
                if (
                    row["is_reproduce"] == "Y"
                    and row["reproduce_acct"] not in principal.copy_sources
                ):
                    raise BusinessError("permission_denied")
        elif any(
            row["employee_no"] not in principal.employee_ids
            for row in request.parameter()["employee"]
        ):
            raise BusinessError("permission_denied")
        deployment = _deployment_guard(self.config, action, request)
        return hashlib.sha256(raw + b"\0" + deployment).hexdigest()

    def _authorize(self, caller, action, request):
        version = self._check_authorization(caller, action, request)
        try:
            secret = (
                _private_bytes(self.config.credential_file, CREDENTIAL_MAX_BYTES).decode().strip()
            )
            if not secret or any(ord(char) < 32 or ord(char) == 127 for char in secret):
                raise ValueError()
        except Exception:
            raise BusinessError("credential_unavailable") from None
        return FunctionGrant(
            binding_version=version,
            credential=SecretStr(secret),
        )

    async def authorize(self, caller, action, request):
        return await anyio.to_thread.run_sync(self._authorize, caller, action, request)

    async def check_authorization(self, caller, action, request):
        """Final delivery check rereads current policy, never the service key."""
        return await anyio.to_thread.run_sync(self._check_authorization, caller, action, request)


def _private_directory(path):
    _ancestors(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise BusinessError("journal_unavailable")


def _save_evidence(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode())
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _ExactQueryClient:
    """An authorized exact selector never releases a fuzzy-match employee."""

    def __init__(self, client):
        self.client = client

    async def execute(self, action, request, credential, *, on_request=None):
        result = await self.client.execute(action, request, credential, on_request=on_request)
        if action == "employee_query":
            selected = request.parameter().get("employee_no")
            if selected and any(row["employee_no"] != selected for row in result["employee"]):
                raise BusinessError("upstream_employee_mismatch")
        return result


class _PersistentFunctionService:
    """Each request retains its own callbacks, evidence directory and UUID."""

    def __init__(self, config, access, audit):
        self.config, self.access, self.audit = config, access, audit

    async def execute(self, action, request, caller, *, check_caller, confirm=False):
        identifier = str(uuid4())
        folder = self.config.evidence_dir / identifier
        try:
            await anyio.to_thread.run_sync(_private_directory, folder)
            await anyio.to_thread.run_sync(
                _save_evidence,
                folder / "request.json",
                {
                    "request_id": identifier,
                    "action": action,
                    "caller": caller.model_dump(mode="json"),
                    "parameter": request.parameter(),
                    "confirm": confirm,
                },
            )
        except Exception:
            return {
                "request_id": identifier,
                "action": action,
                "status": "failed",
                "error": "journal_unavailable",
                "upstream_attempted": False,
                "write_upstream_attempted": False,
                "business_effect_verified": False,
            }

        def observed_write_response(evidence):
            _save_evidence(folder / "upstream-write-response.json", evidence)

        def prepared_write(request_id, selected_action, effective_request):
            _save_evidence(
                folder / "effective-request.json",
                {
                    "request_id": request_id,
                    "action": selected_action,
                    "intent_digest": request_digest(action, request),
                    "effective_request_digest": request_digest(selected_action, effective_request),
                    "parameter": effective_request.parameter(),
                },
            )

        class BoundAccess:
            first_version = None

            async def authorize(inner, selected_caller, selected_action, selected_request):
                grant = await self.access.authorize(
                    selected_caller, selected_action, selected_request
                )
                if inner.first_version is None:
                    inner.first_version = grant.binding_version
                return grant

        bound_access = BoundAccess()
        service = FunctionService(
            enterprise_id=self.config.t100.enterprise_id,
            access=bound_access,
            client=_ExactQueryClient(
                T100InterfaceClient(self.config.t100, on_write_response=observed_write_response)
            ),
            audit=self.audit,
            request_id=identifier,
            on_prepared_write=prepared_write,
        )
        result = await service.execute(
            action, request, caller, check_caller=check_caller, confirm=confirm
        )
        result.update(
            identity="temporary-resource-client",
            identity_mode="temporary-resource-client",
            authenticated_client_id=caller.client_id,
            real_identity_verified=False,
            mcp_transport="streamable-http",
            deployment_mode=self.config.mode,
        )
        try:
            await anyio.to_thread.run_sync(_save_evidence, folder / "result.json", result)
        except Exception:
            result.update(
                status="failed", error="journal_unavailable", business_effect_verified=False
            )
            result.pop("data", None)
        if result["status"] != "failed" and bound_access.first_version is not None:
            try:
                if check_caller() != caller:
                    raise BusinessError("authorization_changed")
                version = await self.access.check_authorization(caller, action, request)
                if version != bound_access.first_version or check_caller() != caller:
                    raise BusinessError("authorization_changed")
            except BusinessError as exc:
                # Audit terminal reflects the actual business execution. Do not rewrite
                # succeeded; retain the independently rejected delivery as new evidence.
                result.update(
                    status="failed",
                    error=exc.code,
                    business_effect_verified=False,
                    delivery_state="rejected_after_execution",
                )
                result.pop("data", None)
                try:
                    await anyio.to_thread.run_sync(
                        _save_evidence, folder / "delivery-rejected.json", result
                    )
                except Exception:
                    result["error"] = "journal_unavailable"
        return result


class _DiscoveryBoundary(Middleware):
    def __init__(self, config, access):
        self.config, self.access = config, access

    async def on_list_tools(self, context, call_next):
        visible = set()
        for action in self.config.actions:
            try:
                caller = current_caller(self.config.identity, required_scope=SCOPES[action])
                principal, _ = await anyio.to_thread.run_sync(self.access.principal, caller)
                if action in principal.actions:
                    visible.add(TOOLS[action])
            except BusinessError:
                continue
        if not visible:
            raise ToolError("unauthenticated")
        return [tool for tool in await call_next(context) if tool.name in visible]


def _inspect_service(config):
    _read_permissions(config)
    _private_metadata(config.public_key_file, maximum=100_000)
    # Credential inspection is metadata-only; readiness never reads its content.
    _private_metadata(config.credential_file, maximum=CREDENTIAL_MAX_BYTES)
    if config._source_path is not None:
        _private_metadata(config._source_path, maximum=MAX_CONFIGURATION_BYTES)
    if config.deployment_permissions_file is not None:
        _private_metadata(config.deployment_permissions_file, maximum=MAX_CONFIGURATION_BYTES)
    _ancestors(config.evidence_dir)
    if config.evidence_dir.exists():
        info = config.evidence_dir.lstat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError()
    for path in _journal_files(config.audit_path):
        if path.exists() or path.is_symlink():
            _private_metadata(path, exact_mode=0o600)


async def check_service_configuration(config):
    checked = _checked(config)
    try:
        await anyio.to_thread.run_sync(_inspect_service, checked)
        public_key = await anyio.to_thread.run_sync(
            _private_bytes, checked.public_key_file, 100_000
        )
        if not public_key.startswith(b"-----BEGIN PUBLIC KEY-----"):
            raise ValueError()
        JWTVerifier(
            public_key=public_key.decode(),
            issuer=checked.identity.issuer,
            audience=checked.identity.audience,
            algorithm="RS256",
        )
    except Exception:
        raise BusinessError(CONFIGURATION_ERROR) from None
    return {
        "status": "configuration_valid",
        "scope": "local_configuration_only",
        "deployment_mode": checked.mode,
        "tools": sorted(TOOLS[action] for action in checked.actions),
        "real_identity_verified": False,
        "business_validated": False,
    }


def create_service_runtime_app(config: ServiceRuntimeConfig) -> Starlette:
    """Assemble private files and native JWT verification; no upstream I/O or listener."""
    checked = _checked(config)
    try:
        public_key = _private_bytes(checked.public_key_file, 100_000)
        if not public_key.startswith(b"-----BEGIN PUBLIC KEY-----"):
            raise ValueError()
        policy, _ = _read_permissions(checked)
        auth = JWTVerifier(
            public_key=public_key.decode(),
            issuer=checked.identity.issuer,
            audience=checked.identity.audience,
            algorithm="RS256",
        )
        audit = FunctionAudit(
            checked.audit_path,
            enterprise_id=checked.t100.enterprise_id,
            legacy_write_path=checked.legacy_write_path,
            legacy_employee_updates=policy.legacy_employee_updates,
        )
    except Exception:
        raise BusinessError(CONFIGURATION_ERROR) from None
    access = ScopedFunctionAccess(checked)
    service = _PersistentFunctionService(checked, access, audit)
    server = create_function_server(
        auth=auth,
        identity=checked.identity,
        service=service,
        actions=checked.actions,
    )
    server.add_middleware(_DiscoveryBoundary(checked, access))
    native = server.http_app(
        path="/mcp",
        stateless_http=True,
        json_response=True,
        host_origin_protection=True,
        allowed_hosts=[checked.host, *checked.allowed_hosts],
        allowed_origins=list(checked.allowed_origins),
    )

    async def locally_ready():
        if not admission.accepting:
            return False
        try:
            await check_service_configuration(checked)
            key = await anyio.to_thread.run_sync(_private_bytes, checked.public_key_file, 100_000)
            if key != public_key:
                return False  # Verification-key rotation requires an explicit restart.
            await audit.check_ready()
        except Exception:
            return False
        return admission.accepting

    admission = _Admission(native, checked.max_in_flight, locally_ready)

    @asynccontextmanager
    async def lifespan(app):
        await check_service_configuration(checked)
        await audit.prepare()
        await anyio.to_thread.run_sync(_private_directory, checked.evidence_dir)
        async with native.router.lifespan_context(native):
            admission.accepting = True
            try:
                yield
            finally:
                admission.accepting = False

    async def live(request):
        return JSONResponse({"status": "live"})

    async def ready(request):
        available = await locally_ready()
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


def issue_gateway_token(
    config: ServiceRuntimeConfig,
    *,
    signing_key: Path,
    subject: str,
    client_id: str,
    output: Path,
    lifetime_seconds: int = 3600,
) -> dict:
    """Rotate a narrowly scoped temporary query/account connector token.

    This command never contacts MetaMCP/T100 or reads a T100 credential. The
    controlled existing principal remains authoritative on every business call.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    checked = _checked(config)
    if type(lifetime_seconds) is not int or not 0 < lifetime_seconds <= 3600:
        raise BusinessError("gateway_token_not_allowed")
    try:
        private_path = _absolute_path(Path(signing_key))
        destination = _absolute_path(Path(output))
        protected = {
            checked._source_path,
            checked.public_key_file,
            checked.permissions_file,
            checked.deployment_permissions_file,
            checked.credential_file,
            *_journal_files(checked.audit_path),
        }
        if checked.legacy_write_path is not None:
            protected.update(_journal_files(checked.legacy_write_path))
        if private_path in protected:
            raise ValueError()
        protected.add(private_path)
        if (
            destination in protected
            or destination == checked.evidence_dir
            or checked.evidence_dir in destination.parents
        ):
            raise ValueError()
        access = ScopedFunctionAccess(checked)
        caller = Caller(issuer=checked.identity.issuer, subject=subject, client_id=client_id)
        principal, _ = access.principal(caller)
        actions = {
            "read": frozenset({"employee_query"}),
            "write": frozenset({"account_maintain"}),
            "maintenance": frozenset({"employee_query", "account_maintain"}),
        }[checked.mode]
        if principal.actions != actions:
            raise BusinessError("gateway_token_not_allowed")
        if "account_maintain" in actions and (
            principal.account_profile != "status-password" or not principal.account_targets
        ):
            raise BusinessError("gateway_token_not_allowed")
        if checked.mode == "maintenance" and (
            not principal.employee_ids
            or principal.allow_search is not False
            or any(
                target.employee_no not in principal.employee_ids
                for target in principal.account_targets
            )
        ):
            raise BusinessError("gateway_token_not_allowed")
        if "employee_query" in actions:
            _deployment_guard(checked, "employee_query", None)
        if "account_maintain" in actions and checked.deployment_permissions_file is not None:
            deployment, _ = _read_deployment_permissions(checked)
            if deployment.get("writes_enabled") is not True:
                raise BusinessError("write_permission_not_configured")
            if any(
                not any(
                    row.get("action") == "account_maintain"
                    and row.get("employee_no") == target.employee_no
                    and row.get("account_id") == target.account_id
                    for row in deployment["write_targets"]
                )
                for target in principal.account_targets
            ):
                raise BusinessError("permission_denied")
        public_bytes = _private_bytes(checked.public_key_file, 100_000)
        private_bytes = _private_bytes(private_path, 100_000)
        if not public_bytes.startswith(b"-----BEGIN PUBLIC KEY-----"):
            raise ValueError()
        private = serialization.load_pem_private_key(private_bytes, password=None)
        public = serialization.load_pem_public_key(public_bytes)
        if (
            not isinstance(private, rsa.RSAPrivateKey)
            or not isinstance(public, rsa.RSAPublicKey)
            or private.public_key().public_numbers() != public.public_numbers()
        ):
            raise ValueError()
        pair = RSAKeyPair(
            private_key=SecretStr(private_bytes.decode()), public_key=public_bytes.decode()
        )
        token = pair.create_token(
            subject=subject,
            issuer=checked.identity.issuer,
            audience=checked.identity.audience,
            scopes=[SCOPES[action] for action in sorted(actions)],
            expires_in_seconds=lifetime_seconds,
            additional_claims={"azp": client_id},
        )
        # Decode only our own freshly signed output for its exact expiry. Never
        # accept identity or expiry from an unverified external token here.
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
        expiry = datetime.fromtimestamp(claims["exp"], UTC).isoformat()
        _replace_private_token(destination, token)
    except BusinessError as exc:
        if exc.code == "journal_unavailable":
            raise BusinessError("gateway_token_configuration_invalid") from None
        raise
    except Exception:
        raise BusinessError("gateway_token_configuration_invalid") from None
    return {
        "status": "issued",
        "token_file": str(destination),
        "expires_at": expiry,
        "identity_mode": "temporary-internal-connector",
        "real_identity_verified": False,
        "upstream_attempted": False,
    }


def _replace_private_token(destination, token):
    _ancestors(destination.parent)
    missing = []
    for directory in (destination.parent, *destination.parent.parents):
        if directory.exists() or directory.is_symlink():
            break
        missing.append(directory)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    _private_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        _private_metadata(destination, exact_mode=0o600)
    temporary = destination.with_name("." + destination.name + "." + str(uuid4()) + ".tmp")
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(token.encode() + b"\n")
            stream.flush()
            os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
