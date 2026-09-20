"""nt8_mcp.analysis (pure breakdown functions) and the nt_analyze tool."""

import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from nt8_mcp import analysis  # noqa: E402
from nt8_mcp import app  # noqa: E402
from nt8_mcp import runs  # noqa: E402
from nt8_mcp import server as nt8  # noqa: E402
from nt8_mcp import tools_analysis  # noqa: E402

# n is chronological (status document v1's own order). Times are exit-time distinct so
# by_month/by_weekday/by_entry_hour all have something to bucket.
TRADES = [
    {"n": 0, "side": "Long", "pnl": 100.0, "entryTime": "2026-01-05T09:00:00", "exitTime": "2026-01-05T10:00:00",
     "mae": 1.0, "mfe": 3.0},
    {"n": 1, "side": "Short", "pnl": -50.0, "entryTime": "2026-01-06T14:00:00", "exitTime": "2026-01-06T15:00:00",
     "mae": 2.0, "mfe": 0.5},
    {"n": 2, "side": "Long", "pnl": -30.0, "entryTime": "2026-02-02T09:00:00", "exitTime": "2026-02-02T09:30:00"},
    {"n": 3, "side": "Long", "pnl": 200.0, "entryTime": "2026-02-03T09:00:00", "exitTime": "2026-02-03T09:45:00"},
]


# -- by_* buckets -------------------------------------------------------------

def test_by_month_uses_exit_time():
    result = analysis.by_month(TRADES)
    assert result["2026-01"]["trades"] == 2 and result["2026-01"]["netProfit"] == 50.0
    assert result["2026-02"]["trades"] == 2 and result["2026-02"]["netProfit"] == 170.0
    assert result["2026-01"]["winners"] == 1 and result["2026-01"]["losers"] == 1


def test_by_weekday_and_entry_hour():
    # 2026-01-05/2026-02-02 are Mondays, 2026-01-06/2026-02-03 are Tuesdays.
    weekday = analysis.by_weekday(TRADES)
    assert weekday["Monday"]["trades"] == 2 and weekday["Tuesday"]["trades"] == 2
    hour = analysis.by_entry_hour(TRADES)
    assert hour[9]["trades"] == 3 and hour[14]["trades"] == 1


def test_by_side():
    result = analysis.by_side(TRADES)
    assert result["Long"]["trades"] == 3 and result["Long"]["netProfit"] == 270.0
    assert result["Short"]["trades"] == 1 and result["Short"]["netProfit"] == -50.0


def test_by_side_skips_a_trade_with_no_entry():
    result = analysis.by_side([{"n": 0, "side": "", "pnl": 10.0}])
    assert result == {}


def test_buckets_skip_trades_missing_the_needed_field_instead_of_raising():
    partial = [{"n": 0, "pnl": 10.0}]  # no entryTime/exitTime/side at all
    assert analysis.by_month(partial) == {}
    assert analysis.by_entry_hour(partial) == {}
    assert analysis.by_side(partial) == {}


# -- MAE/MFE and streaks -------------------------------------------------------

def test_mae_mfe_summary():
    result = analysis.mae_mfe_summary(TRADES)
    assert result["trades"] == 2
    assert result["avgMae"] == 1.5 and result["maxMae"] == 2.0
    assert result["avgMfe"] == 1.75 and result["maxMfe"] == 3.0


def test_mae_mfe_summary_is_none_when_no_trade_carries_it():
    assert analysis.mae_mfe_summary([{"n": 0, "pnl": 1.0}]) is None


def test_streaks():
    trades = [{"pnl": p} for p in (10, 20, -5, -5, -5, 10, 0, 10)]
    result = analysis.streaks(trades)
    assert result["longestWinStreak"] == 2 and result["longestLossStreak"] == 3
    assert result["currentStreak"] == 1 and result["currentStreakKind"] == "win"


def test_streaks_empty_and_all_flat():
    assert analysis.streaks([]) == {"longestWinStreak": 0, "longestLossStreak": 0,
                                    "currentStreak": 0, "currentStreakKind": None}
    assert analysis.streaks([{"pnl": 0.0}, {"pnl": None}])["longestWinStreak"] == 0


# -- drawdown / time under water / flat period --------------------------------

# A simple curve, by exit time: 100 -> 150 (peak) -> 50 (trough, dd=-100) -> 120 -> 200 (new high, recovered)
EQUITY = [
    {"time": "2026-01-01T00:00:00", "cumulativeNetProfit": 100.0},
    {"time": "2026-01-02T00:00:00", "cumulativeNetProfit": 150.0},
    {"time": "2026-01-03T00:00:00", "cumulativeNetProfit": 50.0},
    {"time": "2026-01-04T00:00:00", "cumulativeNetProfit": 120.0},
    {"time": "2026-01-05T00:00:00", "cumulativeNetProfit": 200.0},
]


def test_drawdown_finds_the_worst_period_and_its_recovery():
    d = analysis.drawdown([], EQUITY)
    assert d["amount"] == -100.0
    assert d["start"] == "2026-01-02T00:00:00" and d["trough"] == "2026-01-03T00:00:00"
    assert d["recovered"] == "2026-01-05T00:00:00"
    assert d["durationSeconds"] == 3 * 86400


def test_drawdown_unrecovered_at_series_end():
    equity = EQUITY[:3]  # ends at the trough, never climbs back
    d = analysis.drawdown([], equity)
    assert d["amount"] == -100.0 and d["recovered"] is None and d["durationSeconds"] is None


def test_drawdown_none_when_curve_never_dips():
    equity = [{"time": "2026-01-01T00:00:00", "cumulativeNetProfit": v} for v in (0, 10, 20, 30)]
    assert analysis.drawdown([], equity) is None


