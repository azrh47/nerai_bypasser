"""Wishlist cog for user notifications on new game additions."""
from __future__ import annotations

import logging
import discord
from discord import app_commands
from discord.ext import commands

from database import Database

logger = logging.getLogger(__name__)


class Wishlist(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    wishlist_group = app_commands.Group(
        name="wishlist",
        description="Manage your personal game notifications wishlist.",
    )

    @wishlist_group.command(
        name="add",
        description="Add a game to your wishlist to get DM'd when it is uploaded.",
    )
    async def add_cmd(self, interaction: discord.Interaction, game_name: str) -> None:
        if len(game_name) < 2:
            await interaction.response.send_message(
                "❌ Please provide a longer game name.", ephemeral=True
            )
            return

        await self.db.add_wishlist(interaction.user.id, game_name)
        await interaction.response.send_message(
            f"✅ Added **{game_name}** to your wishlist! You will receive a DM as soon as a link is posted.",
            ephemeral=True,
        )

    @wishlist_group.command(
        name="remove",
        description="Remove a game from your wishlist.",
    )
    async def remove_cmd(
        self, interaction: discord.Interaction, game_name: str
    ) -> None:
        removed = await self.db.remove_wishlist(interaction.user.id, game_name)
        if removed:
            await interaction.response.send_message(
                f"✅ Removed **{game_name}** from your wishlist.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ **{game_name}** was not found in your wishlist.", ephemeral=True
            )

    @wishlist_group.command(
        name="list",
        description="View all games currently in your wishlist.",
    )
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        items = await self.db.get_user_wishlists(interaction.user.id)
        if not items:
            await interaction.response.send_message(
                "📭 Your wishlist is empty. Use `/wishlist add <game>` to add one!",
                ephemeral=True,
            )
            return

        formatted = "\n".join(f"• **{item}**" for item in items)
        embed = discord.Embed(
            title="🎯 Your Game Wishlist",
            description=f"You will be notified via DM when any of these are uploaded:\n\n{formatted}",
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    db: Database = bot.db  # type: ignore[attr-defined]
    await bot.add_cog(Wishlist(bot, db))
