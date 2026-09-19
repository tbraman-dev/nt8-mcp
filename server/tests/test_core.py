"""Passthrough tools (nt8_mcp.tools_core) against the canned AddOn in fake_addon.py."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import server as nt8  # noqa: E402

HEALTH = {
    "ok": True, "addonVersion": "1.0.0", "nt8Version": "8.1.4", "startedAt": "2026-09-17T09:00:00",
    "connections": [{"name": "Sim101", "status": "Connected"}], "charts": 1,
}
CHARTS = [
    {"id": "c1", "title": "ES 12-26 (5 Min)", "instrument": "ES 12-26", "period": "5 Minute",
     "barsCount": 500, "indicators": ["VWAP"], "strategies": []},
]


def test_nt_health():
    with FakeAddon() as fake:
        fake.json("/health", HEALTH)
        health = nt8.nt_health()
    assert health["ok"] is True
    assert health["addonVersion"] == "1.0.0"


def test_nt_charts():
    with FakeAddon() as fake:
        fake.json("/charts", CHARTS)
        charts = nt8.nt_charts()
    assert isinstance(charts, list) and charts[0]["id"] == "c1"


def test_nt_chart_state():
    with FakeAddon() as fake:
        fake.json("/chart/first", {"id": "c1", "instrument": "ES 12-26", "period": "5 Minute"})
        state = nt8.nt_chart_state("first")
    assert state["instrument"] == "ES 12-26"


def test_query_params_reach_the_addon():
    # nt_bars/nt_indicators build a query string; empty values must be dropped, not sent as "".
    seen = {}

    def handler(req):
        seen.update(req.query)
        return []

    with FakeAddon() as fake:
        fake.register("/chart/first/indicators", "GET", handler)
        nt8.nt_indicators("first", name="", n=3)
    assert seen == {"n": ["3"]}, seen


def test_nt_output_window_reads_the_window_route_not_the_ring():
    # GET /output is the event RING (Route_Events); the window scrape is GET /output/window. A tool still
    # pointed at /output would silently answer with ring lines and a different shape.
    with FakeAddon() as fake:
        fake.json("/output/window", {"lines": ["[1] scraped"], "source": "window"})
        fake.json("/output", {"lines": ["[1] ring"], "source": "ring"})
        out = nt8.nt_output_window(n=5)
    assert out["source"] == "window", out


def test_nt_chart_reload_posts_to_one_chart():
    seen = {}

    def handler(req):
        seen["path"] = req.path
        return {"ok": True}

    with FakeAddon() as fake:
        fake.register("/chart/c1/reload", "POST", handler)
        assert nt8.nt_chart_reload("c1")["ok"] is True
    assert seen["path"] == "/chart/c1/reload"


def test_addon_error_body_becomes_error_key():
    with FakeAddon() as fake:
        fake.json("/chart/nope", {"error": "no such chart"}, status=404)
        result = nt8.nt_chart_state("nope")
    assert result == {"error": "no such chart"}


def test_addon_unreachable():
    # Nothing listening on that port: every tool degrades to the same {"error": ...}.
    from nt8_mcp import app
    saved = app.BASE_URL
    app.BASE_URL = "http://127.0.0.1:1"
    try:
        result = nt8.nt_health()
    finally:
        app.BASE_URL = saved
    assert result["error"] == app.NOT_REACHABLE


def test_patching_server_reaches_the_owning_module():
    # nt8_mcp.server re-exports; it must be a live view, not a copy. A test that patches
    # nt8.POLL_S the old way has to reach app.POLL_S — the value nt_backtest actually reads —
    # or it silently sleeps 2 s per poll and nothing raises.
    from nt8_mcp import app, tools_local
    saved = (app.POLL_S, tools_local._find_windows)
    try:
        nt8.POLL_S = 0
        assert app.POLL_S == 0 and nt8.POLL_S == 0
        nt8._find_windows = lambda title: [(1, title)]
        assert tools_local._find_windows("x") == [(1, "x")]
    finally:
        app.POLL_S, tools_local._find_windows = saved
    assert nt8.POLL_S == saved[0]
    try:
        nt8.no_such_name
    except AttributeError:
        pass
    else:
        raise AssertionError("an unknown attribute must raise, not resolve")


def test_nt_status():
    with FakeAddon() as fake:
        fake.json("/ntstatus", {"stale": False, "assemblyBuiltUtc": "2026-09-19T00:00:00Z"})
        status = nt8.nt_status()
    assert status["stale"] is False


def test_nt_compat():
    with FakeAddon() as fake:
        fake.json("/compat", {"members": [{"name": "GetUnrealizedProfitLoss", "resolved": True}]})
        compat = nt8.nt_compat()
    assert compat["members"][0]["resolved"] is True


def test_nt_screenshot_returns_image():
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "nt8_fake_shot.png")
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n")
    try:
        with FakeAddon() as fake:
            fake.json("/chart/first/screenshot", {"ok": True, "path": path}, method="POST")
            result = nt8.nt_screenshot("first")
        assert isinstance(result, nt8.Image)
        assert str(result.path) == path
    finally:
        os.remove(path)
