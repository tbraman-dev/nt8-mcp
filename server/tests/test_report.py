"""nt8_mcp.report (stats + PDF) and the nt_report tool, against status document v1."""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import report  # noqa: E402
from nt8_mcp import server as nt8  # noqa: E402

# The real document from addon/NOTES.md "Status document v1", trades cut to 4.
DOC = {
    "id": "b1", "state": "done", "strategy": "SampleMACrossOver", "instrument": "ES 12-26",
    "period": "5 Minute", "from": "2026-09-10T00:00:00", "to": "2026-09-17T23:59:59",
    "tickReplay": False, "startedAt": "2026-09-18T17:56:53", "finishedAt": "2026-09-18T17:56:53",
    "seconds": 0.63, "error": None, "inputs": {},
    "summary": {"trades": 4, "winners": 2, "losers": 2, "winRate": 0.5, "netProfit": -212.5,
                "grossProfit": 13862.5, "grossLoss": -14075, "profitFactor": 0.9849, "commission": 0,
                "maxDrawdown": -4425, "avgTrade": -2.79, "avgWinner": 478.01, "avgLoser": -327.32,
                "largestWinner": 2512.5, "largestLoser": -2000, "avgMae": 366.6, "avgMfe": 548.6,
                "avgBarsInTrade": 20.2, "sharpe": -1.02, "maxConsecWinners": 4, "maxConsecLosers": 5},
    "trades": [{"n": 0, "side": "Long", "qty": 1, "pnl": 112.5, "pnlPoints": 2.25},
               {"n": 1, "side": "Short", "qty": 1, "pnl": -500.0, "pnlPoints": -10.0},
               {"n": 2, "side": "Long", "qty": 1, "pnl": 300.0, "pnlPoints": 6.0},
               {"n": 3, "side": "Short", "qty": 1, "pnl": -125.0, "pnlPoints": -2.5}],
    "output": [],
}


def test_stats_use_ninjatraders_own_numbers():
    s = report.compute_stats(DOC)
    assert s["net"] == -212.5 and s["trades"] == 4
    assert s["profit_factor"] == 0.9849 and s["max_dd"] == -4425
    assert s["win_rate"] == 50.0, "summary.winRate is a fraction in v1; stats report a percentage"
    assert s["avg_win"] == 478.01 and s["avg_loss"] == -327.32
    assert s["n_wins"] == 2 and s["n_losses"] == 2


def test_equity_and_drawdown_come_from_the_trades():
    s = report.compute_stats(DOC)
    assert s["equity"] == [112.5, -387.5, -87.5, -212.5]
    assert s["running_peak"] == [112.5, 112.5, 112.5, 112.5]
    assert s["drawdown"] == [0.0, -500.0, -200.0, -325.0]


def test_missing_optional_keys_fall_back_instead_of_raising():
    doc = dict(DOC, summary={"trades": 4})           # every other summary key absent
    s = report.compute_stats(doc)
    assert s["net"] == -212.5                         # summed from trades[].pnl
    assert s["max_dd"] == -500.0                      # deepest point of the equity curve
    assert s["win_rate"] == 50.0 and s["sharpe"] is None
    assert round(s["profit_factor"], 4) == round(412.5 / 625.0, 4)


def test_null_values_are_tolerated():
    doc = dict(DOC, summary=dict(DOC["summary"], profitFactor=None, maxDrawdown=None, sharpe=None))
    s = report.compute_stats(doc)
    assert s["profit_factor"] == round(412.5 / 625.0, 10) or s["profit_factor"] > 0
    assert s["max_dd"] == -500.0


def test_no_trades_and_no_summary():
    s = report.compute_stats({"id": "b9", "state": "done", "summary": None, "trades": []})
    assert s["trades"] == 0 and s["net"] == 0.0 and s["equity"] == []
    assert report.compute_stats({}) ["strategy"] == "(strategy)"
    assert report.assess({}) == "no summary"
    assert report.format_metrics_table({}) == "(no summary)"


def test_assess_reads_the_summary():
    text = report.assess(DOC["summary"])
    assert "unprofitable" in text and "4 trades" in text and "no edge" in text


def test_nt_report_stats_only():
    result = nt8.nt_report(status_doc=DOC)
    assert result["stats"]["net"] == -212.5 and result["pdf"] is None
    assert "no pdf_path" in result["note"]
    assert "equity" not in result["stats"], "the arrays are for the PDF, not for the model's context"
    assert "netProfit" in result["table"]


def test_nt_report_fetches_by_id():
    with FakeAddon() as fake:
        fake.json("/backtest/b1", DOC)
        result = nt8.nt_report("b1")
    assert result["stats"]["id"] == "b1"


def test_nt_report_reports_a_missing_backtest():
    with FakeAddon() as fake:
        fake.json("/backtest/b9", {"error": "no backtest 'b9'"}, status=404)
        result = nt8.nt_report("b9")
    assert "no backtest 'b9'" in result["error"]


def test_nt_report_without_an_id_or_a_doc():
    assert "pass id" in nt8.nt_report()["error"]


def test_nt_report_without_matplotlib_still_returns_stats():
    saved = report._plt

    def boom():
        raise RuntimeError(report.NO_MATPLOTLIB)

    report._plt = boom
    try:
        result = nt8.nt_report(status_doc=DOC, pdf_path=os.path.join(tempfile.gettempdir(), "nope.pdf"))
    finally:
        report._plt = saved
    assert result["pdf"] is None and "matplotlib" in result["note"]
    assert result["stats"]["net"] == -212.5, "no PDF must never cost us the stats"


def test_nt_report_writes_a_pdf_when_matplotlib_is_there():
    if not report.has_matplotlib():
        print("      (matplotlib not installed — PDF rendering not exercised)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "b1.pdf")
        result = nt8.nt_report(status_doc=DOC, pdf_path=out)
        assert result["pdf"] == out and os.path.getsize(out) > 1000, result
        with open(out, "rb") as fh:
            assert fh.read(4) == b"%PDF"
        # and a batch page over two documents
        batch = os.path.join(tmp, "batch.pdf")
        assert report.render_batch_pdf([DOC, dict(DOC, id="b2")], batch) == batch
        assert os.path.getsize(batch) > 1000


def test_nt_report_survives_an_unwritable_path():
    if not report.has_matplotlib():
        return
    result = nt8.nt_report(status_doc=DOC, pdf_path=os.path.join(tempfile.gettempdir(), "no-such-dir", "x.pdf"))
    assert result["pdf"] is None and "PDF not written" in result["note"]
    assert result["stats"]["net"] == -212.5
