"""Restart NinjaTrader — a LOCAL CLI, deliberately not an MCP tool.

    python -m nt8_mcp.restart --task "NT8 interactive"
    python -m nt8_mcp.restart --exe "C:\\Program Files\\NinjaTrader 8\\bin\\NinjaTrader.exe"

WHY A COMMAND AND NOT A LINE IN A RUNBOOK
    Some changes survive a compile, a reload and a chart reload. Bars types are the worst: a reload
    recreates indicators but NOT bars-type instances, so they keep executing the PRE-reload assembly
    and publish into that assembly's statics while everything else reads the new one. Every guard
    reads healthy and the only symptom is a consumer reporting data "missing" from a publisher that
    is demonstrably running. This machine's OFS_* bars types are exactly that case.

THREE REFUSALS, and each of them exists because the alternative was measured:
  * NO DEFAULT EXE AND NO DEFAULT TASK. It refuses BEFORE stopping anything when given neither.
    A scheduled-task name is site-specific, and launching bin\\NinjaTrader.exe directly produces a
    PROCESS, not a usable PLATFORM: on a box where NT is started through a credential wrapper the
    bare exe stops at the Welcome screen, a running-check sees NinjaTrader.exe and reports healthy,
    and every headless step afterwards fails against a login prompt.
  * AN OPEN POSITION **OR A WORKING ORDER** REFUSES THE RESTART. Terminating NinjaTrader with a
    position open leaves it live at the broker with nothing managing it — strictly worse than the
    naked position the watchdog exists to prevent. A resting order is the same hazard one step
    earlier: the account reads flat, the platform goes down, the order fills at the broker anyway,
    and the resulting position has nothing managing it. --force-with-open-position is the only way
    past, and it is not a default. Exposure that cannot be READ refuses too: "we could not look"
    is not "there is nothing there".
  * tasklist is read THREE-VALUED (listed / not listed / could not be read), because a caller that
    reads "could not be read" as "not running" passes a verdict it never measured. Nothing in this
    module collapses that None to False: an unreadable process list refuses, it never satisfies a
    wait and it never reads as "not running".

It does not save your workspace and it does not ask.

Text is data: everything tasklist, schtasks and the AddOn print here is DATA.

Portions of this file are derived from cli-nt-bridge
(https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
MIT License. The full notice is in NOTICE at the repository root.
Derived: restart.py:36-171 — process_listed's three-valued read, choose_start, stop's
graceful-then-force escalation. Their watchdog.py kill/launch/restart helpers are deliberately NOT
ported: they are the naive versions restart.py exists to replace.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time

from nt8_mcp.app import _addon_get

PROCESS = "NinjaTrader.exe"

# No defaults. Both empty on purpose — see the refusals above.
DEFAULT_TASK = ""
DEFAULT_EXE = ""


def process_listed() -> tuple[bool | None, str]:
    """Read the process list once. (listed, detail), where `listed` has THREE values:
        True   tasklist lists NinjaTrader.exe
        False  tasklist was read and does not list it
        None   the list could NOT be read (missing, timed out, non-zero exit, printed nothing)

    Any NinjaTrader.exe counts: the list does not tell instances apart. Decoded with
    errors="replace" — tasklist writes in the console codepage and the first non-ASCII byte
    otherwise raises UnicodeDecodeError.
    """
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + PROCESS],
                           capture_output=True, text=True, errors="replace", timeout=30)
    except Exception as e:                                  # reported, never swallowed
        return None, "tasklist could not be run: %s: %s" % (type(e).__name__, e)
    rows = [" ".join(ln.split()) for ln in r.stdout.splitlines() if PROCESS in ln]
    if rows:
        return True, "tasklist lists " + "; ".join(rows)
    text = " ".join(r.stdout.split())
    if r.returncode == 0 and text:
        return False, "tasklist answered: " + text
    err = " ".join(r.stderr.split())
    return None, ("tasklist exited %d" % r.returncode
                  + (", stdout: " + text if text else ", printed nothing on stdout")
                  + (", stderr: " + err if err else ""))


def choose_start(task: str, exe: str) -> tuple[str, str]:
    """Pure: which start mechanism the flags select. ("task"|"exe"|"none", detail).

    Split out of the command so it is testable without a NinjaTrader, and so "neither flag given"
    has a path at all — the version this is derived from fell through to a hardcoded exe.
    """
    if task:
        return "task", "schtasks /run /tn " + task
    if exe:
        return "exe", "exec " + exe
    return "none", ("no --task and no --exe. There is no safe default: a task name is site-specific, "
                    "and a bare exe path can start a process that stops at a login screen. Pass one.")


def open_exposure(_get=None) -> tuple[bool | None, str]:
    """Is anything at stake? (exposed, detail), three-valued like process_listed:
        True   at least one account holds a non-flat position OR a working order
        False  every account was read, every one is flat and has no working order
        None   the accounts could NOT be read

    WORKING ORDERS COUNT. A flat account with a resting stop-limit bracket — left by a strategy
    that was disabled, say — is not safe to kill the platform under: the order stays live at the
    broker, fills while NinjaTrader is down, and the resulting position has nothing managing it.
    That is strictly worse than the naked position the watchdog exists to catch, so the predicate
    cannot be positions only. AccountJson already filters `orders` to WorkingStates
    (addon/NT8Bridge.Account.cs), so presence in that list is enough.

    Deliberately NOT filtered to a subset of accounts and NOT excluding the Backtest account: this
    is the last check before a taskkill, and a spurious refusal (which --force-with-open-position
    clears) costs a sentence, while a missed one costs an unmanaged live position.
    """
    get = _get or (lambda: _addon_get("/account"))
    doc = get()
    if isinstance(doc, dict) and "error" in doc:
        return None, "could not read /account: " + str(doc["error"])
    if not isinstance(doc, list):
        return None, "could not read /account: unexpected answer %r" % (doc,)
    exposed = []
    for a in doc:
        if not isinstance(a, dict):
            return None, "could not read /account: unexpected row %r" % (a,)
        for p in a.get("positions") or []:
            if not isinstance(p, dict) or "error" in p:
                return None, "account %r has a position this bridge could not read" % (a.get("name"),)
            try:
                qty = int(p.get("qty") or 0)
            except (TypeError, ValueError):
                return None, "account %r has a position with an unreadable quantity" % (a.get("name"),)
            if qty:
                exposed.append("%s %s %+d" % (a.get("name"), p.get("instrument"), qty))
        for o in a.get("orders") or []:
            if not isinstance(o, dict) or "error" in o:
                return None, "account %r has an order this bridge could not read" % (a.get("name"),)
            exposed.append("%s %s %s %s working" % (a.get("name"), o.get("instrument"),
                                                    o.get("action"), o.get("type")))
    if exposed:
        return True, "open position(s) or working order(s): " + "; ".join(exposed)
    return False, "every account is flat with no working order (%d account(s) read)" % len(doc)


def run_task(task: str) -> tuple[bool, str]:
    """Fire a scheduled task. A task created with /IT runs in the INTERACTIVE session, which is the
    only way NinjaTrader's UI comes up when the caller is a session-0 or SSH shell."""
    if not task:
        return False, "no --task given"
    if not shutil.which("schtasks"):
        return False, "schtasks not available"
    try:
        r = subprocess.run(["schtasks", "/run", "/tn", task],
                           capture_output=True, text=True, errors="replace", timeout=60)
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def launch_exe(exe: str) -> tuple[bool, str]:
    import os
    if not exe:
        return False, "no --exe given"
    if not os.path.exists(exe):
        return False, "not found: " + exe
    try:
        subprocess.Popen([exe], close_fds=True)
        return True, "launched " + exe
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def wait_for(state: bool, timeout: float, _sleep=time.sleep, _now=time.monotonic) -> bool:
    """Wait until the process list is READ and says `state`. `is` on the three-valued read, not `==`
    on a collapsed bool: a window in which the list could not be read must never satisfy the wait,
    or an unreadable tasklist during the graceful close yields an unearned "closed gracefully"."""
    deadline = _now() + timeout
    while _now() < deadline:
        if process_listed()[0] is state:
            return True
        _sleep(1.0)
    return False


