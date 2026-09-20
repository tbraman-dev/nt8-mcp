"""NinjaScript API lookup by reflection over the loaded NinjaTrader assemblies (Core, Gui, Custom,
Vendor when loaded). See docs/api/api.md for the endpoint contract (`GET /api/search`, `GET /api/type`,
`addon/NT8Bridge.Api.cs`). Pure reflection: nothing is instantiated, nothing is invoked.

Call nt_api_search / nt_api BEFORE writing NinjaScript against a member you are not sure exists —
they report the real signature instead of you guessing one that does not compile.
"""

from nt8_mcp.app import _addon_get, mcp


@mcp.tool(name="nt_api_search")
def nt_api_search(query: str, limit: int = 30) -> dict:
    """Search the loaded NinjaTrader assemblies for public types and members whose name matches
    `query` (ranked: exact name, then prefix, then contains — all case-insensitive). Call this
    BEFORE writing NinjaScript against a member you are not sure exists, to stop an invented API
    call before the compile. Returns {query, results, matched, truncated}: `matched` is the true
    count before the `limit` cap (capped at 200 server-side), `truncated` says whether more matched
    than were returned. Each result has `resultKind` ("Type", "Method", "Property", "Field",
    "Event" or "Constructor"); a Type result also carries `typeKind` and `assembly`; a member
    result carries `declaringType` and `assembly` — pass `declaringType` to nt_api for the full
    signature."""
    return _addon_get("/api/search", q=query, limit=limit)


@mcp.tool(name="nt_api")
def nt_api(type_name: str, member: str = "") -> dict:
    """Look up one type's real shape by reflection: kind, base type, interfaces, and its public +
    protected members with full C# style signatures (parameter names and types, return type,
    static/virtual/override, property get/set) — overloads of the same name are listed separately,
    never collapsed. An enum gets `values` instead of `members`. Call this BEFORE writing
    NinjaScript against a member you are not sure exists, to stop an invented API call before the
    compile. `type_name` is a simple name ("StrategyBase") or a full one
    ("NinjaTrader.NinjaScript.StrategyBase"); a simple name matching more than one loaded type comes
    back as {"ambiguous": true, "candidates": [...]} instead of guessing. `member` filters the
    member list to names containing it (case-insensitive) — use it: without it, a base class with a
    deep inheritance chain can have its member list cut off (`truncated` in the result) before the
    member you actually wanted. An unknown type is a clean {"error": ...} (HTTP 404)."""
    return _addon_get("/api/type", name=type_name, member=member)
