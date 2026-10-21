"""Connector-token issuance only uses synthetic signing material and local files."""

import json
from datetime import UTC, datetime

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from test_service_runtime import (
    AUDIENCE,
    CLIENT,
    ISSUER,
    SUBJECT,
    call,
    connection,
    private_json,
    rpc,
)

from biz_t100 import service_runtime as module
from biz_t100.cli import run
from biz_t100.functions import SCOPES, TOOLS
from biz_t100.models import BusinessError
from biz_t100.service_runtime import issue_gateway_token, load_service_runtime_config

pytest_plugins = ["test_service_runtime"]


@pytest.fixture
def issuance(prepared):
    path, config, policy, pair, requests = prepared
    policy["principals"][0]["actions"] = ["employee_query"]
    parsed = load_service_runtime_config(path)
    private_json(parsed.permissions_file, policy)
    signing_key = path.parent / "signing.pem"
    signing_key.write_text(pair.private_key.get_secret_value())
    signing_key.chmod(0o600)
    output = path.parent / ".runtime" / "connector" / "gateway.jwt"
    return path, config, policy, pair, requests, signing_key, output


def issue(issuance, **changes):
    path, _, _, _, _, signing_key, output = issuance
    arguments = {
        "signing_key": signing_key,
        "subject": SUBJECT,
        "client_id": CLIENT,
        "output": output,
    }
    arguments.update(changes)
    return issue_gateway_token(load_service_runtime_config(path), **arguments)


async def test_read_only_token_is_valid_and_prints_no_secret(issuance, monkeypatch):
    path, _, _, pair, requests, _, output = issuance
    config = load_service_runtime_config(path)
    original = module._private_bytes
    reads = []

    def capture(selected, maximum):
        reads.append(selected)
        return original(selected, maximum)

    monkeypatch.setattr(module, "_private_bytes", capture)
    result = issue(issuance)
    assert result["status"] == "issued"
    assert result["identity_mode"] == "temporary-internal-connector"
    assert result["real_identity_verified"] is False
    assert result["upstream_attempted"] is False
    assert config.credential_file not in reads
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert output.parent.parent.stat().st_mode & 0o777 == 0o700
    token = output.read_text().strip()
    verified = await JWTVerifier(
        public_key=pair.public_key,
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithm="RS256",
    ).verify_token(token)
    assert verified.subject == SUBJECT
    assert verified.client_id == CLIENT
    assert verified.scopes == [SCOPES["employee_query"]]
    assert verified.claims["exp"] - verified.claims["iat"] == 3600
    assert "employee_no" not in verified.claims
    assert datetime.fromisoformat(result["expires_at"]) == datetime.fromtimestamp(
        verified.claims["exp"], UTC
    )
    assert token not in json.dumps(result)
    assert not requests


async def test_write_token_has_only_account_scope_and_discovers_one_tool(issuance):
    path, config, policy, pair, requests, _, output = issuance
    config["mode"] = "write"
    policy["principals"][0]["actions"] = ["account_maintain"]
    policy["principals"][0]["account_profile"] = "status-password"
    private_json(path, config)
    private_json(load_service_runtime_config(path).permissions_file, policy)
    result = issue(issuance)
    supplied = output.read_text().strip()
    verified = await JWTVerifier(
        public_key=pair.public_key,
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithm="RS256",
    ).verify_token(supplied)
    assert verified.scopes == [SCOPES["account_maintain"]]
    assert result["status"] == "issued"
    async with connection(path) as client:
        listing = await rpc(client, pair, "tools/list", {}, bearer=supplied)
        tools = listing.json()["result"]["tools"]
        assert [tool["name"] for tool in tools] == [TOOLS["account_maintain"]]
        for action in ("employee_create", "employee_update"):
            response = await rpc(
                client,
                pair,
                "tools/call",
                {
                    "name": TOOLS[action],
                    "arguments": {
                        "request": {"employee": [{"employee_no": "E001"}]},
                        "confirm": True,
                    },
                },
                bearer=supplied,
            )
            packet = response.json()
            assert packet["result"]["isError"] is True
            assert "unauthenticated" in packet["result"]["content"][0]["text"]
    assert not requests


