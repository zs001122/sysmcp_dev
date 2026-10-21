"""原生用例入口测试只使用合成的私有运行装置。"""

import asyncio
import hashlib
import importlib
import json
import signal
import socket
import sqlite3
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import httpx2
import pytest

import biz_t100.access as access_module
from biz_t100.models import BusinessError
from biz_t100.reset_server import RESET_TOOL_NAME
from biz_t100.server import TOOL_NAME

_PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(_PROJECT / "tests" / "legacy_support"))
    host = importlib.import_module("employee_case")
    support = importlib.import_module("employee_case_support")
    supplied = tmp_path / "input"
    supplied.mkdir(mode=0o700)
    fixture = supplied / "fixture.json"
    fixture.write_text(json.dumps({"employee_no": "CASE100", "display_name": "Case Example"}))
    fixture.chmod(0o600)
    root = tmp_path / "case"
    support.init_case(root, fixture)
    return host, support, support.load_case(root)


def signed(case, user="simulated-approved-user"):
    _, support, state = case
    output = state.root / "tokens" / (str(uuid4()) + ".jwt")
    support.issue_case_token(state.root, user=user, service="read", output=output)
    value = output.read_text().strip()
    output.unlink()
    return value


@asynccontextmanager
async def connection(case, *, scenario="success"):
    host, _, state = case
    app = host.create_case_app(state, scenario=scenario, instance_id="synthetic-case-check")
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            yield client


