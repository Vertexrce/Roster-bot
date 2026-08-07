"""SQLite data layer shared with the RCE/Valora clan bot.

The shared source of truth is DB_PATH and the deployment default is the
Vertex database mounted at /data/Vertex.sqlite3. This bot deliberately uses
that same database and does not create a replacement clans database.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterable

import aiosqlite


def _default_db_path() -> str:
    return "/data/Vertex.sqlite3"


# DB_PATH is the variable used by the uploaded RCE bot. DATABASE_PATH remains
# a backwards-compatible alias for older roster-bot deployments.
DB_PATH = Path(os.getenv("DB_PATH", os.getenv("DATABASE_PATH", _default_db_path())))


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_clan_link (
                guild_id     INTEGER PRIMARY KEY,
                clan_role_id INTEGER NOT NULL,
                linked_at    INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS clan_invites (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                clan_id          INTEGER NOT NULL,
                invited_user_id  INTEGER NOT NULL,
                invited_by_id    INTEGER NOT NULL,
                source_guild_id  INTEGER NOT NULL,
                source_role_id   INTEGER NOT NULL,
                invite_message_id INTEGER,
                status           TEXT NOT NULL DEFAULT 'pending',
                created_at       INTEGER NOT NULL,
                responded_at     INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_clan_invites_pending
                ON clan_invites (clan_id, invited_user_id, status);
            """
        )
        await db.commit()


async def get_schema_status() -> tuple[list[str], int | None]:
    """Return missing RCE tables and the current clan count."""
    required = ("clans", "clan_members", "clan_server_config")
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await db.execute_fetchall(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name IN (?, ?, ?)
            """,
            required,
        )
        present = {str(row[0]) for row in rows}
        missing = [name for name in required if name not in present]
        if "clans" not in present:
            return missing, None
        async with db.execute("SELECT COUNT(*) FROM clans") as cursor:
            row = await cursor.fetchone()
        return missing, int(row[0]) if row else 0


async def fetchone(query: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, tuple(params)) as cursor:
            return await cursor.fetchone()


async def fetchall(query: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, tuple(params)) as cursor:
            return await cursor.fetchall()


async def execute(query: str, params: Iterable[Any] = ()) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, tuple(params))
        await db.commit()


async def create_invite(
    *,
    clan_id: int,
    invited_user_id: int,
    invited_by_id: int,
    source_guild_id: int,
    source_role_id: int,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO clan_invites (
                clan_id, invited_user_id, invited_by_id,
                source_guild_id, source_role_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                clan_id,
                invited_user_id,
                invited_by_id,
                source_guild_id,
                source_role_id,
                int(time.time()),
            ),
        )
        await db.commit()
        return int(cursor.lastrowid)