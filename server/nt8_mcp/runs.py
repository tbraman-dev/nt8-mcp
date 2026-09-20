"""The run registry (write side). See ../../docs/api/runs.md.
Every finished nt_backtest result is saved to
<NinjaTrader user data dir>\\nt8mcp\\runs\\<utc-stamp>-<id>.json.

Read side (nt_runs/nt_run/nt_run_compare) is tools_runs.py, kept separate so tools_backtest.py
(which calls save_run() directly, not through the MCP tool layer) never imports a tools_* module.

`from nt8_mcp import app` (not `from nt8_mcp.app import NT_HOME`): a test patches app.NT_HOME to a
temp directory, and re-reading it through the module on every call is what makes that patch take —
see nt8_mcp/server.py's docstring on why tools prefer the owning module over a copied name.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone

from nt8_mcp import app

_STRATEGIES_SUBDIR = os.path.join("bin", "Custom", "Strategies")


def _runs_dir() -> str:
    d = os.path.join(app.NT_HOME, "nt8mcp", "runs")
    os.makedirs(d, exist_ok=True)
    return d


def _strategy_source_hash(strategy: str) -> str | None:
    """sha256 of the strategy's .cs source under bin\\Custom\\Strategies, or None when this machine
    cannot find it — a strategy whose source is not under Strategies (a base class in another
    NinjaScript folder, a name mismatch) is a missing fact, not an error worth failing the save over."""
    if not strategy:
        return None
    folder = os.path.join(app.NT_HOME, _STRATEGIES_SUBDIR)
    if not os.path.isdir(folder):
        return None
    direct = os.path.join(folder, strategy + ".cs")
    candidate = direct if os.path.isfile(direct) else None
    if candidate is None:
        pattern = re.compile(r"\bclass\s+" + re.escape(strategy) + r"\b")
        for root, _dirs, files in os.walk(folder):
            for name in files:
                if not name.endswith(".cs"):
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        if pattern.search(fh.read()):
                            candidate = path
                            break
                except OSError:
                    continue
            if candidate is not None:
                break
    if candidate is None:
        return None
    try:
        with open(candidate, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def save_run(request: dict, result: dict) -> dict:
    """Save one finished nt_backtest result (request body + the status doc it returned). Never
    raises: nt_backtest treats a save failure as a warning, never a reason to fail the backtest
    itself. Returns {"id","path"} on success, {"error": str} on failure."""
    try:
        strategy = result.get("strategy") or request.get("strategy") or ""
        run_id = result.get("id") or "b0"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        record = {
            "savedAt": stamp,
            "id": run_id,
            "strategy": strategy,
            "instrument": result.get("instrument"),
            "period": result.get("period"),
            "request": request,
            "inputs": result.get("inputs"),
            "settings": result.get("settings"),
            "from": result.get("from"),
            "to": result.get("to"),
            "barsFrom": result.get("barsFrom"),
            "barsTo": result.get("barsTo"),
            "warnings": result.get("warnings"),
            "state": result.get("state"),
            "error": result.get("error"),
            "summary": result.get("summary"),
            "trades": result.get("trades"),
            "equity": result.get("equity"),
            "sourceHash": _strategy_source_hash(strategy),
        }
        name = f"{stamp}-{run_id}.json"
        path = os.path.join(_runs_dir(), name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
        return {"id": name[:-len(".json")], "path": path}
    except Exception as ex:  # a save failure is a warning on the backtest, never a raised exception
        return {"error": str(ex)}
