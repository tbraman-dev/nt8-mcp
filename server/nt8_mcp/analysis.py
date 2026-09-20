"""Pure functions over a trade list — and, when the caller has one, the equity array
status document v1 now carries (`addon/NOTES.md` "Status document v1"):
`[{"time": "...", "cumulativeNetProfit": ...}]`, one point per closed trade, by exit time.

No AddOn call, no NinjaTrader account, no file I/O — `tools_analysis.py` is the only
thing here that reads a run off disk. Every function tolerates a trade missing the field
it needs (no `mae`, no `entryTime`, ...) by leaving that trade out of the one breakdown
that needed it, never by raising. Trade fields are status document v1's own: `side`,
`entryTime`, `exitTime`, `pnl`, `mae`, `mfe` (points).
"""
from __future__ import annotations

from datetime import datetime

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _dt(text):
    """A status-document timestamp ("yyyy-MM-ddTHH:mm:ss", no zone) as a datetime, or
    None for anything missing or unparsable."""
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(text)[:19], fmt)
        except ValueError:
            continue
    return None


def _iso(dt) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def _pnl(trade: dict):
    value = trade.get("pnl")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _bucket(trades: list, key_fn) -> dict:
    """{key: {trades, netProfit, winners, losers}}, in first-seen order, skipping any
    trade `key_fn` can't place (returns None for it)."""
    out = {}
    for t in trades:
        key = key_fn(t)
        if key is None:
            continue
        row = out.setdefault(key, {"trades": 0, "netProfit": 0.0, "winners": 0, "losers": 0})
        row["trades"] += 1
        pnl = _pnl(t)
        if pnl is not None:
            row["netProfit"] += pnl
            row["winners"] += pnl > 0
            row["losers"] += pnl < 0
    return out


def by_month(trades: list) -> dict:
    """Keyed by EXIT month, "YYYY-MM" — never entry time, which misattributes any trade
    that spans a month boundary."""
    return _bucket(trades, lambda t: (lambda d: d.strftime("%Y-%m") if d else None)(_dt(t.get("exitTime"))))


def by_weekday(trades: list) -> dict:
    """Keyed by exit weekday name, only the days that actually have a trade."""
    return _bucket(trades, lambda t: (lambda d: _WEEKDAYS[d.weekday()] if d else None)(_dt(t.get("exitTime"))))


def by_entry_hour(trades: list) -> dict:
    """Keyed by entry hour, 0..23, NT8-local (no timezone conversion — same convention as
    the status document itself)."""
    return _bucket(trades, lambda t: (lambda d: d.hour if d else None)(_dt(t.get("entryTime"))))


def by_side(trades: list) -> dict:
    """Keyed "Long" / "Short"; a trade with no entry (`side == ""`) is left out."""
    return _bucket(trades, lambda t: t.get("side") or None)


def mae_mfe_summary(trades: list) -> dict | None:
    """avg/max MAE and MFE, in points (status document v1's own unit for the per-trade
    fields), over whichever trades carry them. None when none do."""
    mae = [float(t["mae"]) for t in trades if t.get("mae") is not None]
    mfe = [float(t["mfe"]) for t in trades if t.get("mfe") is not None]
    if not mae and not mfe:
        return None
    return {
        "trades": max(len(mae), len(mfe)),
        "avgMae": sum(mae) / len(mae) if mae else None,
        "maxMae": max(mae) if mae else None,
        "avgMfe": sum(mfe) / len(mfe) if mfe else None,
        "maxMfe": max(mfe) if mfe else None,
    }


def streaks(trades: list) -> dict:
    """Longest win streak and longest loss streak, in the list's own order (NinjaTrader's
    chronological `n`), plus the streak still open at the end. A trade with no/zero pnl
    breaks any streak without starting a new one."""
    longest_win = longest_loss = current = 0
    current_kind = None
    for t in trades:
        pnl = _pnl(t)
        if not pnl:  # None or exactly 0.0
            current, current_kind = 0, None
            continue
        kind = "win" if pnl > 0 else "loss"
        current = current + 1 if kind == current_kind else 1
        current_kind = kind
        if kind == "win":
            longest_win = max(longest_win, current)
        else:
            longest_loss = max(longest_loss, current)
    return {"longestWinStreak": longest_win, "longestLossStreak": longest_loss,
            "currentStreak": current, "currentStreakKind": current_kind}


