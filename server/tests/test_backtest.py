"""Backtest tools (nt8_mcp.tools_backtest) against the canned AddOn in fake_addon.py."""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import app, server as nt8  # noqa: E402

STRATEGIES = [
    {"name": "SampleMACrossOver", "fullName": "NinjaTrader.NinjaScript.Strategies.SampleMACrossOver",
     "inputs": {"NearTerm": {"type": "Int32", "default": 3}}},
]


def _backtest_fake(fake: FakeAddon, polls_until_done: int = 2):
    """POST /backtest queues b1; GET /backtest/b1 answers running until the Nth poll, then done."""
    fake.json("/backtest", {"id": "b1", "state": "queued"}, method="POST", status=202)
    fake.register("/backtest/b1", "GET", lambda req: (
        {"id": "b1", "state": "running"} if req.n < polls_until_done
        else {"id": "b1", "state": "done", "summary": {"trades": 1}, "trades": [{"n": 1, "pnl": 100.0}]}
    ))
    return fake


def test_nt_strategies():
    with FakeAddon() as fake:
        fake.json("/strategies", STRATEGIES)
        strategies = nt8.nt_strategies()
    assert isinstance(strategies, list) and strategies[0]["name"] == "SampleMACrossOver"


def test_nt_backtest_polls_to_done():
    saved = app.POLL_S
    app.POLL_S = 0  # don't actually wait between polls in the test
    try:
        with FakeAddon() as fake:
            _backtest_fake(fake)
            result = nt8.nt_backtest("SampleMACrossOver", chart="first", from_date="2026-09-15", to_date="2026-09-17")
    finally:
        app.POLL_S = saved
    assert result["state"] == "done", result
    assert result["summary"]["trades"] == 1


def test_nt_backtest_passes_the_loaded_window_and_warnings_through():
    # barsFrom/barsTo/warnings are the AddOn's word on what REALLY ran; the tool must not drop them.
    saved = app.POLL_S
    app.POLL_S = 0
    try:
        with FakeAddon() as fake:
            fake.json("/backtest", {"id": "b1", "state": "queued"}, method="POST", status=202)
            fake.json("/backtest/b1", {"id": "b1", "state": "done", "from": "2026-09-14T00:00:00",
                                       "to": "2026-09-15T23:59:59", "summary": {"trades": 14},
                                       "barsFrom": "2026-09-08T19:50:00", "barsTo": "2026-09-10T00:00:00",
                                       "warnings": ["the numbers in this document are for ..."]})
            result = nt8.nt_backtest("SampleMACrossOver", from_date="2026-09-14", to_date="2026-09-15")
    finally:
        app.POLL_S = saved
    assert result["barsTo"] == "2026-09-10T00:00:00" and result["barsFrom"] and len(result["warnings"]) == 1, result


def test_nt_backtest_still_running_note():
    # wait_s exhausted while the job is still running: the last status doc comes back with a note
    # naming the id, never an exception and never a fabricated "done".
    saved = app.POLL_S
    app.POLL_S = 0
    try:
        with FakeAddon() as fake:
            _backtest_fake(fake, polls_until_done=10_000)
            result = nt8.nt_backtest("SampleMACrossOver", wait_s=0)
    finally:
        app.POLL_S = saved
    assert result["state"] in ("queued", "running"), result
    assert "b1" in result["note"], result


def test_nt_backtest_post_error_short_circuits():
    with FakeAddon() as fake:
        fake.json("/backtest", {"error": "unknown input NotARealInput_xyz"}, method="POST", status=400)
        result = nt8.nt_backtest("SampleMACrossOver")
    assert "error" in result and "state" not in result


def test_nt_backtest_body_defaults():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.body))
        return (202, {"id": "b1", "state": "done", "summary": {"trades": 0}})

    with FakeAddon() as fake:
        fake.register("/backtest", "POST", handler)
        nt8.nt_backtest("SampleMACrossOver")
    # dates default to a 2-day window and tickReplay defaults to True (OnMarketData strategies)
    assert seen["strategy"] == "SampleMACrossOver"
    assert seen["tickReplay"] is True
    assert len(seen["from"]) == 10 and len(seen["to"]) == 10, seen
    assert "instrument" not in seen and "inputs" not in seen, seen


def test_nt_backtest_cancel():
    with FakeAddon() as fake:
        fake.json("/backtest/b1", {"ok": True}, method="DELETE")
        result = nt8.nt_backtest_cancel("b1")
    assert result["ok"] is True


