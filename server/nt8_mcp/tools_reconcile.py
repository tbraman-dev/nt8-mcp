"""nt_reconcile — the Backtest-versus-Playback check. See ../../docs/api/reconcile.md.

This file only LOADS inputs (a saved run, a live backtest's status, a live playback run's status,
or plain lists handed straight in) and calls the pure functions in reconcile.py. It starts nothing:
no backtest, no playback run, no order. Reading a live backtest/playback status still goes through
the AddOn's read-only HTTP endpoints the same way `nt_backtest_status` / `nt_playback_run_status`
already do — this module reuses those tools rather than re-implementing the read.
"""

from nt8_mcp.app import mcp
from nt8_mcp.reconcile import reconcile
from nt8_mcp.tools_backtest import nt_backtest_status
from nt8_mcp.tools_playback import nt_playback_run_status
from nt8_mcp.tools_runs import nt_run

_UNFINISHED_BACKTEST = ("queued", "running")
_UNFINISHED_PLAYBACK = ("queued", "running")


@mcp.tool(name="nt_reconcile")
def nt_reconcile(
    backtest_id: str = "",
    backtest_run_id: str = "",
    backtest_trades: list | None = None,
    playback_run_id: str = "",
    playback_executions: list | None = None,
    tolerance_ticks: float = 1,
    tolerance_seconds: float = 60,
    tick_size: float | None = None,
) -> dict:
    """Reconcile a backtest's trades against a Market Replay run's real fills — the check no other
    NinjaTrader tool does. Give the backtest ONE of: `backtest_id` (a live/finished id, read through
    nt_backtest_status), `backtest_run_id` (a saved run id from nt_runs/nt_run), or `backtest_trades`
    (a bare trades[] list, or the whole status/run document — docs/api/backtest.md). Give the replay
    ONE of: `playback_run_id` (read through nt_playback_run_status) or `playback_executions` (a bare
    execution list, docs/api/accounts.md GET /executions shape, or a playback run's own `executions`
    field/document, docs/api/playback.md).

    The replay's raw fills are paired into round-trip trades (FIFO by side, per account+instrument,
    docs/api/reconcile.md) and matched to the backtest's trades by side and an entry-time window
    (`tolerance_seconds`, default 60s — the two runs' clocks are not guaranteed to agree exactly).
    Returns `matched` (paired trades with entry/exit price, time and quantity deltas — in ticks too
    when `tick_size` is given), `backtestOnly` / `playbackOnly` (trades only one side has),
    `counts`, `slippage` (`totalTicks`/`totalCurrency`, currency derived from the backtest's OWN
    pnl/pnlPoints ratio — this tool has no instrument table), and a plain-English `verdict` line
    such as "the backtest filled 2 trades the replay did not".

    `tolerance_ticks` (default 1) only affects the `withinTolerance` flag on each matched pair and is
    a no-op — always `null` — unless `tick_size` is given (this tool never resolves one itself; pass
    the instrument's tick size, e.g. 0.25 for ES, if you want it). Reads only: this never starts a
    backtest, a playback run, or any order.
    """
    bt_given = sum([bool(backtest_id), bool(backtest_run_id), backtest_trades is not None])
    if bt_given != 1:
        return {"error": "give exactly one of backtest_id, backtest_run_id, backtest_trades"}

    if backtest_id:
        bt_source = nt_backtest_status(backtest_id)
        if not isinstance(bt_source, dict) or "error" in bt_source:
            return bt_source
        if bt_source.get("state") in _UNFINISHED_BACKTEST:
            return {"error": f"backtest '{backtest_id}' has not finished (state={bt_source.get('state')})"}
    elif backtest_run_id:
        bt_source = nt_run(backtest_run_id)
        if "error" in bt_source:
            return bt_source
    else:
        bt_source = backtest_trades

    pb_given = sum([bool(playback_run_id), playback_executions is not None])
    if pb_given != 1:
        return {"error": "give exactly one of playback_run_id, playback_executions"}

    if playback_run_id:
        pb_source = nt_playback_run_status(playback_run_id)
        if not isinstance(pb_source, dict) or "error" in pb_source:
            return pb_source
        if pb_source.get("state") in _UNFINISHED_PLAYBACK:
            return {"error": f"playback run '{playback_run_id}' has not finished (state={pb_source.get('state')})"}
    else:
        pb_source = playback_executions

    return reconcile(
        bt_source, pb_source,
        tolerance_ticks=tolerance_ticks, tolerance_seconds=tolerance_seconds, tick_size=tick_size,
    )
