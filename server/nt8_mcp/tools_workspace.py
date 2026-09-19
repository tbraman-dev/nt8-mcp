# Portions of this file are derived from cli-nt-bridge
# (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
# MIT License. The full notice is in NOTICE at the repository root.
# (Derived: the `offscreen` heuristic and its minimized exclusion, nt8bridge/windows.py:39-59.)
"""Desktop tools: the loaded workspace, the Control Center strategy grid, window screenshots.

Thin wrappers over the AddOn's HTTP API on :7891 (contract: ../../docs/api/workspace.md).
All three are reads; the only thing written anywhere is one PNG file. Nothing here enables,
disables, starts or stops a strategy, and nothing here restores, fronts, moves or resizes a
window.
"""

import os
import tempfile

from nt8_mcp import app
from nt8_mcp.app import Image, _addon_get, _addon_post, mcp

# ponytail: fixed budgets rather than a caller-supplied one (nt_playback's pattern) — nothing here
# has a natural per-call knob like a coverage scan does. Raise further if a slower desktop needs it.
_WORKSPACE_BUDGET_S = 65   # one Ui() hop per window, each bounded at 5s on the AddOn side
_RUNNING_BUDGET_S = 15     # one Ui() hop bounded at Ws_GridTimeout (10s) on the AddOn side

# Windows parks a minimized window at (-32000, -32000) — well inside the "lost" range below — while
# it still reports as a normal window. Without the exclusion every minimized window is a false
# positive in the one list whose only job is to be short enough to act on (measured against a live
# NinjaTrader: 1 of 67 windows). The taskbar is how you reach a minimized window; it is not lost.
_LOST = 30000


def _offscreen(win: dict) -> bool:
    """Heuristic: is this window's title bar unreachable with the mouse?

    Deliberately generous — it catches the pathological case (a window parked at short.MinValue by a
    runaway move), not a window hanging slightly off an edge, which is normal. A window with no
    handle, and a minimized one, make no claim and answer False.
    """
    # ponytail: coordinate test only, no virtual-screen bounds. Add the bounds test if a multi-monitor
    # layout ever parks a window just past the last monitor instead of at short.MinValue.
    if win.get("isMinimized"):
        return False
    left, top = win.get("left"), win.get("top")
    if left is None or top is None:
        return False
    return left <= -_LOST or top <= -_LOST or left >= _LOST or top >= _LOST


@mcp.tool(name="nt_workspace")
def nt_workspace() -> dict:
    """The loaded NinjaTrader workspace and every window the AddOn can see, in one read: `name` (the
    active workspace, null if NT8 reports none) and `windows`, one row per window including owned
    dialogs and message boxes. Each row carries id (the chart id, or null), kind, type, title, owned,
    screen geometry (hwnd/left/top/width/height/isMinimized/screen, all null when the window has no
    handle) and `offscreen`, a hint that the window is parked where the mouse cannot reach it. A chart
    row also carries details = {instrument, period, barsCount, indicators, strategies}, each script
    with NinjaTrader's own State (a silently disabled strategy is what this call is for); any other
    window has details null plus a note. details null with a note means the window could not be read,
    which is not the same claim as an empty chart. Precondition: NT8 open with the AddOn running. This
    is a read and touches no window state — do not poll it, every chart is visited on its own UI
    thread.
    """
    # _addon_get's default deadline is 5s; nt_workspace makes one bounded Ui() hop PER window plus one
    # OnChart() hop per chart, and a real desktop has dozens of windows — 5s is barely enough for one of
    # them. This one call gets its own deadline; the process-wide app.HTTP_TIMEOUT is never moved.
    result = _addon_get("/workspace", _timeout=max(app.HTTP_TIMEOUT, _WORKSPACE_BUDGET_S))
    if isinstance(result, dict) and isinstance(result.get("windows"), list):
        for win in result["windows"]:
            if isinstance(win, dict):
                win["offscreen"] = _offscreen(win)
    return result


@mcp.tool(name="nt_strategies_running")
def nt_strategies_running(materialize: bool = False) -> dict:
    """Read the Control Center's Strategies grid: the strategy population that is NOT on a chart and
    that nt_charts/nt_workspace therefore cannot see. Returns {gridResolved, strategies, notes};
    `strategies` is a list of rows (name, parent, type, enabled, state, account, instrument, connected,
    connection, dataSeries, position, accountPosition, averagePrice, realized, unrealized, trades,
    parameters, workspace), with one row per master entry plus one per per-instrument child (its
    `parent` names the master). `enabled` is grid state and is NOT proof a strategy is running —
    believe `state`. When the grid could not be read, gridResolved is false and `strategies` is null,
    never [] — "we could not read it" and "there are none" are different claims. The grid is read from
    the Control Center's visual tree when the Strategies tab is selected and from its logical tree when
    it is not (`notes` says which; neither touches the window). Only if both miss does `notes` say to
    retry with materialize=True, which briefly cycles the Control Center's tabs and puts the original
    one back. Precondition: NT8 open with the AddOn running. This
    call never enables, disables or otherwise writes to the grid.
    """
    # The AddOn bounds its own grid read at Ws_GridTimeout (10s); the default 5s client deadline
    # would report a slow-but-healthy AddOn as unreachable before the AddOn's own 504 ever arrives.
    return _addon_get("/strategies/running",
                      _timeout=max(app.HTTP_TIMEOUT, _RUNNING_BUDGET_S),
                      materialize=1 if materialize else None)


@mcp.tool(name="nt_window_shot")
def nt_window_shot(window: str = "", chart: str = "", hwnd: int = 0, path: str = ""):
    """PNG of one NinjaTrader window, captured in-process with PrintWindow and returned as an image.
    Name the target with exactly one of: `window` (a case-insensitive substring of the window's title),
    `chart` (a chart id from nt_charts, or 'first'), or `hwnd`; with none of them it captures the whole
    virtual screen. Precedence if several are given: hwnd, then chart, then window. `path` chooses
    where the PNG is written on the NinjaTrader machine (default: a temp file). Precondition: NT8 open
    with the AddOn running, and the target window not minimized — this call never restores, fronts,
    moves or resizes a window, so a minimized target is an error you fix by restoring it yourself. A
    window covered by other windows still comes back whole (PrintWindow); a chart's canvas is a second
    window owned by NinjaTrader's render form and is merged in. Only the rare screen-blit fallback
    returns an occluded window occluded. Returns the image, or
    {"error": ...} when the window was not found, was minimized, or the file could not be written.
    """
    out = path or os.path.join(tempfile.gettempdir(), "nt8bridge", "window-shot.png")
    body = {"path": out}
    if window:
        body["window"] = window
    if chart:
        body["chart"] = chart
    if hwnd:
        body["hwnd"] = hwnd
    result = _addon_post("/screenshot", body)
    if not isinstance(result, dict) or not result.get("path"):
        return result  # {"error": ...}
    return Image(path=result["path"])
