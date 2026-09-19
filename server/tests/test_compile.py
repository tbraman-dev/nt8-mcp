"""POST /compile through nt8_mcp.tools_compile, against the canned AddOn in fake_addon.py."""

import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import app, tools_compile, tools_local  # noqa: E402
from nt8_mcp import server as nt8  # noqa: E402

OK_BODY = {
    "ok": True, "assemblyReloaded": False, "reloadRequested": False, "checkCompileOnly": True,
    "seconds": 24.3, "errors": [], "warnings": [], "warningCount": 0, "warningsSuppressed": 0,
    "assemblyBuiltUtc": "2026-09-18T10:00:00Z", "startedAt": "2026-09-18T12:00:00",
}
BAD_BODY = dict(OK_BODY, ok=False, errors=[{
    "file": "C:\\Users\\x\\Documents\\NinjaTrader 8\\bin\\Custom\\Strategies\\SampleMACrossOver.cs",
    "line": 184, "column": 13, "code": "CS0103",
    "message": "The name 'foo' does not exist in the current context",
}])


def _fast():
    """Kill every wait in the module and hand back the saved values."""
    saved = (tools_compile.LAST_WAIT_S, tools_compile.LAST_POLL_S,
             tools_compile.VERIFY_WAIT_S, tools_compile.VERIFY_POLL_S,
             app.NT_HOME, app.NT_CUSTOM)
    tools_compile.LAST_WAIT_S = tools_compile.LAST_POLL_S = 0
    tools_compile.VERIFY_WAIT_S = tools_compile.VERIFY_POLL_S = 0
    return saved


def _restore(saved):
    (tools_compile.LAST_WAIT_S, tools_compile.LAST_POLL_S,
     tools_compile.VERIFY_WAIT_S, tools_compile.VERIFY_POLL_S,
     app.NT_HOME, app.NT_CUSTOM) = saved


def test_check_only_never_asks_for_a_reload():
    seen = {}

    def handler(req):
        seen["query"] = req.query
        seen["body"] = json.loads(req.body)
        return OK_BODY

    saved = _fast()
    try:
        with FakeAddon() as fake:
            fake.register("/compile", "POST", handler)
            result = nt8.nt_compile()
    finally:
        _restore(saved)
    assert seen["query"] == {}, seen            # no reload=1: a validate step must not swap the assembly
    assert seen["body"] == {"force": False}
    assert result["ok"] is True and result["assemblyReloaded"] is False
    assert result["source"] == "http"


def test_errors_come_back_structured_not_as_a_screenshot():
    saved = _fast()
    try:
        with FakeAddon() as fake:
            fake.json("/compile", BAD_BODY, method="POST")
            app.NT_CUSTOM = tempfile.mkdtemp(prefix="ntcompile_empty_")  # no duplicated regions to find
            result = nt8.nt_compile()
    finally:
        _restore(saved)
    assert result["ok"] is False
    error = result["errors"][0]
    assert error["line"] == 184 and error["column"] == 13 and error["code"] == "CS0103"
    assert "duplicatedRegions" not in result


def test_live_connection_refusal_passes_through():
    # The AddOn answers 409 with a body; urllib raises HTTPError and the body is still the answer.
    saved = _fast()
    try:
        with FakeAddon() as fake:
            fake.json("/compile", {"error": "compile?reload=1 refused: a live order-routing connection is up",
                                   "anyLive": True}, method="POST", status=409)
            app.NT_CUSTOM = tempfile.mkdtemp(prefix="ntcompile_empty_")
            result = tools_compile.compile_via_addon(reload=True)
    finally:
        _restore(saved)
    assert result["anyLive"] is True and "refused" in result["error"]
    assert "assemblyReloaded" not in result


def test_reload_is_verified_against_health_not_believed():
    def health(req):
        # 1st call: before the compile. Later calls: the swapped assembly answering with a new
        # startedAt (set once in TryBind() when a process binds the port) — the one field that
        # proves a real restart happened.
        started = "2026-09-18T12:00:00" if req.n == 1 else "2026-09-18T13:37:00"
        return {"ok": True, "assemblyBuiltUtc": "2026-09-18T10:00:00Z", "startedAt": started}

    saved = _fast()
    try:
        with FakeAddon() as fake:
            fake.register("/health", "GET", health)
            fake.json("/compile", dict(OK_BODY, assemblyReloaded=True, reloadRequested=True,
                                       checkCompileOnly=False), method="POST")
            result = tools_compile.compile_via_addon(reload=True)
    finally:
        _restore(saved)
    assert result["assemblyReloaded"] is True
    assert result["assemblyReloadedSource"].startswith("observed")


