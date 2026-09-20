"""Chart control tools (nt8_mcp.tools_chartcontrol) against the canned AddOn in fake_addon.py.
Contract: ../../docs/api/chartcontrol.md.
"""

import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import app, runs, server as nt8  # noqa: E402

REPORT = {
    "instrument": "ES 12-26", "period": "5 Minute",
    "firstVisibleTime": "2026-09-17T09:30:00", "lastVisibleTime": "2026-09-17T16:00:00",
    "indicators": [{"name": "SMA", "displayName": "SMA(20)", "panel": 0, "inputs": {"Period": 20}, "plots": [], "drawings": 0}],
}


class _TempNtHome:
    """Points app.NT_HOME at a scratch directory, same as test_runs.py — needed for nt_trade_shot's
    saved-run lookup, which reads through runs.save_run() / tools_runs._find()."""

    def __enter__(self):
        self._saved = app.NT_HOME
        self._dir = tempfile.mkdtemp(prefix="nt8chartctl_")
        app.NT_HOME = self._dir
        return self._dir

    def __exit__(self, *exc):
        app.NT_HOME = self._saved
        shutil.rmtree(self._dir, ignore_errors=True)
        return False


def _seen_body(fake, path, method, response):
    """Register path/method and return a dict that captures the last request's parsed JSON body."""
    seen = {}

    def handler(req):
        seen.clear()
        seen.update(json.loads(req.body) if req.body else {})
        return response

    fake.register(path, method, handler)
    return seen


# ── nt_chart_indicator_add ───────────────────────────────────────────────────

def test_nt_chart_indicator_add_sends_indicator_inputs_and_panel():
    with FakeAddon() as fake:
        seen = _seen_body(fake, "/chart/first/indicator/add", "POST", REPORT)
        out = nt8.nt_chart_indicator_add("SMA", inputs={"Period": 20}, panel=0)
    assert seen == {"indicator": "SMA", "inputs": {"Period": 20}, "panel": 0}, seen
    assert out == REPORT


def test_nt_chart_indicator_add_omits_optional_fields_and_uses_chart_id():
    with FakeAddon() as fake:
        seen = _seen_body(fake, "/chart/c2/indicator/add", "POST", REPORT)
        nt8.nt_chart_indicator_add("EMA", chart="c2")
    assert seen == {"indicator": "EMA"}, seen  # no "inputs" / "panel" key when not given


def test_nt_chart_indicator_add_error_passthrough():
    with FakeAddon() as fake:
        fake.json("/chart/first/indicator/add", {"error": "unknown indicator 'NotReal'"}, method="POST", status=400)
        out = nt8.nt_chart_indicator_add("NotReal")
    assert out == {"error": "unknown indicator 'NotReal'"}


# ── nt_chart_indicator_remove ────────────────────────────────────────────────

def test_nt_chart_indicator_remove_by_name():
    with FakeAddon() as fake:
        seen = _seen_body(fake, "/chart/first/indicator/remove", "POST", REPORT)
        nt8.nt_chart_indicator_remove(indicator="SMA")
    assert seen == {"force": False, "indicator": "SMA"}, seen


def test_nt_chart_indicator_remove_by_index_with_force():
    with FakeAddon() as fake:
        seen = _seen_body(fake, "/chart/first/indicator/remove", "POST", REPORT)
        nt8.nt_chart_indicator_remove(index=2, force=True)
    assert seen == {"force": True, "index": 2}, seen


def test_nt_chart_indicator_remove_requires_indicator_or_index():
    # Neither given: refuse locally, never hit the AddOn.
    out = nt8.nt_chart_indicator_remove()
    assert "error" in out


# ── nt_chart_set_series ──────────────────────────────────────────────────────

def test_nt_chart_set_series_instrument_and_period():
    with FakeAddon() as fake:
        seen = _seen_body(fake, "/chart/first/series", "POST", REPORT)
        nt8.nt_chart_set_series(instrument="ES 12-26", bars_period={"type": "Minute", "value": 15})
    assert seen == {"instrument": "ES 12-26", "barsPeriod": {"type": "Minute", "value": 15}}, seen


def test_nt_chart_set_series_requires_something_to_change():
    out = nt8.nt_chart_set_series()
    assert "error" in out


def test_nt_chart_set_series_refused_with_enabled_strategy():
    with FakeAddon() as fake:
        fake.json("/chart/first/series", {"error": "refused: this chart has an enabled strategy attached — stop it first"},
                  method="POST", status=400)
        out = nt8.nt_chart_set_series(instrument="ES 12-26")
    assert "enabled strategy" in out["error"]


# ── nt_chart_scroll_to ────────────────────────────────────────────────────────

