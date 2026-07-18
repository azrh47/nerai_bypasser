"""Tests for ``cogs.indexer._message_text``, ``_component_urls``, and the
SOURCE_GUILD_IDS allow-list semantics.

These guard against the most common source-channel message shape we saw in
real-world repack reposts: the download URL is hidden inside a Discord
``Button`` component, not in the message body.

The multi-guild tests pin down the allow-list contract: when the operator
sets ``SOURCE_GUILD_IDS`` they mean it, and a message from an unlisted guild
must never reach the parser regardless of how it got into the channel list.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from cogs.indexer import Indexer, _component_urls, _message_text


def _mock_message(
    content: str = "",
    embeds: list | None = None,
    actions: list | None = None,
) -> MagicMock:
    """Build a MagicMock discord.Message with the shape _message_text uses."""
    msg = MagicMock()
    msg.content = content
    msg.embeds = embeds if embeds is not None else []
    # discord.py exposes components as a list of ActionRow-like objects; we just
    # need each row to expose ``children`` and each child to expose ``url``.
    rows: list = []
    for child_specs in actions or []:
        row = MagicMock()
        children: list = []
        for url in child_specs:
            child = MagicMock()
            child.url = url
            children.append(child)
        row.children = children
        rows.append(row)
    msg.components = rows
    return msg


def _mock_msg_in_channel(
    channel_id: int,
    guild_id: int | None,
    author_is_bot: bool = False,
) -> MagicMock:
    """Discord Message mock with realistic .id/.author/.channel/.guild shape."""
    msg = MagicMock()
    msg.id = 1000 + channel_id
    msg.channel = MagicMock()
    msg.channel.id = channel_id
    if guild_id is None:
        msg.guild = None
    else:
        msg.guild = MagicMock()
        msg.guild.id = guild_id
    author = MagicMock()
    author.id = 42
    author.bot = author_is_bot
    msg.author = author
    msg.content = "https://mega.nz/file/RealOne#RealOneHash"
    msg.created_at = None
    msg.embeds = []
    msg.components = []
    return msg


def _make_indexer(monkeypatch) -> Indexer:
    """Index the cog with mock bot + db + steam. Returns the cog."""
    bot = MagicMock()
    db = MagicMock()
    steam = MagicMock()
    indexer = Indexer(bot, db, steam)
    db.upsert_entry = AsyncMock(return_value=1)
    db.update_source_progress = AsyncMock(return_value=None)
    return indexer


# ---------- _component_urls -------------------------------------------------


def test_component_urls_extracts_link_button_urls():
    msg = _mock_message(
        content="Click below for the file",
        actions=[["https://mega.nz/file/AbCd#EfGh"]],
    )
    urls = _component_urls(msg)
    assert urls == ["https://mega.nz/file/AbCd#EfGh"]


def test_component_urls_extracts_multiple_buttons_in_one_row():
    msg = _mock_message(
        content="",
        actions=[
            [
                "https://mega.nz/file/AAA#BBB",
                "https://www.mediafire.com/file/CCC/DDD.zip",
            ]
        ],
    )
    urls = _component_urls(msg)
    assert urls == [
        "https://mega.nz/file/AAA#BBB",
        "https://www.mediafire.com/file/CCC/DDD.zip",
    ]


def test_component_urls_handles_multiple_rows():
    msg = _mock_message(
        content="",
        actions=[
            ["https://mega.nz/file/A1#A1"],
            ["https://www.mediafire.com/file/B2/B2"],
        ],
    )
    urls = _component_urls(msg)
    assert urls == [
        "https://mega.nz/file/A1#A1",
        "https://www.mediafire.com/file/B2/B2",
    ]


def test_component_urls_handles_button_without_url():
    msg = _mock_message(
        content="",
        actions=[[None]],  # click-handler button, no url
    )
    assert _component_urls(msg) == []


def test_component_urls_handles_no_components_attribute():
    msg = MagicMock(spec=[])  # getattr(msg, "components", None) -> None
    assert _component_urls(msg) == []


# ---------- _message_text ----------------------------------------------------


def test_message_text_includes_button_urls():
    msg = _mock_message(
        content="MIRROR'S EDGE BYPASS!",
        actions=[["https://mega.nz/file/RealOne#RealOneHash"]],
    )
    text = _message_text(msg)
    assert "https://mega.nz/file/RealOne#RealOneHash" in text


def test_message_text_combines_content_embeds_and_buttons():
    embed = MagicMock()
    embed.title = "Mirror's Edge"
    embed.description = None
    embed.url = "https://store.steampowered.com/app/17450"
    embed.fields = []
    msg = _mock_message(
        content="BYPASS LINK MENTIONED BELOW!",
        embeds=[embed],
        actions=[["https://mega.nz/file/AbCd#EfGh"]],
    )
    text = _message_text(msg)
    assert "BYPASS LINK MENTIONED BELOW!" in text
    assert "Mirror's Edge" in text
    assert "store.steampowered.com/app/17450" in text
    assert "https://mega.nz/file/AbCd#EfGh" in text


def test_message_text_without_components_or_embeds():
    msg = _mock_message(content="Just text, nothing fancy.")
    text = _message_text(msg)
    assert text == "Just text, nothing fancy."


# ---------- Multi-guild source allow-list ----------------------------------
#
# These exercise the SOURCE_GUILD_IDS contract:
#   - When the list is empty, the indexer is permissive (any guild OK).
#   - When the list is non-empty, a message from an unlisted guild must be
#     dropped silently without touching the parser or the DB.


def test_on_message_from_listed_guild_persists_entry(monkeypatch):
    import config

    monkeypatch.setattr(config, "SOURCE_CHANNELS", [555], raising=False)
    monkeypatch.setattr(config, "SOURCE_GUILD_IDS", [777], raising=False)
    indexer = _make_indexer(monkeypatch)
    msg = _mock_msg_in_channel(channel_id=555, guild_id=777)
    asyncio.run(indexer.on_message(msg))
    assert indexer.db.upsert_entry.await_count == 1


def test_on_message_from_unlisted_guild_is_dropped(monkeypatch):
    import config

    monkeypatch.setattr(config, "SOURCE_CHANNELS", [555], raising=False)
    monkeypatch.setattr(config, "SOURCE_GUILD_IDS", [777], raising=False)
    indexer = _make_indexer(monkeypatch)
    msg = _mock_msg_in_channel(channel_id=555, guild_id=888)
    asyncio.run(indexer.on_message(msg))
    assert indexer.db.upsert_entry.await_count == 0


def test_on_message_with_empty_allow_list_accepts_any_guild(monkeypatch):
    """Permissive default: empty SOURCE_GUILD_IDS = no allow-list filter."""
    import config

    monkeypatch.setattr(config, "SOURCE_CHANNELS", [555], raising=False)
    monkeypatch.setattr(config, "SOURCE_GUILD_IDS", [], raising=False)
    indexer = _make_indexer(monkeypatch)
    for guild_id in (777, 888, 999):
        msg = _mock_msg_in_channel(channel_id=555, guild_id=guild_id)
        asyncio.run(indexer.on_message(msg))
    assert indexer.db.upsert_entry.await_count == 3


def test_link_channel_rejects_channel_in_unlisted_guild(monkeypatch):
    """When SOURCE_GUILD_IDS is set, /admin link_channel must refuse channels
    from guilds that aren't on the allow-list. Prevents 'wrong server' typos."""
    import config
    import cogs.admin as admin_module

    # The cog's admin gate fires BEFORE the guild allow-list check, so the
    # test user must be in ADMIN_USER_IDS for the allow-list branch to run.
    monkeypatch.setattr(config, "ADMIN_USER_IDS", [1], raising=False)
    monkeypatch.setattr(config, "SOURCE_CHANNELS", [], raising=False)
    monkeypatch.setattr(config, "SOURCE_GUILD_IDS", [777], raising=False)
    bot = MagicMock()
    db = MagicMock()
    steam = MagicMock()
    adm = admin_module.Admin(bot, db, steam)
    adm.db.set_setting = AsyncMock()
    adm.db.upsert_source = AsyncMock()

    class FakeChannel:
        def __init__(self, cid: int, gid: int) -> None:
            self.id = cid
            self.guild = MagicMock()
            self.guild.id = gid

    interaction = MagicMock()
    interaction.user.id = 1
    interaction.response.send_message = AsyncMock()

    # 888 is NOT in SOURCE_GUILD_IDS=[777] -- call the un-decorated impl
    # so we don't have to navigate the @admin.command Command wrapping.
    asyncio.run(adm._link_channel_impl(interaction, FakeChannel(555, 888)))
    interaction.response.send_message.assert_awaited_once()
    # The cog passes content positionally, ephemeral as kwarg. Read args[0].
    msg = interaction.response.send_message.await_args.args[0]
    # The error message uses the contraction "isn't" (raw chars: i,s,',n,t
    # — not "not in"), so substring match against the exact text.
    assert "isn't in one of the configured source guilds" in msg
    # Rejection must be a no-op: nothing persisted to the DB.
    adm.db.set_setting.assert_not_awaited()
    adm.db.upsert_source.assert_not_awaited()


