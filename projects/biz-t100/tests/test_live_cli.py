"""统一真实入口的合成验收：原生 FastMCP 身份链、四接口、无真实外联。"""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from test_functions import Harness, request_for

from biz_t100.account_write_store import SQLiteAccountWriteStore
from biz_t100.function_audit import FunctionAudit
from biz_t100.interfaces import REQUEST_MODELS, SERVICES
from biz_t100.models import BusinessError, Caller
from biz_t100.runtime import RuntimeConfig
from biz_t100.server import IdentityProfile


class LiveCase:
    def __init__(self, folder, monkeypatch):
        folder.chmod(0o700)
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
        self.host = importlib.import_module("live")
        self.business = Harness(folder, monkeypatch)
        self.folder = folder
        self.state = folder / "functions"
        self.legacy = folder / "legacy.sqlite3"
        self.after_network = None
        self.config = RuntimeConfig(
            identity=IdentityProfile(
                issuer="https://synthetic-config-identity.example.invalid",
                audience="synthetic-config-audience",
                allowed_clients=frozenset({"synthetic-config-client"}),
            ),
            t100=self.business.config,
            policy_file=folder / "unrelated-self-policy.json",
            credential_file=self.business.key_path,
            journal_path=folder / "unrelated-self-query.sqlite3",
        )
        self.config_path = folder / "runtime.json"
        self.config_path.write_text(self.config.model_dump_json())
        self.config_path.chmod(0o600)
        original_client = self.host.T100InterfaceClient

        async def upstream(request):
            reply = await self.business.upstream(request)
            if self.after_network is not None:
                self.after_network()
            return reply

        def client(config, **options):
            return original_client(config, transport=httpx.MockTransport(upstream), **options)

        monkeypatch.setattr(self.host, "T100InterfaceClient", client)

    @property
    def policy(self):
        return self.state / "permissions.json"

    def permissions(self, **changes):
        self.host.directory(self.state)
        path = self.host.initialize_permissions(self.config, self.state)
        value = json.loads(path.read_text())
        value.update(changes)
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return value

    def approve(self, action, *, copy_source=True):
        rows = {
            "employee_query": [],
            "employee_create": [{"action": action, "employee_no": "E003"}],
            "employee_update": [{"action": action, "employee_no": "E001"}],
            "account_maintain": [
                {
                    "action": action,
                    "employee_no": "E001",
                    "account_id": "LOGIN-A",
                }
            ],
        }[action]
        if action == "account_maintain" and copy_source:
            rows.append(
                {
                    "action": action,
                    "employee_no": "TEMPLATE-A",
                    "account_id": "LOGIN-TEMPLATE",
                }
            )
        return self.permissions(writes_enabled=True, write_targets=rows)

    async def call(self, action="employee_query", request=None, *, confirm=True):
        return await self.host.call(
            self.config,
            action,
            request or request_for(action),
            confirm=confirm,
            state_dir=self.state,
            legacy_path=self.legacy,
        )

    def parse(self, *args):
        return self.host.make_request(self.host.parser().parse_args(list(args)))

    async def audit(self, result):
        return await FunctionAudit(
            self.state / "audit/operations.sqlite3",
            enterprise_id=self.config.t100.enterprise_id,
            legacy_write_path=self.legacy,
        ).status(result["request_id"])


@pytest.fixture
def live_case(tmp_path, monkeypatch):
    return LiveCase(tmp_path, monkeypatch)


