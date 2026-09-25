"""Tests for the login retry wrapper in ``main.py``.

Why this file exists: Discord's Cloudflare layer intermittently answers
``/users/@me`` with Error 1015 (HTTP 429, HTML body) on boot, and the observed
production log showed it happening on every deploy. The wrapper retries that --
but retrying the *wrong* call is worse than not retrying at all, because
discord.py invokes ``setup_hook()`` from inside ``login()``. Re-entering
``bot.start()`` therefore re-runs ``setup_hook()``, re-adds every cog, and dies
with ``ClientException: Cog named 'Indexer' already loaded`` -- turning an
18-second blip into a crash loop. Several tests below exist purely to keep that
from coming back.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import main as main_module


def _fake_response(status: int, content_type: str, reason: str = ""):
    """Only the attributes ``discord.HTTPException`` itself touches are needed.

    ``discord.HTTPException.__init__`` formats ``{status} {reason}`` into the
    message, so ``reason`` has to be present or construction raises.
    """
    return SimpleNamespace(
        status=status, content_type=content_type, reason=reason or "Error"
    )


def _cloudflare_429() -> discord.HTTPException:
    """A 429 with an HTML body, i.e. a Cloudflare block page (not JSON)."""
    return discord.HTTPException(
        _fake_response(429, "text/html", "Too Many Requests"),
        "<html><title>Access denied</title></html>",
    )


def _discord_json_429() -> discord.HTTPException:
    """A *real* Discord rate limit: JSON body, no Cloudflare page."""
    return discord.HTTPException(
        _fake_response(429, "application/json", "Too Many Requests"),
        {"message": "You are being rate limited."},
    )


# ---------- _is_login_rate_limit: what is (and isn't) retryable -------------


def test_cloudflare_429_is_retryable() -> None:
    assert main_module._is_login_rate_limit(_cloudflare_429()) is True


def test_429_without_a_response_is_retryable() -> None:
    """Some transport failures surface as HTTPException with no response object."""
    exc = discord.HTTPException(_fake_response(429, "text/html"), "boom")
    exc.response = None  # type: ignore[assignment]
    assert main_module._is_login_rate_limit(exc) is True


def test_a_real_discord_rate_limit_is_not_retried_here() -> None:
    """discord.py already honours the JSON body's retry_after; don't double-retry.

    Retrying it here would add up to nine minutes of pointless waiting and would
    mask the fact that the token/permissions are the actual problem.
    """
    assert main_module._is_login_rate_limit(_discord_json_429()) is False


@pytest.mark.parametrize(
    "exc",
    [
        discord.LoginFailure("Improper token has been passed."),
        discord.Forbidden(_fake_response(403, "application/json", "Forbidden"), "nope"),
        RuntimeError("something else entirely"),
        KeyboardInterrupt(),
    ],
    ids=["LoginFailure", "Forbidden-403", "RuntimeError", "KeyboardInterrupt"],
)
def test_non_retryable_exceptions_are_not_retried(exc: BaseException) -> None:
    assert main_module._is_login_rate_limit(exc) is False


# ---------- _login_with_retry: the retry contract ---------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_login_is_retried_after_a_cloudflare_block(monkeypatch) -> None:
    """Attempt 2 must log in successfully once the transient block clears."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)

    bot = MagicMock()
    bot.login = AsyncMock(side_effect=[_cloudflare_429(), None])

    _run(main_module._login_with_retry(bot))

    assert bot.login.await_count == 2
    assert len(sleeps) == 1
    # First backoff is LOGIN_BASE_BACKOFF_SEC plus at most 25% jitter.
    assert main_module.LOGIN_BASE_BACKOFF_SEC <= sleeps[0] <= main_module.LOGIN_BASE_BACKOFF_SEC * 1.25


def test_retry_never_calls_bot_start(monkeypatch) -> None:
    """Regression: start() re-enters setup_hook() and re-adds every cog.

    Simulates discord.py's actual behaviour -- login() calls setup_hook(), and
    setup_hook() explodes if it runs twice -- then asserts the wrapper survives.
    """
    setup_calls: list[int] = []

    bot = MagicMock()

    async def fake_setup_hook() -> None:
        setup_calls.append(1)
        if len(setup_calls) > 1:
            raise discord.ClientException("Cog named 'Indexer' already loaded")

    async def fake_login(_token: str) -> None:
        if len(setup_hooks_seen) == 0:
            setup_hooks_seen.append(1)
            raise _cloudflare_429()
        await fake_setup_hook()

    setup_hooks_seen: list[int] = []
    bot.login = AsyncMock(side_effect=fake_login)
    bot.setup_hook = AsyncMock(side_effect=fake_setup_hook)
    # start() would be the buggy re-entry point; make it fail loudly if used.
    bot.start = AsyncMock(side_effect=AssertionError("must not call bot.start()"))
    monkeypatch.setattr(main_module.asyncio, "sleep", AsyncMock())

    _run(main_module._login_with_retry(bot))

    assert len(setup_calls) == 1, "setup_hook ran more than once"
    bot.start.assert_not_awaited()


