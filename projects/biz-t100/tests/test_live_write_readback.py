"""账号维护后的状态回读：合成身份、捕获上游，绝不调用真实网络或重复写入。"""

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from test_live_read import write
from test_live_write import case as case
from test_live_write import live as live

from biz_t100 import runtime
from biz_t100.client import T100EmployeeClient


async def acknowledged(case, action="disable_account", account_status="N"):
    operation_id = case.plan(action, account_status=account_status)
    case.confirm_contract()
    result = await case.call(operation_id)
    assert result["status"] == "upstream_acknowledged", result
    assert len(case.requests) == 1
    return operation_id


def read_result(status="N", **changes):
    request_id = str(uuid4())
    return {
        "status": "passed",
        "upstream_attempted": True,
        "real_business_called": True,
        "remote_response_validated": True,
        "real_identity_verified": False,
        "request_id": request_id,
        "requested_employee_no": "E001",
        "protocol": "2025-11-25",
        "result": {
            "request_id": request_id,
            "data": {
                "system": "t100",
                "employee_id": "E001",
                "employee_status": status,
                "observation": "employee_record",
            },
        },
        **changes,
    }


def install_reader(case, monkeypatch, result, after_read=None):
    calls = []

    async def reader(state, employee_no):
        assert state is case.state
        calls.append(employee_no)
        if after_read is not None:
            after_read()
        return result

    monkeypatch.setattr(case.host, "call_employee", reader)
    return calls


def db_snapshot(case):
    path = case.root / "audit" / "account-write.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        return tuple(
            tuple(connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in ("reset_context", "reset_operations", "reset_events")
        )


def change_operation(case, operation_id, column, value):
    assert column in {
        "state",
        "code",
        "employee_id",
        "account_id",
        "enterprise_id",
        "tool_name",
        "issuer",
        "subject",
        "client_id",
        "binding_version",
    }
    path = case.root / "audit" / "account-write.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"UPDATE reset_operations SET {column}=? WHERE operation_id=?", (value, operation_id)
        )


def assert_readonly(case, before, result, calls):
    assert result["write_upstream_attempted"] is False
    assert len(case.requests) == 1, "回读不得再次调用 user.account.create"
    assert db_snapshot(case) == before, "回读不得改写原写操作或审计事件"
    assert calls == ["E001"]
    assert result["real_identity_verified"] is False
    assert "synthetic-private-key" not in json.dumps(result)
    assert "PRIVATE KEY" not in json.dumps(result)


@pytest.mark.parametrize(
    "observed,expected_result,effect",
    [("N", "verified", True), ("Y", "mismatch", False)],
)
async def test_disable_readback_compares_ack_to_current_status_without_repeating_write(
    case, monkeypatch, observed, expected_result, effect
):
    operation_id = await acknowledged(case)
    before = db_snapshot(case)
    reply = read_result(observed)
    calls = install_reader(case, monkeypatch, reply)
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == expected_result, result
    assert result["operation_id"] == operation_id
    assert result["verification_scope"] == "account_status"
    assert result["expected_account_status"] == "N"
    assert result["observed_account_status"] == observed
    assert result["account_state_matches"] is effect
    assert result["business_effect_verified"] is effect
    assert result["upstream_attempted"] is True
    assert result["read_request_id"] == reply["request_id"]
    if observed == "Y":
        assert result["error"] == "account_status_mismatch"
    evidence = Path(result["evidence_path"])
    assert evidence.is_relative_to(case.root)
    assert evidence.stat().st_mode & 0o777 == 0o600
    assert evidence.parent.stat().st_mode & 0o777 == 0o700
    recorded = json.loads(evidence.read_text())
    assert recorded["operation_id"] == operation_id
    assert recorded["read_request_id"] == reply["request_id"]
    assert recorded["business_effect_verified"] is effect
    assert_readonly(case, before, result, calls)


@pytest.mark.parametrize("expected,observed", [("N", "N"), ("Y", "Y"), ("N", "Y")])
async def test_reset_status_check_never_claims_password_effect(
    case, monkeypatch, expected, observed
):
    operation_id = await acknowledged(case, "reset_password", expected)
    before = db_snapshot(case)
    calls = install_reader(case, monkeypatch, read_result(observed))
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == ("verified" if expected == observed else "mismatch"), result
    assert result["verification_scope"] == "account_status"
    assert result["expected_account_status"] == expected
    assert result["observed_account_status"] == observed
    assert result["account_state_matches"] is (expected == observed)
    assert result["business_effect_verified"] is False
    assert_readonly(case, before, result, calls)