@pytest.mark.parametrize("action", tuple(REQUEST_MODELS))
async def test_unified_live_call_uses_native_mcp_and_document_service(
    live_case, monkeypatch, action
):
    case = live_case
    if action != "employee_query":
        case.approve(action)
    captured = []
    original_rpc = case.host.rpc

    async def rpc(client, method, params, identifier):
        if method == "tools/call":
            captured.append(params)
        return await original_rpc(client, method, params, identifier)

    monkeypatch.setattr(case.host, "rpc", rpc)
    request = request_for(action)
    result = await case.call(action, request)
    expected = {
        "employee_query": ("passed", 1, 0),
        "employee_create": ("verified", 1, 1),
        "employee_update": ("verified", 2, 1),
        "account_maintain": ("acknowledged", 1, 1),
    }[action]
    assert (result["status"], result["read_requests"], result["write_requests"]) == expected, result
    assert result["mode"] == "live"
    assert result["real_identity_verified"] is False
    assert result["protocol"] == "2025-11-25"
    assert result["identity"] == "temporary-local-operator"
    assert len(captured) == 1
    assert captured[0]["name"] == case.host.TOOLS[action]
    assert captured[0]["arguments"]["request"] == request.model_dump(
        mode="json", exclude_unset=True
    )
    actual = next(
        item for item in case.business.requests if item["service"]["name"] == SERVICES[action]
    )
    sent = actual["payload"]["std_data"]["parameter"]
    if action == "employee_update":
        intent = request.parameter()["employee"][0]
        assert all(sent["employee"][0][key] == value for key, value in intent.items())
        assert all(
            case.business.rows["E001"][key] == value for key, value in sent["employee"][0].items()
        )
    else:
        assert sent == request.parameter()
    if action != "employee_query":
        prepared = json.loads(Path(result["effective_request_path"]).read_text())
        assert prepared["parameter"] == sent
        assert prepared["effective_request_digest"] == result["effective_request_digest"]
    assert (await case.audit(result))["operation_state"] == "succeeded"
    evidence = Path(result["evidence_path"])
    assert evidence.stat().st_mode & 0o777 == 0o600
    assert json.loads(evidence.read_text())["request_id"] == result["request_id"]


async def test_local_operator_is_stable_across_independently_authorized_native_calls(
    live_case, monkeypatch
):
    case = live_case
    tokens = []
    signing_keys = []
    original_rpc = case.host.rpc
    original_generate = case.host.RSAKeyPair.generate

    def generate():
        pair = original_generate()
        signing_keys.append(pair.private_key.get_secret_value())
        return pair

    async def rpc(client, method, params, identifier):
        if method == "tools/call":
            tokens.append(client.headers["Authorization"].removeprefix("Bearer "))
        return await original_rpc(client, method, params, identifier)

    monkeypatch.setattr(case.host, "RSAKeyPair", SimpleNamespace(generate=generate))
    monkeypatch.setattr(case.host, "rpc", rpc)
    results = []
    for action in ("employee_update", "account_maintain"):
        case.approve(action)
        request = request_for(action)
        result = await case.call(action, request)
        assert result["status"] in {"verified", "acknowledged"}, result
        results.append(result)
        audit = await case.audit(result)
        assert (audit["issuer"], audit["subject"], audit["client_id"]) == (
            case.host.ISSUER,
            "temporary-local-operator",
            case.host.CLIENT,
        )
        assert audit["operation_state"] == "succeeded"
        folder = Path(result["evidence_path"]).parent
        permission = json.loads((folder / "permission.json").read_text())
        employees, accounts = case.host.request_targets(action, request)
        assert (permission["issuer"], permission["subject"], permission["client_id"]) == (
            audit["issuer"], audit["subject"], audit["client_id"]
        )
        assert permission["action"] == audit["action"] == action
        assert permission["request_digest"] == audit["request_digest"] == (
            case.host.request_digest(action, request)
        )
        assert permission["employee_ids"] == audit["employee_ids"] == list(employees)
        assert permission["account_ids"] == audit["account_ids"] == list(accounts)

    assert results[0]["request_id"] != results[1]["request_id"]
    assert results[0]["evidence_path"] != results[1]["evidence_path"]
    assert len(tokens) == len(set(tokens)) == 2
    assert len(signing_keys) == len(set(signing_keys)) == 2
    for path in case.state.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert b"PRIVATE KEY" not in content
            assert case.business.key_path.read_bytes() not in content
            assert all(token.encode() not in content for token in tokens)
            assert all(key.encode() not in content for key in signing_keys)
            assert path.suffix not in {".pem", ".key"}


