"""运维方选定的合成用例；所有测试都显式拦截上游网络。"""

import asyncio
import base64
import importlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from test_live_read import live  # noqa: F401

import biz_t100.access as access_module
from biz_t100 import runtime
from biz_t100.client import T100EmployeeClient


@pytest.fixture
def operator(live):  # noqa: F811
    return importlib.import_module("live_employee_call"), live


def mock_upstream(monkeypatch, handler):
    def create(config, **kwargs):
        assert kwargs == {}, "runtime must use its default real transport"
        return T100EmployeeClient(config, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(runtime, "T100EmployeeClient", create)


def response(records):
    return httpx.Response(
        200,
        json={
            "payload": {
                "std_data": {
                    "execution": {"code": "0"},
                    "parameter": {"employee": records},
                }
            }
        },
    )


def request_employee(request):
    envelope = json.loads(request.content)
    assert envelope["key"] == "synthetic-private-key"
    assert envelope["datakey"]["EntId"] == "synthetic-enterprise"
    assert envelope["service"]["name"] == "employee.list.data.query.get"
    parameter = envelope["payload"]["std_data"]["parameter"]
    assert set(parameter) == {"employee_no", "is_digiwin_product", "use_has_next"}
    return parameter["employee_no"]


def audit(result):
    with sqlite3.connect(Path(result["audit_path"]).as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM query_events ORDER BY rowid")]


async def test_concurrent_operator_cases_are_isolated_and_preserve_shared_material(
    operator, monkeypatch
):
    module, (_, state, _, _) = operator
    original = {
        path: path.read_bytes()
        for path in (
            state.config.policy_file,
            state.config.credential_file,
            state.root / "runtime.json",
            state.root / "issuer-private.pem",
        )
    }
    captured, arrived = [], asyncio.Event()
    claims_seen, tool_arguments = [], []
    original_rpc = module._rpc

    async def capture_rpc(client, method, params, identifier):
        if method == "initialize":
            token = client.headers["Authorization"].removeprefix("Bearer ")
            encoded = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            assert "employee_no" not in claims and "employee_id" not in claims
            claims_seen.append(claims)
        if method == "tools/call":
            tool_arguments.append(params["arguments"])
        return await original_rpc(client, method, params, identifier)

    monkeypatch.setattr(module, "_rpc", capture_rpc)

    async def handler(request):
        employee = request_employee(request)
        captured.append(employee)
        if len(captured) == 2:
            arrived.set()
        await asyncio.wait_for(arrived.wait(), timeout=2)
        return response([{"employee_no": employee, "full_name": "Synthetic"}])

    mock_upstream(monkeypatch, handler)
    results = await asyncio.gather(
        *(module.call_employee(state, employee) for employee in ("E101", "E202"))
    )
    assert sorted(captured) == ["E101", "E202"]
    assert len({item["case_path"] for item in results}) == 2
    subjects = set()
    for result, employee in zip(results, ("E101", "E202"), strict=True):
        assert result["status"] == "passed", result
        assert result["requested_employee_no"] == employee
        assert result["result"]["data"]["employee_id"] == employee
        assert result["mcp_transport"] == "in-process-asgi"
        assert result["test_mode"] == "operator-selected-employee"
        assert result["real_identity_verified"] is False
        assert result["upstream_attempted"] is True
        assert result["protocol"] == "2025-11-25"
        case = Path(result["case_path"])
        assert case.parent == state.root / "operator-cases"
        policy = json.loads((case / "access.json").read_text())
        principal = policy["principals"][0]
        assert principal["employee_id"] == employee
        assert principal["subject"].startswith("simulated-cli-")
        subjects.add(principal["subject"])
        events = audit(result)
        assert [event["event"] for event in events] == ["query_started", "query_succeeded"]
        assert all(
            (event["enterprise_id"], event["employee_id"], event["subject"])
            == ("synthetic-enterprise", employee, principal["subject"])
            for event in events
        )
        assert all(event["request_id"] == result["request_id"] for event in events)
        assert all(path.stat().st_mode & 0o777 == 0o700 for path in (case, case / "audit"))
        assert all(
            path.stat().st_mode & 0o777 == 0o600 for path in case.rglob("*") if path.is_file()
        )
        assert not list(case.rglob("*.pem"))
        assert not list(case.rglob("*.jwt"))
        assert not list(case.rglob("*.key"))
    assert len(subjects) == 2
    assert {claims["sub"] for claims in claims_seen} == subjects
    assert tool_arguments == [{}, {}]
    assert all(path.read_bytes() == data for path, data in original.items())
    assert not list((state.root / "tokens").iterdir())
    assert not state.config.journal_path.exists()


@pytest.mark.parametrize(
    "records, expected",
    [
        ([], "employee_not_found"),
        ([{"employee_no": "E999"}], "upstream_employee_mismatch"),
        ([{"employee_no": "E101"}, {"employee_no": "E101"}], "employee_not_unique"),
    ],
)
async def test_no_record_or_wrong_cardinality_never_returns_employee_data(
    operator, monkeypatch, records, expected
):
    module, (_, state, _, _) = operator
    captured = []

    def handler(request):
        captured.append(request_employee(request))
        return response(records)

    mock_upstream(monkeypatch, handler)
    result = await module.call_employee(state, "E101")
    assert result["status"] == "failed"
    assert result["error"] == expected
    assert result["message"]
    assert result["upstream_attempted"] is True
    assert result["remote_response_validated"] is False
    assert "result" not in result
    assert captured == ["E101"]
    events = audit(result)
    assert events[-1]["event"] == "query_failed"
    assert events[-1]["code"] == expected


@pytest.mark.parametrize("employee", ["", " ", "E001 E002", "E001\n", "../E001", "*", "E" * 129, 1])
async def test_invalid_employee_has_zero_requests_and_no_private_side_effects(
    operator, monkeypatch, employee
):
    module, (_, state, _, _) = operator

    def forbidden(*args, **kwargs):
        pytest.fail("invalid employee must fail before configuration or signing")

    monkeypatch.setattr(module, "_read_private", forbidden)
    monkeypatch.setattr(module, "create_runtime_app", forbidden)
    result = await module.call_employee(state, employee)
    assert result["status"] == "failed"
    assert result["error"] == "invalid_arguments"
    assert result["upstream_attempted"] is False
    assert result["real_business_called"] is False
    assert "requested_employee_no" not in result
    assert not (state.root / "operator-cases").exists()


@pytest.mark.parametrize(
    "change, expected",
    [
        ("disabled", "identity_not_enabled"),
        ("permission", "permission_denied"),
        ("missing", "identity_not_enabled"),
    ],
)
async def test_base_revocation_prevents_case_and_secret_reads(
    operator, monkeypatch, change, expected
):
    module, (_, state, _, policy) = operator
    if change == "missing":
        policy["principals"] = []
    else:
        field = "enabled" if change == "disabled" else "allow_employee_read"
        policy["principals"][0][field] = False
    state.config.policy_file.write_text(json.dumps(policy))
    original = module._read_private

    def public_only(path, maximum):
        assert path not in {state.config.credential_file, state.root / "issuer-private.pem"}
        return original(path, maximum)

    def forbidden(*args, **kwargs):
        pytest.fail("revoked base grant must not construct a business app")

    monkeypatch.setattr(module, "_read_private", public_only)
    monkeypatch.setattr(module, "create_runtime_app", forbidden)
    result = await module.call_employee(state, "E101")
    assert result["error"] == expected
    assert result["upstream_attempted"] is False
    assert not (state.root / "operator-cases").exists()


async def test_service_key_bytes_are_read_only_after_case_policy_authorization(
    operator, monkeypatch
):
    module, (_, state, _, _) = operator
    original = access_module._private_bytes
    seen = []

    def authorized_only(path, maximum):
        if path == state.config.credential_file:
            assert seen and seen[-1] == "authorized_policy"
            seen.append("service_key")
        elif path.name == "access.json":
            principal = json.loads(original(path, maximum))["principals"][0]
            assert principal["enabled"] and principal["allow_employee_read"]
            assert principal["employee_id"] == "E101"
            seen.append("authorized_policy")
        return original(path, maximum)

    monkeypatch.setattr(access_module, "_private_bytes", authorized_only)
    mock_upstream(
        monkeypatch,
        lambda request: response([{"employee_no": request_employee(request)}]),
    )
    result = await module.call_employee(state, "E101")
    assert result["status"] == "passed", result
    assert seen == ["authorized_policy", "service_key"] * 3


async def test_case_policy_revoked_before_tools_call_is_not_overridden(operator, monkeypatch):
    module, (_, state, _, _) = operator
    original_sign = module._sign
    original_read = access_module._private_bytes

    def revoked_sign(state, subject):
        token = original_sign(state, subject)
        path = next((state.root / "operator-cases").glob("*/access.json"))
        policy = json.loads(path.read_text())
        policy["principals"][0]["enabled"] = False
        path.write_text(json.dumps(policy))
        return token

    def no_service_secret(path, maximum):
        assert path != state.config.credential_file
        return original_read(path, maximum)

    monkeypatch.setattr(module, "_sign", revoked_sign)
    monkeypatch.setattr(access_module, "_private_bytes", no_service_secret)
    mock_upstream(monkeypatch, lambda request: pytest.fail("revoked case reached upstream"))
    result = await module.call_employee(state, "E101")
    assert result["error"] == "identity_not_enabled"
    assert result["upstream_attempted"] is False
    assert audit(result)[-1]["event"] == "query_failed"


async def test_native_signature_verification_rejects_forged_case_token(operator, monkeypatch):
    module, (_, state, _, _) = operator
    original_read = access_module._private_bytes

    def no_service_secret(path, maximum):
        assert path != state.config.credential_file
        return original_read(path, maximum)

    monkeypatch.setattr(module, "_sign", lambda *args: "forged.token.invalid")
    monkeypatch.setattr(access_module, "_private_bytes", no_service_secret)
    mock_upstream(monkeypatch, lambda request: pytest.fail("forged identity reached upstream"))
    result = await module.call_employee(state, "E101")
    assert result["error"] == "unauthenticated"
    assert result["upstream_attempted"] is False
    assert audit(result) == []


async def test_private_signer_must_match_selected_public_key(operator, monkeypatch):
    module, (_, state, _, _) = operator
    (state.root / "issuer-private.pem").write_text(
        RSAKeyPair.generate().private_key.get_secret_value()
    )
    monkeypatch.setattr(
        module, "create_runtime_app", lambda *args, **kwargs: pytest.fail("mismatched signer")
    )
    result = await module.call_employee(state, "E101")
    assert result["error"] == "live_read_token_invalid"
    assert result["upstream_attempted"] is False
    assert not Path(result["audit_path"]).exists()


@pytest.mark.parametrize("target", ["symlink", "public"])
async def test_operator_case_parent_requires_private_real_directory(operator, tmp_path, target):
    module, (_, state, _, _) = operator
    parent = state.root / "operator-cases"
    if target == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        parent.symlink_to(outside, target_is_directory=True)
    else:
        parent.mkdir(mode=0o755)
    result = await module.call_employee(state, "E101")
    assert result["error"] == "live_read_failed"
    assert result["upstream_attempted"] is False
    assert not list(parent.iterdir())


def test_script_has_no_mock_or_old_source_dependencies(operator):
    module, _ = operator
    source = Path(module.__file__).read_text()
    assert "MockTransport" not in source
    assert "account_mt" not in source
    assert "sys.path" not in source
    assert "import live_read" not in source


@pytest.mark.parametrize("failure", ["unauthenticated", "timeout", "bad-payload"])
async def test_upstream_failures_have_safe_messages_and_one_attempt(operator, monkeypatch, failure):
    module, (_, state, _, _) = operator
    captured = []

    def handler(request):
        captured.append(request_employee(request))
        if failure == "timeout":
            raise httpx.ReadTimeout("DO-NOT-DISCLOSE", request=request)
        if failure == "unauthenticated":
            return httpx.Response(401, content=b"DO-NOT-DISCLOSE")
        return httpx.Response(200, content=b"DO-NOT-DISCLOSE")

    mock_upstream(monkeypatch, handler)
    result = await module.call_employee(state, "E101")
    expected = {
        "unauthenticated": "upstream_unauthenticated",
        "timeout": "upstream_timeout",
        "bad-payload": "upstream_contract_error",
    }[failure]
    assert result["status"] == "failed"
    assert result["error"] == expected
    assert result["message"]
    assert result["upstream_attempted"] is True
    assert captured == ["E101"]
    assert "DO-NOT-DISCLOSE" not in json.dumps(result)
    assert audit(result)[-1]["code"] == expected
