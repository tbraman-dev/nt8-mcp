"""Real-fill account reads: `nt_executions` and `nt_performance`.

Both are pure reads over NinjaTrader's own trade database and the account's in-memory
execution list. Nothing here submits, modifies or cancels an order. See
../../docs/api/accounts.md for the endpoint contract.

`nt_account` (positions, orders, cash) lives in tools_core.py.
"""

from nt8_mcp.app import _addon_get, mcp


@mcp.tool(name="nt_executions")
def nt_executions(account: str, from_date: str = "", to_date: str = "", instrument: str = "", n: int = 200):
    """Real order fills on one live/Sim account over a date range, oldest first — the raw executions
    behind NinjaTrader's Trade Performance window, not backtest trades (use nt_backtest_status for
    those). `account` is required and is a name from nt_account (case-insensitive); an unknown name
    is a 404, never an empty list. from_date/to_date are YYYY-MM-DD and default to today 00:00 and
    now; to_date is inclusive of the whole day. `instrument` is a full name like "ES 12-26" and is
    pushed into the database query. Returns {"account","instrument","from","to","source","lookbackDays",
    "total","capped","warnings","executions":[{"id","executionId","account","instrument","side",
    "qty","price","time","commission","fee","position","orderId","orderName"}]} (`position` = the
    account's position in that instrument after the fill, as the provider reported it). `source` is "db" when
    NinjaTrader's trade database answered and "memory" when it did not — in which case only the last
    `lookbackDays` (~3) days exist at all and `warnings` says so, so check it before trusting a thin
    result. Per-fill `commission` is 0 for anything that came out of the database: NinjaTrader never
    persists it — call nt_performance for a labelled reconstruction. This opens no window and changes
    nothing."""
    return _addon_get("/executions", account=account, **{"from": from_date}, to=to_date,
                      instrument=instrument, n=n)


@mcp.tool(name="nt_performance")
def nt_performance(account: str, from_date: str = "", to_date: str = "", instrument: str = "", n: int = 5000):
    """Round-trip trades and performance metrics for real fills on one live/Sim account — the same
    numbers NinjaTrader's Trade Performance window shows, computed by NinjaTrader's own
    SystemPerformance engine over the account's executions. Arguments are exactly nt_executions'.
    Returns the SAME "summary" (21 keys) and "trades" shape a backtest status document carries, so
    nt_report renders a live account and a backtest identically, plus {"source","lookbackDays",
    "executions","padTrimmed","padDays","capped","warnings","commissionInfo"}. The query starts 2 days
    early so a round trip opened before from_date still pairs; those trades are then dropped and
    counted in `padTrimmed`. Pairing is only right when the account was flat at the start of that pad:
    the AddOn checks it (Execution.Position) and widens the pad to 7, 30, then 120 days (`padDays`)
    until it is; if it never is, `warnings` carries "not flat at the start of ..." and the trades in
    that instrument CAN BE MIS-PAIRED — read `warnings` before trusting the numbers.
    sum(trades[].pnl) equals summary.netProfit WHEN `capped` is false; when `capped`
    is true, `trades` is truncated to `n` rows but `summary` still covers every trade in range, so
    the sum is a subtotal. COMMISSION IS OFTEN RECONSTRUCTED:
    NinjaTrader does not persist per-fill commission, so commissionInfo.source says whether each
    trade's commission came from the fills ("stored"), from the account's commission template
    ("template"), from both ("mixed") or is genuinely unavailable ("none", normal on a funded/prop
    account) — trades[].commissionSource says which per trade. summary.commission stays
    NinjaTrader's own number; the reconstruction is commissionInfo.total. Read-only: no order is
    placed, changed or cancelled, and no window is opened."""
    return _addon_get("/performance", account=account, **{"from": from_date}, to=to_date,
                      instrument=instrument, n=n)