async def test_combined_gateway_token_discovers_and_calls_query_and_account_in_one_app(issuance):
    path, config, policy, pair, requests, _, output = issuance
    config["mode"] = "maintenance"
    principal = policy["principals"][0]
    principal["actions"] = ["employee_query", "account_maintain"]
    principal["account_profile"] = "status-password"
    private_json(path, config)
    private_json(load_service_runtime_config(path).permissions_file, policy)
    assert issue(issuance)["status"] == "issued"
    supplied = output.read_text().strip()
    verified = await JWTVerifier(
        public_key=pair.public_key, issuer=ISSUER, audience=AUDIENCE
    ).verify_token(supplied)
    assert set(verified.scopes) == {SCOPES["employee_query"], SCOPES["account_maintain"]}
    assert "employee_no" not in verified.claims
    async with connection(path) as client:
        tools = (await rpc(client, pair, "tools/list", {}, bearer=supplied)).json()["result"][
            "tools"
        ]
        assert {tool["name"] for tool in tools} == {
            TOOLS["employee_query"],
            TOOLS["account_maintain"],
        }
        query = (
            await call(client, pair, "employee_query", {"employee_no": "E001"}, bearer=supplied)
        )["structuredContent"]
        assert query["status"] == "passed"
        assert query["data"]["employee"][0]["employee_no"] == "E001"
        account = (
            await call(
                client,
                pair,
                "account_maintain",
                {
                    "master": [
                        {
                            "gzxa003": "E001",
                            "gzxa001": "LOGIN-A",
                            "gzxastus": "N",
                            "is_reproduce": "N",
                            "password_reset": "N",
                        }
                    ],
                },
                confirm=True,
                bearer=supplied,
            )
        )["structuredContent"]
        assert account["status"] == "acknowledged"
        assert account["business_effect_verified"] is False
        assert account["deployment_mode"] == query["deployment_mode"] == "maintenance"
        assert len(requests) == 3  # query + account prerequisite + account write
        for action in ("employee_create", "employee_update"):
            denied = await rpc(
                client,
                pair,
                "tools/call",
                {
                    "name": TOOLS[action],
                    "arguments": {
                        "request": {"employee": [{"employee_no": "E001"}]},
                        "confirm": True,
                    },
                },
                bearer=supplied,
            )
            assert denied.json()["result"]["isError"] is True
            assert "unauthenticated" in denied.json()["result"]["content"][0]["text"]
        assert len(requests) == 3


@pytest.mark.parametrize(
    "case",
    [
        "query-only",
        "account-only",
        "employee-write",
        "full-profile",
        "empty-employees",
        "empty-accounts",
        "account-employee-outside-query-range",
        "broad-search",
    ],
)
def test_combined_gateway_token_rejects_broader_or_inconsistent_capabilities(issuance, case):
    path, config, policy, _, requests, _, output = issuance
    config["mode"] = "maintenance"
    principal = policy["principals"][0]
    principal["actions"] = ["employee_query", "account_maintain"]
    principal["account_profile"] = "status-password"
    if case == "query-only":
        principal["actions"] = ["employee_query"]
    elif case == "account-only":
        principal["actions"] = ["account_maintain"]
    elif case == "employee-write":
        principal["actions"].append("employee_update")
    elif case == "full-profile":
        principal["account_profile"] = "full"
    elif case == "empty-employees":
        principal["employee_ids"] = []
    elif case == "empty-accounts":
        principal["account_targets"] = []
    elif case == "broad-search":
        principal["allow_search"] = True
    else:
        principal["employee_ids"] = ["E001"]  # LOGIN-B still targets E002.
    private_json(path, config)
    private_json(load_service_runtime_config(path).permissions_file, policy)
    with pytest.raises(BusinessError, match="gateway_token_not_allowed"):
        issue(issuance)
    assert not output.exists()
    assert not requests


@pytest.mark.parametrize("case", ["full-profile", "no-account-target"])
def test_write_gateway_token_requires_status_password_profile_and_target(issuance, case):
    path, config, policy, _, requests, _, output = issuance
    config["mode"] = "write"
    principal = policy["principals"][0]
    principal["actions"] = ["account_maintain"]
    principal["account_profile"] = "full" if case == "full-profile" else "status-password"
    if case == "no-account-target":
        principal["account_targets"] = []
    private_json(path, config)
    private_json(load_service_runtime_config(path).permissions_file, policy)
    with pytest.raises(BusinessError, match="gateway_token_not_allowed"):
        issue(issuance)
    assert not output.exists()
    assert not requests


@pytest.mark.parametrize(
    "mode,actions",
    [
        ("read", ["account_maintain"]),
        ("write", ["employee_query"]),
        ("write", ["employee_create"]),
        ("write", ["employee_update"]),
        ("write", ["account_maintain", "employee_update"]),
        ("write", ["account_maintain", "employee_create", "employee_update"]),
    ],
)
def test_gateway_token_rejects_wrong_unit_and_mixed_capabilities(issuance, mode, actions):
    path, config, policy, _, requests, _, output = issuance
    config["mode"] = mode
    policy["principals"][0]["actions"] = actions
    private_json(path, config)
    private_json(load_service_runtime_config(path).permissions_file, policy)
    with pytest.raises(BusinessError, match="gateway_token_not_allowed"):
        issue(issuance)
    assert not output.exists()
    assert not requests


@pytest.mark.parametrize("case", ["write", "unmapped", "disabled", "wrong-client", "write-scope"])
def test_disallowed_principal_or_unit_produces_no_token(issuance, case):
    path, config, policy, _, requests, _, output = issuance
    arguments = {}
    if case == "write":
        config["mode"] = "write"
        private_json(path, config)
    elif case == "unmapped":
        arguments["subject"] = "not-an-approved-subject"
    elif case == "wrong-client":
        arguments["client_id"] = "other-client"
    elif case == "disabled":
        policy["principals"][0]["enabled"] = False
    else:
        policy["principals"][0]["actions"].append("account_maintain")
    private_json(load_service_runtime_config(path).permissions_file, policy)
    with pytest.raises(BusinessError):
        issue(issuance, **arguments)
    assert not output.exists()
    assert not requests