def test_assemblybuiltutc_alone_changing_is_not_mistaken_for_a_swap():
    # The bug this guards against: a pre-reload assembly that never restarted (hung Start(), a
    # failed port bind) still answers /health, and the reload's emit overwrites the very file its
    # own Assembly.Location points at — so assemblyBuiltUtc changes even though the SAME process
    # (same startedAt) is still running the OLD code. That must not be read as a verified swap.
    def health(req):
        stamp = "2026-09-18T10:00:00Z" if req.n == 1 else "2026-09-18T15:00:00Z"  # dll mtime moved
        return {"ok": True, "assemblyBuiltUtc": stamp, "startedAt": "2026-09-18T12:00:00"}  # same process

    saved = _fast()
    try:
        with FakeAddon() as fake:
            fake.register("/health", "GET", health)
            fake.json("/compile", dict(OK_BODY, assemblyReloaded=True, reloadRequested=True,
                                       checkCompileOnly=False), method="POST")
            result = tools_compile.compile_via_addon(reload=True)
    finally:
        _restore(saved)
    assert result["assemblyReloaded"] is False
    assert result["assemblyReloadedSource"] == "claimed by the AddOn, not observed"


def test_no_health_response_after_reload_leaves_the_claim_standing():
    # /health answers before the compile but goes unreadable after (still loading, or broken).
    # docs/api/compile.md: the downgrade to False applies ONLY when /health answered both times
    # and startedAt did not move — not to "it never answered again", which proves nothing either
    # way and must leave the AddOn's own claim alone.
    def health(req):
        if req.n == 1:
            return {"ok": True, "assemblyBuiltUtc": "2026-09-18T10:00:00Z", "startedAt": "2026-09-18T12:00:00"}
        return {"error": "not answering"}

    saved = _fast()
    try:
        with FakeAddon() as fake:
            fake.register("/health", "GET", health)
            fake.json("/compile", dict(OK_BODY, assemblyReloaded=True, reloadRequested=True,
                                       checkCompileOnly=False), method="POST")
            result = tools_compile.compile_via_addon(reload=True)
    finally:
        _restore(saved)
    assert result["assemblyReloaded"] is True
    assert result["assemblyReloadedSource"] == "claimed by the AddOn, not observed"
    assert "did not answer" in result["assemblyReloadedNote"]


def test_an_unobserved_reload_claim_is_downgraded_to_false():
    saved = _fast()
    try:
        with FakeAddon() as fake:
            # /health never changes: the assembly was not swapped, whatever the compile claimed.
            fake.json("/health", {"ok": True, "assemblyBuiltUtc": "2026-09-18T10:00:00Z",
                                  "startedAt": "2026-09-18T12:00:00"})
            fake.json("/compile", dict(OK_BODY, assemblyReloaded=True, reloadRequested=True,
                                       checkCompileOnly=False), method="POST")
            result = tools_compile.compile_via_addon(reload=True)
    finally:
        _restore(saved)
    assert result["assemblyReloaded"] is False
    assert result["assemblyReloadedSource"] == "claimed by the AddOn, not observed"
    assert "not observed" in result["assemblyReloadedNote"]


