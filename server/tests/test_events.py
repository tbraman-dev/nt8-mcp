"""Event-ring tools (nt8_mcp.tools_events) against the canned AddOn in fake_addon.py."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import server as nt8  # noqa: E402

OUTPUT = {
    "lines": ["[1] hello", "[2] from a strategy"], "index": 41231, "dropped": 0,
    "subscribed": True, "source": "ring",
}
NTLOG = {
    "entries": [{"t": "2026-09-18T10:14:59", "level": "Error", "category": "Order",
                 "name": "CbiOrderRejected", "resource": "Resource", "msg": "..."}],
    "index": 882, "dropped": 0, "subscribed": True, "source": "ring",
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


def test_nt_output_defaults():
    with FakeAddon() as fake:
        seen = _spy(fake, "/output", OUTPUT)
        out = nt8.nt_output()
    # tab=0 and contains="" mean "no filter" and must not reach the AddOn as literal values.
    assert seen == {"since": ["-1"], "n": ["200"]}, seen
    assert out["lines"][0] == "[1] hello"
    assert out["source"] == "ring"


def test_nt_output_cursor_and_filters():
    with FakeAddon() as fake:
        seen = _spy(fake, "/output", OUTPUT)
        nt8.nt_output(since=OUTPUT["index"], n=50, tab=2, contains="hello")
    assert seen == {"since": ["41231"], "n": ["50"], "tab": ["2"], "contains": ["hello"]}, seen


def test_nt_output_since_zero_is_a_real_cursor():
    # 0 means "everything from the first line ever" and must not be dropped as falsy.
    with FakeAddon() as fake:
        seen = _spy(fake, "/output", OUTPUT)
        nt8.nt_output(since=0)
    assert seen["since"] == ["0"], seen


def test_nt_log_defaults_and_filters():
    with FakeAddon() as fake:
        seen = _spy(fake, "/nt-log", NTLOG)
        entries = nt8.nt_log()["entries"]
        assert seen == {"since": ["-1"], "n": ["200"]}, seen
        assert entries[0]["name"] == "CbiOrderRejected"

        nt8.nt_log(since=882, n=5, level="Error", name="Cbi", contains="reject")
    assert seen == {"since": ["882"], "n": ["5"], "level": ["Error"],
                    "name": ["Cbi"], "contains": ["reject"]}, seen


def test_addon_error_body_becomes_error_key():
    with FakeAddon() as fake:
        fake.json("/nt-log", {"error": "boom"}, status=500)
        assert nt8.nt_log()["error"] == "boom"


def test_tool_names_are_registered_and_do_not_shadow_the_window_scrape():
    # Two @mcp.tool functions with the same name break the whole MCP server, not just one module.
    # The ring is nt_output (this module); tools_core's window scrape is nt_output_window.
    names = [t.name for t in nt8.mcp._tool_manager.list_tools()]
    assert names.count("nt_output") == 1
    assert names.count("nt_output_window") == 1
    assert names.count("nt_log") == 1
    assert "nt_output_events" not in names      # the old name is gone
    assert len(names) == len(set(names)), sorted(names)
