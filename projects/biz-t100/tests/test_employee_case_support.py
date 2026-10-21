"""私有批准用例的支撑代码，仅使用合成员工数据测试。"""

import hashlib
import importlib
import json
import os
import stat
import traceback
from pathlib import Path

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from biz_t100.access import FileServiceAccountAccess
from biz_t100.models import BusinessError, Caller
from biz_t100.server import SCOPE

_PROJECT = Path(__file__).resolve().parents[1]
_DATA = {"employee_no": "SYNTH-CASE-001", "display_name": "Synthetic Case Person"}


@pytest.fixture
def support(monkeypatch):
    monkeypatch.syspath_prepend(str(_PROJECT / "tests" / "legacy_support"))
    return importlib.import_module("employee_case_support")


@pytest.fixture
def input_file(tmp_path):
    source = tmp_path / "approved-input.json"
    source.write_text(json.dumps(_DATA))
    source.chmod(0o600)
    return source


@pytest.fixture
def state(support, tmp_path, input_file):
    root = tmp_path / "employee-case"
    support.init_case(root, input_file)
    return support.load_case(root)


def replace_json(path, change):
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


def failure(code, action):
    with pytest.raises(BusinessError) as error:
        action()
    assert error.value.code == code
    printed = "".join(traceback.format_exception(error.value))
    assert _DATA["employee_no"] not in printed
    assert _DATA["display_name"] not in printed
    assert "PRIVATE KEY" not in printed


def test_private_read_only_layout_hides_employee_and_secrets(support, state, capsys):
    assert state.mode == support.MODE == "approved-employee-simulated"
    assert state.issuer == "https://approved-read-case.example.invalid"
    assert state.employee_no == _DATA["employee_no"]
    assert state.display_name == _DATA["display_name"]
    assert state.identity_profile("read").audience == "approved-t100-read"
    assert state.identity_profile("read").allowed_clients == {"approved-read-client"}
    assert state.t100_config("read").url == "https://internal-read.example.invalid/api"
    assert state.t100_config("read").enterprise_id == "INTERNAL-READ"
    assert (
        state.case_digest == hashlib.sha256((state.root / "fixture.json").read_bytes()).hexdigest()
    )
    assert (
        state.credential_digest("read")
        == hashlib.sha256(state.credential_path("read").read_bytes()).hexdigest()
    )
    assert not (state.root / "reset").exists()
    assert not state.journal_path("read").exists()
    for path in (state.root, *state.root.rglob("*")):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    hidden = [state.employee_no, state.display_name, state.public_key, state._credential_digest]
    assert all(item not in repr(state) for item in hidden)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "method",
    [
        "identity_profile",
        "client_id",
        "policy_path",
        "credential_path",
        "journal_path",
        "capture_path",
        "credential_digest",
        "t100_config",
    ],
)
def test_no_service_interface_accepts_reset(state, method):
    failure("internal_case_state_invalid", lambda: getattr(state, method)("reset"))


def test_loading_and_idempotent_init_never_read_secret_contents(
    support, state, input_file, monkeypatch
):
    read = support._read_private
    seen = []

    def observed(path, maximum):
        assert path.name not in {"issuer-private.pem", "service.key"}
        seen.append(path)
        return read(path, maximum)

    monkeypatch.setattr(support, "_read_private", observed)
    assert support.load_case(state.root) == state
    result = support.init_case(state.root, input_file)
    assert result == {"status": "already_initialized", "mode": state.mode, "root": str(state.root)}
    assert _DATA["employee_no"] not in json.dumps(result)
    assert state.publickey_file in seen


def test_idempotent_init_preserves_disabled_policy_keys_and_audit(support, state, input_file):
    replace_json(
        state.policy_path("read"),
        lambda value: (
            value.update(revision="revoked-for-check"),
            value["principals"][0].update(enabled=False, allow_employee_read=False),
        ),
    )
    audit = state.journal_path("read")
    audit.write_bytes(b"preserve-synthetic-audit")
    audit.chmod(0o600)
    files = [path for path in state.root.rglob("*") if path.is_file()]
    before = {path: (path.stat().st_ino, path.read_bytes()) for path in files}
    assert support.init_case(state.root, input_file)["status"] == "already_initialized"
    assert {path: (path.stat().st_ino, path.read_bytes()) for path in files} == before


