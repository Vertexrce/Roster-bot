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
        os.getenv(
            "GUILD_ID",
            os.getenv("VESTIGE_GUILD_ID", "1539760704641437837"),
        ),
    )
)
MAX_MEMBERS = int(os.getenv("MAX_MEMBERS", "200"))
INVITE_EXPIRY_DAYS = int(os.getenv("INVITE_EXPIRY_DAYS", "7"))
RCE_SERVER_ID = os.getenv("RCE_SERVER_ID", "").strip()
RCE_SERVER_NAME = os.getenv("RCE_SERVER_NAME", "").strip()
AUTO_REGISTER_CLANS = os.getenv("AUTO_REGISTER_CLANS", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}


def schema_unavailable_embed(bot: commands.Bot) -> discord.Embed:
    missing = getattr(bot, "rce_schema_missing", ())
    missing_text = ", ".join(missing) if missing else "the required clan tables"
    db_path = getattr(bot, "rce_db_path", "/data/Vertex.sqlite3")
    db_existed = getattr(bot, "rce_db_existed_at_start", False)
    db_size = getattr(bot, "rce_db_size_at_start", 0)
    if not db_existed:
        diagnosis = (
            f"`{db_path}` was not present when the bot started. Attach the volume "
            "containing Vertex.sqlite3 to this bot service."
        )
    elif db_size == 0:
        diagnosis = (
            f"`{db_path}` was empty when the bot started. Replace it with the real "
            "shared Vertex.sqlite3 database."
        )
    else:
        diagnosis = (
            f"`{db_path}` exists ({db_size:,} bytes), but it is not the shared "
            "Vertex database or does not contain the RCE clan schema."
        )
    return error_embed(
        "Recruitment is temporarily unavailable",
        f"{diagnosis}\n\nMissing: `{missing_text}`. Copy the real database to that "
        "mounted path, then restart the bot.",
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


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def derive_clan_identity(role_name: str, source_guild_name: str) -> tuple[str, str]:
    """Turn common Discord role names into a stable clan name and tag."""
    clean = re.sub(r"\s+", " ", role_name).strip()
    match = re.match(
        r"^\[([A-Za-z0-9]{2,8})\]\s*(?:[-|:·—]\s*)?(.+)$",
        clean,
    )
    if not match:
        match = re.match(
            r"^([A-Za-z0-9]{2,8})\s+[-|:·—]\s*(.+)$",
            clean,
        )
    if match and match.group(2).strip():
        tag = match.group(1).upper()
        name = match.group(2).strip()
    else:
        name = clean or source_guild_name.strip() or "Unnamed Clan"
        words = re.findall(r"[A-Za-z0-9]+", name)
        tag = "".join(word[0] for word in words)[:4].upper()
        if len(tag) < 2:
            tag = re.sub(r"[^A-Za-z0-9]", "", name)[:4].upper() or "CLAN"
    return name[:100], tag[:16]


def is_rce_clan_role(role_name: str) -> bool:
    """Return whether a role uses the RCE clans cog's generated format."""
    return bool(re.match(r"^.+\s+\[[A-Za-z0-9]{1,4}\]$", role_name.strip()))


def _eligible_roles(member: discord.Member) -> list[discord.Role]:
    return [
        role
        for role in member.roles
        if role != member.guild.default_role
        and not role.managed
        and not role.is_bot_managed()
    ]


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


async def clear_local_clans(guild_id: int) -> int:
    """Delete local registrations without deleting Discord roles or channels."""
    clans = await fetchall(
        "SELECT id, role_id FROM clans WHERE guild_id=?",
        (guild_id,),
    )
    clan_ids = [int(row["id"]) for row in clans]
    role_ids = [int(row["role_id"]) for row in clans if row["role_id"]]

    if clan_ids:
        placeholders = ",".join("?" for _ in clan_ids)
        await execute(
            f"DELETE FROM clan_invites WHERE clan_id IN ({placeholders})",
            clan_ids,
        )
        await execute(
            f"DELETE FROM clan_invite_codes WHERE clan_id IN ({placeholders})",
            clan_ids,
        )
        await execute(
            f"DELETE FROM clan_members WHERE clan_id IN ({placeholders})",
            clan_ids,
        )

    await execute("DELETE FROM clan_player_stats WHERE guild_id=?", (guild_id,))
    await execute("DELETE FROM clan_panels WHERE guild_id=?", (guild_id,))
    await execute("DELETE FROM clan_server_config WHERE guild_id=?", (guild_id,))
    await execute("DELETE FROM clans WHERE guild_id=?", (guild_id,))

    if role_ids:
        placeholders = ",".join("?" for _ in role_ids)
        await execute(
            f"DELETE FROM guild_clan_link WHERE clan_role_id IN ({placeholders})",
            role_ids,
        )

    return len(clan_ids)


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
        rce_synced = await self.cog.sync_member_to_rce(
            int(clan["id"]),
            member.id,
            "member",
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
                        "They accepted their clan invite and were added via Roster Bot."
                        + (
                            ""
                            if rce_synced
                            else "\n\n⚠️ The RCE bot database could not be reached; "
                            "an administrator should run a roster sync."
                        ),
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


class ConfirmUnregisterAllView(discord.ui.View):
    """One-use confirmation for the administrator-only destructive command."""

    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.confirmed = False

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the administrator who started this action can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Unregister all clans",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self._check_user(interaction):
            return
        self.confirmed = True
        self.stop()
        await interaction.response.edit_message(
            content="Unregistering all clan records…",
            embed=None,
            view=None,
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self._check_user(interaction):
            return
        self.stop()
        await interaction.response.edit_message(
            content="Cancelled. No clan records were changed.",
            embed=None,
            view=None,
        )

    async def on_timeout(self) -> None:
        self.stop()


class RecruitCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def sync_clans_from_rce(self) -> None:
        sync = getattr(self.bot, "rce_sync", None)
        if sync and sync.enabled:
            await sync.pull_clans(MAIN_GUILD_ID)

    async def sync_member_to_rce(self, clan_id: int, user_id: int, clan_role: str = "member") -> bool:
        sync = getattr(self.bot, "rce_sync", None)
        if not sync or not sync.enabled:
            return True
        return await sync.upsert_member(
            clan_id=clan_id,
            user_id=user_id,
            clan_role=clan_role,
        )

    async def _register_clan_from_main_role(
        self,
        member: discord.Member,
        role: discord.Role,
    ) -> tuple[Optional[object], Optional[str]]:
        """Create a local clan from the leader's exact main-server role.

        The bridge is preferred when configured so separate services share the
        RCE clan ID. A bridge outage must not block registration in the roster
        bot, which can continue using its compatible local clan table.
        """
        name, tag = derive_clan_identity(role.name, member.guild.name)
        matching_channels = [
            channel
            for channel in member.guild.text_channels
            if _normalize_name(channel.name)
            in {_normalize_name(name), _normalize_name(role.name)}
        ]
        payload = {
            "guild_id": MAIN_GUILD_ID,
            "server_id": RCE_SERVER_ID or "default",
            "server_name": RCE_SERVER_NAME,
            "name": name,
            "clantag": tag,
            "owner_id": member.id,
            "role_id": role.id,
            "channel_id": (
                matching_channels[0].id
                if len(matching_channels) == 1
                else None
            ),
            "description": f"Registered from main-server role {role.name}.",
        }

        sync = getattr(self.bot, "rce_sync", None)
        if sync and sync.enabled:
            clan = await sync.upsert_clan(payload)
            if clan:
                return clan, None
            log.warning(
                "RCE clan registration was unavailable for role %s; "
                "falling back to the roster database",
                role.id,
            )

        existing = await fetchone(
            """
            SELECT * FROM clans
            WHERE guild_id=?
              AND (role_id=? OR LOWER(name)=LOWER(?) OR LOWER(clantag)=LOWER(?))
            LIMIT 1
            """,
            (MAIN_GUILD_ID, role.id, name, tag),
        )
        if existing:
            await execute(
                """
                UPDATE clans
                SET role_id=?, owner_id=?,
                    channel_id=COALESCE(?, channel_id),
                    description=COALESCE(?, description)
                WHERE id=?
                """,
                (
                    role.id,
                    member.id,
                    payload["channel_id"],
                    payload["description"],
                    int(existing["id"]),
                ),
            )
            clan = await fetchone(
                "SELECT * FROM clans WHERE id=?",
                (int(existing["id"]),),
            )
        else:
            try:
                await execute(
                    """
                    INSERT INTO clans (
                        guild_id, server_id, name, clantag, color, description,
                        owner_id, role_id, channel_id, created_at
                    ) VALUES (?, ?, ?, ?, '#ffffff', ?, ?, ?, ?, ?)
                    """,
                    (
                        MAIN_GUILD_ID,
                        payload["server_id"],
                        name,
                        tag,
                        payload["description"],
                        member.id,
                        role.id,
                        payload["channel_id"],
                        int(time.time()),
                    ),
                )
            except Exception as exc:
                log.exception("Main-server role clan registration failed")
                return None, f"Clan registration failed: `{type(exc).__name__}`"
            clan = await fetchone(
                "SELECT * FROM clans WHERE guild_id=? AND role_id=? LIMIT 1",
                (MAIN_GUILD_ID, role.id),
            )

        if not clan:
            return None, "The clan record could not be created in the roster database."

        await execute(
            """
            INSERT OR IGNORE INTO clan_members
                (clan_id, user_id, clan_role, joined_at)
            VALUES (?, ?, 'owner', ?)
            """,
            (int(clan["id"]), member.id, int(time.time())),
        )
        return clan, None

    async def unregister_all_clans(self, guild_id: int) -> int:
        """Clear the RCE source first, then clear this bot's mirror."""
        sync = getattr(self.bot, "rce_sync", None)
        if sync and sync.enabled:
            if not await sync.clear_clans(guild_id):
                raise RuntimeError(
                    "The RCE sync service did not confirm the clan records were cleared."
                )
        return await clear_local_clans(guild_id)

    async def find_leader_clan(
        self,
        member: discord.Member,
    ) -> tuple[Optional[object], Optional[discord.Role], Optional[str]]:
        """Find the leader's clan and repair a stale Discord role ID if needed."""
        rows = await fetchall(
            """
            SELECT * FROM clans
            WHERE guild_id=? AND role_id IS NOT NULL
            ORDER BY id
            """,
            (MAIN_GUILD_ID,),
        )
        member_role_ids = {role.id for role in member.roles}
        eligible_roles = _eligible_roles(member)

        def role_name_matches(role: discord.Role, clan: object) -> bool:
            role_name = _normalize_name(role.name)
            clan_name = _normalize_name(str(clan["name"]))
            clan_tag = _normalize_name(str(clan["clantag"]))
            return role_name in {
                clan_name,
                clan_tag,
                _normalize_name(f"{clan['name']} {clan['clantag']}"),
                _normalize_name(f"{clan['name']} [{clan['clantag']}]"),
                _normalize_name(f"[{clan['clantag']}] {clan['name']}"),
            }

        matches = [
            row for row in rows
            if int(row["role_id"]) in member_role_ids
        ]

        # If a Discord role was recreated, its ID changes even though its
        # visible name is still the clan's standard "Name [TAG]" format.
        if not matches:
            matches = [
                row for row in rows
                if any(role_name_matches(role, row) for role in eligible_roles)
            ]

        # The owner record is a reliable fallback when the old role ID is
        # stale or missing from the RCE database.
        if not matches:
            owner_matches = [
                row for row in rows
                if int(row["owner_id"]) == member.id
            ]
            if len(owner_matches) == 1:
                clan = owner_matches[0]
                named_roles = [
                    role for role in eligible_roles
                    if role_name_matches(role, clan)
                ]
                if len(named_roles) == 1 or len(eligible_roles) == 1:
                    matches = [clan]

        # A leader can register a new clan even when other clans already
        # exist. Exclude known clan roles first, then require one unambiguous
        # new role so ordinary Discord roles cannot be registered accidentally.
        known_role_ids = {
            int(row["role_id"])
            for row in rows
            if row["role_id"] is not None
        }
        unregistered_roles = [
            role
            for role in eligible_roles
            if role.id not in known_role_ids
            and is_rce_clan_role(role.name)
            and not any(role_name_matches(role, row) for row in rows)
        ]
        if not matches and len(unregistered_roles) == 1:
            role = unregistered_roles[0]
            clan, problem = await self._register_clan_from_main_role(member, role)
            if clan:
                return clan, role, None
            return None, None, problem
        if not matches and len(unregistered_roles) > 1:
            return (
                None,
                None,
                "I found more than one unregistered role on you. Remove your "
                "extra roles so only your clan role remains, then run "
                "`/clan register` again.",
            )

        if not matches:
            return (
                None,
                None,
                "I could not find an RCE clan role on you in the main server. "
                "Clan roles created by the clans cog use the format "
                "`Clan Name [TAG]`.",
            )
        if len(matches) > 1:
            return (
                None,
                None,
                "You hold more than one registered clan role. Remove the extra "
                "clan role before registering.",
            )

        clan = matches[0]
        if int(clan["owner_id"]) != member.id:
            return (
                None,
                None,
                "Only the registered clan leader can use `/clan register`.",
            )

        role = None
        if clan["role_id"] and int(clan["role_id"]) in member_role_ids:
            role = member.guild.get_role(int(clan["role_id"]))
        if role is None:
            named_roles = [
                item for item in eligible_roles
                if role_name_matches(item, clan)
            ]
            if len(named_roles) == 1:
                role = named_roles[0]
            elif len(eligible_roles) == 1:
                role = eligible_roles[0]

        if role is None:
            return (
                None,
                None,
                "I found your clan leader record, but the saved clan role does "
                "not match a role you currently hold. Re-add the clan role, "
                "then run `/clan register` again.",
            )

        # Save the current role ID locally and through the bridge so future
        # accepted invitations receive this exact role.
        if not clan["role_id"] or int(clan["role_id"]) != role.id:
            await execute(
                "UPDATE clans SET role_id=? WHERE id=?",
                (role.id, int(clan["id"])),
            )
            sync = getattr(self.bot, "rce_sync", None)
            if sync and sync.enabled:
                repaired = await sync.upsert_clan(
                    {
                        "guild_id": int(clan["guild_id"]),
                        "server_id": str(clan["server_id"]),
                        "server_name": RCE_SERVER_NAME,
                        "name": str(clan["name"]),
                        "clantag": str(clan["clantag"]),
                        "owner_id": int(clan["owner_id"]),
                        "role_id": role.id,
                        "channel_id": clan["channel_id"],
                        "description": clan["description"],
                    }
                )
                if repaired:
                    clan = repaired

        return clan, role, None

    async def _choose_source_role(
        self,
        source_member: discord.Member,
        main_member: discord.Member,
        requested_role: Optional[discord.Role],
    ) -> tuple[Optional[discord.Role], Optional[str]]:
        if requested_role:
            if requested_role not in source_member.roles:
                return None, "You must hold the roster role you select."
            return requested_role, None

        source_roles = _eligible_roles(source_member)
        if not source_roles:
            return None, "You do not have a roster/clan role in this server."

        # Prefer a source role whose name also appears on the member in the
        # main server. This is the common two-server setup.
        main_names = {_normalize_name(role.name) for role in _eligible_roles(main_member)}
        matching = [role for role in source_roles if _normalize_name(role.name) in main_names]
        if len(matching) == 1:
            return matching[0], None
        if len(source_roles) == 1:
            return source_roles[0], None
        if len(matching) > 1:
            source_roles = matching

        choices = ", ".join(role.mention for role in source_roles[:10])
        return None, (
            "I found more than one role you hold. Run `/clan recruit` again and "
            f"choose the clan role explicitly: {choices}"
        )

    async def _auto_register_clan(
        self,
        *,
        source_guild: discord.Guild,
        source_role: discord.Role,
        main_guild: discord.Guild,
        main_member: discord.Member,
    ):
        """Create the clan record from the role when RCE has no record yet."""
        if not AUTO_REGISTER_CLANS:
            return None, "Automatic clan registration is disabled."

        name, tag = derive_clan_identity(source_role.name, source_guild.name)
        main_roles = _eligible_roles(main_member)
        source_name = _normalize_name(source_role.name)
        exact_roles = [
            role for role in main_roles if _normalize_name(role.name) == source_name
        ]
        related_roles = [
            role
            for role in main_roles
            if source_name
            and (
                source_name in _normalize_name(role.name)
                or _normalize_name(role.name) in source_name
            )
        ]
        main_role = (
            exact_roles[0]
            if len(exact_roles) == 1
            else related_roles[0]
            if len(related_roles) == 1
            else None
        )
        if main_role is None:
            return None, (
                f"I could not match **{source_role.name}** to a clan role in "
                f"**{SERVER_NAME}**. The role names need to match, or an RCE clan "
                "record must already exist."
            )

        # RCE-created roles commonly use the format "Clan Name [TAG]". Prefer
        # that source of truth when the role supplies an explicit tag.
        if "[" in main_role.name or "]" in main_role.name:
            name, tag = derive_clan_identity(main_role.name, source_guild.name)

        matching_channels = [
            channel
            for channel in main_guild.text_channels
            if _normalize_name(channel.name)
            in {_normalize_name(name), _normalize_name(source_role.name)}
        ]
        channel = matching_channels[0] if len(matching_channels) == 1 else None
        payload = {
            "guild_id": main_guild.id,
            "server_id": RCE_SERVER_ID or "default",
            "server_name": RCE_SERVER_NAME,
            "name": name,
            "clantag": tag,
            "owner_id": main_member.id,
            "role_id": main_role.id,
            "channel_id": channel.id if channel else None,
            "description": f"Automatically registered from roster role {source_role.name}.",
        }

        sync = getattr(self.bot, "rce_sync", None)
        if sync and sync.enabled:
            clan = await sync.upsert_clan(payload)
            if clan:
                return clan, None
            return None, (
                "I could not sync the automatically detected clan to the RCE bot. "
                "Check RCE_SYNC_URL, RCE_SYNC_TOKEN, and the RCE service logs."
            )

        existing = await fetchone(
            """
            SELECT * FROM clans
            WHERE guild_id=? AND (role_id=? OR LOWER(name)=LOWER(?) OR LOWER(clantag)=LOWER(?))
            LIMIT 1
            """,
            (main_guild.id, main_role.id, name, tag),
        )
        if existing:
            return existing, None

        try:
            await execute(
                """
                INSERT INTO clans (
                    guild_id, server_id, name, clantag, color, description,
                    owner_id, role_id, channel_id, created_at
                ) VALUES (?, ?, ?, ?, '#ffffff', ?, ?, ?, ?, ?)
                """,
                (
                    main_guild.id,
                    RCE_SERVER_ID or "default",
                    name,
                    tag,
                    payload["description"],
                    main_member.id,
                    main_role.id,
                    channel.id if channel else None,
                    int(time.time()),
                ),
            )
        except Exception as exc:
            log.exception("Automatic local clan registration failed")
            return None, f"Automatic clan registration failed: `{type(exc).__name__}`"

        clan = await fetchone(
            "SELECT * FROM clans WHERE guild_id=? AND role_id=? LIMIT 1",
            (main_guild.id, main_role.id),
        )
        if clan:
            await execute(
                """
                INSERT OR IGNORE INTO clan_members (clan_id, user_id, clan_role, joined_at)
                VALUES (?, ?, 'owner', ?)
                """,
                (int(clan["id"]), main_member.id, int(time.time())),
            )
        return clan, None

    async def _find_clan(
        self,
        source_guild: discord.Guild,
        runner: discord.Member,
        main_guild: discord.Guild,
        clan_name: Optional[str],
        source_role: discord.Role,
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
                normalize = _normalize_name
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
            clan, problem = await self._auto_register_clan(
                source_guild=source_guild,
                source_role=source_role,
                main_guild=main_guild,
                main_member=runner,
            )
            if problem:
                return None, None, problem
            if clan:
                role = main_guild.get_role(int(clan["role_id"])) if clan["role_id"] else None

        if clan is None:
            return None, None, (
                "I could not find or register your clan. Hold the matching clan role "
                f"in **{SERVER_NAME}** and try again."
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
        role: Optional[discord.Role],
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
            "Detecting your clan role and preparing invitations…",
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

        await self.sync_clans_from_rce()

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

        role, role_problem = await self._choose_source_role(
            interaction.user,
            runner,
            role,
        )
        if role_problem or role is None:
            return await interaction.followup.send(
                embed=error_embed("Cannot determine roster role", role_problem or "No role found.")
            )

        clan, clan_role, problem = await self._find_clan(
            interaction.guild, runner, main_guild, clan_name, role
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
    ) -> None:
        await cog.do_recruit(interaction, None, None)

    def make_recruit_command() -> app_commands.Command:
        command = app_commands.Command(
            name="recruit",
            description="Invite your roster members to join your clan.",
            callback=recruit_callback,
        )
        return command

    existing = bot.tree.get_command("clan")
    if isinstance(existing, app_commands.Group):
        group = existing
    else:
        group = app_commands.Group(name="clan", description="Clan operations")
        bot.tree.add_command(group)
    group.add_command(make_recruit_command())

    async def register_callback(
        interaction: discord.Interaction,
    ) -> None:
        """Register the clan role held by a leader in the main server."""
        if interaction.guild is None or interaction.guild.id != MAIN_GUILD_ID:
            return await interaction.response.send_message(
                "This command must be used in the configured main server.",
                ephemeral=True,
            )
        if not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "This command must be used by a clan leader in the main server.",
                ephemeral=True,
            )

        # Pull known records when possible, but do not require the RCE database
        # to already contain this clan. find_leader_clan can create it locally.
        await cog.sync_clans_from_rce()
        clan, role, problem = await cog.find_leader_clan(interaction.user)
        if problem or clan is None:
            return await interaction.response.send_message(
                problem or "I could not determine your clan.",
                ephemeral=True,
            )

        await execute(
            """
            INSERT OR IGNORE INTO clan_members (clan_id, user_id, clan_role, joined_at)
            VALUES (?, ?, 'owner', ?)
            """,
            (int(clan["id"]), interaction.user.id, int(time.time())),
        )
        role_text = role.mention if role else f"<@&{clan['role_id']}>"
        await interaction.response.send_message(
            embed=success_embed(
                "Clan registered",
                f"**[{clan['clantag']}] {clan['name']}** is ready for recruitment.\n"
                f"Registered role: {role_text}\n\n"
                "The clan was detected from your main-server role. "
                "Run `/clan recruit` from your roster server.",
            ),
            ephemeral=True,
        )

    register_command = app_commands.Command(
        name="register",
        description="Register your main-server clan role for recruitment.",
        callback=register_callback,
    )

    async def unregister_all_callback(interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != MAIN_GUILD_ID:
            return await interaction.response.send_message(
                "This command must be used in the configured main server.",
                ephemeral=True,
            )
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            return await interaction.response.send_message(
                "Only a server administrator can unregister all clans.",
                ephemeral=True,
            )

        view = ConfirmUnregisterAllView(interaction.user.id)
        await interaction.response.send_message(
            embed=error_embed(
                "Unregister all clans?",
                "This removes every clan registration, membership record, "
                "invite, and roster sync record from the databases.\n\n"
                "**Discord roles and channels will not be deleted.**",
            ),
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.confirmed:
            return

        try:
            deleted = await cog.unregister_all_clans(MAIN_GUILD_ID)
        except Exception as exc:
            log.exception("Unregister-all failed")
            return await interaction.followup.send(
                embed=error_embed(
                    "Unregister failed",
                    "The databases were not cleared because the RCE sync "
                    f"could not be confirmed.\n`{type(exc).__name__}`",
                ),
                ephemeral=True,
            )

        await interaction.followup.send(
            embed=success_embed(
                "All clans unregistered",
                f"Removed **{deleted}** clan registration(s) from the RCE "
                "and roster records.\nDiscord roles and channels were left in place.",
            ),
            ephemeral=True,
        )

    unregister_command = app_commands.Command(
        name="unregister-all",
        description="Remove all clan registrations (administrator only).",
        callback=unregister_all_callback,
    )
    app_commands.default_permissions(administrator=True)(unregister_command)

    # Keep the recovery/admin command out of roster servers. A separate guild
    # scoped copy of the group gives the main guild both subcommands while the
    # global copy exposes only /clan recruit elsewhere.
    main_group = app_commands.Group(name="clan", description="Clan operations")
    main_group.add_command(make_recruit_command())
    main_group.add_command(register_command)
    main_group.add_command(unregister_command)
    bot.tree.add_command(
        main_group,
        guild=discord.Object(id=MAIN_GUILD_ID),
        override=True,
    )


async def teardown(bot: commands.Bot) -> None:
    group = bot.tree.get_command("clan")
    if isinstance(group, app_commands.Group):
        group.remove_command("recruit")
    bot.tree.remove_command("clan", guild=discord.Object(id=MAIN_GUILD_ID))