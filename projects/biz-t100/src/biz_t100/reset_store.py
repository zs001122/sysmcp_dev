"""持久化的本人密码重置占用记录；不支持重放或自动解锁。

文件系统保护沿用本地查询日志的做法。本存储有自己的应用/模式标识，
绝不会把查询数据库当作重置存储打开。
"""

import os
import re
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Literal
from uuid import UUID

import anyio

from .models import BusinessError, Caller

_APPLICATION_ID = 0x53595231  # SYR1，与查询日志的 SYT1 区分。
_VERSION = 1
_TOOL_NAME = "reset_my_t100_password"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
_CODES = {
    "unauthenticated",
    "identity_not_enabled",
    "account_not_linked",
    "permission_denied",
    "policy_unavailable",
    "credential_unavailable",
    "authorization_changed",
    "business_adapter_not_ready",
    "upstream_unauthenticated",
    "business_forbidden",
    "upstream_unavailable",
    "upstream_timeout",
    "upstream_rejected",
    "upstream_contract_error",
    "journal_unavailable",
    "query_timeout",
    "query_cancelled",
    "query_failed",
    "reset_outcome_unknown",
    "reset_cancelled",
    "reset_timeout",
    "reset_failed",
    "confirmation_required",
}
_CONFLICT_CODES = {"reset_already_recorded", "reset_pending_review"}
_IDENTITY_FIELDS = (
    "operation_id,issuer,subject,client_id,enterprise_id,employee_id,account_id,"
    "tool_name,binding_version"
)
_CONTEXT = """CREATE TABLE reset_context (
 id INTEGER PRIMARY KEY CHECK(id=1),
 enterprise_id TEXT NOT NULL,
 tool_name TEXT NOT NULL CHECK(tool_name='reset_my_t100_password')
)"""
_OPERATIONS = """CREATE TABLE reset_operations (
 operation_id TEXT PRIMARY KEY,
 issuer TEXT NOT NULL,
 subject TEXT NOT NULL,
 client_id TEXT NOT NULL,
 enterprise_id TEXT NOT NULL,
 employee_id TEXT NOT NULL,
 account_id TEXT NOT NULL,
 tool_name TEXT NOT NULL CHECK(tool_name='reset_my_t100_password'),
 binding_version TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN
 ('started','dispatched','succeeded','rejected','not_sent','unknown')),
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 code TEXT
)"""
_EVENTS = """CREATE TABLE reset_events (
 operation_id TEXT NOT NULL REFERENCES reset_operations(operation_id),
 issuer TEXT NOT NULL,
 subject TEXT NOT NULL,
 client_id TEXT NOT NULL,
 enterprise_id TEXT NOT NULL,
 employee_id TEXT NOT NULL,
 account_id TEXT NOT NULL,
 tool_name TEXT NOT NULL CHECK(tool_name='reset_my_t100_password'),
 binding_version TEXT NOT NULL,
 event TEXT NOT NULL CHECK(event IN
 ('reset_started','reset_dispatched','reset_succeeded','reset_rejected','reset_not_sent','reset_unknown')),
 occurred_at REAL NOT NULL,
 code TEXT,
 PRIMARY KEY(operation_id,event)
)"""
_EMPLOYEE_INDEX = """CREATE UNIQUE INDEX one_pending_employee
 ON reset_operations(enterprise_id,employee_id)
 WHERE state IN ('started','dispatched','unknown')"""
_ACCOUNT_INDEX = """CREATE UNIQUE INDEX one_pending_account
 ON reset_operations(enterprise_id,account_id)
 WHERE state IN ('started','dispatched','unknown')"""
_SCHEMA = {
    ("table", "reset_context", _CONTEXT),
    ("table", "reset_operations", _OPERATIONS),
    ("table", "reset_events", _EVENTS),
    ("index", "one_pending_employee", _EMPLOYEE_INDEX),
    ("index", "one_pending_account", _ACCOUNT_INDEX),
}


def _text(value, maximum):
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError()
    return value


