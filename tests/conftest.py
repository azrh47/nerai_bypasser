"""Pytest configuration.

cogs.indexer imports ``config`` at module load time, and ``config`` requires
``DISCORD_TOKEN`` to be present. We can't load any test that imports a cog
without a token. Set dummy env vars here so collection succeeds; the tests
themselves never contact Discord or R2.
"""
from __future__ import annotations

import os

# Required secrets — dummy values to satisfy config._require().
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-real")
os.environ.setdefault("TARGET_GUILD_ID", "0")
# Seed BOTH the plural list form and the singular back-compat alias so the
# config.py shim that maps SOURCE_GUILD_ID -> SOURCE_GUILD_IDS stays
# exercised on every test run.
os.environ.setdefault("SOURCE_GUILD_IDS", "")
os.environ.setdefault("SOURCE_GUILD_ID", "0")
os.environ.setdefault("DATABASE_PATH", ":memory:")
os.environ.setdefault("SCHEMA_PATH", "schema.sql")

# Optional knobs — silence config validation by setting sensible defaults.
os.environ.setdefault("COOLDOWN_SECONDS", "5")
os.environ.setdefault("SEARCH_PAGE_SIZE", "10")
os.environ.setdefault("FUZZY_CANDIDATE_LIMIT", "80")
os.environ.setdefault("MEGA_STALENESS_CHECK", "false")
