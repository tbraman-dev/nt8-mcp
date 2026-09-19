"""Feed/connection tools (nt8_mcp.tools_feeds) against the canned AddOn in fake_addon.py."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import server as nt8  # noqa: E402

FEEDHEALTH = {
    "nowUtc": "2026-09-18T14:15:00Z",
    "now": "2026-09-18T10:15:00",
    "feeds": [
        {"instrument": "ES 12-26", "resolvedName": "ES 12-26", "found": True,
         "hasSeenMarketData": True, "lastPrice": 5812.25,
         "lastTickTime": "2026-09-18T10:14:59", "ageMs": 412, "error": None},
        {"instrument": "NOPE", "resolvedName": None, "found": False, "hasSeenMarketData": None,
         "lastPrice": None, "lastTickTime": None, "ageMs": None, "error": None},
    ],
    "anyNonSim": False,
    "note": "read only: nothing was created and nothing was subscribed.",
}

CONNECTIONS = {
    "connections": [
        {"name": "Sim101 feed", "source": "configured", "provider": "Simulator",
         "canManageOrders": False, "status": "Connected", "priceStatus": "Connected",
         "connected": True, "dropClass": "connected", "inadvertentlyDropped": False,
         "live": False, "nonSim": False},
        {"name": "Broker", "source": "live-only", "provider": "Rithmic",
         "canManageOrders": True, "status": "ConnectionLost", "priceStatus": "ConnectionLost",
         "connected": False, "dropClass": "inadvertent", "inadvertentlyDropped": True,
         "live": False, "nonSim": False},
    ],
    "anyLiveConnected": False,
    "anyNonSimConnected": False,
    "subscribed": True,
    "events": [{"t": "2026-09-18T10:10:00", "name": "Broker", "status": "ConnectionLost",
                "priceStatus": "ConnectionLost", "previousStatus": "Connected",
                "error": "NoError", "class": "inadvertent"}],
    "index": 4,
    "dropped": 0,
    "note": "read only: this endpoint never connects or disconnects anything.",
}


def _spy(fake, path, body):
    """Register `path` and return the dict that collects the query it was called with."""
    seen = {}

    def handler(req):
        seen.clear()
        seen.update(req.query)
        return body

    fake.register(path, "GET", handler)
    return seen


def test_feedhealth_joins_names_with_commas():
    # Instrument full names contain spaces but never commas, so one comma-joined value is safe.
    with FakeAddon() as fake:
        seen = _spy(fake, "/feedhealth", FEEDHEALTH)
        out = nt8.nt_feedhealth(["ES 12-26", "MNQ 12-26"])
    assert seen == {"instruments": ["ES 12-26,MNQ 12-26"]}, seen
    assert out["feeds"][0]["ageMs"] == 412


def test_feedhealth_accepts_a_preformatted_string():
    with FakeAddon() as fake:
        seen = _spy(fake, "/feedhealth", FEEDHEALTH)
        nt8.nt_feedhealth("ES 12-26,MNQ 12-26")
    assert seen == {"instruments": ["ES 12-26,MNQ 12-26"]}, seen


def test_feedhealth_empty_list_reaches_the_addon_as_no_parameter():
    # _addon_get drops empty values, so the AddOn sees no ?instruments= and answers its own 400.
    with FakeAddon() as fake:
        seen = _spy(fake, "/feedhealth", FEEDHEALTH)
        nt8.nt_feedhealth([])
    assert seen == {}, seen


def test_feedhealth_unknown_name_is_a_row_not_a_failure():
    with FakeAddon() as fake:
        fake.json("/feedhealth", FEEDHEALTH)
        rows = nt8.nt_feedhealth(["ES 12-26", "NOPE"])["feeds"]
    bad = [r for r in rows if r["instrument"] == "NOPE"][0]
    assert bad["found"] is False
    assert bad["ageMs"] is None  # null age is STALE, never fresh


def test_feedhealth_400_body_becomes_error_key():
    with FakeAddon() as fake:
        fake.json("/feedhealth", {"error": "GET /feedhealth needs ?instruments="}, status=400)
        assert nt8.nt_feedhealth([])["error"].startswith("GET /feedhealth needs")


def test_connections_union_and_default_event_cap():
    with FakeAddon() as fake:
        seen = _spy(fake, "/connections", CONNECTIONS)
        out = nt8.nt_connections()
    assert seen == {"n": ["20"], "since": ["-1"]}, seen
    assert [c["source"] for c in out["connections"]] == ["configured", "live-only"]
    assert out["anyLiveConnected"] is False


def test_connections_passes_n():
    with FakeAddon() as fake:
        seen = _spy(fake, "/connections", CONNECTIONS)
        nt8.nt_connections(n=5)
    assert seen == {"n": ["5"], "since": ["-1"]}, seen


def test_connections_passes_since():
    with FakeAddon() as fake:
        seen = _spy(fake, "/connections", CONNECTIONS)
        nt8.nt_connections(since=41)
    assert seen == {"n": ["20"], "since": ["41"]}, seen


def test_connections_inadvertent_drop_is_visible():
    with FakeAddon() as fake:
        fake.json("/connections", CONNECTIONS)
        rows = nt8.nt_connections()["connections"]
    dropped = [c for c in rows if c["inadvertentlyDropped"]]
    assert [c["name"] for c in dropped] == ["Broker"]
    assert dropped[0]["dropClass"] == "inadvertent"


def test_connections_addon_error_body_becomes_error_key():
    with FakeAddon() as fake:
        fake.json("/connections", {"error": "boom"}, status=500)
        assert nt8.nt_connections()["error"] == "boom"


def test_tool_names_are_registered_once():
    # Two @mcp.tool functions with the same name break the whole MCP server, not just one module.
    names = [t.name for t in nt8.mcp._tool_manager.list_tools()]
    assert names.count("nt_feedhealth") == 1
    assert names.count("nt_connections") == 1
    assert len(names) == len(set(names)), sorted(names)


def test_docstrings_state_the_null_age_rule():
    # The single most misread field in this module: ageMs null means STALE.
    assert "NEVER AS FRESH" in nt8.nt_feedhealth.__doc__
    # ...and that /connections tells the reader not to judge live-ness by a free-text name.
    flat = " ".join(nt8.nt_connections.__doc__.split()).lower()
    assert "never by the connection's name" in flat, flat
