"""本地账号维护命令用合成资料验证原生身份链；只替换最外层上游 HTTP。"""

import asyncio
import importlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from test_live_read import live as live
from test_live_read import write

from biz_t100.models import BusinessError, Caller

ACTIONS = ("reset_password", "disable_account")


class WriteCase:
    def __init__(self, live, folder, monkeypatch):
        self.host = importlib.import_module("live_write")
        self.state = live[1]
        self.root = folder / "writes"
        self.requests = []
        self.key_reads = []
        self.mode = 200
        self.after_request = None
        original_reader = self.host._read_private
        original_writer = self.host.T100AccountWriteClient

        def read(path, *args):
            if Path(path) == self.state.config.credential_file:
                self.key_reads.append(path)
            return original_reader(path, *args)

        def writer(config, plan):
            return original_writer(config, plan, transport=httpx.MockTransport(self.upstream))

        async def no_network(*args, **kwargs):
            pytest.fail("live_write tests must never open a real network transport")

        monkeypatch.setattr(self.host, "_read_private", read)
        monkeypatch.setattr(self.host, "T100AccountWriteClient", writer)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)

    def plan(self, action="disable_account", **changes):
        values = {
            "action": action,
            "employee_no": "E001",
            "account_id": "LOGIN-A",
            "account_status": "N",
            "existing_account_confirmed": True,
            **changes,
        }
        return self.host.prepare_plan(self.state, self.root, **values)["operation_id"]

    def confirm_contract(self, **changes):
        write(
            self.root / "contract.json",
            {
                "omitted_details_preserved": True,
                "evidence_reference": "synthetic-contract-review-v1",
                # 仅本测试私有 tmp 中开启；上游已被捕获传输替换且禁止外联。
                "internal_test_write_enabled": True,
                **changes,
            },
        )

    def change_plan(self, operation_id, **changes):
        path = self.root / "plans" / (operation_id + ".json")
        body = json.loads(path.read_text())
        body.update(changes)
        write(path, body)

    async def call(self, operation_id, *, confirm=True):
        return await self.host.call_plan(self.state, self.root, operation_id, confirm=confirm)

    async def upstream(self, request):
        self.requests.append(json.loads(request.content))
        if self.after_request:
            await self.after_request()
        if self.mode == "timeout":
            raise httpx.ReadTimeout("synthetic-secret-never-echoed", request=request)
        return httpx.Response(
            self.mode, json={"payload": {"std_data": {"execution": {"code": "0"}}}}
        )

    def rows(self):
        path = self.root / "audit" / "account-write.sqlite3"
        if not path.exists():
            return []
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM reset_operations")]


@pytest.fixture
def case(live, tmp_path, monkeypatch):
    return WriteCase(live, tmp_path, monkeypatch)


@pytest.mark.parametrize("action", ACTIONS)
def test_preview_never_reads_service_key_or_dispatches_and_is_private(case, action):
    original_read_policy = case.state.config.policy_file.read_bytes()
    original_read_issuer = (case.state.root / "issuer-public.pem").read_bytes()
    operation_id = case.plan(action)
    preview = case.host.preview(case.state, case.root, operation_id)
    assert preview["status"] == "planned"
    assert preview["upstream_attempted"] is False
    assert preview["blocker"] == "write_contract_unconfirmed"
    assert preview["ready_for_explicit_call"] is False
    assert case.key_reads == []
    assert case.requests == []
    assert case.rows() == []
    assert case.state.config.policy_file.read_bytes() == original_read_policy
    assert (case.state.root / "issuer-public.pem").read_bytes() == original_read_issuer
    assert (case.root / "issuer-public.pem").read_bytes() != original_read_issuer
    assert case.root.stat().st_mode & 0o777 == 0o700
    for path in case.root.rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    assert "synthetic-private-key" not in json.dumps(preview)
    assert "PRIVATE KEY" not in json.dumps(preview)


@pytest.mark.parametrize("action,status", [("reset_password", None), ("disable_account", "Y")])
def test_invalid_status_rejected_before_private_plan_exists(case, action, status):
    with pytest.raises(BusinessError, match="^invalid_arguments$"):
        case.plan(action, account_status=status)
    assert case.key_reads == []
    assert case.requests == []
    assert not case.root.exists()


