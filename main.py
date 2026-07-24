"""Bot entry point.

Wires together ``Database``, ``SteamCache``, and the three cogs (Indexer,
Search, Admin). On startup it bootstraps the SQLite schema, refreshes the
Steam app list cache if stale, and syncs slash commands to the configured
target guild.

A tiny aiohttp server is started on ``$PORT`` (Render default 10000) so the
``type: web`` Render service satisfies its HTTP health probe. Without it
the probe times out and Render marks the deploy unhealthy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import discord
from discord.ext import commands
from aiohttp import web

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


async def _health_response(_request: web.Request) -> web.Response:
    """Trivial 200 OK handler for Render's HTTP health probe.

    Always returns the same body. Does NOT try to introspect the bot's
    Discord connection state: when the gateway is reconnecting (eg after
    an idle-sleep wake-up) we still want the probe to pass so Render keeps
    the container alive long enough for the resume-backfill to complete.
    """
    return web.Response(text="OK")


def _parse_port() -> int:
    """Return the TCP port to bind the health server to.

    Reads ``$PORT`` (Render / Heroku both set this for web services) and
    falls back to ``10000`` (Render's documented default). Why the ``or``:
    ``getenv`` returns the empty string if ``PORT`` was unset-then-set to
    blank, which would make plain ``int("")`` raise ValueError and crash
    the service before Discord ever gets touched.
    """
    raw = os.getenv("PORT") or "10000"
    try:
        return int(raw)
    except ValueError:
        logging.warning(
            "PORT=%r is not an int; falling back to 10000", raw
        )
        return 10000


def _build_health_app() -> web.Application:
    """Construct the aiohttp Application that serves / and /healthz.

    Extracted so tests can verify the same Application object the
    production code wires up; a future change that adds/removes a route
    must move both call sites together, so the production server and
    the registration test cannot drift out of sync.
    """
    app = web.Application()
    app.router.add_get("/", _health_response)
    app.router.add_get("/healthz", _health_response)
    return app


async def _start_health_server() -> web.AppRunner:
    """Bind a 0.0.0.0:$PORT aiohttp server so ``type: web`` platforms pass
    health probes.

    Returns the ``AppRunner``; the caller is responsible for ``cleanup()``
    it on shutdown. If the port-bind fails (EADDRINUSE, permission denied,
    etc.) the partial runner is cleaned up before re-raising so a
    deploy-time port conflict doesn't leak file descriptors.
    """
    app = _build_health_app()
    runner = web.AppRunner(app)
    await runner.setup()
    port = _parse_port()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    try:
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    logging.info("Health check server listening on 0.0.0.0:%s", port)
    return runner


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

        if config.TARGET_GUILD_IDS:
            for gid in config.TARGET_GUILD_IDS:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logging.info("Synced slash commands to guild %s", gid)
        else:
            await self.tree.sync()
            logging.info("Synced slash commands globally")

        logging.info("Env summary: %s", config.env_summary())


async def main() -> None:
    _configure_logging()
    db = Database(config.DATABASE_PATH, config.SCHEMA_PATH)
    steam = SteamCache(config.DATABASE_PATH)
    bot = GameIndexerBot(db, steam)

    # Start the health server BEFORE the Discord gateway so the probe
    # succeeds even during the multi-second Steam app list cache warmup.
    health_runner = await _start_health_server()
    try:
        await bot.start(config.DISCORD_TOKEN)
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    finally:
        # Each cleanup step is wrapped independently so a failure in one
        # doesn't short-circuit and leak the other resource. A messy
        # shutdown (e.g. Discord gateway already in a bad state when
        # SIGTERM arrives) must still tear down the HTTP server cleanly
        # so the next process start can rebind the port.
        try:
            if not bot.is_closed():
                await bot.close()
        except Exception:
            logging.exception("Error during bot.close(); continuing shutdown")
        try:
            await health_runner.cleanup()
        except Exception:
            logging.exception(
                "Error during health runner cleanup; continuing shutdown"
            )
        logging.info("Health server stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
