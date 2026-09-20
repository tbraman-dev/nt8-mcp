# Portions of this file are derived from cli-nt-bridge
# (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
# MIT License. The full notice is in NOTICE at the repository root.
# (Derived: PlaybackState.clock_unset / ready_to_seek and their reasoning, nt8bridge/playback.py:60-110.)
"""Market Replay transport: read it (`nt_playback`) and drive it (`nt_playback_seek`,
`nt_playback_speed`, `nt_playback_run` + status/cancel). Contract: ../../docs/api/playback.md.

The read side (`GET /playback`) never connects, disconnects, seeks or changes the replay speed —
that is unchanged. The write side is opt-in: every one of the five write tools below is refused by
the AddOn unless `orders.enabled` is armed (the SAME file the order module reads), the Playback
connection is already Connected (nothing here ever connects/disconnects it), no non-Playback
account holds a position or a working order, and no modal dialog is open. There is no HMAC confirm
on these calls — moving a replay clock is not an order — but every armed call is one line in
NinjaTrader's `nt8mcp\\playback.jsonl`.
"""

from urllib.parse import quote

from nt8_mcp import app
from nt8_mcp.app import _addon_delete, _addon_get, _addon_post, mcp

# NT's "no replay data loaded" sentinel: a connected transport with nothing loaded reads back
# 2099-12-01. It is stationary, so it passes a naive moving/not-moving test as READY — the exact
# false green that cost cli-nt-bridge a live run (their CHANGELOG 1.5.0, Tests).
_UNSET_YEAR = "2090"


def _clock_unset(clock_est) -> bool:
    return bool(clock_est) and str(clock_est)[:4] >= _UNSET_YEAR


def _verdict(d: dict) -> tuple[str, bool, str]:
    """(state, ready, why). `ready` means: a seek from this transport can be trusted.

    Kept in exactly one place — the AddOn deliberately reports facts and no verdict.
    """
    if not d.get("transportResolved"):
        return ("unresolved", False,
                "NinjaTrader.Adapter.PlaybackAdapter did not resolve — this NT8 version moved it. "
                "See GET /compat; every playback field is null, which is 'unknown', not 'parked'.")
    if not (d.get("connection") or {}).get("connected"):
        return ("disconnected", False, "the Playback connection is not connected")
    if _clock_unset(d.get("clockEst")):
        return ("empty", False,
                f"the replay clock reads {d.get('clockEst')}, NT's 'no replay data loaded' sentinel — "
                "connected is not the same as loaded")
    if d.get("moving") is None:
        return ("unknown", False, "the replay clock could not be read, so parked and running cannot be told apart")
    if d.get("moving"):
        return ("running", False, f"the replay clock is advancing (now {d.get('clockEst')}) — playback is running")
    if not d.get("coverageScanned"):
        return ("parked", False,
                f"transport parked at {d.get('clockEst')}, but the replay store was not scanned "
                "(pass instrument=... or coverage=True) — an unscanned store is not an empty one")
    if d.get("coverageTruncated"):
        return ("parked", False,
                f"the replay store scan hit its {d.get('coverageBudgetSec')}s budget and stopped "
                "early — the store is not proven empty, re-run with a larger budget_s or name one "
                "instrument")
    if not any((c or {}).get("readable", 0) for c in (d.get("coverage") or [])):
        return ("parked", False, "no readable .nrd files on disk — there is nothing to replay")
    why = f"transport parked at {d.get('clockEst')} with readable replay data on disk"
    clock = str(d.get("clockEst") or "")
    days = [day for c in (d.get("coverage") or []) for day in ((c or {}).get("days") or []) if day.get("readable")]
    if not any(str(day.get("from")) <= clock <= str(day.get("to")) for day in days):
        # Observed: slider range 09-10..09-17, first recorded .nrd 09-11 — the clock sat on a day with no data.
        why += (" — but no readable .nrd covers that clock time: the Playback range is what a human typed, "
                "not what is recorded (see coverage[].days)")
    return ("parked", True, why)


