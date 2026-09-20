"""Read side of the run registry — see runs.py (the writer, called from
nt8_mcp.tools_backtest.nt_backtest(save_run=True)) and ../../docs/api/runs.md.
"""

import json
import os

from nt8_mcp.app import mcp
from nt8_mcp.runs import _runs_dir


def _run_files() -> list[str]:
    """Every saved run's path, newest first. The filename itself IS a sortable UTC stamp
    (<stamp>-<id>.json), so this needs no per-file stat call."""
    d = _runs_dir()
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return []
    names.sort(reverse=True)
    return [os.path.join(d, n) for n in names]


def _load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _run_id_of(path: str) -> str:
    return os.path.basename(path)[: -len(".json")]


def _find(run_id: str) -> dict | None:
    for path in _run_files():
        if _run_id_of(path) == run_id:
            return _load(path)
    return None


@mcp.tool(name="nt_runs")
def nt_runs(limit: int = 20, strategy: str = "") -> list[dict]:
    """List saved backtest runs (newest first), one compact row each: id (pass to nt_run/nt_run_compare),
    savedAt, strategy, instrument, period, from/to, and trades/netProfit off the saved summary. strategy
    filters by exact name (case-insensitive) when given. A run that failed to load off disk is skipped,
    not counted against limit. Full detail (inputs, cost settings, equity, sourceHash) is nt_run(id)."""
    rows = []
    for path in _run_files():
        rec = _load(path)
        if rec is None:
            continue
        if strategy and (rec.get("strategy") or "").lower() != strategy.lower():
            continue
        summary = rec.get("summary") or {}
        rows.append({
            "id": _run_id_of(path),
            "savedAt": rec.get("savedAt"),
            "strategy": rec.get("strategy"),
            "instrument": rec.get("instrument"),
            "period": rec.get("period"),
            "from": rec.get("from"),
            "to": rec.get("to"),
            "state": rec.get("state"),
            "trades": summary.get("trades"),
            "netProfit": summary.get("netProfit"),
        })
        if len(rows) >= limit:
            break
    return rows


@mcp.tool(name="nt_run")
def nt_run(id: str) -> dict:
    """One saved run in full: the original request, inputs, cost settings, from/to and barsFrom/barsTo,
    warnings, summary, equity, and sourceHash (sha256 of the strategy's .cs under bin\\Custom\\Strategies,
    null when it could not be found). id is the value nt_runs prints, not the backtest id alone —
    it is the file's stem, "<utc-stamp>-<backtest id>"."""
    rec = _find(id)
    return rec if rec is not None else {"error": f"no run '{id}'"}


@mcp.tool(name="nt_run_compare")
def nt_run_compare(a: str, b: str) -> dict:
    """Diff two saved runs by id (from nt_runs): differing inputs, differing cost settings, the
    requested/loaded window, and every summary metric that changed. Either id not found is an
    error naming which one — never a partial diff."""
    ra, rb = _find(a), _find(b)
    if ra is None:
        return {"error": f"no run '{a}'"}
    if rb is None:
        return {"error": f"no run '{b}'"}

    def _diff(da: dict, db: dict) -> dict:
        out = {}
        for key in sorted(set(da) | set(db)):
            va, vb = da.get(key), db.get(key)
            if va != vb:
                out[key] = {"a": va, "b": vb}
        return out

    return {
        "a": a,
        "b": b,
        "sourceHashMatches": ra.get("sourceHash") is not None and ra.get("sourceHash") == rb.get("sourceHash"),
        "inputs": _diff(ra.get("inputs") or {}, rb.get("inputs") or {}),
        "settings": _diff(ra.get("settings") or {}, rb.get("settings") or {}),
        "window": _diff(
            {"from": ra.get("from"), "to": ra.get("to"), "barsFrom": ra.get("barsFrom"), "barsTo": ra.get("barsTo")},
            {"from": rb.get("from"), "to": rb.get("to"), "barsFrom": rb.get("barsFrom"), "barsTo": rb.get("barsTo")},
        ),
        "summary": _diff(ra.get("summary") or {}, rb.get("summary") or {}),
    }
