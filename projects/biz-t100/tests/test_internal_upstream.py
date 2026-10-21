"""开发者模拟器在不做真实上游 I/O 的情况下捕获客户端实际报文。"""

import hashlib
import importlib.util
import json
import os
import secrets
import stat
from pathlib import Path

import anyio
import httpx
import pytest
from pydantic import SecretStr

from biz_t100.client import T100Config, T100EmployeeClient
from biz_t100.models import BusinessError, EmployeeGrant, PasswordResetGrant
from biz_t100.reset_client import T100PasswordResetClient

_SPEC = importlib.util.spec_from_file_location(
    "internal_upstream_under_test",
    Path(__file__).parents[1] / "tests" / "legacy_support" / "internal_upstream.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
SyntheticUpstream = _MODULE.SyntheticUpstream


@pytest.fixture
def setup(tmp_path):
    key = secrets.token_urlsafe(32)
    digest = hashlib.sha256(key.encode()).hexdigest()

    def build(service="read", *, scenario="success", log=None, read_fixture=None):
        log = log or tmp_path / service / "upstream.jsonl"
        upstream = SyntheticUpstream(
            service,
            enterprise_id=_MODULE.ENTERPRISES[service],
            credential_sha256=digest,
            request_log=log,
            scenario=scenario,
            read_fixture=read_fixture,
        )
        config = T100Config(
            url=_MODULE.URLS[service],
            enterprise_id=_MODULE.ENTERPRISES[service],
            host_product="SYNTHETIC",
            host_ip="127.0.0.1",
            host_account="synthetic-service",
            service_ip="127.0.0.1",
            service_id="synthetic-service",
        )
        client = (T100EmployeeClient if service == "read" else T100PasswordResetClient)(
            config, transport=upstream.transport
        )
        return upstream, client, log

    return build, key, digest


def grant(key, employee="E001", service="read"):
    fields = dict(
        employee_id=employee, binding_version="synthetic-policy", credential=SecretStr(key)
    )
    if service == "reset":
        return PasswordResetGrant(
            account_id={"E001": "LOGIN-A", "E002": "LOGIN-B"}[employee], **fields
        )
    return EmployeeGrant(**fields)


async def invoke(client, key, service="read", employee="E001"):
    value = grant(key, employee, service)
    return await (client.read(value) if service == "read" else client.reset(value))


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def request_body(key, service="read"):
    parameter = (
        {"employee_no": "E001", "is_digiwin_product": "N", "use_has_next": False}
        if service == "read"
        else {"master": [{"gzxa001": "LOGIN-A", "gzxa003": "E001", "password_reset": "Y"}]}
    )
    return {
        "key": key,
        "type": "sync",
        "host": {"prod": "SYNTHETIC"},
        "service": {"prod": "T100", "name": _MODULE._SERVICES[service]},
        "datakey": {"EntId": _MODULE.ENTERPRISES[service]},
        "payload": {"std_data": {"parameter": parameter}},
    }


@pytest.mark.parametrize("service", ["read", "reset"])
async def test_real_clients_capture_exact_two_synthetic_targets_and_safe_fields(setup, service):
    build, key, _ = setup
    upstream, client, path = build(service)
    assert isinstance(upstream.transport, httpx.MockTransport)
    assert not path.parent.exists()  # 构造函数不读取文件，也不创建任何内容。
    for employee in ("E001", "E002"):
        result = await invoke(client, key, service, employee)
        if service == "read":
            assert result.employee_id == employee
        else:
            assert result.acknowledged is True
    captured = rows(path)
    assert [row["counter"] for row in captured] == [1, 2]
    assert [row["employee_id"] for row in captured] == ["E001", "E002"]
    assert [row["account_id"] for row in captured] == (
        [None, None] if service == "read" else ["LOGIN-A", "LOGIN-B"]
    )
    assert all(set(row) == _MODULE._FIELDS for row in captured)
    assert all(row["enterprise_id"] == _MODULE.ENTERPRISES[service] for row in captured)
    assert all(row["service"] == service and row["scenario"] == "success" for row in captured)
    assert key not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "service,scenario,code",
    [
        ("read", "unauthenticated", "upstream_unauthenticated"),
        ("reset", "unauthenticated", "upstream_unauthenticated"),
        ("read", "timeout", "upstream_timeout"),
        ("reset", "timeout", "upstream_timeout"),
        ("read", "mismatch", "upstream_employee_mismatch"),
    ],
)
async def test_controlled_faults_are_captured_once_without_retries(setup, service, scenario, code):
    build, key, _ = setup
    _, client, path = build(service, scenario=scenario)
    with pytest.raises(BusinessError, match="^" + code + "$"):
        await invoke(client, key, service)
    assert len(rows(path)) == 1
    assert rows(path)[0]["scenario"] == scenario
    assert rows(path)[0]["employee_id"] == "E001"  # 捕获的是请求，而非不匹配时的响应。
    assert key not in path.read_text()


