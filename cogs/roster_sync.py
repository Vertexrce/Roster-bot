"""Private HTTP bridge used by a separately deployed roster bot.

This is intentionally small and authenticated.  It lets the roster bot use
the RCE bot's SQLite database when Railway runs the bots as separate services,
where a Railway variable alone cannot make two local files the same database.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any

import aiosqlite
from aiohttp import web
from discord.ext import commands

from config import GUILD_ID, SERVER_SLUG
from db import DB_PATH

log = logging.getLogger("RCE.roster_sync")


def _json_row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


class RosterSyncCog(commands.Cog):
    """An extension-style cog without Discord commands."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.token = os.getenv("RCE_SYNC_TOKEN", "").strip()
        self.host = os.getenv("RCE_SYNC_HOST", "0.0.0.0")
        self.port = int(os.getenv("PORT", os.getenv("RCE_SYNC_PORT", "8080")))
        self.runner: web.AppRunner | None = None

    def _authorized(self, request: web.Request) -> bool:
        supplied = request.headers.get("X-Roster-Sync-Token", "")
        return bool(
            self.token
            and supplied
            and hmac.compare_digest(supplied, self.token)
        )

    async def _guard(self, request: web.Request) -> web.Response | None:
        if not self.token:
            return web.json_response(
                {"error": "RCE_SYNC_TOKEN is not configured"},
                status=503,
            )
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return None

    async def healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "rce"})

    async def list_clans(self, request: web.Request) -> web.Response:
        if (error := await self._guard(request)) is not None:
            return error
        try:
            guild_id = int(request.query["guild_id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "guild_id must be an integer"}, status=400)

        if GUILD_ID and guild_id != int(GUILD_ID):
            return web.json_response({"error": "guild is not configured"}, status=403)

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM clans WHERE guild_id=? ORDER BY name",
                (guild_id,),
            )
            rows = await cursor.fetchall()
        return web.json_response({"clans": [dict(row) for row in rows]})

    async def upsert_clan(self, request: web.Request) -> web.Response:
        if (error := await self._guard(request)) is not None:
            return error
        try:
            body = await request.json()
            guild_id = int(body["guild_id"])
            role_id = int(body["role_id"])
            owner_id = int(body["owner_id"])
            name = str(body["name"]).strip()
            clantag = str(body["clantag"]).strip().upper()
        except (KeyError, TypeError, ValueError, web.HTTPException):
            return web.json_response(
                {"error": "guild_id, role_id, owner_id, name, and clantag are required"},
                status=400,
            )

        if not name or not clantag:
            return web.json_response({"error": "name and clantag cannot be blank"}, status=400)
        if GUILD_ID and guild_id != int(GUILD_ID):
            return web.json_response({"error": "guild is not configured"}, status=403)

        server_id = str(body.get("server_id") or "").strip()
        server_name = str(body.get("server_name") or "").strip()
        channel_id = body.get("channel_id")
        channel_id = int(channel_id) if channel_id else None
        description = str(body.get("description") or "").strip() or None

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            if not server_id and server_name:
                cursor = await db.execute(
                    """
                    SELECT server_id FROM game_servers
                    WHERE guild_id=? AND LOWER(name)=LOWER(?)
                    LIMIT 1
                    """,
                    (guild_id, server_name),
                )
                server_row = await cursor.fetchone()
                if server_row:
                    server_id = str(server_row["server_id"])

            if not server_id:
                cursor = await db.execute(
                    "SELECT server_id FROM game_servers WHERE guild_id=? ORDER BY id",
                    (guild_id,),
                )
                server_rows = await cursor.fetchall()
                if len(server_rows) == 1:
                    server_id = str(server_rows[0]["server_id"])
                else:
                    server_id = SERVER_SLUG

            # Prefer the Discord role as the stable identity.  Name/tag are
            # fallbacks for clans created before the role was attached.
            cursor = await db.execute(
                """
                SELECT * FROM clans
                WHERE guild_id=?
                  AND (
                    role_id=?
                    OR LOWER(name)=LOWER(?)
                    OR LOWER(clantag)=LOWER(?)
                  )
                ORDER BY CASE WHEN role_id=? THEN 0 ELSE 1 END, id
                LIMIT 1
                """,
                (guild_id, role_id, name, clantag, role_id),
            )
            existing = await cursor.fetchone()

            if existing:
                clan_id = int(existing["id"])
                await db.execute(
                    """
                    UPDATE clans
                    SET role_id=?,
                        channel_id=COALESCE(?, channel_id),
                        description=COALESCE(?, description)
                    WHERE id=?
                    """,
                    (
                        role_id,
                        channel_id,
                        description,
                        clan_id,
                    ),
                )
            else:
                cursor = await db.execute(
                    """
                    INSERT INTO clans (
                        guild_id, server_id, name, clantag, color, description,
                        owner_id, role_id, channel_id, created_at
                    ) VALUES (?, ?, ?, ?, '#ffffff', ?, ?, ?, ?, strftime('%s','now'))
                    """,
                    (
                        guild_id,
                        server_id,
                        name,
                        clantag,
                        description,
                        owner_id,
                        role_id,
                        channel_id,
                    ),
                )
                clan_id = int(cursor.lastrowid)

            await db.execute(
                """
                INSERT OR IGNORE INTO clan_server_config (guild_id, server_id)
                VALUES (?, ?)
                """,
                (guild_id, server_id),
            )
            await db.execute(
                """
                INSERT OR IGNORE INTO clan_members (clan_id, user_id, clan_role, joined_at)
                VALUES (?, ?, 'owner', strftime('%s','now'))
                """,
                (clan_id, owner_id),
            )
            await db.commit()

            cursor = await db.execute("SELECT * FROM clans WHERE id=?", (clan_id,))
            clan = await cursor.fetchone()

        return web.json_response({"ok": True, "clan": _json_row(clan)})

    async def upsert_member(self, request: web.Request) -> web.Response:
        if (error := await self._guard(request)) is not None:
            return error
        try:
            body = await request.json()
            clan_id = int(body["clan_id"])
            user_id = int(body["user_id"])
            clan_role = str(body.get("clan_role", "member")).strip() or "member"
        except (KeyError, TypeError, ValueError, web.HTTPException):
            return web.json_response(
                {"error": "clan_id and user_id are required"},
                status=400,
            )
        if len(clan_role) > 32:
            return web.json_response({"error": "clan_role is too long"}, status=400)

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT id FROM clans WHERE id=?", (clan_id,))
            if not await cursor.fetchone():
                return web.json_response({"error": "clan not found"}, status=404)
            await db.execute(
                """
                INSERT INTO clan_members (clan_id, user_id, clan_role, joined_at)
                VALUES (?, ?, ?, strftime('%s','now'))
                ON CONFLICT(clan_id, user_id) DO UPDATE SET clan_role=excluded.clan_role
                """,
                (clan_id, user_id, clan_role),
            )
            await db.commit()
        return web.json_response({"ok": True})

    async def clear_clans(self, request: web.Request) -> web.Response:
        if (error := await self._guard(request)) is not None:
            return error
        try:
            guild_id = int(request.query["guild_id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response(
                {"error": "guild_id must be an integer"},
                status=400,
            )

        if GUILD_ID and guild_id != int(GUILD_ID):
            return web.json_response(
                {"error": "guild is not configured"},
                status=403,
            )

        async with aiosqlite.connect(DB_PATH) as db:
            rows = await db.execute_fetchall(
                "SELECT id FROM clans WHERE guild_id=?",
                (guild_id,),
            )
            clan_ids = [int(row[0]) for row in rows]
            if clan_ids:
                placeholders = ",".join("?" for _ in clan_ids)
                await db.execute(
                    f"DELETE FROM clan_members WHERE clan_id IN ({placeholders})",
                    clan_ids,
                )
                await db.execute(
                    f"DELETE FROM clan_invite_codes WHERE clan_id IN ({placeholders})",
                    clan_ids,
                )
            await db.execute("DELETE FROM clan_player_stats WHERE guild_id=?", (guild_id,))
            await db.execute("DELETE FROM clan_panels WHERE guild_id=?", (guild_id,))
            await db.execute("DELETE FROM clan_server_config WHERE guild_id=?", (guild_id,))
            await db.execute("DELETE FROM clans WHERE guild_id=?", (guild_id,))
            await db.commit()

        return web.json_response({"ok": True, "deleted": len(clan_ids)})

    async def start(self) -> None:
        if not self.token:
            log.warning("RCE_SYNC_TOKEN is not set; roster HTTP bridge is disabled")
            return
        app = web.Application()
        app.add_routes(
            [
                web.get("/healthz", self.healthz),
                web.get("/internal/roster/clans", self.list_clans),
                web.post("/internal/roster/clans", self.upsert_clan),
                web.post("/internal/roster/members", self.upsert_member),
                web.delete("/internal/roster/clans", self.clear_clans),
            ]
        )
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        log.info("Roster sync bridge listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None


async def setup(bot):
    cog = RosterSyncCog(bot)
    await bot.add_cog(cog)
    await cog.start()


async def teardown(bot):
    cog = bot.get_cog("RosterSyncCog")
    if cog:
        await cog.stop()
        await bot.remove_cog("RosterSyncCog")