def test_disable_without_status_means_only_n(case):
    operation_id = case.plan(account_status=None)
    preview = case.host.preview(case.state, case.root, operation_id)
    assert preview["parameter"]["master"][0]["gzxastus"] == "N"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("confirm", "confirmation_required"),
        ("existing-account", "existing_account_unconfirmed"),
        ("contract-default", "write_contract_unconfirmed"),
        ("contract-false", "write_contract_unconfirmed"),
        ("contract-no-evidence", "write_contract_unconfirmed"),
        ("disabled", "permission_denied"),
    ],
)
async def test_missing_readiness_is_zero_key_reads_and_zero_requests(case, kind, expected):
    operation_id = case.plan(existing_account_confirmed=kind != "existing-account")
    if kind != "contract-default":
        case.confirm_contract(
            omitted_details_preserved=kind != "contract-false",
            evidence_reference=" " if kind == "contract-no-evidence" else "synthetic-reviewed",
        )
    if kind == "disabled":
        case.change_plan(operation_id, enabled=False)
    result = await case.call(operation_id, confirm=kind != "confirm")
    assert result["status"] == "failed"
    assert result["error"] == expected
    assert result["upstream_attempted"] is False
    assert case.requests == []
    assert case.key_reads == []
    assert case.rows() == []


@pytest.mark.parametrize(
    "action,status", [("reset_password", "Y"), ("reset_password", "N"), ("disable_account", "N")]
)
async def test_native_mcp_exact_write_body_and_ack_without_business_success(case, action, status):
    operation_id = case.plan(action, account_status=status)
    case.confirm_contract()
    result = await case.call(operation_id)
    assert result["status"] == "upstream_acknowledged", result
    assert result["operation_id"] == operation_id
    assert result["protocol"] == "2025-11-25"
    assert result["upstream_attempted"] is True
    assert result["business_effect_verified"] is False
    assert result["real_identity_verified"] is False
    assert len(case.requests) == 1
    body = case.requests[0]
    assert body["key"] == "synthetic-private-key"
    assert body["service"]["name"] == "user.account.create"
    assert body["datakey"] == {"EntId": case.state.config.t100.enterprise_id}
    assert body["payload"]["std_data"]["parameter"] == {
        "master": [
            {
                "gzxa001": "LOGIN-A",
                "gzxa003": "E001",
                "gzxastus": status,
                "is_reproduce": "N",
                "password_reset": "Y" if action == "reset_password" else "N",
            }
        ]
    }
    assert len(case.key_reads) >= 3
    assert len(case.rows()) == 1
    row = case.rows()[0]
    assert row["state"] == "succeeded"
    assert row["tool_name"] == case.host.PROFILES[action][0]
    assert (row["employee_id"], row["account_id"]) == ("E001", "LOGIN-A")
    assert (row["issuer"], row["subject"], row["client_id"]) == (
        case.host.ISSUER,
        operation_id,
        case.host.CLIENT,
    )
    assert row["binding_version"]
    assert "synthetic-private-key" not in json.dumps(result)


async def test_same_uuid_never_replays_or_claims_this_attempt_dispatched(case):
    operation_id = case.plan("reset_password")
    case.confirm_contract()
    first = await case.call(operation_id)
    assert first["status"] == "upstream_acknowledged"
    second = await case.call(operation_id)
    assert second["error"] == "reset_already_recorded"
    assert second["upstream_attempted"] is False
    assert len(case.requests) == 1
    assert len(case.rows()) == 1


@pytest.mark.parametrize("mode", [401, 403, 500, "timeout"])
async def test_unknown_blocks_new_uuid_even_for_other_action(case, mode):
    first_id = case.plan("reset_password")
    second_id = case.plan("disable_account")
    case.confirm_contract()
    case.mode = mode
    first = await case.call(first_id)
    assert first["error"] == "reset_outcome_unknown", first
    assert first["upstream_attempted"] is True
    assert first["business_effect_verified"] is False
    second = await case.call(second_id)
    assert second["error"] == "reset_pending_review", second
    assert second["upstream_attempted"] is False
    assert len(case.requests) == 1
    assert case.rows()[0]["state"] == "unknown"
    assert len(case.rows()) == 1
    assert "synthetic-secret-never-echoed" not in json.dumps(first)


