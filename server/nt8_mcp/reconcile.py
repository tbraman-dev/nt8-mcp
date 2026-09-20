"""Backtest-versus-Playback reconciliation — the pure math. See ../../docs/api/reconcile.md,
addon/NOTES.md "Status document v1" (backtest `trades[]` shape) and docs/api/accounts.md /
docs/api/playback.md (execution shapes) for what this reads.

Pure functions only: standard library, no NinjaTrader, no HTTP, no filesystem, nothing started.
`tools_reconcile.py` is the only thing that loads inputs (a saved run, a live backtest status, a
live playback run, or a plain list) and hands them to `reconcile()` below.
"""

from __future__ import annotations

import statistics
from datetime import datetime

_TIME_FMT = "%Y-%m-%dT%H:%M:%S"


def _parse_time(s) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s[:19], _TIME_FMT)
    except ValueError:
        return None


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _sub(a, b):
    a, b = _num(a), _num(b)
    return None if a is None or b is None else a - b


def normalize_backtest_trades(backtest) -> list[dict]:
    """Accepts a backtest status document (`GET /backtest/{id}`) or a saved run record (both have a
    top-level `trades` key, docs/api/backtest.md / docs/api/runs.md), or a bare `trades[]` list.
    Returns the entry/exit fields every trade needs for reconciliation, in a stable shape; a trade
    recorded as `{"error": ...}` or with no entry (`side == ""`) is dropped — there is nothing to
    reconcile it against."""
    trades = backtest.get("trades") if isinstance(backtest, dict) else backtest
    out = []
    for t in trades or []:
        if not isinstance(t, dict) or "error" in t or not t.get("side"):
            continue
        out.append({
            "side": t.get("side"),
            "qty": _num(t.get("qty")),
            "entryTime": t.get("entryTime"),
            "exitTime": t.get("exitTime"),
            "entryPrice": _num(t.get("entryPrice")),
            "exitPrice": _num(t.get("exitPrice")),
            "pnl": _num(t.get("pnl")),
            "pnlPoints": _num(t.get("pnlPoints")),
        })
    return out


def normalize_playback_executions(playback) -> list[dict]:
    """Accepts a flat execution list (`GET /executions` shape, docs/api/accounts.md), a playback
    run's own `executions[]` (a list of `{account, executions:[...]}` groups, docs/api/playback.md
    `GET /playback/run/{id}`) passed either as that list or as the whole run status document, or a
    bare execution list already flattened. Returns one flat list, oldest first is NOT assumed — the
    caller (`build_trades_from_executions`) sorts by time itself. A row recorded as `{"error": ...}`
    or with no side is dropped."""
    if isinstance(playback, dict):
        rows = playback.get("executions") or []
    else:
        rows = playback or []
    if rows and isinstance(rows[0], dict) and "executions" in rows[0] and "side" not in rows[0]:
        rows = [e for group in rows for e in (group.get("executions") or [])]

    out = []
    for e in rows:
        if not isinstance(e, dict) or "error" in e or not e.get("side"):
            continue
        out.append({
            "instrument": e.get("instrument"),
            "account": e.get("account"),
            "side": e.get("side"),
            "qty": _num(e.get("qty")),
            "price": _num(e.get("price")),
            "time": e.get("time"),
        })
    return out


def _open_lot(direction: int, qty: float, price: float, time_: str, account, instrument) -> dict:
    return {
        "dir": direction,
        "side": "Long" if direction > 0 else "Short",
        "qty": qty,
        "entryPriceSum": price * qty,
        "entryTime": time_,
        "exitPriceSum": 0.0,
        "exitQty": 0.0,
        "exitTime": None,
        "account": account,
        "instrument": instrument,
    }


def _close_lot(lot: dict) -> dict:
    qty, exit_qty = lot["qty"], lot["exitQty"]
    return {
        "side": lot["side"],
        "qty": qty,
        "entryTime": lot["entryTime"],
        "exitTime": lot["exitTime"],
        "entryPrice": lot["entryPriceSum"] / qty if qty else None,
        "exitPrice": lot["exitPriceSum"] / exit_qty if exit_qty else None,
        "account": lot["account"],
        "instrument": lot["instrument"],
    }


