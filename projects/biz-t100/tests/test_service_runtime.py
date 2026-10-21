"""Persistent units use synthetic JWTs, captured upstream traffic and private temp state."""

import asyncio
import hashlib
import json
import socket
from contextlib import asynccontextmanager

import httpx
import httpx2
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from pydantic import ValidationError

from biz_t100 import service_runtime as module
from biz_t100.cli import run
from biz_t100.function_audit import FunctionAudit
from biz_t100.functions import SCOPES, TOOLS
from biz_t100.interface_client import T100InterfaceClient
from biz_t100.interfaces import EmployeeQueryRequest
from biz_t100.models import BusinessError, Caller
from biz_t100.service_runtime import (
    ScopedFunctionAccess,
    ServiceRuntimeConfig,
    check_service_configuration,
    create_service_runtime_app,
    load_service_runtime_config,
)

ISSUER = "https://synthetic-persistent.example.invalid"
AUDIENCE = "synthetic-persistent-read"
SUBJECT = "operator-a"
CLIENT = "synthetic-client"
SECRET = "synthetic-persistent-service-key"


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    key = RSAKeyPair.generate()
    public_file = tmp_path / "public.pem"
    public_file.write_text(key.public_key)
    public_file.chmod(0o600)
    credential = tmp_path / "service.key"
    credential.write_text(SECRET)
    credential.chmod(0o600)
    config = {
        "mode": "read",
        "identity": {"issuer": ISSUER, "audience": AUDIENCE, "allowed_clients": [CLIENT]},
        "public_key_file": str(public_file),
        "t100": {
            "url": "https://synthetic-t100.example.invalid/api",
            "enterprise_id": "SYNTHETIC",
            "host_product": "TEST",
            "host_ip": "127.0.0.1",
            "host_account": "synthetic",
            "service_ip": "127.0.0.1",
            "service_id": "synthetic",
        },
        "permissions_file": str(tmp_path / "permissions.json"),
        "credential_file": str(credential),
        "audit_path": str(tmp_path / "audit" / "operations.sqlite3"),
        "evidence_dir": str(tmp_path / "cases"),
        "port": 8781,
    }
    parsed = ServiceRuntimeConfig.model_validate_json(json.dumps(config))
    policy = {
        "enterprise_id": "SYNTHETIC",
        "upstream_config_digest": module.upstream_digest(parsed),
        "principals": [
            {
                "issuer": ISSUER,
                "subject": SUBJECT,
                "client_id": CLIENT,
                "enabled": True,
                "actions": list(SCOPES),
                "employee_ids": ["E001", "E002"],
                "account_targets": [
                    {"employee_no": "E001", "account_id": "LOGIN-A"},
                    {"employee_no": "E002", "account_id": "LOGIN-B"},
                ],
            }
        ],
    }
    private_json(parsed.permissions_file, policy)
    path = tmp_path / "service.json"
    private_json(path, config)
    requests = []

    def upstream(request):
        body = json.loads(request.content)
        assert body["key"] == SECRET
        requests.append(body)
        parameter = body["payload"]["std_data"]["parameter"]
        if body["service"]["name"] == "employee.list.data.query.get":
            number = parameter["employee_no"]
            response = {
                "employee": [{"employee_no": number, "full_name": "Synthetic " + number}],
                "has_next": False,
            }
        else:
            response = {}
        return httpx.Response(
            200,
            json={
                "payload": {"std_data": {"execution": {"code": "0"}, "parameter": response}},
            },
        )

    monkeypatch.setattr(
        module,
        "T100InterfaceClient",
        lambda value, **kwargs: T100InterfaceClient(
            value, transport=httpx.MockTransport(upstream), **kwargs
        ),
    )
    return path, config, policy, key, requests


def token(key, *, subject=SUBJECT, client=CLIENT, scopes=None, expires=120):
    return key.create_token(
        subject=subject,
        issuer=ISSUER,
        audience=AUDIENCE,
        scopes=list(SCOPES.values()) if scopes is None else scopes,
        additional_claims={"azp": client, "employee_no": "E999"},
        expires_in_seconds=expires,
    )


@asynccontextmanager
async def connection(path):
    app = create_service_runtime_app(load_service_runtime_config(path))
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Accept": "application/json, text/event-stream"},
        ) as client:
            yield client


