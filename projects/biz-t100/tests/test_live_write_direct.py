"""单步真实写装配的离线验收：原生 MCP + 捕获上游，禁止实际网络。"""

import asyncio
import json
import sys
from uuid import uuid4

import pytest
from test_live_write import ACTIONS
from test_live_write import case as case
from test_live_write import live as live


async def direct(case, **changes):
    values = {
        "action": "disable_account",
        "employee_no": "E001",
        "account_id": "LOGIN-A",
        "operation_id": str(uuid4()),
        "confirm": True,
        "existing_account_confirmed": True,
        **changes,
    }
    return await case.host.execute_direct(case.state, case.root, **values)


def enable(case):
    case.host.initialize(case.root)
    case.confirm_contract()


@pytest.mark.parametrize(
    "action,status", [("reset_password", "Y"), ("reset_password", "N"), ("disable_account", None)]
)
async def test_direct_runs_native_chain_once_without_separate_plan_step(
    case, monkeypatch, action, status
):
    enable(case)
    original_policy = case.state.config.policy_file.read_bytes()
    original_rpc = case.host._rpc
    calls = []

    async def capture_rpc(client, method, params, identifier):
        if method == "tools/call":
            calls.append(params)
        return await original_rpc(client, method, params, identifier)

    monkeypatch.setattr(case.host, "_rpc", capture_rpc)
    operation_id = str(uuid4())
    result = await direct(case, action=action, account_status=status, operation_id=operation_id)
    assert result["status"] == "upstream_acknowledged", result
    assert result["protocol"] == "2025-11-25"
    assert result["upstream_attempted"] is True
    assert result["real_identity_verified"] is False
    assert result["business_effect_verified"] is False
    assert len(case.requests) == 1
    assert calls == [
        {
            "name": case.host.PROFILES[action][0],
            "arguments": {"confirm": True, "operation_id": operation_id},
        }
    ]
    assert case.requests[0]["payload"]["std_data"]["parameter"] == {
        "master": [
            {
                "gzxa001": "LOGIN-A",
                "gzxa003": "E001",
                "gzxastus": status or "N",
                "is_reproduce": "N",
                "password_reset": "Y" if action == "reset_password" else "N",
            }
        ]
    }
    assert case.state.config.policy_file.read_bytes() == original_policy
    saved_path = case.root / "plans" / f"{operation_id}.json"
    assert saved_path.stat().st_mode & 0o777 == 0o600
    saved = json.loads(saved_path.read_text())
    assert saved["operation_id"] == operation_id
    assert (saved["plan"]["employee_id"], saved["plan"]["account_id"]) == ("E001", "LOGIN-A")
    assert case.rows()[0]["subject"] == operation_id
    assert case.rows()[0]["state"] == "succeeded"
    assert "synthetic-private-key" not in json.dumps(result)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize(
    "changes,contract,expected",
    [
        ({"confirm": False}, {}, "confirmation_required"),
        ({"existing_account_confirmed": False}, {}, "existing_account_unconfirmed"),
        ({}, {"internal_test_write_enabled": False}, "write_execution_not_enabled"),
        ({}, {"omitted_details_preserved": False}, "write_contract_unconfirmed"),
        ({}, {"evidence_reference": " "}, "write_contract_unconfirmed"),
    ],
)
async def test_direct_preconditions_reject_before_key_and_request(
    case, action, changes, contract, expected
):
    enable(case)
    case.confirm_contract(**contract)
    result = await direct(case, action=action, account_status="N", **changes)
    assert result["status"] == "failed", result
    assert result["error"] == expected
    assert result["upstream_attempted"] is False
    assert case.key_reads == []
    assert case.requests == []
    assert case.rows() == []


async def test_direct_does_not_implicitly_enable_fresh_contract(case):
    result = await direct(case)
    assert result["status"] == "failed"
    assert result["error"] == "write_contract_unconfirmed"
    contract = json.loads((case.root / "contract.json").read_text())
    assert contract["omitted_details_preserved"] is False
    assert contract["internal_test_write_enabled"] is False
    assert case.key_reads == []
    assert case.requests == []