def build_trades_from_executions(executions: list[dict]) -> list[dict]:
    """FIFO-pair raw fills (already normalized) into round-trip trades, grouped by
    (account, instrument) and walked in time order. A fill on a flat position opens a trade in its
    own direction (Buy -> Long, Sell -> Short); more fills the SAME side scale it in
    (quantity-weighted average entry price); a fill the OTHER side closes it (quantity-weighted
    average exit price) until the entry quantity is used up, which finishes the trade. A closing
    fill bigger than what is still open finishes that trade and opens a new one the other way with
    the leftover quantity at the same fill price and time — a position reversal, same as the
    broker's own book.

    ponytail: one open lot per (account, instrument) at a time (no independent concurrent entries
    in the same instrument — EntriesPerDirection == 1). Upgrade path: per-entry lot ids if a
    strategy under test ever scales in with independent stops. A lot still open at the end of the
    fill list (never closed) is dropped, not reported as a trade — there is nothing to reconcile a
    still-open position against.
    """
    groups: dict[tuple, list[dict]] = {}
    for e in executions:
        groups.setdefault((e.get("account"), e.get("instrument")), []).append(e)

    trades = []
    for (account, instrument), fills in groups.items():
        fills = sorted(fills, key=lambda e: _parse_time(e.get("time")) or datetime.min)
        lot = None
        for e in fills:
            qty, price, time_ = e.get("qty"), e.get("price"), e.get("time")
            if not qty or qty <= 0 or price is None:
                continue
            direction = 1 if e.get("side") == "Buy" else -1 if e.get("side") == "Sell" else 0
            if direction == 0:
                continue
            if lot is None:
                lot = _open_lot(direction, qty, price, time_, account, instrument)
                continue
            if direction == lot["dir"]:
                lot["qty"] += qty
                lot["entryPriceSum"] += price * qty
                continue
            remaining = lot["qty"] - lot["exitQty"]
            close_qty = min(qty, remaining)
            lot["exitPriceSum"] += price * close_qty
            lot["exitQty"] += close_qty
            lot["exitTime"] = time_
            leftover = qty - close_qty
            if lot["exitQty"] >= lot["qty"]:
                trades.append(_close_lot(lot))
                lot = _open_lot(direction, leftover, price, time_, account, instrument) if leftover > 0 else None
        if lot is not None and lot["exitQty"] > 0:
            trades.append(_close_lot(lot))

    trades.sort(key=lambda t: _parse_time(t.get("entryTime")) or datetime.min)
    return trades


def match_trades(backtest_trades: list[dict], playback_trades: list[dict],
                  tolerance_seconds: float = 60) -> tuple[list[tuple], list[dict], list[dict]]:
    """Greedy nearest-time match: every (backtest trade, playback trade) pair with the same `side`
    and an `entryTime` gap within `tolerance_seconds` is a match candidate; candidates are taken
    closest-first, so two backtest trades competing for one playback trade cannot grab it out of
    order. Returns `(pairs, backtest_only, playback_only)`, `pairs` = list of
    `(backtest_trade, playback_trade, entry_time_delta_seconds)`. A trade whose `entryTime` cannot
    be parsed, or that has no same-side candidate inside the window, is unmatched."""
    candidates = []
    for bi, bt in enumerate(backtest_trades):
        bt_time = _parse_time(bt.get("entryTime"))
        if bt_time is None:
            continue
        for pi, pt in enumerate(playback_trades):
            if pt.get("side") != bt.get("side"):
                continue
            pt_time = _parse_time(pt.get("entryTime"))
            if pt_time is None:
                continue
            delta = abs((pt_time - bt_time).total_seconds())
            if delta <= tolerance_seconds:
                candidates.append((delta, bi, pi))
    candidates.sort(key=lambda c: c[0])

    used_b, used_p = set(), set()
    pairs = []
    for delta, bi, pi in candidates:
        if bi in used_b or pi in used_p:
            continue
        used_b.add(bi)
        used_p.add(pi)
        pairs.append((backtest_trades[bi], playback_trades[pi], delta))

    backtest_only = [bt for i, bt in enumerate(backtest_trades) if i not in used_b]
    playback_only = [pt for i, pt in enumerate(playback_trades) if i not in used_p]
    pairs.sort(key=lambda p: _parse_time(p[0].get("entryTime")) or datetime.min)
    return pairs, backtest_only, playback_only


def _point_value(backtest_trades: list[dict]):
    """Currency per point, derived from the backtest's OWN trades (pnl / (pnlPoints * qty)) rather
    than an instrument table this module does not have — nt_reconcile has no NinjaTrader contact.
    Median of every trade where the ratio is computable, so one trade with a rounding artifact or a
    commission-only difference cannot skew it. None when no trade carries enough to compute one."""
    ratios = []
    for t in backtest_trades:
        pnl, pts, qty = t.get("pnl"), t.get("pnlPoints"), t.get("qty")
        if pnl is not None and pts and qty:
            ratios.append(abs(pnl / (pts * qty)))
    return statistics.median(ratios) if ratios else None