async def test_query_generates_no_persisted_token_or_signing_key_and_leaves_writes_disabled(
    live_case, monkeypatch
):
    case = live_case
    tokens = []
    original_rpc = case.host.rpc

    async def rpc(client, method, params, identifier):
        tokens.append(client.headers["Authorization"].removeprefix("Bearer "))
        return await original_rpc(client, method, params, identifier)

    monkeypatch.setattr(case.host, "rpc", rpc)
    result = await case.call()
    assert result["status"] == "passed"
    assert tokens and all(token.count(".") == 2 for token in tokens)
    permissions = json.loads(case.policy.read_text())
    assert permissions["queries_enabled"] is True
    assert permissions["writes_enabled"] is False
    assert permissions["write_targets"] == []
    for path in case.state.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert b"PRIVATE KEY" not in content
            assert case.business.key_path.read_bytes() not in content
            assert all(token.encode() not in content for token in tokens)
            assert path.suffix not in {".pem", ".key"}


@pytest.mark.parametrize("action", ["employee_create", "employee_update", "account_maintain"])
async def test_writes_default_disabled_before_key_and_upstream(live_case, action):
    case = live_case
    with pytest.raises(BusinessError, match="^write_permission_not_configured$"):
        await case.call(action)
    assert case.business.requests == case.business.key_reads == []
    assert not (case.state / "audit").exists()


async def test_missing_confirmation_is_rejected_before_initializing_runtime(live_case):
    case = live_case
    with pytest.raises(BusinessError, match="^confirmation_required$"):
        await case.call("employee_update", confirm=False)
    assert case.business.requests == case.business.key_reads == []
    assert not case.state.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"employee_no": "E002", "account_id": "LOGIN-A"},
        {"employee_no": "E001", "account_id": "LOGIN-B"},
        {"employee_no": "E001", "account_id": "LOGIN-A", "action": "employee_update"},
    ],
)
async def test_account_requires_exact_employee_account_and_action_approval(live_case, change):
    case = live_case
    case.approve("account_maintain")
    target = {"action": "account_maintain", **change}
    case.permissions(write_targets=[target])
    with pytest.raises(BusinessError, match="^permission_denied$"):
        await case.call("account_maintain")
    assert case.business.requests == case.business.key_reads == []


async def test_copy_requires_independently_approved_source_before_credential(live_case):
    case = live_case
    case.approve("account_maintain", copy_source=False)
    with pytest.raises(BusinessError, match="^permission_denied$"):
        await case.call("account_maintain")
    assert case.business.requests == case.business.key_reads == []


@pytest.mark.parametrize(
    "changes",
    [
        {"enterprise_id": "OTHER-ENT"},
        {"upstream_config_digest": "f" * 64},
    ],
)
async def test_runtime_enterprise_and_configuration_binding_fail_before_key(live_case, changes):
    case = live_case
    case.permissions(**changes)
    with pytest.raises(BusinessError, match="^policy_unavailable$"):
        await case.call()
    assert case.business.requests == case.business.key_reads == []


async def test_valid_signature_without_tool_scope_is_rejected_by_native_mcp(live_case, monkeypatch):
    case = live_case
    pair = RSAKeyPair.generate()

    class WrongScopePair:
        public_key = pair.public_key

        def create_token(self, **kwargs):
            kwargs["scopes"] = []
            return pair.create_token(**kwargs)

    monkeypatch.setattr(case.host, "RSAKeyPair", SimpleNamespace(generate=WrongScopePair))
    with pytest.raises(BusinessError, match="^mcp_call_failed$"):
        await case.call()
    assert case.business.requests == case.business.key_reads == []


async def test_revocation_after_setup_before_native_authorization_reads_no_key(
    live_case, monkeypatch
):
    case = live_case
    original_save = case.host.save

    def save_then_revoke(path, value):
        original_save(path, value)
        if path.name == "permission.json":
            case.permissions(queries_enabled=False)

    monkeypatch.setattr(case.host, "save", save_then_revoke)
    result = await case.call()
    assert result["status"] == "failed", result
    assert result["error"] == "permission_denied"
    assert case.business.requests == case.business.key_reads == []
    assert "data" not in result


