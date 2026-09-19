"""Feed and connection tools, read only.

`nt_feedhealth` answers "is this instrument's feed still ticking?" from the snapshot
NinjaTrader already holds — it subscribes to nothing. `nt_connections` answers "what is
connected, and did anything drop by itself?". Neither connects, disconnects or reconnects
anything; that is a separate, opt-in module. See ../../docs/api/feeds.md for the contract.
"""

from nt8_mcp.app import _addon_get, mcp


@mcp.tool(name="nt_feedhealth")
def nt_feedhealth(instruments: list[str] | str):
    """Last-tick age per instrument, to catch a feed that is frozen but still reports Connected.
    Precondition: the AddOn is running and `instruments` holds full NinjaTrader names ("ES 12-26",
    not "ES"); an empty list is refused with a 400. Returns {"nowUtc","now","feeds":[{"instrument",
    "resolvedName","found","hasSeenMarketData","lastPrice","lastTickTime","ageMs","error"}],
    "anyNonSim","note"}. `ageMs` is computed inside the AddOn from one clock, so do not subtract
    timestamps yourself. READ ageMs null AS STALE, NEVER AS FRESH: if `found` is false the name is
    unknown to NinjaTrader; if `hasSeenMarketData` is false no tick has ever arrived this session:
    either nothing (no chart, no SuperDom, no strategy) is watching that instrument, or what watches
    it has no ticking feed (a parked Playback, a provider that is down) — a chart alone does not make
    it true. Under Playback `lastTickTime` is replay time, so `ageMs` is not a freshness measure
    there; use nt_playback. This tool NEVER subscribes to market data, never creates an
    instrument and never connects anything — an instrument nobody is watching stays null forever.
    A bad name degrades to one row with an `error`, never a failed call."""
    names = instruments if isinstance(instruments, str) else ",".join(instruments or [])
    return _addon_get("/feedhealth", instruments=names)


@mcp.tool(name="nt_connections")
def nt_connections(n: int = 20, since: int = -1):
    """Every connection NinjaTrader has configured or is actually running, with why each one last dropped.
    Precondition: the AddOn is running. Returns {"connections":[{"name","source","provider",
    "canManageOrders","status","priceStatus","connected","dropClass","inadvertentlyDropped","live",
    "nonSim"}],"anyLiveConnected","anyNonSimConnected","subscribed","events":[...],"index","dropped",
    "note"}; `n` caps the recent status-transition `events`, and `since` is a cursor into them (same
    sequence-number convention as `nt_log`/`nt_output`: pass back the previous call's `index` to
    get only what is new). `source` is "configured" (in the connection list) or "live-only" (running
    without being in it) — the union matters, because reading the configuration alone once hid a live
    connection and a reader concluded nothing was connected. Judge live-ness by `provider` and
    `canManageOrders`, or by the top-level `anyLiveConnected`, NEVER by the connection's name, which is
    free text. `dropClass` is "connected", "inadvertent" (it fell over), "user" (a human parked it), "failed" (a connect attempt was refused; nothing dropped), or
    null meaning no transition has been witnessed since the AddOn loaded — null is "not known", not
    "fine". (NinjaTrader replays the current status of every connection it still holds when the AddOn
    subscribes, so a connected connection reads "connected" straight after a reload; one that was
    already gone stays null.) `status` null means NinjaTrader holds no connection object for that configured entry at all.
    This tool is read only: it never connects, disconnects or reconnects."""
    return _addon_get("/connections", n=n, since=since)
