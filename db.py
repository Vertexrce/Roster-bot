"""Small SQLite data layer used by the roster bot.

The clan tables intentionally use the same names and core columns as the
uploaded cog so an existing SQLite database can be copied into this folder.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

DB_PATH = Path(os.getenv("DATABASE_PATH", "data/roster.db"))


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS clans (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                server_id  TEXT NOT NULL,
                name       TEXT NOT NULL,
                clantag    TEXT NOT NULL,
                owner_id   INTEGER NOT NULL,
                role_id    INTEGER,
                channel_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS clan_members (
                clan_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                clan_role  TEXT NOT NULL DEFAULT 'member',
                joined_at  INTEGER NOT NULL,
                PRIMARY KEY (clan_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS clan_server_config (
                guild_id                INTEGER NOT NULL,
                server_id               TEXT NOT NULL,
                active_clans_channel_id INTEGER,
                active_clans_message_id INTEGER,
                PRIMARY KEY (guild_id, server_id)
            );

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