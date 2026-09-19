"""nt_optimize / nt_walkforward (nt8_mcp.tools_optimize) against the canned AddOn."""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import server as nt8  # noqa: E402
from nt8_mcp.tools_optimize import parse_params  # noqa: E402

SUMMARY = {"trades": 10, "winners": 6, "losers": 4, "winRate": 0.6, "netProfit": 0.0,
           "grossProfit": 100.0, "grossLoss": -100.0, "profitFactor": 1.0, "commission": 0.0,
           "maxDrawdown": -50.0, "avgTrade": 0.0, "avgWinner": 20.0, "avgLoser": -10.0,
           "largestWinner": 30.0, "largestLoser": -20.0, "avgMae": 1.0, "avgMfe": 2.0,
           "avgBarsInTrade": 5.0, "sharpe": 0.1, "maxConsecWinners": 2, "maxConsecLosers": 2}


def _doc(job_id, net, trades=10, n_trades=2, **over):
    """A status document v1 whose netProfit is `net`."""
    summary = dict(SUMMARY, netProfit=net, trades=trades)
    patch = over.pop("summary", {})
    if patch is None:
        summary = None          # a run that never produced one (state != "done")
    else:
        summary.update(patch)
    return dict({"id": job_id, "state": "done", "strategy": "SampleMACrossOver",
                 "instrument": "ES 12-26", "period": "5 Minute", "error": None, "inputs": {},
                 "summary": summary,
                 "trades": [{"n": i, "side": "Long", "qty": 1, "pnl": net / max(n_trades, 1)}
                            for i in range(n_trades)],
                 "output": []}, **over)


def _grid_fake(fake, net_of, seen=None):
    """POST /backtest answers straight away with `done`; net profit comes from net_of(inputs)."""
    def handler(req):
        body = json.loads(req.body)
        if seen is not None:
            seen.append(body)
        return (202, net_of(body))
    fake.register("/backtest", "POST", handler)
    return fake


# -- grid expansion ---------------------------------------------------------

def test_grid_is_inclusive():
    assert parse_params({"Fast": {"min": 5, "max": 20, "step": 5}}) == {"Fast": [5, 10, 15, 20]}


def test_grid_epsilon_keeps_the_last_float_point():
    # 0 + 3*0.1 == 0.30000000000000004 > 0.3: a plain `<= max` drops the last point.
    # @DefaultOptimizer.cs:50 compares against max + step/1e6, so it stays in.
    assert parse_params({"Slow": {"min": 0, "max": 0.3, "step": 0.1}}) == {"Slow": [0.0, 0.1, 0.2, 0.3]}


def test_grid_explicit_list_and_constant():
    assert parse_params({"Mode": ["a", "b"], "Fixed": 3}) == {"Mode": ["a", "b"], "Fixed": [3]}


def test_grid_string_form():
    assert parse_params("Fast:5:15:5,Slow:40:60:10") == {"Fast": [5, 10, 15], "Slow": [40, 50, 60]}


def test_grid_wording_matches_cli_nt_bridge():
    for spec, text in (("Fast:5:20", "needs 4 fields"),
                       ("9Fast:5:20:5", "is not a property name"),
                       ("Fast:5:20:x", "is not a number"),
                       ("Fast:5:20:0", "must be > 0"),
                       ("", "needs --opt=")):
        try:
            parse_params(spec)
        except ValueError as e:
            assert text in str(e), (spec, str(e))
        else:
            raise AssertionError(f"{spec!r} should not parse")


def test_grid_dict_step_must_be_positive():
    try:
        parse_params({"Fast": {"min": 5, "max": 20, "step": 0}})
    except ValueError as e:
        assert "must be > 0" in str(e)
    else:
        raise AssertionError("step 0 should not parse")


# -- nt_optimize ------------------------------------------------------------

def test_optimize_refuses_over_max_combos_and_runs_nothing():
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: _doc("b1", 1.0))
        result = nt8.nt_optimize("SampleMACrossOver", {"Fast": {"min": 1, "max": 9, "step": 1}},
                                 max_combos=2, from_date="2026-09-10", to_date="2026-09-17")
        posts = fake.calls[("POST", "/backtest")]
    assert "9 combinations" in result["error"] and "max_combos=2" in result["error"], result
    assert result["combos"] == 9 and result["ran"] == 0
    assert posts == 0, "a refused grid must not run a single backtest"


def test_optimize_max_runs_is_an_alias_for_max_combos():
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: _doc("b1", 1.0))
        result = nt8.nt_optimize("SampleMACrossOver", {"Fast": {"min": 1, "max": 9, "step": 1}},
                                 max_runs=2, from_date="2026-09-10", to_date="2026-09-17")
    assert result["combos"] == 9 and "max_combos=2" in result["error"]


