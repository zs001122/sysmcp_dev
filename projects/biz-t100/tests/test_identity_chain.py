"""离线身份到业务的矩阵：真实组件，仅使用合成密钥与 HTTP。"""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from uuid import UUID

import anyio
import httpx
import httpx2
import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

import biz_t100.access as access_module
from biz_t100 import IdentityProfile, create_app
from biz_t100.access import FileServiceAccountAccess
from biz_t100.client import T100Config, T100EmployeeClient
from biz_t100.errors import error_message
from biz_t100.journal import SQLiteQueryJournal
from biz_t100.server import SCOPE, TOOL_NAME

ISSUER = "https://identity.example.invalid"
SECRET = "synthetic-admin-key-never-disclose"
PRIVATE = "synthetic-upstream-private-payload"
EMPLOYEES = {"employee-a": "E001", "employee-b": "E002"}
CLIENTS = {"employee-a": "agent-a", "employee-b": "agent-b"}
AUTO_TOKEN = object()


def private_file(path, text):
    pending = path.with_name(path.name + ".new")
    pending.write_text(text)
    pending.chmod(0o600)
    pending.replace(path)


class RequestBarrier:
    """要求两个 HTTP 请求都在途之后，任一请求才能返回。"""

    def __init__(self):
        self.arrivals = 0
        self.ready = anyio.Event()

    async def wait(self):
        self.arrivals += 1
        if self.arrivals == 2:
            self.ready.set()
        await self.ready.wait()


class JournalWithFaults(SQLiteQueryJournal):
    """只注入选定的存储边界；成功写入仍是真实的 SQLite。"""

    def __init__(self, path, enterprise):
        super().__init__(path, enterprise_id=enterprise)
        self.fail_events = set()
        self.after_start = None

    async def record(self, **event):
        if event["event"] in self.fail_events:
            raise OSError(PRIVATE + SECRET)
        await super().record(**event)
        if event["event"] == "query_started" and self.after_start:
            self.after_start()


