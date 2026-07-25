"""Unit tests for ``parser.parse_message``.

These cover the common shapes the indexer sees in the source channel:
* bare URL
* labeled title + size + app_id
* realistic repack header (no "Title:" label) - new heuristic
* multi-URL dump
* Steam store URL with appid
* dedup of repeated URLs within a message
* multi-URL filename independence
"""
from __future__ import annotations

from parser import parse_message


SAMPLE_BARE = """
Here's the link for today's drop:
https://mega.nz/file/AbCdEfGh#IjKlMnOpQrStUvWxYz123456
"""

SAMPLE_TITLED = """
Title: Cyberpunk 2077: Phantom Liberty
Genre: RPG
Size: 65.4 GB
App ID: 1091500
https://mega.nz/file/AbCdEfGh#IjKlMnOpQrStUvWxYz123456
"""

# Realistic repack layout — no "Title:" label. The parser's first-line heuristic
# should still pick up "Cyberpunk 2077 v2.1.2 (+12 DLCs, MULTi14) [FitGirl Repack]"
# as the game name.
SAMPLE_REPACK = """
Cyberpunk 2077 v2.1.2 (+12 DLCs, MULTi14) [FitGirl Repack]
Genre: RPG
Size: 65.4 GB
App ID: 1091500
https://mega.nz/file/AbCdEfGh#IjKlMnOpQrStUvWxYz123456
"""

SAMPLE_MULTI = """
Cyberpunk 2077 v2.1.zip
https://mega.nz/file/RealOneAaaa#RealOneHashOneBbbbCccc

The Witcher 3 Wild Hunt - Complete Edition.zip
https://mega.nz/file/RealTwoDddd#RealTwoHashTwoEeeeFfff

https://store.steampowered.com/app/12345/Random_Game
https://mega.nz/folder/FolderKey1234AbCd#FolderHash1234567EfGh
"""

SAMPLE_NOTHING = "Just chat, no Mega links here today."

SAMPLE_STEAM_URL = """
Game: Some Cool Game
https://store.steampowered.com/app/999999/Some_Cool_Game/
https://mega.nz/file/AbCdEfGh#IjKlMnOpQrStUvWxYz123456
"""


def test_bare_url_extracts_one_entry():
    entries = parse_message(SAMPLE_BARE, source_message_id=1)
    assert len(entries) == 1
    assert entries[0].mega_url.startswith("https://mega.nz/file/")
    assert entries[0].is_folder is False


def test_titled_post_extracts_name_and_appid():
    entries = parse_message(SAMPLE_TITLED, source_message_id=2)
    assert len(entries) == 1
    e = entries[0]
    assert e.app_id == 1091500
    assert "Cyberpunk" in (e.game_name or "")
    assert e.size_bytes is not None and e.size_bytes > 60 * 1024 ** 3


def test_first_line_heuristic_handles_repack_header():
    """A repack post with no explicit Title: label is still parsed."""
    entries = parse_message(SAMPLE_REPACK, source_message_id=3)
    assert len(entries) == 1
    e = entries[0]
    assert "Cyberpunk" in (e.game_name or "")
    assert e.app_id == 1091500
    assert e.size_bytes is not None


def test_multiple_urls_in_one_message():
    entries = parse_message(SAMPLE_MULTI, source_message_id=4)
    urls = [e.mega_url for e in entries]
    assert len(urls) == 3
    assert any(u.startswith("https://mega.nz/folder/") for u in urls)


def test_multi_url_independent_filenames():
    """URL #3 in a 3-URL post must NOT inherit URL #1's filename."""
    entries = parse_message(SAMPLE_MULTI, source_message_id=5)
    # Map URL -> filename.
    by_url = {e.mega_url: e.filename for e in entries}
    file_url = next(
        u for u in by_url if "RealOneAaaa" in u
    )
    witcher_url = next(
        u for u in by_url if "RealTwoDddd" in u
    )
    folder_url = next(
        u for u in by_url if "FolderKey" in u
    )
    assert by_url[file_url] is not None
    assert "Cyberpunk 2077 v2.1.zip" in (by_url[file_url] or "")
    assert by_url[witcher_url] is not None
    assert by_url[folder_url] is None  # no filename adjacent -> None


def test_no_mega_returns_empty():
    entries = parse_message(SAMPLE_NOTHING, source_message_id=6)
    assert entries == []


def test_steam_url_provides_app_id():
    entries = parse_message(SAMPLE_STEAM_URL, source_message_id=7)
    assert entries[0].app_id == 999999


def test_dedupes_same_url():
    text = (
        "https://mega.nz/file/AbCdEfGh#IjKlMnOpQrStUvWxYz123456 and again "
        "https://mega.nz/file/AbCdEfGh#IjKlMnOpQrStUvWxYz123456"
    )
    entries = parse_message(text, source_message_id=8)
    assert len(entries) == 1


def test_strips_archive_extension_for_fallback_title():
    text = """
Cool Game Repack.zip
https://mega.nz/file/AbCdEfGh#IjKlMnOpQrStUvWxYz123456
"""
    entries = parse_message(text, source_message_id=9)
    assert len(entries) == 1
    assert entries[0].game_name == "Cool Game Repack"


def test_mediafire_url_extracted():
    text = """
Mirror's Edge BYPASS!
https://www.mediafire.com/file/AbCdEfGh/Mirror's+Edge.zip
"""
    entries = parse_message(text, source_message_id=10)
    assert len(entries) == 1
    assert entries[0].mega_url.startswith(
        "https://www.mediafire.com/file/"
    )
    assert entries[0].is_folder is False


def test_mediafire_url_markdown_wrapper_truncated_at_paren():
    """[Caption](URL) markdown should capture only the URL, not the closing )."""
    text = "[Download](https://www.mediafire.com/file/AbCd/Foo.zip)"
    entries = parse_message(text, source_message_id=20)
    assert len(entries) == 1
    assert entries[0].mega_url == "https://www.mediafire.com/file/AbCd/Foo.zip"


def test_mediafire_folder_url_extracted():
    text = "https://www.mediafire.com/folder/AbCdEfGh"
    entries = parse_message(text, source_message_id=11)
    assert len(entries) == 1
    assert entries[0].is_folder is True


def test_mixed_mega_and_mediafire_urls_deduped():
    text = """
https://mega.nz/file/MegaOne#MegaOneHash
https://www.mediafire.com/file/MFOne/MFOne.zip
https://mega.nz/file/MegaOne#MegaOneHash
"""
    entries = parse_message(text, source_message_id=12)
    urls = {e.mega_url for e in entries}
    assert len(urls) == 2
    assert any(u.startswith("https://mega.nz/") for u in urls)
    assert any(u.startswith("https://www.mediafire.com/") for u in urls)


def test_gdrive_url_extracted():
    text = "https://drive.google.com/file/d/1a2b3c4d5e6f7g8h9i0j/view?usp=sharing"
    entries = parse_message(text, source_message_id=13)
    assert len(entries) == 1
    assert entries[0].mega_url.startswith("https://drive.google.com/file/d/")
    assert entries[0].is_folder is False


def test_1fichier_url_extracted():
    text = "https://1fichier.com/?abcdefghij1234567890"
    entries = parse_message(text, source_message_id=14)
    assert len(entries) == 1
    assert entries[0].mega_url.startswith("https://1fichier.com/?")


def test_magnet_url_extracted():
    text = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Cool+Game"
    entries = parse_message(text, source_message_id=15)
    assert len(entries) == 1
    assert entries[0].mega_url.startswith("magnet:?xt=urn:btih:")