@pytest.mark.parametrize("action", ["employee_query", "employee_update"])
async def test_revocation_during_query_prevents_data_delivery_or_following_write(live_case, action):
    case = live_case
    if action != "employee_query":
        case.approve(action)
    case.after_network = lambda: case.permissions(queries_enabled=False, writes_enabled=False)
    result = await case.call(action)
    assert result["status"] == "failed", result
    assert "data" not in result
    assert result["business_effect_verified"] is False
    assert result["read_requests"] == 1
    assert result["write_requests"] == 0
    assert len(case.business.requests) == 1
    assert (await case.audit(result))["operation_state"] == "failed"


async def test_revocation_after_update_preserves_unknown_without_delivering_data(live_case):
    case = live_case
    case.approve("employee_update")
    case.business.after_write = lambda: case.permissions(writes_enabled=False)
    result = await case.call("employee_update")
    assert result["status"] == "failed", result
    assert "data" not in result
    assert result["business_effect_verified"] is False
    assert (result["read_requests"], result["write_requests"]) == (1, 1)
    assert (await case.audit(result))["operation_state"] == "unknown"


@pytest.mark.parametrize("action", ["employee_create", "employee_update", "account_maintain"])
async def test_legacy_unknown_blocks_new_writes_and_preserves_original_database(live_case, action):
    case = live_case
    employee_id = "E003" if action == "employee_create" else "E001"
    old = SQLiteAccountWriteStore(
        case.legacy, enterprise_id=case.config.t100.enterprise_id, action="disable_account"
    )
    await old.prepare()
    operation_id = str(uuid4())
    await old.reserve(
        operation_id=operation_id,
        caller=Caller(
            issuer="https://synthetic-legacy.invalid", subject="original", client_id="old"
        ),
        employee_id=employee_id,
        account_id="LOGIN-A",
        binding_version="old-authority",
    )
    await old.mark_dispatched(operation_id=operation_id)
    await old.finish(operation_id=operation_id, outcome="unknown", code="upstream_timeout")
    before = case.legacy.read_bytes()
    case.approve(action)
    result = await case.call(action)
    assert result["status"] == "failed", result
    assert result["error"] == "reset_pending_review"
    assert result["upstream_attempted"] is False
    assert result["read_requests"] == result["write_requests"] == 0
    assert case.business.requests == []
    assert case.legacy.read_bytes() == before


def test_command_parser_preserves_name_paging_and_explicit_update_fields(live_case):
    action, query = live_case.parse(
        "employee-query", "--employee-name", "Synthetic", "--page-size", "20", "--page-no", "2"
    )
    assert action == "employee_query"
    assert query.parameter() == {
        "employee_name": "Synthetic",
        "page_size": 20,
        "page_no": 2,
        "use_has_next": True,
        "is_digiwin_product": "N",
    }
    action, update = live_case.parse("employee-update", "--employee-no", "E001", "--status", "N")
    assert action == "employee_update"
    assert update.parameter()["employee"] == [{"employee_no": "E001", "status": "N"}]


@pytest.mark.parametrize("field", ["employee-no", "account-id"])
def test_account_command_reports_empty_required_identifier(live_case, field):
    args = [
        "account-maintain",
        "--employee-no",
        "E001",
        "--account-id",
        "LOGIN-A",
        "--account-status",
        "N",
        "--is-reproduce",
        "N",
        "--password-reset",
        "N",
    ]
    args[args.index("--" + field) + 1] = ""
    with pytest.raises(BusinessError, match="^empty_identifier$"):
        live_case.parse(*args)


def test_empty_identifier_has_actionable_chinese_result(live_case):
    shown = live_case.host.present_result(
        {
            "status": "failed",
            "error": "empty_identifier",
            "business_effect_verified": False,
            "upstream_attempted": False,
        },
        action="account_maintain",
    )
    assert shown["失败原因"] == "员工工号或登录账号为空。"
    assert "$EMPLOYEE_NO" in shown["处理建议"]
    assert shown["upstream_attempted"] is False


