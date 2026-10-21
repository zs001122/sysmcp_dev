"""本地服务账号授权测试，仅使用合成的策略与凭据文件。"""

import hashlib
import json
import os

import pytest

import biz_t100.access as access_module
from biz_t100.access import FileServiceAccountAccess
from biz_t100.models import BusinessError, Caller

ISSUER = "https://identity.example.invalid"
SECRET = "synthetic-service-account-secret"
CALLER = Caller(issuer=ISSUER, subject="employee-a", client_id="agent-a")


def principal(subject="employee-a", employee_id="E001", **overrides):
    return {
        "issuer": ISSUER,
        "subject": subject,
        "employee_id": employee_id,
        "allowed_clients": ["agent-a"],
        "enabled": True,
        "allow_employee_read": True,
        **overrides,
    }


def write_private(path, content):
    path.write_bytes(content)
    path.chmod(0o600)


def write_policy(path, policy):
    write_private(path, json.dumps(policy).encode())


@pytest.fixture
def files(tmp_path):
    policy = {
        "auth_mode": "service-account",
        "revision": "synthetic-v1",
        "principals": [principal()],
    }
    policy_path, credential_path = tmp_path / "policy.json", tmp_path / "credential.txt"
    write_policy(policy_path, policy)
    write_private(credential_path, SECRET.encode())
    return policy_path, credential_path, policy


def access(files):
    return FileServiceAccountAccess(policy_file=files[0], credential_file=files[1])


async def rejected(provider, code, caller=CALLER):
    with pytest.raises(BusinessError, match=f"^{code}$") as failure:
        await provider.authorize(caller)
    assert failure.value.code == code
    assert SECRET not in str(failure.value)
    assert SECRET not in repr(failure.value)


def test_construction_does_not_read_or_create_files(tmp_path):
    policy, key = tmp_path / "absent-policy", tmp_path / "absent-credential"
    provider = FileServiceAccountAccess(policy_file=policy, credential_file=key)
    assert provider is not None
    assert list(tmp_path.iterdir()) == []


async def test_explicit_authorization_hides_credential_and_hashes_policy(files):
    grant = await access(files).authorize(CALLER)
    assert grant.employee_id == "E001"
    assert grant.binding_version == hashlib.sha256(files[0].read_bytes()).hexdigest()
    assert grant.credential.get_secret_value() == SECRET
    assert SECRET not in repr(grant)
    assert SECRET not in str(grant)
    assert SECRET not in grant.model_dump_json()
    assert "credential" not in grant.model_dump()


@pytest.mark.parametrize("mode", [None, "", "delegated", "user", "SERVICE-ACCOUNT", True])
async def test_mode_cannot_be_omitted_or_downgraded(files, mode):
    policy_path, _, policy = files
    if mode is None:
        del policy["auth_mode"]
    else:
        policy["auth_mode"] = mode
    write_policy(policy_path, policy)
    await rejected(access(files), "policy_unavailable")


@pytest.mark.parametrize("revision", [None, "", "   ", 1])
async def test_revision_must_be_explicit_nonblank_text(files, revision):
    policy_path, _, policy = files
    if revision is None:
        del policy["revision"]
    else:
        policy["revision"] = revision
    write_policy(policy_path, policy)
    await rejected(access(files), "policy_unavailable")


@pytest.mark.parametrize(
    ("field", "code"),
    [("enabled", "identity_not_enabled"), ("allow_employee_read", "permission_denied")],
)
@pytest.mark.parametrize("missing", [False, True])
async def test_no_permission_is_granted_by_default(files, field, code, missing):
    policy_path, credential_path, policy = files
    if missing:
        del policy["principals"][0][field]
    else:
        policy["principals"][0][field] = False
    write_policy(policy_path, policy)
    credential_path.unlink()
    await rejected(access(files), code)


@pytest.mark.parametrize("field", ["enabled", "allow_employee_read", "allow_password_reset"])
@pytest.mark.parametrize("value", ["true", 1, None])
async def test_permission_flags_reject_coercion(files, field, value):
    files[2]["principals"][0][field] = value
    write_policy(files[0], files[2])
    await rejected(access(files), "policy_unavailable")