async def rpc(client, token, *, name=TOOL_NAME, arguments=None, method="tools/call"):
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    params = {} if method == "tools/list" else {"name": name, "arguments": arguments or {}}
    return await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def captures(state):
    path = state.capture_path("read")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def audit(state):
    with sqlite3.connect(state.journal_path("read").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM query_events")]


def observe_reads(case, monkeypatch):
    _, support, state = case
    original_support = support._read_private
    original_access = access_module._private_bytes
    credential_reads = []

    def public_state_only(path, maximum):
        assert path != state.root / "issuer-private.pem"
        assert path != state.credential_path("read")
        return original_support(path, maximum)

    def authorized_credential(path, maximum):
        assert path != state.root / "issuer-private.pem"
        if path == state.credential_path("read"):
            credential_reads.append(path)
        return original_access(path, maximum)

    monkeypatch.setattr(support, "_read_private", public_state_only)
    monkeypatch.setattr(access_module, "_private_bytes", authorized_credential)
    return credential_reads


async def test_case_native_signed_read_captures_exact_authorized_target_and_audit(
    case, monkeypatch
):
    _, support, state = case
    token = signed(case)
    credential_reads = observe_reads(case, monkeypatch)
    async with connection(case) as client:
        for endpoint in ("live", "ready"):
            health = await client.get("/health/" + endpoint)
            assert health.status_code == 200
            assert health.json() == {
                "status": endpoint,
                "mode": support.MODE,
                "service": "read",
                "instance_id": "synthetic-case-check",
                "scope": "approved_mock_read",
            }
        tools = await rpc(client, token, method="tools/list")
        assert [tool["name"] for tool in tools.json()["result"]["tools"]] == [TOOL_NAME]
        assert credential_reads == []
        client.headers["X-Employee-No"] = "CASE999"
        response = await rpc(client, token)
    assert response.status_code == 200
    result = response.json()["result"]
    assert not result.get("isError")
    data = result["structuredContent"]
    assert data["data"]["employee_id"] == "CASE100"
    assert data["data"]["display_name"] == "Case Example"
    assert credential_reads and set(credential_reads) == {state.credential_path("read")}
    captured = captures(state)
    assert len(captured) == 1
    assert (
        captured[0]["enterprise_id"],
        captured[0]["employee_id"],
        captured[0]["account_id"],
    ) == ("INTERNAL-READ", "CASE100", None)
    records = audit(state)
    assert {row["event"] for row in records} == {"query_started", "query_succeeded"}
    for row in records:
        assert row["request_id"] == data["request_id"]
        assert (
            row["issuer"],
            row["subject"],
            row["client_id"],
            row["enterprise_id"],
            row["employee_id"],
            row["tool_name"],
        ) == (
            state.issuer,
            "simulated-approved-user",
            state.client_id("read"),
            "INTERNAL-READ",
            "CASE100",
            TOOL_NAME,
        )
    assert "Case Example" not in json.dumps(records)
    assert not (state.root / "reset").exists()


@pytest.mark.parametrize("kind", ["unmapped", "employee_argument", "missing_token", "reset_tool"])
async def test_case_denials_never_read_service_key_or_send_requests(case, monkeypatch, kind):
    _, _, state = case
    token = (
        None
        if kind == "missing_token"
        else signed(case, "unmapped" if kind == "unmapped" else "simulated-approved-user")
    )
    credential_reads = observe_reads(case, monkeypatch)
    arguments = {"employee_no": "CASE999"} if kind == "employee_argument" else {}
    name = TOOL_NAME
    if kind == "reset_tool":
        name = RESET_TOOL_NAME
        arguments = {"confirm": True, "operation_id": str(uuid4())}
    async with connection(case) as client:
        response = await rpc(client, token, name=name, arguments=arguments)
    if kind == "missing_token":
        assert response.status_code in {401, 403}
    else:
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"]
        if kind in {"unmapped", "employee_argument"}:
            code = "identity_not_enabled" if kind == "unmapped" else "invalid_arguments"
            assert code in result["content"][0]["text"]
    assert credential_reads == []
    assert captures(state) == []


@pytest.mark.parametrize("direction", ["lab_to_case", "case_to_lab"])
async def test_case_and_default_lab_tokens_cannot_cross_authentication(case, tmp_path, direction):
    _, _, state = case
    lab_support = importlib.import_module("internal_support")
    lab_host = importlib.import_module("internal_test")
    root = tmp_path / "lab"
    lab_support.init_state(root)
    lab_state = lab_support.load_state(root)
    if direction == "lab_to_case":
        output = root / "tokens" / "cross.jwt"
        lab_support.issue_token(root, user="user-a", service="read", output=output)
        token = output.read_text().strip()
        output.unlink()
        async with connection(case) as client:
            response = await rpc(client, token)
        target_state = state
    else:
        token = signed(case)
        app = lab_host.create_lab_app(lab_state, "read")
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                response = await rpc(client, token)
        target_state = lab_state
    assert response.status_code in {401, 403}
    assert captures(target_state) == []
    assert audit(target_state) == []


@pytest.mark.parametrize("kind", ["fixture_modified", "valid_new_fixture", "key_permissions"])
async def test_changed_case_configuration_fails_health_and_admission(case, monkeypatch, kind):
    _, support, state = case
    token = signed(case)
    credential_reads = observe_reads(case, monkeypatch)
    async with connection(case) as client:
        if kind == "key_permissions":
            state.credential_path("read").chmod(0o644)
        else:
            path = state.root / "fixture.json"
            fixture = json.loads(path.read_text())
            fixture["display_name"] = "Changed Synthetic Display"
            path.write_text(json.dumps(fixture))
            if kind == "valid_new_fixture":
                manifest_path = state.root / "state.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["fixture_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                manifest_path.write_text(json.dumps(manifest))
                assert support.load_case(state.root).display_name == "Changed Synthetic Display"
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 503
        assert (await rpc(client, token)).status_code == 503
    assert credential_reads == []
    assert captures(state) == []


async def test_case_mismatched_mock_response_is_not_exposed_as_employee_data(case):
    _, _, state = case
    token = signed(case)
    async with connection(case, scenario="mismatch") as client:
        response = await rpc(client, token)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"]
    assert "upstream_employee_mismatch" in result["content"][0]["text"]
    assert "structuredContent" not in result
    assert "CASE100" not in json.dumps(result)
    captured = captures(state)
    assert len(captured) == 1 and captured[0]["employee_id"] == "CASE100"
    assert {row["event"] for row in audit(state)} == {"query_started", "query_failed"}


def test_case_cannot_construct_reset_service(case):
    host, _, state = case
    with pytest.raises(BusinessError, match="^internal_case_state_invalid$"):
        host.create_case_app(state, "reset")
    assert not state.journal_path("read").exists()
    assert captures(state) == []
    assert not (state.root / "reset").exists()


async def test_case_subprocess_serve_cli_check_and_sigterm_preserve_audit(case):
    _, support, state = case
    lab_host = importlib.import_module("internal_test")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    assert port not in {8766, 8767, 8768}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; "
        f"sys.path.insert(0, {str(_PROJECT / 'tests' / 'legacy_support')!r}); "
        "from employee_case import run; raise SystemExit(run())",
        "serve",
        "--state-dir",
        str(state.root),
        "--port",
        str(port),
        cwd=_PROJECT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output = error = b""
    try:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=20)
        ready = json.loads(line)
        assert ready["status"] == "ready"
        assert ready["mode"] == support.MODE
        assert ready["service"] == "read"
        assert ready["real_business_called"] is False
        assert ready["url"] == f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=3) as client:
            response = await client.get(f"http://127.0.0.1:{port}/health/ready")
        assert response.status_code == 200
        assert response.json()["mode"] == support.MODE
        check = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; "
            f"sys.path.insert(0, {str(_PROJECT / 'tests' / 'legacy_support')!r}); "
            "from employee_case import run; raise SystemExit(run())",
            "check",
            "--state-dir",
            str(state.root),
            cwd=_PROJECT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            checked, check_error = await asyncio.wait_for(check.communicate(), timeout=25)
        finally:
            if check.returncode is None:
                check.kill()
                await check.communicate()
        assert check.returncode == 0, (checked, check_error)
        summary = json.loads(checked)
        assert summary["status"] == "passed"
        assert summary["mode"] == support.MODE
        assert summary["upstream_requests"] == 1
        assert summary["denied_extra_requests"] == 0
        assert summary["audit_events"] == 2
        assert summary["real_identity_verified"] is False
        assert summary["real_business_called"] is False
        assert summary["upstream"] == "mock_only"
        assert b"CASE100" not in checked + check_error
        assert b"Case Example" not in checked + check_error
        assert list((state.root / "tokens").iterdir()) == []
        assert lab_host.read_listener(state, "read")["state"] == "running"
        process.send_signal(signal.SIGTERM)
        output, error = await asyncio.wait_for(process.communicate(), timeout=12)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    assert process.returncode == 0
    assert b"PRIVATE KEY" not in output + error
    assert lab_host.read_listener(state, "read")["state"] == "stopped"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    assert len(captures(state)) == 1
    assert {row["event"] for row in audit(state)} >= {"query_started", "query_succeeded"}