async def test_two_employees_concurrent_native_chains_keep_exact_targets(case):
    first_id = case.plan("reset_password", employee_no="E001", account_id="LOGIN-A")
    second_id = case.plan("disable_account", employee_no="E002", account_id="LOGIN-B")
    case.confirm_contract()
    # 首次并发也必须分别完成，不借助测试提前创建共享库。
    assert not (case.root / "audit" / "account-write.sqlite3").exists()
    gate = asyncio.Event()

    async def both_arrive():
        if len(case.requests) == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), timeout=3)

    case.after_request = both_arrive
    results = await asyncio.gather(case.call(first_id), case.call(second_id))
    assert [result["status"] for result in results] == ["upstream_acknowledged"] * 2, results
    assert len(case.requests) == 2
    assert {
        (
            request["datakey"]["EntId"],
            request["payload"]["std_data"]["parameter"]["master"][0]["gzxa003"],
            request["payload"]["std_data"]["parameter"]["master"][0]["gzxa001"],
            request["payload"]["std_data"]["parameter"]["master"][0]["password_reset"],
        )
        for request in case.requests
    } == {
        (case.state.config.t100.enterprise_id, "E001", "LOGIN-A", "Y"),
        (case.state.config.t100.enterprise_id, "E002", "LOGIN-B", "N"),
    }
    assert {row["operation_id"] for row in case.rows()} == {first_id, second_id}


@pytest.mark.parametrize("change", ["disable", "rebind", "contract"])
async def test_revocation_or_rebinding_after_upstream_never_returns_ack(case, change):
    operation_id = case.plan()
    case.confirm_contract()

    async def revoke():
        if change == "disable":
            case.change_plan(operation_id, enabled=False)
        elif change == "rebind":
            saved, _, _ = case.host._load(case.state, case.root, operation_id)
            changed = saved.plan.model_dump()
            changed["employee_id"] = "E002"
            changed["account_id"] = "LOGIN-B"
            case.change_plan(operation_id, plan=changed)
        else:
            case.confirm_contract(omitted_details_preserved=False)

    case.after_request = revoke
    result = await case.call(operation_id)
    assert result["error"] == "reset_outcome_unknown", result
    assert result["business_effect_verified"] is False
    assert result["upstream_attempted"] is True
    assert len(case.requests) == 1
    assert case.rows()[0]["state"] == "unknown"
    assert (case.rows()[0]["employee_id"], case.rows()[0]["account_id"]) == ("E001", "LOGIN-A")


@pytest.mark.parametrize("claim", ["issuer", "audience", "subject", "client", "scope", "forged"])
async def test_wrong_signed_identity_never_reads_service_key_or_dispatches(
    case, monkeypatch, claim
):
    operation_id = case.plan()
    case.confirm_contract()
    original = RSAKeyPair.create_token
    other = RSAKeyPair.generate() if claim == "forged" else None

    def altered(pair, **values):
        if claim in {"issuer", "audience", "subject"}:
            values[claim] = "wrong-synthetic-identity"
        elif claim == "client":
            values["additional_claims"] = {"azp": "wrong-client"}
        elif claim == "scope":
            values["scopes"] = ["t100:employee:read:self"]
        return original(other or pair, **values)

    monkeypatch.setattr(RSAKeyPair, "create_token", altered)
    result = await case.call(operation_id)
    assert result["status"] == "failed", result
    assert result["upstream_attempted"] is False
    assert case.key_reads == []
    assert case.requests == []
    assert case.rows() == []


async def test_signed_employee_and_account_claims_cannot_override_plan(case, monkeypatch):
    operation_id = case.plan()
    case.confirm_contract()
    original = RSAKeyPair.create_token

    def altered(pair, **values):
        values["additional_claims"] |= {"employee_no": "E999", "account_id": "OTHER-LOGIN"}
        return original(pair, **values)

    monkeypatch.setattr(RSAKeyPair, "create_token", altered)
    result = await case.call(operation_id)
    assert result["status"] == "upstream_acknowledged", result
    master = case.requests[0]["payload"]["std_data"]["parameter"]["master"][0]
    assert (master["gzxa003"], master["gzxa001"]) == ("E001", "LOGIN-A")


async def test_access_rechecks_permission_before_every_service_key_read(case):
    operation_id = case.plan()
    case.confirm_contract()
    access = case.host.PlanAccess(case.state, case.root, operation_id)
    caller = Caller(issuer=case.host.ISSUER, subject=operation_id, client_id=case.host.CLIENT)
    grant = await access.authorize(caller)
    assert grant.employee_id == "E001" and grant.account_id == "LOGIN-A"
    assert len(case.key_reads) == 1
    case.change_plan(operation_id, enabled=False)
    with pytest.raises(BusinessError, match="^permission_denied$"):
        await access.authorize(caller)
    assert len(case.key_reads) == 1