class IdentityChain:
    def __init__(self, folder, key_reads, *, enterprise="ENT-A"):
        self.folder = folder
        self.enterprise = enterprise
        self.secret = f"{SECRET}-{enterprise}"
        self.audience = f"synthetic-t100-{enterprise}"
        self.key = RSAKeyPair.generate()  # 仅存在于内存；不使用固定或持久化的私钥。
        self.policy_path = folder / "policy.json"
        self.credential_path = folder / "service.key"
        self.journal_path = folder / "journal" / "queries.sqlite3"
        self.policy = {
            "auth_mode": "service-account",
            "revision": "synthetic-v1",
            "principals": [
                {
                    "issuer": ISSUER,
                    "subject": subject,
                    "employee_id": employee,
                    "allowed_clients": [CLIENTS[subject]],
                    "enabled": True,
                    "allow_employee_read": True,
                }
                for subject, employee in EMPLOYEES.items()
            ],
        }
        self.save_policy()
        private_file(self.credential_path, self.secret)
        self.key_reads = key_reads
        self.requests = []
        self.response_case = "valid"
        self.after_upstream = None
        self.barrier = None
        self.active_upstream = 0
        self.peak_upstream = 0
        self.journal = JournalWithFaults(self.journal_path, enterprise)
        self.t100 = T100Config(
            url="https://t100.example.invalid/employee-api",
            enterprise_id=enterprise,
            host_product="TEST",
            host_ip="127.0.0.1",
            host_account="synthetic-service",
            service_ip="127.0.0.1",
            service_id="test",
        )
        self.dependencies = {
            "auth": JWTVerifier(
                public_key=self.key.public_key,
                issuer=ISSUER,
                audience=self.audience,
                required_scopes=[SCOPE],
            ),
            "identity": IdentityProfile(
                issuer=ISSUER,
                audience=self.audience,
                allowed_clients=frozenset(CLIENTS.values()),
            ),
            "access": FileServiceAccountAccess(self.policy_path, self.credential_path),
            "reader": T100EmployeeClient(self.t100, transport=httpx.MockTransport(self.upstream)),
            "journal": self.journal,
        }

    def save_policy(self):
        private_file(self.policy_path, json.dumps(self.policy))

    def revoke(self):
        self.policy["principals"][0]["enabled"] = False
        self.save_policy()

    def token(self, subject="employee-a", *, client=None, claims=None, signer=None, **overrides):
        options = {
            "subject": subject,
            "issuer": ISSUER,
            "audience": self.audience,
            "scopes": [SCOPE],
            "additional_claims": {
                "azp": client or CLIENTS.get(subject, "agent-a"),
                **(claims or {}),
            },
            **overrides,
        }
        return (signer or self.key).create_token(**options)

    @asynccontextmanager
    async def connection(self):
        app = create_app(**self.dependencies)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                yield client

    async def call(
        self,
        client,
        *,
        subject="employee-a",
        bearer=AUTO_TOKEN,
        arguments=None,
        headers=None,
        meta=None,
    ):
        if bearer is AUTO_TOKEN:
            bearer = self.token(subject)
        request_headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
            **(headers or {}),
        }
        if bearer is not None:
            request_headers["Authorization"] = f"Bearer {bearer}"
        params = {"name": TOOL_NAME, "arguments": arguments or {}}
        if meta is not None:
            params["_meta"] = meta
        return await client.post(
            "/mcp",
            headers=request_headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
        )

    async def upstream(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        assert request.method == "POST"
        assert str(request.url) == self.t100.url
        assert body["key"] == self.secret
        assert body["datakey"] == {"EntId": self.enterprise}
        assert body["service"]["name"] == "employee.list.data.query.get"
        employee = body["payload"]["std_data"]["parameter"]["employee_no"]
        self.active_upstream += 1
        self.peak_upstream = max(self.peak_upstream, self.active_upstream)
        try:
            if self.barrier is not None:
                await self.barrier.wait()
            if self.response_case == "http-timeout":
                raise httpx.ReadTimeout(PRIVATE + SECRET, request=request)
            if self.response_case == "tool-timeout":
                await anyio.sleep_forever()
            if self.after_upstream:
                self.after_upstream()
            if self.response_case == "unauthenticated":
                return httpx.Response(401, text=PRIVATE + SECRET)
            rows = [
                {
                    "employee_no": employee,
                    "full_name": f"Employee {self.enterprise} {employee}",
                    "salary": PRIVATE,
                    "password": SECRET,
                }
            ]
            if self.response_case == "wrong":
                rows[0]["employee_no"] = "OTHER"
            elif self.response_case == "zero":
                rows = []
            elif self.response_case == "multi":
                rows *= 2
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "std_data": {
                            "execution": {"code": "0"},
                            "parameter": {"employee": rows},
                        }
                    },
                },
            )
        finally:
            self.active_upstream -= 1

    def targets(self):
        return [
            (body["datakey"]["EntId"], body["payload"]["std_data"]["parameter"]["employee_no"])
            for body in self.requests
        ]

    def credential_reads(self):
        return self.key_reads.count(self.credential_path)

    def audit(self):
        if not self.journal_path.exists():
            return []
        with sqlite3.connect(self.journal_path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row) for row in db.execute("SELECT * FROM query_events ORDER BY occurred_at")
            ]

    def assert_audit(
        self,
        request_id,
        events,
        *,
        subject="employee-a",
        employee="E001",
        code=None,
        attempted=False,
    ):
        UUID(request_id)
        rows = [row for row in self.audit() if row["request_id"] == request_id]
        assert [row["event"] for row in rows] == events
        for row in rows:
            assert (row["issuer"], row["subject"], row["client_id"], row["employee_id"]) == (
                ISSUER,
                subject,
                CLIENTS.get(subject, "agent-a"),
                employee,
            )
            assert row["code"] == (code if row["event"] == "query_failed" else None)
            assert row["enterprise_id"] == self.enterprise
            assert row["tool_name"] == TOOL_NAME
            expected_attempt = row["event"] == "query_succeeded" or (
                row["event"] == "query_failed" and attempted
            )
            assert row["upstream_attempted"] == int(expected_attempt)
        encoded = json.dumps(self.audit())
        assert SECRET not in encoded and PRIVATE not in encoded
        return rows


