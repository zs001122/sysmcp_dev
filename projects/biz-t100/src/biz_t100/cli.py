"""Local preparation and explicit persistent FastMCP startup."""

import argparse
import json
from pathlib import Path

import anyio

from .journal import SQLiteQueryJournal
from .models import BusinessError
from .runtime import check_configuration, load_runtime_config
from .service_runtime import (
    check_service_configuration,
    create_service_runtime_app,
    issue_gateway_token,
    load_service_runtime_config,
)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="BIZ-T100 local checks and explicit service startup"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-config", help="Validate local configuration only")
    check.add_argument("--config", type=Path, required=True)
    pending = commands.add_parser("pending", help="Inspect an existing journal; never replay")
    pending.add_argument("--journal", type=Path, required=True)
    service_check = commands.add_parser(
        "check-service", help="Validate persistent-service files locally; no upstream calls"
    )
    service_check.add_argument("--config", type=Path, required=True)
    serve = commands.add_parser("serve", help="Start one authenticated T100 FastMCP service")
    serve.add_argument("--config", type=Path, required=True)
    gateway_token = commands.add_parser(
        "issue-gateway-token",
        help="Issue a private temporary token for approved query/account actions",
    )
    gateway_token.add_argument("--config", type=Path, required=True)
    gateway_token.add_argument("--signing-key", type=Path, required=True)
    gateway_token.add_argument("--subject", required=True)
    gateway_token.add_argument("--client-id", required=True)
    gateway_token.add_argument("--output", type=Path, required=True)
    gateway_token.add_argument("--lifetime-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    try:
        if args.command == "check-config":
            result = anyio.run(check_configuration, load_runtime_config(args.config))
            result = {
                **result,
                "scope": "local_configuration_only",
                "identity_provider_verified": False,
                "business_validated": False,
            }
        elif args.command == "issue-gateway-token":
            result = issue_gateway_token(
                load_service_runtime_config(args.config),
                signing_key=args.signing_key,
                subject=args.subject,
                client_id=args.client_id,
                output=args.output,
                lifetime_seconds=args.lifetime_seconds,
            )
        elif args.command in {"check-service", "serve"}:
            config = load_service_runtime_config(args.config)
            result = anyio.run(check_service_configuration, config)
            if args.command == "serve":
                import uvicorn

                app = create_service_runtime_app(config)
                # One worker preserves admission limits. SQLite coordinates shared
                # read/write audit and historical occupancy between deployment units.
                uvicorn.run(
                    app,
                    host=config.host,
                    port=config.port,
                    workers=1,
                    log_level="warning",
                    access_log=False,
                )
                return 0
        else:
            if not args.journal.is_absolute() or ".." in args.journal.parts:
                raise BusinessError("journal_unavailable")
            ids = anyio.run(SQLiteQueryJournal(args.journal).pending_request_ids)
            result = {"status": "inspection_complete", "pending_request_ids": ids}
    except (BusinessError, OSError, ValueError):
        # 解析器/模型/驱动异常可能包含运维方配置内容。
        print(json.dumps({"status": "failed", "error": "local_check_failed"}))
        return 1
    print(json.dumps(result))
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
