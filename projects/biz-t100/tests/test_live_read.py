"""针对默认真实传输装配的合成输入检查。"""

import importlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from fastmcp.server.auth.providers.jwt import RSAKeyPair

import biz_t100.access as access_module
from biz_t100 import runtime
from biz_t100.models import BusinessError
from biz_t100.server import TOOL_NAME

_PROJECT = Path(__file__).resolve().parents[1]


def write(path, value):
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(_PROJECT / "tests" / "legacy_support"))
    host = importlib.import_module("live_read")
    root = tmp_path / "live"
    for path in (root, root / "tokens", root / "read", root / "read" / "audit"):
        path.mkdir(mode=0o700)
    pair = RSAKeyPair.generate()
    write(root / "issuer-public.pem", pair.public_key)
    write(root / "issuer-private.pem", pair.private_key.get_secret_value())
    write(root / "read" / "service.key", "synthetic-private-key")
    policy = {
        "auth_mode": "service-account",
        "revision": "synthetic-v1",
        "principals": [
            {
                "issuer": "https://live-test.example.invalid",
                "subject": host.USER,
                "employee_id": "E001",
                "allowed_clients": ["synthetic-client"],
                "enabled": True,
                "allow_employee_read": True,
            }
        ],
    }
    write(root / "read" / "access.json", policy)
    raw = {
        "identity": {
            "issuer": "https://live-test.example.invalid",
            "audience": "synthetic-live",
            "allowed_clients": ["synthetic-client"],
        },
        "t100": {
            "url": "https://live-test.example.invalid/api",
            "enterprise_id": "synthetic-enterprise",
            "host_product": "TEST",
            "host_ip": "127.0.0.1",
            "host_account": "synthetic",
            "service_ip": "127.0.0.1",
            "service_id": "synthetic",
        },
        "policy_file": str(root / "read" / "access.json"),
        "credential_file": str(root / "read" / "service.key"),
        "journal_path": str(root / "read" / "audit" / "query.sqlite3"),
    }
    write(root / "runtime.json", raw)
    return host, host.load_live(root), raw, policy


@asynccontextmanager
async def connect(live):
    host, state, _, _ = live
    app = host.create_live_app(state, instance_id="synthetic-instance")
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            yield client


def signed(live, user="unmapped"):
    host, state, _, _ = live
    path = state.root / "tokens" / (str(uuid4()) + ".jwt")
    host.issue_live_token(state.root, output=path, user=user)
    token = path.read_text().strip()
    path.unlink()
    return token


async def rpc(client, token=None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    return await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": {}},
        },
    )


def test_build_uses_product_client_default_real_transport(live, monkeypatch):
    host, state, _, _ = live
    original, seen = runtime.T100EmployeeClient, []

    def capture(config, **kwargs):
        seen.append(kwargs)
        return original(config, **kwargs)

    monkeypatch.setattr(runtime, "T100EmployeeClient", capture)
    host.create_live_app(state)
    assert seen == [{}]
    assert (
        "MockTransport" not in (_PROJECT / "tests" / "legacy_support" / "live_read.py").read_text()
    )
    assert not state.config.journal_path.exists()


async def test_health_missing_token_and_unmapped_do_not_read_secrets(live, monkeypatch):
    host, state, _, _ = live
    token = signed(live)
    helper_read, business_read = host._read_private, access_module._private_bytes

    def public_only(path, maximum):
        assert path.name not in {"issuer-private.pem", "service.key"}
        return helper_read(path, maximum)

    def authorized_only(path, maximum):
        assert path.name not in {"issuer-private.pem", "service.key"}
        return business_read(path, maximum)

    async def forbidden_read(*args, **kwargs):
        pytest.fail("denied identity must not enter the upstream client")

    monkeypatch.setattr(runtime.T100EmployeeClient, "read", forbidden_read)
    monkeypatch.setattr(host, "_read_private", public_only)
    monkeypatch.setattr(access_module, "_private_bytes", authorized_only)
    async with connect(live) as client:
        health = await client.get("/health/ready")
        assert health.status_code == 200
        assert health.json()["mode"] == host.MODE
        assert health.json()["real_identity_verified"] is False
        assert (await rpc(client)).status_code in {401, 403}
        result = (await rpc(client, token)).json()["result"]
        assert result["isError"]
        error = json.loads(result["content"][0]["text"])
        assert error["error"] == "identity_not_enabled"
        assert host._attempted(state, error["request_id"]) is False


