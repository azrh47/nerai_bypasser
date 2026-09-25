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
import inspect
import json
import logging
import os
import random
import sys

import discord
from discord.ext import commands
from aiohttp import web

import config
from database import Database
from steam_cache import SteamCache

SOURCE_CHANNELS_SETTING_KEY = "source_channels"

# Login retry tuning.
#
# Discord's Cloudflare layer can return Error 1015 / 429 during login when the
# egress IP has been temporarily blocked. Those blocks routinely outlast a
# handful of retries, and *giving up* is what makes them self-sustaining:
# exiting hands control back to Render, whose restart policy re-runs main()
# from scratch and resets the backoff to LOGIN_BASE_BACKOFF_SEC -- so the same
# block gets hammered every few minutes instead of being left alone to expire.
# The health server in this process already answers Render's probe, so the
# deploy is marked live either way; staying up and backing off is strictly
# better than restarting into the block.
#
# Therefore LOGIN_MAX_RETRIES defaults to 0, meaning "retry forever". Set it to
# a positive integer to restore bounded behaviour (useful if you want a
# permanently-blocked egress IP to surface as a non-zero exit instead of a
# process that idles until the platform kills it).
LOGIN_MAX_RETRIES = int(os.getenv("LOGIN_MAX_RETRIES", "0"))
LOGIN_BASE_BACKOFF_SEC = float(os.getenv("LOGIN_BASE_BACKOFF_SEC", "15"))
LOGIN_MAX_BACKOFF_SEC = float(os.getenv("LOGIN_MAX_BACKOFF_SEC", "600"))