def test_a_dropped_socket_falls_back_to_compile_last_json():
    # A reload tears the listener down mid-request, so the result arrives as a file or not at all.
    home = tempfile.mkdtemp(prefix="ntcompile_home_")
    os.makedirs(os.path.join(home, "NT8Bridge"))
    with open(os.path.join(home, "NT8Bridge", "compile_last.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(OK_BODY, assemblyReloaded=True, reloadRequested=True), fh)

    saved = _fast()
    saved_url = app.BASE_URL
    try:
        app.NT_HOME = home
        app.BASE_URL = "http://127.0.0.1:1"  # nothing listening: transport failure on every call
        result = tools_compile.compile_via_addon(reload=True)
    finally:
        app.BASE_URL = saved_url
        _restore(saved)
    assert result["source"] == "compile_last.json"
    assert result["ok"] is True
    # /health was unreadable before and after, so the claim stands but is labelled as a claim.
    assert result["assemblyReloaded"] is True
    assert result["assemblyReloadedSource"] == "claimed by the AddOn, not observed"


def test_no_result_and_no_file_gives_a_hint_not_a_verdict():
    saved = _fast()
    saved_url = app.BASE_URL
    try:
        app.NT_HOME = tempfile.mkdtemp(prefix="ntcompile_home_")  # no compile_last.json at all
        app.NT_CUSTOM = tempfile.mkdtemp(prefix="ntcompile_empty_")
        app.BASE_URL = "http://127.0.0.1:1"
        result = tools_compile.compile_via_addon(reload=False)
    finally:
        app.BASE_URL = saved_url
        _restore(saved)
    assert "ok" not in result                       # a timeout is never reported as a failed compile
    assert "still compiling" in result["hint"]
    assert result["unreachable"] is True            # what nt_compile tests before it considers F5


def test_duplicated_regions_are_attached_to_a_failing_compile():
    custom = tempfile.mkdtemp(prefix="ntcompile_custom_")
    os.makedirs(os.path.join(custom, "Indicators"))
    two = os.path.join(custom, "Indicators", "Dup.cs")
    with open(two, "w", encoding="utf-8") as fh:
        fh.write("// mentions #region NinjaScript generated code in a comment\n"
                 "#region NinjaScript generated code\n#endregion\n"
                 "#region NinjaScript generated code\n#endregion\n")
    one = os.path.join(custom, "Indicators", "Fine.cs")
    with open(one, "w", encoding="utf-8") as fh:
        fh.write("#region NinjaScript generated code\n#endregion\n")

    saved = _fast()
    try:
        app.NT_CUSTOM = custom
        with FakeAddon() as fake:
            fake.json("/compile", BAD_BODY, method="POST")
            result = nt8.nt_compile()
    finally:
        _restore(saved)
    assert result["duplicatedRegions"] == [two]     # the comment line must not count, Fine.cs must not appear
    assert "CS0111" in result["duplicatedRegionsHint"]


def _transport_failure(*_a, **_k):
    """What _post_compile returns when the socket never answers."""
    return {"error": tools_compile.NOT_REACHABLE, "_transport": True}


def test_a_cold_start_falls_back_to_the_f5_path():
    # The AddOn is not loaded at all: nothing answers /compile and nothing answers /health either.
    called = []
    saved = _fast()
    saved_url = app.BASE_URL
    saved_hooks = (tools_compile._post_compile, tools_local.nt_compile_f5)
    try:
        app.NT_HOME = tempfile.mkdtemp(prefix="ntcompile_home_")   # no compile_last.json
        app.BASE_URL = "http://127.0.0.1:1"
        tools_compile._post_compile = _transport_failure
        tools_local.nt_compile_f5 = lambda *a, **k: called.append(1) or {"ok": True, "seconds": 3.0}
        result = nt8.nt_compile()
    finally:
        tools_compile._post_compile, tools_local.nt_compile_f5 = saved_hooks
        app.BASE_URL = saved_url
        _restore(saved)
    assert called == [1]
    assert result["ok"] is True and result["fallback"] == "nt_compile_f5"


def test_a_slow_addon_never_gets_a_second_compile_queued_behind_it():
    # /health answers, so NinjaTrader is up and the compile is probably still running. F5 is a REAL
    # compile that swaps the assembly — pressing it here would queue a second build behind the first.
    called = []
    saved = _fast()
    saved_hooks = (tools_compile._post_compile, tools_local.nt_compile_f5)
    try:
        app.NT_HOME = tempfile.mkdtemp(prefix="ntcompile_home_")
        tools_compile._post_compile = _transport_failure
        tools_local.nt_compile_f5 = lambda *a, **k: called.append(1) or {"ok": True}
        with FakeAddon() as fake:
            fake.json("/health", {"ok": True, "startedAt": "2026-09-19T09:00:00"})
            result = nt8.nt_compile()
    finally:
        tools_compile._post_compile, tools_local.nt_compile_f5 = saved_hooks
        _restore(saved)
    assert called == [], "F5 was pressed while the AddOn was still answering"
    assert "ok" not in result and "still compiling" in result["hint"]
    assert "unreachable" not in result          # an internal marker, never part of the answer


def test_the_reload_tool_asks_for_the_reload_and_nt_compile_never_does():
    seen = {}

    def handler(req):
        seen.setdefault("queries", []).append(req.query)
        return dict(OK_BODY, assemblyReloaded=True, reloadRequested=True, checkCompileOnly=False)

    saved = _fast()
    try:
        with FakeAddon() as fake:
            fake.register("/compile", "POST", handler)
            fake.json("/health", {"ok": True, "startedAt": "2026-09-19T09:00:00"})
            nt8.nt_compile()
            nt8.nt_reload_assembly()
    finally:
        _restore(saved)
    assert seen["queries"] == [{}, {"reload": ["1"]}], seen


def test_the_compile_tools_are_three_distinct_registered_names():
    # Two @mcp.tool functions with the same name break the whole MCP server, not just one module.
    names = [t.name for t in nt8.mcp._tool_manager.list_tools()]
    for name in ("nt_compile", "nt_compile_f5", "nt_reload_assembly"):
        assert names.count(name) == 1, f"{name} is registered {names.count(name)} times"
    assert "nt_compile_addon" not in names      # the old name is gone
    assert nt8.nt_compile is not nt8.nt_compile_f5
    assert nt8.nt_compile is not tools_compile.compile_via_addon