@pytest.mark.parametrize("service", ["read", "reset"])
async def test_wrong_service_key_is_401_and_never_logged(setup, service):
    build, key, _ = setup
    upstream, _, path = build(service)
    wrong = "synthetic-wrong-key"
    async with httpx.AsyncClient(transport=upstream.transport, trust_env=False) as client:
        response = await client.post(_MODULE.URLS[service], json=request_body(wrong, service))
    assert response.status_code == 401
    assert len(rows(path)) == 1
    assert key not in path.read_text() and wrong not in path.read_text()


@pytest.mark.parametrize("fault", ["url", "method", "service", "enterprise", "employee", "extra"])
async def test_read_contract_rejects_overrides_without_echoing_input(setup, fault):
    build, key, _ = setup
    upstream, _, path = build()
    body = request_body(key)
    url, method = _MODULE.URLS["read"], "POST"
    if fault == "url":
        url = "https://outside-synthetic.example.invalid/api"
    elif fault == "method":
        method = "PUT"
    elif fault == "service":
        body["service"]["name"] = "outside-synthetic-service"
    elif fault == "enterprise":
        body["datakey"]["EntId"] = "OUTSIDE-SYNTHETIC"
    elif fault == "employee":
        body["payload"]["std_data"]["parameter"]["employee_no"] = "OUTSIDE-SYNTHETIC"
    else:
        body["payload"]["std_data"]["parameter"]["scenario"] = "success"
    async with httpx.AsyncClient(transport=upstream.transport, trust_env=False) as client:
        response = await client.request(method, url, json=body)
    assert response.status_code == 400
    assert response.json() == {"error": "synthetic_request_rejected"}
    assert len(rows(path)) == 1
    assert "OUTSIDE-SYNTHETIC" not in path.read_text()
    assert key not in path.read_text()
    if fault == "employee":
        assert rows(path)[0]["employee_id"] is None


@pytest.mark.parametrize("fault", ["status", "account", "employee", "reset_flag", "master_count"])
async def test_reset_accepts_only_the_three_field_matching_target_patch(setup, fault):
    build, key, _ = setup
    upstream, _, path = build("reset")
    body = request_body(key, "reset")
    master = body["payload"]["std_data"]["parameter"]["master"]
    if fault == "status":
        master[0]["gzxastus"] = "Y"
    elif fault == "account":
        master[0]["gzxa001"] = "LOGIN-B"
    elif fault == "employee":
        master[0]["gzxa003"] = "E003"
    elif fault == "reset_flag":
        master[0]["password_reset"] = "N"
    else:
        master.append(dict(master[0]))
    async with httpx.AsyncClient(transport=upstream.transport, trust_env=False) as client:
        response = await client.post(_MODULE.URLS["reset"], json=body)
    assert response.status_code == 400
    assert len(rows(path)) == 1
    assert "E003" not in path.read_text()


async def test_capture_counter_survives_reconstruction_and_concurrent_instances(setup):
    build, key, _ = setup
    _, first, path = build()
    await invoke(first, key)
    _, second, _ = build(log=path)
    await invoke(second, key)

    async def request(index):
        await invoke(first if index % 2 else second, key, employee="E001" if index % 2 else "E002")

    async with anyio.create_task_group() as group:
        for index in range(12):
            group.start_soon(request, index)
    captured = rows(path)
    assert [row["counter"] for row in captured] == list(range(1, 15))


