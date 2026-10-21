"""Run BIZ-T100 offline contracts with the current project Python interpreter."""

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
_INTERFACE_PATTERNS = (
    "test_interfaces*.py",
    "test_function*.py",
    "test_live_cli.py",
    "test_remote_live_cli.py",
    "test_service_runtime.py",
    "test_gateway_token.py",
    "test_status_retry.py",
    "test_live_status_retry.py",
    "test_status_continuation.py",
    "test_live_status_continuation.py",
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="离线验证：合成身份和模拟上游；不执行真实业务操作。",
        epilog="示例：.venv/bin/python scripts/offline.py --suite interfaces -- -q",
    )
    parser.add_argument("--suite", choices=("all", "interfaces"), default="all")
    args, pytest_args = parser.parse_known_args(argv)
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    if args.suite == "all":
        targets = ["tests"]
    else:
        targets = sorted(
            {
                str(path.relative_to(PROJECT))
                for pattern in _INTERFACE_PATTERNS
                for path in (PROJECT / "tests").glob(pattern)
            }
        )
        if not targets:
            parser.error("四接口验收测试尚未就绪；未执行任何业务操作。")
    return subprocess.run(
        [sys.executable, "-m", "pytest", *targets, *pytest_args], cwd=PROJECT, check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
