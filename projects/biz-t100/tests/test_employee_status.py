"""员工资料状态更新的合成契约：前置查询、单次最小写入与严格回读。"""

import asyncio
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import httpx2
import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from pydantic import SecretStr, ValidationError

import biz_t100.employee_status as module
import biz_t100.server as identity_module
from biz_t100.client import T100Config
from biz_t100.employee_status import (
    EmployeeStatusPlan,
    EmployeeStatusReceipt,
    T100EmployeeStatusClient,
    action_profile,
    create_employee_status_app,
)
from biz_t100.models import BusinessError, PasswordResetGrant
from biz_t100.reset_client import ResetReceipt
from biz_t100.server import IdentityProfile

SECRET = "synthetic-status-key"
ISSUER = "https://status-issuer.example.invalid"
AUDIENCE = "synthetic-status-service"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    async def blocked(*args, **kwargs):
        pytest.fail("Employee status tests must not open real network connections")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)


@pytest.fixture
def config():
    return T100Config(
        url="https://t100-status.example.invalid/api",
        enterprise_id="SYNTHETIC-ENT",
        host_product="SYNTHETIC",
        host_ip="127.0.0.1",
        host_account="synthetic-admin",
        service_ip="127.0.0.1",
        service_id="synthetic-service",
    )


@pytest.fixture
def grant():
    return PasswordResetGrant(
        employee_id="E001",
        account_id="LOGIN-A",
        binding_version="synthetic-v1",
        credential=SecretStr(SECRET),
    )


def plan(action="disable_account", **changes):
    return EmployeeStatusPlan(
        **{
            "action": action,
            "employee_id": "E001",
            "account_id": "LOGIN-A",
            "account_status": "Y" if action == "enable_account" else "N",
            **changes,
        }
    )


def query(status="Y", employee="E001"):
    return {
        "payload": {
            "std_data": {
                "execution": {"code": "0"},
                "parameter": {
                    "employee": [
                        {
                            "employee_no": employee,
                            "status": status,
                            "full_name": "Synthetic Display",
                            "department_no": "DEMO-DEPT",
                            "email": "synthetic@example.invalid",
                            "account_no": "",
                        }
                    ]
                },
            }
        }
    }


def updated(employee="E001", **changes):
    return {
        "payload": {
            "std_data": {
                "execution": {"code": "0"},
                "parameter": {
                    "employee": [
                        {
                            "source_no": employee,
                            "erp_no": employee,
                            "execute_description": "",
                            **changes,
                        }
                    ]
                },
            }
        }
    }


class Upstream:
    def __init__(self, selected):
        self.selected = selected
        self.requests = []
        self.results = [query(), updated(), query(selected.account_status)]
        self.callback = None

    async def __call__(self, request):
        self.requests.append(json.loads(request.content))
        index = len(self.requests) - 1
        if self.callback is not None:
            await self.callback(index)
        response = self.results[index]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    def writer(self, config):
        return T100EmployeeStatusClient(config, self.selected, transport=httpx.MockTransport(self))

    @property
    def writes(self):
        return [r for r in self.requests if r["service"]["name"] == "employee.data.update"]


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "reset_password"},
        {"action": "enable_account", "account_status": "N"},
        {"action": "disable_account", "account_status": "Y"},
        {"account_status": "n"},
        {"account_status": True},
        {"employee_id": ""},
        {"account_id": " "},
        {"employee_id": "E 001"},
        {"full_name": "Synthetic Display"},
        {"detail": []},
        {"key": SECRET},
    ],
)
def test_plan_rejects_ambiguous_or_additional_input(changes):
    with pytest.raises(ValidationError):
        plan(**changes)


@pytest.mark.parametrize("action", ["enable_account", "disable_account"])
def test_plan_has_no_implicit_status(action):
    with pytest.raises(ValidationError):
        EmployeeStatusPlan(action=action, employee_id="E001", account_id="LOGIN-A")


