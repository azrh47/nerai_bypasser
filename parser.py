"""Extract Mega.nz URLs + game-identifying metadata from Discord messages.

Design goals
------------
* Defensive: tolerate any formatting (plain text, embeds, multi-line repacks).
* Multi-URL: a single message may contain several Mega links; each becomes its
  own ParsedEntry.
* Resilient: when only the URL is present (no game name / app_id / size), still
  emit a ParsedEntry so the index isn't lost.

Entry points
------------
``parse_message(text, source_message_id, ...) -> list[ParsedEntry]``
    The main extractor. The cog layer is responsible for combining message
    content with embed fields before calling this.

Game-name extraction strategies (tried in order):
1. Explicit labeled line (``Title:``, ``Game:``, ``Name:``).
2. First non-empty, non-URL, non-metadata line ("Cyberpunk 2077 v2.1.2" style).
3. Closest filename adjacent to each URL (with archive extension stripped).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Mega URLs:
#   https://mega.nz/file/XXXXXXXX#YYYYYYYYYY
#   https://mega.nz/folder/XXXXXXXX#YYYYYYYYYY
MEGA_URL_RE = re.compile(
    r"https?://mega\.nz/(file|folder)/[A-Za-z0-9_\-]+#[A-Za-z0-9_\-]+",
    re.IGNORECASE,
)

# MediaFire URLs:
#   https://www.mediafire.com/file/XXXXX[/Filename.zip]
#   https://www.mediafire.com/folder/XXXXX
# The filename tail is optional and may be omitted; we accept either form.
# Allowed in the filename portion: alphanumeric + URL-safe punctuation
# (``-``, ``.``, ``+`` for spaces, ``~`` + ``%`` for percent-encoded chars,
# ``'`` for the apostrophe that some real MediaFire links leave unescaped).
# Excluded (``(``, ``)``, whitespace, ``>``, ``"``) so the regex stops cleanly
# at markdown ``[Caption](URL)``, HTML ``<a href="URL">``, and end-of-line.
MEDIAFIRE_URL_RE = re.compile(
    r"https?://(?:www\.)?mediafire\.com/(file|folder)/[A-Za-z0-9_\-]+"
    r"(?:/[\w\-.+~%']*)?",
    re.IGNORECASE,
)

# store.steampowered.com/app/<id>  -> app_id
STEAM_APP_URL_RE = re.compile(
    r"store\.steampowered\.com/app/(\d+)", re.IGNORECASE
)

# "Steam App ID", "App ID", "Steam ID", "App #" markers followed by digits.
APP_ID_RE = re.compile(
    r"(?:steam\s*app\s*id|app(?:lication)?\s*id|steam\s*id|app\s*#)\s*[:#]?\s*(\d{2,8})",
    re.IGNORECASE,
)

# Title line markers used by some repack publishers.
TITLE_LABEL_RE = re.compile(
    r"^\s*(?:title|game(?:\s*name)?|name)\s*[:|\-]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Filename pattern with a known archive / image / exec extension.
FILENAME_RE = re.compile(
    r"([A-Za-z0-9 ._\-\(\)\[\]]{2,}?\.(?:zip|rar|7z|tar\.gz|iso|pkg|exe|dmg))",
    re.IGNORECASE,
)

# Size tokens like "65.4 GB", "11 GB", "640 MB".
SIZE_UNIT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(GB|MB|KB|GiB|MiB|KiB|TB)\b",
    re.IGNORECASE,
)

# Trailing version/patch strings we strip from guessed titles, e.g.
#   "Cyberpunk 2077 v2.1.2" -> "Cyberpunk 2077"
VERSION_TAIL_RE = re.compile(
    # Match a trailing space + optional `-`/`–` + lowercase `v` + digits + `.* to EOL`.
    # Examples: ' v2.1.2', ' - v2.1', '–v3'. Bare numbers like 'Game 2' are NOT
    # stripped (the literal 'v' prefix is required). Case-sensitive: repack
    # convention is lowercase 'v', and dropping IGNORECASE prevents 'Horde V2.zip'
    # from losing its author-intended 'V2'.
    r"\s+(?:[–\-]\s*)?v\d+(?:\.\d+){0,3}.*$",
)

# Lines that look like metadata fields; skip them when picking the
# "first reasonable line" fallback for a game title.
_METADATA_FIELD_RE = re.compile(
    r"^\s*(?:"
    r"genre|size|developer|publisher|release\s*date|language|languages|"
    r"app(?:lication)?\s*id|steam\s*id|app\s*#|"
    r"download|upload|credits|crack|setup|md5|hash|"
    r"updated|update|version|patch|fix|status|progress|note|notes"
    r")\s*[:|\-]",
    re.IGNORECASE,
)

# Common "pure size" line, e.g. "65.4 GB"  — not a title.
_PURE_SIZE_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*(?:GB|MB|KB|GiB|MiB|KiB|TB)\s*$",
    re.IGNORECASE,
)

# URLs anywhere — skip in fallback title selection.
_HAS_URL_RE = re.compile(r"https?://", re.IGNORECASE)


_SIZE_MULTIPLIERS = {
    "GB": 1024 ** 3,
    "MB": 1024 ** 2,
    "KB": 1024,
    "TB": 1024 ** 4,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ParsedEntry:
    """A single Mega.nz entry extracted from a Discord message."""

    mega_url: str
    is_folder: bool = False
    source_message_id: int = 0
    source_author_id: Optional[int] = None
    game_name: Optional[str] = None
    canonical_name: Optional[str] = None
    app_id: Optional[int] = None
    filename: Optional[str] = None
    size_bytes: Optional[int] = None
    posted_at: Optional[str] = None
    raw_text_excerpt: Optional[str] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Only strip a known archive / install extension at the END of the string, so a
# game name like "7zip.zip" doesn't degenerate into "7ip".
_TRAILING_EXTS = re.compile(
    r"\.(?:zip|rar|7z|tar\.gz|iso|pkg|exe|dmg)$", re.IGNORECASE
)


def _strip_extensions(s: str) -> str:
    return _TRAILING_EXTS.sub("", s).strip()


def _first_title_line(text: str) -> Optional[str]:
    """Heuristic fallback for repack posts with no explicit ``Title:`` label.

    Returns the first non-empty line that is NOT a URL, NOT a metadata
    ``Field: value`` line, and is within a reasonable title length —
    with archive-extension suffixes (``Cool Game.zip`` → ``Cool Game``) and
    trailing version strings stripped.
    """
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if _HAS_URL_RE.search(s):
            continue
        if _METADATA_FIELD_RE.match(s):
            continue
        if _PURE_SIZE_RE.match(s):
            continue
        candidate = VERSION_TAIL_RE.sub("", s).strip()
        candidate = _strip_extensions(candidate)
        if 2 <= len(candidate) <= 80:
            return candidate
    return None


def _extract_global(text: str) -> dict[str, Optional[object]]:
    """Message-wide metadata that should be associated with every Mega URL.

    NOTE: filename is NOT included here on purpose - parse_message assigns
    filenames per-URL (via ``_filename_near_url``) so a 3-URL drop doesn't
    leak URL #1's filename onto URL #3's entry.
    """
    out: dict[str, Optional[object]] = {
        "app_id": None,
        "game_name": None,
        "size_bytes": None,
    }

    app_match = APP_ID_RE.search(text)
    if app_match is None:
        app_match = STEAM_APP_URL_RE.search(text)
    if app_match is not None:
        try:
            out["app_id"] = int(app_match.group(1))
        except (ValueError, IndexError):
            pass

    # Title resolution: explicit label first, then first-reasonable-line heuristic.
    labels = TITLE_LABEL_RE.findall(text)
    game_name: Optional[str] = None
    if labels:
        title = labels[0].strip().rstrip(".,;")
        title = VERSION_TAIL_RE.sub("", title).strip()
        if 1 <= len(title) <= 80:
            game_name = title
    if game_name is None:
        game_name = _first_title_line(text)
    out["game_name"] = game_name

    sizes = SIZE_UNIT_RE.findall(text)
    if sizes:
        try:
            value, unit = sizes[0]
            mult = _SIZE_MULTIPLIERS.get(unit.upper().replace("IB", "B"))
            if mult:
                out["size_bytes"] = int(float(value) * mult)
        except (ValueError, KeyError):
            pass

    return out


def _filename_near_url(url: str, text: str) -> Optional[str]:
    """Look for a filename adjacent to ``url``.

    Tries the SAME line as the URL first, then the IMMEDIATELY preceding
    content line (skipping blank lines, ie consecutive ``\\n``s). We do not
    search further back than one content line: in multi-URL posts, a wider
    window would leak an unrelated URL's filename into the wrong entry.
    """
    pos = text.find(url)
    if pos == -1:
        return None

    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos + len(url))
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end]
    match = FILENAME_RE.search(line)
    if match:
        return match.group(1).strip()

    # Immediate preceding content line, skipping blank-line separators.
    if line_start <= 1:
        return None
    cursor = line_start - 2  # last character of the line above (if any)
    while cursor >= 0 and text[cursor] == "\n":
        cursor -= 1
    if cursor < 0:
        return None
    prev_line_end = cursor + 1
    prev_line_start = text.rfind("\n", 0, prev_line_end - 1) + 1
    prev_line = text[prev_line_start:prev_line_end]
    match = FILENAME_RE.search(prev_line)
    return match.group(1).strip() if match else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_message(
    text: str,
    source_message_id: int,
    source_author_id: Optional[int] = None,
    posted_at: Optional[str] = None,
) -> list[ParsedEntry]:
    """Parse a Discord message body for all Mega.nz entries.

    Returns zero or more ``ParsedEntry`` objects - one per distinct Mega URL
    found. Each carries whatever metadata could be confidently extracted.
    """
    if not text:
        return []

    # Walk Mega and MediaFire regexes, dedupe by full URL.
    matches: list[tuple[str, bool]] = []  # (full_url, is_folder)
    seen: set[str] = set()
    for pattern in (MEGA_URL_RE, MEDIAFIRE_URL_RE):
        for m in pattern.finditer(text):
            url = m.group(0)
            if url in seen:
                continue
            seen.add(url)
            matches.append((url, m.group(1).lower() == "folder"))

    if not matches:
        return []

    aggregate = _extract_global(text)
    excerpt = text.strip()[:280]

    entries: list[ParsedEntry] = []
    for url, is_folder in matches:
        per_url_filename = _filename_near_url(url, text)

        # Game name: aggregate heuristic wins. URL-based fallback only if we
        # got literally nothing else.
        game_name = aggregate["game_name"]  # type: ignore[assignment]
        if not game_name:
            candidate = per_url_filename or url
            candidate = _strip_extensions(str(candidate))
            candidate = VERSION_TAIL_RE.sub("", candidate).strip()
            if 1 < len(candidate) <= 80:
                game_name = candidate

        entries.append(
            ParsedEntry(
                mega_url=url,
                is_folder=is_folder,
                source_message_id=source_message_id,
                source_author_id=source_author_id,
                game_name=game_name,
                app_id=aggregate["app_id"],  # type: ignore[assignment]
                # filename is per-URL only — don't leak the first filename in
                # the message to entries that had no filename adjacent to them.
                filename=per_url_filename,
                size_bytes=aggregate["size_bytes"],  # type: ignore[assignment]
                posted_at=posted_at,
                raw_text_excerpt=excerpt,
            )
        )
    return entries