@pytest.mark.parametrize("observed", [None, "", "n", " Y ", "disabled", 0, False])
async def test_missing_or_invalid_status_cannot_verify_account(case, monkeypatch, observed):
    operation_id = await acknowledged(case)
    before = db_snapshot(case)
    calls = install_reader(case, monkeypatch, read_result(observed))
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == "upstream_contract_error"
    assert result["business_effect_verified"] is False
    assert result.get("observed_account_status") is None
    assert_readonly(case, before, result, calls)


@pytest.mark.parametrize("failure", ["employee_not_found", "upstream_timeout", "audit_unavailable"])
async def test_query_failure_is_not_an_account_observation(case, monkeypatch, failure):
    operation_id = await acknowledged(case)
    before = db_snapshot(case)
    reply = read_result()
    reply.pop("result")
    reply.update(status="failed", error=failure, remote_response_validated=False)
    calls = install_reader(case, monkeypatch, reply)
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == failure
    assert result["business_effect_verified"] is False
    assert result.get("observed_account_status") is None
    assert result["upstream_attempted"] is True
    assert_readonly(case, before, result, calls)


@pytest.mark.parametrize("stored_state", [None, "started", "dispatched", "unknown", "not_sent"])
async def test_only_acknowledged_operations_can_be_read_back(case, monkeypatch, stored_state):
    operation_id = case.plan()
    case.confirm_contract()
    if stored_state is not None:
        result = await case.call(operation_id)
        assert result["status"] == "upstream_acknowledged"
        change_operation(case, operation_id, "state", stored_state)
    calls = install_reader(case, monkeypatch, read_result())
    writes_before = len(case.requests)
    key_reads_before = len(case.key_reads)
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == "readback_not_available"
    assert result["upstream_attempted"] is False
    assert result["write_upstream_attempted"] is False
    assert result["business_effect_verified"] is False
    assert calls == []
    assert len(case.requests) == writes_before
    assert len(case.key_reads) == key_reads_before


@pytest.mark.parametrize(
    "column,value",
    [
        ("employee_id", "E999"),
        ("account_id", "LOGIN-OTHER"),
        ("enterprise_id", "other-enterprise"),
        ("tool_name", "reset_my_t100_password"),
        ("issuer", "https://other-issuer.example.invalid"),
        ("subject", "other-subject"),
        ("client_id", "other-client"),
        ("binding_version", "0" * 64),
    ],
)
async def test_write_audit_binding_must_match_entire_original_authorization(
    case, monkeypatch, column, value
):
    operation_id = await acknowledged(case)
    change_operation(case, operation_id, column, value)
    before = db_snapshot(case)
    key_reads_before = len(case.key_reads)
    calls = install_reader(case, monkeypatch, read_result())
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == "readback_binding_mismatch"
    assert result["upstream_attempted"] is False
    assert result["business_effect_verified"] is False
    assert result["write_upstream_attempted"] is False
    assert calls == []
    assert len(case.key_reads) == key_reads_before
    assert len(case.requests) == 1
    assert db_snapshot(case) == before


@pytest.mark.parametrize(
    "change,expected",
    [
        ("disabled", "permission_denied"),
        ("contract", "write_contract_unconfirmed"),
        ("enabled", "write_execution_not_enabled"),
        ("target", "permission_denied"),
        ("rebind", "readback_binding_mismatch"),
    ],
)
async def test_revoked_or_changed_permission_blocks_read_before_any_query(
    case, monkeypatch, change, expected
):
    operation_id = await acknowledged(case)
    if change == "disabled":
        case.change_plan(operation_id, enabled=False)
    elif change == "contract":
        case.confirm_contract(omitted_details_preserved=False)
    elif change == "enabled":
        case.confirm_contract(internal_test_write_enabled=False)
    elif change == "target":
        case.confirm_contract(allowed_targets=[])
    elif change == "rebind":
        path = case.root / "plans" / (operation_id + ".json")
        body = json.loads(path.read_text())
        body["plan"]["account_id"] = "OTHER-LOGIN"
        write(path, body)
    before = db_snapshot(case)
    key_reads_before = len(case.key_reads)
    calls = install_reader(case, monkeypatch, read_result())
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == expected
    assert result["business_effect_verified"] is False
    assert result["upstream_attempted"] is False
    assert calls == []
    assert len(case.key_reads) == key_reads_before
    assert len(case.requests) == 1
    assert db_snapshot(case) == before