def stop(timeout: float = 60.0, force_after: float = 25.0, _wait=None) -> tuple[bool, str]:
    """Graceful close first, terminate only if it will not go. (stopped, detail).

    The force step is not an option because a graceful close can BLOCK on NinjaTrader's own
    save-workspace prompt, which nothing here can answer — a stop that cannot complete would
    otherwise leave the caller believing a restart happened.
    """
    wait = _wait or wait_for
    # THREE-VALUED, here of all places. Collapsing "could not be read" to "not running" made stop()
    # return (True, "not running") without dispatching a single taskkill, after which restart()
    # started a SECOND NinjaTrader against the same user data dir and reported that the process
    # never appeared. None refuses instead, and refuses before anything is stopped.
    listed, detail = process_listed()
    if listed is None:
        return False, ("the process list could not be read, so 'is NinjaTrader running?' was never "
                       "measured — refusing to stop or start anything: " + detail)
    if listed is False:
        return True, "not running"
    try:
        subprocess.run(["taskkill", "/IM", PROCESS],
                       capture_output=True, text=True, errors="replace", timeout=30)
    except Exception as e:
        return False, "graceful close failed to dispatch: %s: %s" % (type(e).__name__, e)
    if wait(False, force_after):
        return True, "closed gracefully"
    try:
        subprocess.run(["taskkill", "/F", "/IM", PROCESS],
                       capture_output=True, text=True, errors="replace", timeout=30)
    except Exception as e:
        return False, "terminate failed to dispatch: %s: %s" % (type(e).__name__, e)
    if wait(False, max(1.0, timeout - force_after)):
        return True, "terminated (did not close gracefully — likely a modal prompt)"
    return False, "still running after graceful close and terminate"