@mcp.tool(name="nt_playback")
def nt_playback(instrument: str = "", coverage: bool = False, budget_s: int = 20) -> dict:
    """Read NinjaTrader's Market Replay transport: whether the Playback connection is connected, whether
    replay data is actually loaded, and whether the replay clock is parked or running — plus, on request,
    what the `.nrd` files in the replay store really cover. Returns the AddOn's fields (transportResolved,
    connection{status,connected}, clockEstFirst/clockEst (US Eastern, not local and not UTC), sampleMs,
    movingSec, moving, speed, maxSpeedValue, fromEst, toEst, isSourceHistoricalData, resolved{member->bool},
    coverageScanned, coverageTruncated, coverage[]) plus `state` (unresolved|disconnected|empty|running|
    parked|unknown), `ready` and `why`. The clock is sampled twice ~1100 ms apart because one reading cannot
    tell a parked transport from a running one; a connected transport with nothing loaded reads clockEst
    2099-12-01 and is reported as `empty`, never as ready. Coverage is opt-in and comes from NinjaTrader's
    own .nrd reader, not from the Playback slider whose bounds are only the connection range a human typed:
    name an `instrument` (one folder under db\\replay, e.g. "ES 12-26" or "ES ##-##") for that one, or pass
    coverage=True for every instrument, which is slow and stops at `budget_s` seconds with
    coverageTruncated true; coverageScanned false means the store was not looked at, so an empty coverage
    list is then "not looked", never "nothing there". Precondition: NT8 open with the AddOn running;
    Playback does NOT have to be connected — a disconnected transport is a valid answer. This call never
    connects, disconnects, seeks, changes the replay speed or starts a replay: there is no write side.
    """
    # _addon_get's default deadline is 5 s and a coverage scan runs far past it, so this ONE call gets its
    # own. A per-call deadline never touches the process-wide app.HTTP_TIMEOUT, so overlapping scans cannot
    # stomp each other (which is why the lock this used to need is gone).
    if instrument or coverage:
        result = _addon_get(
            "/playback",
            _timeout=max(app.HTTP_TIMEOUT, int(budget_s) + 10),
            instrument=instrument or None,
            coverage=1 if coverage else None,
            budgetSec=int(budget_s),
        )
    else:
        result = _addon_get("/playback")

    if not isinstance(result, dict) or "error" in result:
        return result
    state, ready, why = _verdict(result)
    result["state"] = state
    result["ready"] = ready
    result["why"] = why
    return result


# ---------------------------------------------------------------------------
# write side — seek / speed / run
# ---------------------------------------------------------------------------

@mcp.tool(name="nt_playback_seek")
def nt_playback_seek(time: str, wait_s: int = 30) -> dict:
    """Move the Market Replay clock to `time` (US Eastern, e.g. "2026-08-10T09:30:00" — the same basis
    as nt_playback's clockEst/clockBasis). Refused with 403 unless orders.enabled is armed (the SAME file
    the order module reads — docs/api/orders.md), and with 409 unless the Playback connection is already
    Connected (this call never connects or disconnects it), no non-Playback account holds a position or a
    working order, and no modal dialog is open — the error names which. PlaybackAdapter.Reset is
    asynchronous: wait_s (default 30, max 120 — clamped, never rejected) bounds how long this call waits
    for NinjaTrader's own completion callback before reporting timedOut=True; the clock is read back
    independently either way, in "clockEst". `ok` reflects that callback, not a verdict about the
    read-back clock by itself — a large jump can still be settling after timedOut=True, so re-read
    nt_playback if precision matters."""
    return _addon_post("/playback/seek", {"time": time, "waitSec": int(wait_s)})


@mcp.tool(name="nt_playback_speed")
def nt_playback_speed(speed: int) -> dict:
    """Set the Market Replay playback speed. speed=0 pauses; a positive whole number plays at that
    multiple (nt_playback's maxSpeedValue is this build's ceiling) — writing this property IS
    NinjaTrader's play/pause control, there is no separate button to press. Same armed + Connected +
    no-exposure + no-modal gates as nt_playback_seek, checked ONCE, when the call arrives: a positive speed
    leaves the clock running with nobody watching it, so pause it yourself (speed=0) or use nt_playback_run,
    which re-checks the gates while it plays and always pauses at the end. The written value is always read
    back over the write call's own claim: `ok` is False when it does not match (see `problem`), never a
    silent success."""
    return _addon_post("/playback/speed", {"speed": int(speed)})