@pytest.mark.parametrize("change", ["disable", "binding"])
async def test_post_query_revocation_withholds_observed_status_and_keeps_call_fact(
    case, monkeypatch, change
):
    operation_id = await acknowledged(case)
    before = db_snapshot(case)

    def revoke():
        if change == "disable":
            case.change_plan(operation_id, enabled=False)
        else:
            case.confirm_contract(evidence_reference="changed-authority-after-read")

    reply = read_result()
    calls = install_reader(case, monkeypatch, reply, revoke)
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["business_effect_verified"] is False
    assert result.get("observed_account_status") is None
    assert result.get("account_state_matches") in {None, False}
    assert result["upstream_attempted"] is True
    assert result["read_request_id"] == reply["request_id"]
    recorded = json.loads(Path(result["evidence_path"]).read_text())
    assert recorded["business_effect_verified"] is False
    assert recorded.get("observed_account_status") is None
    assert recorded["upstream_attempted"] is True
    assert_readonly(case, before, result, calls)


async def test_native_signed_query_captures_exact_employee_and_only_reads_once(case, monkeypatch):
    operation_id = await acknowledged(case)
    before = db_snapshot(case)
    queries = []
    arguments = []
    module = importlib.import_module("live_employee_call")
    original_rpc = module._rpc

    async def capture_rpc(client, method, params, identifier):
        if method == "tools/call":
            arguments.append(params["arguments"])
        return await original_rpc(client, method, params, identifier)

    def upstream(request):
        envelope = json.loads(request.content)
        queries.append(envelope)
        assert envelope["key"] == "synthetic-private-key"
        assert envelope["datakey"] == {"EntId": "synthetic-enterprise"}
        assert envelope["service"]["name"] == "employee.list.data.query.get"
        assert envelope["payload"]["std_data"]["parameter"]["employee_no"] == "E001"
        return httpx.Response(
            200,
            json={
                "payload": {
                    "std_data": {
                        "execution": {"code": "0"},
                        "parameter": {
                            "employee": [
                                {
                                    "employee_no": "E001",
                                    "status": "N",
                                    "account_no": "",
                                }
                            ]
                        },
                    }
                }
            },
        )

    def reader(config, **kwargs):
        assert kwargs == {}
        return T100EmployeeClient(config, transport=httpx.MockTransport(upstream))

    monkeypatch.setattr(runtime, "T100EmployeeClient", reader)
    monkeypatch.setattr(module, "_rpc", capture_rpc)
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == "verified", result
    assert result["observed_account_status"] == "N"
    assert result["business_effect_verified"] is True
    assert result["real_identity_verified"] is False
    assert result["upstream_attempted"] is True
    assert result["write_upstream_attempted"] is False
    assert len(queries) == 1
    assert len(case.requests) == 1
    assert arguments == [{}], "员工号不得成为远程本人查询工具入参"
    assert db_snapshot(case) == before
    evidence = json.loads(Path(result["evidence_path"]).read_text())
    assert evidence["read_request_id"] == result["read_request_id"]
    journals = list((case.state.root / "operator-cases").glob("*/audit/query.sqlite3"))
    assert len(journals) == 1
    with sqlite3.connect(journals[0]) as db:
        rows = db.execute(
            "SELECT event,request_id,enterprise_id,employee_id FROM query_events ORDER BY rowid"
        ).fetchall()
    assert [row[0] for row in rows] == ["query_started", "query_succeeded"]
    assert all(
        row[1:] == (result["read_request_id"], "synthetic-enterprise", "E001") for row in rows
    )


