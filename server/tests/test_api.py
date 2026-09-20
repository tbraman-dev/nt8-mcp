"""nt8_mcp.tools_api (nt_api_search, nt_api) against the canned AddOn in fake_addon.py."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import server as nt8  # noqa: E402

SEARCH_RESULT = {
    "query": "SMA", "matched": 1, "truncated": False,
    "results": [{"rank": 0, "resultKind": "Type", "typeKind": "Class",
                 "name": "NinjaTrader.NinjaScript.Indicators.SMA", "assembly": "NinjaTrader.Custom"}],
}
TYPE_RESULT = {
    "name": "NinjaTrader.NinjaScript.StrategyBase", "assembly": "NinjaTrader.Custom", "kind": "Class",
    "baseType": "NinjaScriptBase", "interfaces": [], "memberCount": 2, "truncated": False,
    "members": [
        {"kind": "Method", "name": "EnterLong", "declaringType": "NinjaTrader.NinjaScript.StrategyBase",
         "static": False, "signature": "public void EnterLong()"},
        {"kind": "Method", "name": "EnterLong", "declaringType": "NinjaTrader.NinjaScript.StrategyBase",
         "static": False, "signature": "public void EnterLong(int quantity, string signalName)"},
    ],
}


def test_nt_api_search_sends_query_and_default_limit():
    seen = {}

    def handler(req):
        seen.update(req.query)
        return SEARCH_RESULT

    with FakeAddon() as fake:
        fake.register("/api/search", "GET", handler)
        result = nt8.nt_api_search("SMA")
    assert seen == {"q": ["SMA"], "limit": ["30"]}, seen
    assert result["matched"] == 1
    assert result["results"][0]["name"] == "NinjaTrader.NinjaScript.Indicators.SMA"


def test_nt_api_search_custom_limit():
    seen = {}

    def handler(req):
        seen.update(req.query)
        return SEARCH_RESULT

    with FakeAddon() as fake:
        fake.register("/api/search", "GET", handler)
        nt8.nt_api_search("EnterLong", limit=5)
    assert seen["limit"] == ["5"], seen


def test_nt_api_type_lookup_no_member_filter_drops_the_param():
    # member="" is the default: an empty value must be DROPPED from the query string, not sent as
    # "" (the AddOn's own JGetStr treats an empty string as present, which would wrongly filter
    # every member out) — same rule test_core.py's test_query_params_reach_the_addon checks.
    seen = {}

    def handler(req):
        seen.update(req.query)
        return TYPE_RESULT

    with FakeAddon() as fake:
        fake.register("/api/type", "GET", handler)
        result = nt8.nt_api("NinjaTrader.NinjaScript.StrategyBase")
    assert seen == {"name": ["NinjaTrader.NinjaScript.StrategyBase"]}, seen
    assert len(result["members"]) == 2
    assert all(m["name"] == "EnterLong" for m in result["members"])  # overloads listed separately


def test_nt_api_type_lookup_with_member_filter():
    seen = {}

    def handler(req):
        seen.update(req.query)
        return TYPE_RESULT

    with FakeAddon() as fake:
        fake.register("/api/type", "GET", handler)
        nt8.nt_api("StrategyBase", member="EnterLong")
    assert seen == {"name": ["StrategyBase"], "member": ["EnterLong"]}, seen


def test_nt_api_unknown_type_is_a_clean_404():
    with FakeAddon() as fake:
        fake.json("/api/type", {"error": "no type 'Nope' in the loaded NinjaTrader assemblies"}, status=404)
        result = nt8.nt_api("Nope")
    assert result == {"error": "no type 'Nope' in the loaded NinjaTrader assemblies"}


def test_nt_api_ambiguous_name_reports_candidates_not_a_guess():
    ambiguous = {"ambiguous": True, "candidates": ["NinjaTrader.NinjaScript.Order", "NinjaTrader.Cbi.Order"]}
    with FakeAddon() as fake:
        fake.json("/api/type", ambiguous)
        result = nt8.nt_api("Order")
    assert result["ambiguous"] is True
    assert len(result["candidates"]) == 2
