from __future__ import annotations

import json
from typing import Any

import sqlite3


def upsert_projection_row(
    conn: sqlite3.Connection,
    *,
    stream_id: int,
    row: dict[str, Any],
    batch_id: int,
    updated_at: str,
) -> None:
    """Write one entity row.  The caller has already adjudicated that this
    write is allowed by the per-entity seq rule."""
    conn.execute(
        """
        INSERT INTO projection_rows(
            stream_id, entity_key, payload_json, applied_seq, applied_event_id,
            applied_batch_id, deleted, updated_at
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(stream_id, entity_key) DO UPDATE SET
            payload_json=excluded.payload_json,
            applied_seq=excluded.applied_seq,
            applied_event_id=excluded.applied_event_id,
            applied_batch_id=excluded.applied_batch_id,
            deleted=excluded.deleted,
            updated_at=excluded.updated_at
        """,
        (
            stream_id,
            row["entity_key"],
            json.dumps(row.get("payload"), ensure_ascii=False)
            if row.get("payload") is not None
            else None,
            row["applied_seq"],
            row["applied_event_id"],
            batch_id,
            1 if row.get("deleted") else 0,
            updated_at,
        ),
    )


def delete_stream_projection(conn: sqlite3.Connection, stream_id: int) -> None:
    conn.execute("DELETE FROM projection_rows WHERE stream_id=?", (stream_id,))


def list_projections(
    conn: sqlite3.Connection,
    *,
    stream_id: int | None = None,
    include_deleted: bool = True,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM projection_rows WHERE 1=1"
    params: list[Any] = []
    if stream_id is not None:
        sql += " AND stream_id=?"
        params.append(stream_id)
    if not include_deleted:
        sql += " AND deleted=0"
    sql += " ORDER BY stream_id, entity_key"
    out: list[dict[str, Any]] = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json")) if d["payload_json"] else None
        d.pop("payload_json", None)
        d["deleted"] = bool(d["deleted"])
        out.append(d)
    return out


def projection_map(conn: sqlite3.Connection, stream_id: int) -> dict[str, dict[str, Any]]:
    return {r["entity_key"]: r for r in list_projections(conn, stream_id=stream_id)}


def list_known_event_ids(conn: sqlite3.Connection, stream_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT event_id FROM applied_events WHERE stream_id=?",
        (stream_id,),
    ).fetchall()
    return {r["event_id"] for r in rows}


def list_ooo_buffer(conn: sqlite3.Connection, stream_id: int | None = None) -> list[dict[str, Any]]:
    if stream_id is None:
        rows = conn.execute("SELECT * FROM ooo_buffer ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ooo_buffer WHERE stream_id=? ORDER BY id DESC",
            (stream_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload_json"]) if d["payload_json"] else None
        d.pop("payload_json", None)
        out.append(d)
    return out