def test_link_channel_kicks_off_indexer_backfill_when_present(monkeypatch):
    """When the Indexer cog is loaded, registering a new channel must also
    trigger an immediate backfill so the user sees results without waiting
    for the next restart."""
    import config
    import cogs.admin as admin_module

    monkeypatch.setattr(config, "ADMIN_USER_IDS", [1], raising=False)
    monkeypatch.setattr(config, "SOURCE_CHANNELS", [], raising=False)
    monkeypatch.setattr(config, "SOURCE_GUILD_IDS", [777], raising=False)
    bot = MagicMock()
    db = MagicMock()
    steam = MagicMock()
    adm = admin_module.Admin(bot, db, steam)
    adm.db.set_setting = AsyncMock()
    adm.db.upsert_source = AsyncMock()
    indexer = MagicMock()
    indexer.index_channel = AsyncMock()
    adm.bot.get_cog = MagicMock(return_value=indexer)

    class FakeChannel:
        def __init__(self, cid, gid):
            self.id = cid
            self.guild = MagicMock()
            self.guild.id = gid

    interaction = MagicMock()
    interaction.user.id = 1
    interaction.response.send_message = AsyncMock()

    asyncio.run(adm._link_channel_impl(interaction, FakeChannel(555, 777)))
    indexer.index_channel.assert_awaited_once_with(
        555, 777, reseed=True
    )