@pytest.mark.parametrize("action", ["enable_account", "disable_account"])
async def test_exact_minimal_update_and_precise_readback(config, grant, action):
    selected = plan(action)
    upstream = Upstream(selected)
    writer = upstream.writer(config)
    result = await writer.reset(grant)
    assert type(result) is EmployeeStatusReceipt
    assert result.model_dump() == {
        "acknowledged": True,
        "employee_id": "E001",
        "account_id": "LOGIN-A",
        "observed_status": selected.account_status,
        "effect_verified": True,
    }
    assert [r["service"]["name"] for r in upstream.requests] == [
        "employee.list.data.query.get",
        "employee.data.update",
        "employee.list.data.query.get",
    ]
    assert len(upstream.writes) == 1
    assert upstream.writes[0]["payload"]["std_data"]["parameter"] == {
        "is_digiwin_product": "N",
        "employee": [{"employee_no": "E001", "status": selected.account_status}],
    }
    for request in upstream.requests:
        assert request["datakey"] == {"EntId": "SYNTHETIC-ENT"}
        assert request["key"] == SECRET
    serialized = json.dumps(upstream.writes)
    for excluded in ("LOGIN-A", "full_name", "email", "Synthetic Display", "user.account.create"):
        assert excluded not in serialized
    assert writer.write_attempted is True
    assert writer.read_attempts == 2
    assert writer.observed_status == selected.account_status
    assert writer.effect_verified is True
    assert SECRET not in result.model_dump_json()


@pytest.mark.parametrize("field,value", [("employee_id", "E002"), ("account_id", "LOGIN-B")])
async def test_grant_must_match_both_targets_before_any_read(config, grant, field, value):
    upstream = Upstream(plan())
    writer = upstream.writer(config)
    with pytest.raises(BusinessError, match="^authorization_changed$"):
        await writer.reset(grant.model_copy(update={field: value}))
    assert upstream.requests == []
    assert writer.write_attempted is False and writer.read_attempts == 0


@pytest.mark.parametrize("field,value", [("employee_id", ""), ("binding_version", "")])
async def test_bypassed_grant_is_revalidated(config, grant, field, value):
    upstream = Upstream(plan())
    with pytest.raises(BusinessError, match="^upstream_contract_error$"):
        await upstream.writer(config).reset(grant.model_copy(update={field: value}))
    assert upstream.requests == []


async def test_empty_credential_is_rejected_without_http(config, grant):
    upstream = Upstream(plan())
    with pytest.raises(BusinessError, match="^upstream_unauthenticated$"):
        await upstream.writer(config).reset(grant.model_copy(update={"credential": SecretStr(" ")}))
    assert upstream.requests == []


def test_copied_config_and_plan_cannot_bypass_validation(config):
    with pytest.raises(ValidationError):
        T100EmployeeStatusClient(config, plan().model_copy(update={"account_status": "Y"}))
    with pytest.raises(ValidationError):
        T100EmployeeStatusClient(config.model_copy(update={"url": "http://unsafe.invalid"}), plan())


