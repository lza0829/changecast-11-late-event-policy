from __future__ import annotations

from typing import Any

import sqlite3


def insert_batch(
    conn: sqlite3.Connection,
    *,
    batch_no: str,
    stream_id: int,
    status: str,
    counts: dict[str, int],
    batch_kind: str,
    late_policy: str,
    watermark_before: int,
    watermark_after: int,
    note: str | None,
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO project_batches(
            batch_no, stream_id, status, source_events, applied_upsert, applied_delete,
            skipped_duplicate, projected_alive, projected_deleted, max_seq,
            batch_kind, late_policy, watermark_before, watermark_after,
            late_seen, late_rejected, late_routed, late_applied, late_ineffective,
            note, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_no,
            stream_id,
            status,
            counts.get("source_events", 0),
            counts.get("applied_upsert", 0),
            counts.get("applied_delete", 0),
            counts.get("skipped_duplicate", 0),
            counts.get("projected_alive", 0),
            counts.get("projected_deleted", 0),
            counts.get("max_seq", 0),
            batch_kind,
            late_policy,
            watermark_before,
            watermark_after,
            counts.get("late_seen", 0),
            counts.get("late_rejected", 0),
            counts.get("late_routed", 0),
            counts.get("late_applied", 0),
            counts.get("late_ineffective", 0),
            note,
            created_at,
        ),
    )
    return int(cur.lastrowid)


def list_batches(
    conn: sqlite3.Connection, limit: int = 50, kind: str | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM project_batches WHERE 1=1"
    params: list[Any] = []
    if kind is not None:
        sql += " AND batch_kind=?"
        params.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM project_batches WHERE id=?", (batch_id,)).fetchone()
    return dict(r) if r else None


def insert_applied_event(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    stream_id: int,
    event: dict[str, Any],
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO applied_events(
            batch_id, stream_id, event_id, entity_key, op, seq, event_time,
            result, is_late, entity_seq_before, detail, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_id,
            stream_id,
            event["event_id"],
            event["entity_key"],
            event["op"],
            event["seq"],
            event["event_time"],
            event["result"],
            1 if event.get("is_late") else 0,
            event.get("entity_seq_before"),
            event.get("detail"),
            created_at,
        ),
    )


def list_applied_events(
    conn: sqlite3.Connection,
    *,
    batch_id: int | None = None,
    stream_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM applied_events WHERE 1=1"
    params: list[Any] = []
    if batch_id is not None:
        sql += " AND batch_id=?"
        params.append(batch_id)
    if stream_id is not None:
        sql += " AND stream_id=?"
        params.append(stream_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
