"""仅使用临时文件与已签名合成身份的运行边界测试。"""

import asyncio
import json
import os
import sqlite3
import warnings
from contextlib import asynccontextmanager

import httpx
import httpx2
import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from pydantic import ValidationError
from test_server import AUDIENCE, ISSUER, call, rpc, token

from biz_t100 import runtime
from biz_t100.cli import run
from biz_t100.client import T100Config, T100EmployeeClient
from biz_t100.models import BusinessError
from biz_t100.runtime import (
    RuntimeConfig,
    check_configuration,
    create_runtime_app,
    load_runtime_config,
)
from biz_t100.server import SCOPE


@pytest.fixture
def prepared(tmp_path):
    policy_file, key_file = tmp_path / "access.json", tmp_path / "service.secret"
    policy = {
        "auth_mode": "service-account",
        "revision": "test-1",
        "principals": [
            {
                "issuer": ISSUER,
                "subject": subject,
                "employee_id": employee,
                "allowed_clients": ["agent-a"],
                "enabled": True,
                "allow_employee_read": True,
            }
            for subject, employee in [("employee-a", "E001"), ("employee-b", "E002")]
        ],
    }
    policy_file.write_text(json.dumps(policy))
    key_file.write_text("synthetic-service-secret")
    policy_file.chmod(0o600)
    key_file.chmod(0o600)
    raw = {
        "identity": {"issuer": ISSUER, "audience": AUDIENCE, "allowed_clients": ["agent-a"]},
        "t100": {
            "url": "https://t100.example.invalid/api",
            "enterprise_id": "test",
            "host_product": "TEST",
            "host_ip": "127.0.0.1",
            "host_account": "test",
            "service_ip": "127.0.0.1",
            "service_id": "test",
        },
        "policy_file": str(policy_file),
        "credential_file": str(key_file),
        "journal_path": str(tmp_path / "journal" / "queries.sqlite3"),
        "max_in_flight": 1,
    }
    config_file = tmp_path / "runtime.json"
    config_file.write_text(json.dumps(raw))
    config_file.chmod(0o600)
    return load_runtime_config(config_file), raw, policy, config_file


def auth():
    key = RSAKeyPair.generate()
    return key, JWTVerifier(
        public_key=key.public_key, issuer=ISSUER, audience=AUDIENCE, required_scopes=[SCOPE]
    )


@asynccontextmanager
async def connection(app):
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            yield client


def mock_upstream(monkeypatch, handler):
    monkeypatch.setattr(
        runtime,
        "T100EmployeeClient",
        lambda config: T100EmployeeClient(config, transport=httpx.MockTransport(handler)),
    )


def employee_response(request):
    body = json.loads(request.content)
    employee = body["payload"]["std_data"]["parameter"]["employee_no"]
    return httpx.Response(
        200,
        json={
            "payload": {
                "std_data": {
                    "execution": {"code": "0"},
                    "parameter": {
                        "employee": [{"employee_no": employee, "full_name": "Synthetic"}]
                    },
                }
            }
        },
    )


async def test_preflight_reads_no_key_content_and_creates_no_journal(prepared):
    config, _, _, _ = prepared
    config.credential_file.write_bytes(b"\xffinvalid-key-content")
    assert await check_configuration(config) == {"status": "configuration_valid"}
    assert not config.journal_path.parent.exists()


@pytest.mark.parametrize(
    "case", ["missing", "extra-secret", "world-readable", "symlink", "source-collision"]
)
def test_load_masks_bad_configuration(prepared, case, tmp_path):
    config, raw, _, path = prepared
    if case == "missing":
        path = tmp_path / "missing"
    elif case == "extra-secret":
        raw["service_key"] = "must-not-appear"
        path.write_text(json.dumps(raw))
    elif case == "world-readable":
        path.chmod(0o644)
    elif case == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    else:
        raw["journal_path"] = str(path)
        path.write_text(json.dumps(raw))
    with pytest.raises(BusinessError) as caught:
        load_runtime_config(path)
    assert str(caught.value) == "runtime_configuration_invalid"
    assert caught.value.__context__ is None
    assert not config.journal_path.exists()


