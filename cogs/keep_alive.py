"""Keep-alive cog for Render's free/starter web service idle-sleep behavior.

Render's ``type: web`` services are not "always-on" on the free tier; they
idle-sleep after a period of inactivity and need an external pinger to stay
warm. Even on paid tiers it is common for operators to want an internal
heartbeat so the service is clearly alive from the inside.

This cog ticks from inside the bot's event loop: every
``KEEP_ALIVE_INTERVAL_SEC`` seconds it issues a single HTTP GET to the
service's own ``/healthz`` (and ``/``) endpoint.

WHAT THIS DOES NOT DO: it does not keep a sleeping service awake. Render's
idle timer is driven by traffic arriving over the public routing layer, and a
request from inside the container to ``127.0.0.1`` never leaves the container,
so Render cannot see it. Render's own health probes hit the port every few
seconds and free services still spin down -- which is the proof that requests
which don't come through the public URL don't reset the timer. Keeping a
``type: web`` service on the free tier awake requires an EXTERNAL pinger such
as UptimeRobot (or a scheduled GitHub Action / cron-job.org) hitting
``https://<service>.onrender.com/healthz`` every few minutes.

What this cog *is* good for: an internal liveness heartbeat that exercises the
health server and makes a wedged event loop visible in the logs, which is easy
to leave enabled and costs two requests per interval.

It is safe to leave loaded on any platform. On a host that is already always-on
the pings are harmless no-ops, and ``KEEP_ALIVE_INTERVAL_SEC=0`` turns the
heartbeat off entirely while keeping the cog loaded.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import aiohttp
from discord.ext import commands

logger = logging.getLogger(__name__)

_KEEP_ALIVE_INTERVAL_SEC = int(os.getenv("KEEP_ALIVE_INTERVAL_SEC", "600"))
_KEEP_ALIVE_TIMEOUT_SEC = int(os.getenv("KEEP_ALIVE_TIMEOUT_SEC", "10"))


class KeepAlive(commands.Cog):
    def __init__(self, bot: commands.Bot, health_base_url: str) -> None:
        self.bot = bot
        self.health_base_url = health_base_url.rstrip("/")
        self._task: Optional[asyncio.Task[None]] = None

    async def cog_load(self) -> None:
        # 0 (or negative) means "loaded but disabled": the operator explicitly
        # wants no heartbeat, e.g. on a platform that is already always-on.
        # Guarding here rather than sleeping 0 keeps us from spinning the event
        # loop and hammering our own health server.
        if _KEEP_ALIVE_INTERVAL_SEC <= 0:
            logger.info(
                "KeepAlive disabled (KEEP_ALIVE_INTERVAL_SEC=%s)",
                _KEEP_ALIVE_INTERVAL_SEC,
            )
            return
        # Start the heartbeat after the cog is fully attached. Starting it in
        # __init__ is too early: the bot may not be ready yet and we do not
        # want the first tick to fire before the health server is bound.
        self._task = self.bot.loop.create_task(self._run())

    async def cog_unload(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        await self.bot.wait_until_ready()
        logger.info(
            "KeepAlive cog started: tick every %.1f min to %s",
            _KEEP_ALIVE_INTERVAL_SEC / 60.0,
            self.health_base_url,
        )
        try:
            while not self.bot.is_closed():
                await self._tick()
                await asyncio.sleep(_KEEP_ALIVE_INTERVAL_SEC)
        except asyncio.CancelledError:
            logger.info("KeepAlive cog stopped")

    async def _tick(self) -> None:
        # Two lightweight endpoints so the keep-alive work is clearly visible
        # as real HTTP traffic on whichever probe Render ends up using.
        # Each is probed independently: one failing endpoint must not stop the
        # other from being hit.
        for url in (
            f"{self.health_base_url}/healthz",
            f"{self.health_base_url}/",
        ):
            await self._get(url)

    async def _get(self, url: str) -> None:
        # Bound the client lifetime to a single request so we never leak
        # connections across ticks.
        timeout = aiohttp.ClientTimeout(total=_KEEP_ALIVE_TIMEOUT_SEC)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    await response.read()
                    logger.debug("KeepAlive tick: %s -> %s", url, response.status)
        except asyncio.CancelledError:
            # Shutdown must still propagate; only *probe* failures are absorbed.
            raise
        except Exception as exc:
            # One failed probe (timeout, connection refused mid-redeploy, DNS
            # blip) must never kill the heartbeat: a keep-alive task that dies
            # on the first error is worse than none at all, because the logs
            # look healthy while the service quietly sleeps again.
            logger.warning("KeepAlive tick to %s failed: %s", url, exc)


async def setup(bot: commands.Bot) -> KeepAlive:
    health_base_url = os.getenv("KEEP_ALIVE_BASE_URL", "http://127.0.0.1:10000")
    cog = KeepAlive(bot, health_base_url)
    await bot.add_cog(cog)
    return cog
