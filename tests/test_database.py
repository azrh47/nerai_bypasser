"""Async tests for ``database.Database``.

Uses pytest-asyncio in ``auto`` mode (see ``pytest.ini``). The ``db`` fixture
creates a fresh SQLite file in a temp dir against the project's ``schema.sql``.
"""
from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from database import Database


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    db_path = str(tmp_path / "test.sqlite")
    database = Database(db_path, "schema.sql")
    await database.initialize()
    yield database


async def test_initialize_creates_sources(db: Database):
    await db.upsert_source(12345, 99999)
    sources = await db.list_sources()
    assert any(s["channel_id"] == 12345 for s in sources)


async def test_upsert_entry_is_idempotent(db: Database):
    entry = {
        "source_channel_id": 100,
        "source_message_id": 200,
        "mega_url": "https://mega.nz/file/AAAaaaa#BBBbbbb",
        "game_name": "Foo",
        "app_id": 1234,
    }
    await db.upsert_entry(entry)
    await db.upsert_entry(entry)
    matches = await db.search_entries("Foo")
    assert len(matches) == 1


async def test_search_by_app_id(db: Database):
    await db.upsert_entry(
        {
            "source_channel_id": 100,
            "source_message_id": 201,
            "mega_url": "https://mega.nz/file/CCCaaaa#DDDbbbb",
            "app_id": 9999,
            "game_name": "Bar",
        }
    )
    matches = await db.get_entries_by_app_id(9999)
    assert len(matches) == 1
    assert matches[0]["game_name"] == "Bar"


async def test_upsert_updates_metadata_not_duplicates(db: Database):
    await db.upsert_entry(
        {
            "source_channel_id": 100,
            "source_message_id": 200,
            "mega_url": "https://mega.nz/file/AAAbbbb#CCCdddd",
            "game_name": "OldName",
        }
    )
    await db.upsert_entry(
        {
            "source_channel_id": 100,
            "source_message_id": 200,
            "mega_url": "https://mega.nz/file/AAAbbbb#CCCdddd",
            "game_name": "NewName",
            "app_id": 4321,
        }
    )
    matches = await db.search_entries("NewName")
    assert len(matches) == 1
    assert matches[0]["app_id"] == 4321


async def test_delete_entry(db: Database):
    await db.upsert_entry(
        {
            "source_channel_id": 100,
            "source_message_id": 200,
            "mega_url": "https://mega.nz/file/EEEffff#GGGhhhh",
        }
    )
    entries = await db.search_entries("")
    assert entries, "expected at least one entry"
    eid = entries[0]["id"]
    assert await db.delete_entry(eid) is True
    assert await db.delete_entry(eid) is False


async def test_settings_roundtrip(db: Database):
    await db.set_setting("hello", "world")
    assert await db.get_setting("hello") == "world"
    await db.delete_setting("hello")
    assert await db.get_setting("hello") is None
