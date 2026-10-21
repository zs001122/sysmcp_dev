"""独立的内部宿主检查：合成身份/上游，隔离的本地端口。"""

import asyncio
import fcntl
import importlib
import json
import os
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
_NAMES = {"read": TOOL_NAME, "reset": RESET_TOOL_NAME}


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(_PROJECT / "tests" / "legacy_support"))
    host = importlib.import_module("internal_test")
    support = importlib.import_module("internal_support")
    root = tmp_path / "lab"
    support.init_state(root)
    return host, support, support.load_state(root)


def signed(lab, service, user="user-a"):
    _, support, state = lab
    output = state.root / "tokens" / (str(uuid4()) + ".jwt")
    support.issue_token(state.root, user=user, service=service, output=output)
    value = output.read_text().strip()
    output.unlink()
    return value


def observe_reads(lab, monkeypatch):
    _, support, state = lab
    helper_reader = support._read_private
    business_reader = access_module._private_bytes
    key_reads = []

    def only_public_state(path, maximum):
        assert path != state.root / "issuer-private.pem"
        assert path not in {state.credential_path("read"), state.credential_path("reset")}
        return helper_reader(path, maximum)

    def authorized_business_read(path, maximum):
        assert path != state.root / "issuer-private.pem"
        if path.name == "service.key":
            key_reads.append(path)
        return business_reader(path, maximum)

    monkeypatch.setattr(support, "_read_private", only_public_state)
    monkeypatch.setattr(access_module, "_private_bytes", authorized_business_read)
    return key_reads


@asynccontextmanager
async def connection(lab, service):
    host, _, state = lab
    app = host.create_lab_app(state, service, instance_id="synthetic-host-check")
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            yield client


async def rpc(client, token, service, *, method="tools/call", operation=None):
    parameters = {}
    if method == "tools/call":
        arguments = (
            {}
            if service == "read"
            else {"confirm": True, "operation_id": operation or str(uuid4())}
        )
        parameters = {"name": _NAMES[service], "arguments": arguments}
    return await client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + token,
            "MCP-Protocol-Version": "2025-06-18",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": parameters},
    )


def capture_rows(state, service):
    path = state.capture_path(service)
    if not path.exists():
        return []
    return [json.loads(row) for row in path.read_text().splitlines() if row]


