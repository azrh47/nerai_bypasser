"""Steam app list cache for name normalization and ID lookup.

The cache is refreshed daily (configurable). Lookups support both:
* exact ``lookup_id(app_id)`` returning the canonical Steam name, and
* fuzzy ``fuzzy_lookup_id(query)`` returning ranked ``[(app_id, name, score)]``.

Fuzzy lookups use ``rapidfuzz.process.extract`` which is CPU-bound; we run it
in a thread executor so the bot's event loop is never blocked.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
import httpx

import config

logger = logging.getLogger(__name__)

STEAM_APPLIST_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"


class SteamCache:
    """Daily-refreshed Steam app list persisted in the same SQLite database."""

    def __init__(
        self,
        db_path: str,
        refresh_after: timedelta = timedelta(hours=24),
    ) -> None:
        self.db_path = db_path
        self.refresh_after = refresh_after
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Ensure the apps + settings tables exist (schema.sql does the work)."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")

    async def _last_fetched_at(self) -> Optional[datetime]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT value FROM settings WHERE key='steam_cache_fetched_at'"
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        try:
            return datetime.fromisoformat(row["value"])
        except ValueError:
            return None

    async def _ensure_fresh(self) -> None:
        last = await self._last_fetched_at()
        if last is None:
            await self.refresh()
            return
        if datetime.now(timezone.utc) - last > self.refresh_after:
            await self.refresh()

    async def refresh(self) -> int:
        """Fetch the full Steam app list and replace the cache. Returns count stored."""
        async with self._lock:
            if not config.STEAM_API_KEY:
                logger.error("STEAM_API_KEY is not set. Cannot fetch Steam app list. Set it in .env.")
                return 0

            logger.info("Fetching Steam app list from %s", STEAM_APPLIST_URL)
            applist = []
            last_appid = 0
            have_more_results = True
            
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    while have_more_results:
                        params = {
                            "key": config.STEAM_API_KEY,
                            "max_results": 50000,
                            "last_appid": last_appid
                        }
                        response = await client.get(STEAM_APPLIST_URL, params=params)
                        response.raise_for_status()
                        payload = response.json().get("response", {})
                        
                        apps = payload.get("apps", [])
                        applist.extend(apps)
                        
                        have_more_results = payload.get("have_more_results", False)
                        last_appid = payload.get("last_appid", last_appid)
                        
                        if not apps:
                            break
                            
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "Failed to fetch Steam app list (%s). Cache unchanged.", exc
                )
                return 0

            logger.info("Steam returned %d apps", len(applist))
            if not applist:
                return 0

            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute("BEGIN")
                await conn.execute("DELETE FROM apps")
                await conn.executemany(
                    "INSERT OR REPLACE INTO apps (app_id, name) VALUES (?, ?)",
                    ((int(a["appid"]), str(a["name"])) for a in applist),
                )
                await conn.execute(
                    """
                    INSERT INTO settings (key, value) VALUES
                        ('steam_cache_fetched_at', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
                )
                await conn.commit()
            return len(applist)

    async def lookup_id(self, app_id: int) -> Optional[str]:
        await self._ensure_fresh()
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT name FROM apps WHERE app_id = ?", (int(app_id),)
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def fuzzy_lookup_id(
        self, query: str, limit: int = 10, score_cutoff: int = 60
    ) -> list[tuple[int, str, int]]:
        """Return top ``limit`` matching ``(app_id, name, score)`` for ``query``.

        Score is the ``rapidfuzz`` ratio (0-100). May be empty.
        """
        await self._ensure_fresh()
        query = query.strip()
        if not query:
            return []

        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute("SELECT app_id, name FROM apps") as cur:
                rows = await cur.fetchall()
        if not rows:
            return []

        choices = [(int(r[0]), str(r[1])) for r in rows]
        # Build a names-only list for rapidfuzz; map back via index.
        names = [c[1] for c in choices]

        from rapidfuzz import process  # local import: hot path stays light

        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(
            None,
            lambda: process.extract(
                query,
                names,
                limit=limit,
                score_cutoff=score_cutoff,
            ),
        )
        return [(choices[idx][0], choices[idx][1], int(score)) for _, score, idx in raw]

    async def size(self) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute("SELECT COUNT(*) FROM apps") as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0
