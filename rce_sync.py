"""Optional HTTP bridge to the RCE bot.

Railway services do not share a local SQLite file just because they have the
same environment variables.  When the bots run as separate services, the
roster bot uses this small authenticated bridge to keep clan records and
membership changes in the RCE bot's database.  If RCE_SYNC_URL is not set,
the roster bot continues to work with the local/shared DB_PATH mode.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp
import aiosqlite

from db import DB_PATH

log = logging.getLogger("RosterBot.rce_sync")


class RceSyncClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("RCE_SYNC_URL", "").strip().rstrip("/")
        self.token = os.getenv("RCE_SYNC_TOKEN", "").strip()
        self.timeout_seconds = float(os.getenv("RCE_SYNC_TIMEOUT", "15"))

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {"X-Roster-Sync-Token": self.token}
        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
            ) as session:
                async with session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=payload,
                ) as response:
                    body = await response.json(content_type=None)
                    if response.status >= 400:
                        log.error(
                            "RCE sync request failed: %s %s -> HTTP %s (%s)",
                            method,
                            path,
                            response.status,
                            body.get("error", "unknown error")
                            if isinstance(body, dict)
                            else "invalid response",
                        )
                        return None
                    if not isinstance(body, dict):
                        log.error("RCE sync returned a non-object response for %s", path)
                        return None
                    return body
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.error("RCE sync request failed: %s %s: %s", method, path, exc)
            return None

    async def _mirror_clan(self, clan: dict[str, Any]) -> None:
        """Keep the local invite database compatible with the RCE clan ID."""
        required = ("id", "guild_id", "server_id", "name", "clantag", "owner_id")
        if any(key not in clan for key in required):
            raise ValueError("RCE sync returned an incomplete clan record")

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO clans (
                    id, guild_id, server_id, name, clantag, color, description,
                    owner_id, role_id, channel_id, voice_channel_id, created_at,
                    milestone_target, milestone_set_at, milestone_set_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    guild_id=excluded.guild_id,
                    server_id=excluded.server_id,
                    name=excluded.name,
                    clantag=excluded.clantag,
                    color=excluded.color,
                    description=excluded.description,
                    owner_id=excluded.owner_id,
                    role_id=excluded.role_id,
                    channel_id=excluded.channel_id,
                    voice_channel_id=excluded.voice_channel_id,
                    created_at=excluded.created_at,
                    milestone_target=excluded.milestone_target,
                    milestone_set_at=excluded.milestone_set_at,
                    milestone_set_by=excluded.milestone_set_by
                """,
                (
                    clan["id"],
                    clan["guild_id"],
                    clan["server_id"],
                    clan["name"],
                    clan["clantag"],
                    clan.get("color", "#ffffff"),
                    clan.get("description"),
                    clan["owner_id"],
                    clan.get("role_id"),
                    clan.get("channel_id"),
                    clan.get("voice_channel_id"),
                    clan.get("created_at", 0),
                    clan.get("milestone_target"),
                    clan.get("milestone_set_at"),
                    clan.get("milestone_set_by"),
                ),
            )
            await db.execute(
                """
                INSERT OR IGNORE INTO clan_server_config (guild_id, server_id)
                VALUES (?, ?)
                """,
                (clan["guild_id"], clan["server_id"]),
            )
            await db.commit()

    async def pull_clans(self, guild_id: int) -> int:
        """Mirror all clans for the configured main guild into the local DB."""
        if not self.enabled:
            return 0
        result = await self._request(
            "GET",
            f"/internal/roster/clans?guild_id={int(guild_id)}",
        )
        if not result:
            return 0

        mirrored = 0
        for clan in result.get("clans", []):
            try:
                await self._mirror_clan(clan)
                mirrored += 1
            except (KeyError, ValueError, aiosqlite.Error):
                log.exception("Could not mirror one clan from RCE")
        return mirrored

    async def upsert_clan(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        result = await self._request("POST", "/internal/roster/clans", payload)
        clan = result.get("clan") if result else None
        if isinstance(clan, dict):
            await self._mirror_clan(clan)
            return clan
        return None

    async def upsert_member(
        self,
        *,
        clan_id: int,
        user_id: int,
        clan_role: str = "member",
    ) -> bool:
        if not self.enabled:
            return True
        result = await self._request(
            "POST",
            "/internal/roster/members",
            {
                "clan_id": int(clan_id),
                "user_id": int(user_id),
                "clan_role": clan_role,
            },
        )
        return bool(result and result.get("ok"))