@pytest.mark.parametrize("status,expected_exit", [("verified", 0), ("mismatch", 1), ("failed", 1)])
def test_verify_command_calls_readback_and_failure_is_nonzero(
    case, monkeypatch, capsys, status, expected_exit
):
    operation_id = str(uuid4())
    calls = []

    async def verify(state, root, identifier):
        calls.append((state, root, identifier))
        return {"status": status, "operation_id": identifier, "business_effect_verified": False}

    monkeypatch.setattr(case.host, "verify_plan", verify)
    monkeypatch.setattr(case.host, "load_live", lambda root: case.state)
    monkeypatch.setattr(sys, "argv", ["live_write.py", "verify", "--operation-id", operation_id])
    assert case.host.main() == expected_exit
    assert calls == [(case.state, case.host.ROOT, operation_id)]
    assert json.loads(capsys.readouterr().out)["status"] == status


@pytest.mark.parametrize("phase", ["started", "terminal"])
async def test_failed_evidence_write_never_delivers_verified_status(case, monkeypatch, phase):
    operation_id = await acknowledged(case)
    before = db_snapshot(case)
    calls = install_reader(case, monkeypatch, read_result())
    original_write = case.host._write_new

    def fail_evidence(path, value):
        if path.parent.name == "readbacks":
            started = path.name.endswith(".started.json")
            if started == (phase == "started"):
                raise OSError("synthetic-disk-unavailable")
        return original_write(path, value)

    monkeypatch.setattr(case.host, "_write_new", fail_evidence)
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == "journal_unavailable"
    assert result["business_effect_verified"] is False
    assert result["account_state_matches"] is False
    assert result.get("observed_account_status") is None
    assert result["write_upstream_attempted"] is False
    assert result["upstream_attempted"] is (phase == "terminal")
    assert calls == (["E001"] if phase == "terminal" else [])
    assert len(case.requests) == 1
    assert db_snapshot(case) == before
    assert "synthetic-disk-unavailable" not in json.dumps(result)


@pytest.mark.parametrize(
    "corruption,expected",
    [
        ("employee", "upstream_employee_mismatch"),
        ("request_id", "upstream_contract_error"),
        ("protocol", "upstream_contract_error"),
        ("unvalidated", "upstream_contract_error"),
        ("not_attempted", "upstream_contract_error"),
    ],
)
async def test_untrusted_or_misbound_query_output_cannot_verify_write(
    case, monkeypatch, corruption, expected
):
    operation_id = await acknowledged(case)
    reply = read_result()
    if corruption == "employee":
        reply["result"]["data"]["employee_id"] = "E002"
    elif corruption == "request_id":
        reply["request_id"] = str(uuid4())
    elif corruption == "protocol":
        reply["protocol"] = "2025-06-18"
    elif corruption == "unvalidated":
        reply["remote_response_validated"] = False
    elif corruption == "not_attempted":
        reply["upstream_attempted"] = False
    calls = install_reader(case, monkeypatch, reply)
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == expected
    assert result["business_effect_verified"] is False
    assert result.get("observed_account_status") is None
    assert result["write_upstream_attempted"] is False
    assert calls == ["E001"]
    assert len(case.requests) == 1


def change_base_permission(case, change):
    path = case.state.config.policy_file
    policy = json.loads(path.read_text())
    if change == "disabled":
        policy["principals"][0]["enabled"] = False
    elif change == "permission":
        policy["principals"][0]["allow_employee_read"] = False
    elif change == "revision":
        policy["revision"] = "synthetic-revised-authority"
    elif change == "binding":
        policy["principals"][0]["employee_id"] = "E002"
    else:
        raise AssertionError("unrecognized test mutation")
    write(path, policy)


def install_native_reader(case, monkeypatch, after_request=None):
    requests = []

    def upstream(request):
        envelope = json.loads(request.content)
        assert envelope["service"]["name"] == "employee.list.data.query.get"
        assert envelope["datakey"] == {"EntId": "synthetic-enterprise"}
        assert envelope["payload"]["std_data"]["parameter"]["employee_no"] == "E001"
        requests.append(envelope)
        if after_request is not None:
            after_request()
        return httpx.Response(
            200,
            json={
                "payload": {
                    "std_data": {
                        "execution": {"code": "0"},
                        "parameter": {"employee": [{"employee_no": "E001", "status": "N"}]},
                    }
                }
            },
        )

    def reader(config, **kwargs):
        assert kwargs == {}
        return T100EmployeeClient(config, transport=httpx.MockTransport(upstream))

    monkeypatch.setattr(runtime, "T100EmployeeClient", reader)
    return requests


