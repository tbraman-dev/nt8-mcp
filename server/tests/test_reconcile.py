"""reconcile.py (pure) and tools_reconcile.nt_reconcile (input loading) against synthetic data.
No live NinjaTrader; FakeAddon stands in for the AddOn only where nt_reconcile reads a live
backtest/playback status by id.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import reconcile as rec  # noqa: E402
from nt8_mcp import tools_reconcile  # noqa: E402

# ---------------------------------------------------------------------------
# synthetic fixtures
# ---------------------------------------------------------------------------

# One closed Long, ES-style numbers: 10 points = $500 net -> point value $50, tick 0.25 -> $12.50/tick.
_BT_TRADE_LONG = {
    "n": 0, "side": "Long", "qty": 1, "entryName": "Buy", "exitName": "Close",
    "entryTime": "2026-09-10T09:30:00", "exitTime": "2026-09-10T10:00:00",
    "entryPrice": 5000.0, "exitPrice": 5010.0, "pnl": 500.0, "pnlPoints": 10.0, "mae": 0, "mfe": 10, "bars": 6,
}
_BT_TRADE_SHORT_UNMATCHED = {
    "n": 1, "side": "Short", "qty": 2, "entryName": "SellShort", "exitName": "Close",
    "entryTime": "2026-09-11T09:30:00", "exitTime": "2026-09-11T09:45:00",
    "entryPrice": 5100.0, "exitPrice": 5090.0, "pnl": 1000.0, "pnlPoints": 10.0, "mae": 0, "mfe": 10, "bars": 3,
}
_BT_TRADE_NO_ENTRY = {"n": 2, "side": "", "qty": 0, "entryTime": None, "exitTime": None,
                       "entryPrice": None, "exitPrice": None, "pnl": None, "pnlPoints": None, "bars": None}
_BT_TRADE_ERROR = {"error": "could not read trade 3"}


def _backtest_doc(*trades):
    return {"id": "b1", "state": "done", "trades": list(trades)}


def _exec(account, instrument, side, qty, price, time_):
    return {"id": 1, "executionId": f"{side}-{time_}", "account": account, "instrument": instrument,
            "side": side, "qty": qty, "price": price, "time": time_, "commission": 0, "fee": 0,
            "position": None, "orderId": "o1", "orderName": side}


# matches _BT_TRADE_LONG within 60s, with a 4-tick entry slip and a 2-tick exit slip
_PB_FILLS_LONG = [
    _exec("Playback101", "ES 12-26", "Buy", 1, 5001.0, "2026-09-10T09:30:20"),
    _exec("Playback101", "ES 12-26", "Sell", 1, 5009.5, "2026-09-10T10:00:10"),
]


# ---------------------------------------------------------------------------
# normalize_backtest_trades
# ---------------------------------------------------------------------------

def test_normalize_backtest_trades_drops_errors_and_no_entry_rows():
    doc = _backtest_doc(_BT_TRADE_LONG, _BT_TRADE_NO_ENTRY, _BT_TRADE_ERROR)
    out = rec.normalize_backtest_trades(doc)
    assert len(out) == 1
    assert out[0]["side"] == "Long" and out[0]["entryPrice"] == 5000.0


def test_normalize_backtest_trades_accepts_a_bare_list():
    out = rec.normalize_backtest_trades([_BT_TRADE_LONG])
    assert len(out) == 1 and out[0]["pnl"] == 500.0


def test_normalize_backtest_trades_accepts_a_saved_run_record():
    run_record = {"savedAt": "...", "id": "b1", "trades": [_BT_TRADE_LONG]}
    out = rec.normalize_backtest_trades(run_record)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# normalize_playback_executions
# ---------------------------------------------------------------------------

def test_normalize_playback_executions_flat_list():
    out = rec.normalize_playback_executions(list(_PB_FILLS_LONG))
    assert len(out) == 2 and out[0]["side"] == "Buy"


def test_normalize_playback_executions_run_document_groups_by_account():
    run_doc = {"id": "pr1", "state": "done",
               "executions": [{"account": "Playback101", "executions": list(_PB_FILLS_LONG)}]}
    out = rec.normalize_playback_executions(run_doc)
    assert len(out) == 2


def test_normalize_playback_executions_drops_error_rows():
    out = rec.normalize_playback_executions([{"error": "bad row"}, _PB_FILLS_LONG[0]])
    assert len(out) == 1


# ---------------------------------------------------------------------------
# build_trades_from_executions
# ---------------------------------------------------------------------------

def test_build_trades_simple_round_trip():
    trades = rec.build_trades_from_executions(rec.normalize_playback_executions(list(_PB_FILLS_LONG)))
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "Long" and t["qty"] == 1
    assert t["entryPrice"] == 5001.0 and t["exitPrice"] == 5009.5


def test_build_trades_scale_in_averages_entry_price():
    fills = [
        _exec("Sim101", "ES 12-26", "Buy", 1, 5000.0, "2026-09-10T09:30:00"),
        _exec("Sim101", "ES 12-26", "Buy", 1, 5002.0, "2026-09-10T09:31:00"),   # scale in, avg entry 5001
        _exec("Sim101", "ES 12-26", "Sell", 2, 5010.0, "2026-09-10T09:40:00"),  # closes both
    ]
    trades = rec.build_trades_from_executions(rec.normalize_playback_executions(fills))
    assert len(trades) == 1
    assert trades[0]["qty"] == 2 and trades[0]["entryPrice"] == 5001.0 and trades[0]["exitPrice"] == 5010.0


def test_build_trades_reversal_splits_into_two_trades():
    fills = [
        _exec("Sim101", "ES 12-26", "Buy", 2, 100.0, "2026-09-10T09:30:00"),    # open Long 2
        _exec("Sim101", "ES 12-26", "Sell", 3, 101.0, "2026-09-10T09:31:00"),   # close Long 2, open Short 1
        _exec("Sim101", "ES 12-26", "Buy", 1, 99.0, "2026-09-10T09:32:00"),     # close Short 1
    ]
    trades = rec.build_trades_from_executions(rec.normalize_playback_executions(fills))
    assert len(trades) == 2, trades
    first, second = trades
    assert first["side"] == "Long" and first["qty"] == 2 and first["entryPrice"] == 100.0 and first["exitPrice"] == 101.0
    assert second["side"] == "Short" and second["qty"] == 1 and second["entryPrice"] == 101.0 and second["exitPrice"] == 99.0


def test_build_trades_drops_a_lot_never_closed():
    fills = [_exec("Sim101", "ES 12-26", "Buy", 1, 100.0, "2026-09-10T09:30:00")]  # never exits
    trades = rec.build_trades_from_executions(rec.normalize_playback_executions(fills))
    assert trades == []


def test_build_trades_groups_by_account_and_instrument_separately():
    fills = [
        _exec("Sim101", "ES 12-26", "Buy", 1, 100.0, "2026-09-10T09:30:00"),
        _exec("Sim101", "ES 12-26", "Sell", 1, 101.0, "2026-09-10T09:31:00"),
        _exec("Playback101", "ES 12-26", "Buy", 1, 200.0, "2026-09-10T09:30:00"),
        _exec("Playback101", "ES 12-26", "Sell", 1, 205.0, "2026-09-10T09:31:00"),
    ]
    trades = rec.build_trades_from_executions(rec.normalize_playback_executions(fills))
    assert len(trades) == 2
    assert {t["account"] for t in trades} == {"Sim101", "Playback101"}


# ---------------------------------------------------------------------------
# match_trades
# ---------------------------------------------------------------------------

def test_match_trades_within_tolerance():
    bt = rec.normalize_backtest_trades([_BT_TRADE_LONG])
    pt = rec.build_trades_from_executions(rec.normalize_playback_executions(list(_PB_FILLS_LONG)))
    pairs, bt_only, pt_only = rec.match_trades(bt, pt, tolerance_seconds=60)
    assert len(pairs) == 1 and bt_only == [] and pt_only == []
    assert pairs[0][2] == 20.0  # entry time delta in seconds


def test_match_trades_outside_tolerance_is_unmatched():
    bt = rec.normalize_backtest_trades([_BT_TRADE_LONG])
    pt = rec.build_trades_from_executions(rec.normalize_playback_executions(list(_PB_FILLS_LONG)))
    pairs, bt_only, pt_only = rec.match_trades(bt, pt, tolerance_seconds=5)  # the 20s gap is too wide now
    assert pairs == [] and len(bt_only) == 1 and len(pt_only) == 1


def test_match_trades_side_mismatch_never_matches():
    bt = rec.normalize_backtest_trades([_BT_TRADE_LONG])
    short_fills = [
        _exec("Playback101", "ES 12-26", "SellShort", 1, 5000.0, "2026-09-10T09:30:00"),
        _exec("Playback101", "ES 12-26", "BuyToCover", 1, 4995.0, "2026-09-10T09:35:00"),
    ]
    # SellShort/BuyToCover aren't Buy/Sell, so build_trades_from_executions treats them as no-direction
    # fills (dropped) -- confirm a Short trade built with plain Sell-then-Buy also never matches a Long.
    pt = rec.build_trades_from_executions(rec.normalize_playback_executions([
        _exec("Playback101", "ES 12-26", "Sell", 1, 5000.0, "2026-09-10T09:30:00"),
        _exec("Playback101", "ES 12-26", "Buy", 1, 4995.0, "2026-09-10T09:35:00"),
    ]))
    pairs, bt_only, pt_only = rec.match_trades(bt, pt, tolerance_seconds=3600)
    assert pairs == [] and len(bt_only) == 1 and len(pt_only) == 1
    assert not short_fills or True  # short_fills defined only to document the dropped-direction case


# ---------------------------------------------------------------------------
# reconcile() end to end
# ---------------------------------------------------------------------------

def test_reconcile_without_tick_size_reports_price_deltas_only():
    out = rec.reconcile(_backtest_doc(_BT_TRADE_LONG), list(_PB_FILLS_LONG))
    assert out["counts"] == {"backtestTrades": 1, "playbackTrades": 1, "matched": 1, "backtestOnly": 0, "playbackOnly": 0}
    row = out["matched"][0]
    assert row["entryPriceDelta"] == 1.0 and row["exitPriceDelta"] == -0.5
    assert row["entryTicksDelta"] is None and row["withinTolerance"] is None
    assert out["slippage"]["ticksAvailable"] is False
    assert out["slippage"]["totalTicks"] is None and out["slippage"]["totalCurrency"] is None
    assert "all 1 trade matched" in out["verdict"]


def test_reconcile_with_tick_size_computes_ticks_and_currency():
    out = rec.reconcile(_backtest_doc(_BT_TRADE_LONG), list(_PB_FILLS_LONG), tolerance_ticks=1, tick_size=0.25)
    row = out["matched"][0]
    assert row["entryTicksDelta"] == 4.0   # 1.00 / 0.25
    assert row["exitTicksDelta"] == -2.0   # -0.50 / 0.25
    assert row["withinTolerance"] is False  # 4 ticks > tolerance of 1
    slip = out["slippage"]
    assert slip["ticksAvailable"] is True
    assert slip["pointValue"] == 50.0       # 500 pnl / (10 pnlPoints * 1 qty)
    assert slip["totalTicks"] == 6.0        # |4| + |-2|
    assert slip["totalCurrency"] == 75.0    # 6 ticks * 0.25 * 50 * qty 1
    assert "outside the 1-tick tolerance" in out["verdict"]


def test_reconcile_reports_backtest_only_and_playback_only_in_the_verdict():
    out = rec.reconcile(_backtest_doc(_BT_TRADE_LONG, _BT_TRADE_SHORT_UNMATCHED), list(_PB_FILLS_LONG))
    assert out["counts"]["backtestOnly"] == 1 and out["counts"]["matched"] == 1
    assert "the backtest filled 1 trade the replay did not" in out["verdict"]


def test_reconcile_playback_only_when_replay_has_an_extra_fill_pair():
    extra = list(_PB_FILLS_LONG) + [
        _exec("Playback101", "ES 12-26", "Buy", 1, 5200.0, "2026-09-12T09:30:00"),
        _exec("Playback101", "ES 12-26", "Sell", 1, 5205.0, "2026-09-12T09:31:00"),
    ]
    out = rec.reconcile(_backtest_doc(_BT_TRADE_LONG), extra)
    assert out["counts"]["playbackOnly"] == 1
    assert "the replay filled 1 trade the backtest did not" in out["verdict"]


def test_reconcile_neither_side_has_trades():
    out = rec.reconcile(_backtest_doc(), [])
    assert out["counts"] == {"backtestTrades": 0, "playbackTrades": 0, "matched": 0, "backtestOnly": 0, "playbackOnly": 0}
    assert "neither" in out["verdict"]


def test_point_value_uses_the_median_of_computable_trades():
    trades = rec.normalize_backtest_trades([_BT_TRADE_LONG, _BT_TRADE_SHORT_UNMATCHED])
    # LONG: 500/(10*1)=50.0  SHORT: 1000/(10*2)=50.0 -> median 50.0
    assert rec._point_value(trades) == 50.0


def test_point_value_is_none_when_nothing_is_computable():
    trades = rec.normalize_backtest_trades([_BT_TRADE_NO_ENTRY])
    assert rec._point_value([]) is None
    assert trades == []  # the no-entry row is dropped by normalize itself


# ---------------------------------------------------------------------------
# tools_reconcile.nt_reconcile — input loading
# ---------------------------------------------------------------------------

def test_nt_reconcile_with_plain_lists_needs_no_addon():
    out = tools_reconcile.nt_reconcile(
        backtest_trades=[_BT_TRADE_LONG],
        playback_executions=list(_PB_FILLS_LONG),
        tick_size=0.25,
    )
    assert out["counts"]["matched"] == 1
    assert out["slippage"]["totalCurrency"] == 75.0


def test_nt_reconcile_rejects_zero_or_two_backtest_sources():
    assert "error" in tools_reconcile.nt_reconcile(playback_executions=[])
    assert "error" in tools_reconcile.nt_reconcile(
        backtest_id="b1", backtest_trades=[], playback_executions=[])


def test_nt_reconcile_rejects_zero_or_two_playback_sources():
    assert "error" in tools_reconcile.nt_reconcile(backtest_trades=[])
    assert "error" in tools_reconcile.nt_reconcile(
        backtest_trades=[], playback_run_id="pr1", playback_executions=[])


def test_nt_reconcile_loads_a_live_backtest_and_playback_run_by_id():
    with FakeAddon() as fake:
        fake.json("/backtest/b1", _backtest_doc(_BT_TRADE_LONG))
        fake.json("/playback/run/pr1", {
            "id": "pr1", "state": "done",
            "executions": [{"account": "Playback101", "executions": list(_PB_FILLS_LONG)}],
        })
        out = tools_reconcile.nt_reconcile(backtest_id="b1", playback_run_id="pr1")
    assert out["counts"]["matched"] == 1


def test_nt_reconcile_refuses_an_unfinished_backtest():
    with FakeAddon() as fake:
        fake.json("/backtest/b2", {"id": "b2", "state": "running", "trades": None})
        out = tools_reconcile.nt_reconcile(backtest_id="b2", playback_executions=[])
    assert "error" in out and "b2" in out["error"]


def test_nt_reconcile_refuses_an_unfinished_playback_run():
    with FakeAddon() as fake:
        fake.json("/playback/run/pr2", {"id": "pr2", "state": "queued"})
        out = tools_reconcile.nt_reconcile(backtest_trades=[], playback_run_id="pr2")
    assert "error" in out and "pr2" in out["error"]


def test_nt_reconcile_passes_through_a_backtest_status_error():
    with FakeAddon() as fake:
        fake.json("/backtest/nope", {"error": "no backtest 'nope'"}, status=404)
        out = tools_reconcile.nt_reconcile(backtest_id="nope", playback_executions=[])
    assert out == {"error": "no backtest 'nope'"}