async def test_saved_plan_cannot_be_run_against_changed_enterprise_or_endpoint(case):
    operation_id = case.plan()
    case.confirm_contract()
    changed = case.state.config.t100.model_copy(update={"enterprise_id": "different-enterprise"})
    state = case.state.__class__(
        **{**case.state.__dict__, "config": case.state.config.model_copy(update={"t100": changed})}
    )
    result = await case.host.call_plan(state, case.root, operation_id, confirm=True)
    assert result["error"] == "write_state_invalid"
    assert case.key_reads == []
    assert case.requests == []


async def test_bound_plan_cannot_be_replayed_with_another_operation_id(case):
    operation_id = case.plan()
    case.confirm_contract()
    inner = case.host.SQLiteAccountWriteStore(
        case.root / "audit" / "account-write.sqlite3",
        enterprise_id=case.state.config.t100.enterprise_id,
        action="disable_account",
    )
    bound = case.host.BoundStore(inner, operation_id)
    with pytest.raises(BusinessError, match="^journal_unavailable$"):
        await bound.reserve(
            operation_id=str(uuid4()),
            caller=Caller(
                issuer=case.host.ISSUER, subject=operation_id, client_id=case.host.CLIENT
            ),
            employee_id="E001",
            account_id="LOGIN-A",
            binding_version="synthetic-version",
        )
    assert case.requests == []
    assert case.rows() == []


@pytest.mark.parametrize("change", ["status", "action", "contract", "employee", "account"])
async def test_plan_changes_after_initial_load_rejected_before_key_and_dispatch(
    case, monkeypatch, change
):
    operation_id = case.plan("reset_password", account_status="Y")
    case.confirm_contract()
    original = case.host._load
    loads = 0

    def changed_after_snapshot(*args, **kwargs):
        nonlocal loads
        result = original(*args, **kwargs)
        loads += 1
        if loads == 1:
            saved = result[0]
            altered = saved.plan.model_dump()
            if change == "status":
                altered["account_status"] = "N"
            elif change == "action":
                altered.update(action="disable_account", account_status="N")
            elif change == "employee":
                altered["employee_id"] = "E002"
            elif change == "account":
                altered["account_id"] = "LOGIN-B"
            if change == "contract":
                case.confirm_contract(evidence_reference="different-synthetic-review")
            else:
                case.change_plan(operation_id, plan=altered)
        return result

    monkeypatch.setattr(case.host, "_load", changed_after_snapshot)
    result = await case.call(operation_id)
    assert result["error"] == "authorization_changed", result
    assert result["upstream_attempted"] is False
    assert case.requests == []
    assert case.key_reads == []
    assert case.rows() == []


@pytest.mark.parametrize("kind", ["shared-permissions", "symlink"])
async def test_invalid_initialization_lock_rejects_before_key_and_database(case, kind):
    operation_id = case.plan()
    case.confirm_contract()
    lock = case.root / "audit" / "initialize.lock"
    target = case.root / "lock-target"
    if kind == "shared-permissions":
        lock.write_text("synthetic-existing-lock")
        lock.chmod(0o644)
        before = lock.read_bytes()
    else:
        write(target, "synthetic-protected-content")
        lock.symlink_to(target)
        before = target.read_bytes()
    result = await case.call(operation_id)
    assert result["status"] == "failed", result
    assert result["upstream_attempted"] is False
    assert case.key_reads == []
    assert case.requests == []
    assert not (case.root / "audit" / "account-write.sqlite3").exists()
    assert (target if kind == "symlink" else lock).read_bytes() == before


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("explicit_false", [False, True])
async def test_write_execution_disabled_by_default_or_explicitly_is_zero_io(
    case, action, explicit_false
):
    operation_id = case.plan(action)
    contract = {
        "omitted_details_preserved": True,
        "evidence_reference": "synthetic-reviewed-contract",
    }
    if explicit_false:
        contract["internal_test_write_enabled"] = False
    write(case.root / "contract.json", contract)
    assert case.host.Contract.model_validate(contract).internal_test_write_enabled is False
    preview = case.host.preview(case.state, case.root, operation_id)
    assert preview["ready_for_explicit_call"] is False
    assert preview["blocker"] == "write_execution_not_enabled"
    result = await case.call(operation_id, confirm=True)
    assert result["status"] == "failed"
    assert result["error"] == "write_execution_not_enabled"
    assert result["upstream_attempted"] is False
    assert result["business_effect_verified"] is False
    assert case.key_reads == []
    assert case.requests == []
    assert case.rows() == []
    assert not (case.root / "audit" / "account-write.sqlite3").exists()