@pytest.fixture
def chain_factory(tmp_path, monkeypatch):
    reads = []
    original = access_module._private_bytes

    def traced_read(path, maximum):
        if path.name == "service.key":
            reads.append(path)
        return original(path, maximum)

    monkeypatch.setattr(access_module, "_private_bytes", traced_read)
    created = []

    def make(**kwargs):
        folder = tmp_path / f"chain-{len(created)}"
        folder.mkdir(mode=0o700)
        chain = IdentityChain(folder, reads, **kwargs)
        created.append(chain)
        return chain

    return make


@pytest.fixture
def chain(chain_factory):
    return chain_factory()


def success(response, *, employee, enterprise="ENT-A"):
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    output = result["structuredContent"]
    assert output["data"]["employee_id"] == employee
    assert output["data"]["display_name"] == f"Employee {enterprise} {employee}"
    assert SECRET not in response.text and PRIVATE not in response.text
    return output["request_id"]


def failure(response, code):
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert not result.get("structuredContent")
    assert "Employee ENT-" not in response.text
    assert SECRET not in response.text and PRIVATE not in response.text
    text = result["content"][0]["text"]
    if text == code:
        return None
    error = json.loads(text)
    assert error["error"] == code
    assert set(error) == {"error", "message", "request_id"}
    assert error["message"] == error_message(code)
    UUID(error["request_id"])
    return error["request_id"]


@pytest.mark.parametrize("subject", list(EMPLOYEES))
async def test_each_trusted_caller_queries_only_mapped_employee(chain, subject):
    async with chain.connection() as client:
        response = await chain.call(client, subject=subject)
    employee = EMPLOYEES[subject]
    request_id = success(response, employee=employee)
    assert chain.targets() == [(chain.enterprise, employee)]
    assert chain.credential_reads() == 3
    chain.assert_audit(
        request_id, ["query_started", "query_succeeded"], subject=subject, employee=employee
    )
    assert await chain.journal.pending_request_ids() == []


async def test_true_concurrency_keeps_identity_enterprise_target_and_trace_separate(chain):
    chain.barrier = RequestBarrier()
    async with chain.connection() as client:
        with anyio.fail_after(3):
            results = await asyncio.gather(
                chain.call(client, subject="employee-a"),
                chain.call(client, subject="employee-b"),
            )
    assert chain.peak_upstream == 2
    assert sorted(chain.targets()) == [(chain.enterprise, "E001"), (chain.enterprise, "E002")]
    request_ids = []
    for subject, response in zip(EMPLOYEES, results, strict=True):
        employee = EMPLOYEES[subject]
        request_id = success(response, employee=employee)
        request_ids.append(request_id)
        chain.assert_audit(
            request_id, ["query_started", "query_succeeded"], subject=subject, employee=employee
        )
    assert len(set(request_ids)) == 2
    assert len(chain.audit()) == 4
    assert chain.credential_reads() == 6


async def test_same_employee_number_in_distinct_enterprises_keeps_fixed_enterprise(chain_factory):
    a, b = chain_factory(enterprise="ENT-A"), chain_factory(enterprise="ENT-B")
    a.barrier = b.barrier = RequestBarrier()
    async with a.connection() as ca, b.connection() as cb:
        with anyio.fail_after(3):
            results = await asyncio.gather(a.call(ca), b.call(cb))
    assert a.barrier.arrivals == 2
    request_ids = []
    for service, response in zip((a, b), results, strict=True):
        request_id = success(response, employee="E001", enterprise=service.enterprise)
        request_ids.append(request_id)
        assert service.targets() == [(service.enterprise, "E001")]
        service.assert_audit(request_id, ["query_started", "query_succeeded"])
    assert len(set(request_ids)) == 2
    assert a.secret != b.secret
    assert [body["key"] for body in a.requests] == [a.secret]
    assert [body["key"] for body in b.requests] == [b.secret]