def test_optimize_ranks_best_first_and_returns_the_winner_doc():
    seen = []
    nets = {5: 100.0, 10: 300.0, 15: 200.0}
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: _doc(f"b{body['inputs']['Fast']}", nets[body["inputs"]["Fast"]]), seen)
        result = nt8.nt_optimize("SampleMACrossOver", {"Fast": {"min": 5, "max": 15, "step": 5}},
                                 instrument="ES 12-26", bars_period={"type": "Minute", "value": 5},
                                 from_date="2026-09-10", to_date="2026-09-17")
    assert result["combos"] == 3 and result["ran"] == 3, result
    assert [r["inputs"]["Fast"] for r in result["rows"]] == [10, 15, 5], result["rows"]
    assert [r["rank"] for r in result["rows"]] == [1, 2, 3]
    assert result["rows"][0]["fitness"] == 300.0
    assert result["best"]["id"] == "b10" and result["best"]["trades"], "the winner keeps its trades[]"
    assert "trades" not in result["rows"][0], "ranked rows carry summary only, never trades[]"
    # every combination really went to the AddOn, with the grid value in inputs
    assert sorted(b["inputs"]["Fast"] for b in seen) == [5, 10, 15]
    assert seen[0]["from"] == "2026-09-10" and seen[0]["tickReplay"] is False


def test_optimize_fitness_key_and_min_drawdown_direction():
    # maxDrawdown is <= 0 in status document v1, so MinDrawDown = the largest value.
    dds = {5: -800.0, 10: -100.0}
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: _doc(f"b{body['inputs']['Fast']}", 50.0,
                                           summary={"maxDrawdown": dds[body["inputs"]["Fast"]]}))
        result = nt8.nt_optimize("SampleMACrossOver", {"Fast": [5, 10]}, fitness="MinDrawDown",
                                 from_date="2026-09-10", to_date="2026-09-17")
    assert result["fitnessKey"] == "maxDrawdown"
    assert result["rows"][0]["inputs"]["Fast"] == 10 and result["rows"][0]["fitness"] == -100.0


def test_optimize_min_trades_keeps_a_fluke_from_winning():
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: _doc("b1", 9999.0, trades=1) if body["inputs"]["Fast"] == 5
                   else _doc("b2", 10.0, trades=10))
        result = nt8.nt_optimize("SampleMACrossOver", {"Fast": [5, 10]}, min_trades=5,
                                 from_date="2026-09-10", to_date="2026-09-17")
    assert result["rows"][0]["inputs"]["Fast"] == 10, result["rows"]
    skipped = [r for r in result["rows"] if r["skipped"]]
    assert len(skipped) == 1 and "min_trades=5" in skipped[0]["skipped"]
    assert result["best"]["summary"]["netProfit"] == 10.0


def test_optimize_records_a_failed_run_and_keeps_going():
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: (_doc("b1", 0.0, state="error", summary=None,
                                            error="no bars for ES 12-26")
                                       if body["inputs"]["Fast"] == 5 else _doc("b2", 10.0)))
        result = nt8.nt_optimize("SampleMACrossOver", {"Fast": [5, 10]},
                                 from_date="2026-09-10", to_date="2026-09-17")
    assert result["ran"] == 2 and len(result["errors"]) == 1
    assert result["errors"][0]["error"] == "no bars for ES 12-26"
    assert result["best"]["id"] == "b2"


def test_optimize_stops_on_an_addon_refusal():
    with FakeAddon() as fake:
        fake.json("/backtest", {"error": "unknown input NotARealInput_xyz"}, method="POST", status=400)
        result = nt8.nt_optimize("SampleMACrossOver", {"Fast": [5, 10, 15]},
                                 from_date="2026-09-10", to_date="2026-09-17")
        posts = fake.calls[("POST", "/backtest")]
    assert "unknown input NotARealInput_xyz" in result["error"], result
    assert posts == 1, "a refusal must stop the grid, not repeat itself 3 times"


def test_optimize_rejects_an_unknown_fitness():
    result = nt8.nt_optimize("SampleMACrossOver", {"Fast": [5]}, fitness="MaxVibes")
    assert "unknown fitness 'MaxVibes'" in result["error"]


