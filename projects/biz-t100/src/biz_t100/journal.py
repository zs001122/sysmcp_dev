"""私有的单节点查询证据；不含业务重放或账号写入状态。

构造时不做 I/O。异步方法在工作线程中完成全部文件系统与 SQLite 操作，
即使 AnyIO 取消作用域被取消，也会等待该线程结束。
使用属主私有、权限 0700 的目录，任何失败后都保留数据库。
"""

import os
import re
import sqlite3
import stat
import threading
import time
from pathlib import Path

import anyio

from .errors import EMPLOYEE_RESULT_ERRORS
from .models import BusinessError, Caller
from .ports import Event

_APPLICATION_ID = 0x53595431  # SYT1：与来源账号服务数据库区分。
_VERSION = 2
_TOOL_NAME = "get_my_t100_employee_record"
_EVENTS = {"query_started", "query_succeeded", "query_failed", "query_cancelled"}
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
} | EMPLOYEE_RESULT_ERRORS
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
_TABLE = """CREATE TABLE query_events (
 request_id TEXT NOT NULL,
 event TEXT NOT NULL CHECK(event IN
 ('query_started','query_succeeded','query_failed','query_cancelled')),
 occurred_at REAL NOT NULL,
 issuer TEXT NOT NULL,
 subject TEXT NOT NULL,
 client_id TEXT NOT NULL,
 employee_id TEXT,
 code TEXT,
 upstream_attempted INTEGER NOT NULL CHECK(upstream_attempted IN (0,1)),
 enterprise_id TEXT,
 tool_name TEXT NOT NULL CHECK(tool_name='get_my_t100_employee_record'),
 CHECK(event <> 'query_started' OR upstream_attempted=0),
 CHECK(event <> 'query_succeeded' OR upstream_attempted=1),
 PRIMARY KEY(request_id,event)
)"""
_CONTEXT_TABLE = """CREATE TABLE journal_context (
 id INTEGER PRIMARY KEY CHECK(id=1),
 enterprise_id TEXT,
 tool_name TEXT NOT NULL CHECK(tool_name='get_my_t100_employee_record')
)"""
_TERMINAL_INDEX = """CREATE UNIQUE INDEX one_query_terminal ON query_events(request_id)
 WHERE event <> 'query_started'"""
_EXPECTED_SCHEMA = {
    ("table", "query_events", _TABLE),
    ("table", "journal_context", _CONTEXT_TABLE),
    ("index", "one_query_terminal", _TERMINAL_INDEX),
}


