"""Async SQLite layer (aiosqlite).

Holds the schema bootstrap and CRUD helpers used by every cog. Designed for a
single writer + many readers in one event loop (the Discord bot).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import aiosqlite

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _coerce_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(value)


class Database:
    """Thin async wrapper around aiosqlite."""

    def __init__(self, db_path: str, schema_path: str) -> None:
        self.db_path = db_path
        self.schema_path = schema_path

    @staticmethod
    async def _apply_pragmas(conn: aiosqlite.Connection) -> None:
        # journal_mode returns a row but is non-blocking per-connection.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")

    async def initialize(self) -> None:
        """Create the database file and apply schema.sql."""
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        schema_file = Path(self.schema_path)
        if not schema_file.exists():
            raise FileNotFoundError(f"Schema file not found: {schema_file}")

        async with aiosqlite.connect(self.db_path) as conn:
            await self._apply_pragmas(conn)
            schema_sql = schema_file.read_text(encoding="utf-8")
            await conn.executescript(schema_sql)
            await conn.commit()
        logger.info("Database initialized at %s", self.db_path)

    # ---------- Sources -----------------------------------------------------

    async def upsert_source(
        self,
        channel_id: int,
        guild_id: int,
        last_seen_message_id: Optional[int] = None,
        status: str = "active",
    ) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT INTO sources (channel_id, guild_id, last_seen_message_id, last_seen_at, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    guild_id=excluded.guild_id,
                    last_seen_message_id=COALESCE(
                        excluded.last_seen_message_id, sources.last_seen_message_id
                    ),
                    last_seen_at=excluded.last_seen_at,
                    status=excluded.status
                """,
                (channel_id, guild_id, last_seen_message_id, _now_iso(), status),
            )
            await conn.commit()

    async def update_source_progress(
        self,
        channel_id: int,
        last_seen_message_id: int,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                UPDATE sources
                SET last_seen_message_id = ?, last_seen_at = ?
                WHERE channel_id = ?
                """,
                (last_seen_message_id, _now_iso(), channel_id),
            )
            await conn.commit()

    async def set_source_status(self, channel_id: int, status: str) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE sources SET status = ? WHERE channel_id = ?",
                (status, channel_id),
            )
            await conn.commit()

    async def list_sources(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT channel_id, guild_id, last_seen_message_id, last_seen_at, status "
                "FROM sources ORDER BY channel_id"
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_source(self, channel_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM sources WHERE channel_id = ?", (channel_id,)
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    # ---------- Entries -----------------------------------------------------

    async def upsert_entry(self, entry: dict[str, Any]) -> int:
        """Insert or update an entry. Returns row id."""
        posted_at = _coerce_iso(entry.get("posted_at"))
        last_verified_at = _coerce_iso(entry.get("last_verified_at"))
        async with aiosqlite.connect(self.db_path) as conn:
            cur = await conn.execute(
                """
                INSERT INTO entries (
                    source_channel_id, source_message_id, source_author_id,
                    mega_url, game_name, canonical_name, app_id, filename,
                    size_bytes, is_folder, posted_at, is_stale, last_verified_at,
                    raw_text_excerpt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_channel_id, source_message_id, mega_url) DO UPDATE SET
                    source_author_id=excluded.source_author_id,
                    game_name=COALESCE(excluded.game_name, entries.game_name),
                    canonical_name=COALESCE(excluded.canonical_name, entries.canonical_name),
                    app_id=COALESCE(excluded.app_id, entries.app_id),
                    filename=COALESCE(excluded.filename, entries.filename),
                    size_bytes=COALESCE(excluded.size_bytes, entries.size_bytes),
                    is_folder=excluded.is_folder,
                    posted_at=COALESCE(excluded.posted_at, entries.posted_at),
                    is_stale=excluded.is_stale,
                    last_verified_at=excluded.last_verified_at,
                    raw_text_excerpt=COALESCE(
                        excluded.raw_text_excerpt, entries.raw_text_excerpt
                    )
                """,
                (
                    int(entry["source_channel_id"]),
                    int(entry["source_message_id"]),
                    entry.get("source_author_id"),
                    entry["mega_url"],
                    entry.get("game_name"),
                    entry.get("canonical_name"),
                    entry.get("app_id"),
                    entry.get("filename"),
                    entry.get("size_bytes"),
                    int(bool(entry.get("is_folder", False))),
                    posted_at,
                    int(bool(entry.get("is_stale", False))),
                    last_verified_at,
                    entry.get("raw_text_excerpt"),
                ),
            )
            row_id = cur.lastrowid
            await conn.commit()
        return int(row_id) if row_id else 0

    async def bulk_upsert_entries(self, entries: Iterable[dict[str, Any]]) -> int:
        n = 0
        for entry in entries:
            await self.upsert_entry(entry)
            n += 1
        return n

    async def search_entries(
        self,
        query: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Naive substring search over name fields. Autocomplete/resolver layers do their own ranking on top."""
        like = f"%{query.strip()}%"
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """
                SELECT id, mega_url, game_name, canonical_name, app_id,
                       filename, size_bytes, posted_at, is_stale
                FROM entries
                WHERE (? = '' AND 1=1) OR
                      game_name LIKE ? OR canonical_name LIKE ?
                ORDER BY posted_at DESC
                LIMIT ?
                """,
                (query.strip(), like, like, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_entries_by_app_id(
        self, app_id: int, limit: int = 25
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """
                SELECT id, mega_url, game_name, canonical_name, app_id,
                       filename, size_bytes, posted_at, is_stale
                FROM entries WHERE app_id = ?
                ORDER BY posted_at DESC LIMIT ?
                """,
                (app_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_entry_by_id(self, entry_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM entries WHERE id = ?", (entry_id,)
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_entry(self, entry_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            cur = await conn.execute(
                "DELETE FROM entries WHERE id = ?", (entry_id,)
            )
            await conn.commit()
            return cur.rowcount > 0

    async def count_entries(self) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute("SELECT COUNT(*) FROM entries") as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_entries_per_channel(self) -> dict[int, int]:
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT source_channel_id, COUNT(*) FROM entries "
                "GROUP BY source_channel_id"
            ) as cur:
                rows = await cur.fetchall()
        return {int(r[0]): int(r[1]) for r in rows}

    # ---------- Settings ----------------------------------------------------

    async def set_setting(self, key: str, value: Any) -> None:
        import json
        payload = value if isinstance(value, str) else json.dumps(value)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, payload),
            )
            await conn.commit()

    async def get_setting(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def delete_setting(self, key: str) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            await conn.commit()

    # ---------- Wishlists ---------------------------------------------------

    async def add_wishlist(self, user_id: int, query: str) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT INTO wishlists (user_id, query, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, query) DO NOTHING
                """,
                (user_id, query.strip().lower(), _now_iso()),
            )
            await conn.commit()

    async def remove_wishlist(self, user_id: int, query: str) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            cur = await conn.execute(
                "DELETE FROM wishlists WHERE user_id = ? AND query = ?",
                (user_id, query.strip().lower()),
            )
            await conn.commit()
            return cur.rowcount > 0

    async def get_user_wishlists(self, user_id: int) -> list[str]:
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT query FROM wishlists WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def get_matching_wishlists(self, game_name: str) -> list[tuple[int, str]]:
        """Find user_id and query pairs where game_name matches the query."""
        game_lower = game_name.strip().lower()
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute("SELECT user_id, query FROM wishlists") as cur:
                rows = await cur.fetchall()
        matches = []
        for user_id, query in rows:
            if query in game_lower or game_lower in query:
                matches.append((user_id, query))
        return matches

    async def repair_canonical_names(self, steam: Any) -> int:
        """Scan all DB entries and re-run fuzzy matching with updated scoring algorithms to fix wrong app_ids/canonical names."""
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT id, game_name, canonical_name, app_id FROM entries WHERE game_name IS NOT NULL AND game_name != ''"
            ) as cur:
                rows = await cur.fetchall()

        repaired = 0
        updates = []
        for row in rows:
            g_name = row["game_name"]
            if not g_name:
                continue
            fuzzy = await steam.fuzzy_lookup_id(g_name, limit=1, score_cutoff=60)
            if fuzzy:
                new_app_id, new_canonical, _score = fuzzy[0]
                old_app_id = row["app_id"]
                old_canonical = row["canonical_name"]
                if new_app_id != old_app_id or new_canonical != old_canonical:
                    updates.append((new_app_id, new_canonical, row["id"]))
                    repaired += 1

        if updates:
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.executemany(
                    "UPDATE entries SET app_id = ?, canonical_name = ? WHERE id = ?",
                    updates,
                )
                await conn.commit()
            logger.info("Repaired %d entries in database with corrected Steam IDs and canonical names.", repaired)
        return repaired