def test_optimize_sends_chart_even_with_instrument_and_bars_period():
    # chart also carries TradingHours/ResetOnNewTradingDay that instrument+barsPeriod alone
    # do not — dropping it made a grid run silently differ from a plain nt_backtest.
    seen = []
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: _doc("b1", 1.0), seen)
        nt8.nt_optimize("SampleMACrossOver", {"Fast": [5]}, instrument="ES 12-26",
                        bars_period={"type": "Minute", "value": 5}, chart="first",
                        from_date="2026-09-10", to_date="2026-09-17")
    assert seen[0]["chart"] == "first"
    assert seen[0]["instrument"] == "ES 12-26"


def test_optimize_deletes_losing_backtest_jobs_but_keeps_the_winner():
    nets = {5: 100.0, 10: 300.0, 15: 200.0}
    deleted = []
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: _doc(f"b{body['inputs']['Fast']}", nets[body["inputs"]["Fast"]]))
        for fast in (5, 10, 15):
            fake.register(f"/backtest/b{fast}", "DELETE",
                          lambda req, f=fast: (deleted.append(f"b{f}"), (200, {"ok": True}))[1])
        result = nt8.nt_optimize("SampleMACrossOver", {"Fast": {"min": 5, "max": 15, "step": 5}},
                                 top_n=1, from_date="2026-09-10", to_date="2026-09-17")
    assert sorted(deleted) == ["b15", "b5"], "every non-winning, non-returned row must be cleaned up"
    assert result["best"]["id"] == "b10", result


def test_optimize_a_dropped_poll_keeps_the_job_id_instead_of_looking_refused():
    # a transport error DURING a poll (job already queued) must never be mistaken for the
    # AddOn refusing the POST outright — that used to abort the whole grid and lose the id.
    from nt8_mcp import app as _app

    saved = _app.POLL_S
    _app.POLL_S = 0
    try:
        with FakeAddon() as fake:
            fake.register("/backtest", "POST", lambda req: (202, {"id": "b1", "state": "running"}))
            fake.json("/backtest/b1", {"error": "NT8Bridge not reachable on :7891"}, status=200)
            result = nt8.nt_optimize("SampleMACrossOver", {"Fast": [5]},
                                     from_date="2026-09-10", to_date="2026-09-17", wait_s=1)
    finally:
        _app.POLL_S = saved
    assert result.get("refused") is None, "a dropped poll is not a refusal"
    row = result["rows"][0]
    assert row["id"] == "b1"
    assert "not reachable" in row["skipped"]


# -- nt_walkforward ---------------------------------------------------------

def test_walkforward_slices_windows_and_stitches_oos_trades():
    seen = []
    nets = {5: 100.0, 10: 300.0}
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: _doc("b1", nets[body["inputs"]["Fast"]], n_trades=2), seen)
        result = nt8.nt_walkforward("SampleMACrossOver", {"Fast": [5, 10]}, train_days=10, test_days=5,
                                    from_date="2026-01-01", to_date="2026-01-20")
    assert len(result["windows"]) == 2, result["windows"]
    w1, w2 = result["windows"]
    assert w1["inSample"] == {"from": "2026-01-01", "to": "2026-01-10", "inputs": {"Fast": 10},
                              "fitness": 300.0, "ranked": 2}
    assert w1["outOfSample"]["from"] == "2026-01-11" and w1["outOfSample"]["to"] == "2026-01-15"
    # rolling: the second in-sample window slides forward by test_days
    assert w2["inSample"]["from"] == "2026-01-06" and w2["inSample"]["to"] == "2026-01-15"
    assert w2["outOfSample"]["from"] == "2026-01-16" and w2["outOfSample"]["to"] == "2026-01-20"
    # 2 windows x (2 combos + 1 out-of-sample run)
    assert len(seen) == 6
    stitched = result["outOfSample"]
    assert len(stitched["trades"]) == 4 and [t["n"] for t in stitched["trades"]] == [0, 1, 2, 3]
    assert [t["window"] for t in stitched["trades"]] == [1, 1, 2, 2]
    assert stitched["summary"]["netProfit"] == 600.0
    assert "Not a NinjaTrader performance summary" in stitched["note"]


def test_walkforward_anchored_pins_the_in_sample_start():
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: _doc("b1", 100.0))
        result = nt8.nt_walkforward("SampleMACrossOver", {"Fast": [5]}, train_days=10, test_days=5,
                                    anchored=True, from_date="2026-01-01", to_date="2026-01-20")
    assert [w["inSample"]["from"] for w in result["windows"]] == ["2026-01-01", "2026-01-01"]
    assert [w["inSample"]["to"] for w in result["windows"]] == ["2026-01-10", "2026-01-15"]


