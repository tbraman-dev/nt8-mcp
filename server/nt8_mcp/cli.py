"""`nt8` - run any registered MCP tool from a shell, print its JSON result.

    nt8                                 list the tools
    nt8 nt_health                       call one (`nt8 health` is the same: the nt_ prefix is optional)
    nt8 nt_bars --chart first --n 5     keyword args (--key value or --key=value)
    nt8 nt_backtest '{"strategy": "SampleMACrossOver", "tick_replay": false}'  same, as one JSON object
    nt8 nt_screenshot                   image tools print the path of the PNG they wrote

Values are parsed as JSON when they parse (numbers, true/false, null, lists, objects)
and kept as strings otherwise, so --chart first and --n 5 both do the right thing.

Exit codes: 0 did it | 1 could not reach the AddOn (or the tool returned an "error")
| 2 refused: unknown tool or bad arguments.
"""

import inspect
import json
import os
import sys
import tempfile

from nt8_mcp import server
from nt8_mcp.app import Image


def _tool_names() -> list[str]:
    # the sync accessor: mcp.list_tools() is a coroutine in mcp 2.x and we want no asyncio here
    return sorted(t.name for t in server.mcp._tool_manager.list_tools())


def _usage() -> str:
    return __doc__.rstrip() + "\n\nTools:\n  " + "\n  ".join(_tool_names())


def _value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _parse_args(argv: list[str]) -> dict:
    """['--chart', 'first', '--n', '5'] or ['{"chart": "first"}'] -> kwargs."""
    if len(argv) == 1 and argv[0].lstrip().startswith("{"):
        kwargs = json.loads(argv[0])
        if not isinstance(kwargs, dict):
            raise ValueError("JSON argument must be an object")
        return kwargs
    kwargs, i = {}, 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--"):
            raise ValueError(f"expected --key before {arg!r}")
        key, sep, inline = arg[2:].partition("=")
        key = key.replace("-", "_")
        if sep:
            kwargs[key] = _value(inline)
        elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            kwargs[key] = _value(argv[i + 1])
            i += 1
        else:
            kwargs[key] = True  # bare flag
        i += 1
    return kwargs


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0

    name, rest = argv[0], argv[1:]
    if name not in _tool_names() and "nt_" + name in _tool_names():
        name = "nt_" + name  # `nt8 health` == `nt8 nt_health`
    if name not in _tool_names():
        print(f"unknown tool: {name}\n\nTools:\n  " + "\n  ".join(_tool_names()), file=sys.stderr)
        return 2
    # Bind the arguments first, call outside the guard: exit 2 means "your arguments were
    # wrong", so a failure INSIDE the tool (e.g. the AddOn answering non-JSON -> ValueError)
    # must not be reported as one. It surfaces as its own traceback / error document instead.
    fn = getattr(server, name)
    try:
        kwargs = _parse_args(rest)
        inspect.signature(fn).bind(**kwargs)
    except (TypeError, ValueError) as e:
        print(f"{name}: {e}", file=sys.stderr)
        return 2
    result = fn(**kwargs)

    if isinstance(result, Image):
        path = result.path
        if path is None:
            fd, path = tempfile.mkstemp(prefix="nt8_", suffix=".png")
            with os.fdopen(fd, "wb") as fh:
                fh.write(result.data or b"")
        print(path)
        return 0

    print(json.dumps(result, indent=2, default=str))
    return 1 if isinstance(result, dict) and "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
