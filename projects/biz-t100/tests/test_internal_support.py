"""私有的合成实验环境状态与令牌签发；不使用业务网络。"""

import hashlib
import importlib.util
import json
import os
import stat
import sys
import traceback
from pathlib import Path

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from biz_t100.models import BusinessError, Caller
from biz_t100.reset_server import RESET_SCOPE
from biz_t100.server import SCOPE

_SOURCE = Path(__file__).resolve().parents[1] / "tests" / "legacy_support" / "internal_support.py"
_SPEC = importlib.util.spec_from_file_location("_tested_internal_support", _SOURCE)
lab = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = lab
_SPEC.loader.exec_module(lab)


@pytest.fixture
def state(tmp_path):
    root = tmp_path / "lab"
    lab.init_state(root)
    return lab.load_state(root)


def replace_json(path, change):
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


def assert_failure(code, action):
    with pytest.raises(BusinessError) as failure:
        action()
    assert failure.value.code == code
    assert str(failure.value) == code
    assert "PRIVATE KEY" not in "".join(traceback.format_exception(failure.value))


def test_init_has_private_modes_and_distinct_fixed_services(tmp_path, capsys):
    root = tmp_path / "lab"
    summary = lab.init_state(root)
    assert summary == {"status": "initialized", "mode": "internal-simulated", "root": str(root)}
    state = lab.load_state(root)
    assert state.issuer == "https://internal-lab.example.invalid"
    digests = []
    for service in ("read", "reset"):
        assert state.t100_config(service).url == f"https://internal-{service}.example.invalid/api"
        assert state.t100_config(service).enterprise_id == f"INTERNAL-{service.upper()}"
        assert state.identity_profile(service).audience == f"internal-t100-{service}"
        assert state.identity_profile(service).allowed_clients == {f"internal-{service}-client"}
        secret = state.credential_path(service).read_bytes()
        digests.append(state.credential_digest(service))
        assert hashlib.sha256(secret).hexdigest() == state.credential_digest(service)
        assert secret.decode() not in json.dumps(summary)
        assert secret.decode() not in repr(state)
        assert state.capture_path(service) == root / service / "upstream.jsonl"
        assert not state.journal_path(service).exists()
        assert not state.capture_path(service).exists()
    assert digests[0] != digests[1]
    for path in (root, *root.rglob("*")):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert "PRIVATE KEY" not in json.dumps(summary)
    assert "PUBLIC KEY" not in repr(state)
    assert capsys.readouterr().out == ""


def test_load_and_idempotent_init_do_not_read_signing_or_service_secrets(state, monkeypatch):
    original = lab._read_private
    reads = []

    def observed(path, maximum):
        reads.append(path)
        assert path.name not in {"issuer-private.pem", "service.key"}
        return original(path, maximum)

    monkeypatch.setattr(lab, "_read_private", observed)
    assert lab.load_state(state.root).public_key == state.public_key
    assert lab.init_state(state.root)["status"] == "already_initialized"
    assert state.publickey_file in reads


def test_reinitialization_preserves_revocation_keys_and_audit(state):
    replace_json(
        state.policy_path("read"),
        lambda policy: (
            policy.update(revision="disabled-for-check"),
            policy["principals"][0].update(enabled=False, allow_employee_read=False),
        ),
    )
    sentinel = state.journal_path("read")
    sentinel.write_bytes(b"synthetic-audit-must-not-be-overwritten")
    sentinel.chmod(0o600)
    paths = [
        state.policy_path("read"),
        state.publickey_file,
        state.root / "issuer-private.pem",
        sentinel,
        state.credential_path("read"),
        state.credential_path("reset"),
    ]
    before = {path: (path.stat().st_ino, path.read_bytes()) for path in paths}
    assert lab.init_state(state.root)["status"] == "already_initialized"
    assert {path: (path.stat().st_ino, path.read_bytes()) for path in paths} == before


def test_existing_empty_directory_is_rejected_without_filling_it(tmp_path):
    root = tmp_path / "empty"
    root.mkdir(mode=0o700)
    assert_failure("internal_state_invalid", lambda: lab.init_state(root))
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("root", [Path("relative-lab"), Path("/tmp/parent/../lab"), Path("/")])
def test_roots_must_be_explicit_nontraversing_private_directories(root):
    assert_failure("internal_state_invalid", lambda: lab.init_state(root))
    assert_failure("internal_state_invalid", lambda: lab.load_state(root))


