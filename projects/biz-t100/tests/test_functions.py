"""Four native FastMCP tools, private request grants and captured T100 traffic."""

import asyncio
import copy
import json
import sqlite3
import warnings
from contextlib import asynccontextmanager

import anyio
import httpx
import httpx2
import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

import biz_t100.functions as module
from biz_t100.client import T100Config
from biz_t100.function_audit import FunctionAudit
from biz_t100.functions import (
    EMPLOYEE_UPDATE_PRESERVE_FIELDS,
    SCOPES,
    TOOLS,
    FileFunctionAccess,
    FunctionPermission,
    FunctionService,
    create_function_app,
    employee_update_wire_request,
    materialize_employee_update,
    request_digest,
    request_targets,
)
from biz_t100.interface_client import T100InterfaceClient
from biz_t100.interfaces import REQUEST_MODELS, SERVICES
from biz_t100.models import Caller
from biz_t100.server import IdentityProfile

ISSUER = "https://synthetic-operator.example.invalid"
AUDIENCE = "synthetic-maintenance"
CALLER = Caller(issuer=ISSUER, subject="operator-a", client_id="synthetic-client")
SECRET = "synthetic-function-service-key"


def employee(number="E001", **changes):
    return {
        "employee_no": number,
        "full_name": "Synthetic Employee " + number,
        "department_no": "DEMO",
        "status": "Y",
        "last_name": "Synthetic",
        "middle_name": "",
        "name": "Employee " + number,
        "job_title": "",
        "bank_account": "",
        "account_no": "",
        "email": "",
        "telephone": "",
        "mobile_phone": "",
        **changes,
    }


def request_for(action):
    values = {
        "employee_query": {
            "employee_name": "Synthetic",
            "use_has_next": True,
            "page_size": 10,
            "page_no": 1,
        },
        "employee_create": {
            "employee": [employee("E003", email="e3@example.invalid", remark="Test")]
        },
        "employee_update": {"employee": [{"employee_no": "E001", "status": "N"}]},
        "account_maintain": {
            "master": [
                {
                    "gzxastus": "N",
                    "gzxa001": "LOGIN-A",
                    "gzxa003": "E001",
                    "is_reproduce": "Y",
                    "reproduce_acct": "TEMPLATE-A",
                    "password_reset": "Y",
                    "detail": [
                        {
                            "gzxbstus": "Y",
                            "gzxb002": "0",
                            "gzxb003": "ROLE-A",
                            "detail1": [{"gzxcstus": "Y", "gzxc004": "SITE-A", "gzxc007": "N"}],
                        }
                    ],
                }
            ],
        },
    }
    return REQUEST_MODELS[action].model_validate(values[action])


