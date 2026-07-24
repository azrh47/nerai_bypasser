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
import httpx

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

    async def _fetch_steam_details(self, app_id: int) -> dict:
        url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=us&l=english"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }
        try:
            async with httpx.AsyncClient(timeout=10, headers=headers) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
                if data and str(app_id) in data and data[str(app_id)].get("success"):
                    return data[str(app_id)]["data"]
        except Exception as e:
            logger.warning(f"Failed to fetch Steam details for app_id {app_id}: {e}")
        return {}

    def _format_size(self, size_bytes: int) -> str:
        if not size_bytes:
            return "Unknown"
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}".rstrip(".0") if unit != "B" else f"{size_bytes} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} PB"

    # ---------- reply ------------------------------------------------------

    async def _reply_with_entries(
        self,
        interaction: discord.Interaction,
        entries: list[dict],
        query: str,
    ) -> None:
        first = entries[0]
        
        # Determine app_id
        app_id = first.get("app_id")
        game_name = first.get("canonical_name") or first.get("game_name") or "Unknown Game"
        
        if not app_id:
            try:
                steam_populated = await self.steam.size() > 0
                if steam_populated:
                    fuzzy = await self.steam.fuzzy_lookup_id(game_name, limit=1, score_cutoff=75)
                    if fuzzy:
                        app_id = fuzzy[0][0]
            except Exception:
                pass

        # Fetch steam details if we have an app_id
        steam_data = {}
        if app_id:
            steam_data = await self._fetch_steam_details(app_id)
            
        # Extract fields
        title = steam_data.get("name") or game_name
        description = steam_data.get("short_description", "No description available.")
        
        is_free = steam_data.get("is_free", False)
        price_overview = steam_data.get("price_overview", {})
        price = "Free" if is_free else price_overview.get("final_formatted", "Price N/A")
        
        developers = ", ".join(steam_data.get("developers", []))
        publishers = ", ".join(steam_data.get("publishers", []))
        
        release_date = steam_data.get("release_date", {}).get("date", "")
        genres = ", ".join([g["description"] for g in steam_data.get("genres", [])])
        
        header_image = steam_data.get("header_image")

        embed = discord.Embed(
            title=f"🌐 {escape_markdown(title)}",
            description=f"{description}\n\n**{price}**\n\n",
            color=discord.Color.green(),
        )
        
        if app_id:
            embed.url = f"https://store.steampowered.com/app/{app_id}"

        # Setup author (using bot's own avatar as placeholder for Nerai Gen)
        bot_user = interaction.client.user
        embed.set_author(name=bot_user.display_name if bot_user else "Nerai Gen", icon_url=bot_user.display_avatar.url if bot_user else None)

        # Construct download link strings
        dl_text = ""
        for e in entries[:5]:
            filename = e.get("filename") or e.get("game_name") or "Download"
            link = e["mega_url"]
            size_str = self._format_size(e.get("size_bytes") or 0)
            
            # Format: 📥 [Filename](URL) \n 📄 Size
            dl_text += f"📥 [{escape_markdown(title)} - {escape_markdown(filename)}]({link})\n📄 {size_str}\n\n"
        
        embed.description += dl_text
        
        embed.add_field(name="👨‍💻 Developer", value=escape_markdown(developers) if developers else "Unknown", inline=True)
        embed.add_field(name="🏢 Publisher", value=escape_markdown(publishers) if publishers else "Unknown", inline=True)
        embed.add_field(name="📅 Released", value=escape_markdown(release_date) if release_date else "Unknown", inline=True)
        embed.add_field(name="🎮 Genre", value=escape_markdown(genres) if genres else "Unknown", inline=True)
            
        embed.add_field(name="🛡️ Denuvo", value="No / Unknown", inline=True)
        
        if header_image:
            embed.set_image(url=header_image)

        embed.set_footer(
            text=(
                "Links are community-sourced from the source server and may be "
                "expired. Use at your own discretion."
            )
        )

        view = discord.ui.View(timeout=300)
        for e in entries[:5]:
            view.add_item(discord.ui.Button(label="Open Link", url=e["mega_url"]))

        # Send public message (ephemeral=False was handled in the defer)
        await interaction.followup.send(embed=embed, view=view)

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

        # Success messages will be public
        await interaction.response.defer(thinking=True, ephemeral=False)

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