@pytest.mark.parametrize(
    ("caller", "code"),
    [
        (Caller(issuer=ISSUER, subject="employee-b", client_id="agent-a"), "identity_not_enabled"),
        (
            Caller(
                issuer="https://other.example.invalid", subject="employee-a", client_id="agent-a"
            ),
            "identity_not_enabled",
        ),
        (Caller(issuer=ISSUER, subject="employee-a", client_id="other-agent"), "permission_denied"),
    ],
)
async def test_issuer_subject_and_client_isolation(files, caller, code):
    await rejected(access(files), code, caller)


async def test_same_service_credential_never_merges_employee_targets(files):
    files[2]["principals"].append(principal("employee-b", "E002"))
    write_policy(files[0], files[2])
    provider = access(files)
    a = await provider.authorize(CALLER)
    b = await provider.authorize(Caller(issuer=ISSUER, subject="employee-b", client_id="agent-a"))
    assert (a.employee_id, b.employee_id) == ("E001", "E002")
    assert a.credential.get_secret_value() == b.credential.get_secret_value() == SECRET


async def test_same_subject_under_another_issuer_is_a_distinct_principal(files):
    other = "https://other.example.invalid"
    files[2]["principals"].append(principal(employee_id="E002", issuer=other))
    write_policy(files[0], files[2])
    grant = await access(files).authorize(
        Caller(issuer=other, subject="employee-a", client_id="agent-a")
    )
    assert grant.employee_id == "E002"


async def test_duplicate_principal_is_rejected_instead_of_first_match(files):
    files[2]["principals"].append(principal(employee_id="E002"))
    write_policy(files[0], files[2])
    await rejected(access(files), "policy_unavailable")


async def test_mapping_hot_reload_and_raw_policy_version(files):
    provider = access(files)
    before = await provider.authorize(CALLER)
    files[2]["principals"][0]["employee_id"] = "E002"
    write_policy(files[0], files[2])  # 仅修订标签相同绝不能沿用缓存的旧映射。
    after = await provider.authorize(CALLER)
    assert after.employee_id == "E002"
    assert after.binding_version != before.binding_version
    assert after.binding_version == hashlib.sha256(files[0].read_bytes()).hexdigest()
    write_private(files[0], files[0].read_bytes() + b"\n")
    whitespace_change = await provider.authorize(CALLER)
    assert whitespace_change.binding_version != after.binding_version


@pytest.mark.parametrize("change", ["disable", "deny-tool", "deny-client", "remove"])
async def test_revocation_is_seen_on_next_authorization(files, change):
    provider = access(files)
    await provider.authorize(CALLER)
    policy = files[2]
    if change == "disable":
        policy["principals"][0]["enabled"] = False
    elif change == "deny-tool":
        policy["principals"][0]["allow_employee_read"] = False
    elif change == "deny-client":
        policy["principals"][0]["allowed_clients"] = ["other-agent"]
    else:
        policy["principals"] = []
    write_policy(files[0], policy)
    code = "identity_not_enabled" if change in {"disable", "remove"} else "permission_denied"
    await rejected(provider, code)


async def test_credential_rotation_is_seen_without_reconstruction(files):
    provider = access(files)
    before = await provider.authorize(CALLER)
    write_private(files[1], b"synthetic-rotated-secret")
    after = await provider.authorize(CALLER)
    assert before.credential.get_secret_value() == SECRET
    assert after.credential.get_secret_value() == "synthetic-rotated-secret"


@pytest.mark.parametrize("which", [0, 1], ids=["policy", "credential"])
@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "symlink",
        "directory",
        "hardlink",
        "fifo",
        "group-read",
        "group-write",
        "other-read",
    ],
)
async def test_private_file_requirements_fail_closed(files, tmp_path, which, kind):
    path = files[which]
    if kind == "missing":
        path.unlink()
    elif kind == "symlink":
        target = tmp_path / f"target-{which}"
        path.rename(target)
        path.symlink_to(target)
    elif kind == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    elif kind == "hardlink":
        os.link(path, tmp_path / f"linked-{which}")
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, mode=0o600)
    else:
        path.chmod({"group-read": 0o640, "group-write": 0o620, "other-read": 0o604}[kind])
    await rejected(access(files), "policy_unavailable" if which == 0 else "credential_unavailable")


