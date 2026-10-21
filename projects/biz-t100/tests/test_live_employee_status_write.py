"""资料状态命令的离线闭环：原生身份、真实装配、捕获上游，禁止外联。"""

import json
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from test_live_write import WriteCase
from test_live_write import live as live

PROFILE = "employee-status-v1"
QUERY = "employee.list.data.query.get"
UPDATE = "employee.data.update"
STATUS_ACTIONS = (("disable_account", "N"), ("enable_account", "Y"))


class EmployeeStatusCase(WriteCase):
    def __init__(self, live, folder, monkeypatch):
        super().__init__(live, folder, monkeypatch)
        self.current_status = "Y"
        self.returned_status = None
        self.missing_employee = False
        self.after_read = None
        self.write_timeout = False
        original = self.host.T100EmployeeStatusClient

        def writer(config, plan, *args, **kwargs):
            assert "transport" not in kwargs
            return original(
                config, plan, *args, transport=httpx.MockTransport(self.upstream), **kwargs
            )

        monkeypatch.setattr(self.host, "T100EmployeeStatusClient", writer)

    def enable(self, **changes):
        self.host.initialize(self.root)
        self.confirm_contract(
            **{
                "employee_update_omissions_preserved": True,
                "employee_status_updates_enabled": True,
                "allowed_targets": [
                    {
                        "employee_id": "E001",
                        "account_id": "LOGIN-A",
                        "actions": ["enable_account", "disable_account", "reset_password"],
                        "account_statuses": ["Y", "N"],
                    }
                ],
                **changes,
            }
        )

    async def direct(self, **changes):
        return await self.host.execute_direct(
            self.state,
            self.root,
            **{
                "action": "disable_account",
                "employee_no": "E001",
                "account_id": "LOGIN-A",
                "account_status": "N",
                "operation_id": str(uuid4()),
                "confirm": True,
                "existing_account_confirmed": True,
                "upstream_profile": PROFILE,
                **changes,
            },
        )

    def sent(self, service):
        return [body for body in self.requests if body["service"]["name"] == service]

    async def upstream(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        service = body["service"]["name"]
        if service == QUERY:
            employees = (
                []
                if self.missing_employee
                else [
                    {
                        "employee_no": "E001",
                        "status": self.returned_status or self.current_status,
                        "full_name": "Synthetic Employee A",
                        "department_no": "SYNTHETIC-DEPT",
                        # 本人绑定独立受控，不用资料中的空 account_no 推定账号。
                        "account_no": "",
                    }
                ]
            )
            if self.after_read is not None:
                await self.after_read()
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "std_data": {
                            "execution": {"code": "0"},
                            "parameter": {"employee": employees},
                        }
                    }
                },
            )
        if service == UPDATE:
            if self.write_timeout:
                raise httpx.ReadTimeout("synthetic-secret-do-not-expose", request=request)
            self.current_status = body["payload"]["std_data"]["parameter"]["employee"][0]["status"]
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "std_data": {
                            "execution": {"code": "0"},
                            "parameter": {
                                "employee": [
                                    {
                                        "source_no": "E001",
                                        "erp_no": "E001",
                                        "execute_description": "",
                                    }
                                ]
                            },
                        }
                    }
                },
            )
        else:
            # 只允许复测已有密码重置/历史计划路径，禁止不明服务静默得到 ACK。
            assert service == "user.account.create"
        return httpx.Response(200, json={"payload": {"std_data": {"execution": {"code": "0"}}}})


@pytest.fixture
def status_case(live, tmp_path, monkeypatch):
    return EmployeeStatusCase(live, tmp_path, monkeypatch)