def test_drawdown_derives_equity_from_trades_when_none_given():
    trades = [{"pnl": 100.0, "exitTime": "2026-01-01T00:00:00"},
              {"pnl": 50.0, "exitTime": "2026-01-02T00:00:00"},
              {"pnl": -100.0, "exitTime": "2026-01-03T00:00:00"}]
    d = analysis.drawdown(trades)
    assert d["amount"] == -100.0 and d["start"] == "2026-01-02T00:00:00"


def test_drawdown_none_with_fewer_than_two_points():
    assert analysis.drawdown([], []) is None
    assert analysis.drawdown([{"pnl": 1.0, "exitTime": "2026-01-01T00:00:00"}]) is None


def test_time_under_water():
    result = analysis.time_under_water([], EQUITY)
    # underwater from the 01-02 peak until back at/above 150 — that happens at 01-05 (200)
    assert result["totalSeconds"] == 3 * 86400
    assert result["longestSeconds"] == 3 * 86400


def test_longest_flat_period_counts_stalling_at_the_peak_too():
    # peak at 01-02 (150), no NEW high until 01-05 (200) — flat for all of that, even the
    # bar sitting exactly at the old peak, which time_under_water would not count.
    equity = [
        {"time": "2026-01-01T00:00:00", "cumulativeNetProfit": 100.0},
        {"time": "2026-01-02T00:00:00", "cumulativeNetProfit": 150.0},
        {"time": "2026-01-03T00:00:00", "cumulativeNetProfit": 150.0},  # ties the peak, not a new high
        {"time": "2026-01-05T00:00:00", "cumulativeNetProfit": 200.0},
    ]
    result = analysis.longest_flat_period([], equity)
    assert result["start"] == "2026-01-02T00:00:00" and result["end"] == "2026-01-05T00:00:00"
    assert result["seconds"] == 3 * 86400


# -- nt_analyze -----------------------------------------------------------------

def test_nt_analyze_with_a_trade_list():
    result = nt8.nt_analyze(trades=TRADES)
    assert result["runId"] is None and result["trades"] == 4
    assert result["byMonth"]["2026-01"]["trades"] == 2
    assert result["maeMfe"]["trades"] == 2
    assert "longestWinStreak" in result["streaks"]


def test_nt_analyze_needs_run_id_or_trades():
    assert "pass run_id or trades" in nt8.nt_analyze()["error"]


def test_nt_analyze_rejects_a_non_list_trades():
    assert "must be a list" in nt8.nt_analyze(trades={"not": "a list"})["error"]


def test_nt_analyze_reads_a_saved_run():
    with tempfile.TemporaryDirectory() as tmp:
        saved = tools_analysis.NT_HOME
        tools_analysis.NT_HOME = tmp
        try:
            run_dir = os.path.join(tmp, "nt8mcp", "runs")
            os.makedirs(run_dir, exist_ok=True)
            with open(os.path.join(run_dir, "r1.json"), "w", encoding="utf-8") as fh:
                json.dump({"trades": TRADES, "equity": EQUITY, "summary": {"netProfit": 220.0},
                          "inputs": {"Fast": 5}}, fh)
            result = nt8.nt_analyze(run_id="r1")
        finally:
            tools_analysis.NT_HOME = saved
    assert result["runId"] == "r1" and result["trades"] == 4
    assert result["drawdown"] is not None


def test_nt_analyze_reads_a_run_saved_through_the_real_save_run():
    # Regression: runs.save_run() must persist "trades", since _load_run()/nt_analyze()
    # require exactly that key — a hand-written run-file fixture would not catch a save_run()
    # that silently dropped it.
    with tempfile.TemporaryDirectory() as tmp:
        saved_app, saved_tools = app.NT_HOME, tools_analysis.NT_HOME
        app.NT_HOME = tmp
        tools_analysis.NT_HOME = tmp
        try:
            result = {"id": "b7", "state": "done", "strategy": "SampleMACrossOver",
                      "instrument": "ES 12-26", "period": "5 Minute", "trades": TRADES,
                      "equity": EQUITY, "summary": {"trades": 4, "netProfit": 220.0}}
            saved = runs.save_run({"strategy": "SampleMACrossOver"}, result)
            assert "id" in saved, saved
            analyzed = nt8.nt_analyze(run_id=saved["id"])
        finally:
            app.NT_HOME = saved_app
            tools_analysis.NT_HOME = saved_tools
    assert analyzed.get("error") is None, analyzed
    assert analyzed["trades"] == 4
    assert analyzed["byMonth"]["2026-01"]["trades"] == 2
    assert analyzed["drawdown"] is not None


def test_nt_analyze_reports_a_missing_run():
    result = nt8.nt_analyze(run_id="no-such-run")
    assert "no saved run 'no-such-run'" in result["error"]


def test_nt_analyze_run_id_cannot_walk_out_of_the_runs_folder():
    result = nt8.nt_analyze(run_id="../../secrets")
    assert "no saved run" in result["error"]
    assert ".." not in result["error"].split("(")[-1]


def test_nt_analyze_reports_a_non_object_run_file():
    with tempfile.TemporaryDirectory() as tmp:
        saved = tools_analysis.NT_HOME
        tools_analysis.NT_HOME = tmp
        try:
            run_dir = os.path.join(tmp, "nt8mcp", "runs")
            os.makedirs(run_dir, exist_ok=True)
            with open(os.path.join(run_dir, "r2.json"), "w", encoding="utf-8") as fh:
                json.dump([1, 2, 3], fh)
            result = nt8.nt_analyze(run_id="r2")
        finally:
            tools_analysis.NT_HOME = saved
    assert "not a JSON object" in result["error"]