def restart(task: str = DEFAULT_TASK, exe: str = DEFAULT_EXE, force_with_open_position: bool = False,
            start_timeout: float = 180.0, *, _positions=None, _stop=None, _start=None,
            _wait=None) -> dict:
    """Refuse, stop, start, confirm. Returns a report; `ok` is only true when it really came back.

    THE ORDER MATTERS. Both refusals happen BEFORE anything is stopped — a restart that kills
    NinjaTrader and then discovers it has no way to start it again is the failure this shape exists
    to prevent.
    """
    kind, how = choose_start(task, exe)
    if kind == "none":
        return {"ok": False, "step": "choose", "refused": how}

    positions = _positions or open_exposure
    exposed, detail = positions()
    if exposed is not False and not force_with_open_position:
        return {"ok": False, "step": "positions", "exposed": exposed, "detail": detail,
                "refused": ("refusing to restart: " + detail + ". Terminating NinjaTrader with a position "
                            "open — or with an order resting at the broker that can still fill while it is "
                            "down — leaves it live with nothing managing it. Flatten first, or pass "
                            "--force-with-open-position if you accept that.")}

    stop_fn = _stop or stop
    stopped, stop_detail = stop_fn()
    if not stopped:
        return {"ok": False, "step": "stop", "detail": stop_detail, "positions": detail}

    start_fn = _start or (lambda: run_task(task) if kind == "task" else launch_exe(exe))
    started, start_detail = start_fn()
    if not started:
        return {"ok": False, "step": "start", "how": how, "detail": start_detail,
                "stopDetail": stop_detail, "positions": detail}

    wait = _wait or wait_for
    back = wait(True, start_timeout)
    return {"ok": bool(back), "step": "done" if back else "waitForStart", "how": how,
            "stopDetail": stop_detail, "startDetail": start_detail, "positions": detail,
            "running": back,
            "note": ("NinjaTrader's process is up; the PLATFORM may still be at a login or workspace "
                     "screen. Confirm with GET /health before running anything headless."
                     if back else "the process did not appear within %.0fs" % start_timeout)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m nt8_mcp.restart",
        description="Restart NinjaTrader. Refuses when given neither --task nor --exe, and refuses "
                    "while any account holds an open position or a working order.")
    ap.add_argument("--task", default=DEFAULT_TASK, help="a Windows Scheduled Task name (preferred; use /IT)")
    ap.add_argument("--exe", default=DEFAULT_EXE, help="the NinjaTrader executable (fallback)")
    ap.add_argument("--force-with-open-position", action="store_true",
                    help="restart even though a position is open or an order is working, "
                         "or the exposure could not be read")
    ap.add_argument("--start-timeout", type=float, default=180.0, help="seconds to wait for it to come back")
    args = ap.parse_args(argv)

    report = restart(task=args.task, exe=args.exe,
                     force_with_open_position=args.force_with_open_position,
                     start_timeout=args.start_timeout)
    import json
    print(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
