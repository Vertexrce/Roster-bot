"""SQLite data layer for the roster and RCE/Valora clan workflows.

The bot uses DB_PATH and keeps the deployment default at
/data/Vertex.sqlite3. When the original database is unavailable, the
compatible clan schema is created so the bot can start and clans can be
recreated through the normal RCE commands.
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

# This is the clan schema used by the uploaded Valora/RCE source. The source
# ZIPs do not include the live rows, so this creates the structure only; it
# cannot restore clans or members that were stored in the lost database.
CLAN_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS clan_server_config (
    guild_id                INTEGER NOT NULL,
    server_id               TEXT    NOT NULL,
    app_channel_id          INTEGER,
    invite_channel_id       INTEGER,
    active_clans_channel_id INTEGER,
    active_clans_message_id INTEGER,
    role_creation           INTEGER NOT NULL DEFAULT 1,
    channel_creation        INTEGER NOT NULL DEFAULT 1,
    voice_channel_creation  INTEGER NOT NULL DEFAULT 1,
    auto_approve            INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, server_id)
);

CREATE TABLE IF NOT EXISTS clans (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    server_id        TEXT    NOT NULL,
    name             TEXT    NOT NULL,
    clantag          TEXT    NOT NULL,
    color            TEXT    NOT NULL DEFAULT '#ffffff',
    description      TEXT,
    owner_id         INTEGER NOT NULL,
    role_id          INTEGER,
    channel_id       INTEGER,
    voice_channel_id INTEGER,
    created_at       INTEGER NOT NULL,
    milestone_target INTEGER,
    milestone_set_at INTEGER,
    milestone_set_by INTEGER,
    UNIQUE(guild_id, server_id, name),
    UNIQUE(guild_id, server_id, clantag)
);

CREATE TABLE IF NOT EXISTS clan_members (
    clan_id   INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    clan_role TEXT    NOT NULL DEFAULT 'member',
    joined_at INTEGER NOT NULL,
    PRIMARY KEY (clan_id, user_id)
);

CREATE TABLE IF NOT EXISTS clan_invite_codes (
    code          TEXT    NOT NULL,
    guild_id      INTEGER NOT NULL,
    server_id     TEXT    NOT NULL,
    clan_id       INTEGER NOT NULL,
    created_by    INTEGER NOT NULL,
    created_at    INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    max_uses      INTEGER,
    current_uses  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (code, guild_id)
);

CREATE TABLE IF NOT EXISTS clan_player_stats (
    guild_id  INTEGER NOT NULL,
    server_id TEXT    NOT NULL,
    user_id   INTEGER NOT NULL,
    gamertag  TEXT,
    kills     INTEGER NOT NULL DEFAULT 0,
    deaths    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, server_id, user_id)
);

CREATE TABLE IF NOT EXISTS clan_roles (
    guild_id            INTEGER NOT NULL,
    server_id            TEXT    NOT NULL,
    console_access_role TEXT,
    admin_role          TEXT,
    owner_role          TEXT,
    moderator_role      TEXT,
    PRIMARY KEY (guild_id, server_id)
);

CREATE TABLE IF NOT EXISTS clan_panels (
    guild_id   INTEGER NOT NULL,
    server_id  TEXT    NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, server_id)
);
"""


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            CLAN_SCHEMA_SQL
            + """
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