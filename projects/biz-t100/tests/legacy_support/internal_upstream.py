# Historical regression support relocated from scripts; not a supported command.
"""仅供开发使用的捕获式上游，服务于固定的内部合成实验环境。

真实业务客户端只消费传输层；本模块绝不连接远端系统、
读取服务密钥文件，也不替代产品的 SQLite 审计。
"""

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import stat
import time
import unicodedata
from pathlib import Path
from typing import Literal

import anyio
import httpx

URLS = {
    "read": "https://internal-read.example.invalid/api",
    "reset": "https://internal-reset.example.invalid/api",
}
ENTERPRISES = {"read": "INTERNAL-READ", "reset": "INTERNAL-RESET"}
_SERVICES = {"read": "employee.list.data.query.get", "reset": "user.account.create"}
_PEOPLE = {"E001": "LOGIN-A", "E002": "LOGIN-B"}
_FIELDS = {"counter", "time", "service", "scenario", "enterprise_id", "employee_id", "account_id"}
_SCENARIOS = {"success", "unauthenticated", "timeout", "mismatch"}


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(parent):
    for directory in (*reversed(parent.parents), parent):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass  # 另一个捕获实例可能已创建同一个私有目录。
            else:
                _sync_directory(directory.parent)
            info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError()
    info = parent.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError()


