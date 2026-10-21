# Historical regression support relocated from scripts; not a supported command.
"""Read-only write-plan status; configuration permission is not execution readiness."""

import os
from pathlib import Path

import anyio
from internal_support import _directory

from biz_t100.account_write_store import SQLiteAccountWriteStore
from biz_t100.reset_store import _code, _operation_id

_PENDING = {"started", "dispatched", "unknown"}
_TERMINAL = {"succeeded", "rejected", "not_sent"}


def _database_exists(path: Path) -> bool:
    """Only a genuinely absent main file in a private directory means no history."""
    directory = _directory(path.parent)
    try:
        try:
            os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            # A missing main file alongside journal material is not a fresh store.
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    os.stat(path.name + suffix, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise ValueError() from None
            return False
        return True
    finally:
        os.close(directory)


def _snapshot(store, saved, operation_id):
    from live_write import CLIENT, ISSUER, PROFILES

    identity = store._header()
    connection = store._open(create=False)
    try:
        connection.execute("BEGIN")
        store._schema(connection)
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError()
        fields = (
            "enterprise_id",
            "employee_id",
            "account_id",
            "tool_name",
            "issuer",
            "subject",
            "client_id",
            "state",
            "code",
            "binding_version",
        )
        raw = connection.execute(
            "SELECT " + ",".join(fields) + " FROM reset_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        row = dict(zip(fields, raw, strict=True)) if raw else None
        if row is not None:
            expected = {
                "enterprise_id": saved.enterprise_id,
                "employee_id": saved.plan.employee_id,
                "account_id": saved.plan.account_id,
                "tool_name": PROFILES[saved.plan.action][0],
                "issuer": ISSUER,
                "subject": operation_id,
                "client_id": CLIENT,
            }
            if any(row[key] != value for key, value in expected.items()):
                raise ValueError()
            if row["state"] not in _PENDING | _TERMINAL:
                raise ValueError()
            _code(row["code"])
        pending = [
            _operation_id(item[0])
            for item in connection.execute(
                "SELECT operation_id FROM reset_operations WHERE enterprise_id=? "
                "AND (employee_id=? OR account_id=?) "
                "AND state IN ('started','dispatched','unknown') ORDER BY operation_id",
                (saved.enterprise_id, saved.plan.employee_id, saved.plan.account_id),
            )
        ]
        if store._file() != identity:
            raise ValueError()
        return row, pending
    finally:
        connection.close()


async def inspect_status(state, root, operation_id):
    """Inspect plan permission and shared pending locks without reading credentials.

    This snapshot never reserves an operation, changes a lock, or proves a business
    effect. Actual calls must still re-authorize and reserve atomically themselves.
    """
    from live_write import _load, preview

    result = preview(state, root, operation_id)
    saved, _, binding = _load(state, root, operation_id)
    configuration_blocker = result["blocker"]
    result.update(
        configuration_blocker=configuration_blocker,
        execution_blocker=None,
        blocking_operation_ids=[],
        operation_state="not_recorded",
        recorded_possible_dispatch=False,
        upstream_error=None,
        business_effect_verified=False,
    )
    try:
        path = Path(root) / "audit" / "account-write.sqlite3"
        if await anyio.to_thread.run_sync(_database_exists, path):
            store = SQLiteAccountWriteStore(
                path, enterprise_id=saved.enterprise_id, action=saved.plan.action
            )
            await store.check_ready()
            row, pending = await anyio.to_thread.run_sync(_snapshot, store, saved, operation_id)
            result["blocking_operation_ids"] = pending
            if row is not None:
                result.update(
                    operation_state=row["state"],
                    recorded_possible_dispatch=row["state"] not in {"started", "not_sent"},
                    upstream_error=row["code"],
                    authorization_binding_matches=row["binding_version"] == binding,
                )
                result["execution_blocker"] = (
                    "reset_pending_review" if row["state"] in _PENDING else "reset_already_recorded"
                )
                result["status"] = "blocked" if row["state"] in _PENDING else "completed"
            elif pending:
                result.update(status="blocked", execution_blocker="reset_pending_review")
        if configuration_blocker and result["status"] == "planned":
            result["status"] = "blocked"
        result["blocker"] = configuration_blocker or result["execution_blocker"]
        result["ready_for_explicit_call"] = result["blocker"] is None
        return result
    except Exception:
        # No private path, SQL text, corrupted database content, or driver error escapes.
        result.update(
            status="failed",
            error="journal_unavailable",
            ready_for_explicit_call=False,
            blocker=configuration_blocker or "journal_unavailable",
            execution_blocker="journal_unavailable",
            operation_state="unavailable",
            recorded_possible_dispatch=None,
            upstream_error=None,
            blocking_operation_ids=[],
        )
        result.pop("authorization_binding_matches", None)
        return result