def _equity_points(trades: list, equity: list | None) -> list:
    """[(time|None, cumulativeNetProfit|None), ...], oldest first. Uses the supplied
    equity array when there is one (status document v1's own, by exit time); otherwise
    derives one from trades[].pnl in the list's own order."""
    if equity:
        return [(_dt(p.get("time")), p.get("cumulativeNetProfit")) for p in equity]
    points, cum = [], 0.0
    for t in trades:
        pnl = _pnl(t)
        if pnl is None:
            continue
        cum += pnl
        points.append((_dt(t.get("exitTime")), cum))
    return points


def _usable_points(trades: list, equity: list | None) -> list:
    points = [(t, v) for t, v in _equity_points(trades, equity) if t is not None and v is not None]
    points.sort(key=lambda p: p[0])
    return points


def drawdown(trades: list, equity: list | None = None) -> dict | None:
    """The single worst drawdown: `amount` (<=0), `start` (the prior peak's time),
    `trough` (the lowest point's time), `recovered` (first time back at/above that peak,
    null if the series ends still underwater) and `durationSeconds` (start -> recovered,
    null when unrecovered). None with fewer than 2 usable equity points, or none is ever
    underwater (a monotonically non-decreasing curve)."""
    points = _usable_points(trades, equity)
    if len(points) < 2:
        return None

    dd_start = points[0]
    peak_val = points[0][1]
    in_dd, trough, worst = False, None, None

    def _consider(period):
        nonlocal worst
        if worst is None or period["amount"] < worst["amount"]:
            worst = period

    for time, value in points[1:]:
        if value >= peak_val:
            if in_dd and trough is not None:
                _consider({"amount": trough[1] - dd_start[1], "start": dd_start[0],
                          "trough": trough[0], "recovered": time})
            peak_val, dd_start = value, (time, value)
            in_dd, trough = False, None
        else:
            in_dd = True
            if trough is None or value < trough[1]:
                trough = (time, value)

    if in_dd and trough is not None:
        _consider({"amount": trough[1] - dd_start[1], "start": dd_start[0], "trough": trough[0], "recovered": None})

    if worst is None:
        return None
    duration = (worst["recovered"] - worst["start"]).total_seconds() if worst["recovered"] else None
    return {"amount": worst["amount"], "start": _iso(worst["start"]), "trough": _iso(worst["trough"]),
            "recovered": _iso(worst["recovered"]), "durationSeconds": duration}


def time_under_water(trades: list, equity: list | None = None) -> dict | None:
    """Total and longest stretch (seconds) the equity curve spent below its running peak.
    None with fewer than 2 usable equity points."""
    points = _usable_points(trades, equity)
    if len(points) < 2:
        return None
    peak_val, peak_time = points[0][1], points[0][0]
    total = longest = 0.0
    stretch_start = None
    for time, value in points[1:]:
        if value >= peak_val:
            if stretch_start is not None:
                span = (time - stretch_start).total_seconds()
                total += span
                longest = max(longest, span)
                stretch_start = None
            peak_val, peak_time = value, time
        elif stretch_start is None:
            stretch_start = peak_time
    if stretch_start is not None:
        span = (points[-1][0] - stretch_start).total_seconds()
        total += span
        longest = max(longest, span)
    return {"totalSeconds": total, "longestSeconds": longest}


def longest_flat_period(trades: list, equity: list | None = None) -> dict | None:
    """The longest stretch between two consecutive NEW equity highs (or from the last new
    high to the end of the series). Distinct from time_under_water: a curve that stalls
    exactly at its peak without a new drawdown still counts as flat here. None with fewer
    than 2 usable equity points."""
    points = _usable_points(trades, equity)
    if len(points) < 2:
        return None
    peak_val = points[0][1]
    last_high = points[0][0]
    best_start, best_end, best_span = last_high, last_high, 0.0
    for time, value in points[1:]:
        if value > peak_val:
            span = (time - last_high).total_seconds()
            if span > best_span:
                best_start, best_end, best_span = last_high, time, span
            peak_val, last_high = value, time
    tail = (points[-1][0] - last_high).total_seconds()
    if tail > best_span:
        best_start, best_end, best_span = last_high, points[-1][0], tail
    return {"start": _iso(best_start), "end": _iso(best_end), "seconds": best_span}