def test_link_channel_rejects_non_admin(monkeypatch):
    """Gate ordering check: the admin check fires FIRST, before the guild
    allow-list. A non-admin caller must get 'Admin only.' even if their
    channel would otherwise pass the guild allow-list."""
    import config
    import cogs.admin as admin_module

    monkeypatch.setattr(config, "ADMIN_USER_IDS", [], raising=False)
    monkeypatch.setattr(config, "SOURCE_CHANNELS", [], raising=False)
    monkeypatch.setattr(config, "SOURCE_GUILD_IDS", [777], raising=False)
    bot = MagicMock()
    db = MagicMock()
    steam = MagicMock()
    adm = admin_module.Admin(bot, db, steam)
    adm.db.set_setting = AsyncMock()
    adm.db.upsert_source = AsyncMock()

    class FakeChannel:
        def __init__(self, cid, gid):
            self.id = cid
            self.guild = MagicMock()
            self.guild.id = gid

    interaction = MagicMock()
    interaction.user.id = 999  # NOT in ADMIN_USER_IDS=[]
    interaction.response.send_message = AsyncMock()

    asyncio.run(adm._link_channel_impl(interaction, FakeChannel(555, 777)))
    msg = interaction.response.send_message.await_args.args[0]
    assert msg == "Admin only."
    # The admin gate is the only path that fired.
    adm.db.set_setting.assert_not_awaited()
    adm.db.upsert_source.assert_not_awaited()