@pytest.mark.parametrize("stage", [0, 2])
@pytest.mark.parametrize("failure", ["empty", "multiple", "wrong-employee"])
async def test_unique_employee_required_before_and_after_update(config, grant, stage, failure):
    upstream = Upstream(plan())
    rows = upstream.results[stage]["payload"]["std_data"]["parameter"]["employee"]
    if failure == "empty":
        rows.clear()
    elif failure == "multiple":
        rows.append(copy.deepcopy(rows[0]))
    else:
        rows[0]["employee_no"] = "E002"
    writer = upstream.writer(config)
    with pytest.raises(BusinessError, match="^upstream_contract_error$"):
        await writer.reset(grant)
    assert len(upstream.requests) == stage + 1
    assert len(upstream.writes) == (0 if stage == 0 else 1)
    assert writer.write_attempted is (stage == 2)
    assert writer.effect_verified is False


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("nonzero", "upstream_rejected"),
        ("boolean-code", "upstream_contract_error"),
        ("float-code", "upstream_contract_error"),
        ("empty-body", "upstream_contract_error"),
        ("ack-only", "upstream_contract_error"),
        ("zero-rows", "upstream_contract_error"),
        ("multiple-rows", "upstream_contract_error"),
        ("wrong-source", "upstream_contract_error"),
        ("wrong-erp", "upstream_contract_error"),
        ("empty-erp", "upstream_contract_error"),
        ("numeric-source", "upstream_contract_error"),
        ("missing-description", "upstream_contract_error"),
        ("null-description", "upstream_contract_error"),
        ("row-error", "upstream_contract_error"),
    ],
)
async def test_global_success_never_bypasses_per_row_validation(config, grant, kind, expected):
    upstream = Upstream(plan())
    body = updated()
    std = body["payload"]["std_data"]
    rows = std["parameter"]["employee"]
    row = rows[0]
    if kind in {"nonzero", "boolean-code", "float-code"}:
        std["execution"]["code"] = {"nonzero": "failure", "boolean-code": False, "float-code": 0.0}[
            kind
        ]
    elif kind == "empty-body":
        body = httpx.Response(200, content=b"")
    elif kind == "ack-only":
        del std["parameter"]
    elif kind == "zero-rows":
        rows.clear()
    elif kind == "multiple-rows":
        rows.append(copy.deepcopy(row))
    elif kind == "wrong-source":
        row["source_no"] = "E002"
    elif kind == "wrong-erp":
        row["erp_no"] = "E002"
    elif kind == "empty-erp":
        row["erp_no"] = ""
    elif kind == "numeric-source":
        row["source_no"] = 1
    elif kind == "missing-description":
        del row["execute_description"]
    elif kind == "null-description":
        row["execute_description"] = None
    elif kind == "row-error":
        row["execute_description"] = SECRET
    upstream.results[1] = body
    writer = upstream.writer(config)
    with pytest.raises(BusinessError, match=f"^{expected}$") as caught:
        await writer.reset(grant)
    assert SECRET not in str(caught.value)
    assert len(upstream.requests) == 2 and len(upstream.writes) == 1
    assert writer.read_attempts == 1 and writer.effect_verified is False


@pytest.mark.parametrize("action", ["enable_account", "disable_account"])
@pytest.mark.parametrize("observed", [None, "", "unknown", "opposite"])
async def test_readback_must_match_requested_status(config, grant, action, observed):
    selected = plan(action)
    upstream = Upstream(selected)
    if observed == "opposite":
        observed = "N" if selected.account_status == "Y" else "Y"
    upstream.results[2] = query(observed)
    writer = upstream.writer(config)
    with pytest.raises(BusinessError, match="^upstream_contract_error$"):
        await writer.reset(grant)
    assert len(upstream.writes) == 1 and writer.read_attempts == 2
    assert writer.effect_verified is False
    assert writer.observed_status == (observed if observed in {"Y", "N"} else None)


@pytest.mark.parametrize("stage", [0, 1, 2])
@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "upstream_unauthenticated"),
        (403, "business_forbidden"),
        (500, "upstream_unavailable"),
        (307, "upstream_rejected"),
    ],
)
async def test_http_failures_stop_without_retry(config, grant, stage, status, expected):
    upstream = Upstream(plan())
    upstream.results[stage] = httpx.Response(status, headers={"Location": "https://other.invalid"})
    writer = upstream.writer(config)
    with pytest.raises(BusinessError, match=f"^{expected}$"):
        await writer.reset(grant)
    assert len(upstream.requests) == stage + 1
    assert len(upstream.writes) == (0 if stage == 0 else 1)
    assert writer.effect_verified is False


@pytest.mark.parametrize("stage", [0, 1, 2])
async def test_timeouts_never_retry_write(config, grant, stage):
    upstream = Upstream(plan())
    upstream.results[stage] = httpx.ReadTimeout(SECRET)
    writer = upstream.writer(config)
    with pytest.raises(BusinessError, match="^upstream_timeout$"):
        await writer.reset(grant)
    assert len(upstream.requests) == stage + 1
    assert len(upstream.writes) <= 1 and writer.effect_verified is False


