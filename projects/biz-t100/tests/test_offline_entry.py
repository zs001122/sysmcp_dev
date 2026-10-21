"""The single offline entry selects contracts without importing runtime configuration."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def entry(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "offline_entry_under_test", PROJECT / "scripts" / "offline.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PROJECT", tmp_path)
    (tmp_path / "tests").mkdir()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(subprocess, "run", run)
    return module, calls, tmp_path


def test_all_defaults_to_full_suite_and_preserves_test_exit_status(entry):
    module, calls, project = entry
    assert module.main(["--", "-q", "--collect-only"]) == 7
    assert calls == [
        (
            [sys.executable, "-m", "pytest", "tests", "-q", "--collect-only"],
            {"cwd": project, "check": False},
        )
    ]


def test_interface_suite_only_selects_declared_contract_files(entry):
    module, calls, project = entry
    for name in (
        "test_interfaces_client.py",
        "test_function_service.py",
        "test_live_cli.py",
        "test_live_write.py",
    ):
        (project / "tests" / name).write_text("")
    assert module.main(["--suite", "interfaces", "-q"]) == 7
    assert calls[0][0] == [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_function_service.py",
        "tests/test_interfaces_client.py",
        "tests/test_live_cli.py",
        "-q",
    ]


def test_missing_interface_contracts_do_not_silently_run_a_different_suite(entry, capsys):
    module, calls, _ = entry
    with pytest.raises(SystemExit) as failure:
        module.main(["--suite", "interfaces"])
    assert failure.value.code == 2
    assert calls == []
    assert "尚未就绪" in capsys.readouterr().err