class SyntheticUpstream:
    """固定的 A/B 目标，或一个运维方配置的只读装置数据。

    构造时不做 I/O。scenario 是运维方的启动配置，绝不是工具参数。
    摘要比对的是已授权业务客户端提供的密钥；本模块不读取任何原始凭据。
    read_fixture 是受信任的启动数据，绝不是工具参数或重置目标。
    """

    def __init__(
        self,
        service: Literal["read", "reset"],
        *,
        enterprise_id: str,
        credential_sha256: str,
        request_log: Path,
        scenario: Literal["success", "unauthenticated", "timeout", "mismatch"] = "success",
        read_fixture: tuple[str, str] | None = None,
    ):
        if (
            type(service) is not str
            or service not in URLS
            or enterprise_id != ENTERPRISES[service]
            or type(credential_sha256) is not str
            or re.fullmatch(r"[a-f0-9]{64}", credential_sha256) is None
            or type(scenario) is not str
            or scenario not in _SCENARIOS
            or (service == "reset" and scenario == "mismatch")
        ):
            raise ValueError("invalid_synthetic_configuration")
        if read_fixture is not None and (
            service != "read"
            or type(read_fixture) is not tuple
            or len(read_fixture) != 2
            or type(read_fixture[0]) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}", read_fixture[0]) is None
            or type(read_fixture[1]) is not str
            or not read_fixture[1].strip()
            or len(read_fixture[1]) > 256
            or any(unicodedata.category(char).startswith("C") for char in read_fixture[1])
        ):
            raise ValueError("invalid_synthetic_configuration")
        path = Path(request_log)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("invalid_synthetic_configuration")
        self._service = service
        self._enterprise = enterprise_id
        self._credential_digest = credential_sha256
        self._request_log = path
        self._scenario = scenario
        self._read_names = (
            {read_fixture[0]: read_fixture[1]}
            if read_fixture is not None
            else {employee: "Synthetic " + employee for employee in _PEOPLE}
        )
        self.transport = httpx.MockTransport(self._handle)

    def _inspect_request(self, request):
        authenticated, valid = False, False
        employee, account = None, None
        try:
            if len(request.content) > 65_536:
                return authenticated, valid, employee, account
            body = json.loads(request.content)
            key = body.get("key")
            authenticated = (
                type(key) is str
                and len(key) <= 16_384
                and hmac.compare_digest(
                    hashlib.sha256(key.encode()).hexdigest(), self._credential_digest
                )
            )
            parameter = body["payload"]["std_data"]["parameter"]
            if self._service == "read":
                candidate = parameter.get("employee_no")
                if type(candidate) is str and candidate in self._read_names:
                    employee = candidate
                valid = (
                    employee is not None
                    and set(parameter) == {"is_digiwin_product", "use_has_next", "employee_no"}
                    and parameter["is_digiwin_product"] == "N"
                    and parameter["use_has_next"] is False
                )
            else:
                master = parameter["master"]
                if type(master) is list and len(master) == 1 and type(master[0]) is dict:
                    value = master[0]
                    candidate = value.get("gzxa003")
                    if (
                        type(candidate) is str
                        and candidate in _PEOPLE
                        and value.get("gzxa001") == _PEOPLE[candidate]
                    ):
                        employee, account = candidate, _PEOPLE[candidate]
                    valid = (
                        employee is not None
                        and set(parameter) == {"master"}
                        and set(value) == {"gzxa001", "gzxa003", "password_reset"}
                        and value["password_reset"] == "Y"
                    )
            valid = bool(
                valid
                and request.method == "POST"
                and str(request.url) == URLS[self._service]
                and set(body) == {"key", "type", "host", "service", "datakey", "payload"}
                and body["type"] == "sync"
                and type(body["host"]) is dict
                and body["service"]["name"] == _SERVICES[self._service]
                and body["service"]["prod"] == "T100"
                and body["datakey"] == {"EntId": self._enterprise}
                and set(body["payload"]) == {"std_data"}
                and set(body["payload"]["std_data"]) == {"parameter"}
            )
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
            valid = False
        return authenticated, valid, employee, account

    def _last_counter(self, descriptor):
        size = os.fstat(descriptor).st_size
        if size == 0:
            return 0
        os.lseek(descriptor, max(0, size - 4096), os.SEEK_SET)
        tail = os.read(descriptor, 4096)
        if not tail.endswith(b"\n"):
            raise ValueError()
        row = json.loads(tail.splitlines()[-1])
        if (
            type(row) is not dict
            or set(row) != _FIELDS
            or type(row["counter"]) is not int
            or row["counter"] < 1
            or type(row["time"]) not in (int, float)
            or not math.isfinite(row["time"])
            or row["service"] != self._service
            or row["enterprise_id"] != self._enterprise
            or row["scenario"] not in _SCENARIOS
            or (self._service == "reset" and row["scenario"] == "mismatch")
            or (
                row["employee_id"] is not None
                and row["employee_id"]
                not in (self._read_names if self._service == "read" else _PEOPLE)
            )
            or (self._service == "read" and row["account_id"] is not None)
            or (self._service == "reset" and row["account_id"] != _PEOPLE.get(row["employee_id"]))
        ):
            raise ValueError()
        return row["counter"]

    def _capture(self, employee, account):
        path = self._request_log
        _private_directory(path.parent)
        descriptor = os.open(
            path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise ValueError()
            # 短时文件锁同时用于串行化不同的模拟器实例。
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            row = {
                "counter": self._last_counter(descriptor) + 1,
                "time": time.time(),
                "service": self._service,
                "scenario": self._scenario,
                "enterprise_id": self._enterprise,
                "employee_id": employee,
                "account_id": account,
            }
            line = (json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n").encode()
            offset = 0
            while offset < len(line):
                written = os.write(descriptor, line[offset:])
                if written <= 0:
                    raise OSError()
                offset += written
            os.fsync(descriptor)
            _sync_directory(path.parent)
        finally:
            os.close(descriptor)

    async def _handle(self, request):
        authenticated, valid, employee, account = self._inspect_request(request)
        failed = False
        try:
            await anyio.to_thread.run_sync(
                self._capture, employee, account, abandon_on_cancel=False
            )
        except Exception:
            failed = True
        if failed:
            # 在处理器外抛出：绝不把原始路径/数据作为上下文带出。
            raise httpx.RequestError("synthetic_capture_unavailable")
        if not authenticated or self._scenario == "unauthenticated":
            return httpx.Response(401, json={"error": "synthetic_unauthenticated"})
        if not valid:
            return httpx.Response(400, json={"error": "synthetic_request_rejected"})
        if self._scenario == "timeout":
            raise httpx.ReadTimeout("synthetic_upstream_timeout")
        result = {"execution": {"code": "0"}}
        if self._service == "read":
            display_name = self._read_names[employee]
            if self._scenario == "mismatch":
                employee = "E002" if employee == "E001" else "E001"
                display_name = "Synthetic " + employee
            result["parameter"] = {
                "employee": [
                    {
                        "employee_no": employee,
                        "full_name": display_name,
                        "department_no": "SYNTHETIC",
                        "status": "Y",
                    }
                ]
            }
        return httpx.Response(200, json={"payload": {"std_data": result}})