async def rpc(client, key, method, params, *, bearer=None):
    response = await client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + (bearer or token(key))},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    return response


async def call(client, key, action, request, *, confirm=False, bearer=None):
    response = await rpc(
        client,
        key,
        "tools/call",
        {
            "name": TOOLS[action],
            "arguments": {"request": request, "confirm": confirm}
            if action != "employee_query"
            else {"request": request},
        },
        bearer=bearer,
    )
    assert response.status_code == 200
    packet = response.json()
    return packet["result"]


async def test_local_checks_read_no_credential_and_create_no_state(prepared, monkeypatch):
    path, _, _, _, requests = prepared
    config = load_service_runtime_config(path)
    original = module._private_bytes
    reads = []

    def capture(selected, maximum):
        reads.append(selected)
        return original(selected, maximum)

    monkeypatch.setattr(module, "_private_bytes", capture)
    assert (await check_service_configuration(config))["status"] == "configuration_valid"
    assert config.credential_file not in reads
    assert not config.audit_path.exists()
    assert not config.evidence_dir.exists()
    assert not requests


async def test_read_tool_listing_and_direct_write_rejection(prepared):
    path, _, _, key, requests = prepared
    async with connection(path) as client:
        tools = (await rpc(client, key, "tools/list", {})).json()["result"]["tools"]
        assert [item["name"] for item in tools] == [TOOLS["employee_query"]]
        result = await call(
            client,
            key,
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
        )
        assert result["isError"]
        assert "tool_not_available" in result["content"][0]["text"]
        assert not requests


@pytest.mark.parametrize(
    "actions,scopes,expected",
    [
        (list(SCOPES), [SCOPES["employee_query"]], {TOOLS["employee_query"]}),
        (
            ["employee_update", "account_maintain"],
            list(SCOPES.values()),
            {TOOLS["employee_update"], TOOLS["account_maintain"]},
        ),
    ],
)
async def test_one_maintenance_unit_filters_read_and_local_write_callers(
    prepared, actions, scopes, expected
):
    path, config, policy, pair, requests = prepared
    config["mode"] = "maintenance"
    policy["principals"][0]["actions"] = actions
    private_json(path, config)
    private_json(load_service_runtime_config(path).permissions_file, policy)
    async with connection(path) as client:
        response = await rpc(client, pair, "tools/list", {}, bearer=token(pair, scopes=scopes))
        assert {tool["name"] for tool in response.json()["result"]["tools"]} == expected
    assert not requests


async def test_explicit_gateway_host_and_origin_allowlist(prepared):
    path, config, _, key, requests = prepared
    config["allowed_hosts"] = ["host.docker.internal:8781"]
    private_json(path, config)
    packet = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Authorization": "Bearer " + token(key)}
    async with connection(path) as client:
        allowed = await client.post(
            "/mcp",
            headers={**headers, "Host": "host.docker.internal:8781"},
            json=packet,
        )
        assert allowed.status_code == 200
        assert allowed.json()["result"]["tools"][0]["name"] == TOOLS["employee_query"]
        denied_host = await client.post(
            "/mcp",
            headers={**headers, "Host": "unapproved.example.invalid:8781"},
            json=packet,
        )
        assert denied_host.status_code == 421
        denied_origin = await client.post(
            "/mcp",
            headers={
                **headers,
                "Host": "host.docker.internal:8781",
                "Origin": "https://unapproved.example.invalid",
            },
            json=packet,
        )
        assert denied_origin.status_code == 403
    assert not requests


async def test_query_uses_controlled_target_and_keeps_evidence(prepared):
    path, _, _, key, requests = prepared
    config = load_service_runtime_config(path)
    async with connection(path) as client:
        result = (await call(client, key, "employee_query", {"employee_no": "E001"}))[
            "structuredContent"
        ]
        assert result["status"] == "passed"
        assert result["data"]["employee"][0]["employee_no"] == "E001"
        assert result["real_identity_verified"] is False
    assert len(requests) == 1
    assert requests[0]["payload"]["std_data"]["parameter"]["employee_no"] == "E001"
    folder = config.evidence_dir / result["request_id"]
    assert {p.name for p in folder.iterdir()} == {"request.json", "result.json"}
    assert SUBJECT == json.loads((folder / "request.json").read_text())["caller"]["subject"]
    assert SECRET not in "".join(p.read_text() for p in folder.iterdir())
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in folder.iterdir())


