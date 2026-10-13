"""运行步骤、引用明细和操作记录的持久化辅助函数。"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

from gitwarden.db import Store, now
from gitwarden.i18n import redact

MAX_RAW_BYTES = 64 * 1024


def redact_text(store: Store, text: str | None) -> str | None:
    """在写入日志字段前移除当前 Store 中的所有凭据值。"""
    if text is None:
        return None
    return redact(text, store.secret_values())


def truncate_utf8(text: str | None, max_bytes: int = MAX_RAW_BYTES) -> str | None:
    """按 UTF-8 字节数保留末尾，把截断字节数标记放在开头。"""
    if text is None:
        return None
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text

    keep = max_bytes - len("…[truncated 0 bytes]".encode())
    keep = max(0, keep)
    while True:
        tail = encoded[len(encoded) - keep :].decode("utf-8", errors="ignore")
        kept_bytes = len(tail.encode("utf-8"))
        omitted = len(encoded) - kept_bytes
        marker = f"…[truncated {omitted} bytes]"
        if kept_bytes + len(marker.encode("utf-8")) <= max_bytes:
            return marker + tail
        keep = max(0, keep - max(1, kept_bytes + len(marker.encode("utf-8")) - max_bytes))


def _prepare(store: Store, text: str | None, *, truncate: bool = False) -> str | None:
    text = redact_text(store, text)
    return truncate_utf8(text) if truncate else text


def add_step(
    store: Store,
    run_id: int,
    step: str,
    message: str | None,
    *,
    level: str = "info",
    target_id: int | None = None,
    duration_ms: int | None = None,
    raw: str | None = None,
) -> int:
    """追加运行步骤；message/raw 在写入前统一脱敏，raw 另有字节上限。"""
    return store._exec(  # noqa: SLF001 - 日志表只通过此模块写入
        "INSERT INTO run_steps(run_id,at,level,step,target_id,message,duration_ms,raw) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            run_id,
            now(),
            level,
            step,
            target_id,
            _prepare(store, message),
            duration_ms,
            _prepare(store, raw, truncate=True),
        ),
    )


def add_ref(
    store: Store,
    run_id: int,
    ref: str,
    kind: str,
    old: str | None,
    new: str | None,
) -> int:
    return store._exec(  # noqa: SLF001 - 日志表只通过此模块写入
        "INSERT INTO run_refs(run_id,ref,kind,old,new) VALUES(?,?,?,?,?)",
        (run_id, ref, kind, old, new),
    )


def add_activity(
    store: Store,
    action: str,
    object: str | None,
    detail: str | None,
    ip: str | None,
) -> int:
    return store._exec(  # noqa: SLF001 - 日志表只通过此模块写入
        "INSERT INTO activity(at,action,object,detail,ip) VALUES(?,?,?,?,?)",
        (now(), action, object, _prepare(store, detail), ip),
    )


def list_steps(store: Store, run_id: int) -> list[dict[str, Any]]:
    return store._rows(  # noqa: SLF001 - 统一复用 Store 的连接和行解码
        "SELECT * FROM run_steps WHERE run_id=? ORDER BY id", (run_id,)
    )


def list_refs(store: Store, run_id: int) -> list[dict[str, Any]]:
    return store._rows(  # noqa: SLF001 - 统一复用 Store 的连接和行解码
        "SELECT * FROM run_refs WHERE run_id=? ORDER BY id", (run_id,)
    )


def _page(page: int, page_size: int) -> tuple[int, int]:
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    return (page - 1) * page_size, page_size


def _cutoff(days: int | None) -> str | None:
    if days is None:
        return None
    if days < 0:
        raise ValueError("days 不能为负数")
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def query_runs(
    store: Store,
    *,
    task_id: int | None = None,
    status: str | None = None,
    days: int | None = None,
    search: str = "",
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    args: list[Any] = []
    if task_id is not None:
        where.append("r.task_id=?")
        args.append(task_id)
    if status:
        where.append("r.status=?")
        args.append(status)
    cutoff = _cutoff(days)
    if cutoff is not None:
        where.append("COALESCE(r.finished_at,r.started_at)>=?")
        args.append(cutoff)
    if search:
        term = f"%{search}%"
        where.append(
            "(r.message LIKE ? COLLATE NOCASE "
            "OR EXISTS (SELECT 1 FROM run_steps s "
            "WHERE s.run_id=r.id AND s.message LIKE ? COLLATE NOCASE) "
            "OR EXISTS (SELECT 1 FROM run_refs f "
            "WHERE f.run_id=r.id AND f.ref LIKE ? COLLATE NOCASE))"
        )
        args.extend((term, term, term))
    clause = " WHERE " + " AND ".join(where) if where else ""
    count = store._one(  # noqa: SLF001 - 统一复用 Store 的连接和行解码
        f"SELECT count(*) AS n FROM runs r{clause}", tuple(args)
    )
    offset, limit = _page(page, page_size)
    rows = store._rows(  # noqa: SLF001 - 统一复用 Store 的连接和行解码
        "SELECT r.*, t.name AS task_name FROM runs r "
        "LEFT JOIN tasks t ON t.id=r.task_id"
        f"{clause} ORDER BY COALESCE(r.started_at,'') DESC, r.id DESC LIMIT ? OFFSET ?",
        (*args, limit, offset),
    )
    return rows, int(count["n"] if count else 0)


def query_activity(
    store: Store,
    *,
    category: str | None = None,
    days: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    args: list[Any] = []
    if category:
        where.append("(action=? OR action LIKE ? OR action LIKE ?)")
        args.extend((category, f"{category}.%", f"{category}_%"))
    cutoff = _cutoff(days)
    if cutoff is not None:
        where.append("at>=?")
        args.append(cutoff)
    clause = " WHERE " + " AND ".join(where) if where else ""
    count = store._one(  # noqa: SLF001 - 统一复用 Store 的连接和行解码
        f"SELECT count(*) AS n FROM activity{clause}", tuple(args)
    )
    offset, limit = _page(page, page_size)
    rows = store._rows(  # noqa: SLF001 - 统一复用 Store 的连接和行解码
        f"SELECT * FROM activity{clause} ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
        (*args, limit, offset),
    )
    return rows, int(count["n"] if count else 0)


def cleanup(store: Store, retention_days: int = 90) -> None:
    cutoff = _cutoff(retention_days)
    assert cutoff is not None
    # 让 SQLite 级联删除步骤和 refs。
    with closing(store._connect()) as conn, conn:  # noqa: SLF001
        conn.execute("DELETE FROM runs WHERE COALESCE(finished_at,started_at)<?", (cutoff,))
        conn.execute("DELETE FROM activity WHERE at<?", (cutoff,))
