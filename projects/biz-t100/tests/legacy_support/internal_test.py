# Historical regression support relocated from scripts; not a supported command.
"""常驻的仅回环（loopback）内部实验环境。使用 BIZ-T100 项目解释器运行。

仅用于开发装配：默认所有业务目标、身份与凭据都是合成的。
独立的、经运维方批准的读用例会注入其私有状态加载器与模拟装置数据。
产品模块绝不导入这些辅助代码。
"""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
import socket
import stat
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from uuid import uuid4

import anyio
import httpx
import uvicorn
from fastmcp.server.auth.providers.jwt import JWTVerifier
from internal_support import init_state, issue_token, load_state
from internal_upstream import SyntheticUpstream
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from biz_t100 import create_app, create_reset_app
from biz_t100.access import FileServiceAccountAccess
from biz_t100.client import T100EmployeeClient
from biz_t100.journal import SQLiteQueryJournal
from biz_t100.models import BusinessError
from biz_t100.reset_client import T100PasswordResetClient
from biz_t100.reset_server import RESET_SCOPE
from biz_t100.reset_store import SQLiteResetStore
from biz_t100.server import SCOPE

MODE = "internal-simulated"
DEFAULT_STATE = Path(__file__).resolve().parents[2] / ".runtime" / "internal-test"
PORTS = {"read": 8766, "reset": 8767}


