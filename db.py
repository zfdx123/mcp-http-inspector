"""SQLite data layer for HTTP request history.

Copyright (C) 2026  HTTP Inspector Contributors
License: GPL-3.0-or-later
"""

import aiosqlite
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "history.db"


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db


async def init_db():
    db = await get_db()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            method TEXT NOT NULL,
            url TEXT NOT NULL,
            status_code INTEGER,
            duration_ms REAL,
            response_size INTEGER,
            raw_request TEXT NOT NULL,
            raw_response TEXT,
            error TEXT
        )
    """)
    await db.commit()
    await db.close()


async def insert_request(
    method: str,
    url: str,
    status_code: Optional[int],
    duration_ms: Optional[float],
    response_size: Optional[int],
    raw_request: str,
    raw_response: Optional[str],
    error: Optional[str] = None,
) -> int:
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO requests
           (timestamp, method, url, status_code, duration_ms, response_size, raw_request, raw_response, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            method,
            url,
            status_code,
            duration_ms,
            response_size,
            raw_request,
            raw_response,
            error,
        ),
    )
    await db.commit()
    row_id = cursor.lastrowid
    await db.close()
    return row_id


async def list_requests(
    method_filter: Optional[str] = None,
    url_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    db = await get_db()
    conditions = []
    params = []

    if method_filter:
        conditions.append("method = ?")
        params.append(method_filter.upper())
    if url_filter:
        conditions.append("url LIKE ?")
        params.append(f"%{url_filter}%")
    if status_filter:
        conditions.append("status_code = ?")
        params.append(int(status_filter))

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    query = f"SELECT * FROM requests {where} ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    await db.close()
    return [dict(r) for r in rows]


async def get_request(request_id: int) -> Optional[dict]:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
    row = await cursor.fetchone()
    await db.close()
    return dict(row) if row else None


async def clear_history() -> int:
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) as cnt FROM requests")
    count = (await cursor.fetchone())["cnt"]
    await db.execute("DELETE FROM requests")
    await db.execute("DELETE FROM sqlite_sequence WHERE name='requests'")
    await db.commit()
    await db.close()
    return count