@pytest.mark.parametrize(
    "kind", ["file_symlink", "parent_symlink", "hardlink", "file_mode", "directory_mode"]
)
async def test_unsafe_capture_path_fails_without_simulated_success(setup, tmp_path, kind):
    build, key, _ = setup
    _, client, path = build()
    await invoke(client, key)
    before = path.read_bytes()
    if kind == "file_symlink":
        alias = path.with_name("alias.jsonl")
        alias.symlink_to(path)
        _, client, _ = build(log=alias)
    elif kind == "parent_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(path.parent, target_is_directory=True)
        _, client, _ = build(log=alias / path.name)
    elif kind == "hardlink":
        os.link(path, path.with_name("another.jsonl"))
    elif kind == "file_mode":
        path.chmod(0o644)
    else:
        path.parent.chmod(0o755)
    with pytest.raises(BusinessError, match="^upstream_unavailable$"):
        await invoke(client, key)
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["foreign", "partial", "cross_service"])
async def test_invalid_or_other_service_capture_is_not_appended(setup, kind):
    build, key, _ = setup
    _, client, path = build()
    await invoke(client, key)
    if kind == "foreign":
        path.write_text('{"private":"must-not-escape"}\n')
    elif kind == "partial":
        path.write_bytes(path.read_bytes()[:-1])
    else:
        _, client, _ = build("reset", log=path)
    before = path.read_bytes()
    with pytest.raises(BusinessError, match="^upstream_unavailable$"):
        await invoke(client, key, "reset" if kind == "cross_service" else "read")
    assert path.read_bytes() == before


async def test_capture_write_failure_is_fixed_and_carries_no_driver_context(setup, monkeypatch):
    build, key, _ = setup
    upstream, _, _ = build()

    def fail(*args, **kwargs):
        raise OSError("private-path and synthetic-key must not be exposed")

    monkeypatch.setattr(_MODULE.os, "write", fail)
    with pytest.raises(httpx.RequestError, match="^synthetic_capture_unavailable$") as error:
        await upstream._handle(httpx.Request("POST", _MODULE.URLS["read"], json=request_body(key)))
    assert error.value.__context__ is None
    assert key not in str(error.value)


async def test_capture_sync_failure_prevents_reset_acknowledgement(setup, monkeypatch):
    build, key, _ = setup
    _, client, _ = build("reset")

    def fail(*args, **kwargs):
        raise OSError("private filesystem failure")

    monkeypatch.setattr(_MODULE.os, "fsync", fail)
    with pytest.raises(BusinessError, match="^upstream_unavailable$"):
        await invoke(client, key, "reset")


@pytest.mark.parametrize(
    "service,scenario", [("other", "success"), ("reset", "mismatch"), ("read", "arbitrary")]
)
def test_invalid_operator_scenario_or_service_is_rejected_without_io(tmp_path, service, scenario):
    path = tmp_path / "missing" / "upstream.jsonl"
    with pytest.raises(ValueError, match="^invalid_synthetic_configuration$"):
        SyntheticUpstream(
            service,
            enterprise_id=_MODULE.ENTERPRISES.get(service, "INTERNAL-READ"),
            credential_sha256="0" * 64,
            request_log=path,
            scenario=scenario,
        )
    assert not path.parent.exists()


def test_constructor_never_reads_original_service_key_or_touches_files(tmp_path, monkeypatch):
    def no_io(*args, **kwargs):
        pytest.fail("construction must not access files")

    monkeypatch.setattr(_MODULE.os, "open", no_io)
    upstream = SyntheticUpstream(
        "read",
        enterprise_id="INTERNAL-READ",
        credential_sha256="0" * 64,
        request_log=tmp_path / "upstream.jsonl",
    )
    assert isinstance(upstream.transport, httpx.MockTransport)