def test_default_retry_has_no_ceiling(monkeypatch) -> None:
    """By default the wrapper outlasts a long Cloudflare block instead of exiting.

    Giving up would hand control back to Render, whose restart resets the
    backoff to its base and re-hammers the blocked endpoint. Failing more times
    than the old default of 5 must therefore still eventually succeed.
    """
    monkeypatch.setattr(main_module, "LOGIN_MAX_RETRIES", 0)
    monkeypatch.setattr(main_module.asyncio, "sleep", AsyncMock())

    bot = MagicMock()
    bot.login = AsyncMock(side_effect=[_cloudflare_429()] * 8 + [None])

    _run(main_module._login_with_retry(bot))

    assert bot.login.await_count == 9


def test_failed_login_closes_the_leaked_http_session(monkeypatch) -> None:
    """Regression: each failed login strands an aiohttp session.

    discord.py's ``static_login`` creates a fresh ``ClientSession`` per call and
    never closes the old one on failure, so a long block would accumulate one
    unclosed session (and one aiohttp warning) per retry.
    """
    monkeypatch.setattr(main_module.asyncio, "sleep", AsyncMock())

    bot = MagicMock()
    bot.login = AsyncMock(side_effect=[_cloudflare_429(), _cloudflare_429(), None])
    bot.http.close = AsyncMock()

    _run(main_module._login_with_retry(bot))

    # Two failed attempts -> two sessions reclaimed before the next try.
    assert bot.http.close.await_count == 2


def test_gives_up_after_max_retries_and_surfaces_the_error(monkeypatch) -> None:
    """Bounded mode is opt-in: setting a positive cap exits so Render restarts."""
    monkeypatch.setattr(main_module, "LOGIN_MAX_RETRIES", 2)
    sleeps: list[float] = []
    monkeypatch.setattr(
        main_module.asyncio, "sleep", AsyncMock(side_effect=lambda s: sleeps.append(s))
    )

    bot = MagicMock()
    bot.login = AsyncMock(side_effect=_cloudflare_429())

    with pytest.raises(discord.HTTPException):
        _run(main_module._login_with_retry(bot))

    assert bot.login.await_count == 3  # initial attempt + LOGIN_MAX_RETRIES
    assert len(sleeps) == 2


def test_backoff_grows_and_respects_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "LOGIN_MAX_RETRIES", 6)
    monkeypatch.setattr(main_module, "LOGIN_MAX_BACKOFF_SEC", 30.0)
    sleeps: list[float] = []
    monkeypatch.setattr(
        main_module.asyncio, "sleep", AsyncMock(side_effect=lambda s: sleeps.append(s))
    )

    bot = MagicMock()
    bot.login = AsyncMock(side_effect=_cloudflare_429())

    with pytest.raises(discord.HTTPException):
        _run(main_module._login_with_retry(bot))

    # Each sleep is its (capped) base backoff plus up to 25% jitter, so the
    # raw values are allowed to dip; the *base* must still be non-decreasing.
    for attempt, slept in enumerate(sleeps, start=1):
        base = min(30.0, main_module.LOGIN_BASE_BACKOFF_SEC * 2 ** (attempt - 1))
        assert base <= slept <= base * 1.25, (
            f"attempt {attempt} slept {slept}s, expected [{base}, {base * 1.25}]"
        )
    assert sleeps[0] < sleeps[-1], f"backoff must grow: {sleeps}"


def test_non_rate_limit_error_propagates_immediately(monkeypatch) -> None:
    """A bad token is not transient; retrying it just delays the real error."""
    monkeypatch.setattr(
        main_module.asyncio,
        "sleep",
        AsyncMock(side_effect=AssertionError("must not sleep before retrying")),
    )
    bot = MagicMock()
    bot.login = AsyncMock(side_effect=discord.LoginFailure("Improper token has been passed."))

    with pytest.raises(discord.LoginFailure):
        _run(main_module._login_with_retry(bot))

    assert bot.login.await_count == 1


def test_keyboard_interrupt_propagates(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module.asyncio,
        "sleep",
        AsyncMock(side_effect=AssertionError("must not retry on Ctrl-C")),
    )
    bot = MagicMock()
    bot.login = AsyncMock(side_effect=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _run(main_module._login_with_retry(bot))
