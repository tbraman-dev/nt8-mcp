"""Account tools (nt8_mcp.tools_account) against the canned AddOn in fake_addon.py.

No live NT8. The shape assertions below are the contract in docs/api/accounts.md and the
frozen "Status document v1" key order in addon/NOTES.md.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import server as nt8  # noqa: E402

# The 21 summary keys and the 14 trade keys, in the order addon/NOTES.md freezes them.
SUMMARY_KEYS = [
    "trades", "winners", "losers", "winRate", "netProfit", "grossProfit", "grossLoss",
    "profitFactor", "commission", "maxDrawdown", "avgTrade", "avgWinner", "avgLoser",
    "largestWinner", "largestLoser", "avgMae", "avgMfe", "avgBarsInTrade", "sharpe",
    "maxConsecWinners", "maxConsecLosers",
]
TRADE_KEYS = [
    "n", "side", "qty", "entryName", "exitName", "entryTime", "exitTime", "entryPrice",
    "exitPrice", "pnl", "pnlPoints", "mae", "mfe", "bars",
]

EXECUTIONS = {
    "account": "Sim101", "instrument": None,
    "from": "2026-09-18T00:00:00", "to": "2026-09-18T16:30:00",
    "source": "db", "lookbackDays": 3, "total": 2, "capped": False, "warnings": [],
    "executions": [
        {"id": 812, "executionId": "abc-1", "account": "Sim101", "instrument": "ES 12-26",
         "side": "Buy", "qty": 1, "price": 5811.5, "time": "2026-09-18T09:31:04",
         "commission": 0.0, "fee": 0.0, "orderId": "o-1", "orderName": "Entry"},
        {"id": 813, "executionId": "abc-2", "account": "Sim101", "instrument": "ES 12-26",
         "side": "Sell", "qty": 1, "price": 5814.0, "time": "2026-09-18T09:48:11",
         "commission": 0.0, "fee": 0.0, "orderId": "o-2", "orderName": "Exit"},
    ],
}

PERFORMANCE = {
    "account": "Sim101", "instrument": None,
    "from": "2026-09-18T00:00:00", "to": "2026-09-18T16:30:00",
    "source": "db", "lookbackDays": 3, "executions": 4, "padTrimmed": 1, "capped": False,
    "warnings": [],
    "summary": {
        "trades": 2, "winners": 1, "losers": 1, "winRate": 0.5, "netProfit": 62.5,
        "grossProfit": 125.0, "grossLoss": -62.5, "profitFactor": 2.0, "commission": 0.0,
        "maxDrawdown": -62.5, "avgTrade": 31.25, "avgWinner": 125.0, "avgLoser": -62.5,
        "largestWinner": 125.0, "largestLoser": -62.5, "avgMae": 12.5, "avgMfe": 75.0,
        "avgBarsInTrade": 14.0, "sharpe": 0.4, "maxConsecWinners": 1, "maxConsecLosers": 1,
    },
    "trades": [
        {"n": 0, "side": "Long", "qty": 1, "entryName": "Entry", "exitName": "Exit",
         "entryTime": "2026-09-18T09:31:04", "exitTime": "2026-09-18T09:48:11",
         "entryPrice": 5811.5, "exitPrice": 5814.0, "pnl": 125.0, "pnlPoints": 2.5,
         "mae": 0.25, "mfe": 3.0, "bars": 17,
         "commission": 4.18, "commissionSource": "template", "fee": 0.0},
        {"n": 1, "side": "Short", "qty": 1, "entryName": "Entry", "exitName": "Exit",
         "entryTime": "2026-09-18T10:02:00", "exitTime": "2026-09-18T10:11:00",
         "entryPrice": 5814.0, "exitPrice": 5815.25, "pnl": -62.5, "pnlPoints": -1.25,
         "mae": 1.5, "mfe": 0.25, "bars": 9,
         "commission": 4.18, "commissionSource": "template", "fee": 0.0},
    ],
    "commissionInfo": {
        "template": "Default", "source": "template", "total": 8.36,
        "tradesFromStored": 0, "tradesFromTemplate": 2, "tradesNoCommission": 0,
        "tradeCommissionTotal": 0.0, "tradeFeeTotal": 0.0,
        "serverCommissionTotal": 8.36, "serverFeeTotal": 0.0,
    },
}


def _spy(fake, path, body, status=200):
    """Register `path` and return the dict that collects the query it was called with."""
    seen = {}

    def handler(req):
        seen.clear()
        seen.update(req.query)
        return (status, body)

    fake.register(path, "GET", handler)
    return seen


def test_nt_executions_defaults():
    # An empty from/to/instrument means "the AddOn's default", and must not reach it as a literal "".
    with FakeAddon() as fake:
        seen = _spy(fake, "/executions", EXECUTIONS)
        out = nt8.nt_executions(account="Sim101")
    assert seen == {"account": ["Sim101"], "n": ["200"]}, seen
    assert out["source"] == "db"
    assert len(out["executions"]) == 2


def test_nt_executions_passes_every_filter():
    with FakeAddon() as fake:
        seen = _spy(fake, "/executions", EXECUTIONS)
        nt8.nt_executions(account="Sim101", from_date="2026-09-01", to_date="2026-09-18",
                          instrument="ES 12-26", n=50)
    # `from` is a Python keyword, so the tool argument is from_date — the AddOn must still see "from".
    assert seen == {"account": ["Sim101"], "from": ["2026-09-01"], "to": ["2026-09-18"],
                    "instrument": ["ES 12-26"], "n": ["50"]}, seen


def test_nt_executions_unknown_account_is_an_error_not_an_empty_list():
    with FakeAddon() as fake:
        _spy(fake, "/executions", {"error": "no account 'Nope' — call GET /account for the list"}, status=404)
        out = nt8.nt_executions(account="Nope")
    assert "error" in out, out
    assert "no account" in out["error"], out


def test_nt_executions_reports_the_memory_fallback():
    # source=="memory" + a warning is the ONLY signal that the trade DB did not answer and the
    # result is a ~3-day window, not the range that was asked for.
    thin = dict(EXECUTIONS, source="memory",
                warnings=["trade DB unavailable (IOException: ...); limited to the ~3-day in-memory window"])
    with FakeAddon() as fake:
        _spy(fake, "/executions", thin)
        out = nt8.nt_executions(account="Sim101", from_date="2026-01-01")
    assert out["source"] == "memory"
    assert out["warnings"] and "in-memory" in out["warnings"][0]
    assert out["lookbackDays"] == 3


def test_nt_performance_passes_every_filter():
    with FakeAddon() as fake:
        seen = _spy(fake, "/performance", PERFORMANCE)
        nt8.nt_performance(account="Sim101", from_date="2026-09-01", to_date="2026-09-18",
                           instrument="ES 12-26", n=100)
    assert seen == {"account": ["Sim101"], "from": ["2026-09-01"], "to": ["2026-09-18"],
                    "instrument": ["ES 12-26"], "n": ["100"]}, seen


def test_nt_performance_is_a_status_document_v1():
    # The whole point of the shape: report.py (and nt_report) read `summary` + `trades` off a
    # /performance document exactly as off a /backtest/{id} one.
    with FakeAddon() as fake:
        _spy(fake, "/performance", PERFORMANCE)
        out = nt8.nt_performance(account="Sim101")
    assert list(out["summary"].keys()) == SUMMARY_KEYS, list(out["summary"].keys())
    for trade in out["trades"]:
        assert list(trade.keys())[:len(TRADE_KEYS)] == TRADE_KEYS, list(trade.keys())


def test_nt_performance_trades_sum_to_net_profit():
    # Fixture-consistency guard, NOT the real acceptance test — the actual re-pair logic
    # is C# (addon/NT8Bridge.Account.cs Acct_PerformanceJson) and unreachable from this fake HTTP
    # passthrough; the real acceptance is the live scripts/smoke.d/25-accounts.sh check 8. This only
    # pins that the tool does not corrupt the numbers it is handed.
    with FakeAddon() as fake:
        _spy(fake, "/performance", PERFORMANCE)
        out = nt8.nt_performance(account="Sim101")
    assert out["padTrimmed"] == 1
    assert abs(sum(t["pnl"] for t in out["trades"]) - out["summary"]["netProfit"]) < 1e-9


def test_nt_performance_capped_document_passes_through_unchanged():
    # The one thing the tool actually does for the capped case: nothing. A capped document — where
    # trades[] is a subtotal, not the total, per the nt_performance docstring — is passed through
    # exactly as the AddOn sent it. This is the caller-detectable signal the docstring caveat relies on.
    capped_perf = dict(PERFORMANCE, capped=True, trades=PERFORMANCE["trades"][:1])
    with FakeAddon() as fake:
        _spy(fake, "/performance", capped_perf)
        out = nt8.nt_performance(account="Sim101", n=1)
    assert out["capped"] is True
    assert len(out["trades"]) == 1
    assert abs(sum(t["pnl"] for t in out["trades"]) - out["summary"]["netProfit"]) > 1e-9


def test_nt_performance_labels_reconstructed_commission():
    # Never present a reconstruction as an unlabelled number: summary.commission stays
    # NinjaTrader's own value, the reconstruction is commissionInfo.total and says where it came from.
    with FakeAddon() as fake:
        _spy(fake, "/performance", PERFORMANCE)
        out = nt8.nt_performance(account="Sim101")
    info = out["commissionInfo"]
    assert info["source"] in ("stored", "template", "mixed", "none")
    assert info["source"] == "template"
    assert out["summary"]["commission"] == 0.0 and info["total"] == 8.36
    assert info["tradesFromStored"] + info["tradesFromTemplate"] + info["tradesNoCommission"] == len(out["trades"])
    assert all(t["commissionSource"] == "template" for t in out["trades"])


def test_nt_performance_prop_account_has_no_local_commission_answer():
    # A funded/prop account carries no local template: 0 is the true answer, and "none" says so.
    prop = dict(PERFORMANCE, commissionInfo=dict(
        PERFORMANCE["commissionInfo"], template=None, source="none", total=0.0,
        tradesFromTemplate=0, tradesNoCommission=2, serverCommissionTotal=None))
    with FakeAddon() as fake:
        _spy(fake, "/performance", prop)
        out = nt8.nt_performance(account="Prop-1")
    assert out["commissionInfo"]["source"] == "none"
    assert out["commissionInfo"]["template"] is None


def test_both_tools_are_read_only_passthroughs():
    # A GET-only fake: if either tool ever POSTed, the route would not match and this would 404.
    with FakeAddon() as fake:
        fake.json("/executions", EXECUTIONS)
        fake.json("/performance", PERFORMANCE)
        assert "error" not in nt8.nt_executions(account="Sim101")
        assert "error" not in nt8.nt_performance(account="Sim101")