@mcp.tool(name="nt_playback_run")
def nt_playback_run(to: str, from_time: str = "", speed: int = 1, wait_s: int = 910) -> dict:
    """Run Market Replay as a bounded job and wait for it to finish. Positions the clock at `from_time`
    (US Eastern; omit to keep playing from wherever the clock already sits — a resume), sets the replay
    range's end to `to` (also the watch target — required), plays at `speed` (a whole number >= 1; use
    nt_playback_speed(0) to just pause instead of running), watches the replay clock with bounded polls
    until it reaches `to` or a 900s wall-clock cap enforced by the AddOn itself, then ALWAYS pauses and
    reports — there is no free-running mode (the AddOn refuses stop_at_end=False outright, which is why
    this tool does not expose it). Same armed + Connected + no-exposure + no-modal gates as
    nt_playback_seek, and they are RE-CHECKED every few seconds while the clock plays: a run stops itself,
    pauses and reports state "error" when orders.enabled is taken away mid-run, when a non-Playback account
    takes on a position or a working order, or when a modal opens — `error` starts with "stopped mid-run".
    A run already in progress (one shared replay clock) is refused with 409 rather than
    queued behind it, and the check-and-queue is atomic, so two calls that land together cannot both be
    accepted. Returns the finished job doc: "state" (done|error|cancelled), "before" (the clock
    and speed captured right before this call touched anything, for a caller that wants to restore it),
    "clockStartEst"/"clockEndEst"/"reachedTo"/"wallSeconds", "warnings" (e.g. a range/seek write that did
    not read back as written), and "executions" — fills on every Playback-provider account during the run,
    read the same way nt_executions reads them, in a window padded +/-3h around the Eastern replay clock
    (Execution.Time is stamped in the EXCHANGE's own local time, e.g. CME Central for ES, not Eastern —
    filter each row's own "time" yourself if you need the window exact). Polls every app.POLL_S seconds;
    if still queued/running after wait_s (default a little over the AddOn's own 900s cap), returns the
    last status doc with a note to call nt_playback_run_status(id) — the run keeps going either way, this
    only bounds how long THIS call waits for it."""
    body = {"to": to, "speed": int(speed), "stopAtEnd": True}
    if from_time:
        body["from"] = from_time
    status = _addon_post("/playback/run", body)
    if "id" not in status:
        return status  # {"error": ...}
    run_id = status["id"]

    import time as _time_mod
    deadline = _time_mod.time() + wait_s
    while status.get("state") in ("queued", "running") and _time_mod.time() < deadline:
        _time_mod.sleep(app.POLL_S)
        status = _addon_get(f"/playback/run/{quote(run_id)}")

    if status.get("state") in ("queued", "running"):
        status = dict(status)
        status["note"] = f"still running; call nt_playback_run_status({run_id!r})"
    return status


@mcp.tool(name="nt_playback_run_status")
def nt_playback_run_status(id: str) -> dict:
    """Status of one playback run by id (from nt_playback_run): "state" (queued|running|done|error|
    cancelled), and once it is no longer queued/running, the full doc (before/clockStartEst/clockEndEst/
    reachedTo/executions/warnings/error) documented on nt_playback_run."""
    return _addon_get(f"/playback/run/{quote(id)}")


@mcp.tool(name="nt_playback_run_cancel")
def nt_playback_run_cancel(id: str) -> dict:
    """Cancel a queued or running playback run: the AddOn pauses the clock, reports what it saw, and
    drops the record (a later nt_playback_run_status(id) then 404s, same convention as nt_backtest_cancel).
    id from nt_playback_run or nt_playback_run_status."""
    return _addon_delete(f"/playback/run/{quote(id)}")