def test_init_rejects_symlink_ancestor_without_creating_target(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    assert_failure("internal_state_invalid", lambda: lab.init_state(alias / "lab"))
    assert not (actual / "lab").exists()


@pytest.mark.parametrize(
    "relative",
    [
        "state.json",
        "issuer-public.pem",
        "issuer-private.pem",
        "read/access.json",
        "read/service.key",
    ],
)
def test_required_files_reject_group_readable_mode(state, relative):
    (state.root / relative).chmod(0o640)
    assert_failure("internal_state_invalid", lambda: lab.load_state(state.root))


@pytest.mark.parametrize("relative", ["", "read", "read/audit", "tokens"])
def test_state_directories_must_stay_private(state, relative):
    (state.root / relative).chmod(0o750)
    assert_failure("internal_state_invalid", lambda: lab.load_state(state.root))


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_required_file_aliases_rejected(state, tmp_path, kind):
    path = state.policy_path("read")
    other = tmp_path / "alias-material"
    if kind == "hardlink":
        os.link(path, other)
    else:
        path.rename(other)
        path.symlink_to(other)
    assert_failure("internal_state_invalid", lambda: lab.load_state(state.root))


def test_symlink_root_is_rejected_even_when_target_is_valid(state, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(state.root, target_is_directory=True)
    assert_failure("internal_state_invalid", lambda: lab.load_state(alias))
    assert_failure("internal_state_invalid", lambda: lab.init_state(alias))


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(issuer="https://another.example.invalid"),
        lambda value: value.update(schema_version=True),
        lambda value: value.update(extra="not-supported"),
        lambda value: value["services"]["read"].update(url="https://outside.example.invalid/api"),
        lambda value: value["services"]["reset"].update(enterprise_id="OTHER"),
        lambda value: value["services"]["read"].update(client_id="OTHER"),
        lambda value: value["services"]["read"].update(credential_sha256="bad-digest"),
    ],
)
def test_state_cannot_select_arbitrary_authority_or_business_endpoint(state, change):
    replace_json(state.root / "state.json", change)
    assert_failure("internal_state_invalid", lambda: lab.load_state(state.root))


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value["principals"][0].update(employee_id="REAL-OTHER"),
        lambda value: value["principals"][0].update(t100_account_id="OTHER-ACCOUNT"),
        lambda value: value["principals"][0].update(subject="another-person"),
        lambda value: value["principals"][0].update(allowed_clients=["another-client"]),
        lambda value: value["principals"][0].update(allow_employee_read=True),
        lambda value: value["principals"][2].update(enabled=True),
        lambda value: value["principals"].pop(),
    ],
)
def test_policy_changes_cannot_escape_synthetic_targets_or_service(state, change):
    replace_json(state.policy_path("reset"), change)
    assert_failure("internal_state_invalid", lambda: lab.load_state(state.root))


def test_oversized_state_file_rejected_before_parse(state):
    (state.root / "state.json").write_bytes(b" " * 16_385)
    assert_failure("internal_state_invalid", lambda: lab.load_state(state.root))


@pytest.mark.parametrize("name", ["journal", "sidecar", "capture"])
def test_existing_audit_artifacts_are_checked_without_reading_contents(state, tmp_path, name):
    path = {
        "journal": state.journal_path("read"),
        "sidecar": state.journal_path("read").with_suffix(".sqlite3-wal"),
        "capture": state.capture_path("read"),
    }[name]
    target = tmp_path / "not-an-audit-file"
    target.write_bytes(b"synthetic")
    target.chmod(0o600)
    path.symlink_to(target)
    assert_failure("internal_state_invalid", lambda: lab.load_state(state.root))


@pytest.mark.parametrize("service", ["read", "reset"])
@pytest.mark.parametrize("user", ["user-a", "user-b", "disabled", "unmapped"])
async def test_issue_fixed_claims_verified_by_native_provider(state, service, user, capsys):
    output = state.root / "tokens" / f"{service}-{user}.jwt"
    summary = lab.issue_token(state.root, user=user, service=service, output=output)
    encoded = output.read_text().strip()
    assert summary["status"] == "issued"
    assert summary["mode"] == "internal-simulated"
    assert summary["ttl_seconds"] == 300
    assert encoded not in json.dumps(summary)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    profile = state.identity_profile(service)
    scope = SCOPE if service == "read" else RESET_SCOPE
    verifier = JWTVerifier(
        public_key=state.public_key,
        issuer=profile.issuer,
        audience=profile.audience,
        required_scopes=[scope],
    )
    token = await verifier.verify_token(encoded)
    assert token is not None
    assert token.subject == user
    assert token.client_id == state.client_id(service)
    assert token.scopes == [scope]
    assert set(token.claims) == {"sub", "iss", "iat", "exp", "aud", "scope", "azp"}
    assert token.claims["exp"] - token.claims["iat"] == 300
    assert token.claims["aud"] == profile.audience
    other = "read" if service == "reset" else "reset"
    wrong = JWTVerifier(
        public_key=state.public_key,
        issuer=state.issuer,
        audience=state.identity_profile(other).audience,
    )
    assert await wrong.verify_token(encoded) is None
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("user", ["disabled", "unmapped"])
async def test_denial_subject_tokens_do_not_gain_policy_or_credential_access(state, user):
    from biz_t100.access import FileServiceAccountAccess

    with pytest.raises(BusinessError, match="^identity_not_enabled$"):
        await FileServiceAccountAccess(
            state.policy_path("read"), state.credential_path("read")
        ).authorize(Caller(issuer=state.issuer, subject=user, client_id=state.client_id("read")))


