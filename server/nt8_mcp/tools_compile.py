# Portions of this file are derived from cli-nt-bridge
# (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
# MIT License. The full notice is in NOTICE at the repository root.
"""Compile NinjaScript through the AddOn. See ../../docs/api/compile.md.

`POST /compile` runs NinjaTrader's own compiler with `checkCompileOnly=true`: it answers
"does this tree build" with `{file,line,column,code,message}` per error, needs no NinjaScript
Editor window, and disturbs nothing. `POST /compile?reload=1` runs the same compiler for real
and NinjaTrader swaps the assembly as F5 does — disruptive, refused while a live order-routing
connection is up, and reachable only through its own tool `nt_reload_assembly`, never through a
flag on `nt_compile`.
"""

import json
import os
import socket
import time
import urllib.error
import urllib.request

from nt8_mcp import app  # BASE_URL / NT_HOME / NT_CUSTOM are read through the module so tests can patch them
from nt8_mcp import tools_local  # through the module: the F5 fallback is patched by name in tests
from nt8_mcp.app import NOT_REACHABLE, _addon_get, _SCRIPT_FOLDERS, mcp

# The anchored region regex, not a second copy of it: a plain substring match truncates a file at
# any COMMENT that mentions the region, and one rule for that lives in tools_local.
from nt8_mcp.tools_local import _GENERATED_REGION

COMPILE_TIMEOUT_S = 120  # a real tree compiles slower than 30 s (cli-nt-bridge CHANGELOG:660)
RELOAD_TIMEOUT_S = 240
LAST_WAIT_S = 20  # how long to wait for compile_last.json after the socket drops
LAST_POLL_S = 1
VERIFY_WAIT_S = 30  # how long to wait for /health to show the swapped assembly
VERIFY_POLL_S = 2


