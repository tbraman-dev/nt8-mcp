"""MCP server for the NT8Bridge AddOn (see ../../API.md for the HTTP contract).

Read-only by default: passthrough tools hit the AddOn's HTTP API on :7891 (no order
or account-changing endpoints exist there by default; see API.md). Local tools
(nt_check/nt_install/nt_install_addon/nt_compile/nt_shot/nt_trace) touch the filesystem,
dotnet and NT8's own windows directly, without going through the AddOn.

This module is the entry point only. The tools live in `nt8_mcp/tools_*.py` and the
shared seam (the `mcp` instance, the HTTP helpers, NT8's paths) lives in `nt8_mcp/app.py`.
Every `tools_*` module found in the package is imported here, in name order, so a new
module registers itself by existing — never by editing this file.

Their public names are re-exported as a LIVE VIEW, not a copy: reading and writing
`nt8_mcp.server.<NAME>` reads and writes the module that defines `<NAME>`. So
`from nt8_mcp.server import mcp, nt_compile, ...` keeps working, and a test that patches
`nt8_mcp.server.POLL_S` or `nt8_mcp.server._find_windows` really patches `app.POLL_S` /
`tools_local._find_windows` — the values the tools read. Patching the owning module
(`app.*`, `tools_<module>.*`) is the explicit form and is what a test should prefer when
two modules export the same name.

Windows-only (ctypes user32, NinjaTrader's own directory layout).
"""

import importlib
import pkgutil
import sys
from types import ModuleType

from nt8_mcp import app
from nt8_mcp.app import mcp  # by name: a function's globals are never routed through __getattr__

# public name -> the module that defines it; later imports win, as `import *` would
_OWNERS: dict[str, ModuleType] = {}


def _own(module: ModuleType) -> None:
    _OWNERS.update({k: module for k in vars(module) if not k.startswith("__")})


def _import_tool_modules() -> list[str]:
    """Import every nt8_mcp.tools_* module in name order and re-export its names here."""
    import nt8_mcp

    names = sorted(m.name for m in pkgutil.iter_modules(nt8_mcp.__path__) if m.name.startswith("tools_"))
    for name in names:
        _own(importlib.import_module(f"nt8_mcp.{name}"))
    return names


_own(app)
TOOL_MODULES = _import_tool_modules()


class _ReexportModule(ModuleType):
    """Routes attribute reads AND writes to the module that owns the name.

    Copying the values in (`globals().update(vars(module))`) made `nt8_mcp.server.POLL_S = 0`
    bind a second POLL_S here while the tool went on reading the original — a patch that
    silently did nothing. Names nobody owns stay this module's own.
    """

    def __getattr__(self, name):
        owner = _OWNERS.get(name)
        if owner is None:
            raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")
        return getattr(owner, name)

    def __setattr__(self, name, value):
        owner = _OWNERS.get(name)
        if owner is None:
            object.__setattr__(self, name, value)
        else:
            setattr(owner, name, value)

    def __dir__(self):
        return sorted(set(_OWNERS) | set(vars(self)))


sys.modules[__name__].__class__ = _ReexportModule


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