def test_private_full_json_preserves_account_copy_roles_and_sites(live_case):
    case = live_case
    expected = request_for("account_maintain")
    path = case.folder / "full-parameter.json"
    path.write_text(json.dumps(expected.parameter()))
    path.chmod(0o600)
    action, request = case.parse("account-maintain", "--data-file", str(path))
    assert action == "account_maintain"
    assert request.parameter() == expected.parameter()
    assert request.master[0].detail[0].detail1[0].gzxc007 == "N"


@pytest.mark.parametrize("failure", ["public_file", "mixed_arguments", "unknown_field"])
def test_raw_json_rejects_nonprivate_ambiguous_or_out_of_contract_input(live_case, failure):
    case = live_case
    path = case.folder / "parameter.json"
    value = {"employee_no": "E001"}
    if failure == "unknown_field":
        value["key"] = "synthetic-forbidden-secret"
    path.write_text(json.dumps(value))
    path.chmod(0o644 if failure == "public_file" else 0o600)
    args = ["employee-query", "--data-file", str(path)]
    if failure == "mixed_arguments":
        args += ["--employee-no", "E002"]
    with pytest.raises((BusinessError, ValueError)):
        case.parse(*args)
    assert case.business.requests == case.business.key_reads == []


async def test_copy_source_matching_only_account_id_is_denied_before_key_or_upstream(live_case):
    case = live_case
    case.approve("account_maintain")
    case.permissions(
        write_targets=[
            {"action": "account_maintain", "employee_no": "E001", "account_id": "LOGIN-A"},
            {
                "action": "account_maintain",
                "employee_no": "E-TEMPLATE",
                "account_id": "TEMPLATE-A",
            },
        ]
    )
    request = request_for("account_maintain")
    assert request.master[0].reproduce_acct == "TEMPLATE-A"
    with pytest.raises(BusinessError, match="^permission_denied$"):
        await case.call("account_maintain", request)
    assert case.business.requests == case.business.key_reads == []
    assert not (case.state / "audit").exists()


async def test_chinese_empty_query_summary_matches_private_saved_result(live_case):
    case = live_case
    case.business.rows.clear()
    result = await case.call()
    assert result["status"] == "passed"
    assert result["data"]["employee"] == []
    assert result["操作说明"] == "员工资料查询"
    assert result["状态说明"] == "查询完成，未找到匹配员工"
    assert result["匹配员工数"] == 0
    assert result["read_requests"] == 1
    assert result["write_requests"] == 0
    assert "E001" in result["处理建议"]
    assert "失败原因" not in result
    assert list(result)[:2] == ["操作说明", "状态说明"]
    assert json.loads(Path(result["evidence_path"]).read_text()) == result


def test_chinese_page_count_describes_returned_batch_without_changing_contract(live_case):
    original = {
        "action": "employee_query",
        "status": "passed",
        "upstream_attempted": True,
        "data": {"employee": [{"employee_no": "E001"}], "total_results": 80, "has_next": True},
        "read_requests": 1,
        "write_requests": 0,
    }
    shown = live_case.host.present_result(original)
    assert shown["匹配员工数"] == 1
    assert shown["状态说明"] == "查询完成，本次返回 1 条员工资料（还有下一页）"
    assert {key: shown[key] for key in original} == original
    assert live_case.host.present_result(shown) == shown


@pytest.mark.parametrize(
    "status,effect,phrase",
    [
        ("verified", True, "提交字段已回读核对一致"),
        ("acknowledged", False, "业务效果尚未验证"),
        ("failed", False, "请求失败"),
    ],
)
def test_chinese_status_does_not_confuse_verified_acknowledged_and_failed(
    live_case, status, effect, phrase
):
    original = {
        "action": "employee_update",
        "status": status,
        "business_effect_verified": effect,
        "upstream_attempted": True,
    }
    if status == "failed":
        original["error"] = "upstream_rejected"
    shown = live_case.host.present_result(original)
    assert phrase in shown["状态说明"]
    assert shown["status"] == status
    assert shown["business_effect_verified"] is effect
    if status == "failed":
        assert shown["失败原因"] == "上游返回失败状态或非成功业务码。"
    else:
        assert "失败原因" not in shown