async def test_one_budget_covers_all_three_requests(config, grant, monkeypatch):
    upstream = Upstream(plan())

    async def delay(index):
        await asyncio.sleep(0.025)

    upstream.callback = delay
    monkeypatch.setattr(module, "REQUEST_BUDGET_SECONDS", 0.065)
    writer = upstream.writer(config)
    with pytest.raises(BusinessError, match="^upstream_timeout$"):
        await writer.reset(grant)
    assert len(upstream.writes) == 1 and writer.effect_verified is False


async def test_explicit_before_write_guard_runs_after_query_and_prevents_update(config, grant):
    upstream = Upstream(plan())
    writer = upstream.writer(config)

    async def deny(current):
        assert current == grant
        assert len(upstream.requests) == 1
        raise BusinessError("permission_denied")

    with pytest.raises(BusinessError, match="^permission_denied$"):
        await writer.reset(grant, before_write=deny)
    assert writer.read_attempts == 1 and writer.write_attempted is False
    assert upstream.writes == []


class Access:
    def __init__(self, grant):
        self.grant = grant
        self.enabled = True
        self.calls = 0

    async def authorize(self, caller):
        self.calls += 1
        if not self.enabled or caller.subject != "user-a":
            raise BusinessError("permission_denied")
        return self.grant


class Store:
    def __init__(self):
        self.state = None
        self.events = []
        self.fail_success = False

    async def reserve(self, **values):
        if self.state is not None:
            raise BusinessError("reset_already_recorded")
        self.events.append(("reserve", values))
        self.state = "started"

    async def mark_dispatched(self, **values):
        self.events.append(("dispatched", values))
        self.state = "dispatched"

    async def finish(self, **values):
        if self.fail_success and values["outcome"] == "succeeded":
            raise BusinessError("journal_unavailable")
        self.events.append(("finish", values))
        self.state = values["outcome"]


async def native_call(
    config, grant, upstream, *, access=None, store=None, scope=None, arguments=None, writer=None
):
    selected = upstream.selected
    tool, expected_scope = action_profile(selected.action)
    key = RSAKeyPair.generate()
    access = access or Access(grant)
    store = store or Store()
    writer = writer or upstream.writer(config)
    app = create_employee_status_app(
        auth=JWTVerifier(
            public_key=key.public_key,
            issuer=ISSUER,
            audience=AUDIENCE,
            required_scopes=[expected_scope],
        ),
        identity=IdentityProfile(
            issuer=ISSUER, audience=AUDIENCE, allowed_clients=frozenset({"status-client"})
        ),
        access=access,
        writer=writer,
        store=store,
        action=selected.action,
    )
    token = key.create_token(
        subject="user-a",
        issuer=ISSUER,
        audience=AUDIENCE,
        scopes=[scope or expected_scope],
        additional_claims={"azp": "status-client", "employee_no": "E002", "account_id": "OTHER"},
    )
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
        ) as client:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": tool,
                        "arguments": arguments or {"confirm": True, "operation_id": str(uuid4())},
                    },
                },
            )
    return response, writer, access, store


@pytest.mark.parametrize("action", ["enable_account", "disable_account"])
async def test_factory_reports_business_verified_only_after_readback_and_audit(
    config, grant, action
):
    upstream = Upstream(plan(action))
    response, writer, _, store = await native_call(config, grant, upstream)
    assert response.status_code == 200
    result = response.json()["result"]
    assert not result.get("isError"), response.text
    assert result["structuredContent"]["status"] == "business_verified"
    assert store.state == "succeeded" and writer.effect_verified is True
    assert SECRET not in response.text and "Synthetic Display" not in response.text


