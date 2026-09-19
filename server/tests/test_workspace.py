"""Workspace tools (nt8_mcp.tools_workspace) against the canned AddOn in fake_addon.py."""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import server as nt8  # noqa: E402
from nt8_mcp import tools_workspace  # noqa: E402

CHART_ROW = {
    "id": "c1", "kind": "Chart", "type": "Chart", "title": "ES 12-26 (5 Min)", "owned": False,
    "hwnd": 66048, "left": 10, "top": 20, "width": 1200, "height": 800,
    "isMinimized": False, "screen": r"\\.\DISPLAY1",
    "details": {
        "instrument": "ES 12-26", "period": "5 Minute", "barsCount": 1240,
        "indicators": [{"name": "VWAP", "state": "Realtime"}],
        "strategies": [{"name": "SampleMACrossOver", "state": "Realtime"}],
    },
    "note": None,
}
DOM_ROW = {
    "id": None, "kind": "SuperDom", "type": "SuperDom", "title": "ES 12-26", "owned": False,
    "hwnd": 66049, "left": -32000, "top": -32000, "width": 160, "height": 1024,
    "isMinimized": True, "screen": r"\\.\DISPLAY1",
    "details": None, "note": "recognised, not decoded",
}
WORKSPACE = {"name": "Trading", "windows": [CHART_ROW, DOM_ROW]}


def test_nt_workspace_passthrough():
    with FakeAddon() as fake:
        fake.json("/workspace", WORKSPACE)
        ws = nt8.nt_workspace()
    assert ws["name"] == "Trading"
    chart = ws["windows"][0]
    assert chart["id"] == "c1" and chart["kind"] == "Chart"
    assert chart["details"]["strategies"][0]["state"] == "Realtime"
    # details null + a note must survive as null, never as {} or []
    assert ws["windows"][1]["details"] is None


def test_nt_workspace_adds_offscreen():
    with FakeAddon() as fake:
        fake.json("/workspace", WORKSPACE)
        ws = nt8.nt_workspace()
    assert ws["windows"][0]["offscreen"] is False
    # The SuperDom row is parked at -32000 BUT minimized: it is reachable from the taskbar.
    assert ws["windows"][1]["offscreen"] is False


def test_offscreen_rules():
    off = tools_workspace._offscreen
    assert off({"left": -32000, "top": -32000, "isMinimized": False}) is True
    assert off({"left": -32000, "top": -32000, "isMinimized": True}) is False
    assert off({"left": 0, "top": 0, "isMinimized": False}) is False
    assert off({"left": None, "top": None, "isMinimized": False}) is False  # no handle = no claim


def test_nt_workspace_error_passthrough():
    with FakeAddon() as fake:
        fake.json("/workspace", {"error": "boom"}, status=500)
        ws = nt8.nt_workspace()
    assert ws == {"error": "boom"}


def test_running_strategies_sends_materialize_only_when_asked():
    seen = []

    def handler(req):
        seen.append(req.query)
        return {"gridResolved": True, "strategies": [], "notes": []}

    with FakeAddon() as fake:
        fake.register("/strategies/running", "GET", handler)
        nt8.nt_strategies_running()
        nt8.nt_strategies_running(materialize=True)
    assert seen == [{}, {"materialize": ["1"]}], seen


def test_running_strategies_unresolved_grid_is_null_not_empty():
    body = {"gridResolved": False, "strategies": None,
            "notes": ["Strategies tab not realized; retry with ?materialize=1"]}
    with FakeAddon() as fake:
        fake.json("/strategies/running", body)
        result = nt8.nt_strategies_running()
    assert result["gridResolved"] is False
    assert result["strategies"] is None, "null must not become []"


def test_running_strategies_rows():
    row = {"name": "SampleMACrossOver", "parent": None, "type": "SampleMACrossOver", "enabled": True,
           "state": "Realtime", "account": "Sim101", "instrument": "ES 12-26", "connected": True,
           "position": "Flat", "accountPosition": "Flat", "trades": 3}
    child = dict(row, name="SampleMACrossOver", parent="SampleMACrossOver", instrument="NQ 12-26")
    with FakeAddon() as fake:
        fake.json("/strategies/running", {"gridResolved": True, "strategies": [row, child], "notes": []})
        result = nt8.nt_strategies_running()
    assert [r["parent"] for r in result["strategies"]] == [None, "SampleMACrossOver"]
    assert result["strategies"][0]["state"] == "Realtime"


def _png(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n" + b"\x00" * 9000)
    return path


def test_window_shot_returns_image_and_sends_the_target():
    seen = {}
    path = os.path.join(tempfile.gettempdir(), "nt8bridge", "test-shot.png")
    _png(path)

    def handler(req):
        seen["body"] = req.body
        return {"ok": True, "path": path, "window": "ES 12-26", "hwnd": 66048,
                "width": 1200, "height": 800, "bytes": 9006, "method": "PrintWindow",
                "looksBlank": False}

    try:
        with FakeAddon() as fake:
            fake.register("/screenshot", "POST", handler)
            result = nt8.nt_window_shot(window="ES 12-26", path=path)
        assert isinstance(result, nt8.Image)
        assert str(result.path) == path
        assert '"window": "ES 12-26"' in seen["body"] and '"path"' in seen["body"]
        assert "chart" not in seen["body"] and "hwnd" not in seen["body"]
    finally:
        os.remove(path)


def test_window_shot_omits_empty_targets_and_defaults_the_path():
    seen = {}

    def handler(req):
        seen["body"] = req.body
        return {"error": "no visible window of this process matching 'nope'"}

    with FakeAddon() as fake:
        fake.register("/screenshot", "POST", handler)
        result = nt8.nt_window_shot(window="nope")
    # A minimized/unknown window is an error dict, never a half-image.
    assert "error" in result
    assert "window" in seen["body"] and "chart" not in seen["body"]
    assert "nt8bridge" in seen["body"], "a default path must be sent so the tool knows where the PNG is"


def test_window_shot_passes_chart_and_hwnd():
    seen = {}

    def handler(req):
        seen["body"] = req.body
        return {"error": "x"}

    with FakeAddon() as fake:
        fake.register("/screenshot", "POST", handler)
        nt8.nt_window_shot(chart="first", hwnd=1234)
    assert '"chart": "first"' in seen["body"] and '"hwnd": 1234' in seen["body"]


def test_tool_names_are_registered_once():
    names = [t.name for t in nt8.mcp._tool_manager.list_tools()]
    for name in ("nt_workspace", "nt_strategies_running", "nt_window_shot"):
        assert names.count(name) == 1, f"{name} is registered {names.count(name)} times"


def test_both_slow_reads_carry_their_own_deadline_and_never_move_the_shared_one():
    """Each call gets a per-call deadline. The process-wide app.HTTP_TIMEOUT is never written, so
    two overlapping slow reads cannot leak a widened value into every other tool."""
    from nt8_mcp import app

    seen = []

    def spy(path, _timeout=None, **params):
        seen.append((path, _timeout, app.HTTP_TIMEOUT))
        return {}

    before = app.HTTP_TIMEOUT
    saved = tools_workspace._addon_get
    tools_workspace._addon_get = spy
    try:
        nt8.nt_workspace()
        nt8.nt_strategies_running()
    finally:
        tools_workspace._addon_get = saved

    assert [p for p, _t, _g in seen] == ["/workspace", "/strategies/running"], seen
    assert all(t >= before for _p, t, _g in seen), seen
    assert all(g == before for _p, _t, g in seen), "the shared timeout was moved"
    assert app.HTTP_TIMEOUT == before