@pytest.mark.parametrize(
    "options",
    [
        {"user": "someone-else"},
        {"service": "admin"},
        {"ttl_seconds": 0},
        {"ttl_seconds": 3601},
        {"ttl_seconds": True},
        {"ttl_seconds": "300"},
    ],
)
def test_issue_rejects_unapproved_inputs_without_output(state, options):
    output = state.root / "tokens" / "rejected.jwt"
    arguments = {"user": "user-a", "service": "read", "output": output, **options}
    assert_failure("internal_token_invalid", lambda: lab.issue_token(state.root, **arguments))
    assert not output.exists()


@pytest.mark.parametrize(
    "relative",
    [
        "state.json",
        "issuer-private.pem",
        "issuer-public.pem",
        "read/access.json",
        "read/service.key",
        "read/audit/query.sqlite3",
        "read/upstream.jsonl",
        "new.jwt",
    ],
)
def test_output_cannot_be_a_reserved_or_non_token_state_path(state, relative):
    output = state.root / relative
    before = output.read_bytes() if output.exists() else None
    assert_failure(
        "internal_token_invalid",
        lambda: lab.issue_token(state.root, user="user-a", service="read", output=output),
    )
    assert (output.read_bytes() if output.exists() else None) == before


def test_output_is_exclusive_and_does_not_overwrite_existing_token(state):
    output = state.root / "tokens" / "issued.jwt"
    arguments = dict(user="user-a", service="read", output=output)
    lab.issue_token(state.root, **arguments)
    before = output.read_bytes()
    assert_failure("internal_token_invalid", lambda: lab.issue_token(state.root, **arguments))
    assert output.read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "ancestor"])
def test_token_aliases_are_rejected_without_overwriting_target(state, tmp_path, kind):
    target = tmp_path / "untouched"
    target.write_bytes(b"do-not-overwrite")
    target.chmod(0o600)
    output = state.root / "tokens" / "alias.jwt"
    if kind == "symlink":
        output.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, output)
    else:
        alias = tmp_path / "alias-directory"
        alias.symlink_to(state.root / "tokens", target_is_directory=True)
        output = alias / "new.jwt"
    assert_failure(
        "internal_token_invalid",
        lambda: lab.issue_token(state.root, user="user-a", service="read", output=output),
    )
    assert target.read_bytes() == b"do-not-overwrite"


def test_external_output_directory_must_be_private(state, tmp_path):
    directory = tmp_path / "external"
    directory.mkdir(mode=0o755)
    output = directory / "token.jwt"
    assert_failure(
        "internal_token_invalid",
        lambda: lab.issue_token(state.root, user="user-a", service="read", output=output),
    )
    assert not output.exists()
    directory.chmod(0o700)
    assert (
        lab.issue_token(state.root, user="user-a", service="read", output=output)["status"]
        == "issued"
    )


def test_signing_key_must_match_persisted_public_key(state):
    different = RSAKeyPair.generate()
    (state.root / "issuer-private.pem").write_text(different.private_key.get_secret_value())
    output = state.root / "tokens" / "mismatched.jwt"
    assert lab.load_state(state.root).public_key == state.public_key
    assert_failure(
        "internal_token_invalid",
        lambda: lab.issue_token(state.root, user="user-a", service="read", output=output),
    )
    assert not output.exists()


def test_public_file_rejects_trailing_private_material_even_with_updated_digest(state):
    extra = RSAKeyPair.generate().private_key.get_secret_value().encode("ascii")
    combined = state.publickey_file.read_bytes() + extra
    state.publickey_file.write_bytes(combined)
    replace_json(
        state.root / "state.json",
        lambda value: value.update(public_key_sha256=hashlib.sha256(combined).hexdigest()),
    )
    assert_failure("internal_state_invalid", lambda: lab.load_state(state.root))
