"""Background indexer cog.

* On startup + Discord gateway reconnect: backfills every configured source
  channel from its last-seen message ID (resume) or from the very beginning if
  no progress has been recorded yet.
* On every new message in a source channel: incrementally parses and stores
  any Mega.nz link found.

Supported source channel shapes (discord.py 2.x):
* ``TextChannel`` -- tight ``history()`` loop, the original path.
* ``Thread``     -- same tight loop, walking the thread's messages.
* ``ForumChannel`` -- enumerate accessible threads inside the forum
  (``forum.threads`` cache + ``guild.fetch_active_threads()`` filtered by
  ``parent_id``), then run the same per-thread loop. ``source_channel_id``
  recorded in the DB is the FORUM's id, so admin stats group by forum.

A single ``asyncio.Lock`` keeps the heavy ``on_ready`` backfill from racing the
``on_resumed`` re-scan.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

import config
import parser
from database import Database
from steam_cache import SteamCache

logger = logging.getLogger(__name__)

# Discord history pagination: max 100/per request, ~5 req/2s budget.
HISTORY_FETCH_BATCH = 100
HISTORY_FETCH_SLEEP = 0.5

# Persist progress every N messages within a single backfill so a crash mid-run
# does not force a restart from the previous checkpoint.
CHECKPOINT_EVERY = 500


def _message_text(message: discord.Message) -> str:
    """Combine content + embed fields + Link-button URLs into one searchable string.

    Repack reposts in the source channel often hide the actual download URL
    inside a Discord ``Button`` component (rendered as ``CLICK ME!`` text)
    rather than in the message body. We walk ``message.components`` so those
    URLs are visible to the parser.
    """
    parts: list[str] = []
    if message.content:
        parts.append(message.content)
    for embed in message.embeds:
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        if embed.url:
            parts.append(embed.url)
        for field in embed.fields:
            if field.name:
                parts.append(field.name)
            if field.value:
                parts.append(field.value)
    parts.extend(_component_urls(message))
    return "\n".join(parts)


def _component_urls(message: discord.Message) -> list[str]:
    """Return every Link-style button URL inside ``message.components``.

    Discord groups buttons into ActionRows. Each row has a ``.children`` list;
    Link buttons expose a ``.url`` string while click-handler buttons do not.
    We deliberately include only Link URLs -- click-handler buttons have no
    off-Discord destination and would just add noise to the parser input.

    Partial-state guard: under discord.py 2.4+ a Gateway message with
    ``Intents.members`` partial state can raise on attribute access instead
    of returning an empty container. We catch the narrow set of exceptions
    that indicate partial/eager access failure (anything else is a real bug
    and should still propagate); failures are logged so a partial-message
    storm during a gateway reconnect is observable.
    """
    urls: list[str] = []
    try:
        components = message.components
    except (AttributeError, TypeError, discord.HTTPException):
        logger.warning(
            "Could not read message.components on message %s; skipping",
            getattr(message, "id", "?"),
        )
        return urls
    if not components:
        return urls
    for row in components:
        children = getattr(row, "children", None)
        if not children:
            continue
        for child in children:
            url = getattr(child, "url", None)
            if url:
                urls.append(url)
    return urls


def _resolve_source_cid(message: discord.Message) -> Optional[int]:
    """Return the SOURCE channel id for ``message``, or ``None`` if we can't
    safely determine it.

    The indexer stores its source identity as the CONTAINER of the message:
    * top-level ``TextChannel`` / ``ForumChannel`` messages -> the channel id
    * ``Thread`` messages (a "post" inside a forum)        -> ``parent_id``

    Without this distinction, every message posted inside a forum thread is
    silently dropped by ``on_message`` because ``message.channel.id `` is the
    *thread* id, not the forum we registered.
    """
    channel = getattr(message, "channel", None)
    if channel is None:
        return None
    if isinstance(channel, discord.Thread):
        return getattr(channel, "parent_id", None)
    return getattr(channel, "id", None)


class Indexer(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        steam: SteamCache,
    ) -> None:
        self.bot = bot
        self.db = db
        self.steam = steam
        self._indexing_lock = asyncio.Lock()

    # ---------- per-message ingest ----------------------------------------

    async def _canonicalize(
        self, entry: parser.ParsedEntry
    ) -> parser.ParsedEntry:
        if entry.app_id is not None and not entry.canonical_name:
            try:
                canonical = await self.steam.lookup_id(entry.app_id)
            except Exception:
                canonical = None
            if canonical:
                entry.canonical_name = canonical
                if not entry.game_name:
                    entry.game_name = canonical
        return entry

    async def _persist_entry(
        self, entry: parser.ParsedEntry, source_channel_id: int
    ) -> int:
        await self._canonicalize(entry)
        return await self.db.upsert_entry(
            {
                "source_channel_id": source_channel_id,
                "source_message_id": entry.source_message_id,
                "source_author_id": entry.source_author_id,
                "mega_url": entry.mega_url,
                "game_name": entry.game_name,
                "canonical_name": entry.canonical_name,
                "app_id": entry.app_id,
                "filename": entry.filename,
                "size_bytes": entry.size_bytes,
                "is_folder": entry.is_folder,
                "posted_at": entry.posted_at,
                "raw_text_excerpt": entry.raw_text_excerpt,
            }
        )

    async def _run_parser(
        self,
        message: discord.Message,
        source_channel_id: int,
    ) -> int:
        author_id = message.author.id if message.author else None
        posted_at = (
            message.created_at.isoformat(timespec="seconds")
            if message.created_at
            else datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        text = _message_text(message)
        entries = parser.parse_message(
            text,
            source_message_id=message.id,
            source_author_id=author_id,
            posted_at=posted_at,
        )
        count = 0
        for entry in entries:
            try:
                await self._persist_entry(entry, source_channel_id=source_channel_id)
                count += 1
            except Exception:
                logger.exception("Failed to persist entry: %s", entry.mega_url)
        return count

    # ---------- forum-aware helpers ---------------------------------------
    #
    # Discord Forum channels expose their messages inside ``Thread`` objects
    # ("posts"), not on the forum channel itself. To index a forum we therefore
    # enumerate its threads and then walk each thread's history. discord.py 2.x
    # has two reliable sources of threads for a forum:
    #
    #   * ``forum.threads``  -- gateway-cached list of currently-active threads
    #                           (cheap; usually incomplete for long-lived forums)
    #   * ``forum.guild.fetch_active_threads()`` -- one API call that returns
    #                           every active thread across the guild; filter by
    #                           ``parent_id`` to keep only this forum's threads
    #
    # Neither API exposes ARCHIVED forum threads cleanly in 2.7. Discord's REST
    # endpoints ``GET /channels/{id}/threads/archived/{public,private}`` exist
    # but are not wrapped by ``ForumChannel``. We lean on Discord's eventual
    # consistency: a freshly-archived thread typically appears in
    # ``fetch_active_threads`` for a brief window before flipping to archived,
    # and ``on_message`` covers threads that go live afterwards. Manual
    # ``/admin reseed`` is the user-driven escape hatch -- acceptable for a
    # bot whose operator is actively monitoring the index.

    async def _collect_forum_threads(
        self, forum: discord.ForumChannel
    ) -> list[discord.Thread]:
        """Return every accessible active thread inside ``forum``.

        Order: gateway-cached threads first (cheapest iteration), then any
        additional active threads surfaced by ``guild.fetch_active_threads``
        that the gateway doesn't yet know about. The two lists are deduped
        by thread id.
        """
        seen: set[int] = set()
        out: list[discord.Thread] = []
        # Step 1: gateway cache. ``forum.threads`` may be None during partial
        # state; treat as empty.
        try:
            for thread in (getattr(forum, "threads", None) or []):
                if thread.id not in seen:
                    seen.add(thread.id)
                    out.append(thread)
        except Exception:
            logger.exception(
                "Failed to read forum.threads property for %s", forum.id
            )
        # Step 2: guild-wide active threads (one HTTP call per channel index
        # pass; acceptable for a bot that indexer-pauses the bot's event loop
        # in a Lock). On a populated guild this returns at most 100 threads
        # which is sufficient for "active" filtering in practice.
        try:
            guild_active = await forum.guild.fetch_active_threads()
        except (discord.HTTPException, discord.Forbidden) as exc:
            logger.warning(
                "fetch_active_threads failed for guild %s: %s; "
                "falling back to cache-only list",
                forum.guild.id,
                exc,
            )
            guild_active = []
        except Exception:
            logger.exception(
                "Unexpected error fetching active threads for guild %s",
                forum.guild.id,
            )
            guild_active = []
        for thread in guild_active:
            if thread.parent_id == forum.id and thread.id not in seen:
                seen.add(thread.id)
                out.append(thread)
        return out

    async def _iter_thread_messages(
        self,
        thread: discord.Thread,
        source_channel_id: int,
        after_id: Optional[int],
    ) -> tuple[int, int]:
        """Iterate ``thread.history(...)`` oldest-first; persist entries.

        Returns ``(indexed, last_message_id)``. Honors ``after_id`` for
        incremental resumes; checkpoints every ``CHECKPOINT_EVERY`` messages
        so a crash mid-thread doesn't force a replay of the whole thread.
        """
        last: int = 0
        indexed: int = 0
        local_after = after_id
        while True:
            kwargs: dict[str, object] = {
                "limit": HISTORY_FETCH_BATCH,
                "oldest_first": True,
            }
            if local_after is not None:
                kwargs["after"] = local_after
            batch_count = 0
            async for msg in thread.history(**kwargs):
                added = await self._run_parser(
                    msg, source_channel_id=source_channel_id
                )
                indexed += added
                last = max(last, msg.id)
                local_after = msg.id
                batch_count += 1
                if batch_count % CHECKPOINT_EVERY == 0:
                    try:
                        await self.db.update_source_progress(
                            source_channel_id, last
                        )
                    except Exception:
                        logger.exception(
                            "Checkpoint write failed for forum %s",
                            source_channel_id,
                        )
            # A short page = end of thread history (or near it).
            if batch_count < HISTORY_FETCH_BATCH:
                break
            await asyncio.sleep(HISTORY_FETCH_SLEEP)
        return indexed, last

    # ---------- channel backfill ------------------------------------------

    async def index_channel(
        self,
        channel_id: int,
        guild_id: int,
        *,
        reseed: bool = False,
    ) -> dict[str, object]:
        """Backfill a single source channel. Returns a status dict.

        Channel types accepted:
          * ``TextChannel``  - the original tight ``history()`` loop.
          * ``Thread``       - same tight loop on the thread's messages.
          * ``ForumChannel`` - walks every active thread inside the forum
                               and parses each thread's history. The DB's
                               ``source_channel_id`` is the FORUM id, so
                               ``/admin stats`` groups by forum.
        """
        existing = None if reseed else await self.db.get_source(channel_id)
        after_id: Optional[int] = (
            None
            if reseed
            else (existing.get("last_seen_message_id") if existing else None)
        )

        await self.db.upsert_source(
            channel_id,
            guild_id,
            last_seen_message_id=after_id,
            status="active",
        )

        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(
                channel_id
            )
        except discord.NotFound:
            await self.db.set_source_status(channel_id, "not_found")
            return {"channel_id": channel_id, "error": "Channel not found"}
        except discord.Forbidden:
            await self.db.set_source_status(channel_id, "forbidden")
            return {"channel_id": channel_id, "error": "Forbidden"}

        # Type gate: TextChannel or Thread or ForumChannel. Anything else
        # (VoiceChannel, StageChannel, Category, DM, ..) gets the historical
        # "wrong_type" status so /admin stats surfaces it cleanly.
        if not isinstance(
            channel,
            (discord.TextChannel, discord.Thread, discord.ForumChannel),
        ):
            await self.db.set_source_status(channel_id, "wrong_type")
            return {"channel_id": channel_id, "error": "Not a text-like channel"}

        indexed = 0
        last_seen = after_id or 0
        try:
            if isinstance(channel, discord.ForumChannel):
                # Forum: enumerate accessible threads, then scan each thread's
                # history. We swallow per-thread errors individually so one
                # bad thread doesn't abort the whole forum's indexer pass.
                threads = await self._collect_forum_threads(channel)
                logger.info(
                    "Forum %s: %d accessible threads to scan",
                    channel_id,
                    len(threads),
                )
                for thread in threads:
                    try:
                        added, thread_last = await self._iter_thread_messages(
                            thread,
                            source_channel_id=channel_id,
                            after_id=after_id,
                        )
                    except discord.Forbidden:
                        await self.db.set_source_status(channel_id, "forbidden")
                        return {
                            "channel_id": channel_id,
                            "error": "Forbidden reading forum thread",
                            "indexed": indexed,
                        }
                    except discord.HTTPException as exc:
                        await self.db.set_source_status(channel_id, "error")
                        return {
                            "channel_id": channel_id,
                            "error": str(exc),
                            "indexed": indexed,
                        }
                    indexed += added
                    last_seen = max(last_seen, thread_last)
                # Persist progress once all threads have been scanned. This
                # acts as a "forum fully scanned at <message_id>" marker; on
                # resume we filter each thread's history with `after=last_seen`
                # so we never reindex already-processed messages.
                await self.db.update_source_progress(
                    channel_id, last_seen or after_id or 0
                )
                return {
                    "channel_id": channel_id,
                    "indexed": indexed,
                    "last_message_id": last_seen,
                    "error": None,
                }
            # TextChannel / Thread path: tight history loop, batched.
            while True:
                kwargs: dict[str, object] = {
                    "limit": HISTORY_FETCH_BATCH,
                    "oldest_first": True,
                }
                if after_id is not None:
                    kwargs["after"] = after_id
                batch_count = 0
                async for msg in channel.history(**kwargs):
                    added = await self._run_parser(
                        msg, source_channel_id=channel_id
                    )
                    indexed += added
                    batch_count += 1
                    last_seen = max(last_seen, msg.id)
                    after_id = msg.id
                    # Checkpoint every CHECKPOINT_EVERY messages so a crash
                    # mid-backfill doesn't cost us the whole run.
                    if batch_count % CHECKPOINT_EVERY == 0:
                        try:
                            await self.db.update_source_progress(
                                channel_id, last_seen
                            )
                        except Exception:
                            logger.exception(
                                "Checkpoint write failed for %s", channel_id
                            )
                # A short page = end of channel history (or near it).
                if batch_count < HISTORY_FETCH_BATCH:
                    break
                await asyncio.sleep(HISTORY_FETCH_SLEEP)
        except discord.Forbidden:
            await self.db.set_source_status(channel_id, "forbidden")
            return {
                "channel_id": channel_id,
                "error": "Forbidden during backfill",
                "indexed": indexed,
            }
        except discord.HTTPException as exc:
            await self.db.set_source_status(channel_id, "error")
            return {
                "channel_id": channel_id,
                "error": str(exc),
                "indexed": indexed,
            }

        await self.db.update_source_progress(
            channel_id, last_seen or after_id or 0
        )
        return {
            "channel_id": channel_id,
            "indexed": indexed,
            "last_message_id": last_seen,
            "error": None,
        }

    async def _backfill_all(self) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for channel_id in list(config.SOURCE_CHANNELS):
            try:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(channel_id)
                guild_id = channel.guild.id
            except (discord.NotFound, discord.Forbidden):
                logger.warning(
                    "Cannot fetch source channel %s during backfill", channel_id
                )
                await self.db.upsert_source(channel_id, 0, status="error")
                results.append({"channel_id": channel_id, "error": "fetch failed"})
                continue

            # If a SOURCE_GUILD_IDS allow-list is in effect, a stale channel
            # id loaded from earlier (pre-rename) settings might point at a
            # guild that's no longer permitted. Surface it instead of silently
            # skipping so misconfiguration is visible in /admin stats.
            if not config.guild_allowed(guild_id):
                logger.warning(
                    "Source channel %s is in guild %s which is not in "
                    "SOURCE_GUILD_IDS allow-list; skipping backfill",
                    channel_id,
                    guild_id,
                )
                await self.db.set_source_status(channel_id, "not_allowed")
                results.append(
                    {"channel_id": channel_id, "error": "guild not allowed"}
                )
                continue

            logger.info(
                "Indexing source channel %s in guild %s", channel_id, guild_id
            )
            try:
                result = await self.index_channel(channel_id, guild_id)
            except Exception:
                logger.exception(
                    "Backfill crashed for channel %s", channel_id
                )
                results.append({"channel_id": channel_id, "error": "crashed"})
                continue
            results.append(result)
            logger.info(
                "Backfill %s: indexed=%s error=%s",
                channel_id,
                result.get("indexed"),
                result.get("error"),
            )
        return results

    # ---------- Discord event listeners -----------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not config.SOURCE_CHANNELS:
            logger.info("No source channels configured. Skipping backfill.")
            return
        # Guard against re-entrancy if on_ready somehow fires twice.
        if self._indexing_lock.locked():
            return
        async with self._indexing_lock:
            await self._backfill_all()

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        """Discord gateway reconnected. Catch up since last_seen."""
        logger.info("Gateway resumed — re-checking source channels")
        if self._indexing_lock.locked():
            return
        async with self._indexing_lock:
            await self._backfill_all()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.bot.user:
            return
        if message.author is not None and message.author.bot:
            return
        # The source-channel identity of a forum-post message is the FORUM,
        # not the thread, so we route through ``_resolve_source_cid`` to
        # produce the correct lookup key against SOURCE_CHANNELS. Without
        # this, every forum-post Mega link would be silently dropped because
        # ``message.channel.id`` is the THREAD id, not the forum we registered.
        source_cid = _resolve_source_cid(message)
        if source_cid is None or source_cid not in config.SOURCE_CHANNELS:
            return
        # Route through config.guild_allowed so live ingest and backfill
        # share one allow-list definition; if the helper ever grows
        # nuance (DM carve-outs, blocked guilds, etc.), both paths pick
        # it up together.
        if not config.guild_allowed(
            message.guild.id if message.guild is not None else None
        ):
            return
        try:
            added = await self._run_parser(
                message, source_channel_id=source_cid
            )
            if added:
                logger.info(
                    "Indexed %d entries from live message %s (source=%s)",
                    added,
                    message.id,
                    source_cid,
                )
                await self.db.update_source_progress(source_cid, message.id)
        except Exception:
            logger.exception("Indexer on_message failed for message %s", message.id)