@pytest.mark.parametrize(
    "change,expected", [("disabled", "identity_not_enabled"), ("permission", "permission_denied")]
)
async def test_base_read_permission_revoked_before_verify_is_zero_query(
    case, monkeypatch, change, expected
):
    operation_id = await acknowledged(case)
    before = db_snapshot(case)
    change_base_permission(case, change)
    queries = install_native_reader(case, monkeypatch)
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == expected
    assert result["upstream_attempted"] is False
    assert result["write_upstream_attempted"] is False
    assert result["business_effect_verified"] is False
    assert result.get("observed_account_status") is None
    assert queries == []
    assert len(case.requests) == 1
    assert db_snapshot(case) == before


@pytest.mark.parametrize("phase", ["after_started", "in_upstream"])
@pytest.mark.parametrize(
    "change,expected",
    [
        ("disabled", "identity_not_enabled"),
        ("permission", "permission_denied"),
        ("revision", "authorization_changed"),
        ("binding", "authorization_changed"),
    ],
)
async def test_native_read_base_policy_change_withholds_status_and_preserves_dispatch_fact(
    case, monkeypatch, phase, change, expected
):
    operation_id = await acknowledged(case)
    before = db_snapshot(case)
    original_write = case.host._write_new

    def mutate():
        change_base_permission(case, change)

    def write_then_revoke(path, value):
        result = original_write(path, value)
        if path.parent.name == "readbacks" and path.name.endswith(".started.json"):
            mutate()
        return result

    if phase == "after_started":
        monkeypatch.setattr(case.host, "_write_new", write_then_revoke)
    queries = install_native_reader(case, monkeypatch, mutate if phase == "in_upstream" else None)
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == expected
    assert result["upstream_attempted"] is (phase == "in_upstream")
    assert result["write_upstream_attempted"] is False
    assert result["business_effect_verified"] is False
    assert result["account_state_matches"] is False
    assert result.get("observed_account_status") is None
    assert len(queries) == (1 if phase == "in_upstream" else 0)
    assert len(case.requests) == 1
    assert db_snapshot(case) == before
    evidence = json.loads(Path(result["evidence_path"]).read_text())
    assert evidence["upstream_attempted"] is (phase == "in_upstream")
    assert evidence.get("observed_account_status") is None
    assert evidence["business_effect_verified"] is False
    if phase == "in_upstream":
        assert result["read_request_id"]
        assert evidence["read_request_id"] == result["read_request_id"]
        journals = list((case.state.root / "operator-cases").glob("*/audit/query.sqlite3"))
        assert len(journals) == 1
        with sqlite3.connect(journals[0]) as db:
            rows = db.execute(
                "SELECT request_id,upstream_attempted FROM query_events "
                "WHERE event<>'query_started'"
            ).fetchall()
        assert rows == [(result["read_request_id"], 1)]


async def test_final_evidence_written_before_fsync_failure_leaves_no_verified_artifact(
    case, monkeypatch
):
    operation_id = await acknowledged(case)
    before = db_snapshot(case)
    calls = install_reader(case, monkeypatch, read_result())
    original_write = case.host._write_new
    written_final = []

    def write_then_fail(path, value):
        original_write(path, value)
        if path.parent.name == "readbacks" and not path.name.endswith(".started.json"):
            assert path.is_file()
            assert json.loads(path.read_text())["status"] == "verified"
            written_final.append(path)
            raise OSError("synthetic-fsync-failed-after-visible-write")

    monkeypatch.setattr(case.host, "_write_new", write_then_fail)
    result = await case.host.verify_plan(case.state, case.root, operation_id)
    assert result["status"] == "failed", result
    assert result["error"] == "journal_unavailable"
    assert result["business_effect_verified"] is False
    assert result["account_state_matches"] is False
    assert result.get("observed_account_status") is None
    assert "evidence_path" not in result
    assert result["upstream_attempted"] is True
    assert result["write_upstream_attempted"] is False
    assert calls == ["E001"]
    assert len(written_final) == 1
    assert not written_final[0].exists(), "fsync失败后不得留下貌似成功的终态证据"
    surviving = list((case.root / "readbacks").iterdir())
    assert len(surviving) == 1
    assert surviving[0].name.endswith(".started.json")
    assert json.loads(surviving[0].read_text())["status"] == "started"
    assert len(case.requests) == 1
    assert db_snapshot(case) == before
    assert "synthetic-fsync-failed-after-visible-write" not in json.dumps(result)
