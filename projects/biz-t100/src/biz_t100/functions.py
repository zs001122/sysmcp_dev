"""Four T100 interfaces for an explicitly authorized maintenance operator.

This factory is separate from the employee self-service factories. Business selectors
never identify the caller: native authentication and a controlled request-bound grant do.
"""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import anyio
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from fastmcp.server.middleware import Middleware
from pydantic import Field, SecretStr
from sysmcp_core import create_service

from .access import CREDENTIAL_MAX_BYTES, _private_bytes
from .errors import ERROR_MESSAGES
from .interface_client import InterfaceUpstreamError
from .interfaces import (
    REQUEST_MODELS,
    SERVICES,
    AccountMaintainRequest,
    EmployeeCreateRequest,
    EmployeeQueryRequest,
    EmployeeUpdateRequest,
    Parameters,
)
from .models import BusinessError, Caller, Model
from .server import IdentityProfile, current_caller

SCOPES = {
    "employee_query": "t100:employee:query",
    "employee_create": "t100:employee:create",
    "employee_update": "t100:employee:update",
    "account_maintain": "t100:account:maintain",
}
TOOLS = {action: "t100_" + action for action in SCOPES}


def request_digest(action, request):
    body = json.dumps(
        {"action": action, "parameter": request.parameter()},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def request_targets(action, request):
    body = request.parameter()
    if action == "employee_query":
        return (), ()  # Search criteria are bound by digest; results may contain many employees.
    if action == "account_maintain":
        return (
            tuple(sorted({row["gzxa003"] for row in body["master"]})),
            tuple(sorted({row["gzxa001"] for row in body["master"]})),
        )
    return tuple(sorted(row["employee_no"] for row in body["employee"])), ()


EMPLOYEE_UPDATE_PRESERVE_FIELDS = (
    "full_name",
    "department_no",
    "status",
    "last_name",
    "middle_name",
    "name",
    "job_title",
    "bank_account",
    "account_no",
    "email",
    "telephone",
    "mobile_phone",
)


def materialize_employee_update(request, snapshots):
    """将已授权的变更意图与同工号前查中的保留字段合成实际更新报文。

    显式字段（包括可选字段的空字符串）优先；省略的白名单字段必须在前查中
    存在且为字符串，不臆造空值。备注、修改者和日期不会自动回填。
    """
    try:
        if type(request) is not EmployeeUpdateRequest or type(snapshots) is not dict:
            raise ValueError()
        parameter = request.parameter()
        targets = {row["employee_no"] for row in parameter["employee"]}
        if set(snapshots) != targets:
            raise ValueError()
        effective_rows = []
        for intended in parameter["employee"]:
            baseline = snapshots[intended["employee_no"]]
            if type(baseline) is not dict:
                raise ValueError()
            if baseline.get("employee_no") != intended["employee_no"]:
                raise BusinessError("upstream_employee_mismatch")
            effective = dict(intended)
            for field in EMPLOYEE_UPDATE_PRESERVE_FIELDS:
                if field in effective:
                    continue
                if field not in baseline or type(baseline[field]) is not str:
                    raise ValueError()
                effective[field] = baseline[field]
            if any(not effective[field].strip() for field in ("full_name", "department_no")):
                raise ValueError()
            if effective["status"] not in {"Y", "N"}:
                raise ValueError()
            effective_rows.append(effective)
        parameter["employee"] = effective_rows
        result = EmployeeUpdateRequest.model_validate(parameter)
        if request_targets("employee_update", result) != request_targets(
            "employee_update", request
        ):
            raise ValueError()
        return result
    except BusinessError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise BusinessError("upstream_contract_error") from None


def employee_update_wire_request(intent, materialized):
    """省略前查已知为空的可选保留字段；显式空值仍作为清空请求发送。

    materialized 保留完整回读预期，本函数只形成与旧接口实现一致的实际报文。
    """
    try:
        if (
            type(intent) is not EmployeeUpdateRequest
            or type(materialized) is not EmployeeUpdateRequest
        ):
            raise ValueError()
        if request_targets("employee_update", intent) != request_targets(
            "employee_update", materialized
        ):
            raise ValueError()
        explicit = {row["employee_no"]: row for row in intent.parameter()["employee"]}
        parameter = materialized.parameter()
        optional = set(EMPLOYEE_UPDATE_PRESERVE_FIELDS) - {"full_name", "department_no", "status"}
        for row in parameter["employee"]:
            provided = explicit[row["employee_no"]]
            for field in optional:
                if field not in provided and row.get(field) == "":
                    del row[field]
        return EmployeeUpdateRequest.model_validate(parameter)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise BusinessError("upstream_contract_error") from None


class FunctionPermission(Model):
    """An explicit operator capability, not an employee identity inferred from input."""

    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    enterprise_id: str = Field(min_length=1)
    enabled: bool = False
    action: str
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    employee_ids: tuple[str, ...] = ()
    account_ids: tuple[str, ...] = ()


class FunctionGrant(Model):
    binding_version: str
    credential: SecretStr = Field(repr=False, exclude=True)


class FileFunctionAccess:
    """Reads a private, narrowly approved request before reading the service key."""

    def __init__(self, policy_file: Path, credential_file: Path, *, enterprise_id: str):
        if policy_file == credential_file:
            raise ValueError("policy and service credential must be separate")
        self.policy_file = policy_file
        self.credential_file = credential_file
        self.enterprise_id = enterprise_id

    def _authorize(self, caller, action, request):
        try:
            raw = _private_bytes(self.policy_file, 1_000_000)
            permission = FunctionPermission.model_validate_json(raw)
        except Exception:
            raise BusinessError("policy_unavailable") from None
        employees, accounts = request_targets(action, request)
        if not permission.enabled:
            raise BusinessError("identity_not_enabled")
        if (
            (permission.issuer, permission.subject, permission.client_id)
            != (caller.issuer, caller.subject, caller.client_id)
            or permission.enterprise_id != self.enterprise_id
            or permission.action != action
            or permission.request_digest != request_digest(action, request)
            or permission.employee_ids != employees
            or permission.account_ids != accounts
        ):
            raise BusinessError("permission_denied")
        try:
            value = _private_bytes(self.credential_file, CREDENTIAL_MAX_BYTES).decode().strip()
            if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError()
        except Exception:
            raise BusinessError("credential_unavailable") from None
        return FunctionGrant(
            binding_version=hashlib.sha256(raw).hexdigest(), credential=SecretStr(value)
        )

    async def authorize(self, caller, action, request):
        return await anyio.to_thread.run_sync(self._authorize, caller, action, request)


class GuardedFunctionAccess(FileFunctionAccess):
    """Live testing additionally requires a current deployment/target permission guard."""

    def __init__(self, policy_file, credential_file, *, enterprise_id, guard):
        if not callable(guard):
            raise ValueError("a live permission guard is required")
        super().__init__(policy_file, credential_file, enterprise_id=enterprise_id)
        self.guard = guard

    def _authorize(self, caller, action, request):
        version = self.guard()
        grant = super()._authorize(caller, action, request)
        return FunctionGrant(
            binding_version=hashlib.sha256((grant.binding_version + version).encode()).hexdigest(),
            credential=grant.credential,
        )


class FunctionService:
    """Shared business execution for native MCP and the two test entrypoints."""

    def __init__(
        self,
        *,
        enterprise_id,
        access,
        client,
        audit,
        request_id=None,
        on_prepared_write: Callable[[str, str, Parameters], None] | None = None,
    ):
        self.enterprise_id = enterprise_id
        self.access, self.client, self.audit = access, client, audit
        self._on_prepared_write = on_prepared_write
        # Only a trusted assembly may bind one reviewed retry to an existing UUID.
        # Remote tool callers cannot supply or change this identifier.
        self._request_id = request_id

    async def execute(self, action, request, caller: Caller, *, check_caller, confirm=False):
        identifier = self._request_id or str(uuid4())
        write = action != "employee_query"
        result = {
            "request_id": identifier,
            "action": action,
            "business_object": "user_account"
            if action == "account_maintain"
            else "employee_record",
            "upstream_service": SERVICES.get(action),
            "enterprise_id": self.enterprise_id,
            "status": "failed",
            "upstream_attempted": False,
            "write_upstream_attempted": False,
            "read_requests": 0,
            "write_requests": 0,
            "business_effect_verified": False,
        }
        started = False
        terminal = False

        async def authorized(previous=None):
            if check_caller() != caller:
                raise BusinessError("authorization_changed")
            grant = await self.access.authorize(caller, action, request)
            if check_caller() != caller:
                raise BusinessError("authorization_changed")
            if previous is not None and previous.binding_version != grant.binding_version:
                raise BusinessError("authorization_changed")
            return grant

        async def invoke(selected_action, selected_request, grant):
            def dispatched():
                result["upstream_attempted"] = True
                if selected_action == "employee_query":
                    result["read_requests"] += 1
                else:
                    result["write_requests"] += 1
                    result["write_upstream_attempted"] = True

            return await self.client.execute(
                selected_action, selected_request, grant.credential, on_request=dispatched
            )

        async def exact_employee(employee_id, grant):
            data = await invoke(
                "employee_query", EmployeeQueryRequest(employee_no=employee_id), grant
            )
            rows = data["employee"]
            if not rows:
                raise BusinessError("employee_not_found")
            if len(rows) != 1 or data.get("has_next") is True:
                raise BusinessError("employee_not_unique")
            if rows[0]["employee_no"] != employee_id:
                raise BusinessError("upstream_employee_mismatch")
            return rows[0]

        try:
            if action not in REQUEST_MODELS:
                raise BusinessError("invalid_request")
            request = REQUEST_MODELS[action].model_validate(
                request.model_dump(exclude_unset=True, warnings="error")
            )
            if write and confirm is not True:
                raise BusinessError("confirmation_required")
            with anyio.fail_after(10):
                grant = await authorized()
                employees, accounts = request_targets(action, request)
                await self.audit.start(
                    request_id=identifier,
                    caller=caller,
                    action=action,
                    employee_ids=employees,
                    account_ids=accounts,
                    binding_version=grant.binding_version,
                    request_digest=request_digest(action, request),
                    write=write,
                )
                started = True
                grant = await authorized(grant)
                # Authorization remains bound to the original change intent. Only current,
                # exactly identified employee fields may fill the replacement update payload.
                snapshots = {}
                if action in {"employee_update", "account_maintain"}:
                    for employee_id in employees:
                        snapshots[employee_id] = await exact_employee(employee_id, grant)
                        grant = await authorized(grant)
                verification_request = (
                    materialize_employee_update(request, snapshots)
                    if action == "employee_update"
                    else request
                )
                effective = (
                    employee_update_wire_request(request, verification_request)
                    if action == "employee_update"
                    else verification_request
                )
                if write:
                    digest = request_digest(action, effective)
                    result["effective_request_digest"] = digest
                    if self._on_prepared_write is not None:
                        try:
                            # Frozen models contain mutable nested lists; observers receive
                            # a detached validated model, never the outbound object itself.
                            observed = REQUEST_MODELS[action].model_validate(effective.parameter())
                            self._on_prepared_write(identifier, action, observed)
                        except Exception:
                            raise BusinessError("journal_unavailable") from None
                    effective = REQUEST_MODELS[action].model_validate(effective.parameter())
                    if request_digest(action, effective) != digest or request_targets(
                        action, effective
                    ) != (employees, accounts):
                        raise BusinessError("journal_unavailable")
                    grant = await authorized(grant)
                await self.audit.dispatched(identifier)
                grant = await authorized(grant)
                data = await invoke(action, effective, grant)
                grant = await authorized(grant)
                if action in {"employee_create", "employee_update"}:
                    observations = []
                    for expected in verification_request.parameter()["employee"]:
                        actual = await exact_employee(expected["employee_no"], grant)
                        grant = await authorized(grant)
                        # These timestamps are accepted inputs but their exact round-trip
                        # semantics are unverified. Report that gap rather than treating
                        # successful comparison of the other fields as full verification.
                        unverified = sorted(set(expected) & {"create_date", "modify_date"})
                        compared = {
                            key: value for key, value in expected.items() if key not in unverified
                        }
                        if any(actual.get(key) != value for key, value in compared.items()):
                            raise BusinessError("upstream_verification_failed")
                        observations.append(
                            {
                                "employee_no": expected["employee_no"],
                                "verified_fields": sorted(compared),
                                "unverified_fields": unverified,
                            }
                        )
                    data["verification"] = observations
                    fully_verified = all(not row["unverified_fields"] for row in observations)
                    result["business_effect_verified"] = fully_verified
                    data["business_effect_verified"] = fully_verified
                await authorized(grant)
                await self.audit.finish(identifier, "succeeded", None)
                terminal = True
                await authorized(grant)
                result.update(
                    status="passed"
                    if not write
                    else ("verified" if result["business_effect_verified"] else "acknowledged"),
                    data=data,
                )
                return result
        except anyio.get_cancelled_exc_class():
            # Backend cancellation is a BaseException, not an ordinary business
            # failure. Persist the observed dispatch boundary before propagating it.
            if started and not terminal:
                with anyio.CancelScope(shield=True):
                    try:
                        await self.audit.finish(
                            identifier,
                            "unknown" if result["write_upstream_attempted"] else "failed",
                            "operation_failed",
                        )
                    except Exception:
                        # A failed terminal write leaves the durable pending lock intact.
                        # Never hide the caller's original cancellation or imply success.
                        pass
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                code = "upstream_timeout"
            elif isinstance(exc, BusinessError):
                code = exc.code if exc.code in ERROR_MESSAGES else "operation_failed"
            else:
                code = "operation_failed"
            rejected = (
                result["write_upstream_attempted"]
                and isinstance(exc, InterfaceUpstreamError)
                and getattr(exc, "write_rejected", False) is True
            )
            result.update(error=code, business_effect_verified=False)
            if rejected:
                result["write_outcome"] = "rejected"
            result.pop("data", None)
            diagnostics = getattr(exc, "diagnostics", None)
            if type(diagnostics) is dict:
                result["diagnostics"] = diagnostics
            if started and not terminal:
                try:
                    with anyio.CancelScope(shield=True):
                        await self.audit.finish(
                            identifier,
                            "unknown"
                            if result["write_upstream_attempted"] and not rejected
                            else "failed",
                            code,
                        )
                except Exception:
                    result["error"] = "journal_unavailable"
            return result


class _FunctionBoundary(Middleware):
    def __init__(self, identity, actions=None):
        self.identity = identity
        self.actions = frozenset(SCOPES) if actions is None else frozenset(actions)

    async def on_call_tool(self, context, call_next):
        action = next((key for key, value in TOOLS.items() if value == context.message.name), None)
        if action not in self.actions:
            raise ToolError("tool_not_available")
        try:
            current_caller(self.identity, required_scope=SCOPES[action])
        except BusinessError:
            raise ToolError("unauthenticated") from None
        # Tool validation must never echo arbitrary submitted credentials or invalid data.
        args = context.message.arguments or {}
        allowed = {"request"} if action == "employee_query" else {"request", "confirm"}
        try:
            if set(args) - allowed:
                raise ValueError()
            REQUEST_MODELS[action].model_validate(args.get("request"))
            if "confirm" in args and type(args["confirm"]) is not bool:
                raise ValueError()
        except Exception:
            raise ToolError("invalid_arguments") from None
        return await call_next(context)


def create_function_server(
    *, auth: AuthProvider, identity: IdentityProfile, service: FunctionService, actions=None
):
    """Four interfaces, optionally restricted to one deployment's explicit tool set."""
    if not isinstance(auth, AuthProvider):
        raise ValueError("an explicit AuthProvider is required")
    selected = frozenset(SCOPES) if actions is None else frozenset(actions)
    if not selected or not selected.issubset(SCOPES):
        raise ValueError("an explicit nonempty subset of interface actions is required")
    server = create_service("t100-maintenance", auth=auth)
    server.add_middleware(_FunctionBoundary(identity, selected))

    async def execute(action, request, confirm=False):
        def caller():
            return current_caller(identity, required_scope=SCOPES[action])

        return await service.execute(
            action, request, caller(), check_caller=caller, confirm=confirm
        )

    @server.tool(name=TOOLS["employee_query"], annotations={"readOnlyHint": True})
    async def query(request: EmployeeQueryRequest) -> dict:
        """按工号、姓名等条件检索员工资料；多条结果是候选列表，不自动选人写入。"""
        return await execute("employee_query", request)

    @server.tool(name=TOOLS["employee_create"], annotations={"destructiveHint": True})
    async def create(request: EmployeeCreateRequest, confirm: bool = False) -> dict:
        """新建员工档案并回读提交字段；不会隐式创建登录账号。"""
        return await execute("employee_create", request, confirm)

    @server.tool(name=TOOLS["employee_update"], annotations={"destructiveHint": True})
    async def update(request: EmployeeUpdateRequest, confirm: bool = False) -> dict:
        """修改已有员工档案并回读；status 是档案状态，不控制登录账号启停。"""
        return await execute("employee_update", request, confirm)

    @server.tool(name=TOOLS["account_maintain"], annotations={"destructiveHint": True})
    async def account(request: AccountMaintainRequest, confirm: bool = False) -> dict:
        """维护登录账号：gzxastus Y/N 启停，password_reset 重置；支持复制和权限明细，仅声明回执。"""
        return await execute("account_maintain", request, confirm)

    # Native visibility also removes disabled tools from direct calls. The boundary
    # separately refuses excluded actions before argument validation or business I/O.
    hidden = {TOOLS[action] for action in set(SCOPES) - selected}
    if hidden:
        server.disable(names=hidden, components={"tool"})
    return server


def create_function_app(**dependencies):
    return create_function_server(**dependencies).http_app(
        path="/mcp", stateless_http=True, json_response=True
    )