def test_init_cannot_rebind_existing_case(support, state, input_file):
    before = (state.root / "fixture.json").read_bytes()
    input_file.write_text(json.dumps({**_DATA, "employee_no": "SYNTH-OTHER"}))
    failure("internal_case_state_invalid", lambda: support.init_case(state.root, input_file))
    assert (state.root / "fixture.json").read_bytes() == before
    assert support.load_case(state.root) == state


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "[]",
        "null",
        json.dumps({**_DATA, "extra": "unsupported"}),
        json.dumps({**_DATA, "employee_no": 10001}),
        json.dumps({**_DATA, "employee_no": "../somewhere"}),
        json.dumps({**_DATA, "display_name": " "}),
        json.dumps({**_DATA, "display_name": "display\nname"}),
        json.dumps({**_DATA, "display_name": "display\u202ename"}),
        json.dumps({**_DATA, "display_name": "display\u0085name"}),
        json.dumps({**_DATA, "display_name": 123}),
        '{"employee_no":"SYNTH-A","employee_no":"SYNTH-B","display_name":"Test"}',
    ],
)
def test_input_schema_is_strict_and_invalid_input_creates_no_state(
    support, input_file, tmp_path, raw
):
    input_file.write_text(raw)
    root = tmp_path / "rejected"
    failure("internal_case_input_invalid", lambda: support.init_case(root, input_file))
    assert not root.exists()


@pytest.mark.parametrize("kind", ["public", "symlink", "hardlink", "oversized"])
def test_input_must_be_private_bounded_regular_file(support, input_file, tmp_path, kind):
    if kind == "public":
        input_file.chmod(0o644)
    elif kind == "symlink":
        original = tmp_path / "original-input"
        input_file.rename(original)
        input_file.symlink_to(original)
    elif kind == "hardlink":
        os.link(input_file, tmp_path / "second-input")
    else:
        input_file.write_bytes(b" " * 16_385)
    root = tmp_path / "rejected"
    failure("internal_case_input_invalid", lambda: support.init_case(root, input_file))
    assert not root.exists()


@pytest.mark.parametrize(
    "file,change",
    [
        ("fixture.json", lambda value: value.update(employee_no="SYNTH-OTHER")),
        ("fixture.json", lambda value: value.update(display_name="Another synthetic name")),
        ("state.json", lambda value: value.update(schema_version=True)),
        ("state.json", lambda value: value.update(issuer="https://different.example.invalid")),
        ("state.json", lambda value: value["services"]["read"].update(url="https://other.invalid")),
        ("state.json", lambda value: value["services"].update(reset={})),
        (
            "read/access.json",
            lambda value: value["principals"][0].update(employee_id="SYNTH-OTHER"),
        ),
        (
            "read/access.json",
            lambda value: value["principals"][0].update(t100_account_id="LOGIN-X"),
        ),
        (
            "read/access.json",
            lambda value: value["principals"][0].update(allow_password_reset=True),
        ),
        ("read/access.json", lambda value: value["principals"][0].update(subject="someone-else")),
        (
            "read/access.json",
            lambda value: value["principals"][0].update(allowed_clients=["other"]),
        ),
        (
            "read/access.json",
            lambda value: value["principals"].append(value["principals"][0].copy()),
        ),
    ],
)
def test_state_rejects_unapproved_binding_authority_or_reset(support, state, file, change):
    replace_json(state.root / file, change)
    failure("internal_case_state_invalid", lambda: support.load_case(state.root))


def test_reset_directory_is_not_a_valid_read_only_case(support, state):
    (state.root / "reset").mkdir(mode=0o700)
    failure("internal_case_state_invalid", lambda: support.load_case(state.root))


@pytest.mark.parametrize(
    "path",
    ["fixture.json", "state.json", "issuer-private.pem", "read/access.json", "read/service.key"],
)
def test_required_files_stay_private(support, state, path):
    (state.root / path).chmod(0o640)
    failure("internal_case_state_invalid", lambda: support.load_case(state.root))


def test_public_key_is_canonical_even_if_manifest_is_updated(support, state):
    body = (
        state.publickey_file.read_bytes()
        + RSAKeyPair.generate().private_key.get_secret_value().encode()
    )
    state.publickey_file.write_bytes(body)
    replace_json(
        state.root / "state.json",
        lambda value: value.update(public_key_sha256=hashlib.sha256(body).hexdigest()),
    )
    failure("internal_case_state_invalid", lambda: support.load_case(state.root))