async def test_read_fixture_uses_exact_target_and_display_name_without_remote_io(setup):
    build, key, _ = setup
    upstream, client, path = build(read_fixture=("LAB-901", "合成测试用户"))
    assert isinstance(upstream.transport, httpx.MockTransport)
    assert not path.parent.exists()
    result = await invoke(client, key, employee="LAB-901")
    assert result.employee_id == "LAB-901"
    assert result.display_name == "合成测试用户"
    captured = rows(path)
    assert len(captured) == 1
    assert captured[0]["employee_id"] == "LAB-901"
    assert captured[0]["enterprise_id"] == "INTERNAL-READ"
    assert captured[0]["account_id"] is None
    assert "合成测试用户" not in path.read_text()
    assert key not in path.read_text()


@pytest.mark.parametrize("employee", ["E001", "E002", "LAB-902", "lab-901", "LAB-901 "])
async def test_read_fixture_rejects_every_other_target_without_echoing_it(setup, employee):
    build, key, _ = setup
    upstream, _, path = build(read_fixture=("LAB-901", "Synthetic Fixture"))
    body = request_body(key)
    body["payload"]["std_data"]["parameter"]["employee_no"] = employee
    async with httpx.AsyncClient(transport=upstream.transport, trust_env=False) as client:
        response = await client.post(_MODULE.URLS["read"], json=body)
    assert response.status_code == 400
    assert response.json() == {"error": "synthetic_request_rejected"}
    assert len(rows(path)) == 1
    assert rows(path)[0]["employee_id"] is None
    assert employee not in path.read_text()


@pytest.mark.parametrize("employee", ["LAB-901", "E001", "E002"])
async def test_fixture_mismatch_always_returns_a_different_employee_and_is_rejected(
    setup, employee
):
    build, key, _ = setup
    _, client, path = build(scenario="mismatch", read_fixture=(employee, "Synthetic Fixture"))
    with pytest.raises(BusinessError, match="^upstream_employee_mismatch$"):
        await invoke(client, key, employee=employee)
    assert len(rows(path)) == 1
    assert rows(path)[0]["employee_id"] == employee


@pytest.mark.parametrize(
    "before_fixture,after_fixture,before_employee,after_employee",
    [
        (None, ("LAB-901", "Synthetic One"), "E001", "LAB-901"),
        (("LAB-901", "Synthetic One"), ("LAB-902", "Synthetic Two"), "LAB-901", "LAB-902"),
        (("LAB-901", "Synthetic One"), None, "LAB-901", "E001"),
    ],
)
async def test_capture_tail_of_another_fixture_is_not_reused(
    setup, before_fixture, after_fixture, before_employee, after_employee
):
    build, key, _ = setup
    _, before_client, path = build(read_fixture=before_fixture)
    await invoke(before_client, key, employee=before_employee)
    before = path.read_bytes()
    _, after_client, _ = build(read_fixture=after_fixture, log=path)
    with pytest.raises(BusinessError, match="^upstream_unavailable$"):
        await invoke(after_client, key, employee=after_employee)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "service,fixture",
    [
        ("reset", ("LAB-901", "Synthetic Fixture")),
        ("read", ["LAB-901", "Synthetic Fixture"]),
        ("read", ("LAB-901",)),
        ("read", ("LAB-901", "Synthetic Fixture", "extra")),
        ("read", (901, "Synthetic Fixture")),
        ("read", ("", "Synthetic Fixture")),
        ("read", ("LAB 901", "Synthetic Fixture")),
        ("read", ("X" * 129, "Synthetic Fixture")),
        ("read", ("LAB-901", None)),
        ("read", ("LAB-901", "")),
        ("read", ("LAB-901", "   ")),
        ("read", ("LAB-901", "X" * 257)),
        ("read", ("LAB-901", "Synthetic\nFixture")),
        ("read", ("LAB-901", "Synthetic\x7fFixture")),
        ("read", ("LAB-901", "Synthetic\u0085Fixture")),
        ("read", ("LAB-901", "Synthetic\u202eFixture")),
    ],
)
def test_invalid_read_fixture_is_rejected_before_io(setup, service, fixture):
    build, _, _ = setup
    with pytest.raises(ValueError, match="^invalid_synthetic_configuration$"):
        build(service, read_fixture=fixture)