@pytest.mark.parametrize("change", ["disable", "employee", "account", "binding"])
async def test_factory_revocation_during_prequery_cannot_write(config, grant, change):
    upstream = Upstream(plan())
    access = Access(grant)

    async def revoke(index):
        if index != 0:
            return
        if change == "disable":
            access.enabled = False
        else:
            field, value = {
                "employee": ("employee_id", "E002"),
                "account": ("account_id", "LOGIN-B"),
                "binding": ("binding_version", "synthetic-v2"),
            }[change]
            access.grant = grant.model_copy(update={field: value})

    upstream.callback = revoke
    response, writer, _, store = await native_call(config, grant, upstream, access=access)
    assert response.json()["result"]["isError"]
    assert len(upstream.requests) == 1 and upstream.writes == []
    assert writer.write_attempted is False and writer.effect_verified is False
    assert store.state != "succeeded"


@pytest.mark.parametrize("failure", ["revoked-after-write", "terminal-audit"])
async def test_factory_cannot_deliver_success_after_revocation_or_audit_failure(
    config, grant, failure
):
    upstream = Upstream(plan())
    access, store = Access(grant), Store()
    if failure == "terminal-audit":
        store.fail_success = True
    else:

        async def revoke(index):
            if index == 2:
                access.enabled = False

        upstream.callback = revoke
    response, _, _, store = await native_call(config, grant, upstream, access=access, store=store)
    assert response.json()["result"]["isError"]
    assert len(upstream.writes) == 1 and store.state != "succeeded"


@pytest.mark.parametrize("scope", ["t100:account:enable:self", "t100:password:reset:self"])
async def test_factory_wrong_scope_rejected_before_key_or_http(config, grant, scope):
    upstream = Upstream(plan())
    response, _, access, _ = await native_call(config, grant, upstream, scope=scope)
    assert response.status_code in {401, 403}
    assert access.calls == 0 and upstream.requests == []


@pytest.mark.parametrize("field", ["employee_no", "account_id", "status", "key"])
async def test_factory_never_accepts_remote_target_or_credential_override(config, grant, field):
    upstream = Upstream(plan())
    response, _, access, _ = await native_call(
        config,
        grant,
        upstream,
        arguments={"confirm": True, "operation_id": str(uuid4()), field: "synthetic-other"},
    )
    assert response.json()["result"]["isError"]
    assert "invalid_arguments" in response.text
    assert access.calls == 0 and upstream.requests == []


def test_factory_rejects_native_writer_for_other_action(config, grant):
    writer = T100EmployeeStatusClient(config, plan("disable_account"))
    with pytest.raises(ValueError, match="Writer action"):
        create_employee_status_app(
            auth=None,
            identity=IdentityProfile(
                issuer=ISSUER, audience=AUDIENCE, allowed_clients=frozenset({"status-client"})
            ),
            access=Access(grant),
            writer=writer,
            store=Store(),
            action="enable_account",
        )


async def test_mutated_native_plan_is_revalidated_before_any_request(config, grant):
    upstream = Upstream(plan())
    writer = upstream.writer(config)
    writer.plan = writer.plan.model_copy(update={"account_status": "Y"})
    with pytest.raises(BusinessError, match="^upstream_contract_error$"):
        await writer.reset(grant)
    assert upstream.requests == []


async def test_concurrent_enterprises_never_mix_employee_or_admin_credential(config, grant):
    first = Upstream(plan())
    second = Upstream(plan("enable_account", employee_id="E002", account_id="LOGIN-B"))
    second.results = [query("N", "E002"), updated("E002"), query("Y", "E002")]
    arrivals = []
    ready = asyncio.Event()

    async def synchronize(index):
        if index == 0:
            arrivals.append(index)
            if len(arrivals) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=1)

    first.callback = second.callback = synchronize
    other_grant = grant.model_copy(
        update={
            "employee_id": "E002",
            "account_id": "LOGIN-B",
            "credential": SecretStr("other-synthetic-key"),
        }
    )
    first_writer = first.writer(config)
    second_writer = second.writer(config.model_copy(update={"enterprise_id": "OTHER-ENT"}))
    await asyncio.gather(first_writer.reset(grant), second_writer.reset(other_grant))
    for upstream, enterprise, employee, credential in (
        (first, "SYNTHETIC-ENT", "E001", SECRET),
        (second, "OTHER-ENT", "E002", "other-synthetic-key"),
    ):
        assert len(upstream.requests) == 3 and len(upstream.writes) == 1
        for request in upstream.requests:
            assert request["datakey"] == {"EntId": enterprise}
            assert request["key"] == credential
            parameter = request["payload"]["std_data"]["parameter"]
            if request["service"]["name"] == "employee.data.update":
                assert parameter["employee"][0]["employee_no"] == employee
            else:
                assert parameter["employee_no"] == employee
    assert first_writer.observed_status == "N" and second_writer.observed_status == "Y"


