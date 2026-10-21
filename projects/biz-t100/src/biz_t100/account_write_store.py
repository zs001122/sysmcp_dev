"""账号维护动作共用的持久化占用；与旧重置库、查询库严格分离。

复用当前项目 SQLiteResetStore 的私有文件保护、耐久事务、状态机与
双目标占用。不导入旧项目或另建一套去重实现。重置、启用与禁用必须使用同企业
同一路径；操作 UUID 全库唯一，unknown 跨动作持续占用员工和登录账号。
"""

import sqlite3
from pathlib import Path
from typing import Literal

from .models import BusinessError
from .reset_store import SQLiteResetStore

AccountWriteAction = Literal["reset_password", "disable_account", "enable_account"]
_TOOLS = {
    "reset_password": "reset_my_t100_password",
    "disable_account": "disable_my_t100_account",
    "enable_account": "enable_my_t100_account",
}
_CONTEXT = """CREATE TABLE reset_context (
 id INTEGER PRIMARY KEY CHECK(id=1),
 enterprise_id TEXT NOT NULL,
 tool_name TEXT NOT NULL CHECK(tool_name='t100_account_maintenance')
)"""
_V1_OPERATIONS = """CREATE TABLE reset_operations (
 operation_id TEXT PRIMARY KEY,
 issuer TEXT NOT NULL,
 subject TEXT NOT NULL,
 client_id TEXT NOT NULL,
 enterprise_id TEXT NOT NULL,
 employee_id TEXT NOT NULL,
 account_id TEXT NOT NULL,
 tool_name TEXT NOT NULL CHECK(tool_name IN
 ('reset_my_t100_password','disable_my_t100_account')),
 binding_version TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN
 ('started','dispatched','succeeded','rejected','not_sent','unknown')),
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 code TEXT
)"""
_V1_EVENTS = """CREATE TABLE reset_events (
 operation_id TEXT NOT NULL REFERENCES reset_operations(operation_id),
 issuer TEXT NOT NULL,
 subject TEXT NOT NULL,
 client_id TEXT NOT NULL,
 enterprise_id TEXT NOT NULL,
 employee_id TEXT NOT NULL,
 account_id TEXT NOT NULL,
 tool_name TEXT NOT NULL CHECK(tool_name IN
 ('reset_my_t100_password','disable_my_t100_account')),
 binding_version TEXT NOT NULL,
 event TEXT NOT NULL CHECK(event IN
 ('reset_started','reset_dispatched','reset_succeeded','reset_rejected','reset_not_sent','reset_unknown')),
 occurred_at REAL NOT NULL,
 code TEXT,
 PRIMARY KEY(operation_id,event)
)"""
# 与基类相同的表名使事务和状态机可直接复用。索引不含 tool_name，
# 因而同一目标不能通过切换动作、调用者、UUID 或映射版本绕过未决占用。
_EMPLOYEE_INDEX = """CREATE UNIQUE INDEX one_pending_employee
 ON reset_operations(enterprise_id,employee_id)
 WHERE state IN ('started','dispatched','unknown')"""
_ACCOUNT_INDEX = """CREATE UNIQUE INDEX one_pending_account
 ON reset_operations(enterprise_id,account_id)
 WHERE state IN ('started','dispatched','unknown')"""


# 保留逐字历史模式用于唯一允许的版本迁移，不能接受“相似”表或自行修复坏库。
_OLD_TOOLS = "('reset_my_t100_password','disable_my_t100_account')"
_NEW_TOOLS = "('reset_my_t100_password','disable_my_t100_account','enable_my_t100_account')"
_OPERATIONS = _V1_OPERATIONS.replace(_OLD_TOOLS, _NEW_TOOLS)
_EVENTS = _V1_EVENTS.replace(_OLD_TOOLS, _NEW_TOOLS)


class _VersionOneStore(SQLiteResetStore):
    """只用于核对原 SYA1 v1 模式；不向业务暴露写入或恢复入口。"""

    _application_id = 0x53594131
    _version = 1
    _context_tool_name = "t100_account_maintenance"
    _schema_definition = {
        ("table", "reset_context", _CONTEXT),
        ("table", "reset_operations", _V1_OPERATIONS),
        ("table", "reset_events", _V1_EVENTS),
        ("index", "one_pending_employee", _EMPLOYEE_INDEX),
        ("index", "one_pending_account", _ACCOUNT_INDEX),
    }