@pytest.mark.parametrize("action,target", STATUS_ACTIONS)
async def test_status_direct_prechecks_updates_minimal_fields_and_verifies_readback(
    status_case, monkeypatch, action, target
):
    case = status_case
    case.enable()
    case.current_status = "Y" if target == "N" else "N"
    original_policy = case.state.config.policy_file.read_bytes()
    calls = []
    original_rpc = case.host._rpc

    async def capture(client, method, params, identifier):
        if method == "tools/call":
            calls.append(params)
        return await original_rpc(client, method, params, identifier)

    monkeypatch.setattr(case.host, "_rpc", capture)
    operation_id = str(uuid4())
    result = await case.direct(action=action, account_status=target, operation_id=operation_id)
    assert result["status"] == "business_verified", result
    assert result["business_effect_verified"] is True
    assert result["real_identity_verified"] is False
    assert result["write_upstream_attempted"] is True
    assert result["read_attempts"] == 2
    assert result["observed_account_status"] == target
    assert result["protocol"] == "2025-11-25"
    assert [body["service"]["name"] for body in case.requests] == [QUERY, UPDATE, QUERY]
    for body in case.requests:
        assert body["key"] == "synthetic-private-key"
        assert body["datakey"] == {"EntId": case.state.config.t100.enterprise_id}
    for body in case.sent(QUERY):
        assert body["payload"]["std_data"]["parameter"]["employee_no"] == "E001"
    assert case.sent(UPDATE)[0]["payload"]["std_data"]["parameter"] == {
        "is_digiwin_product": "N",
        "employee": [{"employee_no": "E001", "status": target}],
    }
    assert calls == [
        {
            "name": case.host.PROFILES[action][0],
            "arguments": {"operation_id": operation_id, "confirm": True},
        }
    ]
    assert case.rows()[0]["state"] == "succeeded"
    assert case.rows()[0]["tool_name"] == case.host.PROFILES[action][0]
    assert case.state.config.policy_file.read_bytes() == original_policy
    saved_path = case.root / "plans" / f"{operation_id}.json"
    assert json.loads(saved_path.read_text())["upstream_profile"] == PROFILE
    assert saved_path.stat().st_mode & 0o777 == 0o600
    assert "synthetic-private-key" not in json.dumps(result)


@pytest.mark.parametrize("action,target", STATUS_ACTIONS)
@pytest.mark.parametrize(
    "contract",
    [
        {"employee_update_omissions_preserved": False},
        {"employee_status_updates_enabled": False},
        {"internal_test_write_enabled": False},
        {"allowed_targets": []},
        {"evidence_reference": " "},
        {"enterprise_id": "another-synthetic-enterprise"},
    ],
)
async def test_status_contract_or_permission_denial_reads_no_key_and_calls_no_upstream(
    status_case, action, target, contract
):
    case = status_case
    case.enable(**contract)
    result = await case.direct(action=action, account_status=target)
    assert result["status"] == "failed", result
    assert result["business_effect_verified"] is False
    assert case.requests == []
    assert case.key_reads == []
    assert case.rows() == []


async def test_missing_employee_is_one_read_and_zero_writes(status_case):
    case = status_case
    case.enable()
    case.missing_employee = True
    result = await case.direct()
    assert result["status"] == "failed", result
    assert result["business_effect_verified"] is False
    assert result["write_upstream_attempted"] is False
    assert result["read_attempts"] == 1
    assert [body["service"]["name"] for body in case.requests] == [QUERY]
    assert case.sent(UPDATE) == []
    assert case.rows()[0]["state"] == "unknown"


@pytest.mark.parametrize(
    "next_action,next_status", [("enable_account", "Y"), ("reset_password", "N")]
)
async def test_mismatched_readback_retains_unknown_and_blocks_other_actions(
    status_case, next_action, next_status
):
    case = status_case
    case.enable()
    case.returned_status = "Y"
    result = await case.direct()
    assert result["status"] == "failed", result
    assert result["business_effect_verified"] is False
    assert result["write_upstream_attempted"] is True
    assert result["read_attempts"] == 2
    assert len(case.sent(UPDATE)) == 1
    assert len(case.sent(QUERY)) == 2
    assert case.rows()[0]["state"] == "unknown"
    before = len(case.requests)
    second = await case.direct(
        action=next_action,
        account_status=next_status,
        upstream_profile="legacy-account-v1" if next_action == "reset_password" else PROFILE,
    )
    assert second["status"] == "failed", second
    assert second["error"] == "reset_pending_review"
    assert second["business_effect_verified"] is False
    assert len(case.requests) == before
    assert len(case.rows()) == 1


async def test_revocation_while_precheck_is_in_flight_prevents_update(status_case):
    case = status_case
    case.enable()

    async def revoke():
        case.enable(employee_status_updates_enabled=False)

    case.after_read = revoke
    result = await case.direct()
    assert result["status"] == "failed", result
    assert result["business_effect_verified"] is False
    assert result["write_upstream_attempted"] is False
    assert [body["service"]["name"] for body in case.requests] == [QUERY]
    assert case.rows()[0]["state"] == "unknown"


async def test_enable_cannot_use_disable_allowlist(status_case):
    case = status_case
    case.enable(
        allowed_targets=[
            {
                "employee_id": "E001",
                "account_id": "LOGIN-A",
                "actions": ["disable_account"],
                "account_statuses": ["Y", "N"],
            }
        ]
    )
    result = await case.direct(action="enable_account", account_status="Y")
    assert result["status"] == "failed", result
    assert result["error"] == "permission_denied"
    assert case.requests == []
    assert case.key_reads == []


