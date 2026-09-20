"""Data store tools: coverage of NinjaTrader's own db, the opt-in historical/replay
download, and the offline .nrd -> parquet decoder.

See ../../docs/api/data.md for the HTTP contract these wrap.
"""

import os
import time

from nt8_mcp import app  # POLL_S is read through the module so tests can patch it
from nt8_mcp.app import NT_HOME, _addon_delete, _addon_get, _addon_post, mcp

# db\replay\<contract>\<YYYYMMDD>.nrd — the default root nt_nrd_export discovers under.
NT_REPLAY_DIR = os.path.join(NT_HOME, "db", "replay")


@mcp.tool(name="nt_data_coverage")
def nt_data_coverage(instrument: str, kind: str = "", from_date: str = "", to_date: str = "") -> dict:
    """Pre-flight the local data store for one instrument: which days NinjaTrader actually has on disk,
    before you run a backtest or a replay that would silently run on a hole. Reads the four stores under
    Documents\\NinjaTrader 8\\db (tick, minute, day, replay) by file NAME only — it answers "is there
    anything for that day", never "is that day complete". Returns per store: scanned (false when the folder
    does not exist — never an empty day map), dir, granularity (tick files are hourly, minute daily, day
    per YEAR, replay daily) and a days map with per-day file counts, bytes, and Last/Bid/Ask counts; plus
    missingWeekdays, daysLackingBidAsk and thinDays over the chosen store. It also scans the continuous
    contract name (ES 12-26 -> ES ##-##) and reports it under alsoScanned, because market-replay recordings
    routinely live there while every tick file sits under the front month. Also returns cache: what a
    BACKTEST can use without hitting the provider at all — NT8's own bars cache
    (db\\cache\\<TradingHours>.<TimeZone>\\<MINUTE|TICK>\\<contract>\\<seriesKey>.<from>.<to>.<Type>.ncd),
    scanned by file name across every contract of the instrument's chain (root-symbol match), reporting each
    series key's contract, firstDay and lastDay plus whether the bounded scan hit its cap. day/replay have no
    cache folder, so cache.series is empty for those kinds. kind narrows the scan to one store;
    from_date/to_date (YYYYMMDD) narrow the analysis window. Reads only — it downloads nothing."""
    return _addon_get("/data/coverage", instrument=instrument, kind=kind, **{"from": from_date, "to": to_date})


@mcp.tool(name="nt_data_download")
def nt_data_download(instrument: str, from_date: str, to_date: str, kinds: list | None = None,
                     types: list | None = None, overwrite: bool = False, big: bool = False) -> dict:
    """Queue a historical/market-replay download into NinjaTrader's own data store and return {id, state} at
    once (202) — poll nt_data_download_status(id). This is the ONE tool here that writes NT8's db and spends
    the data provider's bandwidth. No arming file is needed — a download moves no money — but it refuses with
    409 while any account but the Backtest one has an open position or a working order (exposure: a throttled
    feed while something is at stake), and with 409 when no real data provider is connected at all. Ranges
    wider than 10 days need big=True. kinds defaults to ["replay"] (also tick, minute, day); types defaults to
    ["Last","Bid","Ask"] and applies to the tick/minute/day stores. Saturdays and the current/future day in
    New York time are never downloaded — replay data is partial until the session closes. For tick/minute/day
    it tries each connected data connection's DownloadFromProvider in turn (a broker adapter before
    NinjaTrader's own hosted service) and, when none of them answer, falls back to Bars.GetBars with
    MergePolicy.DoNotMerge — the same call shape a headless backtest uses to pull missing bars on demand
    (NT8Bridge.Backtest.cs RunA2). Proven live: DownloadFromProvider fails on this machine even through a
    connected broker adapter, but the fallback with DoNotMerge writes real .ncd files into db\\<kind> where
    MergePolicy.UseDefault silently wrote nothing. Every downloaded entry and every failure names which path
    served or refused it. It never touches an order, a position or an account."""
    body = {"instrument": instrument, "from": from_date, "to": to_date,
            "kinds": list(kinds) if kinds else ["replay"],
            "types": list(types) if types else ["Last", "Bid", "Ask"],
            "overwrite": bool(overwrite), "big": bool(big)}
    return _addon_post("/data/download", body)


