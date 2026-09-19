"""The `nt8` CLI (nt8_mcp.cli): argument parsing, printed JSON, exit codes."""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import app, cli  # noqa: E402


def _run(argv):
    """cli.main(argv) -> (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_bare_and_help_list_tools():
    for argv in ([], ["--help"]):
        code, out, _ = _run(argv)
        assert code == 0
        assert "nt_health" in out and "nt_install_addon" in out, out


def test_unknown_tool_exits_2():
    code, _, err = _run(["nt_bogus"])
    assert code == 2
    assert "nt_bogus" in err


def test_bad_argument_exits_2():
    code, _, err = _run(["nt_chart_state", "--no-such-arg", "1"])
    assert code == 2, err


def test_tool_failure_is_not_reported_as_a_bad_argument():
    # Exit 2 means "your arguments were wrong". A ValueError raised INSIDE the tool (json.loads
    # raises one when something other than the AddOn answers :7891 with non-JSON) must not be
    # dressed up as that — the caller would go and fix arguments that were fine.
    from nt8_mcp import tools_core
    saved = tools_core.nt_health

    def boom():
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    tools_core.nt_health = boom
    try:
        _run(["nt_health"])
    except ValueError:
        pass
    else:
        raise AssertionError("a tool's own failure must not become exit 2")
    finally:
        tools_core.nt_health = saved


def test_happy_path_prints_json_and_exits_0():
    with FakeAddon() as fake:
        fake.json("/health", {"ok": True, "addonVersion": "1.1.0"})
        code, out, _ = _run(["nt_health"])
    assert code == 0
    assert json.loads(out)["addonVersion"] == "1.1.0"


def test_nt_prefix_is_optional():
    with FakeAddon() as fake:
        fake.json("/health", {"ok": True, "addonVersion": "1.1.0"})
        code, out, _ = _run(["health"])
    assert code == 0
    assert json.loads(out)["addonVersion"] == "1.1.0"


def test_error_result_exits_1():
    saved = app.BASE_URL
    app.BASE_URL = "http://127.0.0.1:1"  # nothing listening
    try:
        code, out, _ = _run(["nt_health"])
    finally:
        app.BASE_URL = saved
    assert code == 1
    assert "error" in json.loads(out)


def test_keyword_args_are_json_coerced():
    seen = {}

    def handler(req):
        seen.update(req.query)
        return []

    with FakeAddon() as fake:
        fake.register("/chart/c1/bars", "GET", handler)
        code, _, err = _run(["nt_bars", "--chart", "c1", "--n", "5"])
    assert code == 0, err
    assert seen == {"n": ["5"]}, seen  # "c1" stayed a string, 5 became an int


def test_json_object_argument():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.body))
        return (202, {"id": "b1", "state": "done", "summary": {"trades": 0}})

    with FakeAddon() as fake:
        fake.register("/backtest", "POST", handler)
        code, _, err = _run(["nt_backtest", '{"strategy": "SampleMACrossOver", "tick_replay": false}'])
    assert code == 0, err
    assert seen["strategy"] == "SampleMACrossOver" and seen["tickReplay"] is False, seen


def test_image_result_prints_a_path():
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "nt8_cli_shot.png")
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n")
    try:
        with FakeAddon() as fake:
            fake.json("/chart/first/screenshot", {"ok": True, "path": path}, method="POST")
            code, out, err = _run(["nt_screenshot"])
    finally:
        os.remove(path)
    assert code == 0, err
    assert out.strip() == path, out


def test_tools_are_plain_callables():
    # FastMCP's @mcp.tool returns the undecorated function; the CLI's getattr(server, name)(...)
    # and every test in this suite depend on that SDK detail.
    from nt8_mcp import server
    for name in cli._tool_names():
        assert callable(getattr(server, name)), name