def test_nt_chart_scroll_to_sends_time():
    with FakeAddon() as fake:
        seen = _seen_body(fake, "/chart/first/scroll", "POST", REPORT)
        out = nt8.nt_chart_scroll_to("2026-09-17T14:30:00")
    assert seen == {"time": "2026-09-17T14:30:00"}, seen
    assert out == REPORT


# ── nt_trade_shot ─────────────────────────────────────────────────────────────

_TRADES = [
    {"n": 0, "side": "Long", "entryTime": "2026-09-17T10:05:00", "exitTime": "2026-09-17T10:20:00", "pnl": 100.0},
    {"n": 1, "side": "Short", "entryTime": None, "exitTime": None, "pnl": None},  # no entry
]


def test_nt_trade_shot_from_a_saved_run_scrolls_then_shoots():
    shot_path = os.path.join(tempfile.gettempdir(), "nt8_trade_shot_1.png")
    with _TempNtHome():
        saved = runs.save_run(
            {"strategy": "SampleMACrossOver"},
            {"id": "b1", "state": "done", "trades": _TRADES, "summary": {"trades": 2}},
        )
        with FakeAddon() as fake:
            scroll_seen = _seen_body(fake, "/chart/first/scroll", "POST", REPORT)
            fake.json("/chart/first/screenshot", {"ok": True, "path": shot_path, "width": 10, "height": 10}, method="POST")
            result = nt8.nt_trade_shot(saved["id"], 0)
    assert scroll_seen == {"time": "2026-09-17T10:05:00"}, scroll_seen
    assert str(result.path) == shot_path


def test_nt_trade_shot_falls_back_to_a_live_backtest_id_when_no_saved_run_matches():
    shot_path = os.path.join(tempfile.gettempdir(), "nt8_trade_shot_2.png")
    with _TempNtHome():
        with FakeAddon() as fake:
            fake.json("/backtest/b7", {"id": "b7", "state": "done", "trades": _TRADES})
            scroll_seen = _seen_body(fake, "/chart/first/scroll", "POST", REPORT)
            fake.json("/chart/first/screenshot", {"ok": True, "path": shot_path}, method="POST")
            result = nt8.nt_trade_shot("b7", 0)
    assert scroll_seen == {"time": "2026-09-17T10:05:00"}, scroll_seen
    assert str(result.path) == shot_path


def test_nt_trade_shot_unknown_run_is_an_error_never_a_screenshot_of_the_wrong_place():
    with _TempNtHome():
        with FakeAddon() as fake:
            fake.json("/backtest/nope", {"error": "no backtest 'nope'"}, status=404)
            result = nt8.nt_trade_shot("nope", 0)
    assert "error" in result and "nope" in result["error"]


def test_nt_trade_shot_trade_index_out_of_range():
    with _TempNtHome():
        saved = runs.save_run({"strategy": "S"}, {"id": "b1", "state": "done", "trades": _TRADES})
        with FakeAddon():
            result = nt8.nt_trade_shot(saved["id"], 99)
    assert "error" in result and "out of range" in result["error"]


def test_nt_trade_shot_trade_with_no_entry_time_is_an_error():
    with _TempNtHome():
        saved = runs.save_run({"strategy": "S"}, {"id": "b1", "state": "done", "trades": _TRADES})
        with FakeAddon():
            result = nt8.nt_trade_shot(saved["id"], 1)  # _TRADES[1] has entryTime None
    assert "error" in result and "entryTime" in result["error"]


def test_nt_trade_shot_stops_at_a_scroll_error_and_never_screenshots():
    with _TempNtHome():
        saved = runs.save_run({"strategy": "S"}, {"id": "b1", "state": "done", "trades": _TRADES})
        with FakeAddon() as fake:
            fake.json("/chart/first/scroll", {"error": "refused: a modal dialog is open (Foo)"}, method="POST", status=409)
            shot_called = {"n": 0}

            def shot_handler(req):
                shot_called["n"] += 1
                return {"ok": True, "path": "/tmp/should-not-happen.png"}
            fake.register("/chart/first/screenshot", "POST", shot_handler)

            result = nt8.nt_trade_shot(saved["id"], 0)
    assert "error" in result, result
    assert shot_called["n"] == 0  # never reached the screenshot call


# ── tool registration ─────────────────────────────────────────────────────────

def test_tool_names_are_registered_once():
    names = [t.name for t in nt8.mcp._tool_manager.list_tools()]
    for name in ("nt_chart_indicator_add", "nt_chart_indicator_remove", "nt_chart_set_series",
                 "nt_chart_scroll_to", "nt_trade_shot"):
        assert names.count(name) == 1, (name, names.count(name))
    assert len(names) == len(set(names)), sorted(names)