def _pluralize(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def reconcile(backtest, playback_executions, tolerance_ticks: float = 1, tolerance_seconds: float = 60,
              tick_size: float | None = None) -> dict:
    """The whole check: normalize both sides, pair the replay's raw fills into round-trip trades,
    match them against the backtest's trades by side + entry-time window, and report matched pairs
    (price/time/quantity deltas), the fills only one side has, total slippage, and a plain verdict.

    `tick_size` is optional — this module never talks to NinjaTrader, so it has no instrument table.
    Without it, price deltas are reported in raw price units only (`entryTicksDelta` /
    `exitTicksDelta` / `slippage.totalTicks` / `slippage.totalCurrency` are `null`,
    `slippage.ticksAvailable` is `false`). Currency is derived from the BACKTEST's own trades
    (`pnl / pnlPoints`, see `_point_value`), never a table this module would otherwise have to trust.

    ponytail: `entryTime`/`exitTime` on both sides are compared as naive wall-clock strings — a
    backtest's historical timestamps and a replay's real execution timestamps are not guaranteed to
    share one clock/timezone (docs/api/playback.md admits the same gap with its own +/-3h pad).
    `tolerance_seconds` absorbs small skew; a large, systematic one needs a bigger tolerance. Upgrade
    path: an explicit `clock_offset_seconds` if a real mismatch is ever observed live.
    """
    backtest_trades = normalize_backtest_trades(backtest)
    playback_fills = normalize_playback_executions(playback_executions)
    playback_trades = build_trades_from_executions(playback_fills)
    pairs, backtest_only, playback_only = match_trades(backtest_trades, playback_trades, tolerance_seconds)

    point_value = _point_value(backtest_trades)
    ticks_available = bool(tick_size) and tick_size > 0
    tick_value = tick_size * point_value if ticks_available and point_value is not None else None

    matched = []
    total_ticks = 0.0
    total_currency = 0.0
    for bt, pt, entry_delta_sec in pairs:
        entry_price_delta = _sub(pt.get("entryPrice"), bt.get("entryPrice"))
        exit_price_delta = _sub(pt.get("exitPrice"), bt.get("exitPrice"))
        entry_ticks = entry_price_delta / tick_size if ticks_available and entry_price_delta is not None else None
        exit_ticks = exit_price_delta / tick_size if ticks_available and exit_price_delta is not None else None
        row = {
            "side": bt.get("side"),
            "entryTimeDeltaSec": entry_delta_sec,
            "backtestEntryTime": bt.get("entryTime"), "playbackEntryTime": pt.get("entryTime"),
            "backtestExitTime": bt.get("exitTime"), "playbackExitTime": pt.get("exitTime"),
            "backtestEntryPrice": bt.get("entryPrice"), "playbackEntryPrice": pt.get("entryPrice"),
            "entryPriceDelta": entry_price_delta, "entryTicksDelta": entry_ticks,
            "backtestExitPrice": bt.get("exitPrice"), "playbackExitPrice": pt.get("exitPrice"),
            "exitPriceDelta": exit_price_delta, "exitTicksDelta": exit_ticks,
            "backtestQty": bt.get("qty"), "playbackQty": pt.get("qty"), "qtyDelta": _sub(pt.get("qty"), bt.get("qty")),
            "withinTolerance": (
                (entry_ticks is None or abs(entry_ticks) <= tolerance_ticks) and
                (exit_ticks is None or abs(exit_ticks) <= tolerance_ticks)
            ) if ticks_available else None,
        }
        matched.append(row)
        if ticks_available:
            leg_ticks = (abs(entry_ticks) if entry_ticks is not None else 0.0) + \
                        (abs(exit_ticks) if exit_ticks is not None else 0.0)
            total_ticks += leg_ticks
            if tick_value is not None and bt.get("qty"):
                total_currency += leg_ticks * tick_value * bt["qty"]

    verdict_parts = []
    if backtest_only:
        verdict_parts.append(f"the backtest filled {_pluralize(len(backtest_only), 'trade')} the replay did not")
    if playback_only:
        verdict_parts.append(f"the replay filled {_pluralize(len(playback_only), 'trade')} the backtest did not")
    if not verdict_parts:
        verdict_parts.append(
            f"all {_pluralize(len(matched), 'trade')} matched between backtest and replay" if matched
            else "neither the backtest nor the replay had a trade to compare"
        )
    if matched and any(row["withinTolerance"] is False for row in matched):
        off = sum(1 for row in matched if row["withinTolerance"] is False)
        verdict_parts.append(f"{_pluralize(off, 'matched trade')} outside the {tolerance_ticks}-tick tolerance")

    return {
        "matched": matched,
        "backtestOnly": backtest_only,
        "playbackOnly": playback_only,
        "counts": {
            "backtestTrades": len(backtest_trades), "playbackTrades": len(playback_trades),
            "matched": len(matched), "backtestOnly": len(backtest_only), "playbackOnly": len(playback_only),
        },
        "slippage": {
            "ticksAvailable": ticks_available,
            "tickSize": tick_size,
            "pointValue": point_value,
            "totalTicks": total_ticks if ticks_available else None,
            "totalCurrency": total_currency if ticks_available and tick_value is not None else None,
        },
        "toleranceTicks": tolerance_ticks,
        "toleranceSeconds": tolerance_seconds,
        "verdict": "; ".join(verdict_parts) + ".",
    }
