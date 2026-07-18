"""Admin-only slash command group: ``/admin``.

Gated on ``config.ADMIN_USER_IDS``. Provides:
* ``/admin stats`` - indexer counts
* ``/admin reseed <channel>`` - re-scan from scratch
* ``/admin delete_entry <id>`` - delete a noisy entry
* ``/admin refresh_steam`` - force-refresh the Steam app list cache
* ``/admin parser_test <text>`` - dry-run parser on raw message text
* ``/admin link_channel`` / ``/admin unlink_channel`` - runtime channel mgmt
  (the list persists in the ``settings`` table so it survives restarts).
"""
from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import Database
from parser import parse_message
from steam_cache import SteamCache

logger = logging.getLogger(__name__)


def _is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id in config.ADMIN_USER_IDS


class Admin(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        steam: SteamCache,
    ) -> None:
        self.bot = bot
        self.db = db
        self.steam = steam

    admin = app_commands.Group(
        name="admin",
        description="Admin-only maintenance commands.",
    )

    # ---------- commands ---------------------------------------------------

    @admin.command(
        name="stats", description="Show indexer statistics."
    )
    @app_commands.guild_only()
    async def stats_cmd(
        self, interaction: discord.Interaction
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Admin only.", ephemeral=True
            )
            return

        total = await self.db.count_entries()
        sources = await self.db.list_sources()
        per_channel = await self.db.count_entries_per_channel()

        embed = discord.Embed(
            title="Indexer Stats", color=discord.Color.blurple()
        )
        embed.add_field(name="Total entries", value=str(total))
        lines: list[str] = []
        for s in sources:
            cid = s["channel_id"]
            lines.append(
                f"<#{cid}> — {per_channel.get(cid, 0)} entries, "
                f"status=`{s['status']}`"
            )
        if lines:
            embed.add_field(
                name="Source channels",
                value="\n".join(lines)[:1024],
                inline=False,
            )
        try:
            steam_count = await self.steam.size()
        except Exception:
            steam_count = "?"
        embed.add_field(
            name="Steam cache", value=f"{steam_count} apps", inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin.command(
        name="reseed",
        description="Re-index a source channel from scratch.",
    )
    @app_commands.guild_only()
    async def reseed_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Admin only.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        indexer = self.bot.get_cog("Indexer")
        if indexer is None:
            await interaction.followup.send(
                "Indexer cog not loaded.", ephemeral=True
            )
            return
        result = await indexer.index_channel(
            channel.id, channel.guild.id, reseed=True
        )
        await interaction.followup.send(
            f"Re-seeded <#{channel.id}>: indexed "
            f"**{result.get('indexed', 0)}** entries "
            f"(\"{result.get('error') or 'ok'}\").",
            ephemeral=True,
        )

    @admin.command(
        name="delete_entry",
        description="Remove a single entry by DB id.",
    )
    @app_commands.guild_only()
    async def delete_entry_cmd(
        self,
        interaction: discord.Interaction,
        entry_id: int,
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Admin only.", ephemeral=True
            )
            return
        deleted = await self.db.delete_entry(entry_id)
        await interaction.response.send_message(
            f"{'Deleted' if deleted else 'Not found'}: entry `{entry_id}`.",
            ephemeral=True,
        )

    @admin.command(
        name="refresh_steam",
        description="Force-refresh the Steam app list cache.",
    )
    @app_commands.guild_only()
    async def refresh_steam_cmd(
        self, interaction: discord.Interaction
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Admin only.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        n = await self.steam.refresh()
        await interaction.followup.send(
            f"Steam cache refreshed: **{n}** apps.", ephemeral=True
        )

    @admin.command(
        name="parser_test",
        description="Test the parser against raw text (no indexing).",
    )
    @app_commands.guild_only()
    async def parser_test_cmd(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Admin only.", ephemeral=True
            )
            return
        entries = parse_message(
            message, source_message_id=0, posted_at="test"
        )
        if not entries:
            await interaction.response.send_message(
                "No Mega.nz entries found.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"Parser results ({len(entries)})",
            color=discord.Color.dark_gold(),
        )
        for e in entries:
            embed.add_field(
                name=e.mega_url[:64],
                value=(
                    f"name: `{e.game_name}`\n"
                    f"app_id: `{e.app_id}`\n"
                    f"filename: `{e.filename}`\n"
                    f"size_bytes: `{e.size_bytes}`\n"
                    f"is_folder: `{e.is_folder}`"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin.command(
        name="link_channel",
        description=(
            "Register a channel as a source for Mega links (persisted)."
        ),
    )
    @app_commands.guild_only()
    async def link_channel_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        # Public binding is a thin wrapper around `_link_channel_impl` so the
        # underlying logic stays callable from tests without the
        # `app_commands`/`Group` decorator wrapping it into a Command object.
        return await self._link_channel_impl(interaction, channel)

    async def _link_channel_impl(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Admin only.", ephemeral=True
            )
            return
        if not config.guild_allowed(channel.guild.id):
            allowed = ", ".join(str(g) for g in config.SOURCE_GUILD_IDS)
            await interaction.response.send_message(
                f"That channel isn't in one of the configured source guilds "
                f"(allowed: {allowed}). Add the guild to `SOURCE_GUILD_IDS` "
                f"in your env first.",
                ephemeral=True,
            )
            return
        if channel.id in config.SOURCE_CHANNELS:
            await interaction.response.send_message(
                f"Channel <#{channel.id}> is already registered.",
                ephemeral=True,
            )
            return

        config.SOURCE_CHANNELS.append(channel.id)
        await self.db.set_setting(
            "source_channels", json.dumps(config.SOURCE_CHANNELS)
        )
        await self.db.upsert_source(
            channel.id, channel.guild.id, status="active"
        )
        await interaction.response.send_message(
            f"✅ Registered <#{channel.id}> as a source channel. "
            f"Backfill starting…",
            ephemeral=True,
        )
        indexer = self.bot.get_cog("Indexer")
        if indexer is not None:
            try:
                await indexer.index_channel(
                    channel.id, channel.guild.id, reseed=True
                )
            except Exception:
                logger.exception("Initial backfill failed for new channel")

    @admin.command(
        name="unlink_channel",
        description="Stop indexing a source channel.",
    )
    @app_commands.guild_only()
    async def unlink_channel_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Admin only.", ephemeral=True
            )
            return
        if channel.id in config.SOURCE_CHANNELS:
            config.SOURCE_CHANNELS.remove(channel.id)
            await self.db.set_setting(
                "source_channels", json.dumps(config.SOURCE_CHANNELS)
            )
            await self.db.set_source_status(channel.id, "unlinked")
        await interaction.response.send_message(
            f"✅ Stopped indexing <#{channel.id}>.", ephemeral=True
        )
