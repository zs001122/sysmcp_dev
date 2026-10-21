# Historical regression support relocated from scripts; not a supported command.
"""本机操作员的真实账号维护入口；可直接执行或保留显式预览流程。

临时身份只代表本机批准的测试用例。工号和登录账号来自私有计划，不能从工具
参数覆盖。真实写入依赖接口维护方对局部更新语义的确认；计划预览不读取服务 key。
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import anyio
import httpx2
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from internal_support import _directory, _metadata, _mkdir, _read_private, _write_new
from live_employee_call import _base_permission, call_employee
from live_read import DEFAULT_STATE, load_live
from pydantic import Field, SecretStr

from biz_t100.account_write import (
    AccountWritePlan,
    T100AccountWriteClient,
    create_account_write_app,
)
from biz_t100.account_write_store import SQLiteAccountWriteStore
from biz_t100.employee_status import (
    EmployeeStatusPlan,
    T100EmployeeStatusClient,
    create_employee_status_app,
)
from biz_t100.errors import error_message
from biz_t100.models import (
    BusinessError,
    Caller,
    Identifier,
    Model,
    PasswordResetGrant,
    QueryResult,
)
from biz_t100.reset_server import RESET_ERRORS
from biz_t100.server import IdentityProfile

ROOT = Path(__file__).resolve().parents[2] / ".runtime" / "live-write"
ISSUER = "https://temporary-t100-account-write.example.invalid"
AUDIENCE = "t100-internal-account-write"
CLIENT = "t100-internal-write-operator"
PROTOCOL = "2025-11-25"
MODE = "live-t100-write-with-temporary-identity"
LEGACY_PROFILE = "legacy-account-v1"
EMPLOYEE_PROFILE = "employee-status-v1"
WriteProfile = Literal["legacy-account-v1", "employee-status-v1"]
Action = Literal["reset_password", "disable_account", "enable_account"]
PROFILES = {
    "enable_account": ("enable_my_t100_account", "t100:account:enable:self"),
    "reset_password": ("reset_my_t100_password", "t100:password:reset:self"),
    "disable_account": ("disable_my_t100_account", "t100:account:disable:self"),
}
LOCAL_ERRORS = {
    "employee_update_contract_unconfirmed": "尚未确认员工更新省略字段保持，未调用上游。",
    "legacy_status_write_disabled": "启停已改用员工资料状态更新，旧用户账号启停计划不能再次执行。",
    "write_state_invalid": "写入配置或私有计划无效。",
    "readback_not_available": "尚无已确认的写入 ACK，不能以本命令验证该操作。",
    "review_not_available": "仅支持核查员工状态更新的 unknown 操作；不会重发或释放占用。",
    "readback_binding_mismatch": "写入审计与当前受控目标或授权不一致，未执行回读。",
    "account_status_mismatch": "资料查询返回的账号状态与写入预期不一致。",
    "operation_conflict": "此操作编号已绑定其他目标、动作或状态，未调用上游。",
    "write_contract_unconfirmed": "尚未确认旧接口省略角色/站点明细时的保留语义，未调用上游。",
    "existing_account_unconfirmed": "尚未确认登录账号已存在且属于此员工，未调用上游。",
    "write_execution_not_enabled": "内部真实写入尚未启用，计划可预览但不会调用上游。",
    "confirmation_required": "真实账号维护需要显式 --confirm。",
    "write_call_failed": "账号维护调用未能确认结果，请核对操作库，勿自动重试。",
    "reset_already_recorded": "该操作编号已有记录，不会重复写入。",
    "reset_pending_review": "此员工或账号存在待核查写操作，重置、启用与禁用均暂不执行。",
    "reset_outcome_unknown": "账号维护结果未知，保留员工和账号占用，禁止自动重试。",
}


class AllowedTarget(Model):
    """运行方批准的具体员工、登录账号、动作及状态范围。"""

    employee_id: Identifier
    account_id: Identifier
    actions: list[Action] = Field(min_length=1)
    account_statuses: list[Literal["Y", "N"]] = Field(min_length=1)


class Contract(Model):
    """维护方提供的上游语义证据；布尔值本身不是验证证据。"""

    employee_update_omissions_preserved: bool = False
    employee_status_updates_enabled: bool = False
    legacy_status_writes_enabled: bool = True
    omitted_details_preserved: bool = False
    evidence_reference: str = Field(default="", max_length=1024)
    internal_test_write_enabled: bool = False
    allowed_targets: list[AllowedTarget] | None = None
    enterprise_id: str | None = None
    upstream_config_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SavedPlan(Model):
    operation_id: str
    plan: AccountWritePlan | EmployeeStatusPlan
    upstream_profile: WriteProfile = LEGACY_PROFILE
    enterprise_id: str = Field(min_length=1, max_length=256)
    upstream_config_digest: str
    existing_account_confirmed: bool = False
    enabled: bool = True
    identity_mode: Literal["temporary-local-test"] = "temporary-local-test"


def _id(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError()
    return value


def _digest(config):
    return hashlib.sha256(config.model_dump_json().encode()).hexdigest()


def _root(root):
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError()
    return root


def initialize(root: Path):
    root = _root(root)
    _mkdir(root)
    os.close(_directory(root))
    for name in ("plans", "audit"):
        _mkdir(root / name)
        os.close(_directory(root / name))
    if not (root / "contract.json").exists():
        _write_new(root / "contract.json", Contract().model_dump_json().encode())
    # 本写入口不复用只读服务的签名密钥，也不改变只读配置。
    if not (root / "issuer-private.pem").exists():
        pair = RSAKeyPair.generate()
        _write_new(root / "issuer-private.pem", pair.private_key.get_secret_value().encode())
        _write_new(root / "issuer-public.pem", pair.public_key.encode())
    _metadata(root / "issuer-private.pem", 16384)
    _metadata(root / "issuer-public.pem", 16384)


def prepare_plan(
    state,
    root: Path,
    *,
    action: str,
    employee_no: str,
    account_id: str,
    account_status: str | None = None,
    existing_account_confirmed: bool = False,
    upstream_profile: WriteProfile = LEGACY_PROFILE,
) -> dict:
    if account_status is None and action in {"disable_account", "enable_account"}:
        account_status = "N" if action == "disable_account" else "Y"
    try:
        plan_type = EmployeeStatusPlan if upstream_profile == EMPLOYEE_PROFILE else AccountWritePlan
        plan = plan_type(
            action=action,
            employee_id=employee_no,
            account_id=account_id,
            account_status=account_status,
        )
        initialize(root)
        identifier = str(uuid4())
        saved = SavedPlan(
            operation_id=identifier,
            plan=plan,
            upstream_profile=upstream_profile,
            enterprise_id=state.config.t100.enterprise_id,
            upstream_config_digest=_digest(state.config.t100),
            existing_account_confirmed=existing_account_confirmed,
        )
        _write_new(root / "plans" / (identifier + ".json"), saved.model_dump_json().encode())
        return preview(state, root, identifier)
    except BusinessError:
        raise
    except Exception:
        raise BusinessError("invalid_arguments") from None


def _load(state, root, operation_id):
    try:
        root = _root(root)
        _id(operation_id)
        raw = _read_private(root / "plans" / (operation_id + ".json"), 100_000)
        saved = SavedPlan.model_validate_json(raw)
        contract_raw = _read_private(root / "contract.json", 100_000)
        contract = Contract.model_validate_json(contract_raw)
        if (
            saved.operation_id != operation_id
            or saved.enterprise_id != state.config.t100.enterprise_id
            or saved.upstream_config_digest != _digest(state.config.t100)
        ):
            raise ValueError()
        return saved, contract, hashlib.sha256(raw + b"\x00" + contract_raw).hexdigest()
    except Exception:
        raise BusinessError("write_state_invalid") from None


def _allowed(saved, contract, *, readback=False):
    if saved.upstream_profile == EMPLOYEE_PROFILE:
        EmployeeStatusPlan.model_validate(saved.plan.model_dump())
        if not contract.employee_update_omissions_preserved:
            raise BusinessError("employee_update_contract_unconfirmed")
        if not contract.employee_status_updates_enabled:
            raise BusinessError("write_execution_not_enabled")
    elif (
        saved.plan.action != "reset_password"
        and not contract.legacy_status_writes_enabled
        and not readback
    ):
        raise BusinessError("legacy_status_write_disabled")
    if not saved.enabled:
        raise BusinessError("permission_denied")
    if not saved.existing_account_confirmed:
        raise BusinessError("existing_account_unconfirmed")
    if (
        saved.upstream_profile == LEGACY_PROFILE and not contract.omitted_details_preserved
    ) or not contract.evidence_reference.strip():
        raise BusinessError("write_contract_unconfirmed")
    if not contract.internal_test_write_enabled:
        raise BusinessError("write_execution_not_enabled")
    if (contract.enterprise_id is not None and saved.enterprise_id != contract.enterprise_id) or (
        contract.upstream_config_digest is not None
        and saved.upstream_config_digest != contract.upstream_config_digest
    ):
        raise BusinessError("permission_denied")
    if contract.allowed_targets is not None and not any(
        row.employee_id == saved.plan.employee_id
        and row.account_id == saved.plan.account_id
        and saved.plan.action in row.actions
        and saved.plan.account_status in row.account_statuses
        for row in contract.allowed_targets
    ):
        raise BusinessError("permission_denied")


def preview(state, root, operation_id):
    saved, contract, _ = _load(state, root, operation_id)
    blocked = None
    try:
        _allowed(saved, contract)
    except BusinessError as exc:
        blocked = exc.code
    return {
        "mode": MODE,
        "status": "planned",
        "operation_id": operation_id,
        "action": saved.plan.action,
        "upstream_profile": saved.upstream_profile,
        "service": "employee.data.update"
        if saved.upstream_profile == EMPLOYEE_PROFILE
        else "user.account.create",
        "parameter": (
            EmployeeStatusPlan.model_validate(saved.plan.model_dump()).parameter()
            if saved.upstream_profile == EMPLOYEE_PROFILE
            else {"master": [saved.plan.master()]}
        ),
        "real_identity_verified": False,
        "upstream_attempted": False,
        "ready_for_explicit_call": blocked is None,
        "blocker": blocked,
        "plan_path": str(root / "plans" / (operation_id + ".json")),
        "audit_path": str(root / "audit" / "account-write.sqlite3"),
    }


def _direct_intent(
    state,
    root,
    *,
    action,
    employee_no,
    account_id,
    account_status,
    operation_id,
    confirm,
    existing_account_confirmed,
    upstream_profile=LEGACY_PROFILE,
):
    if confirm is not True:
        raise BusinessError("confirmation_required")
    if existing_account_confirmed is not True:
        raise BusinessError("existing_account_unconfirmed")
    try:
        _id(operation_id)
        if account_status is None and action in {"disable_account", "enable_account"}:
            account_status = "N" if action == "disable_account" else "Y"
        plan_type = EmployeeStatusPlan if upstream_profile == EMPLOYEE_PROFILE else AccountWritePlan
        selected = plan_type(
            action=action,
            employee_id=employee_no,
            account_id=account_id,
            account_status=account_status,
        )
    except Exception:
        raise BusinessError("invalid_arguments") from None
    initialize(root)
    intended = SavedPlan(
        operation_id=operation_id,
        plan=selected,
        upstream_profile=upstream_profile,
        enterprise_id=state.config.t100.enterprise_id,
        upstream_config_digest=_digest(state.config.t100),
        existing_account_confirmed=True,
    )
    try:
        contract = Contract.model_validate_json(_read_private(root / "contract.json", 100_000))
    except Exception:
        raise BusinessError("write_state_invalid") from None
    _allowed(intended, contract)
    path = root / "plans" / (operation_id + ".json")
    try:
        # 内部保留授权快照用于重验与审计；CLI 无需先运行 plan。
        _write_new(path, intended.model_dump_json().encode())
    except FileExistsError:
        pass
    existing, contract, _ = _load(state, root, operation_id)
    if (
        existing.plan.model_dump() != selected.model_dump()
        or existing.upstream_profile != upstream_profile
    ):
        raise BusinessError("operation_conflict")
    # 重试不能覆盖已撤权/停用的既有授权文件。
    _allowed(existing, contract)
    return selected


async def execute_direct(
    state,
    root,
    *,
    action,
    employee_no,
    account_id,
    operation_id,
    account_status=None,
    confirm=False,
    existing_account_confirmed=False,
    upstream_profile=LEGACY_PROFILE,
):
    """一条命令完成可信映射、原生 MCP 调用和真实上游请求；不自动重试。"""
    try:
        from functools import partial

        selected = await anyio.to_thread.run_sync(
            partial(
                _direct_intent,
                state,
                root,
                action=action,
                employee_no=employee_no,
                account_id=account_id,
                account_status=account_status,
                operation_id=operation_id,
                confirm=confirm,
                existing_account_confirmed=existing_account_confirmed,
                upstream_profile=upstream_profile,
            )
        )
        result = await call_plan(state, root, operation_id, confirm=True)
        result.update(
            entrypoint="direct",
            requested_employee_no=selected.employee_id,
            requested_account_id=selected.account_id,
            requested_account_status=selected.account_status,
        )
        return result
    except BusinessError as exc:
        code = exc.code
    except Exception:
        code = "write_state_invalid"
    return {
        "mode": MODE,
        "entrypoint": "direct",
        "operation_id": operation_id,
        "status": "failed",
        "error": code,
        "message": LOCAL_ERRORS.get(code, error_message(code)),
        "upstream": "real_t100",
        "upstream_attempted": False,
        "real_identity_verified": False,
        "business_effect_verified": False,
    }


class PlanAccess:
    def __init__(self, state, root, operation_id, *, expected_binding=None):
        self.state, self.root, self.operation_id = state, root, operation_id
        self.expected_binding = expected_binding

    def _authorize(self, caller):
        saved, contract, binding = _load(self.state, self.root, self.operation_id)
        if self.expected_binding is not None and binding != self.expected_binding:
            raise BusinessError("authorization_changed")
        if caller != Caller(issuer=ISSUER, subject=self.operation_id, client_id=CLIENT):
            raise BusinessError("permission_denied")
        _allowed(saved, contract)
        # 此处才读取管理员 key；计划预览、令牌验签和前置拒绝不会读取它。
        try:
            credential = _read_private(self.state.config.credential_file, 16_384).decode().strip()
            if not credential or any(ord(c) < 32 or ord(c) == 127 for c in credential):
                raise ValueError()
        except Exception:
            raise BusinessError("credential_unavailable") from None
        return PasswordResetGrant(
            employee_id=saved.plan.employee_id,
            account_id=saved.plan.account_id,
            binding_version=binding,
            credential=SecretStr(credential),
        )

    async def authorize(self, caller):
        return await anyio.to_thread.run_sync(self._authorize, caller)


class BoundStore:
    """一个计划只允许一个操作 ID，不能换 UUID 重放同一计划。"""

    def __init__(self, store, operation_id):
        self.store, self.operation_id = store, operation_id

    async def reserve(self, **values):
        if values.get("operation_id") != self.operation_id:
            raise BusinessError("journal_unavailable")
        await self.store.reserve(**values)

    async def mark_dispatched(self, **values):
        await self.store.mark_dispatched(**values)

    async def finish(self, **values):
        await self.store.finish(**values)


class ObservedWriter:
    def __init__(self, writer):
        self.writer = writer
        self.attempted = False

    async def reset(self, grant, **kwargs):
        self.attempted = True
        return await self.writer.reset(grant, **kwargs)


def _initialization_lock(root):
    parent = _directory(root / "audit")
    descriptor = None
    try:
        descriptor = os.open(
            "initialize.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise ValueError()
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        os.close(parent)


async def _prepare_store(store, root):
    # 只串行首次建库/模式检查，不持锁等待上游，也不串行不同员工的业务调用。
    descriptor = await anyio.to_thread.run_sync(_initialization_lock, root)
    try:
        await store.prepare()
    finally:
        os.close(descriptor)


async def _rpc(client, method, params, identifier):
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if identifier is not None:
        body["id"] = identifier
    response = await client.post("/mcp", json=body)
    if response.status_code in {401, 403}:
        raise BusinessError("unauthenticated")
    if identifier is None and response.status_code == 202:
        return None
    if response.status_code != 200:
        raise BusinessError("write_call_failed")
    body = response.json()
    if body.get("id") != identifier or not isinstance(body.get("result"), dict):
        raise BusinessError("write_call_failed")
    return body["result"]


def _error(result):
    for item in result.get("content", []):
        text = item.get("text", "")
        try:
            value = json.loads(text)
            code = value.get("error") if isinstance(value, dict) else None
        except (ValueError, TypeError):
            code = text
        if isinstance(code, str) and code in RESET_ERRORS | LOCAL_ERRORS.keys():
            return code
    return "write_call_failed"


async def call_plan(state, root, operation_id, *, confirm=False):
    result = {
        "mode": MODE,
        "operation_id": operation_id,
        "real_identity_verified": False,
        "upstream": "real_t100",
        "upstream_attempted": False,
        "business_effect_verified": False,
    }
    store = None
    writer = None
    try:
        if confirm is not True:
            raise BusinessError("confirmation_required")
        saved, contract, expected_binding = _load(state, root, operation_id)
        _allowed(saved, contract)
        tool, scope = PROFILES[saved.plan.action]
        result.update(
            action=saved.plan.action,
            upstream_profile=saved.upstream_profile,
            enterprise_id=saved.enterprise_id,
            requested_employee_no=saved.plan.employee_id,
            requested_account_id=saved.plan.account_id,
            expected_account_status=saved.plan.account_status,
        )
        store = SQLiteAccountWriteStore(
            root / "audit" / "account-write.sqlite3",
            enterprise_id=saved.enterprise_id,
            action=saved.plan.action,
        )
        await _prepare_store(store, root)
        # 临时测试身份在内存签发；工具调用不传员工号、账号或状态。
        pair = RSAKeyPair(
            private_key=SecretStr(_read_private(root / "issuer-private.pem", 16384).decode()),
            public_key=_read_private(root / "issuer-public.pem", 16384).decode(),
        )
        token = pair.create_token(
            subject=operation_id,
            issuer=ISSUER,
            audience=AUDIENCE,
            scopes=[scope],
            expires_in_seconds=120,
            additional_claims={"azp": CLIENT},
        )
        is_employee_status = saved.upstream_profile == EMPLOYEE_PROFILE
        client_type = T100EmployeeStatusClient if is_employee_status else T100AccountWriteClient
        client_plan = (
            EmployeeStatusPlan.model_validate(saved.plan.model_dump())
            if is_employee_status
            else saved.plan
        )
        writer = ObservedWriter(client_type(state.config.t100, client_plan))
        factory = create_employee_status_app if is_employee_status else create_account_write_app
        app = factory(
            auth=JWTVerifier(
                public_key=pair.public_key,
                issuer=ISSUER,
                audience=AUDIENCE,
                required_scopes=[scope],
            ),
            identity=IdentityProfile(
                issuer=ISSUER, audience=AUDIENCE, allowed_clients=frozenset({CLIENT})
            ),
            access=PlanAccess(state, root, operation_id, expected_binding=expected_binding),
            writer=writer,
            store=BoundStore(store, operation_id),
            action=saved.plan.action,
        )
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://127.0.0.1",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": PROTOCOL,
                },
            ) as client:
                initialized = await _rpc(
                    client,
                    "initialize",
                    {
                        "protocolVersion": PROTOCOL,
                        "capabilities": {},
                        "clientInfo": {"name": "t100-local-write", "version": "1"},
                    },
                    1,
                )
                if initialized.get("protocolVersion") != PROTOCOL:
                    raise BusinessError("write_call_failed")
                await _rpc(client, "notifications/initialized", {}, None)
                tools = await _rpc(client, "tools/list", {}, 2)
                if [item["name"] for item in tools.get("tools", [])] != [tool]:
                    raise BusinessError("tool_not_available")
                reply = await _rpc(
                    client,
                    "tools/call",
                    {"name": tool, "arguments": {"confirm": True, "operation_id": operation_id}},
                    3,
                )
        if reply.get("isError"):
            raise BusinessError(_error(reply))
        payload = reply.get("structuredContent")
        expected_status = "business_verified" if is_employee_status else "upstream_acknowledged"
        if payload != {"operation_id": operation_id, "status": expected_status}:
            raise BusinessError("write_call_failed")
        result.update(
            status=expected_status, protocol=PROTOCOL, upstream_profile=saved.upstream_profile
        )
        if is_employee_status:
            if not writer.writer.effect_verified:
                raise BusinessError("write_call_failed")
            result.update(
                business_effect_verified=True,
                observed_account_status=writer.writer.observed_status,
            )
    except BusinessError as exc:
        result.update(status="failed", error=exc.code)
        result["message"] = LOCAL_ERRORS.get(exc.code, error_message(exc.code))
    except Exception:
        result.update(
            status="failed", error="write_call_failed", message=LOCAL_ERRORS["write_call_failed"]
        )
    if store is not None:
        result.update(_evidence(root, operation_id))
    if result.get("business_effect_verified") and result.get("operation_state") != "succeeded":
        result.update(status="failed", error="journal_unavailable", business_effect_verified=False)
        result.pop("observed_account_status", None)
    result["upstream_attempted"] = writer.attempted if writer else False
    if writer is not None and hasattr(writer.writer, "write_attempted"):
        result.update(
            write_upstream_attempted=writer.writer.write_attempted,
            read_attempts=writer.writer.read_attempts,
        )
        if writer.attempted:
            # 原生写审计保留状态机；此私有终态另存真实读写阶段与观测证据。
            _save_status_result(
                root, operation_id, result, diagnostics=writer.writer.write_diagnostics
            )
    return result


def _save_status_result(root, operation_id, result, *, diagnostics):
    path = None
    try:
        folder = root / "status-results"
        _mkdir(folder)
        os.close(_directory(folder))
        path = folder / (operation_id + "-" + str(uuid4()) + ".json")
        result.update(evidence_path=str(path), recorded_at=datetime.now(UTC).isoformat())
        _write_new(
            path,
            json.dumps(
                {**result, "upstream_diagnostics": diagnostics}, ensure_ascii=False
            ).encode(),
        )
    except Exception:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
                parent = _directory(path.parent)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            except OSError:
                pass
        result.update(
            status="failed",
            error="journal_unavailable",
            message=error_message("journal_unavailable"),
            business_effect_verified=False,
        )
        result.pop("observed_account_status", None)
        result.pop("evidence_path", None)


def _evidence(root, operation_id):
    path = root / "audit" / "account-write.sqlite3"
    try:
        _metadata(path, 1_000_000_000)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT state,code FROM reset_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        return {
            "audit_path": str(path),
            "operation_state": row[0] if row else None,
            # dispatched 是可能已派发；终态未知时不宣称远端收到请求。
            "recorded_possible_dispatch": row[0] not in {"started", "not_sent"} if row else False,
            "upstream_error": row[1] if row else None,
        }
    except Exception:
        return {
            "audit_path": str(path),
            "recorded_possible_dispatch": None,
            "operation_state": "unavailable",
        }


def _readback_context(state, root, operation_id):
    """只读核对原写入的真实目标与当前许可，不创建或修改操作库。"""
    return _operation_read_context(state, root, operation_id, pending_review=False)


def _review_context(state, root, operation_id):
    return _operation_read_context(state, root, operation_id, pending_review=True)


def _operation_read_context(state, root, operation_id, *, pending_review):
    saved, contract, binding = _load(state, root, operation_id)
    _allowed(saved, contract, readback=True)
    unavailable = "review_not_available" if pending_review else "readback_not_available"
    if pending_review and (
        saved.upstream_profile != EMPLOYEE_PROFILE
        or saved.plan.action not in {"enable_account", "disable_account"}
    ):
        raise BusinessError(unavailable)
    path = root / "audit" / "account-write.sqlite3"
    try:
        _metadata(path, 1_000_000_000)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            if db.execute("PRAGMA application_id").fetchone()[0] != 0x53594131:
                raise BusinessError("readback_binding_mismatch")
            row = db.execute(
                "SELECT * FROM reset_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        expected_state = "unknown" if pending_review else "succeeded"
        if row is None or row["state"] != expected_state:
            raise BusinessError(unavailable)
        expected = {
            "enterprise_id": saved.enterprise_id,
            "employee_id": saved.plan.employee_id,
            "account_id": saved.plan.account_id,
            "tool_name": PROFILES[saved.plan.action][0],
            "issuer": ISSUER,
            "subject": operation_id,
            "client_id": CLIENT,
            "binding_version": binding,
        }
        if not pending_review:
            expected["code"] = None
        if any(row[key] != value for key, value in expected.items()):
            raise BusinessError("readback_binding_mismatch")
        return saved, binding, row["updated_at"]
    except BusinessError:
        raise
    except Exception:
        raise BusinessError(unavailable) from None


async def verify_plan(state, root, operation_id):
    """回读已 ACK 操作的当前账号状态；一次查询、零写入，不改原操作终态。"""
    return await _observe_plan(state, root, operation_id, pending_review=False)


async def review_plan(state, root, operation_id):
    """为未知状态写入留存只读核查证据；观测不是原请求终结证明，不释放占用。"""
    return await _observe_plan(state, root, operation_id, pending_review=True)


async def _observe_plan(state, root, operation_id, *, pending_review):
    context = _review_context if pending_review else _readback_context
    result = {
        "mode": "live-t100-pending-write-review"
        if pending_review
        else "live-t100-account-status-readback",
        "operation_id": operation_id,
        "verification_scope": "account_status",
        "upstream": "real_t100",
        "service": "employee.list.data.query.get",
        "upstream_attempted": False,
        "write_upstream_attempted": False,
        "real_identity_verified": False,
        "account_state_matches": False,
        "business_effect_verified": False,
    }
    evidence_path = None
    try:
        if pending_review:
            # 核查只校验现有库，不调用 prepare，不迁移、创建或修改操作审计。
            saved, _, _ = _load(state, root, operation_id)
            store = SQLiteAccountWriteStore(
                root / "audit" / "account-write.sqlite3",
                enterprise_id=saved.enterprise_id,
                action=saved.plan.action,
            )
            await store.check_ready()
        saved, binding, acknowledged_at = await anyio.to_thread.run_sync(
            context, state, root, operation_id
        )
        read_binding = await anyio.to_thread.run_sync(_base_permission, state)
        result.update(
            action=saved.plan.action,
            enterprise_id=saved.enterprise_id,
            requested_employee_no=saved.plan.employee_id,
            requested_account_id=saved.plan.account_id,
            expected_account_status=saved.plan.account_status,
        )
        if pending_review:
            result.update(
                operation_state="unknown",
                pending_review_retained=True,
                original_operation_updated_at=acknowledged_at,
                next_step="upstream_execution_review_required",
            )
        else:
            result["write_acknowledged_at"] = acknowledged_at
        folder = root / ("reviews" if pending_review else "readbacks")
        _mkdir(folder)
        os.close(_directory(folder))
        identifier = str(uuid4())
        evidence_path = folder / (identifier + ".json")
        # 在任何上游请求前留下关联事实；完成文件独立落盘，未完成不能判成功。
        _write_new(
            folder / (identifier + ".started.json"),
            json.dumps(
                {**result, "status": "started", "started_at": datetime.now(UTC).isoformat()}
            ).encode(),
        )
        # 权限快照在准备期间改变时，不派发查询。
        before = await anyio.to_thread.run_sync(context, state, root, operation_id)
        if (
            before[1] != binding
            or before[2] != acknowledged_at
            or await anyio.to_thread.run_sync(_base_permission, state) != read_binding
        ):
            raise BusinessError("authorization_changed")
        result["upstream_attempted"] = None
        read = await call_employee(state, saved.plan.employee_id)
        result.update(
            upstream_attempted=read.get("upstream_attempted"),
            read_request_id=read.get("request_id"),
            read_audit_path=read.get("audit_path"),
            protocol=read.get("protocol"),
        )
        # 即使查询已完成，撤权或改绑后也不交付状态。
        current = await anyio.to_thread.run_sync(context, state, root, operation_id)
        if (
            current[1] != binding
            or current[2] != acknowledged_at
            or await anyio.to_thread.run_sync(_base_permission, state) != read_binding
        ):
            raise BusinessError("authorization_changed")
        if read.get("status") != "passed":
            raise BusinessError(read.get("error") or "live_read_failed")
        if (
            read.get("remote_response_validated") is not True
            or read.get("upstream_attempted") is not True
            or read.get("protocol") != PROTOCOL
        ):
            raise BusinessError("upstream_contract_error")
        try:
            parsed = QueryResult.model_validate(read.get("result"))
        except Exception:
            raise BusinessError("upstream_contract_error") from None
        if parsed.data.employee_id != saved.plan.employee_id:
            raise BusinessError("upstream_employee_mismatch")
        if parsed.request_id != read.get("request_id"):
            raise BusinessError("upstream_contract_error")
        observed = parsed.data.employee_status
        if observed not in {"Y", "N"}:
            raise BusinessError("upstream_contract_error")
        matches = observed == saved.plan.account_status
        result.update(
            status="reviewed" if pending_review else ("verified" if matches else "mismatch"),
            observed_account_status=observed,
            account_state_matches=matches,
            business_effect_verified=not pending_review
            and matches
            and saved.plan.action in {"disable_account", "enable_account"},
            observed_at=datetime.now(UTC).isoformat(),
        )
        if not matches and not pending_review:
            result.update(
                error="account_status_mismatch", message=LOCAL_ERRORS["account_status_mismatch"]
            )
    except Exception as exc:
        code = exc.code if isinstance(exc, BusinessError) else "journal_unavailable"
        result.update(
            status="failed",
            error=code,
            message=LOCAL_ERRORS.get(code, error_message(code)),
            account_state_matches=False,
            business_effect_verified=False,
        )
        result.pop("observed_account_status", None)
    if evidence_path is not None:
        try:
            result["evidence_path"] = str(evidence_path)
            _write_new(evidence_path, json.dumps(result, ensure_ascii=False).encode())
        except Exception:
            # write/flush/fsync 可能在创建文件后失败；不留下可误读为完成的成功记录。
            try:
                evidence_path.unlink(missing_ok=True)
                parent = _directory(evidence_path.parent)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            except OSError:
                pass
            result.update(
                status="failed",
                error="journal_unavailable",
                message=error_message("journal_unavailable"),
                account_state_matches=False,
                business_effect_verified=False,
            )
            result.pop("observed_account_status", None)
            result.pop("evidence_path", None)
    return result


def parser():
    root = argparse.ArgumentParser(description="T100 真实账号维护：本地临时身份、独立写入口")
    root.add_argument("--read-state-dir", type=Path, default=DEFAULT_STATE)
    # 固定写库位置；不能通过换 CLI 运行目录绕过未知结果占用。
    root.set_defaults(state_dir=ROOT)
    sub = root.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="保存并预览计划；不调用上游、不读取服务 key")
    plan.add_argument("action", choices=["reset-password", "disable-account", "enable-account"])
    plan.add_argument("--employee-no", required=True)
    plan.add_argument("--account-id", required=True)
    plan.add_argument("--account-status", choices=["Y", "N"], help="重置必须明确；禁用固定 N")
    plan.add_argument("--existing-account-confirmed", action="store_true")
    for action in ("reset-password", "disable-account", "enable-account"):
        direct = sub.add_parser(action, help="直接调用真实上游，无需预览步骤")
        direct.add_argument("--employee-no", required=True)
        direct.add_argument("--account-id", required=True)
        direct.add_argument("--operation-id", required=True, help="本次操作 UUID；重试必须沿用")
        direct.add_argument(
            "--account-status", choices=["Y", "N"], required=action == "reset-password"
        )
        direct.add_argument("--confirm", action="store_true")
        direct.add_argument("--existing-account-confirmed", action="store_true")
    for command in ("show", "call", "status", "verify", "review"):
        item = sub.add_parser(command)
        item.add_argument("--operation-id", required=True)
        if command == "call":
            item.add_argument("--confirm", action="store_true")
    return root


def main():
    args = parser().parse_args()
    logging.disable(logging.CRITICAL)
    try:
        state = load_live(args.read_state_dir)
        if args.command == "plan":
            if args.action == "reset-password" and args.account_status is None:
                parser().error("reset-password 必须明确 --account-status Y 或 N；不会默认启用账号")
            result = prepare_plan(
                state,
                args.state_dir,
                action=args.action.replace("-", "_"),
                employee_no=args.employee_no,
                account_id=args.account_id,
                account_status=args.account_status,
                existing_account_confirmed=args.existing_account_confirmed,
                upstream_profile=LEGACY_PROFILE
                if args.action == "reset-password"
                else EMPLOYEE_PROFILE,
            )
        elif args.command in {"reset-password", "disable-account", "enable-account"}:
            result = asyncio.run(
                execute_direct(
                    state,
                    args.state_dir,
                    action=args.command.replace("-", "_"),
                    employee_no=args.employee_no,
                    account_id=args.account_id,
                    account_status=args.account_status,
                    operation_id=args.operation_id,
                    confirm=args.confirm,
                    existing_account_confirmed=args.existing_account_confirmed,
                    upstream_profile=LEGACY_PROFILE
                    if args.command == "reset-password"
                    else EMPLOYEE_PROFILE,
                )
            )
        elif args.command == "verify":
            result = asyncio.run(verify_plan(state, args.state_dir, args.operation_id))
        elif args.command == "review":
            result = asyncio.run(review_plan(state, args.state_dir, args.operation_id))
        elif args.command == "status":
            from live_write_status import inspect_status

            result = asyncio.run(inspect_status(state, args.state_dir, args.operation_id))
        elif args.command == "call":
            result = asyncio.run(
                call_plan(state, args.state_dir, args.operation_id, confirm=args.confirm)
            )
        else:
            result = preview(state, args.state_dir, args.operation_id)
    except BusinessError as exc:
        result = {
            "status": "failed",
            "error": exc.code,
            "message": LOCAL_ERRORS.get(exc.code, error_message(exc.code)),
        }
    except Exception:
        result = {
            "status": "failed",
            "error": "write_state_invalid",
            "message": LOCAL_ERRORS["write_state_invalid"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") in {"failed", "mismatch", "blocked"} else 0
