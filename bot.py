"""Entry point for the roster recruitment bot."""

from __future__ import annotations

import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

from db import DB_PATH, get_schema_status, init_db
from rce_sync import RceSyncClient

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("RosterBot")


def required_int(name: str, default: str | None = None) -> int:
    value = os.getenv(name, default)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a Discord ID number") from exc


class RosterBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        self.rce_schema_ready = False
        self.rce_schema_missing: tuple[str, ...] = ()
        self.rce_db_path = str(DB_PATH)
        self.rce_db_existed_at_start = False
        self.rce_db_size_at_start = 0
        self.rce_sync = RceSyncClient()

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self) -> None:
        self.rce_db_existed_at_start = DB_PATH.is_file()
        if self.rce_db_existed_at_start:
            self.rce_db_size_at_start = DB_PATH.stat().st_size

        await init_db()

        if self.rce_sync.enabled:
            mirrored = await self.rce_sync.pull_clans(required_int(
                "MAIN_GUILD_ID",
                os.getenv(
                    "GUILD_ID",
                    os.getenv("VESTIGE_GUILD_ID", "1539760704641437837"),
                ),
            ))
            log.info("Mirrored %d clan record(s) from the RCE bot", mirrored)

        # This is a separate bot. It loads clan extensions from its own cogs
        # package and does not depend on the RCE bot's auto-loader.
        await self.load_extension("cogs.clans")

        missing, clan_count = await get_schema_status()
        self.rce_schema_missing = tuple(missing)
        self.rce_schema_ready = not missing
        if missing:
            log.error(
                "Clan database schema could not be initialized: %s. "
                "Recruitment commands remain disabled. DB_PATH=%s",
                ", ".join(missing),
                os.getenv("DB_PATH", "<default: /data/Vertex.sqlite3>"),
            )
            log.error(
                "Database file before startup initialization: exists=%s size=%d bytes path=%s",
                self.rce_db_existed_at_start,
                self.rce_db_size_at_start,
                DB_PATH,
            )
        else:
            if clan_count:
                log.info(
                    "Using clan database %s (%d clan(s))",
                    os.getenv("DB_PATH", "<default: /data/Vertex.sqlite3>"),
                    clan_count,
                )
            else:
                log.warning(
                    "Clan schema is ready at %s, but no clans exist. "
                    "The first /clan recruit can auto-register a clan from its "
                    "matching Discord role.",
                    os.getenv("DB_PATH", "<default: /data/Vertex.sqlite3>"),
                )

        # Global sync makes /clan recruit available in every server the bot
        # belongs to. Set COMMAND_SYNC_GUILD_ID for an immediate test copy.
        sync_guild_id = os.getenv("COMMAND_SYNC_GUILD_ID")
        main_guild_id = int(os.getenv(
            "MAIN_GUILD_ID",
            os.getenv("GUILD_ID", os.getenv("VESTIGE_GUILD_ID", "1539760704641437837")),
        ))
        if sync_guild_id:
            global_synced = await self.tree.sync()
            guild = discord.Object(id=int(sync_guild_id))
            # Do not replace the main-guild command set: it also contains the
            # guild-scoped /clan register recovery command.
            if int(sync_guild_id) != main_guild_id:
                self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            main_synced = (
                synced
                if int(sync_guild_id) == main_guild_id
                else await self.tree.sync(guild=discord.Object(id=main_guild_id))
            )
            log.info(
                "Synced %d global command(s), %d test-guild command(s), "
                "and %d main-guild command(s)",
                len(global_synced),
                len(synced),
                len(main_synced),
                sync_guild_id,
            )
        else:
            synced = await self.tree.sync()
            main_synced = await self.tree.sync(guild=discord.Object(id=main_guild_id))
            log.info(
                "Synced %d global command(s) and %d main-guild command(s)",
                len(synced),
                len(main_synced),
            )

    async def on_ready(self) -> None:
        if self.user:
            log.info("Logged in as %s (%s)", self.user, self.user.id)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "Missing DISCORD_TOKEN. Copy .env.example to .env and add your bot token."
        )

    # Accept the RCE name first, with aliases for older roster deployments.
    required_int(
        "GUILD_ID",
        os.getenv(
            "MAIN_GUILD_ID",
            os.getenv("VESTIGE_GUILD_ID", "1539760704641437837"),
        ),
    )

    bot = RosterBot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()