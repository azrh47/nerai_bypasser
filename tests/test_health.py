"""Unit tests for the aiohttp /healthz endpoint started by ``main.py``.

The server exists to satisfy Render's ``type: web`` HTTP health probe. If it
ever regresses (eg. removed, returns non-200, fails to bind $PORT), the
service will be marked "Unhealthy" in the dashboard.

The tests here intentionally avoid real socket binds: socket tests are
flaky (TIME_WAIT reuse, parallel pytest workers) and the handler's
contract is the only thing Render actually probes. We exercise the
handler directly, the route registration via the shared prod helper,
and the ``_parse_port`` env-var parsing edge cases.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from main import _build_health_app, _health_response, _parse_port


def _run(coro):
    return asyncio.run(coro)


# ---------- _health_response: the contract Render depends on ----------------


def test_health_response_returns_200_ok() -> None:
    """Render's probe hits the registered route; ensure it gets 200 OK."""
    request = SimpleNamespace()  # handler ignores the request entirely
    resp = _run(_health_response(request))
    assert resp.status == 200
    assert resp.text == "OK"


def test_health_response_works_for_any_request_shape() -> None:
    """The handler must not look at any request attribute.

    Passing ``None`` is a strict test: any unintended attribute access
    will raise AttributeError, surfacing the regression before Render does.
    """
    resp = _run(_health_response(None))
    assert resp.status == 200


# ---------- _build_health_app: route registration contract ------------------
#
# Render probes the path it expects; if a future edit drops a ``add_get``
# from the shared prod helper, the handler unit tests above would still
# pass while Render starts getting 404s. These tests guard the wiring
# on the actual production ``web.Application``.


def test_health_app_registers_root_and_healthz_routes() -> None:
    """If someone removes an add_get call, Render would 404; this catches it."""
    app = _build_health_app()  # sync helper -- not wrapped in _run
    routes = {r.canonical for r in app.router.resources()}
    assert "/" in routes, "Render's default probe hits / -- must be 200 OK"
    assert "/healthz" in routes, (
        "Kubernetes-style pinger / UptimeRobot points at /healthz -- "
        "must be 200 OK"
    )


def test_health_app_endpoint_count_matches_expectation() -> None:
    """If someone accidentally duplicates route registration, count it."""
    app = _build_health_app()
    paths = [r.canonical for r in app.router.resources()]
    assert len(paths) == 2, f"expected exactly 2 routes, got {paths}"
    assert paths.count("/") == 1, "duplicate / registration"
    assert paths.count("/healthz") == 1, "duplicate /healthz registration"


# ---------- _parse_port: env-var parsing edge cases --------------------------


def test_parse_port_returns_integer_when_set(monkeypatch) -> None:
    monkeypatch.setenv("PORT", "12345")
    assert _parse_port() == 12345


def test_parse_port_falls_back_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("PORT", raising=False)
    assert _parse_port() == 10000


def test_parse_port_falls_back_when_blank(monkeypatch) -> None:
    """A blank PORT would otherwise blow up ``int('')``. Must fall back."""
    monkeypatch.setenv("PORT", "")
    assert _parse_port() == 10000


def test_parse_port_falls_back_when_non_numeric(monkeypatch) -> None:
    """A typo'd PORT must NOT crash the deploy; fall back to default."""
    monkeypatch.setenv("PORT", "abc123")
    assert _parse_port() == 10000