@pytest.mark.parametrize("case", ["unmapped", "disabled", "client", "tool", "duplicate"])
async def test_policy_denials_never_read_key_or_call_upstream(chain, case):
    subject = "employee-a"
    bearer = AUTO_TOKEN
    expected = "identity_not_enabled"
    if case == "unmapped":
        subject = "unmapped"
    elif case == "disabled":
        chain.policy["principals"][0]["enabled"] = False
    elif case == "client":
        chain.policy["principals"][0]["allowed_clients"] = ["agent-b"]
        expected = "permission_denied"
    elif case == "tool":
        chain.policy["principals"][0]["allow_employee_read"] = False
        expected = "permission_denied"
    else:
        chain.policy["principals"].append({**chain.policy["principals"][0], "employee_id": "OTHER"})
        expected = "policy_unavailable"
    chain.save_policy()
    async with chain.connection() as client:
        response = await chain.call(client, subject=subject, bearer=bearer)
    request_id = failure(response, expected)
    assert chain.requests == [] and chain.credential_reads() == 0
    chain.assert_audit(request_id, ["query_failed"], subject=subject, employee=None, code=expected)


@pytest.mark.parametrize("case", ["missing", "expired", "forged", "api-key-only"])
async def test_native_auth_rejection_has_no_business_identity_or_key_reads(chain, case):
    bearer = None
    if case == "expired":
        bearer = chain.token(expires_in_seconds=-60)
    elif case == "forged":
        bearer = chain.token(signer=RSAKeyPair.generate())
    headers = {"X-API-Key": SECRET} if case == "api-key-only" else None
    async with chain.connection() as client:
        response = await chain.call(client, bearer=bearer, headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    if case in {"missing", "api-key-only"}:
        # 原生 FastMCP 有意在初始 OAuth 发现阶段省略错误响应体。
        assert response.content == b""
    else:
        assert response.json()["error"] == "invalid_token"
    assert "Employee ENT-" not in response.text
    assert SECRET not in response.text and PRIVATE not in response.text
    assert chain.requests == [] and chain.credential_reads() == 0 and chain.audit() == []


async def test_employee_no_argument_is_rejected_without_dispatch_or_echo(chain):
    async with chain.connection() as client:
        response = await chain.call(client, arguments={"employee_no": "injected-other-employee"})
    assert failure(response, "invalid_arguments") is None
    assert "injected-other-employee" not in response.text
    assert chain.requests == [] and chain.credential_reads() == 0 and chain.audit() == []


@pytest.mark.parametrize(
    "injection", ["headers", "email", "name", "meta", "api-key", "signed-target"]
)
async def test_identity_injections_and_signed_targets_cannot_override_mapping(chain, injection):
    claims, headers, meta = {}, {}, None
    if injection == "headers":
        headers = {"X-Subject": "employee-b", "X-Employee-No": "E002", "X-User-Id": "employee-b"}
    elif injection in {"email", "name"}:
        claims = {injection: "employee-b@example.invalid" if injection == "email" else "employee-b"}
    elif injection == "meta":
        meta = {"subject": "employee-b", "employee_no": "E002", "EntId": "ENT-B"}
    elif injection == "api-key":
        headers = {"X-API-Key": "synthetic-key-for-other-subject"}
    else:
        claims = {"employee_no": "E002", "employee_id": "E002", "EntId": "ENT-B"}
    async with chain.connection() as client:
        response = await chain.call(
            client, bearer=chain.token(claims=claims), headers=headers, meta=meta
        )
    request_id = success(response, employee="E001")
    assert chain.targets() == [("ENT-A", "E001")]
    chain.assert_audit(request_id, ["query_started", "query_succeeded"])


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("wrong", "upstream_employee_mismatch"),
        ("zero", "employee_not_found"),
        ("multi", "employee_not_unique"),
        ("unauthenticated", "upstream_unauthenticated"),
    ],
)
async def test_upstream_failures_have_one_request_and_no_employee_payload(chain, case, code):
    chain.response_case = case
    async with chain.connection() as client:
        response = await chain.call(client)
    request_id = failure(response, code)
    assert chain.targets() == [("ENT-A", "E001")]
    chain.assert_audit(request_id, ["query_started", "query_failed"], code=code, attempted=True)