@pytest.mark.parametrize("lifetime", [0, -1, 3601, True, 1.5])
def test_lifetime_is_bounded_integer(issuance, lifetime):
    with pytest.raises(BusinessError, match="gateway_token_not_allowed"):
        issue(issuance, lifetime_seconds=lifetime)
    assert not issuance[-1].exists()


def test_wrong_private_key_produces_no_token(issuance):
    _, _, _, _, requests, signing_key, output = issuance
    signing_key.write_text(RSAKeyPair.generate().private_key.get_secret_value())
    with pytest.raises(BusinessError, match="gateway_token_configuration_invalid"):
        issue(issuance)
    assert not output.exists()
    assert not requests


def test_t100_credential_cannot_be_read_as_a_signing_key(issuance, monkeypatch):
    path, _, _, _, requests, _, output = issuance
    config = load_service_runtime_config(path)
    original = module._private_bytes
    reads = []

    def capture(selected, maximum):
        reads.append(selected)
        return original(selected, maximum)

    monkeypatch.setattr(module, "_private_bytes", capture)
    with pytest.raises(BusinessError, match="gateway_token_configuration_invalid"):
        issue(issuance, signing_key=config.credential_file)
    assert config.credential_file not in reads
    assert not output.exists()
    assert not requests


@pytest.mark.parametrize("case", ["key-public", "output-public", "symlink", "parent-public"])
def test_unsafe_private_paths_do_not_get_overwritten(issuance, case):
    _, _, _, _, requests, signing_key, output = issuance
    output.parent.mkdir(mode=0o700, parents=True)
    output.write_text("previous-private-token")
    output.chmod(0o600)
    if case == "key-public":
        signing_key.chmod(0o644)
    elif case == "output-public":
        output.chmod(0o644)
    elif case == "parent-public":
        output.parent.chmod(0o755)
    else:
        output.unlink()
        output.symlink_to(signing_key)
    with pytest.raises(BusinessError, match="gateway_token_configuration_invalid"):
        issue(issuance)
    if case == "symlink":
        assert output.is_symlink()
        assert signing_key.read_text().startswith("-----BEGIN PRIVATE KEY-----")
    else:
        assert output.read_text() == "previous-private-token"
    assert not requests


async def test_rotation_atomically_replaces_private_file(issuance):
    _, _, _, pair, requests, _, output = issuance
    issue(issuance, lifetime_seconds=60)
    old_inode = output.stat().st_ino
    result = issue(issuance, lifetime_seconds=120)
    assert output.stat().st_ino != old_inode
    verified = await JWTVerifier(
        public_key=pair.public_key, issuer=ISSUER, audience=AUDIENCE
    ).verify_token(output.read_text().strip())
    assert verified.claims["exp"] - verified.claims["iat"] == 120
    assert result["status"] == "issued"
    assert output.stat().st_mode & 0o777 == 0o600
    assert {item.name for item in output.parent.iterdir()} == {output.name}
    assert not requests


def test_replace_failure_preserves_original_and_removes_temporary(issuance, monkeypatch):
    _, _, _, _, _, _, output = issuance
    output.parent.mkdir(mode=0o700, parents=True)
    output.write_text("original-private-token")
    output.chmod(0o600)

    def refused(*args):
        raise OSError("synthetic replacement failure")

    monkeypatch.setattr(module.os, "replace", refused)
    with pytest.raises(BusinessError, match="gateway_token_configuration_invalid"):
        issue(issuance)
    assert output.read_text() == "original-private-token"
    assert {item.name for item in output.parent.iterdir()} == {output.name}


@pytest.mark.parametrize("target", ["configuration", "credential", "signing", "audit", "evidence"])
def test_token_output_cannot_replace_product_state(issuance, target):
    path, _, _, _, _, signing, _ = issuance
    config = load_service_runtime_config(path)
    destination = {
        "configuration": path,
        "credential": config.credential_file,
        "signing": signing,
        "audit": config.audit_path,
        "evidence": config.evidence_dir / "result.json",
    }[target]
    with pytest.raises(BusinessError, match="gateway_token_configuration_invalid"):
        issue(issuance, output=destination)


def test_cli_only_returns_file_expiry_and_temporary_identity(issuance, capsys):
    path, _, _, _, requests, signing, output = issuance
    assert (
        run(
            [
                "issue-gateway-token",
                "--config",
                str(path),
                "--signing-key",
                str(signing),
                "--subject",
                SUBJECT,
                "--client-id",
                CLIENT,
                "--output",
                str(output),
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    result = json.loads(printed)
    assert result["token_file"] == str(output)
    assert output.read_text().strip() not in printed
    assert not requests
