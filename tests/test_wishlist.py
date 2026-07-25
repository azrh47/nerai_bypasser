import asyncio
import pytest
import pytest_asyncio
from pathlib import Path
import tempfile
import sqlite3

from database import Database

@pytest_asyncio.fixture
async def db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        schema_path = Path("schema.sql").resolve()
        database = Database(str(db_path), str(schema_path))
        await database.initialize()
        yield database

@pytest.mark.asyncio
async def test_wishlist_crud(db: Database):
    user_id = 12345
    await db.add_wishlist(user_id, "Cyberpunk")
    await db.add_wishlist(user_id, "Witcher 3")
    
    items = await db.get_user_wishlists(user_id)
    assert "cyberpunk" in items
    assert "witcher 3" in items
    
    matches = await db.get_matching_wishlists("Cyberpunk 2077 v2.1")
    assert len(matches) == 1
    assert matches[0] == (user_id, "cyberpunk")
    
    removed = await db.remove_wishlist(user_id, "cyberpunk")
    assert removed is True
    
    items_after = await db.get_user_wishlists(user_id)
    assert "cyberpunk" not in items_after
    assert "witcher 3" in items_after