def _operation_id(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError()
    return value


def _code(value):
    if value is not None and (type(value) is not str or value not in _CODES):
        raise ValueError()
    return value


class SQLiteResetStore:
    """单企业的写操作证据。构造时不做文件系统 I/O。

    started/dispatched/unknown 状态会跨调用者同时占用员工与账号。
    dispatched 记录的是可能已派发，不代表上游已接收。succeeded 记录的是
    API 已接受的响应，不证明密码修改已生效。只有派发前明确的 not_sent
    或已知的派发结果才会释放目标。未知结果没有自动释放或重放接口。
    """

    # 同项目的账号维护库复用文件/事务保护；各库固定自己的应用标识与模式。
    # 默认值保留既有重置库的逐字模式，不自动迁移或读取其它用途的数据库。
    _application_id = _APPLICATION_ID
    _version = _VERSION
    _tool_name = _TOOL_NAME
    _context_tool_name = _TOOL_NAME
    _schema_definition = _SCHEMA
    _schema_statements = (_CONTEXT, _OPERATIONS, _EVENTS, _EMPLOYEE_INDEX, _ACCOUNT_INDEX)

    def __init__(self, path: Path, *, enterprise_id: str):
        failed = False
        try:
            self._enterprise_id = _text(enterprise_id, 256)
            self._path = Path(os.path.abspath(os.fspath(path)))
        except (TypeError, ValueError):
            failed = True
        if failed:
            raise BusinessError("journal_unavailable")
        self._lock = threading.Lock()

    def _directory(self, *, create):
        parent = self._path.parent
        # 逐级检查路径祖先，不跟随符号链接。允许 /tmp 这类已存在的共享祖先；
        # 直接父目录必须是私有的。
        for directory in (*reversed(parent.parents), parent):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                if not create:
                    raise
                directory.mkdir(mode=0o700)
                # 在其下创建任何内容之前，先持久化这个新目录项。
                self._sync_directory(directory.parent)
                info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError()
        info = parent.lstat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError()

    @staticmethod
    def _sync_directory(path):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _file(self):
        info = self._path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise ValueError()
        # SQLite 的附属文件同样属于本存储；主文件合法时，
        # 打开附属文件也绝不跟随被植入的符号链接。
        for suffix in ("-journal", "-wal", "-shm"):
            sibling = self._path.with_name(self._path.name + suffix)
            try:
                side = sibling.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(side.st_mode)
                or side.st_uid != os.geteuid()
                or stat.S_IMODE(side.st_mode) != 0o600
                or side.st_nlink != 1
            ):
                raise ValueError()
        return info.st_dev, info.st_ino

    def _header(self):
        identity = self._file()
        descriptor = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != identity:
                raise ValueError()
            header = os.read(descriptor, 100)
        finally:
            os.close(descriptor)
        if (
            len(header) != 100
            or header[:16] != b"SQLite format 3\x00"
            or int.from_bytes(header[60:64], "big") != self._version
            or int.from_bytes(header[68:72], "big") != self._application_id
        ):
            raise ValueError()
        return identity

    def _schema(self, connection):
        actual = set(
            connection.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        )
        if (
            actual != self._schema_definition
            or connection.execute("PRAGMA application_id").fetchone()[0] != self._application_id
            or connection.execute("PRAGMA user_version").fetchone()[0] != self._version
            or connection.execute("SELECT * FROM reset_context").fetchall()
            != [(1, self._enterprise_id, self._context_tool_name)]
        ):
            raise ValueError()

    def _open(self, *, create=False):
        self._directory(create=create)
        created = False
        if create:
            try:
                descriptor = os.open(
                    self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
                )
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
                created = True
        identity = self._file() if created else self._header()
        connection = sqlite3.connect(
            self._path.as_uri() + ("?mode=rw" if create else "?mode=ro"), uri=True, timeout=1
        )
        try:
            if self._file() != identity:
                raise ValueError()
            connection.execute("PRAGMA foreign_keys=ON")
            if created:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                for statement in self._schema_statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO reset_context VALUES (1,?,?)",
                    (self._enterprise_id, self._context_tool_name),
                )
                connection.execute(f"PRAGMA application_id={self._application_id}")
                connection.execute(f"PRAGMA user_version={self._version}")
                connection.commit()
                self._sync_directory(self._path.parent)
            self._schema(connection)
            return connection
        except BaseException:
            connection.close()
            raise

    async def _run(self, function, *args):
        failure = None
        try:
            return await anyio.to_thread.run_sync(function, *args, abandon_on_cancel=False)
        except BusinessError as exc:
            failure = exc.code if exc.code in _CONFLICT_CODES else "journal_unavailable"
        except Exception:
            failure = "journal_unavailable"
        # 不保留包含 SQL 或私有数据的驱动/文件异常上下文。
        raise BusinessError(failure)

    def _prepare(self, create):
        with self._lock:
            connection = self._open(create=create)
            connection.close()

    async def prepare(self) -> None:
        """只创建/校验独立的模式（schema）；不凭空生成业务事件。"""
        await self._run(self._prepare, True)

    async def check_ready(self) -> None:
        """只读的本地模式/上下文检查；绝不创建缺失的数据库。"""
        await self._run(self._prepare, False)

    def _reserve(self, operation_id, caller, employee_id, account_id, binding_version):
        operation_id = _operation_id(operation_id)
        if type(caller) is not Caller:
            raise ValueError()
        caller = Caller.model_validate(caller.model_dump())
        _text(caller.issuer, 512)
        _text(caller.subject, 256)
        _text(caller.client_id, 256)
        for value in (employee_id, account_id):
            if type(value) is not str or not _IDENTIFIER.fullmatch(value):
                raise ValueError()
        _text(binding_version, 256)
        identity = (
            operation_id,
            caller.issuer,
            caller.subject,
            caller.client_id,
            self._enterprise_id,
            employee_id,
            account_id,
            self._tool_name,
            binding_version,
        )
        with self._lock:
            connection = self._open(create=True)
            try:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM reset_operations WHERE operation_id=?", (operation_id,)
                ).fetchone():
                    raise BusinessError("reset_already_recorded")
                if connection.execute(
                    "SELECT 1 FROM reset_operations WHERE enterprise_id=? "
                    "AND (employee_id=? OR account_id=?) "
                    "AND state IN ('started','dispatched','unknown') LIMIT 1",
                    (self._enterprise_id, employee_id, account_id),
                ).fetchone():
                    raise BusinessError("reset_pending_review")
                now = time.time()
                connection.execute(
                    "INSERT INTO reset_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*identity, "started", now, now, None),
                )
                connection.execute(
                    "INSERT INTO reset_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*identity, "reset_started", now, None),
                )
                connection.commit()
            finally:
                connection.close()

    async def reserve(
        self,
        *,
        operation_id: str,
        caller: Caller,
        employee_id: str,
        account_id: str,
        binding_version: str,
    ) -> None:
        """在任何派发之前，持久化占用一组调用者/员工/账号/策略。"""
        await self._run(
            self._reserve, operation_id, caller, employee_id, account_id, binding_version
        )

    def _transition(self, operation_id, state, code):
        _operation_id(operation_id)
        _code(code)
        if state == "succeeded" and code is not None:
            raise ValueError()
        previous = "started" if state in {"dispatched", "not_sent"} else "dispatched"
        with self._lock:
            connection = self._open(create=True)
            try:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                operation = connection.execute(
                    f"SELECT {_IDENTITY_FIELDS},state FROM reset_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if (
                    operation is None
                    or operation[-1] != previous
                    or operation[7] != self._tool_name
                ):
                    raise ValueError()
                now = time.time()
                connection.execute(
                    "UPDATE reset_operations SET state=?,updated_at=?,code=? WHERE operation_id=?",
                    (state, now, code, operation_id),
                )
                connection.execute(
                    "INSERT INTO reset_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*operation[:-1], "reset_" + state, now, code),
                )
                connection.commit()
            finally:
                connection.close()

    async def mark_dispatched(self, *, operation_id: str) -> None:
        """在调用上游适配器之前，持久化标记可能已派发。"""
        await self._run(self._transition, operation_id, "dispatched", None)

    def _finish(self, operation_id, outcome, code):
        if type(outcome) is not str or outcome not in {
            "succeeded",
            "rejected",
            "not_sent",
            "unknown",
        }:
            raise ValueError()
        self._transition(operation_id, outcome, code)

    async def finish(
        self,
        *,
        operation_id: str,
        outcome: Literal["succeeded", "rejected", "not_sent", "unknown"],
        code: str | None = None,
    ) -> None:
        """持久化允许的结果；unknown 保持对员工与账号的双重占用。"""
        await self._run(self._finish, operation_id, outcome, code)

    def _pending(self):
        with self._lock:
            connection = self._open()
            try:
                return [
                    row[0]
                    for row in connection.execute(
                        "SELECT operation_id FROM reset_operations "
                        "WHERE state IN ('started','dispatched','unknown') ORDER BY operation_id"
                    )
                ]
            finally:
                connection.close()

    async def pending_operation_ids(self) -> list[str]:
        """只读取待核查的 ID；绝不重放或释放占用。"""
        return await self._run(self._pending)
