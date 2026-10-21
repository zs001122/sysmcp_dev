"""四接口耐久审计和跨动作目标占用，不保存业务请求或响应。

复用本项目 SQLiteResetStore 的私有路径、文件及异常保护；使用独立 SYF1
模式与真实动作名称。历史 SYA1 v2 仅只读核验，不迁移、解锁或支持旧入口并行写入。
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .account_write_store import SQLiteAccountWriteStore
from .errors import ERROR_MESSAGES
from .interfaces import EmployeeUpdateRequest
from .models import BusinessError, Caller, Identifier, Model
from .reset_store import SQLiteResetStore, _operation_id, _text

_ACTIONS = frozenset({"employee_query", "employee_create", "employee_update", "account_maintain"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CONTEXT = """CREATE TABLE function_context (
 id INTEGER PRIMARY KEY CHECK(id=1),
 enterprise_id TEXT NOT NULL,
 legacy_path TEXT,
 legacy_required INTEGER NOT NULL CHECK(legacy_required IN (0,1))
)"""
_OPERATIONS = """CREATE TABLE function_operations (
 request_id TEXT PRIMARY KEY,
 issuer TEXT NOT NULL,
 subject TEXT NOT NULL,
 client_id TEXT NOT NULL,
 enterprise_id TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action IN
 ('employee_query','employee_create','employee_update','account_maintain')),
 employee_ids TEXT NOT NULL,
 account_ids TEXT NOT NULL,
 binding_version TEXT NOT NULL,
 request_digest TEXT NOT NULL,
 is_write INTEGER NOT NULL CHECK(is_write IN (0,1)),
 state TEXT NOT NULL CHECK(state IN ('started','dispatched','succeeded','failed','unknown')),
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 code TEXT
)"""
_EVENTS = """CREATE TABLE function_events (
 request_id TEXT NOT NULL REFERENCES function_operations(request_id),
 event TEXT NOT NULL CHECK(event IN ('started','dispatched','succeeded','failed','unknown')),
 occurred_at REAL NOT NULL,
 code TEXT,
 PRIMARY KEY(request_id,event)
)"""


def _targets(values):
    if type(values) is not tuple or len(values) > 100:
        raise ValueError()
    if any(type(value) is not str or not _IDENTIFIER.fullmatch(value) for value in values):
        raise ValueError()
    if len(set(values)) != len(values):
        raise ValueError()
    return tuple(sorted(values))


class LegacyStatusRetry(Model):
    """明确复核的一次性状态写入，不更改或解除原操作的 unknown 事实。"""

    operation_id: str
    retry_request_id: str
    enterprise_id: str = Field(min_length=1, max_length=256)
    employee_id: Identifier
    account_id: Identifier
    status: Literal["Y", "N"]
    legacy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_by: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def controlled_review(self):
        _operation_id(self.operation_id)
        _operation_id(self.retry_request_id)
        if self.operation_id == self.retry_request_id:
            raise ValueError("retry requires a distinct request ID")
        _text(self.enterprise_id, 256)
        _text(self.approved_by, 512)
        _text(self.reason, 1024)
        return self

    @property
    def request_digest(self) -> str:
        """摘要算法与 functions.request_digest 一致，不接受额外更新字段。"""
        request = EmployeeUpdateRequest.model_validate(
            {"employee": [{"employee_no": self.employee_id, "status": self.status}]}
        )
        body = json.dumps(
            {"action": "employee_update", "parameter": request.parameter()},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(body.encode()).hexdigest()


class ReviewedStatusRecord(Model):
    """复核的原记录及全部事件摘要；不改写其 unknown 状态。"""

    source: Literal["legacy", "function"]
    request_id: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def controlled_record(self):
        _operation_id(self.request_id)
        return self


class ReviewedStatusContinuation(Model):
    """显式员工状态用例的有限续测许可，不是通用解除占用接口。"""

    enterprise_id: str = Field(min_length=1, max_length=256)
    employee_id: Identifier
    approved_by: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=1024)
    request_digests: tuple[str, ...] = Field(min_length=1, max_length=16)
    records: tuple[ReviewedStatusRecord, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def controlled_review(self):
        _text(self.enterprise_id, 256)
        _text(self.approved_by, 512)
        _text(self.reason, 1024)
        if any(not _DIGEST.fullmatch(value) for value in self.request_digests):
            raise ValueError("invalid request digest")
        if len(set(self.request_digests)) != len(self.request_digests):
            raise ValueError("duplicate request digest")
        if len({record.request_id for record in self.records}) != len(self.records):
            raise ValueError("duplicate reviewed record")
        return self


class LegacyEmployeeUpdateRecord(Model):
    """旧库中已核实为员工档案更新的记录；只纠正对象分类，不改变原终态。"""

    request_id: str
    enterprise_id: str = Field(min_length=1, max_length=256)
    employee_id: Identifier
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def controlled_record(self):
        _operation_id(self.request_id)
        _text(self.enterprise_id, 256)
        return self


class FunctionAudit(SQLiteResetStore):
    """单企业审计；未决写操作占用任一交集目标，查询既不占用也不被阻断。

    新库操作由 BEGIN IMMEDIATE 串行化；unknown 不可改写，无自动重试/解锁。
    配置历史库时必须停用旧入口的新增写操作，本库只协调自身写事务。
    """

    _application_id = 0x53594631
    _version = 1
    _schema_statements = (_CONTEXT, _OPERATIONS, _EVENTS)
    _schema_definition = {
        ("table", "function_context", _CONTEXT),
        ("table", "function_operations", _OPERATIONS),
        ("table", "function_events", _EVENTS),
    }

    def __init__(
        self,
        path: Path,
        *,
        enterprise_id: str,
        legacy_write_path: Path | None = None,
        legacy_status_retry: LegacyStatusRetry | None = None,
        status_continuation: ReviewedStatusContinuation | None = None,
        legacy_employee_updates: tuple[LegacyEmployeeUpdateRecord, ...] = (),
    ):
        super().__init__(path, enterprise_id=enterprise_id)
        self._legacy_status_retry = None
        self._status_continuation = None
        self._legacy_employee_updates = ()
        try:
            if type(legacy_employee_updates) is not tuple:
                raise ValueError()
            classified = tuple(
                LegacyEmployeeUpdateRecord.model_validate(record.model_dump(warnings="error"))
                for record in legacy_employee_updates
                if type(record) is LegacyEmployeeUpdateRecord
            )
            if len(classified) != len(legacy_employee_updates) or len(classified) > 32:
                raise ValueError()
            if len({record.request_id for record in classified}) != len(classified):
                raise ValueError()
            if any(record.enterprise_id != self._enterprise_id for record in classified):
                raise ValueError()
            if classified and legacy_write_path is None:
                raise ValueError()
            self._legacy_employee_updates = classified
        except (TypeError, ValueError):
            raise BusinessError("journal_unavailable") from None
        if status_continuation is not None:
            try:
                if (
                    type(status_continuation) is not ReviewedStatusContinuation
                    or legacy_status_retry is not None
                ):
                    raise ValueError()
                continuation = ReviewedStatusContinuation.model_validate(
                    status_continuation.model_dump(warnings="error")
                )
                if continuation.enterprise_id != self._enterprise_id or (
                    legacy_write_path is None
                    and any(record.source == "legacy" for record in continuation.records)
                ):
                    raise ValueError()
                self._status_continuation = continuation
            except (TypeError, ValueError):
                raise BusinessError("journal_unavailable") from None
        if legacy_status_retry is not None:
            try:
                if type(legacy_status_retry) is not LegacyStatusRetry or legacy_write_path is None:
                    raise ValueError()
                review = LegacyStatusRetry.model_validate(
                    legacy_status_retry.model_dump(warnings="error")
                )
                if review.enterprise_id != self._enterprise_id:
                    raise ValueError()
                self._legacy_status_retry = review
            except (TypeError, ValueError):
                raise BusinessError("journal_unavailable") from None
        self._legacy = (
            SQLiteAccountWriteStore(
                legacy_write_path, enterprise_id=enterprise_id, action="reset_password"
            )
            if legacy_write_path is not None
            else None
        )
        if self._legacy is not None and self._legacy._path == self._path:
            raise BusinessError("journal_unavailable")

    @property
    def _legacy_path(self):
        return str(self._legacy._path) if self._legacy else None

    def _legacy_open(self, *, required=False):
        if self._legacy is None:
            if required:
                raise ValueError()
            return None
        path = self._legacy._path
        try:
            path.lstat()
        except FileNotFoundError:
            # 新安装允许缺少旧库，遗留附属文件或已登记旧库丢失则拒绝。
            for directory in (*reversed(path.parent.parents), path.parent):
                try:
                    directory.lstat()
                except FileNotFoundError:
                    break
                if directory.is_symlink() or not directory.is_dir():
                    raise ValueError() from None
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    path.with_name(path.name + suffix).lstat()
                except FileNotFoundError:
                    continue
                raise ValueError() from None
            if required:
                raise ValueError() from None
            return None
        connection = self._legacy._open()  # mode=ro，严格 SYA1 v2，无 prepare/迁移。
        try:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError()
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError()
            if connection.execute(
                "SELECT 1 FROM reset_operations WHERE enterprise_id<>? LIMIT 1",
                (self._enterprise_id,),
            ).fetchone():
                raise ValueError()
            fields = (
                "issuer",
                "subject",
                "client_id",
                "enterprise_id",
                "employee_id",
                "account_id",
                "tool_name",
                "binding_version",
            )
            mismatch = " OR ".join(f"e.{field}<>o.{field}" for field in fields)
            if connection.execute(
                "SELECT 1 FROM reset_events e JOIN reset_operations o "
                "ON e.operation_id=o.operation_id WHERE " + mismatch + " LIMIT 1"
            ).fetchone():
                raise ValueError()
            return connection
        except BaseException:
            connection.close()
            raise

    def _schema(self, connection):
        actual = set(
            connection.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        )
        context = connection.execute("SELECT * FROM function_context").fetchall()
        if (
            actual != self._schema_definition
            or connection.execute("PRAGMA application_id").fetchone()[0] != self._application_id
            or connection.execute("PRAGMA user_version").fetchone()[0] != self._version
            or len(context) != 1
            or context[0][:3] != (1, self._enterprise_id, self._legacy_path)
            or context[0][3] not in (0, 1)
        ):
            raise ValueError()
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError()
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError()
        if connection.execute(
            "SELECT 1 FROM function_operations WHERE enterprise_id<>? "
            "OR is_write<>(action<>'employee_query') LIMIT 1",
            (self._enterprise_id,),
        ).fetchone():
            raise ValueError()

    def _open(self, *, create=False, writable=False):
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
            self._path.as_uri() + ("?mode=rw" if create or writable else "?mode=ro"),
            uri=True,
            timeout=1,
        )
        try:
            if self._file() != identity:
                raise ValueError()
            connection.execute("PRAGMA foreign_keys=ON")
            if create or writable:
                connection.execute("PRAGMA synchronous=FULL")
            if created:
                connection.execute("BEGIN IMMEDIATE")
                for statement in self._schema_statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO function_context VALUES (1,?,?,0)",
                    (self._enterprise_id, self._legacy_path),
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

    @staticmethod
    def _legacy_required(connection):
        row = connection.execute("SELECT legacy_required FROM function_context").fetchone()
        return bool(row[0])

    def _prepare(self, create):
        with self._lock:
            connection = self._open(create=create)
            try:
                legacy = self._legacy_open(required=self._legacy_required(connection))
                if legacy is not None:
                    legacy.close()
                    if create:
                        with connection:
                            connection.execute("UPDATE function_context SET legacy_required=1")
            finally:
                connection.close()

    async def prepare(self) -> None:
        await self._run(self._prepare, True)

    async def check_ready(self) -> None:
        await self._run(self._prepare, False)

    @staticmethod
    def _legacy_subject(connection, operation_id):
        _operation_id(operation_id)
        cursor = connection.execute(
            "SELECT * FROM reset_operations WHERE operation_id=?", (operation_id,)
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError()
        operation = dict(zip((field[0] for field in cursor.description), row, strict=True))
        cursor = connection.execute(
            "SELECT * FROM reset_events WHERE operation_id=? ORDER BY rowid", (operation_id,)
        )
        columns = [field[0] for field in cursor.description]
        events = [dict(zip(columns, row, strict=True)) for row in cursor]
        body = json.dumps(
            {"operation": operation, "events": events},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return {
            key: operation[key]
            for key in (
                "operation_id",
                "enterprise_id",
                "employee_id",
                "account_id",
                "state",
                "tool_name",
                "code",
            )
        } | {"fingerprint": hashlib.sha256(body.encode()).hexdigest()}

    def _legacy_review_subject(self, operation_id):
        with self._lock:
            connection = self._legacy_open(required=True)
            try:
                # Keep the operation and its complete event history in one read snapshot.
                connection.execute("BEGIN")
                return self._legacy_subject(connection, operation_id)
            finally:
                connection.close()

    async def legacy_review_subject(self, operation_id: str) -> dict:
        """只读返回形成复核许可所需的目标与完整旧记录摘要，不改写旧事实。"""
        return await self._run(self._legacy_review_subject, operation_id)

    @staticmethod
    def _review_subject(connection, request_id):
        _operation_id(request_id)
        cursor = connection.execute(
            "SELECT * FROM function_operations WHERE request_id=?", (request_id,)
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError()
        operation = dict(zip((field[0] for field in cursor.description), row, strict=True))
        cursor = connection.execute(
            "SELECT * FROM function_events WHERE request_id=? ORDER BY rowid", (request_id,)
        )
        columns = [field[0] for field in cursor.description]
        events = [dict(zip(columns, row, strict=True)) for row in cursor]
        body = json.dumps(
            {"operation": operation, "events": events},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return {
            key: operation[key]
            for key in ("request_id", "enterprise_id", "action", "state", "code")
        } | {
            "employee_ids": list(_targets(tuple(json.loads(operation["employee_ids"])))),
            "account_ids": list(_targets(tuple(json.loads(operation["account_ids"])))),
            "fingerprint": hashlib.sha256(body.encode()).hexdigest(),
        }

    def _read_review_subject(self, request_id):
        with self._lock:
            connection = self._open()
            try:
                connection.execute("BEGIN")
                return self._review_subject(connection, request_id)
            finally:
                connection.close()

    async def review_subject(self, request_id: str) -> dict:
        """只读取得当前操作及完整事件指纹，不变更原记录或创建库。"""
        return await self._run(self._read_review_subject, request_id)

    def _reviewed_current(self, connection, employee_ids, account_ids, action, digest):
        review = self._status_continuation
        if review is None:
            return frozenset()
        if (
            action != "employee_update"
            or employee_ids != (review.employee_id,)
            or account_ids != ()
            or digest not in review.request_digests
        ):
            raise BusinessError("reset_pending_review")
        reviewed = set()
        for record in review.records:
            if record.source != "function":
                continue
            try:
                prior = self._review_subject(connection, record.request_id)
            except ValueError:
                raise BusinessError("reset_pending_review") from None
            if (
                prior["enterprise_id"] != review.enterprise_id
                or prior["employee_ids"] != [review.employee_id]
                or prior["account_ids"] != []
                or prior["action"] != "employee_update"
                or prior["state"] != "unknown"
                or prior["fingerprint"] != record.fingerprint
            ):
                raise BusinessError("reset_pending_review")
            reviewed.add(record.request_id)
        return frozenset(reviewed)

    def _legacy_conflict(
        self,
        connection,
        request_id,
        employee_ids,
        account_ids,
        action,
        request_digest,
    ):
        review = self._legacy_status_retry
        continuation = self._status_continuation
        reviewed_records = (
            tuple(record for record in continuation.records if record.source == "legacy")
            if continuation is not None
            else ()
        )
        classified_employee_updates = {
            record.request_id: record for record in self._legacy_employee_updates
        }
        if review is not None and (
            request_id != review.retry_request_id
            or action != "employee_update"
            or employee_ids != (review.employee_id,)
            or account_ids != ()
            or request_digest != review.request_digest
        ):
            raise BusinessError("reset_pending_review")
        legacy = self._legacy_open(
            required=review is not None
            or bool(reviewed_records)
            or self._legacy_required(connection)
        )
        if legacy is None:
            return
        try:
            legacy.execute("BEGIN")
            connection.execute("UPDATE function_context SET legacy_required=1")
            if legacy.execute(
                "SELECT 1 FROM reset_operations WHERE operation_id=?", (request_id,)
            ).fetchone():
                raise BusinessError("reset_already_recorded")
            if review is not None:
                try:
                    prior = self._legacy_subject(legacy, review.operation_id)
                except ValueError:
                    raise BusinessError("reset_pending_review") from None
                if (
                    prior["enterprise_id"] != review.enterprise_id
                    or prior["employee_id"] != review.employee_id
                    or prior["account_id"] != review.account_id
                    or prior["state"] != "unknown"
                    or prior["tool_name"]
                    not in {
                        "enable_my_t100_account",
                        "disable_my_t100_account",
                    }
                    or prior["fingerprint"] != review.legacy_fingerprint
                ):
                    raise BusinessError("reset_pending_review")
            reviewed_ids = set()
            for record in reviewed_records:
                try:
                    prior = self._legacy_subject(legacy, record.request_id)
                except ValueError:
                    raise BusinessError("reset_pending_review") from None
                if (
                    prior["enterprise_id"] != continuation.enterprise_id
                    or prior["employee_id"] != continuation.employee_id
                    or prior["state"] != "unknown"
                    or prior["tool_name"]
                    not in {"enable_my_t100_account", "disable_my_t100_account"}
                    or prior["fingerprint"] != record.fingerprint
                ):
                    raise BusinessError("reset_pending_review")
                reviewed_ids.add(record.request_id)
            classified_ids = set()
            if action == "account_maintain":
                for record in classified_employee_updates.values():
                    try:
                        prior = self._legacy_subject(legacy, record.request_id)
                    except ValueError:
                        raise BusinessError("reset_pending_review") from None
                    if (
                        prior["enterprise_id"] != record.enterprise_id
                        or prior["employee_id"] != record.employee_id
                        or prior["state"] != "unknown"
                        or prior["tool_name"]
                        not in {"enable_my_t100_account", "disable_my_t100_account"}
                        or prior["fingerprint"] != record.fingerprint
                    ):
                        raise BusinessError("reset_pending_review")
                    classified_ids.add(record.request_id)
            for operation_id, employee_id, account_id in legacy.execute(
                "SELECT operation_id,employee_id,account_id FROM reset_operations "
                "WHERE state IN ('started','dispatched','unknown')"
            ):
                if employee_id in employee_ids or account_id in account_ids:
                    if action == "account_maintain" and operation_id in classified_ids:
                        continue
                    if review is not None and operation_id == review.operation_id:
                        continue
                    if operation_id in reviewed_ids:
                        continue
                    raise BusinessError("reset_pending_review")
        finally:
            legacy.close()

    def _start(
        self,
        request_id,
        caller,
        action,
        employee_ids,
        account_ids,
        binding_version,
        request_digest,
        write,
    ):
        _operation_id(request_id)
        if type(caller) is not Caller or type(action) is not str or action not in _ACTIONS:
            raise ValueError()
        caller = Caller.model_validate(caller.model_dump())
        _text(caller.issuer, 512)
        _text(caller.subject, 256)
        _text(caller.client_id, 256)
        _text(binding_version, 256)
        if type(request_digest) is not str or not _DIGEST.fullmatch(request_digest):
            raise ValueError()
        if type(write) is not bool or write != (action != "employee_query"):
            raise ValueError()
        employee_ids, account_ids = _targets(employee_ids), _targets(account_ids)
        if write and not (employee_ids or account_ids):
            raise ValueError()
        with self._lock:
            connection = self._open(writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM function_operations WHERE request_id=?", (request_id,)
                ).fetchone():
                    raise BusinessError("reset_already_recorded")
                if write:
                    reviewed_current = self._reviewed_current(
                        connection, employee_ids, account_ids, action, request_digest
                    )
                    self._legacy_conflict(
                        connection, request_id, employee_ids, account_ids, action, request_digest
                    )
                    for (
                        prior_id,
                        prior_action,
                        employees,
                        accounts,
                        prior_state,
                    ) in connection.execute(
                        "SELECT request_id,action,employee_ids,account_ids,state "
                        "FROM function_operations "
                        "WHERE is_write=1 AND state IN ('started','dispatched','unknown')"
                    ):
                        prior_employees = _targets(tuple(json.loads(employees)))
                        prior_accounts = _targets(tuple(json.loads(accounts)))
                        employee_overlap = bool(set(employee_ids).intersection(prior_employees))
                        account_overlap = bool(set(account_ids).intersection(prior_accounts))
                        employee_update_is_separate = (
                            action == "account_maintain"
                            and prior_action == "employee_update"
                            and prior_state == "unknown"
                            and not prior_accounts
                        )
                        if account_overlap or (
                            employee_overlap and not employee_update_is_separate
                        ):
                            if prior_id in reviewed_current:
                                continue
                            raise BusinessError("reset_pending_review")
                now = time.time()
                connection.execute(
                    "INSERT INTO function_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request_id,
                        caller.issuer,
                        caller.subject,
                        caller.client_id,
                        self._enterprise_id,
                        action,
                        json.dumps(employee_ids),
                        json.dumps(account_ids),
                        binding_version,
                        request_digest,
                        int(write),
                        "started",
                        now,
                        now,
                        None,
                    ),
                )
                connection.execute(
                    "INSERT INTO function_events VALUES (?,'started',?,NULL)", (request_id, now)
                )
                connection.commit()
            finally:
                connection.close()

    async def start(
        self,
        *,
        request_id: str,
        caller: Caller,
        action: str,
        employee_ids: tuple[str, ...],
        account_ids: tuple[str, ...],
        binding_version: str,
        request_digest: str,
        write: bool,
    ) -> None:
        await self._run(
            self._start,
            request_id,
            caller,
            action,
            employee_ids,
            account_ids,
            binding_version,
            request_digest,
            write,
        )

    def _transition(self, request_id, outcome, code):
        _operation_id(request_id)
        if type(outcome) is not str or outcome not in {
            "dispatched",
            "succeeded",
            "failed",
            "unknown",
        }:
            raise ValueError()
        if code is not None and (type(code) is not str or code not in ERROR_MESSAGES):
            raise ValueError()
        if outcome in {"dispatched", "succeeded"} and code is not None:
            raise ValueError()
        with self._lock:
            connection = self._open(writable=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state,is_write FROM function_operations WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                allowed = (
                    {"started", "dispatched"}
                    if outcome == "failed"
                    else {"started" if outcome == "dispatched" else "dispatched"}
                )
                if row is None or row[0] not in allowed or (outcome == "unknown" and not row[1]):
                    raise ValueError()
                now = time.time()
                connection.execute(
                    "UPDATE function_operations SET state=?,updated_at=?,code=? WHERE request_id=?",
                    (outcome, now, code, request_id),
                )
                connection.execute(
                    "INSERT INTO function_events VALUES (?,?,?,?)", (request_id, outcome, now, code)
                )
                connection.commit()
            finally:
                connection.close()

    async def dispatched(self, request_id: str) -> None:
        await self._run(self._transition, request_id, "dispatched", None)

    async def finish(
        self,
        request_id: str,
        outcome: Literal["succeeded", "failed", "unknown"],
        code: str | None = None,
    ) -> None:
        if type(outcome) is not str or outcome not in {"succeeded", "failed", "unknown"}:
            raise BusinessError("journal_unavailable")
        await self._run(self._transition, request_id, outcome, code)

    def _status(self, request_id):
        _operation_id(request_id)
        with self._lock:
            connection = self._open()
            try:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT * FROM function_operations WHERE request_id=?", (request_id,)
                ).fetchone()
                if row is None:
                    raise ValueError()
                result = dict(row)
                result["employee_ids"] = list(_targets(tuple(json.loads(row["employee_ids"]))))
                result["account_ids"] = list(_targets(tuple(json.loads(row["account_ids"]))))
                result["write"] = bool(result.pop("is_write"))
                result["events"] = [
                    dict(event)
                    for event in connection.execute(
                        "SELECT event,occurred_at,code FROM function_events "
                        "WHERE request_id=? ORDER BY rowid",
                        (request_id,),
                    )
                ]
                result["operation_state"] = result.pop("state")
                result["recorded_possible_dispatch"] = any(
                    event["event"] == "dispatched" for event in result["events"]
                )
                result["upstream_attempted"] = result["recorded_possible_dispatch"]
                return result
            finally:
                connection.close()

    async def status(self, request_id: str) -> dict:
        return await self._run(self._status, request_id)
