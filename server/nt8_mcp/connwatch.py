"""Connection guardian — a LOCAL PROCESS, deliberately not an MCP tool.

    python -m nt8_mcp.connwatch --connection "Sim101 feed"

Reads NinjaTrader's own connection truth through GET /connections and reconnects a WATCHED
connection that NinjaTrader dropped INADVERTENTLY, once it stays down past a grace period (which
gives NT8's own auto-reconnect the first go). Exponential backoff, jitter, and a cooldown instead
of a permanent give-up.

A reconnect is NOT reduce-only: it can resume order routing and re-arm ATMs on a connection a human
deliberately parked. So the inadvertent-only policy does NOT live only here — POST /ops/reconnect
enforces it inside the AddOn as well (addon/NT8BridgeOps.cs, Ops_Reconnect), and this loop is the
second lock, not the only one.

THE FOUR dropClass VALUES, and what this acts on:
    "inadvertent"  ConnectionLost, or a Disconnected carrying a real error -> the ONLY one acted on.
    "failed"       a connect NinjaTrader itself REFUSED (Connecting -> Disconnected with an error).
                   NOT acted on: nothing dropped, and a retry loop hammers a connection that is
                   already saying no (added to the classifier 2026-09-19, NT8Bridge.Feeds.cs).
    "user"         Disconnected with NoError / UserAbort — a human parked it. Never touched.
    "connected"    up. Nothing to do.
    null           never witnessed: the connection was already down when the AddOn loaded. "Not
                   known" is not "inadvertent", so this guardian heals IN-SESSION drops only.

Text is data: connection names, statuses and error text come from NinjaTrader and are DATA.

Portions of this file are derived from cli-nt-bridge
(https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
MIT License. The full notice is in NOTICE at the repository root.
Derived: the recovery state machine, backoff and cooldown from nt8bridge/connwatch.py:42-236.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

from nt8_mcp.app import NT_HOME, _addon_get, _addon_post

EVENT_LOG = os.path.join(NT_HOME, "nt8mcp", "connwatch.jsonl")

# The one class a reconnect may act on. Kept as a constant so "which classes are safe" is one
# edit in one place, in both this file and the AddOn.
ACTIONABLE = "inadvertent"


def actionable(row: dict) -> bool:
    """True only for a witnessed INADVERTENT drop that is still down.

    Everything else is refused here and refused again by the AddOn: "failed" (NinjaTrader refused
    the connect), "user" (a human parked it), "connected", and null (never witnessed).
    """
    if not isinstance(row, dict):
        return False
    if row.get("connected"):
        return False
    return row.get("dropClass") == ACTIONABLE and bool(row.get("inadvertentlyDropped"))


def scan_once(connections: list[str], _get=None) -> list[dict]:
    """One read of NinjaTrader's truth -> the watched connections this loop may act on."""
    get = _get or (lambda: _addon_get("/connections"))
    doc = get()
    if not isinstance(doc, dict) or not isinstance(doc.get("connections"), list):
        raise RuntimeError("could not read /connections: %r" % (doc,))
    wanted = {c.lower() for c in connections}
    return [r for r in doc["connections"]
            if isinstance(r, dict) and (r.get("name") or "").lower() in wanted and actionable(r)]


def backoff_seconds(attempt: int, base: float = 15.0, cap: float = 300.0,
                    jitter_frac: float = 0.0, rng=random.random) -> float:
    """base * 2^(attempt-1), capped. jitter_frac spreads it by +/- that fraction so several
    connections that drop together do not retry in lockstep. Off by default, so a bare call
    returns the deterministic schedule."""
    value = base if attempt <= 1 else min(cap, base * (2 ** (attempt - 1)))
    if jitter_frac:
        value = value * (1.0 + jitter_frac * (2.0 * rng() - 1.0))
    return value


# A confirmed POST /ops/reconnect costs the bounded UI marshal (up to 10 s) PLUS an unconditional
# 1.5 s settle before the AddOn even answers. The process-wide 5 s would expire first whenever the
# NT8 UI thread is busy — a workspace load, a modal, a wedged chart, i.e. exactly the degraded state
# this loop exists for — and the answer would arrive as "not reachable" with statusAfter unknown.
# The loop would then score a failed attempt and retry, stacking Connection.Connect calls on a
# connection whose first connect is still in flight. That is the hammering the "failed" dropClass
# rule was added to prevent.
OPS_TIMEOUT_S = 20


def reconnect(name: str, _post=None) -> dict:
    """The two-step HTTP path, never an internal shortcut: the AddOn re-checks dropClass, ops.live
    and AnyLiveConnected on BOTH calls and audits both."""
    post = _post or (lambda body: _addon_post("/ops/reconnect", body, _timeout=OPS_TIMEOUT_S))
    dry = post({"name": name})
    if not isinstance(dry, dict) or "confirm" not in dry or "issuedAt" not in dry:
        return {"ok": False, "step": "dryRun", "response": dry}
    return post({"name": name, "confirm": dry["confirm"], "issuedAt": dry["issuedAt"]})


