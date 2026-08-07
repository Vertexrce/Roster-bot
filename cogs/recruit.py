"""The /clan recruit command and DM-based clan invitations."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from db import create_invite, execute, fetchall, fetchone

log = logging.getLogger("RosterBot.recruit")

THEME_COLOR = int(os.getenv("THEME_COLOR", str(0xBF00FF)))
SERVER_NAME = os.getenv("SERVER_NAME", "Main Server")
MAIN_GUILD_ID = int(
    os.getenv(
        "MAIN_GUILD_ID",
        os.getenv("GUILD_ID", os.getenv("VESTIGE_GUILD_ID", "0")),
    )
)
MAX_MEMBERS = int(os.getenv("MAX_MEMBERS", "200"))
INVITE_EXPIRY_DAYS = int(os.getenv("INVITE_EXPIRY_DAYS", "7"))


def schema_unavailable_embed(bot: commands.Bot) -> discord.Embed:
    missing = getattr(bot, "rce_schema_missing", ())
    missing_text = ", ".join(missing) if missing else "the required clan tables"
    return error_embed(
        "Recruitment is temporarily unavailable",
        "The bot is connected, but it cannot find the RCE/Valora clan database. "
        f"Missing: `{missing_text}`. An administrator must set `DB_PATH` to the "
        "real `ruin.sqlite3` file and restart the bot.",
    )


def error_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )


def success_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        color=THEME_COLOR,
        timestamp=discord.utils.utcnow(),
    )


async def get_guild_link(guild_id: int) -> Optional[int]:
    row = await fetchone(
        "SELECT clan_role_id FROM guild_clan_link WHERE guild_id=?",
        (guild_id,),
    )
    return int(row["clan_role_id"]) if row else None


async def save_guild_link(guild_id: int, clan_role_id: int) -> None:
    await execute(
        """
        INSERT OR REPLACE INTO guild_clan_link (guild_id, clan_role_id, linked_at)
        VALUES (?, ?, ?)
        """,
        (guild_id, clan_role_id, int(time.time())),
    )


async def clear_guild_link(guild_id: int) -> None:
    await execute("DELETE FROM guild_clan_link WHERE guild_id=?", (guild_id,))


async def get_clan_by_role(role_id: int):
    return await fetchone("SELECT * FROM clans WHERE role_id=?", (role_id,))


async def get_owned_clan(guild_id: int, owner_id: int):
    return await fetchone(
        "SELECT * FROM clans WHERE guild_id=? AND owner_id=?",
        (guild_id, owner_id),
    )


async def get_coled_clan(guild_id: int, user_id: int):
    return await fetchone(
        """
        SELECT c.* FROM clans c
        JOIN clan_members cm ON cm.clan_id = c.id
        WHERE c.guild_id=? AND cm.user_id=? AND cm.clan_role='co-leader'
        LIMIT 1
        """,
        (guild_id, user_id),
    )


async def member_count(clan_id: int) -> int:
    row = await fetchone(
        "SELECT COUNT(*) AS count FROM clan_members WHERE clan_id=?",
        (clan_id,),
    )
    return int(row["count"]) if row else 0


async def update_active_clans(guild: discord.Guild, guild_id: int, server_id: str) -> None:
    """Refresh the optional active-clans message used by the existing clan setup."""
    try:
        config = await fetchone(
            "SELECT * FROM clan_server_config WHERE guild_id=? AND server_id=?",
            (guild_id, server_id),
        )
        if not config or not config["active_clans_channel_id"]:
            return

        channel = guild.get_channel(int(config["active_clans_channel_id"]))
        if not channel or not isinstance(channel, discord.abc.Messageable):
            return

        clans = await fetchall(
            "SELECT * FROM clans WHERE guild_id=? AND server_id=? ORDER BY name",
            (guild_id, server_id),
        )
        rows: list[tuple[object, int]] = []
        for clan in clans:
            role = guild.get_role(int(clan["role_id"])) if clan["role_id"] else None
            count = len(role.members) if role else await member_count(int(clan["id"]))
            rows.append((clan, count))
        rows.sort(key=lambda item: item[1], reverse=True)

        embed = discord.Embed(title="Active Clans", color=THEME_COLOR)
        embed.description = (
            "\n".join(
                f"<@&{clan['role_id']}> — {count} member(s)"
                if clan["role_id"]
                else f"**[{clan['clantag']}] {clan['name']}** — {count} member(s)"
                for clan, count in rows[:25]
            )
            or "*No active clans.*"
        )
        embed.set_footer(
            text=f"{len(rows)} clan(s) · {sum(count for _, count in rows)} member(s)"
        )

        message = None
        if config["active_clans_message_id"]:
            try:
                message = await channel.fetch_message(int(config["active_clans_message_id"]))
                await message.edit(embed=embed)
            except (discord.NotFound, discord.HTTPException):
                message = None

        if message is None:
            message = await channel.send(embed=embed)
            await execute(
                """
                UPDATE clan_server_config SET active_clans_message_id=?
                WHERE guild_id=? AND server_id=?
                """,
                (message.id, guild_id, server_id),
            )
    except Exception:
        log.exception("Could not refresh the active-clans message")


class InviteView(discord.ui.View):
    """Persistent Accept/Decline buttons attached to a DM invitation."""

    def __init__(self, cog: "RecruitCog", invite_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.invite_id = invite_id

        accept = discord.ui.Button(
            label="Accept invite",
            style=discord.ButtonStyle.success,
            custom_id=f"roster_invite_accept:{invite_id}",
        )
        decline = discord.ui.Button(
            label="Decline",
            style=discord.ButtonStyle.secondary,
            custom_id=f"roster_invite_decline:{invite_id}",
        )
        accept.callback = self.accept
        decline.callback = self.decline
        self.add_item(accept)
        self.add_item(decline)

    async def accept(self, interaction: discord.Interaction) -> None:
        if not getattr(self.cog.bot, "rce_schema_ready", False):
            return await interaction.response.send_message(
                embed=schema_unavailable_embed(self.cog.bot),
                ephemeral=True,
            )

        invite = await fetchone(
            "SELECT * FROM clan_invites WHERE id=?",
            (self.invite_id,),
        )
        if not invite:
            return await interaction.response.send_message("This invite no longer exists.")
        if interaction.user.id != int(invite["invited_user_id"]):
            return await interaction.response.send_message(
                "This invite was sent to a different Discord account."
            )
        if invite["status"] != "pending":
            return await interaction.response.send_message(
                f"This invite has already been {invite['status']}."
            )
        if int(invite["created_at"]) + INVITE_EXPIRY_DAYS * 86400 < int(time.time()):
            await execute(
                "UPDATE clan_invites SET status='expired', responded_at=? WHERE id=?",
                (int(time.time()), self.invite_id),
            )
            return await interaction.response.send_message("This invite has expired.")

        guild = self.cog.bot.get_guild(MAIN_GUILD_ID)
        if guild is None:
            return await interaction.response.send_message(
                f"The {SERVER_NAME} server is temporarily unavailable. Try again later."
            )

        member = guild.get_member(interaction.user.id)
        if member is None:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is None:
            return await interaction.response.send_message(
                f"You must be in the {SERVER_NAME} server before accepting this invite."
            )

        clan = await fetchone("SELECT * FROM clans WHERE id=?", (int(invite["clan_id"]),))
        if not clan:
            return await interaction.response.send_message("The clan on this invite no longer exists.")

        role_ids = {
            int(row["role_id"])
            for row in await fetchall(
                "SELECT role_id FROM clans WHERE guild_id=? AND role_id IS NOT NULL",
                (guild.id,),
            )
        }
        clan_role = guild.get_role(int(clan["role_id"])) if clan["role_id"] else None
        current_role_id = clan_role.id if clan_role else 0
        other_role_ids = (role_ids & {role.id for role in member.roles}) - {current_role_id}
        if other_role_ids:
            other_role = guild.get_role(next(iter(other_role_ids)))
            return await interaction.response.send_message(
                f"You are already in another registered clan"
                f"{f' ({other_role.name})' if other_role else ''}. "
                "Leave that clan before accepting this invite."
            )

        try:
            if clan_role and clan_role not in member.roles:
                await member.add_roles(
                    clan_role,
                    reason=f"Clan invite accepted for {member} ({self.invite_id})",
                )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "I could not assign the clan role. Ask a server administrator to move "
                "the bot's highest role above the clan role."
            )
        except discord.HTTPException:
            return await interaction.response.send_message(
                "Discord rejected the role update. Please try accepting again later."
            )

        await execute(
            """
            INSERT OR IGNORE INTO clan_members (clan_id, user_id, clan_role, joined_at)
            VALUES (?, ?, 'member', ?)
            """,
            (int(clan["id"]), member.id, int(time.time())),
        )
        await execute(
            "UPDATE clan_invites SET status='accepted', responded_at=? WHERE id=?",
            (int(time.time()), self.invite_id),
        )

        team_chat = (
            guild.get_channel(int(clan["channel_id"]))
            if clan["channel_id"]
            else None
        )
        if team_chat and isinstance(team_chat, discord.abc.Messageable):
            try:
                await team_chat.send(
                    embed=success_embed(
                        "New member",
                        f"Welcome to **{clan['name']}**, {member.mention}!\n"
                        "They accepted their clan invite.",
                    )
                )
            except discord.HTTPException:
                log.warning("Could not post clan welcome for %s", member.id)

        await update_active_clans(guild, guild.id, str(clan["server_id"]))
        await interaction.response.send_message(
            f"You joined **[{clan['clantag']}] {clan['name']}**."
        )
        if interaction.message:
            await interaction.message.edit(
                embed=success_embed(
                    "Invite accepted",
                    f"{member.mention} accepted the invitation to **{clan['name']}**.",
                ),
                view=None,
            )

    async def decline(self, interaction: discord.Interaction) -> None:
        if not getattr(self.cog.bot, "rce_schema_ready", False):
            return await interaction.response.send_message(
                embed=schema_unavailable_embed(self.cog.bot),
                ephemeral=True,
            )

        invite = await fetchone(
            "SELECT * FROM clan_invites WHERE id=?",
            (self.invite_id,),
        )
        if not invite:
            return await interaction.response.send_message("This invite no longer exists.")
        if interaction.user.id != int(invite["invited_user_id"]):
            return await interaction.response.send_message(
                "This invite was sent to a different Discord account."
            )
        if invite["status"] != "pending":
            return await interaction.response.send_message(
                f"This invite has already been {invite['status']}."
            )

        await execute(
            "UPDATE clan_invites SET status='declined', responded_at=? WHERE id=?",
            (int(time.time()), self.invite_id),
        )
        await interaction.response.send_message("The clan invite was declined.")
        if interaction.message:
            await interaction.message.edit(
                embed=success_embed("Invite declined", "This invitation is no longer active."),
                view=None,
            )


class RecruitCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _find_clan(
        self,
        source_guild: discord.Guild,
        runner: discord.Member,
        main_guild: discord.Guild,
        clan_name: Optional[str],
    ):
        if clan_name:
            normalized = clan_name.strip().lower()
            clan = await fetchone(
                """
                SELECT * FROM clans
                WHERE guild_id=? AND (LOWER(name)=? OR LOWER(clantag)=?)
                """,
                (main_guild.id, normalized, normalized),
            )
            if not clan:
                return None, None, "No clan with that name or tag is registered."
            role = main_guild.get_role(int(clan["role_id"])) if clan["role_id"] else None
            return clan, role, None

        clan = None
        role = None

        cached_role_id = await get_guild_link(source_guild.id)
        if cached_role_id:
            role = main_guild.get_role(cached_role_id)
            if role is None:
                await clear_guild_link(source_guild.id)
            else:
                clan = await get_clan_by_role(cached_role_id)
                if clan and int(clan["guild_id"]) != main_guild.id:
                    clan = None

        if clan is None:
            clan = await get_owned_clan(main_guild.id, runner.id)
            if clan:
                role = main_guild.get_role(int(clan["role_id"])) if clan["role_id"] else None

        if clan is None:
            clan = await get_coled_clan(main_guild.id, runner.id)
            if clan:
                role = main_guild.get_role(int(clan["role_id"])) if clan["role_id"] else None

        if clan is None:
            runner_role_ids = {item.id for item in runner.roles}
            candidates = [
                (item, main_guild.get_role(int(item["role_id"])))
                for item in await fetchall(
                    "SELECT * FROM clans WHERE guild_id=? AND role_id IS NOT NULL",
                    (main_guild.id,),
                )
                if int(item["role_id"]) in runner_role_ids
                and main_guild.get_role(int(item["role_id"])) is not None
            ]
            if len(candidates) == 1:
                clan, role = candidates[0]
            elif len(candidates) > 1:
                normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
                source_name = normalize(source_guild.name)
                matching = [
                    pair
                    for pair in candidates
                    if source_name
                    and (
                        source_name in normalize(str(pair[0]["name"]))
                        or source_name in normalize(str(pair[0]["clantag"]))
                    )
                ]
                if len(matching) == 1:
                    clan, role = matching[0]
                else:
                    return (
                        None,
                        None,
                        "You are associated with multiple clans. Use "
                        "`clan_name` to choose the right one.",
                    )

        if clan is None:
            return (
                None,
                None,
                "I could not find your clan. You must own, co-lead, or hold the "
                "registered clan role on the main server.",
            )

        # An explicit clan name still requires the runner to be an owner,
        # co-leader, or holder of that clan's registered role.
        authorized = int(clan["owner_id"]) == runner.id
        if not authorized:
            colead = await fetchone(
                """
                SELECT 1 FROM clan_members
                WHERE clan_id=? AND user_id=? AND clan_role='co-leader'
                """,
                (int(clan["id"]), runner.id),
            )
            authorized = colead is not None
        if not authorized and role:
            authorized = role.id in {item.id for item in runner.roles}
        if not authorized:
            return None, None, "Only the clan owner, a co-leader, or a clan role holder can recruit."

        return clan, role, None

    async def _do_recruit_impl(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        clan_name: Optional[str] = None,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                embed=error_embed("Server only", "Use this command inside a Discord server."),
                ephemeral=True,
            )
        if interaction.guild.id == MAIN_GUILD_ID:
            return await interaction.response.send_message(
                embed=error_embed(
                    "Use this from a partner server",
                    "Run `/clan recruit` in the other Discord server where the roster role exists.",
                ),
                ephemeral=True,
            )

        await interaction.response.defer()
        progress = await interaction.followup.send(
            f"Checking **{role.name}** and preparing invitations…",
            wait=True,
        )
        main_guild = self.bot.get_guild(MAIN_GUILD_ID)
        if main_guild is None:
            return await interaction.followup.send(
                embed=error_embed(
                    "Main server unavailable",
                    f"I cannot reach **{SERVER_NAME}**. Check that the bot is in that server "
                    "and that Server Members Intent is enabled.",
                )
            )

        runner = main_guild.get_member(interaction.user.id)
        if runner is None:
            try:
                runner = await main_guild.fetch_member(interaction.user.id)
            except (discord.NotFound, discord.HTTPException):
                runner = None
        if runner is None:
            return await interaction.followup.send(
                embed=error_embed(
                    "Not in the main server",
                    f"You must be a member of **{SERVER_NAME}** before using recruitment.",
                )
            )

        clan, clan_role, problem = await self._find_clan(
            interaction.guild, runner, main_guild, clan_name
        )
        if problem or clan is None:
            return await interaction.followup.send(embed=error_embed("Cannot recruit", problem or "No clan found."))

        source_members = [member for member in role.members if not member.bot]
        if not source_members:
            return await interaction.followup.send(
                embed=error_embed(
                    "No members found",
                    f"**{role.name}** has no non-bot members, or Server Members Intent is off.",
                )
            )
        if len(source_members) > MAX_MEMBERS:
            return await interaction.followup.send(
                embed=error_embed(
                    "Too many members",
                    f"**{role.name}** has {len(source_members)} members. The limit is {MAX_MEMBERS}.",
                )
            )

        if clan_role:
            await save_guild_link(interaction.guild.id, clan_role.id)

        all_clan_role_ids = {
            int(row["role_id"])
            for row in await fetchall(
                "SELECT role_id FROM clans WHERE guild_id=? AND role_id IS NOT NULL",
                (main_guild.id,),
            )
        }
        current_role_id = clan_role.id if clan_role else 0
        sent: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        for source_member in source_members:
            tag = f"{source_member.display_name} ({source_member.id})"
            main_member = main_guild.get_member(source_member.id)
            if main_member is None:
                try:
                    main_member = await asyncio.wait_for(
                        main_guild.fetch_member(source_member.id),
                        timeout=15,
                    )
                except (discord.NotFound, discord.HTTPException):
                    main_member = None
                except asyncio.TimeoutError:
                    main_member = None
            if main_member is None:
                skipped.append(f"{tag} — not in {SERVER_NAME}")
                continue

            member_role_ids = {item.id for item in main_member.roles}
            other_clan_roles = (all_clan_role_ids & member_role_ids) - {current_role_id}
            if other_clan_roles:
                other_role = main_guild.get_role(next(iter(other_clan_roles)))
                skipped.append(
                    f"{main_member.mention} — already in "
                    f"{other_role.name if other_role else 'another clan'}"
                )
                continue

            already_member = await fetchone(
                "SELECT 1 FROM clan_members WHERE clan_id=? AND user_id=?",
                (int(clan["id"]), main_member.id),
            )
            if current_role_id in member_role_ids or already_member:
                skipped.append(f"{main_member.mention} — already in this clan")
                continue

            pending = await fetchone(
                """
                SELECT id FROM clan_invites
                WHERE clan_id=? AND invited_user_id=? AND status='pending'
                ORDER BY id DESC LIMIT 1
                """,
                (int(clan["id"]), main_member.id),
            )
            if pending:
                skipped.append(f"{main_member.mention} — invite already pending")
                continue

            invite_id = await create_invite(
                clan_id=int(clan["id"]),
                invited_user_id=main_member.id,
                invited_by_id=interaction.user.id,
                source_guild_id=interaction.guild.id,
                source_role_id=role.id,
            )
            invite_embed = discord.Embed(
                title=f"Clan invitation · [{clan['clantag']}] {clan['name']}",
                description=(
                    f"**{interaction.user.display_name}** invited you to join "
                    f"**{clan['name']}**.\n\n"
                    f"You are already a member of **{SERVER_NAME}**. "
                    "Choose **Accept invite** to join the clan, or **Decline** to dismiss it."
                ),
                color=THEME_COLOR,
            )
            invite_embed.set_footer(text=f"Invite expires in {INVITE_EXPIRY_DAYS} days")
            try:
                message = await asyncio.wait_for(
                    main_member.send(
                        embed=invite_embed,
                        view=InviteView(self, invite_id),
                    ),
                    timeout=15,
                )
                await execute(
                    "UPDATE clan_invites SET invite_message_id=? WHERE id=?",
                    (message.id, invite_id),
                )
                sent.append(main_member.mention)
            except discord.Forbidden:
                await execute(
                    "UPDATE clan_invites SET status='delivery_failed', responded_at=? WHERE id=?",
                    (int(time.time()), invite_id),
                )
                failed.append(f"{tag} — DMs are closed or blocked")
            except discord.HTTPException as exc:
                await execute(
                    "UPDATE clan_invites SET status='delivery_failed', responded_at=? WHERE id=?",
                    (int(time.time()), invite_id),
                )
                failed.append(f"{tag} — Discord error {exc.status}")
            except asyncio.TimeoutError:
                await execute(
                    "UPDATE clan_invites SET status='delivery_failed', responded_at=? WHERE id=?",
                    (int(time.time()), invite_id),
                )
                failed.append(f"{tag} — Discord took too long to deliver the DM")

            processed = len(sent) + len(skipped) + len(failed)
            if processed == 1 or processed % 10 == 0:
                try:
                    await progress.edit(
                        content=(
                            f"Processed **{processed}/{len(source_members)}** members "
                            f"({len(sent)} invites sent)…"
                        )
                    )
                except discord.HTTPException:
                    pass

        summary = success_embed(
            f"Recruit invitations sent · [{clan['clantag']}] {clan['name']}",
            "No roles were assigned automatically. Each person must accept their DM invite.",
        )
        summary.add_field(name="Invites sent", value=str(len(sent)), inline=True)
        summary.add_field(name="Skipped", value=str(len(skipped)), inline=True)
        summary.add_field(name="Failed", value=str(len(failed)), inline=True)
        if sent:
            summary.add_field(name="Sent to", value="\n".join(f"• {item}" for item in sent[:30]), inline=False)
        if skipped:
            summary.add_field(
                name="Skipped details",
                value="\n".join(f"• {item}" for item in skipped[:15]),
                inline=False,
            )
        if failed:
            summary.add_field(
                name="Failed details",
                value="\n".join(f"• {item}" for item in failed[:15]),
                inline=False,
            )
        await interaction.followup.send(embed=summary)

    async def do_recruit(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        clan_name: Optional[str] = None,
    ) -> None:
        """Run recruitment and always resolve the Discord interaction."""
        if not getattr(self.bot, "rce_schema_ready", False):
            return await interaction.response.send_message(
                embed=schema_unavailable_embed(self.bot),
                ephemeral=True,
            )

        try:
            await self._do_recruit_impl(interaction, role, clan_name)
        except Exception as exc:
            log.exception("Unhandled /clan recruit error")
            message = (
                "The recruitment could not finish. Check the bot logs for the "
                f"error details.\n`{type(exc).__name__}`"
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        embed=error_embed("Recruitment error", message)
                    )
                else:
                    await interaction.response.send_message(
                        embed=error_embed("Recruitment error", message),
                        ephemeral=True,
                    )
            except (discord.HTTPException, discord.NotFound):
                log.exception("Could not send /clan recruit error response")


async def setup(bot: commands.Bot) -> None:
    cog = RecruitCog(bot)
    await bot.add_cog(cog)

    # Re-register pending buttons after a restart so old DMs still work.
    for row in await fetchall(
        "SELECT id FROM clan_invites WHERE status='pending'",
    ):
        bot.add_view(InviteView(cog, int(row["id"])))

    async def recruit_callback(
        interaction: discord.Interaction,
        role: discord.Role,
        clan_name: Optional[str] = None,
    ) -> None:
        await cog.do_recruit(interaction, role, clan_name)

    command = app_commands.Command(
        name="recruit",
        description="DM main-server members an invitation to join your clan.",
        callback=recruit_callback,
    )
    app_commands.describe(
        role="Role whose members should receive a clan invitation",
        clan_name="Optional clan name or tag when automatic detection is ambiguous",
    )(command)

    existing = bot.tree.get_command("clan")
    if isinstance(existing, app_commands.Group):
        existing.add_command(command)
    else:
        group = app_commands.Group(name="clan", description="Clan operations")
        group.add_command(command)
        bot.tree.add_command(group)


async def teardown(bot: commands.Bot) -> None:
    group = bot.tree.get_command("clan")
    if isinstance(group, app_commands.Group):
        group.remove_command("recruit")