def audit_rows(state, service):
    table = "query_events" if service == "read" else "reset_events"
    with sqlite3.connect(state.journal_path(service).as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


@pytest.mark.parametrize("service", ["read", "reset"])
async def test_native_health_and_jwt_call_use_real_business_components(lab, monkeypatch, service):
    _, _, state = lab
    token = signed(lab, service)
    key_reads = observe_reads(lab, monkeypatch)
    operation = str(uuid4())
    async with connection(lab, service) as client:
        for endpoint in ("live", "ready"):
            health = await client.get("/health/" + endpoint)
            assert health.status_code == 200
            assert health.json() == {
                "status": endpoint,
                "mode": "internal-simulated",
                "service": service,
                "instance_id": "synthetic-host-check",
                "scope": "synthetic_local",
            }
        assert key_reads == []
        tools = await rpc(client, token, service, method="tools/list")
        assert tools.status_code == 200
        assert [tool["name"] for tool in tools.json()["result"]["tools"]] == [_NAMES[service]]
        assert key_reads == []
        response = await rpc(client, token, service, operation=operation)
        assert response.status_code == 200
        result = response.json()["result"]
        assert not result.get("isError")
        data = result["structuredContent"]
        if service == "read":
            assert data["data"]["employee_id"] == "E001"
            correlation = data["request_id"]
        else:
            assert data == {"operation_id": operation, "status": "upstream_acknowledged"}
            correlation = operation
    assert key_reads and set(key_reads) == {state.credential_path(service)}
    captured = capture_rows(state, service)
    assert len(captured) == 1
    assert captured[0]["enterprise_id"] == state.t100_config(service).enterprise_id
    assert captured[0]["employee_id"] == "E001"
    assert captured[0]["account_id"] == ("LOGIN-A" if service == "reset" else None)
    rows = audit_rows(state, service)
    expected_events = (
        {"query_started", "query_succeeded"}
        if service == "read"
        else {"reset_started", "reset_dispatched", "reset_succeeded"}
    )
    assert {row["event"] for row in rows} == expected_events
    for row in rows:
        assert row["request_id" if service == "read" else "operation_id"] == correlation
        assert (row["issuer"], row["subject"], row["client_id"], row["employee_id"]) == (
            state.issuer,
            "user-a",
            state.client_id(service),
            "E001",
        )


@pytest.mark.parametrize("service", ["read", "reset"])
@pytest.mark.parametrize("user", ["disabled", "unmapped"])
async def test_denied_identity_sends_zero_synthetic_requests(lab, monkeypatch, service, user):
    _, _, state = lab
    token = signed(lab, service, user)
    key_reads = observe_reads(lab, monkeypatch)
    async with connection(lab, service) as client:
        response = await rpc(client, token, service)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"]
    assert "identity_not_enabled" in result["content"][0]["text"]
    assert key_reads == []
    assert capture_rows(state, service) == []


async def test_read_token_is_rejected_by_reset_native_authentication(lab, monkeypatch):
    _, _, state = lab
    token = signed(lab, "read")
    key_reads = observe_reads(lab, monkeypatch)
    async with connection(lab, "reset") as client:
        response = await rpc(client, token, "reset")
    assert response.status_code in {401, 403}
    assert key_reads == []
    assert capture_rows(state, "reset") == []
    assert audit_rows(state, "reset") == []


@pytest.mark.parametrize("service", ["read", "reset"])
async def test_bad_credential_metadata_disables_ready_and_business_admission(
    lab, monkeypatch, service
):
    _, _, state = lab
    token = signed(lab, service)
    key_reads = observe_reads(lab, monkeypatch)
    async with connection(lab, service) as client:
        credential = state.credential_path(service)
        credential.chmod(0o644)
        try:
            assert (await client.get("/health/live")).status_code == 200
            ready = await client.get("/health/ready")
            assert ready.status_code == 503
            assert ready.json()["status"] == "not_ready"
            assert (await rpc(client, token, service)).status_code == 503
        finally:
            credential.chmod(0o600)
    assert key_reads == []
    assert capture_rows(state, service) == []


@pytest.mark.parametrize("service", ["read", "reset"])
async def test_changed_credential_digest_requires_restart_without_reading_key(
    lab, monkeypatch, service
):
    _, _, state = lab
    token = signed(lab, service)
    key_reads = observe_reads(lab, monkeypatch)
    async with connection(lab, service) as client:
        path = state.root / "state.json"
        manifest = json.loads(path.read_text())
        manifest["services"][service]["credential_sha256"] = "1" * 64
        path.write_text(json.dumps(manifest))
        assert (await client.get("/health/ready")).status_code == 503
        assert (await rpc(client, token, service)).status_code == 503
    assert key_reads == []
    assert capture_rows(state, service) == []


@pytest.mark.parametrize("service", ["read", "reset"])
async def test_same_service_flock_prevents_a_second_listener(lab, monkeypatch, service):
    host, _, state = lab
    path = state.policy_path(service).parent / "listener.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def forbidden_socket(*args, **kwargs):
        pytest.fail("a held service lock must reject before opening any socket")

    monkeypatch.setattr(host.socket, "socket", forbidden_socket)
    try:
        with pytest.raises(BusinessError, match="^internal_service_already_running$"):
            await host.serve(state, service, port=45678, scenario="success")
    finally:
        os.close(descriptor)
    assert not (path.parent / "listener.json").exists()
    assert capture_rows(state, service) == []


@pytest.mark.parametrize("service", ["read", "reset"])
async def test_subprocess_listener_sigterm_closes_its_own_temporary_port(lab, service):
    host, _, state = lab
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    assert port not in {8766, 8767}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; "
        f"sys.path.insert(0, {str(_PROJECT / 'tests' / 'legacy_support')!r}); "
        "from internal_test import run; raise SystemExit(run())",
        "serve",
        "--state-dir",
        str(state.root),
        "--service",
        service,
        "--port",
        str(port),
        cwd=_PROJECT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output = b""
    error = b""
    try:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=20)
        ready = json.loads(line)
        assert ready["status"] == "ready"
        assert ready["mode"] == "internal-simulated"
        assert ready["service"] == service
        assert ready["real_business_called"] is False
        assert ready["url"] == f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=3) as client:
            response = await client.get(f"http://127.0.0.1:{port}/health/ready")
        assert response.status_code == 200
        assert response.json()["service"] == service
        assert host.read_listener(state, service)["state"] == "running"
        process.send_signal(signal.SIGTERM)
        output, error = await asyncio.wait_for(process.communicate(), timeout=12)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    assert process.returncode in {0, -signal.SIGTERM}
    assert b"PRIVATE KEY" not in output + error
    assert host.read_listener(state, service)["state"] == "stopped"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    assert capture_rows(state, service) == []