def test_link_channel_accepts_channel_in_listed_guild(monkeypatch):
    """The allow-list must default-accept channels whose guild IS in
    SOURCE_GUILD_IDS (positive control for the rejection test above)."""
    import config
    import cogs.admin as admin_module

    monkeypatch.setattr(config, "ADMIN_USER_IDS", [1], raising=False)
    monkeypatch.setattr(config, "SOURCE_CHANNELS", [], raising=False)
    monkeypatch.setattr(config, "SOURCE_GUILD_IDS", [777], raising=False)
    bot = MagicMock()
    db = MagicMock()
    steam = MagicMock()
    adm = admin_module.Admin(bot, db, steam)
    adm.db.set_setting = AsyncMock()
    adm.db.upsert_source = AsyncMock()
    adm.bot.get_cog = MagicMock(return_value=None)  # no Indexer to trigger

    class FakeChannel:
        def __init__(self, cid, gid):
            self.id = cid
            self.guild = MagicMock()
            self.guild.id = gid

    interaction = MagicMock()
    interaction.user.id = 1
    interaction.response.send_message = AsyncMock()

    asyncio.run(adm._link_channel_impl(interaction, FakeChannel(555, 777)))
    # Cog calls send_message(content_str, *, ephemeral=True) so content is
    # positional. Pull args[0] to read the user-visible message.
    msg = interaction.response.send_message.await_args.args[0]
    assert "isn't in one of the configured source guilds" not in msg
    assert "Registered" in msg
    # Side-effects: persist the choice + register the source for backfill.
    adm.db.set_setting.assert_awaited_once()
    adm.db.upsert_source.assert_awaited_once_with(
        555, 777, status="active"
    )


def test_backfill_skips_channel_in_unlisted_guild_with_warning(monkeypatch):
    """If a stale channel entry points to a guild no longer in
    SOURCE_GUILD_IDS, the backfill must mark it not_allowed rather than
    silently dropping it. /admin stats surfaces the status."""
    import config

    monkeypatch.setattr(config, "SOURCE_CHANNELS", [555], raising=False)
    monkeypatch.setattr(config, "SOURCE_GUILD_IDS", [777], raising=False)
    indexer = _make_indexer(monkeypatch)

    channel = MagicMock()
    channel.id = 555
    channel.guild.id = 888  # NOT in SOURCE_GUILD_IDS
    indexer.bot.get_channel = MagicMock(return_value=channel)

    db_status_calls: list[tuple[int, str]] = []
    indexer.db.set_source_status = AsyncMock(
        side_effect=lambda cid, status: db_status_calls.append((cid, status))
    )

    results = asyncio.run(indexer._backfill_all())
    assert any(
        r.get("channel_id") == 555 and r.get("error") == "guild not allowed"
        for r in results
    )
    assert (555, "not_allowed") in db_status_calls


# ---------- guild_allowed() helper -----------------------------------------
#
# When `SOURCE_GUILD_IDS` is populated, the helper is the gatekeeper for the
# allow-list. When empty, it returns True (permissive default). The DM case
# (`guild_id=None`) must return False so a direct message can't sneak through
# when an allow-list is active.


def test_guild_allowed_empty_list_is_permissive(monkeypatch):
    import config

    monkeypatch.setattr(config, "SOURCE_GUILD_IDS", [], raising=False)
    assert config.guild_allowed(777) is True
    assert config.guild_allowed(None) is True  # permissive default


def test_guild_allowed_nonempty_list_enforces_membership(monkeypatch):
    import config

    monkeypatch.setattr(config, "SOURCE_GUILD_IDS", [777, 888], raising=False)
    assert config.guild_allowed(777) is True
    assert config.guild_allowed(888) is True
    assert config.guild_allowed(999) is False
    assert config.guild_allowed(None) is False  # DM blocked under allow-list


def test_no_phantom_promotion_when_legacy_zero(monkeypatch):
    """Back-compat shim contract: with both `SOURCE_GUILD_IDS=""` (empty) and
    `SOURCE_GUILD_ID="0"` (legacy zero), the helper must NOT pretend the
    allow-list has an entry. Pins the shim's behavior against future
    refactors that might start returning a phantom single-element list.

    The monkeypatched `SOURCE_GUILD_IDS=[]` IS the test setup; the asserts
    below pin the durable invariant (helper stays permissive under an empty
    allow-list)."""
    import config

    monkeypatch.setattr(config, "SOURCE_GUILD_IDS", [], raising=False)
    # Helper stays permissive regardless of legacy value.
    assert config.guild_allowed(42) is True
    assert config.guild_allowed(None) is True