def _validate_enterprise(value):
    if value is not None and (
        type(value) is not str
        or not value.strip()
        or len(value) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError()
    return value


def _validate(request_id, caller, employee_id, event, code, upstream_attempted, enterprise_id):
    if type(request_id) is not str or not _IDENTIFIER.fullmatch(request_id):
        raise ValueError()
    if type(caller) is not Caller:
        raise ValueError()
    caller = Caller.model_validate(caller.model_dump())
    for value in (caller.issuer, caller.subject, caller.client_id):
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError()
    if employee_id is not None and (
        type(employee_id) is not str or not _IDENTIFIER.fullmatch(employee_id)
    ):
        raise ValueError()
    if type(event) is not str or event not in _EVENTS:
        raise ValueError()
    if code is not None and (type(code) is not str or code not in _CODES):
        raise ValueError()
    if event in {"query_started", "query_succeeded"} and code is not None:
        raise ValueError()
    if type(upstream_attempted) is not bool:
        raise ValueError()
    if (event == "query_started" and upstream_attempted) or (
        event == "query_succeeded" and not upstream_attempted
    ):
        raise ValueError()
    return (
        request_id,
        event,
        time.time(),
        caller.issuer,
        caller.subject,
        caller.client_id,
        employee_id,
        code,
        int(upstream_attempted),
        _validate_enterprise(enterprise_id),
        _TOOL_NAME,
    )


class SQLiteQueryJournal:
    """追加封闭集合内的查询事实，并检查未完成的请求 ID。

    缺少终态记录表示结果需要人工核对，而不是查询从未发出。
    数据库故障绝不授权再次发起业务请求。该文件不是加密或防篡改的审计存储。
    运行时写入方绑定 enterprise_id；None 仅用于底层测试和无上下文检查，
    不构成完整的业务证据。已绑定的数据库在重新打开时不能再绑定。
    upstream_attempted 记录的是进入 EmployeeReader，而非远端已接收。
    """

    def __init__(self, path: Path, *, enterprise_id: str | None = None):
        self._path = Path(os.path.abspath(os.fspath(path)))
        self._lock = threading.Lock()
        failed = False
        try:
            self._enterprise_id = _validate_enterprise(enterprise_id)
        except ValueError:
            failed = True
        if failed:
            raise BusinessError("journal_unavailable")

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
        # SQLite 的附属文件同样属于本日志；主文件合法时，
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
            or int.from_bytes(header[60:64], "big") != _VERSION
            or int.from_bytes(header[68:72], "big") != _APPLICATION_ID
        ):
            raise ValueError()
        return identity

    def _schema(self, connection, *, writable):
        actual = set(
            connection.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        )
        if (
            actual != _EXPECTED_SCHEMA
            or connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0] != _VERSION
        ):
            raise ValueError()
        contexts = connection.execute(
            "SELECT id,enterprise_id,tool_name FROM journal_context"
        ).fetchall()
        if len(contexts) != 1 or contexts[0][0] != 1 or contexts[0][2] != _TOOL_NAME:
            raise ValueError()
        enterprise_id = _validate_enterprise(contexts[0][1])
        # 无上下文检查用于支持 pending CLI；写入/prepare 绝不隐式沿用
        # 已有的 enterprise，也不改变其身份。
        if (writable or self._enterprise_id is not None) and enterprise_id != self._enterprise_id:
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
            self._path.as_uri() + ("?mode=rw" if create else "?mode=ro"),
            uri=True,
            timeout=1,
        )
        try:
            if self._file() != identity:
                raise ValueError()
            if created:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(_TABLE)
                connection.execute(_CONTEXT_TABLE)
                connection.execute(_TERMINAL_INDEX)
                connection.execute(
                    "INSERT INTO journal_context VALUES (1,?,?)", (self._enterprise_id, _TOOL_NAME)
                )
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={_VERSION}")
                connection.commit()
                # 除 SQLite 数据外，还要持久化新的目录项。
                self._sync_directory(self._path.parent)
            self._schema(connection, writable=create)
            return connection
        except BaseException:
            connection.close()
            raise

    def _prepare(self, create):
        with self._lock:
            connection = self._open(create=create)
            connection.close()

    async def _prepare_async(self, create):
        failed = False
        try:
            await anyio.to_thread.run_sync(self._prepare, create, abandon_on_cancel=False)
        except Exception:
            failed = True
        if failed:
            raise BusinessError("journal_unavailable")

    async def prepare(self) -> None:
        """创建或校验属主私有的日志库，不记录任何业务事件。"""
        await self._prepare_async(True)

    async def check_ready(self) -> None:
        """只读的模式与文件系统检查；绝不创建或扫描查询事件。"""
        await self._prepare_async(False)

    def _record(self, values):
        with self._lock:
            connection = self._open(create=True)
            try:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                if (
                    values[1] == "query_started"
                    and connection.execute(
                        "SELECT 1 FROM query_events WHERE request_id=? LIMIT 1", (values[0],)
                    ).fetchone()
                ):
                    raise ValueError()
                connection.execute(
                    "INSERT INTO query_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", values
                )
                connection.commit()
            finally:
                connection.close()

    async def record(
        self,
        *,
        request_id: str,
        caller: Caller,
        employee_id: str | None,
        event: Event,
        code: str | None = None,
        upstream_attempted: bool = False,
    ) -> None:
        failed = False
        try:
            values = _validate(
                request_id,
                caller,
                employee_id,
                event,
                code,
                upstream_attempted,
                self._enterprise_id,
            )
            await anyio.to_thread.run_sync(self._record, values, abandon_on_cancel=False)
        except Exception:
            failed = True
        if failed:
            # 在处理器外抛出，避免驱动/文件异常作为上下文保留，
            # 其中可能携带路径、SQL 或调用者信息。
            raise BusinessError("journal_unavailable")

    def _pending(self):
        with self._lock:
            connection = self._open()
            try:
                return [
                    row[0]
                    for row in connection.execute(
                        "SELECT request_id FROM query_events WHERE event='query_started' "
                        "AND NOT EXISTS (SELECT 1 FROM query_events terminal "
                        "WHERE terminal.request_id=query_events.request_id "
                        "AND terminal.event<>'query_started') ORDER BY request_id"
                    )
                ]
            finally:
                connection.close()

    async def pending_request_ids(self) -> list[str]:
        """只读取已有证据；缺失或失败绝不授予重试权利。"""
        failed = False
        try:
            result = await anyio.to_thread.run_sync(self._pending, abandon_on_cancel=False)
        except Exception:
            failed = True
        if failed:
            raise BusinessError("journal_unavailable")
        return result