@pytest.mark.parametrize(
    "kind",
    [
        "ack-only",
        "wrong-employee",
        "wrong-account",
        "wrong-status",
        "unverified",
        "invalid-status",
        "invalid-employee",
        "numeric-proof",
        "numeric-ack",
    ],
)
async def test_factory_rejects_ack_only_or_unbound_status_receipt(config, grant, kind):
    receipt = EmployeeStatusReceipt(
        employee_id="E001", account_id="LOGIN-A", observed_status="N", effect_verified=True
    )
    if kind == "ack-only":
        receipt = ResetReceipt()
    else:
        field, value = {
            "wrong-employee": ("employee_id", "E002"),
            "wrong-account": ("account_id", "LOGIN-B"),
            "wrong-status": ("observed_status", "Y"),
            "unverified": ("effect_verified", False),
            "invalid-status": ("observed_status", "n"),
            "invalid-employee": ("employee_id", ""),
            "numeric-proof": ("effect_verified", 1),
            "numeric-ack": ("acknowledged", 1),
        }[kind]
        receipt = receipt.model_copy(update={field: value})

    class InvalidWriter:
        async def reset(self, current, *, before_write=None):
            await before_write(current)
            return receipt

    upstream = Upstream(plan())
    response, _, _, store = await native_call(config, grant, upstream, writer=InvalidWriter())
    assert response.status_code == 200
    assert response.json()["result"]["isError"]
    assert store.state == "unknown"
    assert store.events[-1][1]["code"] == "upstream_contract_error"
    assert "business_verified" not in response.text


async def test_factory_token_expiring_during_before_write_authorization_prevents_write(
    config, grant, monkeypatch
):
    upstream = Upstream(plan())
    clock = identity_module.time
    delayed_authorizations = []

    class ExpiringAccess(Access):
        async def authorize(self, caller):
            current = await super().authorize(caller)
            if len(upstream.requests) == 1 and not delayed_authorizations:
                delayed_authorizations.append(caller)
                await asyncio.sleep(0)
                # 原生 JWT 已验签；模拟等待授权期间到期，只推进身份时钟。
                monkeypatch.setattr(
                    identity_module, "time", SimpleNamespace(time=lambda: clock.time() + 10_000_000)
                )
            return current

    response, writer, _, store = await native_call(
        config, grant, upstream, access=ExpiringAccess(grant)
    )
    assert delayed_authorizations
    assert response.json()["result"]["isError"]
    assert len(upstream.requests) == 1 and upstream.writes == []
    assert writer.write_attempted is False
    assert store.state == "unknown"
    assert store.events[-1][1]["code"] == "unauthenticated"


async def test_factory_caller_cannot_change_while_waiting_for_before_write_authorization(
    config, grant, monkeypatch
):
    original = module.current_caller
    checks = []

    def current(profile, *, required_scope):
        caller = original(profile, required_scope=required_scope)
        checks.append(caller)
        return caller if len(checks) == 1 else caller.model_copy(update={"subject": "other-user"})

    monkeypatch.setattr(module, "current_caller", current)
    upstream = Upstream(plan())
    response, writer, _, store = await native_call(config, grant, upstream)
    assert response.json()["result"]["isError"]
    assert len(checks) == 2
    assert len(upstream.requests) == 1 and upstream.writes == []
    assert writer.write_attempted is False
    assert store.events[-1][1]["code"] == "authorization_changed"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