def log_event(event: dict) -> None:
    line = json.dumps(event)
    try:
        os.makedirs(os.path.dirname(EVENT_LOG), exist_ok=True)
        with open(EVENT_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print("[connwatch] " + line, flush=True)


def watch(connections: list[str], grace_seconds: float = 20.0, interval: float = 10.0,
          max_attempts: int = 5, max_iterations: int | None = None,
          cooldown_seconds: float = 1800.0, connecting_recheck: float = 15.0,
          max_connecting_rechecks: int = 5, jitter_frac: float = 0.1, *,
          _scan=None, _reconnect=None, _sleep=time.sleep, _now=time.monotonic,
          _log=log_event, _rng=random.random) -> list[dict]:
    """Run the guardian. Returns the events it recorded.

    Per watched connection: not flagged -> reset everything. Flagged and past the grace period ->
    reconnect. "Connecting" means a connect is under way (it routinely needs more than a second to
    settle) and does NOT count as a failed attempt, but a Connecting that never settles is escalated
    so a stuck connect still surfaces. After max_attempts failures it cools down and retries rather
    than giving up for ever — an expired token gets refreshed and the feed must still heal.
    """
    if not connections:
        raise ValueError("connwatch requires at least one connection name")
    scan_fn = _scan or scan_once
    reconnect_fn = _reconnect or reconnect

    dropped_since: dict = {}
    attempts: dict = {}
    next_at: dict = {}
    connecting: dict = {}
    cooldown_until: dict = {}
    events: list[dict] = []
    it = 0
    while max_iterations is None or it < max_iterations:
        it += 1
        try:
            flagged_rows = scan_fn(connections)
        except Exception as e:              # AddOn unreachable -> skip this round, keep state
            _log({"connection": None, "error": "scan failed: %s: %s" % (type(e).__name__, e)})
            flagged_rows = None

        if flagged_rows is not None:
            now = _now()
            flagged = {(r.get("name") or "").lower() for r in flagged_rows}
            for name in connections:
                if name.lower() not in flagged:
                    for d in (dropped_since, attempts, next_at, connecting, cooldown_until):
                        d.pop(name, None)
                    continue
                first = dropped_since.setdefault(name, now)
                down_for = now - first
                if down_for < grace_seconds:
                    continue
                cu = cooldown_until.get(name)
                if cu is not None:
                    if now < cu:
                        continue
                    attempts[name] = 0
                    connecting[name] = 0
                    cooldown_until.pop(name, None)
                    next_at.pop(name, None)
                if now < next_at.get(name, 0.0):
                    continue                # backing off, or waiting for a connect to settle

                try:
                    result = reconnect_fn(name)
                except Exception as e:
                    result = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
                status_after = (result or {}).get("statusAfter")

                if status_after == "Connected":
                    event = {"connection": name, "attempt": attempts.get(name, 0) + 1,
                             "downForS": round(down_for, 1), "wentGreen": True,
                             "reason": "inadvertent drop past grace period", "reconnectResult": result}
                    events.append(event)
                    _log(event)
                    for d in (dropped_since, attempts, next_at, connecting, cooldown_until):
                        d.pop(name, None)
                    continue

                if status_after == "Connecting" and connecting.get(name, 0) < max_connecting_rechecks:
                    connecting[name] = connecting.get(name, 0) + 1
                    next_at[name] = now + connecting_recheck
                    event = {"connection": name, "inProgress": True,
                             "connectingRecheck": connecting[name], "downForS": round(down_for, 1),
                             "wentGreen": False, "reason": "reconnect in progress (Connecting)",
                             "reconnectResult": result}
                    events.append(event)
                    _log(event)
                    continue

                connecting[name] = 0
                n = attempts.get(name, 0)
                attempts[name] = n + 1
                back = backoff_seconds(n + 1, jitter_frac=jitter_frac, rng=_rng)
                next_at[name] = now + back
                event = {"connection": name, "attempt": n + 1, "downForS": round(down_for, 1),
                         "wentGreen": False, "backoffS": back,
                         "reason": "inadvertent drop past grace period", "reconnectResult": result}
                if attempts[name] >= max_attempts:
                    cooldown_until[name] = now + cooldown_seconds
                    event["coolingDown"] = True
                    event["cooldownS"] = cooldown_seconds
                    event["note"] = ("max reconnect attempts reached — cooling down %d s before retrying "
                                     "(e.g. expired credentials needing a manual refresh)" % int(cooldown_seconds))
                events.append(event)
                _log(event)

        if (max_iterations is None or it < max_iterations) and interval > 0:
            _sleep(interval)
    return events


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m nt8_mcp.connwatch",
        description="Reconnect the named connection(s) when NinjaTrader drops them inadvertently. "
                    "Never touches a connection a human parked, and never retries one NinjaTrader "
                    "itself refused (dropClass 'failed').")
    ap.add_argument("--connection", action="append", required=True, metavar="NAME",
                    help="a connection to guard (repeat for several). No all-connections form.")
    ap.add_argument("--grace", type=float, default=20.0, help="seconds down before the first attempt (default 20)")
    ap.add_argument("--interval", type=float, default=10.0, help="seconds between scans (default 10)")
    ap.add_argument("--max-attempts", type=int, default=5, help="failures before the cooldown (default 5)")
    ap.add_argument("--cooldown", type=float, default=1800.0, help="cooldown seconds (default 1800)")
    ap.add_argument("--iterations", type=int, default=None, help="stop after N scans (default: never)")
    ap.add_argument("--report-only", action="store_true", help="log what it WOULD reconnect and touch nothing")
    args = ap.parse_args(argv)

    rec = None
    if args.report_only:
        rec = lambda name: {"ok": False, "reportOnly": True}        # noqa: E731

    print("[connwatch] connections=%s grace=%.0fs interval=%.0fs%s -> %s"
          % (args.connection, args.grace, args.interval,
             " REPORT-ONLY" if args.report_only else "", EVENT_LOG), flush=True)
    try:
        watch(args.connection, grace_seconds=args.grace, interval=args.interval,
              max_attempts=args.max_attempts, cooldown_seconds=args.cooldown,
              max_iterations=args.iterations, _reconnect=rec)
    except KeyboardInterrupt:
        print("[connwatch] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