@pytest.mark.parametrize("user", ["simulated-approved-user", "unmapped"])
async def test_native_signed_identity_omits_employee_data(support, state, user, capsys):
    output = state.root / "tokens" / f"{user}.jwt"
    result = support.issue_case_token(state.root, user=user, service="read", output=output)
    encoded = output.read_text().strip()
    verifier = JWTVerifier(
        public_key=state.public_key,
        issuer=state.issuer,
        audience=state.identity_profile("read").audience,
        required_scopes=[SCOPE],
    )
    token = await verifier.verify_token(encoded)
    assert token is not None
    assert token.subject == user
    assert token.client_id == state.client_id("read")
    assert token.scopes == [SCOPE]
    assert set(token.claims) == {"iss", "sub", "aud", "azp", "scope", "iat", "exp"}
    assert token.claims["exp"] - token.claims["iat"] == 300
    assert state.employee_no not in json.dumps(token.claims)
    assert state.display_name not in json.dumps(token.claims)
    assert encoded not in json.dumps(result)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert capsys.readouterr().out == ""
    original_lab = JWTVerifier(
        public_key=state.public_key,
        issuer="https://internal-lab.example.invalid",
        audience="internal-t100-read",
    )
    assert await original_lab.verify_token(encoded) is None


async def test_approved_mapping_authorizes_only_read_and_unmapped_does_not_read_key(
    support, state, monkeypatch
):
    import biz_t100.access as access_module

    caller = Caller(issuer=state.issuer, subject=support.USER, client_id=state.client_id("read"))
    access = FileServiceAccountAccess(state.policy_path("read"), state.credential_path("read"))
    assert (await access.authorize(caller)).employee_id == state.employee_no
    read = access_module._private_bytes

    def denied_key_read(path, maximum):
        assert path != state.credential_path("read")
        return read(path, maximum)

    monkeypatch.setattr(access_module, "_private_bytes", denied_key_read)
    with pytest.raises(BusinessError, match="^identity_not_enabled$"):
        await access.authorize(caller.model_copy(update={"subject": "unmapped"}))
    write = FileServiceAccountAccess(
        state.policy_path("read"), state.credential_path("read"), permission="password_reset"
    )
    with pytest.raises(BusinessError, match="^permission_denied$"):
        await write.authorize(caller)


@pytest.mark.parametrize(
    "change",
    [
        {"service": "reset"},
        {"user": "user-a"},
        {"ttl_seconds": 0},
        {"ttl_seconds": 3601},
        {"ttl_seconds": True},
    ],
)
def test_token_inputs_cannot_widen_scope_or_subject(support, state, change):
    output = state.root / "tokens" / "rejected.jwt"
    arguments = {"user": support.USER, "service": "read", "output": output, **change}
    failure(
        "internal_case_token_invalid", lambda: support.issue_case_token(state.root, **arguments)
    )
    assert not output.exists()


def test_token_cannot_overwrite_private_case_files_or_existing_tokens(support, state):
    before = (state.root / "fixture.json").read_bytes()
    failure(
        "internal_case_token_invalid",
        lambda: support.issue_case_token(
            state.root, user=support.USER, service="read", output=state.root / "fixture.json"
        ),
    )
    assert (state.root / "fixture.json").read_bytes() == before
    output = state.root / "tokens" / "once.jwt"
    support.issue_case_token(state.root, user=support.USER, service="read", output=output)
    token = output.read_bytes()
    failure(
        "internal_case_token_invalid",
        lambda: support.issue_case_token(
            state.root, user=support.USER, service="read", output=output
        ),
    )
    assert output.read_bytes() == token


def test_signing_requires_matching_private_key_without_changing_serve_loader(support, state):
    (state.root / "issuer-private.pem").write_text(
        RSAKeyPair.generate().private_key.get_secret_value()
    )
    assert support.load_case(state.root) == state
    failure(
        "internal_case_token_invalid",
        lambda: support.issue_case_token(
            state.root, user=support.USER, service="read", output=state.root / "tokens" / "bad.jwt"
        ),
    )