@pytest.mark.parametrize("change", ["revoke", "rebind"])
async def test_durable_start_then_policy_change_prevents_dispatch(chain, change):
    def changed():
        if change == "revoke":
            chain.revoke()
        else:
            chain.policy["principals"][0]["employee_id"] = "E002"
            chain.save_policy()

    chain.journal.after_start = changed
    code = "identity_not_enabled" if change == "revoke" else "authorization_changed"
    async with chain.connection() as client:
        response = await chain.call(client)
    request_id = failure(response, code)
    assert chain.requests == []
    assert chain.credential_reads() == (1 if change == "revoke" else 2)
    chain.assert_audit(request_id, ["query_started", "query_failed"], code=code, attempted=False)


async def test_revocation_after_actual_upstream_call_withholds_data_and_keeps_failure_trace(chain):
    chain.after_upstream = chain.revoke
    async with chain.connection() as client:
        response = await chain.call(client)
    request_id = failure(response, "identity_not_enabled")
    assert chain.targets() == [("ENT-A", "E001")]
    assert chain.credential_reads() == 2
    chain.assert_audit(
        request_id,
        ["query_started", "query_failed"],
        code="identity_not_enabled",
        attempted=True,
    )


@pytest.mark.parametrize("stage", ["start", "terminal"])
async def test_durable_journal_failure_never_becomes_business_success_or_retry(chain, stage):
    chain.journal.fail_events = (
        {"query_started"} if stage == "start" else {"query_succeeded", "query_failed"}
    )
    async with chain.connection() as client:
        response = await chain.call(client)
    request_id = failure(response, "journal_unavailable")
    if stage == "start":
        assert chain.requests == []
        chain.assert_audit(request_id, ["query_failed"], code="journal_unavailable")
        assert await chain.journal.pending_request_ids() == []
    else:
        assert chain.targets() == [("ENT-A", "E001")]
        chain.assert_audit(request_id, ["query_started"])
        assert await chain.journal.pending_request_ids() == [request_id]


@pytest.mark.parametrize("kind", ["http-timeout", "tool-timeout"])
async def test_timeout_has_at_most_one_upstream_attempt_and_no_retry(chain, kind):
    chain.response_case = kind
    if kind == "tool-timeout":
        chain.dependencies["budget_seconds"] = 1.0
    code = "upstream_timeout" if kind == "http-timeout" else "query_timeout"
    async with chain.connection() as client:
        response = await chain.call(client)
    request_id = failure(response, code)
    assert chain.targets() == [("ENT-A", "E001")]
    assert chain.active_upstream == 0
    chain.assert_audit(request_id, ["query_started", "query_failed"], code=code, attempted=True)


async def test_only_the_existing_parameterless_read_tool_is_discoverable(chain):
    async with chain.connection() as client:
        response = await client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
                "Authorization": f"Bearer {chain.token()}",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == [TOOL_NAME]
    assert tools[0]["inputSchema"]["properties"] == {}
    assert tools[0]["annotations"]["readOnlyHint"] is True
    assert chain.requests == [] and chain.credential_reads() == 0 and chain.audit() == []
