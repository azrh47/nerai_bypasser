"""Uploader/Partner command group for posting new game links via /add."""
from __future__ import annotations

import logging
import discord
from discord import app_commands
from discord.ext import commands

import config

logger = logging.getLogger(__name__)


def can_upload(interaction: discord.Interaction) -> bool:
    if interaction.user.id in config.ADMIN_USER_IDS:
        return True
    if interaction.user.id in config.UPLOADER_USER_IDS:
        return True
    if isinstance(interaction.user, discord.Member):
        if any(role.id in config.UPLOADER_ROLE_IDS for role in interaction.user.roles):
            return True
    return False


class Uploader(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="add",
        description="Add and index a new game link directly into the configured source channel.",
    )
    @app_commands.describe(
        game_name="The name of the game",
        link="The download link (Mega.nz, etc.)",
        channel_id_str="Optional: The ID of a specific source channel to post to",
    )
    @app_commands.guild_only()
    async def add_cmd(
        self,
        interaction: discord.Interaction,
        game_name: str,
        link: str,
        channel_id_str: str | None = None,
    ) -> None:
        if not can_upload(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            return

        if not config.SOURCE_CHANNELS:
            await interaction.response.send_message(
                "❌ No source channels configured in the bot.", ephemeral=True
            )
            return

        is_superadmin = interaction.user.id in config.ADMIN_USER_IDS

        if channel_id_str:
            try:
                target_ids = [int(channel_id_str)]
            except ValueError:
                await interaction.response.send_message("❌ Invalid channel ID.", ephemeral=True)
                return
            if target_ids[0] not in config.SOURCE_CHANNELS:
                await interaction.response.send_message(
                    f"❌ Channel <#{target_ids[0]}> is not a configured source channel.", ephemeral=True
                )
                return
            if not is_superadmin:
                channel = self.bot.get_channel(target_ids[0])
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(target_ids[0])
                    except Exception:
                        pass
                if not channel or getattr(channel, "guild", None) != interaction.guild:
                    await interaction.response.send_message(
                        "❌ You can only post to source channels located within this Discord server.",
                        ephemeral=True,
                    )
                    return
        else:
            if is_superadmin:
                target_ids = list(set(config.SOURCE_CHANNELS))
            else:
                target_ids = []
                for cid in set(config.SOURCE_CHANNELS):
                    c = self.bot.get_channel(cid)
                    if not c:
                        try:
                            c = await self.bot.fetch_channel(cid)
                        except Exception:
                            continue
                    if c and getattr(c, "guild", None) == interaction.guild:
                        target_ids.append(cid)

                if not target_ids:
                    await interaction.response.send_message(
                        "❌ No source channels are configured for this server. Please specify a valid channel_id or ask an admin to link a channel.",
                        ephemeral=True,
                    )
                    return

        await interaction.response.defer(thinking=True, ephemeral=True)

        successes = []
        errors = []

        for target_id in target_ids:
            try:
                channel = self.bot.get_channel(target_id) or await self.bot.fetch_channel(target_id)
            except (discord.NotFound, discord.Forbidden):
                errors.append(f"<#{target_id}>: Could not fetch channel")
                continue

            try:
                if isinstance(channel, discord.ForumChannel):
                    thread_with_message = await channel.create_thread(
                        name=game_name,
                        content=link,
                    )
                    url = thread_with_message.thread.jump_url if hasattr(thread_with_message, "thread") else None
                    successes.append(f"<#{target_id}>: [Thread]({url})" if url else f"<#{target_id}>: Thread created")
                elif isinstance(channel, discord.TextChannel):
                    message = await channel.send(content=f"**{game_name}**\n{link}")
                    successes.append(f"<#{target_id}>: [Message]({message.jump_url})")
                else:
                    errors.append(f"<#{target_id}>: Neither a Forum nor a TextChannel")
            except Exception as e:
                logger.exception("Failed to post new game to %s", target_id)
                errors.append(f"<#{target_id}>: Error - {e}")

        msg = f"**{game_name}**\n"
        if successes:
            msg += "✅ **Posted in:**\n" + "\n".join(successes) + "\n"
        if errors:
            msg += "❌ **Failed in:**\n" + "\n".join(errors)

        await interaction.followup.send(msg, ephemeral=True)