@pytest.mark.parametrize("which", [0, 1], ids=["policy", "credential"])
async def test_private_file_size_is_bounded(files, which):
    if which == 0:
        content = files[0].read_bytes().ljust(1_000_000, b" ")
    else:
        content = b"x" * 16_384
    write_private(files[which], content)
    await access(files).authorize(CALLER)
    write_private(files[which], content + b" ")
    await rejected(access(files), "policy_unavailable" if which == 0 else "credential_unavailable")


@pytest.mark.parametrize("content", [b"", b" \n\t", b"\xff", b"{", b"[]", b"null"])
async def test_invalid_policy_is_not_accepted(files, content):
    write_private(files[0], content)
    await rejected(access(files), "policy_unavailable")


@pytest.mark.parametrize("content", [b"", b" \n\t", b"\xff"])
async def test_empty_or_non_utf8_credential_is_not_accepted(files, content):
    write_private(files[1], content)
    await rejected(access(files), "credential_unavailable")


@pytest.mark.parametrize("which", [0, 1], ids=["policy", "credential"])
async def test_wrong_file_owner_is_rejected(files, which, monkeypatch):
    expected = files[which].stat()
    original = access_module.os.fstat

    def wrong_owner(descriptor):
        result = original(descriptor)
        if (result.st_dev, result.st_ino) == (expected.st_dev, expected.st_ino):
            values = list(result)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(access_module.os, "fstat", wrong_owner)
    await rejected(access(files), "policy_unavailable" if which == 0 else "credential_unavailable")


@pytest.mark.parametrize("which", [0, 1], ids=["policy", "credential"])
async def test_symlink_ancestor_is_rejected(files, tmp_path, which):
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    paths = list(files[:2])
    paths[which] = alias / paths[which].name
    provider = FileServiceAccountAccess(policy_file=paths[0], credential_file=paths[1])
    await rejected(provider, "policy_unavailable" if which == 0 else "credential_unavailable")


async def test_owner_read_only_files_are_accepted(files):
    for path in files[:2]:
        path.chmod(0o400)
    assert (await access(files).authorize(CALLER)).employee_id == "E001"


@pytest.mark.parametrize("reason", ["missing", "disabled", "client", "tool", "invalid-policy"])
async def test_denial_does_not_read_credential(files, reason, monkeypatch):
    policy = files[2]
    code = "identity_not_enabled"
    if reason == "missing":
        policy["principals"] = []
    elif reason == "disabled":
        policy["principals"][0]["enabled"] = False
    elif reason == "client":
        policy["principals"][0]["allowed_clients"] = ["other-agent"]
        code = "permission_denied"
    elif reason == "tool":
        policy["principals"][0]["allow_employee_read"] = False
        code = "permission_denied"
    else:
        policy["auth_mode"] = "delegated"
        code = "policy_unavailable"
    write_policy(files[0], policy)
    original = access_module._private_bytes
    reads = []

    def tracking_read(path, maximum):
        reads.append(path)
        assert path != files[1], "denied caller must never read a service credential"
        return original(path, maximum)

    monkeypatch.setattr(access_module, "_private_bytes", tracking_read)
    await rejected(access(files), code)
    assert reads == [files[0]]


@pytest.mark.parametrize("kind", ["relative-policy", "relative-credential", "same-file"])
def test_constructor_requires_separate_absolute_paths(files, kind):
    policy, credential = files[:2]
    if kind == "relative-policy":
        policy = policy.name
    elif kind == "relative-credential":
        credential = credential.name
    else:
        credential = policy
    with pytest.raises(ValueError):
        FileServiceAccountAccess(policy_file=policy, credential_file=credential)
