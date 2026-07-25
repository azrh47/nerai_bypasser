import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from steam_cache import SteamCache, clean_game_name


class TestSteamCache(unittest.IsolatedAsyncioTestCase):
    def test_clean_game_name(self):
        self.assertEqual(clean_game_name("RAGE - ASSASSINS CREED MIRAGE BYPASS!"), "ASSASSINS CREED MIRAGE")
        self.assertEqual(clean_game_name("Cyberpunk 2077 v2.1.2 (+12 DLCs, MULTi14) [FitGirl Repack]"), "Cyberpunk 2077")
        self.assertEqual(clean_game_name("[RUNE] Elden Ring Deluxe Edition v1.10"), "Elden Ring Deluxe Edition")
        self.assertEqual(clean_game_name("RAGE"), "RAGE")

    async def test_fuzzy_lookup_avoids_short_substrings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "steam.db")
            cache = SteamCache(db_path, refresh_after=timedelta(hours=24))
            
            # Manually insert test tables and apps into sqlite
            import aiosqlite
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("CREATE TABLE apps (app_id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
                await conn.execute("CREATE INDEX idx_apps_name ON apps(name)")
                await conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
                
                # Mark cache as recently fetched so it won't hit network
                now_str = datetime.now(timezone.utc).isoformat()
                await conn.execute("INSERT INTO settings VALUES (?, ?)", ("steam_cache_fetched_at", now_str))
                
                await conn.execute("INSERT INTO apps VALUES (?, ?)", (100, "RAGE"))
                await conn.execute("INSERT INTO apps VALUES (?, ?)", (200, "RAGE 2"))
                await conn.execute("INSERT INTO apps VALUES (?, ?)", (300, "Assassin's Creed Mirage"))
                await conn.commit()

            results = await cache.fuzzy_lookup_id("RAGE - ASSASSINS CREED MIRAGE BYPASS!", limit=1, score_cutoff=70)
            self.assertTrue(results)
            self.assertEqual(results[0][0], 300)  # Should match Assassin's Creed Mirage, NOT RAGE
            self.assertEqual(results[0][1], "Assassin's Creed Mirage")


if __name__ == "__main__":
    unittest.main()