@pytest.mark.parametrize(
    "query",
    [
        {"employee_no": "E999"},
        {"employee_name": "Synthetic"},
        {},
    ],
)
async def test_employee_or_name_cannot_escape_query_scope(prepared, query):
    path, _, _, key, requests = prepared
    async with connection(path) as client:
        response = await call(client, key, "employee_query", query)
        if not query:
            assert response["isError"] is True
            assert "invalid_arguments" in response["content"][0]["text"]
        else:
            result = response["structuredContent"]
            assert result["error"] == "permission_denied"
            assert result["upstream_attempted"] is False
    assert not requests


@pytest.mark.parametrize("case", ["missing", "forged", "expired", "wrong-client", "wrong-scope"])
async def test_native_identity_failures_prevent_upstream(prepared, case):
    path, _, _, key, requests = prepared
    supplied = {
        "forged": token(RSAKeyPair.generate()),
        "expired": token(key, expires=-10),
        "wrong-client": token(key, client="other-client"),
        "wrong-scope": token(key, scopes=[]),
    }.get(case)
    async with connection(path) as client:
        if case == "missing":
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {},
                },
            )
            assert response.status_code == 401
        else:
            response = await rpc(client, key, "tools/list", {}, bearer=supplied)
            packet = response.json()
            assert (
                response.status_code == 401 or "error" in packet or packet["result"].get("isError")
            )
    assert not requests


async def test_denied_access_does_not_read_service_key(prepared, monkeypatch):
    path, _, _, _, requests = prepared
    config = load_service_runtime_config(path)
    original = module._private_bytes
    reads = []

    def capture(selected, maximum):
        reads.append(selected)
        return original(selected, maximum)

    monkeypatch.setattr(module, "_private_bytes", capture)
    access = ScopedFunctionAccess(config)
    wrong = Caller(issuer=ISSUER, subject="unmapped", client_id=CLIENT)
    with pytest.raises(BusinessError, match="permission_denied"):
        await access.authorize(wrong, "employee_query", EmployeeQueryRequest(employee_no="E001"))
    assert config.credential_file not in reads
    correct = Caller(issuer=ISSUER, subject=SUBJECT, client_id=CLIENT)
    await access.authorize(correct, "employee_query", EmployeeQueryRequest(employee_no="E001"))
    assert config.credential_file in reads
    assert not requests


async def test_write_unit_preserves_existing_audit_and_account_pair_scope(prepared):
    path, config, _, key, requests = prepared
    config["mode"] = "write"
    private_json(path, config)
    parsed = load_service_runtime_config(path)
    async with connection(path) as client:
        tools = (await rpc(client, key, "tools/list", {})).json()["result"]["tools"]
        assert {item["name"] for item in tools} == {
            TOOLS[action] for action in module.WRITE_ACTIONS
        }
        intent = {
            "master": [
                {
                    "gzxa003": "E001",
                    "gzxa001": "LOGIN-B",
                    "gzxastus": "N",
                    "is_reproduce": "N",
                    "password_reset": "N",
                }
            ]
        }
        denied = (await call(client, key, "account_maintain", intent, confirm=True))[
            "structuredContent"
        ]
        assert denied["error"] == "permission_denied"
        assert not requests
        intent["master"][0]["gzxa001"] = "LOGIN-A"
        result = (await call(client, key, "account_maintain", intent, confirm=True))[
            "structuredContent"
        ]
        assert result["status"] == "acknowledged"
        assert result["business_effect_verified"] is False
        assert len(requests) == 2  # employee prerequisite + one account maintenance
    folder = parsed.evidence_dir / result["request_id"]
    assert {p.name for p in folder.iterdir()} == {
        "request.json",
        "effective-request.json",
        "upstream-write-response.json",
        "result.json",
    }
    assert SECRET not in "".join(p.read_text() for p in folder.iterdir())
    state = await FunctionAudit(parsed.audit_path, enterprise_id="SYNTHETIC").status(
        result["request_id"]
    )
    assert state["operation_state"] == "succeeded"