def test_walkforward_refuses_a_range_too_short_for_one_window():
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: _doc("b1", 1.0))
        result = nt8.nt_walkforward("SampleMACrossOver", {"Fast": [5]}, train_days=30, test_days=10,
                                    from_date="2026-01-01", to_date="2026-01-20")
        posts = fake.calls[("POST", "/backtest")]
    assert "not enough for one" in result["error"] and result["windows"] == []
    assert posts == 0


def test_walkforward_refuses_over_max_backtests_and_runs_nothing():
    # a grid small enough to clear max_combos can still multiply into thousands of runs
    # once spread over enough windows — this refuses the FULL total up front.
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: _doc("b1", 1.0))
        result = nt8.nt_walkforward("SampleMACrossOver", {"Fast": {"min": 5, "max": 45, "step": 5}},
                                    train_days=30, test_days=5, max_backtests=50,
                                    from_date="2024-01-01", to_date="2026-09-18")
        posts = fake.calls[("POST", "/backtest")]
    assert "exceeds max_backtests=50" in result["error"], result
    assert result["ran"] == 0 and posts == 0, "a refused walk-forward must not run a single backtest"
    assert result["windows"] > 0 and result["combos"] == 9 and result["backtests"] == result["windows"] * 10


def test_walkforward_accepts_the_documented_parameter_names():
    with FakeAddon() as fake:
        _grid_fake(fake, lambda body: _doc("b1", 100.0))
        result = nt8.nt_walkforward("SampleMACrossOver", {"Fast": [5]},
                                    optimization_period_days=10, test_period_days=5,
                                    from_date="2026-01-01", to_date="2026-01-20")
    assert result["trainDays"] == 10 and result["testDays"] == 5 and len(result["windows"]) == 2


# -- observed AddOn behavior -------------------------------------------------

def test_a_fraction_for_an_int32_input_is_refused_and_nothing_runs():
    # Observed: the AddOn rounded Fast=5.5 to 6 and still echoed 5.5 — a silently different run.
    posts = []
    with FakeAddon() as fake:
        fake.register("/strategies", "GET", lambda req: (200, [
            {"name": "SampleMACrossOver", "inputs": {"Fast": {"type": "Int32", "default": 10},
                                                     "Ratio": {"type": "Double", "default": 1.0}}}]))
        _grid_fake(fake, lambda body: _doc("b1", 1.0), seen=posts)
        refused = nt8.nt_optimize("SampleMACrossOver", {"Fast": {"min": 5, "max": 6, "step": 0.5}})
        fine = nt8.nt_optimize("SampleMACrossOver", {"Ratio": {"min": 1, "max": 2, "step": 0.5}})
    assert "Fast" in refused["error"] and "Int32" in refused["error"] and refused["ran"] == 0, refused
    assert fine.get("error") is None and fine["ran"] == 3 and len(posts) == 3, (fine, posts)


def test_a_run_over_a_different_window_is_flagged():
    # Observed with Playback connected: asked for 09-14..09-15, NinjaTrader ran 09-08..09-10 and echoed the request.
    shifted = [{"n": 0, "pnl": 1.0, "entryTime": "2026-09-08T19:50:00", "exitTime": "2026-09-10T00:00:00"}]
    honest = [{"n": 0, "pnl": 1.0, "entryTime": "2026-09-13T18:05:00", "exitTime": "2026-09-15T16:55:00"}]
    for trades, flagged in ((shifted, True), (honest, False)):
        with FakeAddon() as fake:
            doc = _doc("b1", 1.0, **{"from": "2026-09-14T00:00:00", "to": "2026-09-15T23:59:59"})
            doc["trades"] = trades
            _grid_fake(fake, lambda body: doc)
            result = nt8.nt_optimize("SampleMACrossOver", {"Fast": [5]},
                                     from_date="2026-09-14", to_date="2026-09-15")
        assert bool(result["warnings"]) is flagged, result["warnings"]
        assert not flagged or "different window" in result["warnings"][0]


def test_the_addons_own_window_warning_is_passed_on():
    # the job document now carries barsFrom/barsTo/warnings; the tool relays the AddOn's sentence.
    with FakeAddon() as fake:
        doc = _doc("b1", 1.0, **{"from": "2026-09-14T00:00:00", "to": "2026-09-15T23:59:59"})
        doc.update(barsFrom="2026-09-08T19:50:00", barsTo="2026-09-10T00:00:00",
                   warnings=["the numbers in this document are for the bars really loaded, ..."])
        _grid_fake(fake, lambda body: doc)
        result = nt8.nt_optimize("SampleMACrossOver", {"Fast": [5]}, from_date="2026-09-14", to_date="2026-09-15")
    assert "bars really loaded" in result["warnings"][0], result["warnings"]