def _is_login_rate_limit(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like a Discord/Cloudflare login rate limit.

    discord.py surfaces the HTTP response payload via ``HTTPException.response``
    (an ``aiohttp.ClientResponse``). A Cloudflare block page is HTML, not JSON,
    and the status is 429, so we check both the status code and the content type.
    """
    if not isinstance(exc, discord.HTTPException):
        return False
    if exc.status != 429:
        return False
    resp = getattr(exc, "response", None)
    if resp is None:
        return True
    content_type = resp.content_type
    return content_type is None or not content_type.startswith("application/json")


async def _close_http_session(bot: commands.Bot) -> None:
    """Close the HTTP session stranded by a failed ``login()``.

    discord.py 2.x's ``HTTPClient.static_login`` builds a brand-new
    ``aiohttp.ClientSession`` on EVERY call and simply overwrites
    ``self.__session`` on the next attempt -- it does not close the previous
    one when ``/users/@me`` raises. Each retry therefore leaks a session,
    which aiohttp reports as ``Unclosed client session`` when it is garbage
    collected (exactly what the production logs showed between retries).

    Safe to call repeatedly: ``HTTPClient.close()`` closes the underlying
    session but not the reusable ``TCPConnector`` it was built with, so the next
    ``login()`` still works. Clients whose ``.http`` is absent or not
    awaitable (tests, a partially-constructed bot) are ignored, hence the
    ``isawaitable`` guard rather than a bare ``await``.
    """
    http = getattr(bot, "http", None)
    close = getattr(http, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logging.warning(
            "Failed to close leaked HTTP session after a failed login",
            exc_info=True,
        )


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    # Render health-probes this port every few seconds, and the keep-alive
    # heartbeat adds its own requests on top. aiohttp's access log prints one
    # line per request at INFO, which drowns out the bot's own logs in the
    # Render dashboard. Keep warnings/errors (real handler failures) visible.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


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

        # Populate the Steam cache on startup so fuzzy lookups work immediately.
        # This takes ~10s but the health server is already running, so Render won't kill us.
        try:
            await self.steam._ensure_fresh()
            repaired = await self.db.repair_canonical_names(self.steam)
            if repaired > 0:
                logging.info("Auto-repaired %d database entries on startup", repaired)
        except Exception as exc:
            logging.warning(
                "Failed to refresh Steam cache or repair DB on startup: %s", exc
            )

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
        from cogs.wishlist import Wishlist
        from cogs.uploader import Uploader

        await self.add_cog(Indexer(self, self.db, self.steam))
        await self.add_cog(Search(self, self.db, self.steam))
        await self.add_cog(Admin(self, self.db, self.steam))
        await self.add_cog(Wishlist(self, self.db))
        await self.add_cog(Uploader(self))

        # Keep-alive cog is optional: it only matters on platforms that sleep
        # idle web services (Render free tier, etc.). On an always-on host it
        # is a harmless no-op, so we load it unconditionally as long as the
        # module is present.
        try:
            from cogs.keep_alive import KeepAlive

            await self.add_cog(
                KeepAlive(
                    self,
                    health_base_url=os.getenv(
                        "KEEP_ALIVE_BASE_URL", "http://127.0.0.1:10000"
                    ),
                )
            )
            # Interval / enabled-vs-disabled detail is logged by the cog itself
            # once it starts (or skips starting), so don't restate the knobs
            # here where they could drift out of sync with the cog's guard.
            logging.info(
                "Loaded keep-alive cog (probing %s)",
                os.getenv("KEEP_ALIVE_BASE_URL", "http://127.0.0.1:10000"),
            )
        except Exception:
            logging.debug(
                "Keep-alive cog not available; skipping", exc_info=True
            )

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


async def _login_with_retry(bot: GameIndexerBot) -> None:
    """Log in to Discord, retrying only on Cloudflare-style login rate limits.

    Retries indefinitely by default (``LOGIN_MAX_RETRIES<=0``) with capped
    exponential backoff. Handing control back to Render mid-block is worse
    than waiting it out: Render's restart policy re-runs ``main()`` from
    scratch, resetting the backoff to ``LOGIN_BASE_BACKOFF_SEC`` and pounding
    the same blocked endpoint every few minutes -- which keeps the block
    alive. The health server is already serving, so an idle-and-backed-off
    process looks exactly as healthy to Render as a logged-in one.

    Deliberately calls ``bot.login()``, never ``bot.start()``:

    * ``start()`` is just ``login() + connect()``, and discord.py calls
      ``setup_hook()`` from *inside* ``login()`` (discord/client.py).
    * Our ``setup_hook()`` calls ``add_cog(...)`` five times, and ``add_cog``
      raises ``ClientException: Cog named 'Indexer' already loaded`` on a
      second registration.

    So retrying the whole ``start()`` would re-run setup on attempt 2 and turn
    a transient ~18s rate limit into a crash loop -- exactly the failure this
    wrapper exists to prevent. ``connect()`` is called once, after a successful
    login; it handles its own reconnects internally.

    Non-login failures (KeyboardInterrupt, gateway hard errors, a genuinely
    malformed token) are re-raised immediately so Render observes the real
    failure mode instead of us hiding it behind retries.
    """
    retry = 0
    while True:
        try:
            await bot.login(config.DISCORD_TOKEN)
            return
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if not _is_login_rate_limit(exc):
                raise
            retry += 1
            # LOGIN_MAX_RETRIES <= 0 means "no ceiling": keep backing off until
            # Cloudflare lets us through. Only a positive value is bounded.
            if LOGIN_MAX_RETRIES > 0 and retry > LOGIN_MAX_RETRIES:
                # ``retry`` is the attempt counter, so the number of retries
                # actually performed is LOGIN_MAX_RETRIES -- logging the raw
                # counter here would overstate it by one.
                logging.error(
                    "Login rate-limited after %d retries; giving up",
                    LOGIN_MAX_RETRIES,
                )
                raise
            # Reclaim the session the failed attempt leaked before we wait;
            # otherwise a long block accumulates one unclosed session per
            # retry (and one aiohttp "Unclosed client session" warning each).
            await _close_http_session(bot)
            backoff = min(
                LOGIN_MAX_BACKOFF_SEC,
                LOGIN_BASE_BACKOFF_SEC * (2 ** (retry - 1)),
            )
            # Small jitter so a fleet of retries does not land on the same
            # second and re-trigger the block immediately.
            jitter = random.uniform(0, backoff * 0.25)
            sleep_for = backoff + jitter
            if LOGIN_MAX_RETRIES > 0:
                logging.warning(
                    "Login rate-limited (attempt %d/%d); backing off %.1fs",
                    retry,
                    LOGIN_MAX_RETRIES,
                    sleep_for,
                )
            else:
                logging.warning(
                    "Login rate-limited (attempt %d); backing off %.1fs",
                    retry,
                    sleep_for,
                )
            await asyncio.sleep(sleep_for)


async def _close_db(db: Database) -> None:
    """Shared teardown step for the SQLite connection.

    ``Database`` used to expose ``close()`` in its signature, and several
    callers wrote ``await db.close()`` expecting a real coroutine. It is a
    no-op today, but keeping the shutdown path in one place makes the
    contract explicit and guarantees a failed ``.close()`` can never
    short-circuit the rest of the shutdown sequence.
    """
    try:
        await db.close()
    except Exception:
        logging.exception("Failed to close database on shutdown")


async def main() -> None:
    _configure_logging()
    db = Database(config.DATABASE_PATH, config.SCHEMA_PATH)
    steam = SteamCache(config.DATABASE_PATH)
    bot = GameIndexerBot(db, steam)

    # Start the health server BEFORE the Discord gateway so the probe
    # succeeds even during the multi-second Steam app list cache warmup.
    health_runner = await _start_health_server()
    try:
        await _login_with_retry(bot)
        await bot.connect()
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

        await _close_db(db)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        logging.exception("Unhandled error in main(); exiting")
        sys.exit(1)
