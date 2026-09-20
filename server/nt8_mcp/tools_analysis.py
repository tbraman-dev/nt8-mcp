"""`nt_analyze` — trade-list breakdowns for a saved run or a trade list handed in
directly. See ../../docs/api/analysis.md for the response schema.

No AddOn call, no NinjaTrader account: the math lives in `nt8_mcp/analysis.py` as pure
functions, this module only adds the tool and the (optional) run-file read. A saved run
is a JSON file the run registry (a separate module) writes under
`<NinjaTrader user data dir>\\nt8mcp\\runs\\<id>.json`; that module is not imported here
(it may not exist yet) — only the three keys nt_analyze needs are read off the file.
"""

import json
import os

from nt8_mcp import analysis
from nt8_mcp.app import NT_HOME, mcp


def _load_run(run_id: str) -> dict:
    """{"trades", "equity", "summary"} off <NT_HOME>/nt8mcp/runs/<run_id>.json, or
    {"error": ...} — never raises. `run_id` is reduced to its basename first so it can
    never walk out of the runs folder."""
    safe_id = os.path.basename(str(run_id))
    path = os.path.join(NT_HOME, "nt8mcp", "runs", f"{safe_id}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {"error": f"no saved run '{run_id}' ({path})"}
    except (OSError, ValueError) as e:
        return {"error": f"could not read run '{run_id}': {type(e).__name__}: {e}"}
    if not isinstance(doc, dict):
        return {"error": f"run '{run_id}' is not a JSON object ({path})."}
    return {"trades": doc.get("trades"), "equity": doc.get("equity"), "summary": doc.get("summary")}


@mcp.tool(name="nt_analyze")
def nt_analyze(run_id: str = "", trades: list | None = None):
    """Breakdowns for a saved run's trade list, or one handed in directly: by month (exit
    time — never entry time, which misattributes any trade spanning a month boundary), by
    exit weekday, by entry hour, long vs short, MAE/MFE summary (when the trades carry
    it), win/loss streaks, the single worst drawdown with its start/trough/recovery time,
    time under water and the longest flat period. Pure Python over the trade list (and the
    equity array a saved run carries, [{"time","cumulativeNetProfit"}] by exit time,
    status document v1) — derived from trades[].pnl when there is no equity
    array. Pass run_id (a saved run's id under
    "<NinjaTrader user data dir>\\nt8mcp\\runs\\<id>.json") or trades (a status-document
    v1 trades[] list) — run_id wins when both are given. A missing or unreadable run, or a
    trades value that is not a list, is {"error": "..."}, never an empty breakdown."""
    if run_id:
        loaded = _load_run(run_id)
        if "error" in loaded:
            return loaded
        trade_list, equity = loaded.get("trades"), loaded.get("equity")
    elif trades is not None:
        trade_list, equity = trades, None
    else:
        return {"error": "pass run_id or trades."}

    if not isinstance(trade_list, list):
        return {"error": "trades must be a list."}

    return {
        "runId": run_id or None,
        "trades": len(trade_list),
        "byMonth": analysis.by_month(trade_list),
        "byWeekday": analysis.by_weekday(trade_list),
        "byEntryHour": analysis.by_entry_hour(trade_list),
        "bySide": analysis.by_side(trade_list),
        "maeMfe": analysis.mae_mfe_summary(trade_list),
        "streaks": analysis.streaks(trade_list),
        "drawdown": analysis.drawdown(trade_list, equity),
        "timeUnderWater": analysis.time_under_water(trade_list, equity),
        "longestFlatPeriod": analysis.longest_flat_period(trade_list, equity),
    }