@pytest.mark.parametrize("status,reset", [("N", "N"), ("Y", "N"), ("Y", "Y")])
async def test_status_password_profile_accepts_only_requested_account_operations(
    prepared, status, reset
):
    path, config, policy, key, requests = prepared
    config["mode"] = "write"
    principal = policy["principals"][0]
    principal["actions"] = ["account_maintain"]
    principal["account_profile"] = "status-password"
    private_json(path, config)
    private_json(load_service_runtime_config(path).permissions_file, policy)
    request = {
        "master": [
            {
                "gzxa003": "E001",
                "gzxa001": "LOGIN-A",
                "gzxastus": status,
                "is_reproduce": "N",
                "password_reset": reset,
            }
        ]
    }
    async with connection(path) as client:
        supplied = token(key, scopes=[SCOPES["account_maintain"]])
        result = (
            await call(client, key, "account_maintain", request, confirm=True, bearer=supplied)
        )["structuredContent"]
        assert result["status"] == "acknowledged"
        assert result["business_effect_verified"] is False
    assert len(requests) == 2
    assert requests[1]["service"]["name"] == "user.account.create"
    assert requests[1]["payload"]["std_data"]["parameter"] == request


@pytest.mark.parametrize(
    "case", ["clear-detail", "role-detail", "copy", "disabled-reset", "null-detail"]
)
async def test_status_password_profile_rejects_expanded_effects_before_key_or_upstream(
    prepared, monkeypatch, case
):
    path, config, policy, key, requests = prepared
    config["mode"] = "write"
    principal = policy["principals"][0]
    principal["actions"] = ["account_maintain"]
    principal["account_profile"] = "status-password"
    principal["copy_sources"] = ["TEMPLATE-A"]
    private_json(path, config)
    parsed = load_service_runtime_config(path)
    private_json(parsed.permissions_file, policy)
    row = {
        "gzxa003": "E001",
        "gzxa001": "LOGIN-A",
        "gzxastus": "Y",
        "is_reproduce": "N",
        "password_reset": "N",
    }
    if case == "clear-detail":
        row["detail"] = []
    elif case == "role-detail":
        row["detail"] = [{"gzxbstus": "Y", "gzxb002": "0", "gzxb003": "ROLE-A"}]
    elif case == "copy":
        row.update(is_reproduce="Y", reproduce_acct="TEMPLATE-A")
    elif case == "disabled-reset":
        row.update(gzxastus="N", password_reset="Y")
    else:
        row["detail"] = None
    original_read = module._private_bytes
    key_reads = []

    def capture(selected, maximum):
        if selected == parsed.credential_file:
            key_reads.append(selected)
        return original_read(selected, maximum)

    monkeypatch.setattr(module, "_private_bytes", capture)
    async with connection(path) as client:
        response = await call(client, key, "account_maintain", {"master": [row]}, confirm=True)
        if case == "null-detail":
            assert response["isError"]
            assert "invalid_arguments" in response["content"][0]["text"]
        else:
            result = response["structuredContent"]
            assert result["error"] == "permission_denied"
            assert result["upstream_attempted"] is False
    assert not key_reads
    assert not requests


async def test_default_full_account_profile_retains_detail_and_combined_reset(prepared):
    path, config, _, key, requests = prepared
    config["mode"] = "write"
    private_json(path, config)
    request = {
        "master": [
            {
                "gzxa003": "E001",
                "gzxa001": "LOGIN-A",
                "gzxastus": "N",
                "is_reproduce": "N",
                "password_reset": "Y",
                "detail": [],
            }
        ]
    }
    async with connection(path) as client:
        result = (await call(client, key, "account_maintain", request, confirm=True))[
            "structuredContent"
        ]
        assert result["status"] == "acknowledged"
    assert len(requests) == 2
    assert requests[1]["payload"]["std_data"]["parameter"] == request


async def test_deployment_permission_revocation_keeps_zero_upstream(prepared):
    path, config, _, key, requests = prepared
    deployment = path.parent / "deployment.json"
    config["deployment_permissions_file"] = str(deployment)
    private_json(path, config)
    parsed = load_service_runtime_config(path)
    private_json(
        deployment,
        {
            "enterprise_id": "SYNTHETIC",
            "upstream_config_digest": module.upstream_digest(parsed),
            "queries_enabled": False,
            "writes_enabled": False,
            "write_targets": [],
        },
    )
    async with connection(path) as client:
        result = (await call(client, key, "employee_query", {"employee_no": "E001"}))[
            "structuredContent"
        ]
        assert result["error"] == "permission_denied"
        assert result["upstream_attempted"] is False
    assert not requests