class SQLiteAccountWriteStore(SQLiteResetStore):
    """共享账号维护库的单动作视图；调用者只能推进该动作自己的记录。

    pending_operation_ids 返回三个动作的全部未决 ID，供本地检查共同的阻塞。
    事件沿用 reset_* 状态名称和错误码；具体动作以 tool_name 为准。
    succeeded 仍仅表示收到 ACK，不是密码或禁用状态已读回验证。
    """

    _application_id = 0x53594131  # SYA1；不同于查询 SYT1 和旧重置 SYR1。
    _version = 2
    _context_tool_name = "t100_account_maintenance"
    _schema_statements = (_CONTEXT, _OPERATIONS, _EVENTS, _EMPLOYEE_INDEX, _ACCOUNT_INDEX)
    _schema_definition = {
        ("table", "reset_context", _CONTEXT),
        ("table", "reset_operations", _OPERATIONS),
        ("table", "reset_events", _EVENTS),
        ("index", "one_pending_employee", _EMPLOYEE_INDEX),
        ("index", "one_pending_account", _ACCOUNT_INDEX),
    }

    def __init__(self, path: Path, *, enterprise_id: str, action: AccountWriteAction):
        if type(action) is not str or action not in _TOOLS:
            raise BusinessError("journal_unavailable")
        self._tool_name = _TOOLS[action]
        super().__init__(path, enterprise_id=enterprise_id)

    def _migrate_version_one(self):
        # 正常 v2 路径仍由基类逐字检查模式；只有显式 prepare 会到达本分支。
        try:
            self._header()
            return
        except ValueError:
            legacy = _VersionOneStore(self._path, enterprise_id=self._enterprise_id)
            try:
                identity = legacy._header()
            except ValueError:
                # 其它进程可能恰在两次 header 检查之间完成升级。
                # 仍只接受完整 v2 标识；其余异常继续拒绝。
                self._header()
                return
        connection = sqlite3.connect(self._path.as_uri() + "?mode=rw", uri=True, timeout=1)
        try:
            if self._file() != identity:
                raise ValueError()
            connection.execute("PRAGMA synchronous=FULL")
            # 事务内原名重建两张表，避免重命名生成不同 SQL 或修改外键目标。
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA user_version").fetchone()[0] == self._version:
                # 另一个 prepare 已完成迁移；在锁内校验后退出，不重复复制。
                self._schema(connection)
                connection.rollback()
                return
            legacy._schema(connection)
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError()
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError()
            for table in ("reset_operations", "reset_events"):
                if connection.execute(
                    f"SELECT 1 FROM {table} WHERE enterprise_id<>? LIMIT 1",
                    (self._enterprise_id,),
                ).fetchone():
                    raise ValueError()
            # 审计身份必须仍然对应原操作，不能迁移时把错关联“固化”为合格证据。
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
            connection.execute(
                "CREATE TEMP TABLE prior_operations AS SELECT rowid AS legacy_rowid,* "
                "FROM reset_operations"
            )
            connection.execute(
                "CREATE TEMP TABLE prior_events AS SELECT rowid AS legacy_rowid,* FROM reset_events"
            )
            connection.execute("DROP TABLE reset_events")
            connection.execute("DROP TABLE reset_operations")
            connection.execute(_OPERATIONS)
            connection.execute(_EVENTS)
            connection.execute(
                "INSERT INTO reset_operations(rowid,operation_id,issuer,subject,client_id,"
                "enterprise_id,employee_id,account_id,tool_name,binding_version,state,"
                "created_at,updated_at,code) SELECT * FROM prior_operations"
            )
            connection.execute(
                "INSERT INTO reset_events(rowid,operation_id,issuer,subject,client_id,"
                "enterprise_id,employee_id,account_id,tool_name,binding_version,event,"
                "occurred_at,code) SELECT * FROM prior_events"
            )
            connection.execute(_EMPLOYEE_INDEX)
            connection.execute(_ACCOUNT_INDEX)
            connection.execute("DROP TABLE prior_events")
            connection.execute("DROP TABLE prior_operations")
            connection.execute(f"PRAGMA user_version={self._version}")
            self._schema(connection)
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError()
            if self._file() != identity:
                raise ValueError()
            # 原库原 inode 就地升级；目录同步失败时事务仍可回滚。
            # SQLite FULL commit 同步 rollback journal 与主文件后才返回成功。
            self._sync_directory(self._path.parent)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _prepare(self, create):
        with self._lock:
            self._directory(create=create)
            if create:
                try:
                    self._path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    self._migrate_version_one()
            connection = self._open(create=create)
            connection.close()

    async def prepare(self) -> None:
        """显式创建 v2 或原子升级已核对的 v1；历史事件和未决占用保持不变。"""
        await self._run(self._prepare, True)
