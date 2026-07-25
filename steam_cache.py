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

import re
import aiosqlite
import httpx

import config

logger = logging.getLogger(__name__)

STEAM_APPLIST_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"


def clean_game_name(text: str) -> str:
    """Strip release tags, group prefixes, and repack noise from a game title."""
    if not text:
        return ""
    s = str(text)
    # 1. Strip leading group prefixes like "RAGE - ", "RUNE - ", "FLT | ", "CODEX : "
    s = re.sub(r"^[A-Za-z0-9_]{2,10}\s*[-|:]\s+", "", s)
    # 2. Strip leading bracketed tags like "[RUNE] ", "[FitGirl Repack] "
    s = re.sub(r"^\[.*?\]\s*", "", s)
    # 3. Strip trailing parenthetical or bracketed tags like "(+12 DLCs)", "[FitGirl Repack]"
    s = re.sub(r"\s*[\(\[].*?[\)\]]\s*$", "", s)
    # 4. Strip common repack/piracy tags as whole words
    s = re.sub(
        r"\b(BYPASS|REPACK|CRACK|CRACKED|HOTFIX|PATCH|FIX|PRE-?INSTALLED|FREE DOWNLOAD|GOG|MULTI\d*|ONLINE|V\d+(?:\.\d+)*)\b",
        "",
        s,
        flags=re.IGNORECASE,
    )
    # 5. Clean up multiple spaces and trailing separators
    s = re.sub(r"\s+", " ", s).strip(" -|_!.")
    return s


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
        self._apps_cache: list[tuple[int, str]] = []
        self._names_cache: list[str] = []
        self._id_map_cache: dict[int, str] = {}

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

    async def _load_memory_cache(self) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                async with conn.execute("SELECT app_id, name FROM apps") as cur:
                    rows = await cur.fetchall()
            self._apps_cache = [(int(r[0]), str(r[1])) for r in rows]
            self._names_cache = [c[1] for c in self._apps_cache]
            self._id_map_cache = {c[0]: c[1] for c in self._apps_cache}
        except Exception as exc:
            logger.warning("Failed to load Steam cache into memory: %s", exc)

    async def _ensure_fresh(self) -> None:
        last = await self._last_fetched_at()
        if last is None:
            await self.refresh()
            return
        if datetime.now(timezone.utc) - last > self.refresh_after:
            await self.refresh()
            return
        if not self._apps_cache:
            await self._load_memory_cache()

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
            await self._load_memory_cache()
            return len(applist)

    async def lookup_id(self, app_id: int) -> Optional[str]:
        await self._ensure_fresh()
        return self._id_map_cache.get(int(app_id))

    async def fuzzy_lookup_id(
        self, query: str, limit: int = 10, score_cutoff: int = 60
    ) -> list[tuple[int, str, int]]:
        """Return top ``limit`` matching ``(app_id, name, score)`` for ``query``.

        Score is the ``rapidfuzz`` ratio (0-100). May be empty.
        """
        await self._ensure_fresh()
        query = query.strip()
        if not query or not self._apps_cache:
            return []

        from rapidfuzz import process, fuzz, utils  # local import: hot path stays light

        cleaned_query = clean_game_name(query)
        if not cleaned_query:
            cleaned_query = query

        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(
            None,
            lambda: process.extract(
                cleaned_query,
                self._names_cache,
                scorer=fuzz.token_set_ratio,
                processor=utils.default_process,
                limit=limit,
                score_cutoff=score_cutoff,
            ),
        )
        return [(self._apps_cache[idx][0], self._apps_cache[idx][1], int(score)) for _, score, idx in raw]

    async def size(self) -> int:
        await self._ensure_fresh()
        return len(self._apps_cache)