def test_chinese_unknown_history_does_not_turn_observation_into_write_success(live_case):
    original = {
        "action": "employee_update",
        "operation_state": "unknown",
        "code": "upstream_rejected",
        "upstream_attempted": False,
        "recorded_possible_dispatch": True,
    }
    shown = live_case.host.present_result(original, history=True)
    assert shown["操作说明"] == "查看本地操作记录（员工资料修改）"
    assert "写入结果未知" in shown["状态说明"]
    assert "不能判定成功" in shown["状态说明"]
    assert "未调用 T100" not in shown["状态说明"]
    assert shown["operation_state"] == "unknown"
    assert shown["upstream_attempted"] is False


def test_chinese_pending_message_describes_original_unresolved_write(live_case):
    shown = live_case.host.present_result(
        {
            "action": "employee_update",
            "status": "failed",
            "error": "reset_pending_review",
            "upstream_attempted": False,
            "read_requests": 0,
            "write_requests": 0,
        }
    )
    assert shown["状态说明"] == "请求未完成，未调用 T100"
    assert "待核对的写操作" in shown["失败原因"]
    assert "历史未决写入" in shown["处理建议"]
    assert "不要" in shown["处理建议"]


def test_chinese_main_write_permission_explains_confirm_without_claiming_requests(
    live_case, monkeypatch, capsys
):
    case = live_case
    original_call = case.host.call

    async def local_call(config, action, request, *, confirm):
        return await original_call(
            config, action, request, confirm=confirm, state_dir=case.state, legacy_path=case.legacy
        )

    monkeypatch.setattr(case.host, "call", local_call)
    code = case.host.main(
        [
            "--config",
            str(case.config_path),
            "employee-update",
            "--employee-no",
            "E001",
            "--status",
            "N",
            "--confirm",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert result["error"] == "write_permission_not_configured"
    assert result["upstream_attempted"] is False
    assert result["操作说明"] == "员工资料修改"
    assert "未调用 T100" in result["状态说明"]
    assert "尚未开启真实写入" in result["处理建议"]
    assert "--confirm 不能替代" in result["处理建议"]
    assert "read_requests" not in result and "write_requests" not in result
    assert case.business.requests == case.business.key_reads == []


@pytest.mark.parametrize("failure", ["unknown_exception", "mcp_result_failure"])
def test_chinese_main_post_entry_failure_does_not_invent_zero_upstream_attempts(
    live_case, monkeypatch, capsys, failure
):
    case = live_case

    async def broken_call(*args, **kwargs):
        if failure == "mcp_result_failure":
            raise BusinessError("mcp_call_failed")
        raise RuntimeError("synthetic-private-exception-do-not-expose")

    monkeypatch.setattr(case.host, "call", broken_call)
    assert (
        case.host.main(
            ["--config", str(case.config_path), "employee-query", "--employee-no", "E001"]
        )
        == 1
    )
    stdout = capsys.readouterr().out
    result = json.loads(stdout)
    assert result["status"] == "failed"
    assert result["状态说明"] == "请求失败，请按请求编号核查"
    assert "upstream_attempted" not in result
    assert "read_requests" not in result and "write_requests" not in result
    assert "synthetic-private-exception-do-not-expose" not in stdout


def test_chinese_error_text_uses_fixed_messages_not_upstream_free_text(live_case):
    original = {
        "status": "failed",
        "error": "upstream_contract_error",
        "action": "employee_query",
        "message": "synthetic-upstream-private-description",
    }
    shown = live_case.host.present_result(original)
    summary = {key: shown[key] for key in live_case.host._SUMMARY_FIELDS if key in shown}
    assert "synthetic-upstream-private-description" not in json.dumps(summary)
    assert shown["失败原因"] == "上游响应格式、字段类型或大小不符合接口约定。"


async def test_chinese_summary_is_recomputed_when_result_evidence_cannot_be_saved(
    live_case, monkeypatch
):
    case = live_case
    original_save = case.host.save

    def fail_terminal(path, value):
        if path.name == "result.json":
            raise OSError("synthetic-disk-private-description")
        original_save(path, value)

    monkeypatch.setattr(case.host, "save", fail_terminal)
    result = await case.call()
    assert result["status"] == "failed"
    assert result["error"] == "journal_unavailable"
    assert "查询完成" not in result["状态说明"]
    assert "匹配员工数" not in result and "data" not in result
    assert "审计或操作记录不可用" in result["失败原因"]
    assert result["read_requests"] == 1
    assert result["upstream_attempted"] is True
    assert "synthetic-disk-private-description" not in json.dumps(result)


def test_chinese_rejected_field_diagnostic_explains_full_name_without_claiming_no_effect(live_case):
    original = {
        "action": "employee_update",
        "status": "failed",
        "error": "upstream_rejected",
        "upstream_attempted": True,
        "write_upstream_attempted": True,
        "business_effect_verified": False,
        "diagnostics": {
            "http_status": 200,
            "execution_code": "aws-00102",
            "sql_code": "0",
            "invalid_fields": ["full_name"],
        },
    }
    result = live_case.host.present_result(original)
    assert result["失败原因"] == "上游提示员工全名（full_name）缺失或无效。"
    assert "有效字段" in result["处理建议"]
    assert "最终结果" in result["处理建议"]
    assert "不要自动重试" in result["处理建议"]
    assert "最终效果需核查" in result["状态说明"]
    assert result["error"] == "upstream_rejected"
    assert result["business_effect_verified"] is False
    assert result["diagnostics"] == original["diagnostics"]
    assert "未生效" not in result["状态说明"] + result["失败原因"]


@pytest.mark.parametrize(
    "fields",
    [
        ["unknown_field"],
        ["full_name", "synthetic-private-field"],
        "full_name输入的资料无效！",
        [{"field": "full_name"}],
        None,
    ],
)
def test_chinese_invalid_fields_rechecks_whitelist_and_never_copies_free_text(live_case, fields):
    result = live_case.host.present_result(
        {
            "action": "employee_update",
            "status": "failed",
            "error": "upstream_rejected",
            "diagnostics": {"invalid_fields": fields, "description": "synthetic-private-message"},
        }
    )
    assert result["失败原因"] == "上游返回失败状态或非成功业务码。"
    summary = {key: result[key] for key in live_case.host._SUMMARY_FIELDS if key in result}
    assert "synthetic-private" not in json.dumps(summary)
    assert "unknown_field" not in json.dumps(summary)
    assert "输入的资料无效" not in json.dumps(summary)


async def test_target_scope_allows_disable_enable_without_reconfiguring_permission(live_case):
    from biz_t100.interfaces import EmployeeUpdateRequest

    case = live_case
    case.approve("employee_update")
    policy_before = case.policy.read_bytes()
    for status in ("N", "Y"):
        request = EmployeeUpdateRequest(employee=[{"employee_no": "E001", "status": status}])
        result = await case.call("employee_update", request)
        assert result["status"] == "verified", result
        assert case.business.rows["E001"]["status"] == status
        assert case.policy.read_bytes() == policy_before
        prepared = json.loads(Path(result["effective_request_path"]).read_text())
        assert prepared["parameter"]["employee"][0]["full_name"] == "Synthetic Employee E001"
        assert prepared["parameter"]["employee"][0]["status"] == status
    assert len(case.business.requests) == 6


def test_definitive_rejection_chinese_does_not_claim_unknown(live_case):
    result = live_case.host.present_result(
        {
            "status": "failed",
            "error": "upstream_rejected",
            "action": "employee_update",
            "write_outcome": "rejected",
            "upstream_attempted": True,
            "write_upstream_attempted": True,
        }
    )
    assert "上游明确拒绝" in result["状态说明"]
    assert "最终效果需核查" not in result["状态说明"]


def test_failed_rejection_audit_does_not_claim_released_reservation(live_case):
    result = live_case.host.present_result(
        {
            "status": "failed",
            "error": "journal_unavailable",
            "action": "employee_update",
            "write_outcome": "rejected",
            "upstream_attempted": True,
            "write_upstream_attempted": True,
        }
    )
    assert "已记录失败" not in result["状态说明"]
    assert "不作为结果未知占用" not in result["状态说明"]