async def test_enable_cannot_use_disable_scope(status_case, monkeypatch):
    case = status_case
    case.enable()
    original = RSAKeyPair.create_token

    def disable_only(pair, **values):
        values["scopes"] = [case.host.PROFILES["disable_account"][1]]
        return original(pair, **values)

    monkeypatch.setattr(RSAKeyPair, "create_token", disable_only)
    result = await case.direct(action="enable_account", account_status="Y")
    assert result["status"] == "failed", result
    assert result["business_effect_verified"] is False
    assert case.requests == []
    assert case.key_reads == []
    assert case.rows() == []


async def test_update_timeout_is_not_retried_and_locks_target(status_case):
    case = status_case
    case.enable()
    case.write_timeout = True
    result = await case.direct()
    assert result["status"] == "failed", result
    assert result["business_effect_verified"] is False
    assert result["write_upstream_attempted"] is True
    assert [body["service"]["name"] for body in case.requests] == [QUERY, UPDATE]
    assert case.rows()[0]["state"] == "unknown"
    assert "synthetic-secret-do-not-expose" not in json.dumps(result)
    second = await case.direct()
    assert second["error"] == "reset_pending_review", second
    assert [body["service"]["name"] for body in case.requests] == [QUERY, UPDATE]


async def test_operation_id_cannot_rebind_historical_write_to_employee_profile(status_case):
    case = status_case
    case.enable()
    operation_id = str(uuid4())
    first = await case.direct(operation_id=operation_id, upstream_profile="legacy-account-v1")
    assert first["status"] == "upstream_acknowledged", first
    path = case.root / "plans" / f"{operation_id}.json"
    before = path.read_bytes()
    key_reads = len(case.key_reads)
    result = await case.direct(operation_id=operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == "operation_conflict"
    assert len(case.requests) == 1
    assert len(case.key_reads) == key_reads
    assert path.read_bytes() == before
    assert len(case.rows()) == 1


@pytest.mark.parametrize(
    "action,profile",
    [
        ("disable_account", PROFILE),
        ("enable_account", PROFILE),
        ("reset_password", "legacy-account-v1"),
    ],
)
def test_cli_selects_employee_status_profile_only_for_status_actions(
    status_case, monkeypatch, capsys, action, profile
):
    case = status_case
    observed = []
    operation_id = str(uuid4())

    async def execute(state, root, **values):
        observed.append(values)
        return {"status": "business_verified", "operation_id": values["operation_id"]}

    arguments = [
        "live_write.py",
        "--read-state-dir",
        str(case.state.root),
        action.replace("_", "-"),
        "--employee-no",
        "E001",
        "--account-id",
        "LOGIN-A",
        "--operation-id",
        operation_id,
        "--confirm",
        "--existing-account-confirmed",
    ]
    if action == "reset_password":
        arguments += ["--account-status", "N"]
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(case.host, "execute_direct", execute)
    assert case.host.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "business_verified"
    assert len(observed) == 1
    assert observed[0]["action"] == action
    assert observed[0]["upstream_profile"] == profile
    assert observed[0]["employee_no"] == "E001"
    assert observed[0]["account_id"] == "LOGIN-A"
    assert observed[0]["operation_id"] == operation_id
    assert case.requests == []


def status_audit_snapshot(case):
    path = case.root / "audit" / "account-write.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        return tuple(
            tuple(connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in ("reset_context", "reset_operations", "reset_events")
        )


def persisted_status(case, result):
    path = Path(result["evidence_path"])
    assert path.parent == case.root / "status-results"
    assert path.name.startswith(result["operation_id"] + "-")
    assert path.suffix == ".json"
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    raw = path.read_text()
    for prohibited in (
        "synthetic-private-key",
        "PRIVATE KEY",
        "Synthetic Employee A",
        "SYNTHETIC-DEPT",
        '"credential"',
        '"token"',
        '"payload"',
    ):
        assert prohibited not in raw
    evidence = json.loads(raw)
    assert evidence["operation_id"] == result["operation_id"]
    assert evidence["upstream_profile"] == PROFILE
    assert evidence["enterprise_id"] == "synthetic-enterprise"
    assert evidence["requested_employee_no"] == "E001"
    assert evidence["requested_account_id"] == "LOGIN-A"
    assert evidence["audit_path"] == str(case.root / "audit" / "account-write.sqlite3")
    assert evidence["recorded_at"]
    assert evidence["real_identity_verified"] is False
    return evidence


@pytest.mark.parametrize("action,target", STATUS_ACTIONS)
async def test_status_success_persists_target_readback_counts_and_private_evidence(
    status_case, action, target
):
    case = status_case
    case.enable()
    operation_id = str(uuid4())
    result = await case.direct(action=action, account_status=target, operation_id=operation_id)
    assert result["status"] == "business_verified", result
    evidence = persisted_status(case, result)
    assert evidence["action"] == action
    assert evidence["expected_account_status"] == target
    assert evidence["observed_account_status"] == target
    assert evidence["status"] == "business_verified"
    assert evidence["operation_state"] == "succeeded"
    assert evidence["business_effect_verified"] is True
    assert evidence["upstream_attempted"] is True
    assert evidence["write_upstream_attempted"] is True
    assert evidence["read_attempts"] == 2
    assert evidence["recorded_possible_dispatch"] is True
    assert evidence["upstream_error"] is None
    assert [body["service"]["name"] for body in case.requests] == [QUERY, UPDATE, QUERY]
    before = status_audit_snapshot(case)
    saved = Path(result["evidence_path"]).read_bytes()
    repeated = await case.direct(action=action, account_status=target, operation_id=operation_id)
    assert repeated["error"] == "reset_already_recorded", repeated
    assert repeated["upstream_attempted"] is False
    assert repeated["write_upstream_attempted"] is False
    assert repeated["read_attempts"] == 0
    assert "evidence_path" not in repeated
    assert status_audit_snapshot(case) == before
    assert Path(result["evidence_path"]).read_bytes() == saved
    assert len(list((case.root / "status-results").iterdir())) == 1
    assert len(case.requests) == 3


@pytest.mark.parametrize(
    "failure,reads,wrote",
    [("missing", 1, False), ("revoked", 1, False), ("timeout", 1, True), ("mismatch", 2, True)],
)
async def test_unknown_result_persists_actual_read_and_write_facts_without_observed_data(
    status_case, failure, reads, wrote
):
    case = status_case
    case.enable()
    if failure == "missing":
        case.missing_employee = True
    elif failure == "revoked":

        async def revoke():
            case.enable(employee_status_updates_enabled=False)

        case.after_read = revoke
    elif failure == "timeout":
        case.write_timeout = True
    else:
        case.returned_status = "Y"
    result = await case.direct()
    assert result["status"] == "failed", result
    evidence = persisted_status(case, result)
    assert evidence["status"] == "failed"
    assert evidence["operation_state"] == "unknown"
    assert evidence["expected_account_status"] == "N"
    assert evidence["business_effect_verified"] is False
    assert evidence["read_attempts"] == reads
    assert evidence["write_upstream_attempted"] is wrote
    assert evidence["upstream_attempted"] is True
    assert "observed_account_status" not in evidence
    assert evidence["error"] == result["error"]
    assert evidence["upstream_error"] == result["upstream_error"]
    assert evidence["recorded_possible_dispatch"] is True
    assert len(case.sent(QUERY)) == reads
    assert len(case.sent(UPDATE)) == int(wrote)
    assert case.rows()[0]["state"] == "unknown"


@pytest.mark.parametrize("phase", ["before_write", "after_visible_write"])
@pytest.mark.parametrize("outcome", ["succeeded", "unknown"])
async def test_status_evidence_failure_never_leaves_success_or_rewrites_business_audit(
    status_case, monkeypatch, phase, outcome
):
    case = status_case
    case.enable()
    if outcome == "unknown":
        case.returned_status = "Y"
    operation_id = str(uuid4())
    original_write = case.host._write_new
    attempted_evidence = []
    audit_at_persistence = []

    def fail_evidence(path, value):
        if path.parent.name != "status-results":
            return original_write(path, value)
        attempted_evidence.append(path)
        audit_at_persistence.append(status_audit_snapshot(case))
        if phase == "after_visible_write":
            original_write(path, value)
            assert path.is_file()
            assert json.loads(path.read_text())["operation_state"] == outcome
        raise OSError("synthetic-status-result-storage-failure")

    monkeypatch.setattr(case.host, "_write_new", fail_evidence)
    result = await case.direct(operation_id=operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == "journal_unavailable"
    assert result["business_effect_verified"] is False
    assert result["operation_state"] == outcome
    assert "observed_account_status" not in result
    assert "evidence_path" not in result
    assert result["write_upstream_attempted"] is True
    assert result["read_attempts"] == 2
    assert result["upstream_attempted"] is True
    assert len(attempted_evidence) == 1
    assert not attempted_evidence[0].exists()
    assert list((case.root / "status-results").iterdir()) == []
    assert status_audit_snapshot(case) == audit_at_persistence[0]
    assert case.rows()[0]["state"] == outcome
    assert [body["service"]["name"] for body in case.requests] == [QUERY, UPDATE, QUERY]
    assert "synthetic-status-result-storage-failure" not in json.dumps(result)
    before = status_audit_snapshot(case)
    repeated = await case.direct(operation_id=operation_id)
    assert repeated["status"] == "failed", repeated
    assert repeated["error"] == "reset_already_recorded"
    assert repeated["upstream_attempted"] is False
    assert status_audit_snapshot(case) == before
    assert len(case.requests) == 3
    assert len(attempted_evidence) == 1