async def test_fuzzy_upstream_response_does_not_release_another_employee(prepared, monkeypatch):
    path, _, _, key, requests = prepared

    def mismatch(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "payload": {
                    "std_data": {
                        "execution": {"code": "0"},
                        "parameter": {"employee": [{"employee_no": "E0019"}]},
                    }
                }
            },
        )

    monkeypatch.setattr(
        module,
        "T100InterfaceClient",
        lambda value, **kwargs: T100InterfaceClient(
            value,
            transport=httpx.MockTransport(mismatch),
            **kwargs,
        ),
    )
    async with connection(path) as client:
        result = (await call(client, key, "employee_query", {"employee_no": "E001"}))[
            "structuredContent"
        ]
        assert result["error"] == "upstream_employee_mismatch"
        assert "data" not in result
    assert len(requests) == 1


async def test_two_signed_callers_concurrently_return_only_their_target(prepared):
    path, _, policy, key, requests = prepared
    config = load_service_runtime_config(path)
    policy["principals"][0]["employee_ids"] = ["E001"]
    second = dict(policy["principals"][0], subject="operator-b", employee_ids=["E002"])
    policy["principals"].append(second)
    private_json(config.permissions_file, policy)
    async with connection(path) as client:
        a, b = await asyncio.gather(
            call(client, key, "employee_query", {"employee_no": "E001"}),
            call(
                client,
                key,
                "employee_query",
                {"employee_no": "E002"},
                bearer=token(key, subject="operator-b"),
            ),
        )
    assert a["structuredContent"]["data"]["employee"][0]["employee_no"] == "E001"
    assert b["structuredContent"]["data"]["employee"][0]["employee_no"] == "E002"
    assert a["structuredContent"]["request_id"] != b["structuredContent"]["request_id"]
    assert len(requests) == 2