def _post_compile(reload: bool, force: bool, timeout_s: int) -> dict:
    """POST /compile with a compile-sized timeout (app's shared helper allows 5 s). A dropped
    socket comes back as {"error": ..., "_transport": True} — the caller falls back to the file."""
    url = app.BASE_URL.rstrip("/") + "/compile" + ("?reload=1" if reload else "")
    data = json.dumps({"force": force}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # 409 refusal, 500, ... — the body is the answer
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": f"HTTP {e.code}: {e.reason}"}
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        return {"error": f"{NOT_REACHABLE} ({e})", "_transport": True}


def _compile_last_path() -> str:
    return os.path.join(app.NT_HOME, "NT8Bridge", "compile_last.json")


def _wait_for_compile_last(after_ts: float):
    """The AddOn's own record of the compile, written just before it answers. Only accepted if it
    is newer than the request we sent, so a stale file can never be reported as this run."""
    path = _compile_last_path()
    deadline = time.time() + LAST_WAIT_S
    while True:
        try:
            if os.path.getmtime(path) >= after_ts - 2:  # 2 s of clock slack
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
        except (OSError, ValueError):
            pass
        if time.time() >= deadline:
            return None
        time.sleep(LAST_POLL_S)


def _health_stamp():
    """(assemblyBuiltUtc, startedAt) of whatever is answering on :7891, or (None, None)."""
    health = _addon_get("/health")
    if not isinstance(health, dict) or "error" in health:
        return (None, None)
    return (health.get("assemblyBuiltUtc"), health.get("startedAt"))


def _verify_reload(before):
    """Did the assembly really get swapped? The compile response cannot know — it is written by
    the PRE-swap assembly. This is the out-of-band half: re-read /health and compare `startedAt`
    ONLY. `startedAt` is set once, in TryBind(), when a process binds :7891 — a new instance
    always reports a new one, and an old instance that never restarted can never produce one.
    `assemblyBuiltUtc` is NOT safe to compare: it is the mtime of the file the running assembly
    loaded from, and on a cold start that file IS bin\\Custom\\NinjaTrader.Custom.dll — the exact
    file the reload's emit overwrites. So a pre-reload assembly that never restarted (a hung
    Start(), a port bind failure) reports a brand-new assemblyBuiltUtc while still being the old
    code, which is the false confirmation this check exists to prevent.
    Returns (True | False | None, note); None = not observable, keep the AddOn's claim."""
    if before[1] is None:
        return None, "/health could not be read before the compile, so the swap could not be observed"
    deadline = time.time() + VERIFY_WAIT_S
    after = before
    while True:
        after = _health_stamp()
        if after[1] is not None and after[1] != before[1]:
            return True, None
        if time.time() >= deadline:
            break
        time.sleep(VERIFY_POLL_S)
    if after[1] is None:
        return None, (f"the AddOn did not answer /health within {VERIFY_WAIT_S}s of the reload — the new "
                      "assembly may be broken or still loading; check GET /health and GET /ntstatus")
    return False, (f"/health still reports the pre-compile assembly's startedAt {VERIFY_WAIT_S}s later "
                   f"(startedAt={before[1]!r}) — the swap was claimed but not observed")


def _duplicated_regions() -> list[str]:
    """.cs files under bin\\Custom carrying more than one `#region NinjaScript generated code`.
    Every one of them compiles into the same assembly, so duplicates are the usual cause of a
    CS0111/CS0102 that names code nobody edited."""
    hits = []
    for folder in _SCRIPT_FOLDERS:
        for root, _dirs, files in os.walk(os.path.join(app.NT_CUSTOM, folder)):
            for name in files:
                if not name.endswith(".cs"):
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        if len(_GENERATED_REGION.findall(fh.read())) > 1:
                            hits.append(path)
                except OSError:
                    continue
    return sorted(hits)


def compile_via_addon(reload: bool = False, force: bool = False, timeout_s: int | None = None) -> dict:
    """Compile the NinjaScript tree through the AddOn. Not an MCP tool: `nt_compile` (check-only,
    with the F5 path as the cold-start fallback) and `nt_reload_assembly` are the two tools built on
    it. `reload=True` makes NinjaTrader swap the assembly — it restarts indicators, can
    interrupt a running strategy and orphans bars-type instances, and the AddOn refuses it with
    HTTP 409 while a live order-routing connection is up unless `force=True`."""
    timeout = timeout_s or (RELOAD_TIMEOUT_S if reload else COMPILE_TIMEOUT_S)
    sent_at = time.time()  # before the /health read: an unreachable AddOn makes that read slow, not instant
    before = _health_stamp() if reload else (None, None)

    result = _post_compile(reload, force, timeout)
    if result.pop("_transport", False):
        cached = _wait_for_compile_last(sent_at)
        if cached is None:
            # A timeout is NOT proof of failure: NinjaTrader may still be compiling.
            # `unreachable` says only "no verdict came back on this path" — it is what nt_compile
            # tests before it considers the F5 fallback, and it never means "the compile failed".
            result["unreachable"] = True
            result["hint"] = (
                "no response and no compile_last.json newer than the request. Either NinjaTrader is still "
                f"compiling (wait, then re-read GET /health and {_compile_last_path()}), or the AddOn is "
                "not loaded (GET /health does not answer at all)."
            )
        else:
            cached["source"] = "compile_last.json"
            cached["note"] = ("the socket dropped before the response arrived — a reload tears this listener "
                              "down mid-request; this is the AddOn's own record of the same compile")
            result = cached
    else:
        result.setdefault("source", "http")

    if reload and result.get("assemblyReloaded"):
        observed, note = _verify_reload(before)
        if observed is True:
            result["assemblyReloadedSource"] = "observed: /health reports a different startedAt"
        else:
            # observed is False only for the one case that disproves the claim: /health answered
            # both times and startedAt did not move. observed is None (unreadable before, or no
            # answer within the timeout after) is NOT disproof — leave the AddOn's claim standing.
            if observed is False:
                result["assemblyReloaded"] = False
            result["assemblyReloadedSource"] = "claimed by the AddOn, not observed"
            result["assemblyReloadedNote"] = note

    if result.get("ok") is False:  # a real failed build, not a 409 refusal or a timeout hint
        try:
            dups = _duplicated_regions()
            if dups:
                result["duplicatedRegions"] = dups
                result["duplicatedRegionsHint"] = (
                    "each of these files has more than one `#region NinjaScript generated code`; they all "
                    "compile into one assembly, so duplicated members show up as CS0111/CS0102 against code "
                    "you did not touch. nt_install strips the stale region (keeping a .bak)."
                )
        except Exception as exc:  # a scan failure must never mask the compile result
            result["duplicatedRegionsError"] = str(exc)
    return result


@mcp.tool(name="nt_compile")
def nt_compile(timeout_s: int = COMPILE_TIMEOUT_S) -> dict:
    """Compile-check every NinjaScript .cs under bin\\Custom with NinjaTrader's own compiler, through the
    AddOn — no NinjaScript Editor window needed and no screenshot to read. Preconditions: NinjaTrader is
    running; install your files first (nt_install /
    nt_install_addon), because the compiler builds what is on disk, not what you have in the repo. Returns
    {ok, errors, warnings, seconds, assemblyReloaded, ...} where errors and warnings are lists of
    {file, line, column, code, message} with 1-based line and column; a failing build adds duplicatedRegions
    when duplicated generated regions could explain a CS0111/CS0102. This NEVER swaps the running assembly:
    assemblyReloaded is always false here, nothing is reloaded, no indicator restarts and no strategy is
    interrupted, so new types do not become available until NinjaTrader reloads (it does that by itself
    20-150 s after a .cs lands in bin\\Custom). A timeout is not proof of failure — the compile keeps going
    and its result is also written to <Documents>\\NinjaTrader 8\\NT8Bridge\\compile_last.json.
    If the AddOn is not loaded at all, this falls back by itself to the F5 path (nt_compile_f5) and says so
    in `fallback`; that path IS a real compile and swaps the assembly."""
    result = compile_via_addon(reload=False, timeout_s=timeout_s)
    if not result.pop("unreachable", False):
        return result

    # No answer and no compile_last.json. Only the cold-start case earns the F5 fallback: F5 is a REAL
    # compile that swaps the assembly, so an AddOn that is merely slow (still compiling) must not get a
    # second build queued behind the first. /health answering at all settles which one this is.
    health = _addon_get("/health")
    if isinstance(health, dict) and "error" not in health:
        return result
    fallback = tools_local.nt_compile_f5()
    fallback["fallback"] = "nt_compile_f5"
    fallback["fallbackWhy"] = f"the AddOn did not answer: {result.get('hint') or result.get('error')}"
    return fallback


@mcp.tool(name="nt_reload_assembly")
def nt_reload_assembly(timeout_s: int = RELOAD_TIMEOUT_S) -> dict:
    """Compile for real AND make NinjaTrader swap the running assembly (`POST /compile?reload=1`), so new and
    changed types become available without waiting for NT8's own 20-150 s folder watcher. This is DISRUPTIVE:
    every indicator restarts, a running strategy can be interrupted, and bars-type instances are ORPHANED —
    they keep executing the pre-reload assembly and only restarting the NinjaTrader process fixes that, which
    matters here because of the OFS_* bars types. The
    AddOn refuses it with HTTP 409 while a live order-routing connection is up, and there is no way to
    override that from here (the endpoint's `force` is deliberately not exposed as a tool argument).
    Preconditions: NinjaTrader running with the AddOn loaded (GET /health answers on
    :7891); your files already installed. Returns what nt_compile returns plus `assemblyReloaded` and
    `assemblyReloadedSource`: the swap is verified out of band against /health's `startedAt`, and a claim
    that could not be observed is reported as a claim, never as a fact. The socket is torn down by the
    reload itself, so the answer often comes from compile_last.json (`source`) — that is normal, not an
    error. Use nt_compile when you only want to know whether the tree builds."""
    return compile_via_addon(reload=True, timeout_s=timeout_s)
