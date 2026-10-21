"""运维方选择的目标不能与调用者提供的身份混用。"""

import argparse
import importlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host(monkeypatch):
    monkeypatch.syspath_prepend(str(_PROJECT / "tests" / "legacy_support"))
    return importlib.import_module("live_read")


@pytest.mark.parametrize("extra", [["--user", "unmapped"], ["--token-file", "/tmp/synthetic.jwt"]])
def test_operator_target_cannot_override_another_identity(host, extra):
    with pytest.raises(SystemExit) as error:
        host.parser().parse_args(["call", "--employee-no", "E001", *extra])
    assert error.value.code == 2


@pytest.mark.parametrize("employee", ["", "E*", " E001", "E001/OTHER", "E" * 129])
def test_invalid_target_is_rejected_before_dispatch(host, employee):
    with pytest.raises(SystemExit) as error:
        host.parser().parse_args(["call", "--employee-no", employee])
    assert error.value.code == 2


async def test_operator_case_uses_independent_path_not_shared_listener(host, monkeypatch):
    helper = importlib.import_module("live_employee_call")
    selected = AsyncMock(return_value={"status": "passed", "requested_employee_no": "E001"})
    monkeypatch.setattr(helper, "call_employee", selected)
    listener = AsyncMock(side_effect=AssertionError("operator case must not query default mapping"))
    monkeypatch.setattr(host, "service_status", listener)
    state = object()
    result = await host._client_command(
        state, argparse.Namespace(command="call", employee_no="E001")
    )
    selected.assert_awaited_once_with(state, "E001")
    listener.assert_not_awaited()
    assert result["requested_employee_no"] == "E001"
