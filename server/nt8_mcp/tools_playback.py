# Portions of this file are derived from cli-nt-bridge
# (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
# MIT License. The full notice is in NOTICE at the repository root.
# (Derived: PlaybackState.clock_unset / ready_to_seek and their reasoning, nt8bridge/playback.py:60-110.)
"""Market Replay transport state: is the replay transport connected, loaded, parked or running.

One read over the AddOn's `GET /playback` (contract: ../../docs/api/playback.md). Nothing here
connects, disconnects, seeks, or changes the replay speed — writing the speed property IS the play
button, and this repo has no write side for Playback at all.
"""

from nt8_mcp import app
from nt8_mcp.app import _addon_get, mcp

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