@mcp.tool(name="nt_data_probe")
def nt_data_probe(instrument: str, kind: str = "minute", wait_s: int = 120) -> dict:
    """How far back the CONNECTED provider actually serves data for one instrument, at one resolution
    (minute|tick|day) — found with a bounded binary search of small requests (a hard cap on both request
    count and wall time), so the caller does not have to guess 3 months / 1 year / 2 years by trial. No arming
    file: reads at most a handful of days into NinjaTrader's own store as a side effect, same guards as
    nt_data_download (409 on exposure, 409 with no real data provider connected). The AddOn runs the search
    off its own request-handling thread pool (it can legitimately take up to ~90s, worse with a slow
    provider), so this queues it (202 {id,state:"queued"}) and polls GET /data/probe/{id} for up to wait_s
    seconds (default 120); still running past that returns the last {id,state} with a note to poll again.
    Each probed day counts only bars stamped INSIDE that day — a provider answering with bars from outside
    the asked window (observed live before this was fixed) no longer counts as "has data there". Returns
    requestsUsed, capRequests, capReached, earliestDate (YYYYMMDD, null if unknown), depthDays and a note;
    when capReached is true the note says "at least N days (search cap reached)" — the provider may serve
    more, this is only the floor the search proved. A dated contract's earliestDate is its own listing date,
    not the merged chain a backtest sees — the note says so when relevant. Reads only for the caller's
    purposes; it downloads nothing on its own beyond what a normal Bars.GetBars fetch inherently caches."""
    status = _addon_get("/data/probe", instrument=instrument, kind=kind)
    if "id" not in status:
        return status  # {"error": ...}
    probe_id = status["id"]
    deadline = time.time() + wait_s
    while status.get("state") in ("queued", "running") and time.time() < deadline:
        time.sleep(app.POLL_S)
        status = _addon_get(f"/data/probe/{probe_id}")
    if status.get("state") in ("queued", "running"):
        status = dict(status)
        status["note"] = f"still running; call nt_data_probe again or GET /data/probe/{probe_id}"
    return status


@mcp.tool(name="nt_data_download_status")
def nt_data_download_status(id: str) -> dict:
    """Status of one data download by id (from nt_data_download): state (queued|running|done|error|cancelled|
    refused), the date currently being fetched, and four SEPARATE buckets — downloaded, skipped (already on
    disk), skippedCurrent (the current/future day, never fetched) and failed (each with its date, kind and
    error), so "we did not ask" is always distinguishable from "it failed". Also reports anyLive, anyNonSim,
    the arming flag's age and free disk bytes. state "refused" means a position or a working order appeared
    mid-run and the job stopped itself."""
    return _addon_get(f"/data/download/{id}")


@mcp.tool(name="nt_data_download_cancel")
def nt_data_download_cancel(id: str) -> dict:
    """Cancel a queued or running data download by id. The worker stops before the next date; a date already
    in flight inside NinjaTrader's own downloader finishes on its own. Returns {ok, state}."""
    return _addon_delete(f"/data/download/{id}")


@mcp.tool(name="nt_nrd_export")
def nt_nrd_export(instrument_glob: str, out_dir: str, levels: list | None = None,
                  force: bool = False, replay_dir: str = "") -> dict:
    """Decode NinjaTrader market-replay .nrd files straight to per-day L1/L2 UTC parquet — pure local Python,
    NinjaTrader is not involved and need not even be running. instrument_glob matches contract folders under
    db\\replay (e.g. "ES *" or "MNQ ##-##"); output lands at <out_dir>/<SEASON>/<SYM>-<SEASON>_<L1|L2>/
    <YYYYMMDD>.parquet and a date whose targets already exist is skipped unless force=True. levels defaults
    to ["L1","L2"]. Each decode is cross-checked against the .nrd header's own volume sums and price ranges:
    a corrupt file writes NOTHING and is listed in corrupt[], a truncated one writes its clean prefix and is
    listed in truncated[]; every parquet is committed atomically and a 0-row result is never written.
    Returns {exported, failed, truncated, corrupt, empty, count}. Needs numpy and pyarrow installed."""
    try:
        from nt8_mcp import nrd_offline
    except ImportError as e:   # numpy / pyarrow are not declared dependencies of nt8-mcp
        return {"error": f"nt_nrd_export needs numpy and pyarrow: {e}"}
    root = replay_dir or NT_REPLAY_DIR
    if not os.path.isdir(root):
        return {"error": f"no replay folder at {root}"}
    try:
        return nrd_offline.export(root, instrument_glob, out_dir,
                                  levels=tuple(levels) if levels else ("L1", "L2"), force=force)
    except (ValueError, OSError) as e:
        return {"error": str(e)}