@pytest.mark.parametrize("change", ["runtime", "public"])
async def test_startup_snapshot_changes_disable_readiness(live, change):
    host, state, raw, _ = live
    async with connect(live) as client:
        assert (await client.get("/health/ready")).status_code == 200
        if change == "runtime":
            raw["max_in_flight"] = 2
            write(state.root / "runtime.json", raw)
        else:
            write(state.root / "issuer-public.pem", RSAKeyPair.generate().public_key)
        assert (await client.get("/health/ready")).status_code == 503
        assert (await rpc(client)).status_code == 503


async def test_policy_revocation_applies_without_restart(live):
    _, state, _, policy = live
    token = signed(live, user="simulated-live-user")
    async with connect(live) as client:
        policy["principals"][0]["enabled"] = False
        policy["revision"] = "revoked"
        write(state.config.policy_file, policy)
        assert (await client.get("/health/ready")).status_code == 200
        result = (await rpc(client, token)).json()["result"]
        assert result["isError"]
        assert json.loads(result["content"][0]["text"])["error"] == "identity_not_enabled"


@pytest.mark.parametrize("field", ["policy_file", "credential_file", "journal_path"])
def test_runtime_cannot_point_outside_private_case(live, field, tmp_path):
    host, state, raw, _ = live
    raw[field] = str(tmp_path / "elsewhere")
    write(state.root / "runtime.json", raw)
    with pytest.raises(BusinessError, match="^live_read_state_invalid$"):
        host.load_live(state.root)


@pytest.mark.parametrize(
    "target", ["runtime.json", "issuer-public.pem", "read/service.key", "read/access.json"]
)
def test_private_inputs_reject_symlink_and_hardlink(live, tmp_path, target):
    host, state, _, _ = live
    path, backup = state.root / target, tmp_path / "outside"
    path.rename(backup)
    path.symlink_to(backup)
    with pytest.raises(BusinessError, match="^live_read_state_invalid$"):
        host.load_live(state.root)
    path.unlink()
    os.link(backup, path)
    with pytest.raises(BusinessError, match="^live_read_state_invalid$"):
        host.load_live(state.root)


@pytest.mark.parametrize("target", ["read", "read/audit", "tokens"])
def test_group_readable_private_directories_rejected(live, target):
    host, state, _, _ = live
    (state.root / target).chmod(0o750)
    with pytest.raises(BusinessError, match="^live_read_state_invalid$"):
        host.load_live(state.root)


def test_public_key_cannot_contain_trailing_private_material(live):
    host, state, _, _ = live
    path = state.root / "issuer-public.pem"
    path.write_text(path.read_text() + (state.root / "issuer-private.pem").read_text())
    with pytest.raises(BusinessError, match="^live_read_state_invalid$"):
        host.load_live(state.root)


def test_token_output_is_private_exclusive_and_confined(live, tmp_path):
    host, state, _, _ = live
    path = state.root / "tokens" / "test.jwt"
    assert host.issue_live_token(state.root, output=path)["mode"] == host.MODE
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(BusinessError, match="^live_read_token_invalid$"):
        host.issue_live_token(state.root, output=path)
    with pytest.raises(BusinessError, match="^live_read_token_invalid$"):
        host.issue_live_token(state.root, output=tmp_path / "token.jwt")
    assert not (tmp_path / "token.jwt").exists()


def test_reset_and_simulated_scenarios_are_rejected(live):
    host, state, _, _ = live
    with pytest.raises(BusinessError, match="^live_read_service_invalid$"):
        host.create_live_app(state, "reset")
    with pytest.raises(BusinessError, match="^live_read_scenario_invalid$"):
        host.create_live_app(state, scenario="success")


def test_fixed_failure_does_not_print_sensitive_config(live, capsys):
    host, state, _, _ = live
    write(state.root / "runtime.json", "DO-NOT-DISCLOSE")
    assert host.run(["status", "--state-dir", str(state.root)]) == 1
    output = capsys.readouterr().out
    assert "DO-NOT-DISCLOSE" not in output
    assert json.loads(output)["error"] == "live_read_state_invalid"
    assert json.loads(output)["real_business_called"] is None
