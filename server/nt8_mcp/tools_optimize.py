# Portions of this file are derived from cli-nt-bridge
# (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
# MIT License. The full notice is in NOTICE at the repository root.
"""Parameter search in Python over the AddOn's `POST /backtest` plus the
backtest report tool.

There is no optimizer inside NinjaTrader here and no new AddOn surface: `nt_optimize`
expands a parameter grid, runs one ordinary backtest per combination (serially — the
AddOn queues them on its single backtest worker anyway), ranks the finished runs on one
number out of NinjaTrader's own `summary`, and returns the ranked table plus the winner's
full status document. `nt_walkforward` slices the date range in Python, optimizes on each
in-sample window and runs the winner once out-of-sample. `nt_report` turns any status
document into stats and, when matplotlib is installed, a one-page PDF.

Every run goes through the Backtest account, exactly like `nt_backtest`; nothing here
touches a Sim or live account, an order, or the chart.
"""

import itertools
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote

from nt8_mcp import app  # POLL_S is read through the module so tests can patch it
from nt8_mcp import report as _report
from nt8_mcp.app import _addon_delete, _addon_get, _addon_post, mcp

# Status document v1 `summary` keys (addon/NOTES.md). A fitness must name one of these
# (or the derived winLossRatio) — anything else is a typo, not a silent None ranking.
SUMMARY_KEYS = (
    "trades", "winners", "losers", "winRate", "netProfit", "grossProfit", "grossLoss",
    "profitFactor", "commission", "maxDrawdown", "avgTrade", "avgWinner", "avgLoser",
    "largestWinner", "largestLoser", "avgMae", "avgMfe", "avgBarsInTrade", "sharpe",
    "maxConsecWinners", "maxConsecLosers",
)

# NinjaTrader's own fitness names -> the summary key they rank on. Every one is MAXIMISED
# on the raw v1 value: `maxDrawdown` is <= 0 there, so max() is "smallest drawdown".
FITNESS = {
    "MaxNetProfit": "netProfit",
    "MaxProfitFactor": "profitFactor",
    "MinDrawDown": "maxDrawdown",
    "MaxSharpeRatio": "sharpe",
    "MaxPercentProfitable": "winRate",
    "MaxWinLossRatio": "winLossRatio",
}
DERIVED = ("winLossRatio",)


# ---------------------------------------------------------------------------
# grid
# ---------------------------------------------------------------------------

def _steps(lo: float, hi: float, step: float, what: str) -> list:
    """min..max inclusive, with @DefaultOptimizer.cs's own epsilon rule
    (`bin\\Custom\\Optimizers\\@DefaultOptimizer.cs:44,50`): a value is in while
    `min + i*step <= max + step/1e6`. Plain `<= max` silently drops the last point of a
    float grid."""
    values, i = [], 0
    while lo + i * step <= hi + step / 1000000:
        values.append(round(lo + i * step, 10))
        i += 1
        if i > 10000:
            raise ValueError(f"{what}: min/max/step yields more than 10000 values.")
    if not values:
        raise ValueError(f"{what}: max {hi} is below min {lo}.")
    if all(float(v).is_integer() for v in [lo, hi, step]):
        values = [int(v) for v in values]
    return values


