"""Shared seam for every nt8_mcp tool module: the single `mcp` instance, the AddOn
HTTP passthrough helpers, and NinjaTrader's directory layout.

A tool module (`nt8_mcp/tools_*.py`) does:

    from nt8_mcp import app                       # for values tests patch: app.BASE_URL, app.POLL_S
    from nt8_mcp.app import mcp, _addon_get       # everything else by name

and registers its tools with `@mcp.tool(name="nt_...")`. `nt8_mcp/server.py` imports
every `tools_*` module; a module never edits `server.py`.

Windows-only (ctypes user32, NinjaTrader's own directory layout).
"""

import json
import os
import socket
import urllib.error
import urllib.request
from urllib.parse import urlencode

# mcp 1.x named this FastMCP; mcp 2.x renamed it MCPServer (mcp.server.fastmcp raises
# ModuleNotFoundError with a migration pointer). Support whichever is installed.
try:
    from mcp.server.fastmcp import FastMCP, Image
except ModuleNotFoundError:
    from mcp.server.mcpserver import Image, MCPServer as FastMCP

mcp = FastMCP("nt8-mcp")

# ---------------------------------------------------------------------------
# AddOn HTTP passthrough
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("NT8BRIDGE_URL", "http://localhost:7891")
HTTP_TIMEOUT = 5
POLL_S = 2  # backtest status poll interval; tests patch this to 0
NOT_REACHABLE = "NT8Bridge not reachable on :7891 — is NT8 open and the AddOn compiled?"


def _addon_request(path: str, method: str = "GET", body: dict | None = None,
                   _timeout: float | None = None):
    """`_timeout` is this one call's deadline in seconds; None = the module-wide HTTP_TIMEOUT.
    Use it for a long read (a coverage scan, a workspace walk) instead of mutating
    app.HTTP_TIMEOUT, which is process-wide and visible to every other tool meanwhile."""
    url = BASE_URL.rstrip("/") + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_timeout or HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read().decode("utf-8")).get("error", str(e))}
        except Exception:
            return {"error": f"HTTP {e.code}: {e.reason}"}
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
        return {"error": NOT_REACHABLE}


def _addon_get(path: str, _timeout: float | None = None, **params):
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    qs = f"?{urlencode(clean)}" if clean else ""
    return _addon_request(path + qs, _timeout=_timeout)


def _addon_post(path: str, body: dict | None = None, _timeout: float | None = None):
    return _addon_request(path, method="POST", body=body, _timeout=_timeout)


def _addon_delete(path: str, _timeout: float | None = None):
    return _addon_request(path, method="DELETE", _timeout=_timeout)


# ---------------------------------------------------------------------------
# NinjaTrader's directory layout (local tools)
# ---------------------------------------------------------------------------

NT_HOME = os.path.join(os.path.expanduser("~"), "Documents", "NinjaTrader 8")
NT_CUSTOM = os.path.join(NT_HOME, "bin", "Custom")
NT_CSPROJ = os.path.join(NT_CUSTOM, "NinjaTrader.Custom.csproj")
NT_CUSTOM_DLL = os.path.join(NT_CUSTOM, "NinjaTrader.Custom.dll")
NT_TRACE_DIR = os.path.join(NT_HOME, "trace")
NT_LOG_DIR = os.path.join(NT_HOME, "log")
_SCRIPT_FOLDERS = ("Indicators", "AddOns", "Strategies", "SuperDomColumns")