async def test_write_diagnostics_record_http_rejection_without_response_text(config, grant, status):
    upstream = Upstream(plan())
    upstream.results[1] = httpx.Response(status, text=SECRET)
    writer = upstream.writer(config)
    assert writer.write_diagnostics == {}
    with pytest.raises(BusinessError):
        await writer.reset(grant)
    assert writer.write_diagnostics == {"http_status": status}
    assert len(upstream.writes) == 1 and writer.read_attempts == 1
    assert SECRET not in json.dumps(writer.write_diagnostics)


@pytest.mark.parametrize("code,sql", [("17", "-100"), (17, -100)])
async def test_write_diagnostics_capture_numeric_business_codes_only(config, grant, code, sql):
    upstream = Upstream(plan())
    execution = upstream.results[1]["payload"]["std_data"]["execution"]
    execution.update(code=code, sql_code=sql, description=SECRET, key=SECRET)
    writer = upstream.writer(config)
    with pytest.raises(BusinessError, match="^upstream_rejected$"):
        await writer.reset(grant)
    assert writer.write_diagnostics == {"http_status": 200, "execution_code": code, "sql_code": sql}
    assert SECRET not in json.dumps(writer.write_diagnostics)
    assert len(upstream.writes) == 1 and writer.read_attempts == 1


@pytest.mark.parametrize(
    "value,expected_type",
    [
        (SECRET, "string"),
        ({"key": SECRET}, "object"),
        ([SECRET], "array"),
        (True, "boolean"),
        (False, "boolean"),
        (None, "null"),
        (1.5, "number"),
        ("1" * 33, "string"),
        (10**32, "integer"),
    ],
)
async def test_write_diagnostics_never_echo_invalid_code_values(
    config, grant, value, expected_type
):
    upstream = Upstream(plan())
    upstream.results[1]["payload"]["std_data"]["execution"] = {
        "code": value,
        "sql_code": value,
        "description": SECRET,
        "key": SECRET,
    }
    writer = upstream.writer(config)
    with pytest.raises(BusinessError) as caught:
        await writer.reset(grant)
    assert writer.write_diagnostics == {
        "http_status": 200,
        "execution_code_type": expected_type,
        "sql_code_type": expected_type,
    }
    assert SECRET not in json.dumps(writer.write_diagnostics)
    assert SECRET not in str(caught.value)
    assert len(upstream.writes) == 1 and writer.read_attempts == 1


async def test_write_diagnostics_malformed_json_retains_only_http_status(config, grant):
    upstream = Upstream(plan())
    upstream.results[1] = httpx.Response(200, content=("not-json-" + SECRET).encode())
    writer = upstream.writer(config)
    with pytest.raises(BusinessError, match="^upstream_contract_error$"):
        await writer.reset(grant)
    assert writer.write_diagnostics == {"http_status": 200}
    assert len(upstream.writes) == 1


async def test_write_diagnostics_are_cleared_before_a_later_rejected_call(config, grant):
    upstream = Upstream(plan())
    writer = upstream.writer(config)
    await writer.reset(grant)
    assert writer.write_diagnostics == {"http_status": 200, "execution_code": "0"}
    with pytest.raises(BusinessError, match="^authorization_changed$"):
        await writer.reset(grant.model_copy(update={"employee_id": "E002"}))
    assert writer.write_diagnostics == {} and writer.write_attempted is False
    assert len(upstream.writes) == 1


async def test_numeric_service_key_is_not_exposed_if_upstream_echoes_it_as_code(config, grant):
    upstream = Upstream(plan())
    numeric_secret = "123456789"
    upstream.results[1]["payload"]["std_data"]["execution"].update(
        code=numeric_secret, sql_code=int(numeric_secret)
    )
    writer = upstream.writer(config)
    with pytest.raises(BusinessError, match="^upstream_rejected$"):
        await writer.reset(grant.model_copy(update={"credential": SecretStr(numeric_secret)}))
    assert writer.write_diagnostics == {
        "http_status": 200,
        "execution_code_type": "string",
        "sql_code_type": "integer",
    }
    assert numeric_secret not in json.dumps(writer.write_diagnostics)
