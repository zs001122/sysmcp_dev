"""Publish one BIZ-issued connector JWT through MetaMCP 2.4.22 management API.

JWT parsing here checks publication constraints, NOT identity or signature validity.
The private handoff file is trusted; BIZ still performs native signature validation.
This tool has no business API, employee mapping, private signing key or database access.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import http.cookies
import ipaddress
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

MAX_FILE = 65536
MAX_RESPONSE = 1048576
FIELDS = {
    "schema_version",
    "base_url",
    "origin",
    "server_uuid",
    "expected_name",
    "expected_type",
    "expected_url",
    "token_file",
    "admin_login_file",
    "state_file",
    "expected_issuer",
    "expected_audience",
    "expected_subject",
    "expected_client_id",
    "expected_scopes",
}
SERVER_FIELDS = (
    "uuid",
    "name",
    "type",
    "url",
    "headers",
    "args",
    "env",
    "user_id",
    "description",
    "command",
)


class SyncError(Exception):
    def __init__(self, code: str, stage: str):
        self.code, self.stage = code, stage
        super().__init__(code)


def fail(code: str, stage: str) -> None:
    raise SyncError(code, stage)


def _json(raw: bytes | str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )


def _text(value) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 4096
        and value.strip() == value
        and not any(ord(c) < 32 for c in value)
    )


def private_path(value: str | Path, *, missing: bool = False) -> Path:
    """Reject links, shared writers, non-private parent and unowned/non-0600 files."""
    path = Path(value)
    try:
        if not path.is_absolute() or ".." in path.parts or path == Path("/"):
            raise ValueError()
        for ancestor in reversed(path.parents):
            info = ancestor.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, os.getuid()}
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise ValueError()
        parent = path.parent.lstat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError()
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing:
                return path
            raise
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError()
        return path
    except (OSError, ValueError, TypeError):
        fail("private_file_invalid", "files")


def read_private(path: Path, *, limit: int = MAX_FILE) -> bytes:
    private_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise ValueError()
            value = stream.read(limit + 1)
            if len(value) > limit:
                raise ValueError()
            return value
    except (OSError, ValueError):
        fail("private_file_invalid", "files")


def _url(value, *, management: bool = False) -> None:
    if not _text(value):
        raise ValueError()
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError()
    if management:
        if parsed.path or parsed.port is None:
            raise ValueError()
        if (
            parsed.scheme == "http"
            and parsed.hostname != "localhost"
            and not ipaddress.ip_address(parsed.hostname).is_loopback
        ):
            raise ValueError()


def load_config(path: Path) -> dict:
    try:
        value = _json(read_private(path))
        if type(value) is not dict or set(value) != FIELDS:
            raise ValueError()
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError()
        if any(
            not _text(value[k]) for k in FIELDS - {"schema_version", "expected_scopes"}
        ):
            raise ValueError()
        if str(uuid.UUID(value["server_uuid"])) != value["server_uuid"]:
            raise ValueError()
        if (
            value["expected_name"] != "sysmcp-t100"
            or value["expected_type"] != "STREAMABLE_HTTP"
        ):
            raise ValueError()
        _url(value["base_url"], management=True)
        _url(value["origin"])
        if urllib.parse.urlsplit(value["origin"]).path:
            raise ValueError()
        _url(value["expected_url"])
        scopes = value["expected_scopes"]
        if (
            type(scopes) is not list
            or not scopes
            or not all(_text(s) and " " not in s for s in scopes)
            or len(set(scopes)) != len(scopes)
        ):
            raise ValueError()
        paths = [path]
        for key in ("token_file", "admin_login_file", "state_file"):
            paths.append(private_path(value[key], missing=key == "state_file"))
        lock_path = Path(value["state_file"] + ".lock")
        private_path(lock_path, missing=True)
        if len(set(paths + [lock_path])) != 5:
            raise ValueError()
        return value
    except SyncError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError):
        fail("configuration_invalid", "configuration")


def token_constraints(config: dict, now: int) -> tuple[str, int]:
    try:
        token = read_private(Path(config["token_file"])).decode("ascii").strip()
        pieces = token.split(".")
        if len(pieces) != 3 or any(
            not re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in pieces
        ):
            raise ValueError()
        header, claims = [
            _json(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))) for p in pieces[:2]
        ]
        if (
            type(header) is not dict
            or header.get("alg") != "RS256"
            or header.get("crit")
        ):
            raise ValueError()
        if type(claims) is not dict:
            raise ValueError()
        for claim, setting in (
            ("iss", "expected_issuer"),
            ("aud", "expected_audience"),
            ("sub", "expected_subject"),
            ("azp", "expected_client_id"),
        ):
            if claims.get(claim) != config[setting]:
                raise ValueError()
        issued, expiry = claims.get("iat"), claims.get("exp")
        if (
            type(issued) is not int
            or type(expiry) is not int
            or issued > now + 30
            or not 0 < expiry - issued <= 3600
            or expiry - now < 600
        ):
            raise ValueError()
        if "nbf" in claims and (
            type(claims["nbf"]) is not int or claims["nbf"] > now + 30
        ):
            raise ValueError()
        scopes = claims.get("scope")
        if type(scopes) is not str or scopes.split(" ") != list(
            dict.fromkeys(scopes.split(" "))
        ):
            raise ValueError()
        if set(scopes.split(" ")) != set(config["expected_scopes"]):
            raise ValueError()
        # Competing identity/scope claims are not part of this publication contract.
        if any(k in claims for k in ("client_id", "scp", "employee_no", "employee_id")):
            raise ValueError()
        return token, expiry
    except SyncError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError):
        fail("token_constraints_failed", "token")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MetaMCPClient:
    def __init__(self, config: dict):
        self.config = config
        self.cookie = ""
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def _request(self, method: str, route: str, payload, stage: str):
        headers = {"Origin": self.config["origin"], "Accept": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        body = None if payload is None else json.dumps(payload).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.config["base_url"] + route, data=body, method=method, headers=headers
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE or response.status != 200:
                    raise ValueError()
                return _json(raw), response.headers
        except Exception:  # noqa: BLE001 - Transport errors may contain credentials or remote bodies.
            fail(stage + "_failed", stage)

    def login(self, login: dict) -> None:
        _, headers = self._request("POST", "/api/auth/sign-in/email", login, "login")
        try:
            cookies = http.cookies.SimpleCookie()
            for value in headers.get_all("Set-Cookie", []):
                cookies.load(value)
            if not cookies:
                raise ValueError()
            cookie = "; ".join(
                f"{key}={value.coded_value}" for key, value in cookies.items()
            )
            if len(cookie) > 16384 or any(ord(c) < 32 for c in cookie):
                raise ValueError()
            self.cookie = cookie
        except (http.cookies.CookieError, TypeError, ValueError):
            fail("login_failed", "login")

    def get(self) -> dict:
        query = urllib.parse.urlencode(
            {"input": json.dumps({"uuid": self.config["server_uuid"]})}
        )
        result, _ = self._request(
            "GET", "/trpc/frontend.mcpServers.get?" + query, None, "get"
        )
        return self._result(result, "get")

    def update(self, payload: dict) -> None:
        result, _ = self._request(
            "POST", "/trpc/frontend.mcpServers.update", payload, "update"
        )
        self._result(result, "update")

    @staticmethod
    def _result(result, stage) -> dict:
        try:
            result = result["result"]["data"]
            if result["success"] is not True or type(result.get("data")) is not dict:
                raise ValueError()
            return result["data"]
        except (ValueError, TypeError, KeyError):
            fail(stage + "_failed", stage)


def server_snapshot(server: dict, config: dict) -> dict:
    try:
        if type(server) is not dict:
            raise ValueError()
        for key, expected in (
            ("uuid", "server_uuid"),
            ("name", "expected_name"),
            ("type", "expected_type"),
            ("url", "expected_url"),
        ):
            if server[key] != config[expected]:
                fail("server_identity_mismatch", "server")
        for key in ("headers", "env"):
            if type(server[key]) is not dict or any(
                type(k) is not str or type(v) is not str for k, v in server[key].items()
            ):
                raise ValueError()
        if type(server["args"]) is not list or any(
            type(v) is not str for v in server["args"]
        ):
            raise ValueError()
        for key in ("description", "command", "user_id", "bearerToken"):
            if server[key] is not None and type(server[key]) is not str:
                raise ValueError()
        return {key: server[key] for key in SERVER_FIELDS}
    except (ValueError, TypeError, KeyError):
        fail("server_contract_failed", "server")


def sync(config: dict, token: str, client) -> str:
    before = client.get()
    snapshot = server_snapshot(before, config)
    if before["bearerToken"] == token:
        return "unchanged"
    payload = {
        key: value
        for key, value in snapshot.items()
        if value is not None or key == "user_id"
    }
    payload["bearerToken"] = token
    update_failed = False
    try:
        client.update(payload)
    except SyncError:
        # The POST may already have taken effect. Reconcile using one GET only.
        update_failed = True
    try:
        after = client.get()
        checked = server_snapshot(after, config)
        if checked != snapshot:
            fail("server_fields_changed", "verify")
        if after["bearerToken"] != token:
            fail("update_not_verified", "verify")
    except SyncError as exc:
        if exc.stage == "get":
            fail("readback_failed", "verify")
        raise
    return "reconciled" if update_failed else "updated"


def timestamp(value: int) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


def write_state(path: Path, value: dict) -> None:
    temporary = None
    try:
        private_path(path, missing=True)
        fd, temporary = tempfile.mkstemp(prefix=".connector-state-", dir=path.parent)
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, ValueError, TypeError, SyncError):
        fail("state_write_failed", "state")
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


@contextmanager
def state_lock(path: Path):
    lock = private_path(str(path) + ".lock", missing=True)
    fd = None
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        private_path(lock)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("sync_already_running", "lock")
        yield
    except OSError:
        fail("lock_failed", "lock")
    finally:
        if fd is not None:
            os.close(fd)


def execute(config: dict, *, client_factory=MetaMCPClient, clock=time.time) -> dict:
    state_path = Path(config["state_file"])
    with state_lock(state_path):
        now = int(clock())
        state = {
            "schema_version": 1,
            "server_uuid": config["server_uuid"],
            "last_attempt": timestamp(now),
            "status": "running",
            "last_success": None,
            "expires_at": None,
            "token_sha256": None,
            "error": None,
            "stage": "token",
        }
        if state_path.exists():
            try:
                previous = _json(read_private(state_path))
                if (
                    type(previous) is not dict
                    or previous.get("schema_version") != 1
                    or previous.get("server_uuid") != config["server_uuid"]
                ):
                    raise ValueError()
                for key in ("last_success", "expires_at"):
                    value = previous.get(key)
                    if value is not None and (
                        type(value) is not str
                        or datetime.fromisoformat(value).tzinfo is None
                    ):
                        raise ValueError()
                    state[key] = value
                sha = previous.get("token_sha256")
                if sha is not None and (
                    type(sha) is not str or not re.fullmatch(r"[0-9a-f]{64}", sha)
                ):
                    raise ValueError()
                state["token_sha256"] = sha
            except (ValueError, TypeError, KeyError, UnicodeError):
                fail("state_invalid", "state")
        write_state(state_path, state)
        try:
            token, expiry = token_constraints(config, now)
            try:
                login = _json(read_private(Path(config["admin_login_file"])))
                if (
                    type(login) is not dict
                    or set(login) != {"email", "password"}
                    or not _text(login["email"])
                    or not _text(login["password"])
                ):
                    raise ValueError()
            except (ValueError, TypeError, KeyError, UnicodeError):
                fail("admin_login_invalid", "login")
            client = client_factory(config)
            client.login(login)
            outcome = sync(config, token, client)
            # Never announce success once the installed token is already too near expiry.
            finished = int(clock())
            if expiry - finished < 600:
                fail("token_constraints_failed", "verify")
            completed = state | {
                "status": "succeeded",
                "stage": "complete",
                "last_success": timestamp(finished),
                "expires_at": timestamp(expiry),
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            }
            write_state(state_path, completed)
            return {
                "status": "succeeded",
                "outcome": outcome,
                "expires_at": completed["expires_at"],
                "business_requests": 0,
            }
        except SyncError as exc:
            state.update(status="failed", stage=exc.stage, error=exc.code)
            write_state(state_path, state)
            raise
        except Exception:  # noqa: BLE001 - Record only fixed errors at the credential boundary.
            state.update(status="failed", stage="internal", error="sync_failed")
            write_state(state_path, state)
            fail("sync_failed", "internal")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = execute(load_config(args.config))
    except SyncError as exc:
        result = {"status": "failed", "error": exc.code, "stage": exc.stage}
    except Exception:  # noqa: BLE001 - Never expose credentials from unexpected exceptions.
        result = {"status": "failed", "error": "sync_failed", "stage": "internal"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