def _number(text, what: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: '{text}' is not a number.")


def parse_params(params) -> dict:
    """Expand the parameter spec into `{name: [value, ...]}`, preserving order.

    Accepted: `{"Fast": {"min": 5, "max": 20, "step": 5}}`, `{"Fast": [5, 10, 20]}` (an
    explicit list, which is how a bool/enum/string input is searched), `{"Fast": 10}` (a
    constant), and cli-nt-bridge's string form `"Fast:5:20:5,Slow:40:80:10"` with its own
    validation wording (`cli-nt-bridge: nt8bridge/analyzerrun.py:95-120`)."""
    if isinstance(params, str):
        return _parse_opt_spec(params)
    if not isinstance(params, dict) or not params:
        raise ValueError("params must be {'Name': {'min':..,'max':..,'step':..}} or 'Name:min:max:step[,...]'.")

    grid = {}
    for name, spec in params.items():
        what = f"params['{name}']"
        if not re.match(r"^[A-Za-z_]\w*$", str(name)):
            raise ValueError(f"{what}: '{name}' is not a property name.")
        if isinstance(spec, dict):
            missing = [k for k in ("min", "max", "step") if k not in spec]
            if missing:
                raise ValueError(f"{what} needs min, max and step (missing: {', '.join(missing)}).")
            step = _number(spec["step"], f"{what}: step")
            if step <= 0:
                raise ValueError(f"step width in {what} must be > 0.")
            grid[name] = _steps(_number(spec["min"], f"{what}: min"), _number(spec["max"], f"{what}: max"), step, what)
        elif isinstance(spec, (list, tuple)):
            if not spec:
                raise ValueError(f"{what}: the value list is empty.")
            grid[name] = list(spec)
        else:
            grid[name] = [spec]
    return grid


def _parse_opt_spec(spec: str) -> dict:
    """cli-nt-bridge's `--opt=Name:min:max:step[,...]`, their wording kept verbatim."""
    grid = {}
    for part in [p for p in (spec or "").split(",") if p.strip()]:
        f = part.split(":")
        if len(f) != 4:
            raise ValueError("--opt entry '%s' needs 4 fields Name:min:max:step." % part)
        name = f[0].strip()
        if not re.match(r"^[A-Za-z_]\w*$", name):
            raise ValueError("--opt entry '%s': '%s' is not a property name." % (part, name))
        try:
            step = float(f[3].strip())
        except ValueError:
            raise ValueError("--opt entry '%s': step '%s' is not a number." % (part, f[3].strip()))
        if step <= 0:
            raise ValueError("step width in '%s' must be > 0." % part)
        grid[name] = _steps(_number(f[1].strip(), "--opt entry '%s': min" % part),
                            _number(f[2].strip(), "--opt entry '%s': max" % part), step, "--opt entry '%s'" % part)
    if not grid:
        raise ValueError("needs --opt=Name:min:max:step[,...]")
    return grid


def _combos(grid: dict) -> list:
    names = list(grid)
    return [dict(zip(names, values)) for values in itertools.product(*(grid[n] for n in names))]


# ---------------------------------------------------------------------------
# fitness
# ---------------------------------------------------------------------------

def _fitness_key(fitness: str) -> str:
    key = FITNESS.get(fitness, fitness)
    if key not in SUMMARY_KEYS and key not in DERIVED:
        raise ValueError(
            f"unknown fitness '{fitness}'. Use one of {', '.join(sorted(FITNESS))}, "
            f"any status-document summary key ({', '.join(SUMMARY_KEYS)}), or winLossRatio.")
    return key


def _score(summary: dict, key: str):
    """The ranking number, or None when this run cannot be ranked (null in the summary —
    e.g. profitFactor with no losing trade, which NinjaTrader reports as Infinity)."""
    if not summary:
        return None
    if key == "winLossRatio":
        win, loss = summary.get("avgWinner"), summary.get("avgLoser")
        if win is None or not loss:
            return None
        return abs(float(win)) / abs(float(loss))
    value = summary.get(key)
    return None if value is None or isinstance(value, bool) else float(value)


# ---------------------------------------------------------------------------
# cost pass-through
# ---------------------------------------------------------------------------
# Same names/semantics as nt_backtest (server/nt8_mcp/tools_backtest.py): read its
# signature, never edited here (a separate module owns that file).

def _cost_body(slippage_ticks, commission_template, include_commission, fill_resolution,
              fill_resolution_type, fill_resolution_value, fill_limit_on_touch) -> dict:
    """The exact body keys nt_backtest sends for these settings, so a grid combo and a
    plain nt_backtest of the same inputs are configured identically."""
    body = {}
    if fill_resolution:
        body["fillResolution"] = fill_resolution
    if fill_resolution_type:
        body["fillResolutionType"] = fill_resolution_type
    if fill_resolution_value:
        body["fillResolutionValue"] = fill_resolution_value
    if slippage_ticks is not None:
        body["slippageTicks"] = slippage_ticks
    if commission_template:
        body["commissionTemplate"] = commission_template
    if include_commission is not None:
        body["includeCommission"] = include_commission
    if fill_limit_on_touch is not None:
        body["fillLimitOnTouch"] = fill_limit_on_touch
    return body


def _costs_echo(slippage_ticks, commission_template, include_commission, fill_resolution,
                fill_resolution_type, fill_resolution_value, fill_limit_on_touch) -> dict:
    """The cost settings used by every inner backtest call, echoed back — never guessed,
    never recomputed. All null/empty means nothing was modelled, and the result says so
    plainly instead of leaving a consumer to assume a gross number includes costs."""
    out = {
        "slippageTicks": slippage_ticks,
        "commissionTemplate": commission_template or None,
        "includeCommission": include_commission,
        "fillResolution": fill_resolution or None,
        "fillResolutionType": fill_resolution_type or None,
        "fillResolutionValue": fill_resolution_value or None,
        "fillLimitOnTouch": fill_limit_on_touch,
    }
    if all(v is None for v in out.values()):
        out["note"] = "gross: no slippage or commission modelled"
    return out


# ---------------------------------------------------------------------------
# one backtest
# ---------------------------------------------------------------------------

def _body(strategy, chart, instrument, bars_period, from_date, to_date, tick_replay, inputs, costs=None):
    body = {"strategy": strategy, "from": from_date, "to": to_date, "tickReplay": bool(tick_replay)}
    # Same condition as nt_backtest: send `chart` whenever it is truthy. The AddOn's
    # explicit instrument/barsPeriod already override the chart's seed values
    # (NT8Bridge.Backtest.cs:393-418) — dropping chart here just silently lost its
    # TradingHours/ResetOnNewTradingDay, which nothing else supplies.
    if chart:
        body["chart"] = chart
    if instrument:
        body["instrument"] = instrument
    if bars_period:
        body["barsPeriod"] = bars_period
    if inputs:
        body["inputs"] = inputs
    if costs:
        body.update(costs)
    # Every inner combo is disposable (ranked, then usually DELETEd) and never touches the
    # run registry: this posts straight to the AddOn's /backtest, never through
    # tools_backtest.nt_backtest() — the only place that ever calls runs.save_run() — so
    # there is no save_run/"saveRun" body key to send here (the AddOn has no such field
    # either). If _optimize() is ever changed to call nt_backtest() directly, pass its
    # Python-level save_run=False argument there instead of reintroducing a JSON body key.
    return body


def _run_backtest(body: dict, wait_s: int) -> dict:
    """POST /backtest and poll until terminal. Returns the status document, or the
    AddOn's `{"error":...}` refusal when the job was never queued (no `id` came back
    from the POST). A transport error on a POLL — the job is already queued inside
    NT8 — never overwrites the last known status: it is kept, `id` stays present, and
    the failure is attached as `pollError` so the caller can recover with
    nt_backtest_status(id) instead of the whole grid being mistaken for a refusal."""
    status = _addon_post("/backtest", body)
    if "id" not in status:
        return status
    job_id = status["id"]
    deadline = time.time() + wait_s
    # Back off from 0.1 s up to POLL_S. Observed on NinjaTrader 8.1.8.2: a 5-minute-ES combo finishes in ~0.03 s, so a
    # flat POLL_S (2 s) sleep made a 9-combo grid take 18.3 s for 0.2 s of NinjaTrader work.
    delay = 0.1
    while status.get("state") in ("queued", "running") and time.time() < deadline:
        time.sleep(min(delay, app.POLL_S))
        delay *= 2
        polled = _addon_get(f"/backtest/{quote(str(job_id))}")
        if "id" not in polled:
            status = dict(status)
            status["pollError"] = polled.get("error", polled)
            break
        status = polled
    if status.get("state") in ("queued", "running"):
        status = dict(status)
        status["note"] = f"still running after wait_s={wait_s}s; call nt_backtest_status({job_id!r})"
    return status


def _never_ran(doc: dict) -> bool:
    """True for a run that cannot be ranked as a real result: any non-`done` terminal
    state (error/timeout/cancelled), or a `done` state whose own `barsFrom` is null —
    NinjaTrader's own signal (addon/NOTES.md "Status document v1") that the strategy
    never actually started (an unhandled modal, a refused fill mode, an unresolved
    instrument). Both look identical to a legitimate zero-trade
    result unless this is checked explicitly. A document with no `barsFrom` key at all
    (a `nt_backtest` build that predates it) is not flagged — absence of the field is
    not the same claim as an explicit null."""
    if doc.get("state") != "done":
        return True
    return "barsFrom" in doc and doc.get("barsFrom") is None


def _never_ran_reason(doc: dict) -> str:
    """Assumes `_never_ran(doc)` is true. The sentence for a row's/window's `skipped`/`error`."""
    if doc.get("state") != "done":
        return f"state={doc.get('state')}"
    return "state done but barsFrom is null — the strategy never started"


def _window_problem(doc: dict):
    """A sentence when NinjaTrader backtested a DIFFERENT window than the one asked for, else None.

    Observed on NinjaTrader 8.1.8.2: with a Playback connection connected and parked at a fixed
    clock, every run ends at the replay clock and keeps only its LENGTH — a request for one date
    range can come back with the trades of an earlier range of the same length, while the returned
    document still echoes the dates that were asked for. A session opens the evening before `from`,
    so one day of slack is normal; more is a different window."""
    if doc.get("warnings"):  # the AddOn now says so itself (barsFrom/barsTo + warnings); trades are the fallback
        return f"{doc.get('id')}: " + " ".join(str(w) for w in doc["warnings"])
    trades = [t for t in (doc.get("trades") or []) if t.get("entryTime") and t.get("exitTime")]
    if not trades or not doc.get("from") or not doc.get("to"):
        return None
    try:
        first, last = _day(trades[0]["entryTime"]), _day(trades[-1]["exitTime"])
        start, end = _day(doc["from"]), _day(doc["to"])
    except ValueError:
        return None
    if first >= start - timedelta(days=1) and last <= end + timedelta(days=1):
        return None
    return (f"{doc.get('id')}: asked for {doc['from']}..{doc['to']} but the trades run "
            f"{trades[0]['entryTime']}..{trades[-1]['exitTime']} — NinjaTrader backtested a different window "
            "(it does this while the Playback connection is connected: bars end at the replay clock). "
            "These numbers are NOT for the dates requested.")


def _brief(warnings: list) -> list:
    """One sentence per run is noise on a 200-combo grid: the first three, then a count."""
    return warnings[:3] + ([f"... and {len(warnings) - 3} more runs like these"] if len(warnings) > 3 else [])


_INT_TYPES = {"Byte", "SByte", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"}


def _int_input_problem(strategy: str, grid: dict, inputs: dict | None):
    """A sentence when a fractional value is headed for an integer input, else None (also None when
    GET /strategies cannot say). The AddOn coerces with Convert.ChangeType, which ROUNDS and still
    echoes the fraction: observed on NinjaTrader 8.1.8.2, Fast=5.5 and Fast=6.0 gave the same netProfit and the
    5.5 document said `"inputs":{"Fast":5.5}` — a silently different run."""
    listing = _addon_get("/strategies")
    if not isinstance(listing, list):
        return None
    types = next((s.get("inputs") or {} for s in listing if s.get("name") == strategy), {})
    candidates = {name: [value] for name, value in (inputs or {}).items()}
    candidates.update(grid)
    for name, values in candidates.items():
        kind = (types.get(name) or {}).get("type")
        bad = [v for v in values if isinstance(v, float) and not v.is_integer()]
        if kind in _INT_TYPES and bad:
            return (f"'{name}' has fractional values {bad[:3]} but {strategy}.{name} is {kind} — "
                    "NinjaTrader would round them silently. Nothing was run.")
    return None


def _dates(from_date: str, to_date: str) -> tuple:
    to_date = to_date or datetime.now().strftime("%Y-%m-%d")
    from_date = from_date or (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    return from_date, to_date


def _day(text: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(text)[:19], fmt)
        except ValueError:
            continue
    raise ValueError(f"'{text}' is not a date (YYYY-MM-DD).")


# ---------------------------------------------------------------------------
# the grid loop
# ---------------------------------------------------------------------------

def _optimize(strategy, grid, key, *, chart, instrument, bars_period, from_date, to_date,
              tick_replay, inputs, top_n, min_trades, wait_s, costs) -> dict:
    """Run every combination serially and rank the ones that finished. Returns
    `{rows, best, ran, errors}`; `best` is the winner's full status document."""
    rows, errors, warnings, best_doc, best_score = [], [], [], None, None
    for combo in _combos(grid):
        merged = dict(inputs or {})
        merged.update(combo)
        doc = _run_backtest(_body(strategy, chart, instrument, bars_period, from_date, to_date,
                                  tick_replay, merged, costs), wait_s)
        if "error" in doc and "id" not in doc:
            # a refusal, not a run (bad strategy, bad instrument, unknown input): every
            # remaining combination would be refused the same way, so stop here.
            return {"rows": rows, "best": None, "ran": len(rows), "ranked": 0, "errors": errors,
                    "warnings": warnings, "refused": doc["error"], "refusedInputs": combo}
        problem = _window_problem(doc)
        if problem:
            warnings.append(problem)
        summary = doc.get("summary")
        score = _score(summary, key)
        skipped = None
        if _never_ran(doc):
            # a job that never ran cannot say "done" — never let this be mistaken for a
            # legitimate zero-trade/zero-profit run.
            skipped = doc.get("error") or doc.get("pollError") or _never_ran_reason(doc)
            errors.append({"inputs": combo, "id": doc.get("id"), "error": skipped})
        elif summary and (summary.get("trades") or 0) < min_trades:
            skipped = f"only {summary.get('trades') or 0} trades (min_trades={min_trades})"
        elif score is None:
            skipped = f"summary.{key} is null"
        rows.append({"id": doc.get("id"), "state": doc.get("state"), "inputs": combo,
                     "fitness": score, "summary": summary, "skipped": skipped,
                     "seconds": doc.get("seconds")})
        if skipped is None and (best_score is None or score > best_score):
            best_score, best_doc = score, doc  # only the winner keeps its trades[]

    ranked = sorted([r for r in rows if r["skipped"] is None], key=lambda r: r["fitness"], reverse=True)
    for i, row in enumerate(ranked, 1):
        row["rank"] = i
    out_rows = ranked[:top_n] + [r for r in rows if r["skipped"] is not None]

    # The AddOn keeps every job in a static in-process dict until DELETEd (addon/NOTES.md
    # "the jobs dictionary only shrinks on DELETE"). A grid the caller will never read the
    # rest of is otherwise unbounded heap growth in the live NinjaTrader process — delete
    # every combo that isn't the winner and isn't a row we're actually returning.
    keep_ids = {r["id"] for r in out_rows if r.get("id")}
    if best_doc and best_doc.get("id"):
        keep_ids.add(best_doc["id"])
    for row in rows:
        rid = row.get("id")
        if rid and rid not in keep_ids:
            try:
                _addon_delete(f"/backtest/{quote(str(rid))}")
            except Exception:
                pass  # best-effort cleanup; a 404/unreachable AddOn must not abort the grid

    return {"rows": out_rows, "best": best_doc, "ran": len(rows), "ranked": len(ranked), "errors": errors,
            "warnings": warnings}


@mcp.tool(name="nt_optimize")
def nt_optimize(
    strategy: str,
    params: dict | str,
    chart: str = "first",
    instrument: str = "",
    bars_period: dict | None = None,
    from_date: str = "",
    to_date: str = "",
    tick_replay: bool = False,
    inputs: dict | None = None,
    fill_resolution: str = "",
    fill_resolution_type: str = "",
    fill_resolution_value: int = 0,
    slippage_ticks: float | None = None,
    commission_template: str = "",
    include_commission: bool | None = None,
    fill_limit_on_touch: bool | None = None,
    fitness: str = "netProfit",
    top_n: int = 10,
    min_trades: int = 5,
    max_combos: int = 200,
    max_runs: int = 0,
    wait_s: int = 600,
    include_trades: bool = False,
):
    """Grid-search a strategy's inputs by running one ordinary backtest per combination and ranking the results.
    params is {"Fast":{"min":5,"max":20,"step":5}} (inclusive, NinjaTrader's own epsilon step rule), or
    {"Fast":[5,10,20]} for an explicit list, or the string form "Fast:5:20:5,Slow:40:80:10". fitness names one
    status-document summary key (netProfit, profitFactor, sharpe, winRate, maxDrawdown, ...) or a NinjaTrader
    fitness name (MaxNetProfit, MinDrawDown, MaxSharpeRatio, ...); every one is maximised on NinjaTrader's own
    number and nothing is recomputed here. fill_resolution/fill_resolution_type/fill_resolution_value/
    slippage_ticks/commission_template/include_commission/fill_limit_on_touch are passed straight through to
    EVERY inner backtest, same names and semantics as nt_backtest; the response's "costs" object echoes exactly
    what was used and says "gross: no slippage or commission modelled" when none of them was set — a run with no
    cost settings is gross, not free of cost. Combinations run SERIALLY on the Backtest account (the AddOn has
    one backtest worker), so cost is combos x one backtest — roughly 0.7 s for 8 days of 5-minute ES, 5 s for 2
    days of tick-replay Renko. If the grid is larger than max_combos (alias max_runs) NOTHING is run: you get the
    combination count back and choose. Returns {combos, ran, costs, rows:[{rank,inputs,fitness,summary,id}],
    best:<the winner's full status doc>, errors}. include_trades (default False) controls whether "best" keeps
    its trades[] — set it True to get them; a combo that never really ran (state error/timeout/cancelled, or a
    "done" job whose own barsFrom came back null — NinjaTrader raised a modal, refused the fill mode, or the
    instrument never resolved) is listed with a skip reason in "errors" and can never win, even though its
    summary would otherwise read as a legitimate zero-trade result. Rows with fewer than min_trades trades are
    listed but never win. Every combo's backtest is DELETEd from the AddOn afterwards except the winner and the
    rows actually returned (top_n plus every skipped/errored row) — the AddOn keeps jobs until DELETEd, so a grid
    the caller never rereads would otherwise leak into NinjaTrader's live process. Never touches a Sim or live
    account, an order, or the chart."""
    try:
        grid = parse_params(params)
        key = _fitness_key(fitness)
    except ValueError as e:
        return {"error": str(e)}

    total = 1
    for values in grid.values():
        total *= len(values)
    cap = max_runs or max_combos
    if total > cap:
        return {"error": f"{total} combinations exceeds max_combos={cap} — nothing was run. "
                         f"Narrow the grid or raise max_combos.",
                "combos": total, "maxCombos": cap, "grid": grid, "ran": 0}
    problem = _int_input_problem(strategy, grid, inputs)
    if problem:
        return {"error": problem, "combos": total, "grid": grid, "ran": 0}

    from_date, to_date = _dates(from_date, to_date)
    costs = _cost_body(slippage_ticks, commission_template, include_commission, fill_resolution,
                       fill_resolution_type, fill_resolution_value, fill_limit_on_touch)
    result = _optimize(strategy, grid, key, chart=chart, instrument=instrument, bars_period=bars_period,
                       from_date=from_date, to_date=to_date, tick_replay=tick_replay, inputs=inputs,
                       top_n=top_n, min_trades=min_trades, wait_s=wait_s, costs=costs)
    if not include_trades and result.get("best"):
        result["best"] = dict(result["best"], trades=None)
    out = {"strategy": strategy, "fitness": fitness, "fitnessKey": key, "grid": grid,
           "combos": total, "from": from_date, "to": to_date, "minTrades": min_trades,
           "costs": _costs_echo(slippage_ticks, commission_template, include_commission, fill_resolution,
                                fill_resolution_type, fill_resolution_value, fill_limit_on_touch),
           **result}
    out["warnings"] = _brief(out["warnings"])
    if "refused" in out:
        out["error"] = f"the AddOn refused the backtest: {out['refused']} (stopped after {out['ran']} runs)"
    return out


@mcp.tool(name="nt_walkforward")
def nt_walkforward(
    strategy: str,
    params: dict | str,
    train_days: int = 30,
    test_days: int = 10,
    anchored: bool = False,
    from_date: str = "",
    to_date: str = "",
    chart: str = "first",
    instrument: str = "",
    bars_period: dict | None = None,
    tick_replay: bool = False,
    inputs: dict | None = None,
    fill_resolution: str = "",
    fill_resolution_type: str = "",
    fill_resolution_value: int = 0,
    slippage_ticks: float | None = None,
    commission_template: str = "",
    include_commission: bool | None = None,
    fill_limit_on_touch: bool | None = None,
    fitness: str = "netProfit",
    min_trades: int = 5,
    max_combos: int = 200,
    max_runs: int = 0,
    max_backtests: int = 200,
    wait_s: int = 600,
    optimization_period_days: int = 0,
    test_period_days: int = 0,
    include_trades: bool = False,
):
    """Walk-forward test: slice from_date..to_date into in-sample/out-of-sample windows, optimize on each
    in-sample window and run the winning inputs ONCE out-of-sample. train_days/test_days are window lengths in
    days (also spelled optimization_period_days/test_period_days; both are accepted). anchored
    pins every in-sample window's start to from_date; rolling (the default) slides it forward by test_days.
    fill_resolution/fill_resolution_type/fill_resolution_value/slippage_ticks/commission_template/
    include_commission/fill_limit_on_touch are passed straight through to EVERY inner backtest (in-sample and
    out-of-sample alike), same names and semantics as nt_backtest; the response's "costs" object echoes exactly
    what was used and says "gross: no slippage or commission modelled" when none of them was set. An inner run
    that ended in state error/timeout/cancelled, or a "done" run whose own barsFrom came back null (NinjaTrader
    never actually started it), is never ranked as a zero-profit result and is reported as an error row instead
    — both in a window's own entry and in the compact "table". Total backtests = windows x (combinations + 1)
    and they run serially — the grid alone is refused up front if it exceeds max_combos, and the FULL total
    (windows x (combos + 1)) is refused up front if it exceeds max_backtests (default 200, same order as
    max_combos): nothing is queued, you get {windows, combos, backtests, ran:0} back so you can narrow the date
    range or raise the cap, the same shape nt_optimize uses for its own refusal. include_trades (default False)
    keeps the top-level "outOfSample" stitched trade list empty ([]) unless set True — the trades then appear
    ONCE there, never duplicated per window. Returns {costs, table:[{window, inSample:{from,to},
    outOfSample:{from,to}, inputs, inSampleFitness, outOfSampleNetProfit, outOfSampleTrades, state, error}] (one
    row per window, ALWAYS present, compact, no trades — read this first), windows:[{window,
    inSample:{from,to,inputs,fitness}, outOfSample:{from,to,summary,id,error}}] (the detailed per-window record),
    outOfSample:{summary,trades,...}} where the last object stitches every out-of-sample trade together in date
    order; its summary is SUMMED in Python across windows and is not a NinjaTrader performance report — the
    per-window summaries are. Backtest account only."""
    train_days = optimization_period_days or train_days
    test_days = test_period_days or test_days
    try:
        grid = parse_params(params)
        key = _fitness_key(fitness)
        from_date, to_date = _dates(from_date, to_date)
        start, end = _day(from_date), _day(to_date)
    except ValueError as e:
        return {"error": str(e)}
    if train_days < 1 or test_days < 1:
        return {"error": "train_days and test_days must both be >= 1."}

    total = 1
    for values in grid.values():
        total *= len(values)
    cap = max_runs or max_combos
    if total > cap:
        return {"error": f"{total} combinations exceeds max_combos={cap} — nothing was run.",
                "combos": total, "maxCombos": cap, "grid": grid}

    windows, k = [], 0
    while True:
        is_end = start + timedelta(days=train_days + k * test_days)
        oos_end = is_end + timedelta(days=test_days)
        if oos_end > end + timedelta(days=1):
            break
        is_start = start if anchored else start + timedelta(days=k * test_days)
        windows.append((is_start, is_end, oos_end))
        k += 1
    if not windows:
        return {"error": f"{(end - start).days + 1} days from {from_date} to {to_date} is not enough for one "
                         f"train_days={train_days} + test_days={test_days} window.",
                "windows": []}

    n_backtests = len(windows) * (total + 1)
    if n_backtests > max_backtests:
        return {"error": f"{len(windows)} windows x ({total} combos + 1) = {n_backtests} backtests exceeds "
                         f"max_backtests={max_backtests} — nothing was run. Narrow the date range or raise "
                         f"max_backtests.",
                "windows": len(windows), "combos": total, "backtests": n_backtests, "ran": 0}
    problem = _int_input_problem(strategy, grid, inputs)
    if problem:
        return {"error": problem, "windows": len(windows), "combos": total, "ran": 0}

    costs = _cost_body(slippage_ticks, commission_template, include_commission, fill_resolution,
                       fill_resolution_type, fill_resolution_value, fill_limit_on_touch)
    costs_echo = _costs_echo(slippage_ticks, commission_template, include_commission, fill_resolution,
                             fill_resolution_type, fill_resolution_value, fill_limit_on_touch)

    fmt = "%Y-%m-%d"
    out_windows, out_table, oos_trades, oos_docs, warnings = [], [], [], [], []
    for i, (is_start, is_end, oos_end) in enumerate(windows, 1):
        # in-sample window ends the day before the out-of-sample window starts
        is_to = (is_end - timedelta(days=1)).strftime(fmt)
        oos_from, oos_to = is_end.strftime(fmt), (oos_end - timedelta(days=1)).strftime(fmt)
        opt = _optimize(strategy, grid, key, chart=chart, instrument=instrument, bars_period=bars_period,
                        from_date=is_start.strftime(fmt), to_date=is_to, tick_replay=tick_replay,
                        inputs=inputs, top_n=1, min_trades=min_trades, wait_s=wait_s, costs=costs)
        if opt.get("refused"):
            return {"error": f"the AddOn refused the backtest: {opt['refused']} (window {i})",
                    "windows": out_windows, "table": out_table}
        warnings += opt["warnings"]
        winner = opt["rows"][0] if opt["rows"] and opt["rows"][0]["skipped"] is None else None
        row = {"window": i,
               "inSample": {"from": is_start.strftime(fmt), "to": is_to,
                            "inputs": winner["inputs"] if winner else None,
                            "fitness": winner["fitness"] if winner else None,
                            "ranked": opt["ranked"]},
               "outOfSample": None}
        if winner is None:
            row["note"] = f"no in-sample combination qualified (min_trades={min_trades}); window skipped"
            out_windows.append(row)
            out_table.append({"window": i, "inSample": {"from": is_start.strftime(fmt), "to": is_to},
                              "outOfSample": {"from": oos_from, "to": oos_to}, "inputs": None,
                              "inSampleFitness": None, "outOfSampleNetProfit": None, "outOfSampleTrades": None,
                              "state": "skipped", "error": row["note"]})
            continue

        merged = dict(inputs or {})
        merged.update(winner["inputs"])
        doc = _run_backtest(_body(strategy, chart, instrument, bars_period, oos_from, oos_to,
                                  tick_replay, merged, costs), wait_s)
        if "error" in doc and "id" not in doc:
            return {"error": f"the AddOn refused the out-of-sample backtest: {doc['error']} (window {i})",
                    "windows": out_windows, "table": out_table}
        oos_never_ran = _never_ran(doc)
        oos_error = doc.get("error") or (_never_ran_reason(doc) if oos_never_ran else None)
        row["outOfSample"] = {"from": oos_from, "to": oos_to, "id": doc.get("id"),
                              "state": doc.get("state"), "inputs": merged,
                              "summary": doc.get("summary"), "error": oos_error}
        out_windows.append(row)
        oos_summary = doc.get("summary") or {}
        out_table.append({"window": i, "inSample": {"from": is_start.strftime(fmt), "to": is_to},
                          "outOfSample": {"from": oos_from, "to": oos_to}, "inputs": winner["inputs"],
                          "inSampleFitness": winner["fitness"],
                          "outOfSampleNetProfit": None if oos_never_ran else oos_summary.get("netProfit"),
                          "outOfSampleTrades": None if oos_never_ran else oos_summary.get("trades"),
                          "state": "error" if oos_never_ran else "done", "error": oos_error})
        problem = _window_problem(doc)
        if problem:
            warnings.append(problem)
        if doc.get("state") == "done" and not oos_never_ran:
            oos_docs.append(doc)
            for trade in doc.get("trades") or []:
                stitched = dict(trade)
                stitched["window"] = i
                stitched["n"] = len(oos_trades)
                oos_trades.append(stitched)

    return {"strategy": strategy, "fitness": fitness, "fitnessKey": key, "grid": grid, "combos": total,
            "from": from_date, "to": to_date, "trainDays": train_days, "testDays": test_days,
            "anchored": bool(anchored), "minTrades": min_trades, "costs": costs_echo,
            "table": out_table,
            "windows": out_windows,
            "outOfSample": _stitch(strategy, oos_docs, oos_trades, include_trades),
            "warnings": _brief(warnings)}


def _stitch(strategy: str, docs: list, trades: list, include_trades: bool = False) -> dict:
    """A status-document-SHAPED object for the concatenated out-of-sample runs. Its
    `summary` is summed here, not read from NinjaTrader — the per-window summaries are
    the authoritative ones. `trades` is [] unless include_trades — the caller already has
    every trade once in `windows[].outOfSample` if they want the per-window detail, so
    this is the single place the full stitched list appears."""
    def total(key):
        values = [d.get("summary", {}).get(key) for d in docs]
        values = [v for v in values if v is not None]
        return sum(values) if values else None

    n = total("trades") or 0
    gross_profit, gross_loss, net = total("grossProfit"), total("grossLoss"), total("netProfit")
    winners = total("winners")
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for trade in trades:
        pnl = trade.get("pnl")
        if pnl is None:
            continue
        equity += float(pnl)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "strategy": strategy, "state": "done" if docs else "empty", "windows": len(docs),
        "summary": {
            "trades": n, "winners": winners, "losers": total("losers"),
            "winRate": (winners / n) if (n and winners is not None) else 0,
            "netProfit": net, "grossProfit": gross_profit, "grossLoss": gross_loss,
            "profitFactor": (gross_profit / abs(gross_loss)) if (gross_profit is not None and gross_loss) else None,
            "commission": total("commission"),
            "maxDrawdown": max_dd,
            "avgTrade": (net / n) if (n and net is not None) else None,
        },
        "trades": trades if include_trades else [],
        "note": "summed in Python across the out-of-sample windows; maxDrawdown is from the stitched "
                "equity curve. Not a NinjaTrader performance summary — see windows[].outOfSample.summary.",
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

@mcp.tool(name="nt_report")
def nt_report(id: str = "", status_doc: dict | None = None, pdf_path: str = ""):
    """Stats — and optionally a one-page PDF — for a finished backtest: equity curve, drawdown, KPI tiles.
    Pass id ("b3", or "b1,b2,b3" for a batch page) to fetch the status document(s) from the AddOn, or pass
    status_doc directly (anything status-document v1 shaped, including nt_walkforward's stitched outOfSample
    object, which needs no AddOn at all). NinjaTrader's own summary numbers are used as they are; only the
    equity curve and the underwater drawdown, which the status document does not carry, are derived from
    trades[].pnl. pdf_path writes the PDF there; without it you get stats only. matplotlib is optional — if it
    is not installed you still get the stats plus a note saying so, never an error. Returns
    {stats, assessment, table, pdf, note}."""
    docs = []
    if status_doc is not None:
        docs = [status_doc]
    elif id:
        for one in [s.strip() for s in str(id).split(",") if s.strip()]:
            doc = _addon_get(f"/backtest/{quote(one)}")
            # every status document carries an `error` key (null unless it failed), so the
            # refusal test is the missing id, not the presence of `error`
            if "id" not in doc:
                return {"error": f"{one}: {doc.get('error', doc)}"}
            docs.append(doc)
    if not docs:
        return {"error": "pass id (e.g. 'b3', or 'b1,b2') or status_doc."}

    stats = [_report.compute_stats(d) for d in docs]
    out = {
        "stats": [{k: v for k, v in s.items() if not isinstance(v, list)} for s in stats],
        "assessment": [_report.assess(d.get("summary") or {}) for d in docs],
        "table": _report.format_metrics_table(docs[0].get("summary") or {}),
        "pdf": None,
        "note": None,
    }
    if len(docs) == 1:
        out["stats"] = out["stats"][0]
        out["assessment"] = out["assessment"][0]
    if not pdf_path:
        out["note"] = "no pdf_path given — stats only."
        return out
    try:
        out["pdf"] = (_report.render_pdf(docs[0], pdf_path) if len(docs) == 1
                      else _report.render_batch_pdf(docs, pdf_path))
    except RuntimeError as e:          # matplotlib missing: stats are still the answer
        out["note"] = str(e)
    except Exception as e:             # a bad path, a locked file: say so, keep the stats
        out["note"] = f"PDF not written: {type(e).__name__}: {e}"
    return out