@pytest.mark.parametrize(
    "case",
    [
        "relative",
        "parent-traversal",
        "secret-collision",
        "wal-collision",
        "ancestor-collision",
        "zero-capacity",
        "long-budget",
    ],
)
def test_runtime_rejects_ambiguous_paths_and_limits(prepared, case):
    _, raw, _, _ = prepared
    if case == "relative":
        raw["policy_file"] = "access.json"
    elif case == "parent-traversal":
        raw["policy_file"] = "/tmp/a/../access.json"
    elif case == "secret-collision":
        raw["credential_file"] = raw["policy_file"]
    elif case == "wal-collision":
        raw["credential_file"] = raw["journal_path"] + "-wal"
    elif case == "ancestor-collision":
        raw["journal_path"] = raw["credential_file"] + "/queries.sqlite3"
    elif case == "zero-capacity":
        raw["max_in_flight"] = 0
    else:
        raw["budget_seconds"] = 20
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize(
    "case", ["issuer", "client", "key-permissions", "key-hardlink", "key-symlink"]
)
async def test_preflight_rejects_mismatched_policy_and_unsafe_key(prepared, case, tmp_path):
    config, _, policy, _ = prepared
    if case in {"issuer", "client"}:
        if case == "issuer":
            policy["principals"][0]["issuer"] = "https://other.invalid"
        else:
            policy["principals"][0]["allowed_clients"] = ["unconfirmed-client"]
        config.policy_file.write_text(json.dumps(policy))
    elif case == "key-permissions":
        config.credential_file.chmod(0o644)
    elif case == "key-hardlink":
        os.link(config.credential_file, tmp_path / "another-name")
    else:
        config.credential_file.unlink()
        config.credential_file.symlink_to(config.policy_file)
    with pytest.raises(BusinessError, match="^runtime_configuration_invalid$"):
        await check_configuration(config)
    assert not config.journal_path.exists()


def test_assembly_requires_explicit_auth_and_revalidates_copied_models(prepared):
    config, _, _, _ = prepared
    with pytest.raises(BusinessError):
        create_runtime_app(config, auth=None)
    _, verifier = auth()
    copied = config.model_copy(update={"max_in_flight": 0})
    with pytest.raises(BusinessError):
        create_runtime_app(copied, auth=verifier)
    create_runtime_app(config, auth=verifier)
    assert not config.journal_path.parent.exists()


async def test_health_is_local_and_adds_no_fake_business_events(prepared):
    config, _, _, _ = prepared
    _, verifier = auth()
    app = create_runtime_app(config, auth=verifier)
    async with connection(app) as client:
        assert (await client.get("/health/live")).json() == {"status": "live"}
        response = await client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready", "scope": "local"}
        denied = await rpc(client, None, "tools/list", {})
        assert denied.status_code in {401, 403}
        with sqlite3.connect(config.journal_path) as db:
            assert db.execute("SELECT COUNT(*) FROM query_events").fetchone()[0] == 0
        config.credential_file.chmod(0o644)
        assert (await client.get("/health/ready")).status_code == 503
        assert (await rpc(client, None, "tools/list", {})).status_code == 503
        assert (await client.get("/health/live")).status_code == 200
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        assert (await rpc(client, None, "tools/list", {})).status_code == 503


async def test_startup_fails_closed_without_key_and_never_creates_journal(prepared):
    config, _, _, _ = prepared
    _, verifier = auth()
    config.credential_file.unlink()
    app = create_runtime_app(config, auth=verifier)
    with pytest.raises(BusinessError, match="^runtime_configuration_invalid$"):
        async with connection(app):
            pytest.fail("must not start")
    assert not config.journal_path.parent.exists()