@pytest.mark.parametrize("action", ACTIONS)
async def test_controlled_allowlist_permits_only_exact_employee_account_action_status(case, action):
    enable(case)
    case.confirm_contract(
        enterprise_id=case.state.config.t100.enterprise_id,
        upstream_config_digest=case.host._digest(case.state.config.t100),
        allowed_targets=[
            {
                "employee_id": "E001",
                "account_id": "LOGIN-A",
                "actions": [action],
                "account_statuses": ["N"],
            }
        ],
    )
    result = await direct(case, action=action, account_status="N")
    assert result["status"] == "upstream_acknowledged", result
    assert len(case.requests) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"employee_no": "E002"},
        {"account_id": "LOGIN-B"},
        {"action": "disable_account"},
        {"account_status": "Y"},
    ],
)
async def test_outside_allowlist_rejected_before_service_key_and_dispatch(case, changes):
    enable(case)
    case.confirm_contract(
        allowed_targets=[
            {
                "employee_id": "E001",
                "account_id": "LOGIN-A",
                "actions": ["reset_password"],
                "account_statuses": ["N"],
            }
        ]
    )
    result = await direct(case, **({"action": "reset_password", "account_status": "N"} | changes))
    assert result["error"] == "permission_denied", result
    assert result["upstream_attempted"] is False
    assert case.requests == []
    assert case.key_reads == []
    assert case.rows() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"enterprise_id": "other-synthetic-enterprise"},
        {"upstream_config_digest": "0" * 64},
    ],
)
async def test_contract_environment_mismatch_rejected_before_key_and_dispatch(case, changes):
    enable(case)
    case.confirm_contract(
        **(
            {
                "enterprise_id": case.state.config.t100.enterprise_id,
                "upstream_config_digest": case.host._digest(case.state.config.t100),
            }
            | changes
        )
    )
    result = await direct(case)
    assert result["error"] == "permission_denied", result
    assert result["upstream_attempted"] is False
    assert case.requests == []
    assert case.key_reads == []
    assert case.rows() == []


async def test_empty_allowlist_does_not_grant_any_target(case):
    enable(case)
    case.confirm_contract(allowed_targets=[])
    result = await direct(case)
    assert result["error"] == "permission_denied", result
    assert case.requests == []
    assert case.key_reads == []


@pytest.mark.parametrize("action", ACTIONS)
async def test_same_operation_id_reuses_private_binding_and_never_replays(case, action):
    enable(case)
    operation_id = str(uuid4())
    first = await direct(case, action=action, account_status="N", operation_id=operation_id)
    assert first["status"] == "upstream_acknowledged", first
    saved_path = case.root / "plans" / f"{operation_id}.json"
    saved = saved_path.read_bytes()
    second = await direct(case, action=action, account_status="N", operation_id=operation_id)
    assert second["error"] == "reset_already_recorded", second
    assert second["upstream_attempted"] is False
    assert len(case.requests) == 1
    assert len(case.rows()) == 1
    assert saved_path.read_bytes() == saved


