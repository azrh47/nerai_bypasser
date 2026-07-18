"""Bot entry point.

Wires together ``Database``, ``SteamCache``, and the three cogs (Indexer,
Search, Admin). On startup it bootstraps the SQLite schema, refreshes the
Steam app list cache if stale, and syncs slash commands to the configured
target guild.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

import discord
from discord.ext import commands

import config
from database import Database
from steam_cache import SteamCache

SOURCE_CHANNELS_SETTING_KEY = "source_channels"


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("discord.http").setLevel(logging.WARNING)


async def _load_runtime_source_channels(db: Database) -> list[int]:
    raw = await db.get_setting(SOURCE_CHANNELS_SETTING_KEY)
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [int(x) for x in decoded if str(x).isdigit()]


class GameIndexerBot(commands.Bot):
    def __init__(self, db: Database, steam: SteamCache) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # required to read message bodies
        intents.messages = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )
        self.db = db
        self.steam = steam

    async def setup_hook(self) -> None:
        await self.db.initialize()
        await self.steam.initialize()

        # Hydrate config.SOURCE_CHANNELS from any previously-registered list.
        runtime = await _load_runtime_source_channels(self.db)
        seen = set(config.SOURCE_CHANNELS)
        for cid in runtime:
            if cid not in seen:
                config.SOURCE_CHANNELS.append(cid)
                seen.add(cid)
        await self.db.set_setting(
            SOURCE_CHANNELS_SETTING_KEY,
            json.dumps(config.SOURCE_CHANNELS),
        )

        from cogs.indexer import Indexer
        from cogs.search import Search
        from cogs.admin import Admin

        await self.add_cog(Indexer(self, self.db, self.steam))
        await self.add_cog(Search(self, self.db, self.steam))
        await self.add_cog(Admin(self, self.db, self.steam))

        if config.TARGET_GUILD_ID:
            guild = discord.Object(id=config.TARGET_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logging.info(
                "Synced slash commands to guild %s",
                config.TARGET_GUILD_ID,
            )
        else:
            await self.tree.sync()
            logging.info("Synced slash commands globally")

        logging.info("Env summary: %s", config.env_summary())


async def main() -> None:
    _configure_logging()
    db = Database(config.DATABASE_PATH, config.SCHEMA_PATH)
    steam = SteamCache(config.DATABASE_PATH)
    bot = GameIndexerBot(db, steam)
    try:
        await bot.start(config.DISCORD_TOKEN)
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