def _private_file(path, *, create=False):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > 16_384
        ):
            raise ValueError("invalid private file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _metadata_path(state, service):
    return state.policy_path(service).parent / "listener.json"


def _write_metadata(state, service, value):
    path = _metadata_path(state, service)
    if path.exists() or path.is_symlink():
        fd = _private_file(path)
        os.close(fd)
    temporary = path.with_name(".listener-" + str(uuid4()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_listener(state, service):
    """只读取运维方控制的本地元数据；它绝不可能指向远端 URL。"""
    try:
        fd = _private_file(_metadata_path(state, service))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r") as stream:
        result = json.load(stream)
    if (
        type(result) is not dict
        or result.get("mode") != getattr(state, "mode", MODE)
        or result.get("service") != service
        or type(result.get("port")) is not int
        or not 1024 <= result["port"] <= 65535
        or type(result.get("instance_id")) is not str
        or result.get("state") not in {"starting", "running", "stopped"}
    ):
        raise BusinessError("internal_listener_invalid")
    return result


async def service_status(state, service):
    record = read_listener(state, service)
    result = {"service": service, "mode": getattr(state, "mode", MODE), "status": "stopped"}
    if record is None or record["state"] == "stopped":
        return result
    url = f"http://127.0.0.1:{record['port']}"
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=2) as client:
            response = await client.get(url + "/health/ready")
        body = response.json()
        valid = (
            response.status_code == 200
            and body.get("mode") == getattr(state, "mode", MODE)
            and body.get("service") == service
            and body.get("instance_id") == record["instance_id"]
            and body.get("status") == "ready"
        )
        result.update(
            status="ready" if valid else "not_ready",
            url=url + "/mcp",
            pid=record.get("pid"),
        )
    except (httpx.HTTPError, ValueError, AttributeError):
        result["status"] = "unreachable"
    return result


def create_lab_app(
    state, service, *, scenario="success", instance_id=None, state_loader=load_state, capture=None
):
    """用捕获式的合成 HTTP 显式组合原生业务服务。"""
    profile = state.identity_profile(service)
    auth = JWTVerifier(
        public_key=state.public_key,
        issuer=profile.issuer,
        audience=profile.audience,
        required_scopes=[SCOPE if service == "read" else RESET_SCOPE],
    )
    capture = (
        capture
        if capture is not None
        else SyntheticUpstream(
            service,
            enterprise_id=state.t100_config(service).enterprise_id,
            credential_sha256=state.credential_digest(service),
            request_log=state.capture_path(service),
            scenario=scenario,
        )
    )
    access = FileServiceAccountAccess(
        state.policy_path(service),
        state.credential_path(service),
        permission="employee_read" if service == "read" else "password_reset",
    )
    if service == "read":
        journal = SQLiteQueryJournal(
            state.journal_path(service),
            enterprise_id=state.t100_config(service).enterprise_id,
        )
        native = create_app(
            auth=auth,
            identity=profile,
            access=access,
            journal=journal,
            reader=T100EmployeeClient(state.t100_config(service), transport=capture.transport),
        )
    elif service == "reset":
        journal = SQLiteResetStore(
            state.journal_path(service),
            enterprise_id=state.t100_config(service).enterprise_id,
        )
        native = create_reset_app(
            auth=auth,
            identity=profile,
            access=access,
            store=journal,
            resetter=T100PasswordResetClient(
                state.t100_config(service),
                transport=capture.transport,
            ),
        )
    else:
        raise BusinessError("internal_service_invalid")
    instance_id = instance_id or str(uuid4())
    accepting = False
    active = 0

    async def local_ready():
        if not accepting:
            return False
        try:
            current = await anyio.to_thread.run_sync(state_loader, state.root)
            if current.public_key != state.public_key or current.credential_digest(
                service
            ) != state.credential_digest(service):
                return False
            if getattr(current, "case_digest", None) != getattr(state, "case_digest", None):
                return False
            await journal.check_ready()
        except Exception:
            return False
        return accepting

    @asynccontextmanager
    async def lifespan(app):
        nonlocal accepting
        await anyio.to_thread.run_sync(state_loader, state.root)
        await journal.prepare()
        async with native.router.lifespan_context(native):
            accepting = True
            try:
                yield
            finally:
                accepting = False

    async def health(request):
        live = request.url.path.endswith("/live")
        ready = accepting if live else await local_ready()
        return JSONResponse(
            {
                "status": ("live" if live else "ready") if ready else "not_ready",
                "mode": getattr(state, "mode", MODE),
                "service": service,
                "instance_id": instance_id,
                "scope": "synthetic_local"
                if getattr(state, "mode", MODE) == MODE
                else "approved_mock_read",
            },
            status_code=200 if ready else 503,
        )

    async def admitted(scope, receive, send):
        nonlocal active
        if scope["type"] == "http":
            if active >= 4:
                await JSONResponse({"error": "service_unavailable"}, status_code=503)(
                    scope, receive, send
                )
                return
            active += 1
            try:
                if not await local_ready():
                    await JSONResponse({"error": "service_unavailable"}, status_code=503)(
                        scope, receive, send
                    )
                    return
                await native(scope, receive, send)
            finally:
                active -= 1
        else:
            await native(scope, receive, send)

    return Starlette(
        routes=[
            Route("/health/live", health),
            Route("/health/ready", health),
            Mount("/", app=admitted),
        ],
        lifespan=lifespan,
    )


class _LocalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # Uvicorn 会在自身清理之后重新抛出捕获的信号。实验环境宿主
        # 必须先持久化 stopped 状态，并同时释放其监听锁。
        previous = {}

        def stop(signum, frame):
            if self.should_exit:
                self.force_exit = True
            self.should_exit = True

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, stop)
            yield
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


async def serve(state, service, *, port, scenario, app_factory=create_lab_app):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise BusinessError("internal_port_invalid")
    lock_path = state.policy_path(service).parent / "listener.lock"
    lock = _private_file(lock_path, create=True)
    sock = None
    record = None
    task = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BusinessError("internal_service_already_running") from None
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        sock.setblocking(False)
        record = {
            "mode": getattr(state, "mode", MODE),
            "service": service,
            "port": port,
            "pid": os.getpid(),
            "instance_id": str(uuid4()),
            "state": "starting",
            "scenario": scenario,
        }
        app = app_factory(
            state,
            service,
            scenario=scenario,
            instance_id=record["instance_id"],
        )
        _write_metadata(state, service, record)
        server = _LocalServer(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                access_log=False,
                log_level="critical",
                log_config=None,
                lifespan="on",
                timeout_graceful_shutdown=5,
                proxy_headers=False,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        with anyio.fail_after(10):
            while not server.started:
                if task.done():
                    await task
                    raise BusinessError("internal_service_start_failed")
                await asyncio.sleep(0.02)
        record["state"] = "running"
        _write_metadata(state, service, record)
        print(
            json.dumps(
                {
                    "status": "ready",
                    "mode": getattr(state, "mode", MODE),
                    "service": service,
                    "url": f"http://127.0.0.1:{port}/mcp",
                    "pid": os.getpid(),
                    "real_business_called": False,
                }
            ),
            flush=True,
        )
        await task
    finally:
        if task is not None and not task.done():
            server.should_exit = True
            try:
                await asyncio.wait_for(task, timeout=8)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        try:
            if record is not None:
                record["state"] = "stopped"
                _write_metadata(state, service, record)
        finally:
            if sock is not None:
                sock.close()
            os.close(lock)


def parser():
    result = argparse.ArgumentParser(description="BIZ-T100 internal lab; synthetic systems only")
    commands = result.add_subparsers(dest="command", required=True)
    for command in ("init", "token", "serve", "status", "call", "check"):
        sub = commands.add_parser(command)
        sub.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
        if command in {"token", "serve", "call"}:
            sub.add_argument("--service", choices=PORTS, required=True)
        if command == "token":
            sub.add_argument(
                "--user", choices=("user-a", "user-b", "disabled", "unmapped"), required=True
            )
            sub.add_argument("--output", type=Path, required=True)
            sub.add_argument("--ttl", type=int, default=300)
        elif command == "serve":
            sub.add_argument("--port", type=int)
            sub.add_argument(
                "--scenario",
                choices=("success", "unauthenticated", "timeout", "mismatch"),
                default="success",
            )
        elif command == "call":
            who = sub.add_mutually_exclusive_group()
            who.add_argument("--user", choices=("user-a", "user-b", "disabled", "unmapped"))
            who.add_argument("--token-file", type=Path)
            sub.add_argument("--confirm", action="store_true")
            sub.add_argument("--operation-id")
    return result


async def _status(state):
    values = await asyncio.gather(*(service_status(state, service) for service in PORTS))
    return {"mode": MODE, "services": values}


async def _client_command(state, args):
    from internal_client import call_once, check_services

    statuses = await _status(state)
    ready = {
        value["service"]: value["url"]
        for value in statuses["services"]
        if value["status"] == "ready"
    }
    if args.command == "check":
        if set(ready) != set(PORTS):
            raise BusinessError("internal_services_not_ready")
        return await check_services(state, ready)
    if args.service not in ready:
        raise BusinessError("internal_services_not_ready")
    return await call_once(
        state,
        args.service,
        ready[args.service],
        user=args.user or "user-a",
        token_file=args.token_file,
        confirm=args.confirm,
        operation_id=args.operation_id,
    )


def run(argv=None):
    args = parser().parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        if args.command == "init":
            if args.state_dir == DEFAULT_STATE and not DEFAULT_STATE.parent.exists():
                DEFAULT_STATE.parent.mkdir(mode=0o700)
            value = init_state(args.state_dir)
        elif args.command == "token":
            value = issue_token(
                args.state_dir,
                user=args.user,
                service=args.service,
                output=args.output,
                ttl_seconds=args.ttl,
            )
        else:
            state = load_state(args.state_dir)
            if args.command == "serve":
                asyncio.run(
                    serve(
                        state,
                        args.service,
                        port=args.port if args.port is not None else PORTS[args.service],
                        scenario=args.scenario,
                    )
                )
                return 0
            value = asyncio.run(
                _status(state) if args.command == "status" else _client_command(state, args)
            )
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, BusinessError) and exc.code.startswith("internal_")
            else "internal_test_failed"
        )
        print(json.dumps({"status": "failed", "error": code, "mode": MODE}))
        return 1
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 1 if value.get("status") == "failed" else 0
