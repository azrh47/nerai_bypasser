"""Centralized configuration loaded from environment variables.

A single `import config` gives the rest of the bot a typed view of all
runtime settings. Missing-required values raise on import.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name)
    if val is None or not val.strip():
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val.strip()


def _optional_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"Env var {name} must be an integer, got {raw!r}") from exc


def _optional_int_list(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError as exc:
            raise RuntimeError(
                f"Env var {name} contains non-integer value: {chunk!r}"
            ) from exc
    return out


# Required ---------------------------------------------------------------

# Bot token from the Discord Developer Portal.
DISCORD_TOKEN: str = _require("DISCORD_TOKEN")

# Optional with sensible defaults -----------------------------------------

# User-facing server(s) where /get, /search are issued.
TARGET_GUILD_IDS: list[int] = _optional_int_list("TARGET_GUILD_IDS")
if not TARGET_GUILD_IDS:
    legacy_target = _optional_int("TARGET_GUILD_ID", 0)
    if legacy_target:
        TARGET_GUILD_IDS = [legacy_target]

# Deprecated single-int alias for backwards compat
TARGET_GUILD_ID: int = TARGET_GUILD_IDS[0] if TARGET_GUILD_IDS else 0

# Source server(s) where Mega.nz links are posted (the bot must be a member
# of every listed guild). Accepts either:
#   * SOURCE_GUILD_IDS=111,222,333   — canonical comma-separated form
#   * SOURCE_GUILD_ID=111            — singular form, kept for backwards
#                                       compat; treated as a 1-element list
# If both are set, SOURCE_GUILD_IDS wins.
# An empty list (the default) means "no allow-list" — any guild whose
# channels are configured in SOURCE_CHANNELS is accepted. SOURCE_CHANNELS
# remains the authoritative source-of-truth for which channels are indexed.
SOURCE_GUILD_IDS: list[int] = _optional_int_list("SOURCE_GUILD_IDS")
if not SOURCE_GUILD_IDS:
    # Plural form is unset/empty: fall back to the legacy singular env var
    # for backwards compatibility. Wrapped in `else:` for clarity — the
    # legacy lookup only matters when the canonical env was not populated.
    legacy = _optional_int("SOURCE_GUILD_ID", 0)
    if legacy:
        SOURCE_GUILD_IDS = [legacy]

# Deprecated single-int alias. Kept so external code/tests that reference
# `config.SOURCE_GUILD_ID` keep working; reflects the first element of
# SOURCE_GUILD_IDS (or 0 if the list is empty). For multi-guild configs
# this picks an arbitrary first entry -- prefer SOURCE_GUILD_IDS.
SOURCE_GUILD_ID: int = SOURCE_GUILD_IDS[0] if SOURCE_GUILD_IDS else 0

# Source channel IDs configured at startup. May also be added at runtime via
# /admin link_channel (persisted to the settings table).
SOURCE_CHANNELS: list[int] = _optional_int_list("SOURCE_CHANNELS")


def guild_allowed(guild_id: Optional[int]) -> bool:
    """Return True if ``guild_id`` passes the SOURCE_GUILD_IDS allow-list.

    Empty list = no filter (permissive default). Useful for both on_message
    (live ingest) and the backfill loop, which needs to gate at runtime in
    case a stale channel resolves to a guild that's not in the allow-list.
    """
    if not SOURCE_GUILD_IDS:
        return True
    if guild_id is None:
        return False
    return guild_id in SOURCE_GUILD_IDS


# Optional role gate in TARGET_GUILD_ID for the user-facing commands.
ALLOWED_ROLE_NAME: Optional[str] = os.getenv("ALLOWED_ROLE_NAME") or None

STEAM_API_KEY: str = os.getenv("STEAM_API_KEY", "").strip()

# Admin user IDs (may use /admin reseed, /admin stats, etc.).
ADMIN_USER_IDS: list[int] = _optional_int_list("ADMIN_USER_IDS")

# Paths.
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/bot.sqlite")
SCHEMA_PATH: str = os.getenv("SCHEMA_PATH", "schema.sql")

# Behavior.
COOLDOWN_SECONDS: int = _optional_int("COOLDOWN_SECONDS", 5)
SEARCH_PAGE_SIZE: int = _optional_int("SEARCH_PAGE_SIZE", 10)
FUZZY_CANDIDATE_LIMIT: int = _optional_int("FUZZY_CANDIDATE_LIMIT", 80)

# Toggle Mega staleness HEAD checks (default OFF - Mega blocks bots).
MEGA_STALENESS_CHECK: bool = os.getenv("MEGA_STALENESS_CHECK", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def env_summary() -> dict[str, Any]:
    """Return a redacted summary for /admin stats and startup logs."""
    return {
        "target_guild_ids": list(TARGET_GUILD_IDS),
        "target_guild_id": TARGET_GUILD_ID or None,
        "source_guild_ids": list(SOURCE_GUILD_IDS),
        "source_guilds_configured": len(SOURCE_GUILD_IDS),
        "source_channels_configured": len(SOURCE_CHANNELS),
        "admin_users": len(ADMIN_USER_IDS),
        "allowed_role": ALLOWED_ROLE_NAME,
        "database_path": DATABASE_PATH,
        "staleness_check_enabled": MEGA_STALENESS_CHECK,
    }


__all__ = [
    "DISCORD_TOKEN",
    "TARGET_GUILD_IDS",
    "TARGET_GUILD_ID",
    "SOURCE_GUILD_IDS",
    "SOURCE_GUILD_ID",
    "SOURCE_CHANNELS",
    "guild_allowed",
    "ALLOWED_ROLE_NAME",
    "ADMIN_USER_IDS",
    "DATABASE_PATH",
    "SCHEMA_PATH",
    "COOLDOWN_SECONDS",
    "SEARCH_PAGE_SIZE",
    "FUZZY_CANDIDATE_LIMIT",
    "MEGA_STALENESS_CHECK",
    "env_summary",
]
