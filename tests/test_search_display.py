import unittest
from cogs.search import Search

class TestSearchDisplay(unittest.TestCase):
    def test_clean_display_filename(self):
        # Create a dummy instance without calling __init__ (since __init__ needs bot/db/steam)
        cog = Search.__new__(Search)

        # 1. Strip .rar and clean underscores
        raw = "RESIDENT_EVIL_9_REQUIEM_GAME_FIX_BY_XERO_NATION.rar"
        cleaned = cog._clean_display_filename(raw, "Resident Evil")
        self.assertEqual(cleaned, "Resident Evil 9 Requiem Game Fix By Xero Nation")

        # 2. Short names or generic terms get fallback title prepended
        raw_generic = "Patch_v1.2.rar"
        cleaned_generic = cog._clean_display_filename(raw_generic, "Atomic Heart")
        self.assertEqual(cleaned_generic, "Atomic Heart - Patch V1.2")

        # 3. Already clean title case preservation
        raw_clean = "Cyberpunk 2077 Phantom Liberty DLC"
        cleaned_clean = cog._clean_display_filename(raw_clean, "Cyberpunk 2077")
        self.assertEqual(cleaned_clean, "Cyberpunk 2077 Phantom Liberty DLC")

if __name__ == "__main__":
    unittest.main()