@pytest.mark.parametrize("profile", ["https", "http-test"])
async def test_assembled_runtime_preserves_self_isolation_and_revocation(
    prepared, monkeypatch, profile
):
    config, raw, policy, _ = prepared
    if profile == "http-test":
        raw["t100"].update(transport_profile=profile, url="http://10.1.2.3/api")
        config = RuntimeConfig.model_validate_json(json.dumps(raw))
    key, verifier = auth()
    requests = []

    def upstream(request):
        requests.append(json.loads(request.content))
        return employee_response(request)

    mock_upstream(monkeypatch, upstream)
    app = create_runtime_app(config, auth=verifier)
    async with connection(app) as client:
        for subject, expected in [("employee-a", "E001"), ("employee-b", "E002")]:
            result = await call(client, token(key, subject=subject))
            assert result["result"]["structuredContent"]["data"]["employee_id"] == expected
        denied = await call(client, token(key, subject="unmapped"))
        assert "identity_not_enabled" in json.dumps(denied)
        policy["principals"][0]["enabled"] = False
        config.policy_file.write_text(json.dumps(policy))
        denied = await call(client, token(key))
        assert "identity_not_enabled" in json.dumps(denied)
    assert len(requests) == 2
    assert {row["key"] for row in requests} == {"synthetic-service-secret"}
    with sqlite3.connect(config.journal_path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM query_events WHERE event='query_succeeded'"
            ).fetchone()[0]
            == 2
        )


async def test_capacity_refuses_without_dispatch_and_recovers_after_cancellation(
    prepared, monkeypatch
):
    config, _, _, _ = prepared
    key, verifier = auth()
    started, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def upstream(request):
        requests.append(request)
        started.set()
        await release.wait()
        return employee_response(request)

    mock_upstream(monkeypatch, upstream)
    app = create_runtime_app(config, auth=verifier)
    async with connection(app) as client:
        task = asyncio.create_task(call(client, token(key)))
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            rejected = await rpc(client, token(key), "tools/list", {})
            assert rejected.status_code == 503
            assert len(requests) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            result = await call(client, token(key, subject="employee-b"))
            assert result["result"]["structuredContent"]["data"]["employee_id"] == "E002"
        finally:
            release.set()
            if not task.done():
                task.cancel()
    assert len(requests) == 2


def test_cli_only_reports_local_preparation_and_does_not_create_state(prepared, capsys):
    config, _, _, source = prepared
    assert run(["check-config", "--config", str(source)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "configuration_valid"
    assert result["identity_provider_verified"] is False
    assert result["business_validated"] is False
    assert not config.journal_path.exists()
    config.policy_file.write_text("must-not-appear")
    assert run(["check-config", "--config", str(source)]) == 1
    output = capsys.readouterr().out
    assert "must-not-appear" not in output
    assert str(source) not in output
    assert run(["pending", "--journal", str(config.journal_path)]) == 1
    assert not config.journal_path.exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/api",
        "http://169.254.169.254/api",
        "http://8.8.8.8/api",
        "http://t100.example.invalid/api",
        "http://10.1.2.3/api?key=x",
        "http://user:secret@10.1.2.3/api",
        "http://[::1]/api",
        "https://10.1.2.3/api",
    ],
)
def test_http_test_profile_cannot_expand_to_other_targets(prepared, url):
    _, raw, _, _ = prepared
    with pytest.raises(ValidationError):
        T100Config.model_validate({**raw["t100"], "transport_profile": "http-test", "url": url})


def test_http_test_profile_requires_explicit_opt_in(prepared):
    _, raw, _, _ = prepared
    values = {**raw["t100"], "url": "http://10.1.2.3/api"}
    with pytest.raises(ValidationError):
        T100Config.model_validate(values)
    config = T100Config.model_validate({**values, "transport_profile": "http-test"})
    assert T100EmployeeClient(config).config.url == values["url"]


def test_double_root_sidecar_alias_is_rejected(prepared):
    _, raw, _, _ = prepared
    raw["credential_file"] = raw["journal_path"] + "-journal"
    raw["journal_path"] = "/" + raw["journal_path"]
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate_json(json.dumps(raw))


async def test_invalid_copied_config_does_not_leak_through_warnings(prepared):
    config, _, _, _ = prepared
    copied = config.model_copy(update={"max_in_flight": "synthetic-sensitive-config"})
    with warnings.catch_warnings(record=True) as messages:
        warnings.simplefilter("always")
        with pytest.raises(BusinessError, match="^runtime_configuration_invalid$"):
            await check_configuration(copied)
    assert not messages