async def test_unknown_account_write_blocks_next_request_without_redispatch(prepared, monkeypatch):
    path, config, _, key, requests = prepared
    config["mode"] = "write"
    private_json(path, config)

    def timeout(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["service"]["name"] == "user.account.create":
            raise httpx.ReadTimeout("synthetic timeout")
        number = body["payload"]["std_data"]["parameter"]["employee_no"]
        return httpx.Response(
            200,
            json={
                "payload": {
                    "std_data": {
                        "execution": {"code": "0"},
                        "parameter": {"employee": [{"employee_no": number}]},
                    }
                }
            },
        )

    monkeypatch.setattr(
        module,
        "T100InterfaceClient",
        lambda value, **kwargs: T100InterfaceClient(
            value,
            transport=httpx.MockTransport(timeout),
            **kwargs,
        ),
    )
    intent = {
        "master": [
            {
                "gzxa003": "E001",
                "gzxa001": "LOGIN-A",
                "gzxastus": "N",
                "is_reproduce": "N",
                "password_reset": "N",
            }
        ]
    }
    async with connection(path) as client:
        first = (await call(client, key, "account_maintain", intent, confirm=True))[
            "structuredContent"
        ]
        assert first["error"] == "upstream_timeout"
        second = (await call(client, key, "account_maintain", intent, confirm=True))[
            "structuredContent"
        ]
        assert second["error"] == "reset_pending_review"
        assert second["upstream_attempted"] is False
    assert len(requests) == 2


async def test_revocation_after_upstream_returns_no_employee_data(prepared, monkeypatch):
    path, _, policy, key, requests = prepared
    config = load_service_runtime_config(path)

    def revoke(request):
        requests.append(json.loads(request.content))
        policy["principals"][0]["enabled"] = False
        private_json(config.permissions_file, policy)
        return httpx.Response(
            200,
            json={
                "payload": {
                    "std_data": {
                        "execution": {"code": "0"},
                        "parameter": {"employee": [{"employee_no": "E001"}]},
                    }
                }
            },
        )

    monkeypatch.setattr(
        module,
        "T100InterfaceClient",
        lambda value, **kwargs: T100InterfaceClient(
            value,
            transport=httpx.MockTransport(revoke),
            **kwargs,
        ),
    )
    async with connection(path) as client:
        result = (await call(client, key, "employee_query", {"employee_no": "E001"}))[
            "structuredContent"
        ]
        assert result["status"] == "failed"
        assert result["error"] == "identity_not_enabled"
        assert result["upstream_attempted"] is True
        assert "data" not in result
    assert len(requests) == 1


@pytest.mark.parametrize("case", ["disabled", "rebound"])
async def test_result_fsync_revocation_rejects_delivery_without_rewriting_audit(
    prepared, monkeypatch, case
):
    path, _, policy, key, requests = prepared
    config = load_service_runtime_config(path)
    original_save = module._save_evidence
    original_read = module._private_bytes
    credential_reads = []
    before_delivery_check = None

    def capture_read(selected, maximum):
        if selected == config.credential_file:
            credential_reads.append(selected)
        return original_read(selected, maximum)

    def revoke_while_saved(selected, value):
        nonlocal before_delivery_check
        original_save(selected, value)
        if selected.name == "result.json":
            before_delivery_check = len(credential_reads)
            if case == "disabled":
                policy["principals"][0]["enabled"] = False
            else:
                policy["principals"][0]["employee_ids"] = ["E002"]
            private_json(config.permissions_file, policy)

    monkeypatch.setattr(module, "_private_bytes", capture_read)
    monkeypatch.setattr(module, "_save_evidence", revoke_while_saved)
    async with connection(path) as client:
        result = (await call(client, key, "employee_query", {"employee_no": "E001"}))[
            "structuredContent"
        ]
        assert result["status"] == "failed"
        assert result["error"] == (
            "identity_not_enabled" if case == "disabled" else "permission_denied"
        )
        assert result["delivery_state"] == "rejected_after_execution"
        assert result["upstream_attempted"] is True
        assert "data" not in result
    assert len(credential_reads) == before_delivery_check
    assert len(requests) == 1
    saved = config.evidence_dir / result["request_id"]
    assert json.loads((saved / "result.json").read_text())["status"] == "passed"
    assert json.loads((saved / "delivery-rejected.json").read_text())["status"] == "failed"
    state = await FunctionAudit(config.audit_path, enterprise_id="SYNTHETIC").status(
        result["request_id"]
    )
    assert state["operation_state"] == "succeeded"
    assert state["recorded_possible_dispatch"] is True


@pytest.mark.parametrize("mode", ["read", "maintenance"])
async def test_native_client_on_real_loopback_tcp(prepared, mode):
    path, config, _, key, requests = prepared
    config["mode"] = mode
    private_json(path, config)
    app = create_service_runtime_app(load_service_runtime_config(path))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        async with Client(f"http://127.0.0.1:{port}/mcp", auth=token(key), mode="legacy") as client:
            expected = {TOOLS["employee_query"]} if mode == "read" else set(TOOLS.values())
            assert {tool.name for tool in await client.list_tools()} == expected
            result = await client.call_tool(
                TOOLS["employee_query"], {"request": {"employee_no": "E001"}}
            )
            assert result.structured_content["status"] == "passed"
            assert result.structured_content["data"]["employee"][0]["employee_no"] == "E001"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()
    assert len(requests) == 1


def test_cli_check_service_remains_local(prepared, capsys):
    path, _, _, _, requests = prepared
    assert run(["check-service", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_valid"
    assert not requests


@pytest.mark.parametrize("case", ["relative", "collision", "invalid-mode", "hostname"])
def test_configuration_boundaries(prepared, case):
    _, config, _, _, _ = prepared
    if case == "relative":
        config["audit_path"] = "relative.sqlite3"
    elif case == "collision":
        config["public_key_file"] = config["credential_file"]
    elif case == "invalid-mode":
        config["mode"] = "all"
    else:
        config["host"] = "uncontrolled-host.example.invalid"
    with pytest.raises(ValidationError):
        ServiceRuntimeConfig.model_validate_json(json.dumps(config))


async def test_config_digest_change_rejects_without_upstream(prepared):
    path, _, policy, _, requests = prepared
    config = load_service_runtime_config(path)
    policy["upstream_config_digest"] = hashlib.sha256(b"different deployment").hexdigest()
    private_json(config.permissions_file, policy)
    with pytest.raises(BusinessError, match="runtime_configuration_invalid"):
        await check_service_configuration(config)
    assert not requests
