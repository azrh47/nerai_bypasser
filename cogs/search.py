"""Public slash commands: ``/get`` and ``/search``.

* ``/get <query>`` returns a single best match's Mega link as an ephemeral embed.
* ``/search <query>`` returns up to ``SEARCH_PAGE_SIZE`` distinct matches.
* Both have a per-user cooldown to discourage scraping.
* Both honor ``ALLOWED_ROLE_NAME`` for role gating (set in .env).
* Autocomplete combines Steam fuzzy-name lookup + direct DB name search, and
  short-circuits to DB-only suggestions if the Steam cache hasn't filled yet.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

import config
from database import Database
from steam_cache import SteamCache

logger = logging.getLogger(__name__)

# user_id -> last invocation timestamp (monotonic seconds).
_cooldowns: dict[int, float] = defaultdict(float)


def _is_allowed(interaction: discord.Interaction) -> bool:
    if not config.ALLOWED_ROLE_NAME:
        return True
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    return any(r.name == config.ALLOWED_ROLE_NAME for r in member.roles)


def _check_cooldown(user_id: int) -> Optional[int]:
    cooldown = config.COOLDOWN_SECONDS
    if cooldown <= 0:
        return None
    now = time.monotonic()
    last = _cooldowns[user_id]
    remaining = cooldown - (now - last)
    if remaining > 0:
        return int(remaining) + 1
    _cooldowns[user_id] = now
    return None


# ---------------------------------------------------------------------------
# Module-level autocomplete delegate
#
# discord.py 2.x invokes autocomplete callbacks as ``callback(interaction,
# current)`` (no ``self``). Because ``Search._do_autocomplete`` is an instance
# method and needs our DB + Steam cache, we define a free function here that
# looks up the cog via the interaction's client and delegates to it.
# ---------------------------------------------------------------------------


async def _search_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    client = interaction.client
    cog: Optional[Search] = (
        client.get_cog("Search") if client is not None else None
    )
    if cog is None:
        return []
    return await cog._do_autocomplete(interaction, current)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Search(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        steam: SteamCache,
    ) -> None:
        self.bot = bot
        self.db = db
        self.steam = steam

    # ---------- helpers ----------------------------------------------------

    @staticmethod
    def _display_name(entry: dict) -> str:
        name = (
            entry.get("canonical_name")
            or entry.get("game_name")
            or "Untitled"
        )
        if entry.get("app_id"):
            name = f"{name} ({entry['app_id']})"
        if entry.get("filename"):
            name = f"{name} — {entry['filename']}"
        return name

    async def _do_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Combined fuzzy-from-Steam + substring-from-DB autocomplete provider."""
        current = current.strip()
        if not current:
            try:
                recent = await self.db.search_entries("", limit=25)
            except Exception:
                recent = []
            return [
                app_commands.Choice(
                    name=self._display_name(e)[:100],
                    value=f"db:{e['id']}",
                )
                for e in recent[:25]
            ]

        if current.isdigit():
            return [
                app_commands.Choice(
                    name=f"App ID: {current}", value=current
                )
            ]

        # Short-circuit: if the Steam app list is not cached yet, fall back
        # to DB-only suggestions so we don't exceed Discord's 3 s autocomplete
        # window with a first-call Steam fetch + 150k-row rapidfuzz scan.
        try:
            steam_populated = await self.steam.size() > 0
        except Exception:
            steam_populated = False

        # Always pull DB substring first; it's fast and good-on-its-own.
        try:
            db_matches = await self.db.search_entries(current, limit=8)
        except Exception:
            db_matches = []

        choices: list[app_commands.Choice[str]] = []
        seen_labels: set[str] = set()

        # Steam fuzzy name match — only if cache is populated.
        if steam_populated:
            try:
                fuzzy = await self.steam.fuzzy_lookup_id(
                    current, limit=8, score_cutoff=60
                )
            except Exception:
                fuzzy = []
            for app_id, name, _score in fuzzy:
                label = f"[Steam] {escape_markdown(name)[:80]}"
                if label in seen_labels:
                    continue
                seen_labels.add(label)
                choices.append(
                    app_commands.Choice(name=label, value=str(app_id))
                )

        for e in db_matches:
            label = f"[DB] {escape_markdown(self._display_name(e))[:80]}"
            if not label.strip() or label in seen_labels:
                continue
            seen_labels.add(label)
            value = (
                f"app:{e['app_id']}"
                if e.get("app_id") is not None
                else f"db:{e['id']}"
            )
            choices.append(app_commands.Choice(name=label, value=value))

        return choices[:25]

    # ---------- resolvers --------------------------------------------------

    async def _resolve(self, query: str) -> list[dict]:
        """Resolve a single query to one or more entries."""
        if query.startswith("app:"):
            try:
                app_id = int(query[4:])
            except ValueError:
                return []
            return await self.db.get_entries_by_app_id(app_id)

        if query.startswith("db:"):
            try:
                db_id = int(query[3:])
            except ValueError:
                return []
            entry = await self.db.get_entry_by_id(db_id)
            return [entry] if entry else []

        if query.isdigit():
            app_id = int(query)
            entries = await self.db.get_entries_by_app_id(app_id)
            if entries:
                return entries
            entry = await self.db.get_entry_by_id(app_id)
            return [entry] if entry else []

        # Text query: Steam fuzzy name first, then DB substring.
        try:
            steam_populated = await self.steam.size() > 0
        except Exception:
            steam_populated = False
        if steam_populated:
            fuzzy = await self.steam.fuzzy_lookup_id(
                query, limit=1, score_cutoff=70
            )
            if fuzzy:
                app_id, _name, _score = fuzzy[0]
                return await self.db.get_entries_by_app_id(app_id)
        return await self.db.search_entries(query, limit=10)

    async def _resolve_multi(self, query: str) -> list[dict]:
        """Resolve a query with multiple results for /search."""
        if query.isdigit():
            return await self.db.get_entries_by_app_id(int(query), limit=20)

        results: dict[tuple[Optional[str], Optional[int]], dict] = {}
        try:
            steam_populated = await self.steam.size() > 0
        except Exception:
            steam_populated = False
        if steam_populated:
            fuzzy = await self.steam.fuzzy_lookup_id(
                query, limit=10, score_cutoff=70
            )
            for app_id, _name, _score in fuzzy:
                for e in await self.db.get_entries_by_app_id(app_id, limit=5):
                    key = (e.get("canonical_name"), e.get("app_id"))
                    if key not in results:
                        results[key] = e

        for e in await self.db.search_entries(query, limit=20):
            key = (e.get("canonical_name"), e.get("app_id"))
            if key not in results:
                results[key] = e

        return sorted(
            results.values(),
            key=lambda e: e.get("posted_at") or "",
            reverse=True,
        )

    # ---------- reply ------------------------------------------------------

    async def _reply_with_entries(
        self,
        interaction: discord.Interaction,
        entries: list[dict],
        query: str,
    ) -> None:
        first = entries[0]
        title = (
            first.get("canonical_name")
            or first.get("game_name")
            or "Match"
        )
        # Escape the title too so markdown-in-game_name can't shape the embed.
        safe_title = escape_markdown(str(title))[:256]

        # Provide ample but bounded room for the description (Discord's total
        # embed char budget is 6000; with title+footer+two 1024-char fields
        # the description should stay under ~3800).
        description_lines: list[str] = []
        for e in entries[:10]:
            name = self._display_name(e)
            link = e["mega_url"]
            stale = " ⚠️ *may be expired*" if e.get("is_stale") else ""
            # escape_markdown protects against phishing via crafted filenames.
            description_lines.append(
                f"[{escape_markdown(name)[:100]}]({link}){stale}"
            )
        embed = discord.Embed(
            title=safe_title,
            description="\n".join(description_lines)[:3800]
            or "_(no entries)_",
            color=discord.Color.blurple(),
        )
        if first.get("app_id"):
            embed.add_field(
                name="Steam",
                value=(
                    f"https://store.steampowered.com/app/{first['app_id']}"
                ),
            )
        if first.get("filename") and len(embed) + 1024 < 6000:
            embed.add_field(
                name="File",
                value=str(escape_markdown(first["filename"]))[:1024],
                inline=False,
            )
        embed.set_footer(
            text=(
                "Links are community-sourced from the source server and may be "
                "expired. Use at your own discretion."
            )
        )

        view = discord.ui.View(timeout=300)
        for e in entries[:5]:
            view.add_item(discord.ui.Button(label="Open", url=e["mega_url"]))

        await interaction.followup.send(
            embed=embed, view=view, ephemeral=True
        )

    # ---------- commands ---------------------------------------------------

    async def _handle_lookup(
        self,
        interaction: discord.Interaction,
        query: str,
        *,
        multi: bool,
    ) -> None:
        if not _is_allowed(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True,
            )
            return

        remaining = _check_cooldown(interaction.user.id)
        if remaining is not None:
            await interaction.response.send_message(
                f"Slow down — try again in **{remaining}s**.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        if multi:
            entries = await self._resolve_multi(query)
        else:
            entries = await self._resolve(query)

        if not entries:
            hint = (
                "It may exist on Steam but nobody posted it yet, or the "
                "spelling is different. Try `/search <keyword>`."
            )
            await interaction.followup.send(
                f"❌ No indexed game matches `{query}`. {hint}",
                ephemeral=True,
            )
            return

        await self._reply_with_entries(
            interaction,
            entries[: config.SEARCH_PAGE_SIZE],
            query,
        )

    @app_commands.command(
        name="get",
        description=(
            "Look up Mega.nz link for a game (by name or Steam app ID)."
        ),
    )
    @app_commands.guild_only()
    @app_commands.autocomplete(query=_search_autocomplete)
    async def get_cmd(
        self,
        interaction: discord.Interaction,
        query: str,
    ) -> None:
        await self._handle_lookup(interaction, query, multi=False)

    @app_commands.command(
        name="search",
        description="Search the index for partial game names.",
    )
    @app_commands.guild_only()
    async def search_cmd(
        self,
        interaction: discord.Interaction,
        query: str,
    ) -> None:
        await self._handle_lookup(interaction, query, multi=True)