def test_nt_backtests():
    with FakeAddon() as fake:
        fake.json("/backtests", [{"id": "b1", "state": "done"}])
        result = nt8.nt_backtests()
    assert result[0]["id"] == "b1"


# ── settings / templates ────────────────────────────────────────────────────

def _capture_body(fake: FakeAddon) -> dict:
    """Register POST /backtest and hand back the dict the tool sent."""
    seen = {}

    def handler(req):
        seen.update(json.loads(req.body))
        return (202, {"id": "b1", "state": "done", "summary": {"trades": 0}, "settings": {}})

    fake.register("/backtest", "POST", handler)
    return seen


def test_nt_templates():
    with FakeAddon() as fake:
        fake.json("/templates", {"strategy": "SampleMACrossOver", "folder": "C:\\t", "templates": ["Base", "ES-5m"]})
        result = nt8.nt_templates("SampleMACrossOver")
    assert result["templates"] == ["Base", "ES-5m"]


def test_nt_templates_quotes_the_strategy_name():
    seen = {}

    with FakeAddon() as fake:
        fake.register("/templates", "GET", lambda req: seen.update(req.query) or {"templates": []})
        nt8.nt_templates("NinjaTrader.NinjaScript.Strategies.Foo Bar")
    # a space (and any other reserved character) must survive the query string intact
    assert seen["strategy"] == ["NinjaTrader.NinjaScript.Strategies.Foo Bar"], seen


def test_nt_backtest_settings_are_sent_only_when_asked_for():
    with FakeAddon() as fake:
        seen = _capture_body(fake)
        nt8.nt_backtest("SampleMACrossOver", slippage_ticks=2.0, fill_resolution="High",
                        fill_resolution_type="Tick", fill_resolution_value=1,
                        commission_template="ES-RT", include_commission=True,
                        fill_limit_on_touch=True, include_trade_history=False,
                        max_trades=50, tick_replay=False)
    assert seen["slippageTicks"] == 2.0
    assert seen["fillResolution"] == "High" and seen["fillResolutionType"] == "Tick"
    assert seen["fillResolutionValue"] == 1 and seen["maxTrades"] == 50
    assert seen["commissionTemplate"] == "ES-RT"
    assert seen["includeCommission"] is True and seen["fillLimitOnTouch"] is True
    assert seen["includeTradeHistory"] is False


def test_nt_backtest_omits_unasked_settings():
    # a settings key the caller did not pass must not appear at all: the AddOn keeps NinjaTrader's own
    # default for an absent key, and sending 0/"" would silently overwrite it.
    with FakeAddon() as fake:
        seen = _capture_body(fake)
        nt8.nt_backtest("SampleMACrossOver")
    for key in ("template", "fillResolution", "fillResolutionType", "fillResolutionValue",
                "slippageTicks", "commissionTemplate", "includeCommission",
                "fillLimitOnTouch", "includeTradeHistory", "maxTrades"):
        assert key not in seen, key


def test_nt_backtest_slippage_zero_is_still_sent():
    # 0.0 is falsy and is also the only way to ask for "no slippage" explicitly -- the None sentinel
    # is what keeps those two apart. Same for include_* False.
    with FakeAddon() as fake:
        seen = _capture_body(fake)
        nt8.nt_backtest("SampleMACrossOver", slippage_ticks=0.0, include_commission=False)
    assert seen["slippageTicks"] == 0.0 and seen["includeCommission"] is False


def test_nt_backtest_template_does_not_invent_dates_or_chart():
    with FakeAddon() as fake:
        seen = _capture_body(fake)
        nt8.nt_backtest("SampleMACrossOver", template="ES-5m")
    assert seen["template"] == "ES-5m"
    # the template carries its own dates/instrument/period; defaulting them here would silently win
    assert "from" not in seen and "to" not in seen, seen
    assert "chart" not in seen, seen


def test_nt_backtest_template_explicit_args_still_win():
    with FakeAddon() as fake:
        seen = _capture_body(fake)
        nt8.nt_backtest("SampleMACrossOver", template="ES-5m", chart="c2",
                        from_date="2026-09-15", to_date="2026-09-17")
    assert seen["chart"] == "c2" and seen["from"] == "2026-09-15" and seen["to"] == "2026-09-17"


def test_nt_backtest_without_template_still_sends_chart_and_dates():
    with FakeAddon() as fake:
        seen = _capture_body(fake)
        nt8.nt_backtest("SampleMACrossOver")
    assert seen["chart"] == "first"
    assert len(seen["from"]) == 10 and len(seen["to"]) == 10, seen
