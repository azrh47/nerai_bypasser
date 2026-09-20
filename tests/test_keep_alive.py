"""Unit tests for the keep-alive heartbeat cog.

The cog's only job is to generate inbound HTTP traffic against the service's
own ``/healthz`` and ``/`` endpoints so a sleeping platform (Render free tier)
sees activity and resets its idle timer. Two properties make it worth having,
and both are pinned here:

1. it ticks both probe paths, and
2. a failed probe does NOT kill the loop. A heartbeat that dies on the first
   timeout is worse than no heartbeat: the logs stay quiet while the service
   silently sleeps again.

The aiohttp fake matters: ``ClientSession.get(url)`` is a *synchronous* call
that returns an object usable as an ``async with`` context manager (in real
aiohttp it is a ``_RequestContextManager``, which is also awaitable). The cog
uses the ``async with`` form, so the fake models exactly that.
"""
from __future__ import annotations

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from cogs import keep_alive

BASE_URL = "http://127.0.0.1:10000"


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    async def read(self) -> bytes:
        return b"OK"


class _FakeGetContext:
    """What ``ClientSession.get(url)`` returns in the cog's usage pattern."""

    def __init__(self, url: str, outcome: object) -> None:
        self._url = url
        self._outcome = outcome

    async def __aenter__(self) -> _FakeResponse:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome  # type: ignore[return-value]

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Stand-in for ``aiohttp.ClientSession`` recording every probed URL."""

    def __init__(self, outcome_for) -> None:
        self._outcome_for = outcome_for
        self.urls: list[str] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def get(self, url: str, **kwargs: object) -> _FakeGetContext:
        self.urls.append(url)
        return _FakeGetContext(url, self._outcome_for(url))


@pytest.fixture(autouse=True)
def _clean_keep_alive_env(monkeypatch: pytest.MonkeyPatch):
    """Keep module-level constants deterministic across tests.

    The interval/timeout/base-url knobs are read once at import time, so a test
    that changes them reloads the module and this fixture reloads it back to
    defaults afterwards (otherwise a rewritten interval would leak sideways
    into the other tests in this file).
    """
    for key in (
        "KEEP_ALIVE_INTERVAL_SEC",
        "KEEP_ALIVE_TIMEOUT_SEC",
        "KEEP_ALIVE_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    importlib.reload(keep_alive)


def _install_session(monkeypatch: pytest.MonkeyPatch, outcome_for) -> _FakeSession:
    """Patch the aiohttp ClientSession the cog constructs (one per request)."""
    session = _FakeSession(outcome_for)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
    return session


def _ok(_url: str) -> _FakeResponse:
    return _FakeResponse(200)


def test_tick_hits_healthz_then_root(monkeypatch) -> None:
    async def run() -> list[str]:
        session = _install_session(monkeypatch, _ok)
        cog = keep_alive.KeepAlive(MagicMock(), health_base_url=BASE_URL)
        await cog._tick()
        return session.urls

    urls = asyncio.run(run())
    # /healthz first: that is the path render.yaml sets as healthCheckPath and
    # the path an external pinger (UptimeRobot) is pointed at.
    assert urls == [f"{BASE_URL}/healthz", f"{BASE_URL}/"]


def test_tick_tolerates_a_failing_probe(monkeypatch) -> None:
    def outcome(url: str) -> object:
        if url.endswith("/healthz"):
            return aiohttp.ClientConnectorError(
                MagicMock(), OSError("connection refused")
            )
        return _FakeResponse(200)

    async def run() -> list[str]:
        session = _install_session(monkeypatch, outcome)
        cog = keep_alive.KeepAlive(MagicMock(), health_base_url=BASE_URL)
        await cog._tick()  # must not raise
        return session.urls

    urls = asyncio.run(run())
    # The failing endpoint must not short-circuit the healthy one.
    assert urls == [f"{BASE_URL}/healthz", f"{BASE_URL}/"]


def test_run_keeps_ticking_after_repeated_failures(monkeypatch) -> None:
    """Regression: a transient error used to kill the task permanently."""

    def boom(_url: str) -> object:
        return asyncio.TimeoutError()

    async def run() -> tuple[bool, int]:
        session = _install_session(monkeypatch, boom)
        monkeypatch.setattr(keep_alive, "_KEEP_ALIVE_INTERVAL_SEC", 0.01)

        bot = MagicMock()
        bot.wait_until_ready = AsyncMock()
        bot.is_closed = MagicMock(return_value=False)
        cog = keep_alive.KeepAlive(bot, health_base_url=BASE_URL)

        task = asyncio.get_running_loop().create_task(cog._run())
        await asyncio.sleep(0.08)
        still_running = not task.done()

        task.cancel()
        # _run swallows CancelledError to log a clean shutdown, so await it
        # without letting the cancellation escape this test.
        await asyncio.gather(task, return_exceptions=True)
        return still_running, len(session.urls)

    still_running, probes = asyncio.run(run())
    assert still_running, "heartbeat task died on a failed probe"
    assert probes > 2, f"expected several retries, only probed {probes} times"


def test_cog_load_starts_task_and_unload_cancels_it(monkeypatch) -> None:
    async def run() -> tuple[bool, bool]:
        session = _install_session(monkeypatch, _ok)
        bot = MagicMock()
        bot.loop = asyncio.get_running_loop()
        bot.wait_until_ready = AsyncMock()
        bot.is_closed = MagicMock(return_value=False)
        cog = keep_alive.KeepAlive(bot, health_base_url=BASE_URL)

        await cog.cog_load()
        started = cog._task is not None and not cog._task.done()

        await cog.cog_unload()
        return started, cog._task is None

    started, cancelled = asyncio.run(run())
    assert started, "cog_load() must start the heartbeat task"
    assert cancelled, "cog_unload() must cancel it and clear the reference"


def test_unload_is_safe_without_a_started_task() -> None:
    """Unloading a cog whose task never started (or already died) must not raise."""
    cog = keep_alive.KeepAlive(MagicMock(), health_base_url=BASE_URL)
    cog._task = None
    asyncio.run(cog.cog_unload())  # no exception == pass


def test_interval_defaults_to_ten_minutes(monkeypatch) -> None:
    """10 min sits safely inside the 15-min inactivity sleep window."""
    monkeypatch.delenv("KEEP_ALIVE_INTERVAL_SEC", raising=False)
    reloaded = importlib.reload(keep_alive)
    assert reloaded._KEEP_ALIVE_INTERVAL_SEC == 600


def test_zero_interval_loads_cog_without_starting_task(monkeypatch) -> None:
    """A non-positive interval must disable the heartbeat, not hot-loop it."""
    monkeypatch.setenv("KEEP_ALIVE_INTERVAL_SEC", "0")
    reloaded = importlib.reload(keep_alive)

    async def run() -> object:
        bot = MagicMock()
        bot.loop = asyncio.get_running_loop()
        cog = reloaded.KeepAlive(bot, health_base_url=BASE_URL)
        await cog.cog_load()
        return cog._task

    assert asyncio.run(run()) is None


def test_interval_is_read_from_env_on_import(monkeypatch) -> None:
    monkeypatch.setenv("KEEP_ALIVE_INTERVAL_SEC", "120")
    reloaded = importlib.reload(keep_alive)
    assert reloaded._KEEP_ALIVE_INTERVAL_SEC == 120


def test_base_url_trailing_slash_is_stripped() -> None:
    """Render's dashboard value is easy to paste with a trailing slash."""
    cog = keep_alive.KeepAlive(MagicMock(), health_base_url=f"{BASE_URL}/")
    assert cog.health_base_url == BASE_URL