class Harness:
    def __init__(self, folder, monkeypatch):
        self.root = folder
        self.key = RSAKeyPair.generate()
        self.policy_path = folder / "permission.json"
        self.key_path = folder / "service.key"
        self.key_path.write_text(SECRET)
        self.key_path.chmod(0o600)
        self.requests = []
        self.key_reads = []
        self.rows = {row["employee_no"]: row for row in (employee(), employee("E002"))}
        self.fail_write = False
        self.after_write = None
        self.config = T100Config(
            url="https://synthetic-t100.example.invalid/api",
            enterprise_id="SYNTHETIC-ENT",
            host_product="SYNTHETIC",
            host_ip="127.0.0.1",
            host_account="synthetic-service",
            service_ip="127.0.0.1",
            service_id="synthetic",
        )
        self.audit = FunctionAudit(
            folder / "audit" / "functions.sqlite3", enterprise_id=self.config.enterprise_id
        )
        self.access = FileFunctionAccess(
            self.policy_path, self.key_path, enterprise_id=self.config.enterprise_id
        )
        self.client = T100InterfaceClient(self.config, transport=httpx.MockTransport(self.upstream))
        self.service = FunctionService(
            enterprise_id=self.config.enterprise_id,
            access=self.access,
            client=self.client,
            audit=self.audit,
        )
        original = module._private_bytes

        def read(path, limit):
            if path == self.key_path:
                self.key_reads.append(path)
            return original(path, limit)

        async def forbidden(*args, **kwargs):
            pytest.fail("Function tests must never access a real network")

        monkeypatch.setattr(module, "_private_bytes", read)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
        monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", forbidden)

    def approve(self, action, request, **changes):
        employees, accounts = request_targets(action, request)
        value = FunctionPermission(
            issuer=ISSUER,
            subject=CALLER.subject,
            client_id=CALLER.client_id,
            enterprise_id=self.config.enterprise_id,
            enabled=True,
            action=action,
            request_digest=request_digest(action, request),
            employee_ids=employees,
            account_ids=accounts,
        ).model_dump(mode="json")
        value.update(changes)
        self.policy_path.write_text(json.dumps(value))
        self.policy_path.chmod(0o600)

    async def upstream(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        assert body["key"] == SECRET
        assert body["datakey"] == {"EntId": self.config.enterprise_id}
        action = next(
            action for action, service in SERVICES.items() if service == body["service"]["name"]
        )
        parameters = body["payload"]["std_data"]["parameter"]
        if action == "employee_query":
            selected = (
                [self.rows[parameters["employee_no"]]]
                if parameters.get("employee_no") in self.rows
                else []
                if "employee_no" in parameters
                else list(self.rows.values())
            )
            output = {
                "employee": copy.deepcopy(selected),
                "total_results": len(selected),
                "has_next": False,
            }
        else:
            if self.fail_write:
                raise httpx.ReadTimeout("synthetic-private-timeout", request=request)
            if action in {"employee_create", "employee_update"}:
                for row in parameters["employee"]:
                    self.rows.setdefault(row["employee_no"], {}).update(row)
                output = {
                    "employee": [
                        {
                            "source_no": row["employee_no"],
                            "erp_no": row["employee_no"],
                            "execute_description": "",
                        }
                        for row in parameters["employee"]
                    ]
                }
            else:
                output = {}
            if self.after_write:
                self.after_write()
        return httpx.Response(
            200, json={"payload": {"std_data": {"execution": {"code": "0"}, "parameter": output}}}
        )

    async def execute(self, action, request, *, confirm=True):
        return await self.service.execute(
            action, request, CALLER, check_caller=lambda: CALLER, confirm=confirm
        )

    @asynccontextmanager
    async def connection(self):
        app = create_function_app(
            auth=JWTVerifier(public_key=self.key.public_key, issuer=ISSUER, audience=AUDIENCE),
            identity=IdentityProfile(
                issuer=ISSUER, audience=AUDIENCE, allowed_clients=frozenset({CALLER.client_id})
            ),
            service=self.service,
        )
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                yield client

    def token(self, action, **changes):
        values = {
            "subject": CALLER.subject,
            "issuer": ISSUER,
            "audience": AUDIENCE,
            "scopes": [SCOPES[action]],
            "additional_claims": {"azp": CALLER.client_id},
        }
        values.update(changes)
        return self.key.create_token(**values)


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    result = Harness(tmp_path, monkeypatch)
    await result.audit.prepare()
    return result


async def rpc(client, token, method, params):
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    return await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def tool_arguments(action, request, **extra):
    values = {"request": request.model_dump(mode="json", exclude_unset=True)}
    if action != "employee_query":
        values["confirm"] = True
    return {"name": TOOLS[action], "arguments": {**values, **extra}}


@pytest.mark.parametrize("action", tuple(REQUEST_MODELS))
async def test_four_native_tools_preserve_fields_and_distinguish_verified_from_ack(harness, action):
    request = request_for(action)
    harness.approve(action, request)
    token = harness.token(action)
    async with harness.connection() as client:
        listing = await rpc(client, token, "tools/list", {})
        assert {tool["name"] for tool in listing.json()["result"]["tools"]} == set(TOOLS.values())
        response = await rpc(client, token, "tools/call", tool_arguments(action, request))
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False, result
    value = result["structuredContent"]
    expected = {
        "employee_query": ("passed", 1, 0),
        "employee_create": ("verified", 1, 1),
        "employee_update": ("verified", 2, 1),
        "account_maintain": ("acknowledged", 1, 1),
    }[action]
    assert (value["status"], value["read_requests"], value["write_requests"]) == expected
    assert value["business_effect_verified"] is (action in {"employee_create", "employee_update"})
    observed = next(
        body for body in harness.requests if body["service"]["name"] == SERVICES[action]
    )
    expected = (
        employee_update_wire_request(
            request, materialize_employee_update(request, {"E001": employee()})
        ).parameter()
        if action == "employee_update"
        else request.parameter()
    )
    assert observed["payload"]["std_data"]["parameter"] == expected
    audit = await harness.audit.status(value["request_id"])
    assert audit["operation_state"] == "succeeded"
    assert audit["subject"] == CALLER.subject
    assert audit["employee_ids"] == list(request_targets(action, request)[0])
    assert SECRET not in json.dumps(value) + json.dumps(audit)
    if action == "employee_query":
        assert [row["employee_no"] for row in value["data"]["employee"]] == ["E001", "E002"]
    elif action == "employee_update":
        assert harness.rows["E001"]["status"] == "N"
        assert harness.rows["E001"]["full_name"] == employee()["full_name"]
        assert value["data"]["verification"][0]["verified_fields"] == sorted(
            {"employee_no", *EMPLOYEE_UPDATE_PRESERVE_FIELDS}
        )
    elif action == "employee_create":
        assert harness.rows["E003"] == request.parameter()["employee"][0]
    else:
        assert value["data"]["acknowledged"] is True
        assert value["data"]["business_effect_verified"] is False


@pytest.mark.parametrize("kind", ["missing", "issuer", "subject", "client", "scope"])
async def test_native_identity_and_scope_rejections_have_zero_upstream_and_key_reads(harness, kind):
    action = "employee_query"
    request = request_for(action)
    harness.approve(action, request)
    changes = {
        "missing": {},
        "issuer": {"issuer": "https://untrusted.example.invalid"},
        "subject": {"subject": "unapproved-operator"},
        "client": {"additional_claims": {"azp": "unapproved-client"}},
        "scope": {"scopes": []},
    }[kind]
    token = None if kind == "missing" else harness.token(action, **changes)
    async with harness.connection() as client:
        response = await rpc(client, token, "tools/call", tool_arguments(action, request))
    assert response.status_code in {200, 401, 403}
    if response.status_code == 200:
        result = response.json()["result"]
        assert result["isError"] or result["structuredContent"]["status"] == "failed"
    assert harness.requests == harness.key_reads == []


@pytest.mark.parametrize("kind", ["disabled", "digest", "target", "enterprise", "changed_request"])
async def test_controlled_request_permission_is_checked_before_service_key(harness, kind):
    action = "employee_update"
    request = request_for(action)
    changes = {
        "disabled": {"enabled": False},
        "digest": {"request_digest": "f" * 64},
        "target": {"employee_ids": ["E002"]},
        "enterprise": {"enterprise_id": "OTHER"},
        "changed_request": {},
    }[kind]
    harness.approve(action, request, **changes)
    if kind == "changed_request":
        request = REQUEST_MODELS[action].model_validate(
            {"employee": [{"employee_no": "E001", "status": "Y"}]}
        )
    value = await harness.execute(action, request)
    assert value["status"] == "failed"
    assert value["error"] == ("identity_not_enabled" if kind == "disabled" else "permission_denied")
    assert harness.requests == harness.key_reads == []


async def test_missing_confirmation_is_rejected_before_key_or_audit_reservation(harness):
    request = request_for("employee_update")
    harness.approve("employee_update", request)
    result = await harness.execute("employee_update", request, confirm=False)
    assert result["error"] == "confirmation_required"
    assert harness.requests == harness.key_reads == []


async def test_invalid_native_arguments_do_not_echo_submitted_data(harness):
    action = "employee_update"
    request = request_for(action)
    harness.approve(action, request)
    async with harness.connection() as client:
        response = await rpc(
            client,
            harness.token(action),
            "tools/call",
            tool_arguments(action, request, credential="synthetic-injected-never-echo"),
        )
    result = response.json()["result"]
    assert result["isError"] is True
    assert "invalid_arguments" in json.dumps(result)
    assert "synthetic-injected-never-echo" not in json.dumps(result)
    assert harness.requests == harness.key_reads == []


async def test_nested_request_mutation_is_revalidated_before_authorization(harness):
    request = request_for("employee_update")
    harness.approve("employee_update", request)
    request.employee.append({"employee_no": "E002", "status": "INVALID"})
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        result = await harness.execute("employee_update", request)
    assert seen == []
    assert result["status"] == "failed"
    assert harness.requests == harness.key_reads == []


async def test_unknown_write_keeps_target_lock_and_does_not_retry(harness):
    action = "employee_update"
    request = request_for(action)
    harness.approve(action, request)
    harness.fail_write = True
    first = await harness.execute(action, request)
    assert first["error"] == "upstream_timeout"
    assert (first["read_requests"], first["write_requests"]) == (1, 1)
    assert first["business_effect_verified"] is False
    assert (await harness.audit.status(first["request_id"]))["operation_state"] == "unknown"
    second = await harness.execute(action, request)
    assert second["error"] == "reset_pending_review"
    assert (second["read_requests"], second["write_requests"]) == (0, 0)
    assert len(harness.requests) == 2


async def test_revocation_after_real_client_dispatch_withholds_data_and_retains_unknown(harness):
    action = "employee_update"
    request = request_for(action)
    harness.approve(action, request)
    harness.after_write = lambda: harness.approve(action, request, enabled=False)
    result = await harness.execute(action, request)
    assert result["status"] == "failed"
    assert result["error"] == "identity_not_enabled"
    assert "data" not in result
    assert result["business_effect_verified"] is False
    assert (result["read_requests"], result["write_requests"]) == (1, 1)
    assert (await harness.audit.status(result["request_id"]))["operation_state"] == "unknown"


@pytest.mark.parametrize(
    ("action", "changed"),
    [
        ("employee_update", {"modify_date": "20260930010101"}),
        ("employee_update", {"status": "N", "modify_date": "20260930010101"}),
        ("employee_create", {"create_date": "20260930010101"}),
    ],
)
async def test_submitted_unverified_timestamps_never_claim_full_effect(harness, action, changed):
    row = {"employee_no": "E001"} if action == "employee_update" else employee("E003")
    row.update(changed)
    request = REQUEST_MODELS[action].model_validate({"employee": [row]})
    harness.approve(action, request)
    result = await harness.execute(action, request)
    assert result["status"] == "acknowledged"
    assert result["business_effect_verified"] is False
    assert result["data"]["business_effect_verified"] is False
    observation = result["data"]["verification"][0]
    excluded = {"create_date", "modify_date"} & changed.keys()
    assert observation["unverified_fields"] == sorted(excluded)
    expected_fields = row.keys() - excluded
    if action == "employee_update":
        expected_fields |= set(EMPLOYEE_UPDATE_PRESERVE_FIELDS)
    assert observation["verified_fields"] == sorted(expected_fields)
    assert (await harness.audit.status(result["request_id"]))["operation_state"] == "succeeded"


@pytest.mark.parametrize("matching", [True, False])
async def test_modify_no_is_compared_against_readback(harness, matching):
    action = "employee_update"
    request = REQUEST_MODELS[action].model_validate(
        {"employee": [{"employee_no": "E001", "modify_no": "SYNTHETIC-EDITOR"}]}
    )
    harness.approve(action, request)
    if not matching:
        harness.after_write = lambda: harness.rows["E001"].update(modify_no="DIFFERENT-EDITOR")
    result = await harness.execute(action, request)
    if matching:
        assert result["status"] == "verified"
        assert result["business_effect_verified"] is True
        assert result["data"]["verification"] == [
            {
                "employee_no": "E001",
                "verified_fields": sorted(
                    {"employee_no", "modify_no", *EMPLOYEE_UPDATE_PRESERVE_FIELDS}
                ),
                "unverified_fields": [],
            }
        ]
    else:
        assert result["status"] == "failed"
        assert result["error"] == "upstream_verification_failed"
        assert result["business_effect_verified"] is False
        assert "data" not in result
        assert (await harness.audit.status(result["request_id"]))["operation_state"] == "unknown"


async def test_one_unverified_batch_item_prevents_full_batch_effect_claim(harness):
    action = "employee_update"
    request = REQUEST_MODELS[action].model_validate(
        {
            "employee": [
                {"employee_no": "E001", "status": "N"},
                {"employee_no": "E002", "modify_date": "20260930010101"},
            ]
        }
    )
    harness.approve(action, request)
    result = await harness.execute(action, request)
    assert result["status"] == "acknowledged"
    assert result["business_effect_verified"] is False
    assert [row["unverified_fields"] for row in result["data"]["verification"]] == [
        [],
        ["modify_date"],
    ]
    assert (result["read_requests"], result["write_requests"]) == (4, 1)


async def cancel_after_ready(work, reached, backend):
    observed = []

    async def worker():
        try:
            await work()
        except anyio.get_cancelled_exc_class():
            observed.append("cancelled")
            raise

    if backend == "asyncio":
        task = asyncio.create_task(worker())
        await reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        async with anyio.create_task_group() as group:
            group.start_soon(worker)
            await reached.wait()
            group.cancel_scope.cancel()
    assert observed == ["cancelled"]


def operation_ids(harness):
    with sqlite3.connect(harness.root / "audit" / "functions.sqlite3") as connection:
        return [row[0] for row in connection.execute("SELECT request_id FROM function_operations")]


@pytest.mark.parametrize("backend", ["asyncio", "anyio"])
async def test_cancellation_before_audit_start_never_dispatches(harness, backend):
    action = "account_maintain"
    request = request_for(action)
    harness.approve(action, request)
    reached = anyio.Event()

    async def pending_authorization(*args):
        reached.set()
        await anyio.sleep_forever()

    harness.access.authorize = pending_authorization
    await cancel_after_ready(lambda: harness.execute(action, request), reached, backend)
    assert operation_ids(harness) == []
    assert harness.requests == harness.key_reads == []


@pytest.mark.parametrize("backend", ["asyncio", "anyio"])
async def test_cancellation_after_reservation_before_write_records_failed(harness, backend):
    action = "account_maintain"
    request = request_for(action)
    harness.approve(action, request)
    reached = anyio.Event()
    authorize = harness.access.authorize
    calls = 0

    async def pending_before_write(*args):
        nonlocal calls
        calls += 1
        grant = await authorize(*args)
        if calls == 5:  # Preparation guard and durable dispatch are complete; write is not sent.
            reached.set()
            await anyio.sleep_forever()
        return grant

    harness.access.authorize = pending_before_write
    await cancel_after_ready(lambda: harness.execute(action, request), reached, backend)
    identifiers = operation_ids(harness)
    assert len(identifiers) == 1
    audit = await harness.audit.status(identifiers[0])
    assert audit["operation_state"] == "failed"
    assert audit["code"] == "operation_failed"
    assert [event["event"] for event in audit["events"]] == ["started", "dispatched", "failed"]
    assert [body["service"]["name"] for body in harness.requests] == [SERVICES["employee_query"]]
    harness.access.authorize = authorize
    # Known absence of a dispatched write releases the target without rewriting history.
    result = await harness.execute(action, request)
    assert result["status"] == "acknowledged"


@pytest.mark.parametrize("backend", ["asyncio", "anyio"])
@pytest.mark.parametrize("action", ["employee_query", "account_maintain"])
async def test_cancellation_after_http_dispatch_records_terminal_and_preserves_unknown_lock(
    harness,
    backend,
    action,
):
    request = request_for(action)
    harness.approve(action, request)
    reached = anyio.Event()

    async def pending_upstream(request):
        body = json.loads(request.content)
        if body["service"]["name"] == SERVICES[action]:
            harness.requests.append(body)
            reached.set()
            await anyio.sleep_forever()
        return await harness.upstream(request)

    harness.service.client = T100InterfaceClient(
        harness.config, transport=httpx.MockTransport(pending_upstream)
    )
    await cancel_after_ready(lambda: harness.execute(action, request), reached, backend)
    identifiers = operation_ids(harness)
    assert len(identifiers) == 1
    audit = await harness.audit.status(identifiers[0])
    expected = "failed" if action == "employee_query" else "unknown"
    assert audit["operation_state"] == expected
    assert audit["code"] == "operation_failed"
    assert [event["event"] for event in audit["events"]] == ["started", "dispatched", expected]
    assert sum(body["service"]["name"] == SERVICES[action] for body in harness.requests) == 1
    if action == "account_maintain":
        second = await harness.execute(action, request)
        assert second["error"] == "reset_pending_review"
        assert (second["read_requests"], second["write_requests"]) == (0, 0)


async def test_cancellation_with_terminal_audit_failure_keeps_pending_lock(harness, monkeypatch):
    action = "account_maintain"
    request = request_for(action)
    harness.approve(action, request)
    reached = anyio.Event()

    async def pending_upstream(request):
        body = json.loads(request.content)
        if body["service"]["name"] == SERVICES[action]:
            harness.requests.append(body)
            reached.set()
            await anyio.sleep_forever()
        return await harness.upstream(request)

    async def failed_terminal(*args, **kwargs):
        raise OSError("synthetic-audit-failure")

    harness.service.client = T100InterfaceClient(
        harness.config, transport=httpx.MockTransport(pending_upstream)
    )
    monkeypatch.setattr(harness.audit, "finish", failed_terminal)
    await cancel_after_ready(lambda: harness.execute(action, request), reached, "anyio")
    (identifier,) = operation_ids(harness)
    assert (await harness.audit.status(identifier))["operation_state"] == "dispatched"
    second = await harness.execute(action, request)
    assert second["error"] == "reset_pending_review"
