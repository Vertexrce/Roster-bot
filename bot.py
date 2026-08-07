"""Entry point for the roster recruitment bot."""

from __future__ import annotations

import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from db import init_db

load_dotenv()

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

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self) -> None:
        await init_db()
        await self.load_extension("cogs.recruit")

        # Global sync makes /clan recruit available in every server the bot
        # belongs to. Set COMMAND_SYNC_GUILD_ID for an immediate test copy.
        sync_guild_id = os.getenv("COMMAND_SYNC_GUILD_ID")
        if sync_guild_id:
            guild = discord.Object(id=int(sync_guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d command(s) to test guild %s", len(synced), sync_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global command(s)", len(synced))

    async def on_ready(self) -> None:
        if self.user:
            log.info("Logged in as %s (%s)", self.user, self.user.id)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "Missing DISCORD_TOKEN. Copy .env.example to .env and add your bot token."
        )

    # Accept either the new name or the name used by the uploaded cog.
    required_int("MAIN_GUILD_ID", os.getenv("VESTIGE_GUILD_ID"))

    bot = RosterBot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()