async def test_concurrent_same_operation_id_dispatches_once(case):
    enable(case)
    operation_id = str(uuid4())
    results = await asyncio.gather(
        direct(case, operation_id=operation_id), direct(case, operation_id=operation_id)
    )
    assert sum(result["status"] == "upstream_acknowledged" for result in results) == 1, results
    assert sum(result.get("error") == "reset_already_recorded" for result in results) == 1
    assert len(case.requests) == 1
    assert len(case.rows()) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"employee_no": "E002"},
        {"account_id": "LOGIN-B"},
        {"action": "disable_account"},
        {"account_status": "Y"},
    ],
)
async def test_operation_id_cannot_be_rebound_to_other_target_action_or_status(case, changes):
    enable(case)
    operation_id = str(uuid4())
    values = {"action": "reset_password", "account_status": "N", "operation_id": operation_id}
    first = await direct(case, **values)
    assert first["status"] == "upstream_acknowledged", first
    saved_path = case.root / "plans" / f"{operation_id}.json"
    before = saved_path.read_bytes()
    key_reads = len(case.key_reads)
    result = await direct(case, **(values | changes))
    assert result["error"] == "operation_conflict", result
    assert result["upstream_attempted"] is False
    assert len(case.requests) == 1
    assert len(case.key_reads) == key_reads
    assert saved_path.read_bytes() == before


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"enabled": False}, "permission_denied"),
        ({"existing_account_confirmed": False}, "existing_account_unconfirmed"),
    ],
)
async def test_repeating_direct_command_does_not_overwrite_revocation(case, changes, expected):
    enable(case)
    operation_id = case.plan()
    case.change_plan(operation_id, **changes)
    saved_path = case.root / "plans" / f"{operation_id}.json"
    before = saved_path.read_bytes()
    result = await direct(case, operation_id=operation_id)
    assert result["error"] == expected, result
    assert result["upstream_attempted"] is False
    assert case.requests == []
    assert case.key_reads == []
    assert saved_path.read_bytes() == before


@pytest.mark.parametrize("mode", [401, 403, 500, "timeout"])
async def test_direct_unknown_keeps_target_locked_across_action_and_new_uuid(case, mode):
    enable(case)
    case.mode = mode
    first = await direct(case, action="reset_password", account_status="N")
    assert first["error"] == "reset_outcome_unknown", first
    assert first["upstream_attempted"] is True
    second = await direct(case, action="disable_account")
    assert second["error"] == "reset_pending_review", second
    assert second["upstream_attempted"] is False
    assert len(case.requests) == 1
    assert len(case.rows()) == 1
    assert case.rows()[0]["state"] == "unknown"


@pytest.mark.parametrize("operation_id", ["not-a-uuid", "../other", str(uuid4()).upper()])
async def test_direct_rejects_noncanonical_operation_id_before_io(case, operation_id):
    result = await direct(case, operation_id=operation_id)
    assert result["error"] == "invalid_arguments", result
    assert result["upstream_attempted"] is False
    assert case.key_reads == []
    assert case.requests == []
    assert case.rows() == []


@pytest.mark.parametrize("command", ["reset-password", "disable-account"])
def test_direct_cli_requires_explicit_operation_id(case, command):
    arguments = [command, "--employee-no", "E001", "--account-id", "LOGIN-A"]
    if command == "reset-password":
        arguments += ["--account-status", "N"]
    with pytest.raises(SystemExit) as exc:
        case.host.parser().parse_args(arguments)
    assert exc.value.code == 2
    assert case.requests == []


@pytest.mark.parametrize("action", ACTIONS)
def test_cli_dispatches_direct_action_with_explicit_parameters(case, monkeypatch, capsys, action):
    operation_id = str(uuid4())
    observed = []

    async def execute(state, root, **values):
        observed.append((state, root, values))
        return {"status": "upstream_acknowledged", "operation_id": values["operation_id"]}

    command = action.replace("_", "-")
    arguments = [
        "live_write.py",
        "--read-state-dir",
        str(case.state.root),
        command,
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
        arguments += ["--account-status", "Y"]
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(case.host, "execute_direct", execute)
    assert case.host.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "upstream_acknowledged"
    assert len(observed) == 1
    state, root, values = observed[0]
    assert state.root == case.state.root
    assert root == case.host.ROOT
    assert values["action"] == action
    assert values["employee_no"] == "E001"
    assert values["account_id"] == "LOGIN-A"
    assert values["operation_id"] == operation_id
    assert values["confirm"] is True
    assert values["existing_account_confirmed"] is True
    assert values.get("account_status") == ("Y" if action == "reset_password" else None)
    assert case.requests == []
