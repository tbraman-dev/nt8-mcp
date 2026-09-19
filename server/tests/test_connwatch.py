"""The connection guardian (nt8_mcp.connwatch).

The rule that matters: a reconnect acts on dropClass "inadvertent" and on nothing else. "failed",
"user", "connected" and null are all refused — "failed" means a connect NinjaTrader itself refused.
Retrying one hammers a connection that is already saying no, so it is explicitly NOT the same thing
as an inadvertent drop.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from nt8_mcp import connwatch as cw  # noqa: E402


def row(name="Sim101 feed", drop_class="inadvertent", connected=False, **kw):
    r = {"name": name, "source": "configured", "provider": "Simulator", "canManageOrders": True,
         "status": "Disconnected" if not connected else "Connected",
         "priceStatus": "Disconnected", "connected": connected, "dropClass": drop_class,
         "inadvertentlyDropped": drop_class == "inadvertent" and not connected,
         "live": False, "nonSim": False}
    r.update(kw)
    return r


# ── the drop classifier's four values ───────────────────────────────────────

def test_an_inadvertent_drop_is_the_one_class_acted_on():
    assert cw.actionable(row(drop_class="inadvertent")) is True


def test_a_failed_connect_is_refused():
    # dropClass "failed" = Connecting -> Disconnected with an error, i.e. NinjaTrader refused the
    # connect. NOTHING dropped, so there is nothing to heal, and a retry loop hammers it.
    assert cw.actionable(row(drop_class="failed")) is False


def test_a_failed_connect_is_filtered_out_of_the_scan():
    doc = {"connections": [row(name="MyBrokerAccount", drop_class="failed")]}
    assert cw.scan_once(["MyBrokerAccount"], _get=lambda: doc) == []


def test_a_failed_connect_is_never_reconnected_by_the_loop():
    calls = []
    events = cw.watch(["MyBrokerAccount"], grace_seconds=0, interval=0, max_iterations=3,
                      _scan=lambda names: cw.scan_once(
                          names, _get=lambda: {"connections": [row(name="MyBrokerAccount", drop_class="failed")]}),
                      _reconnect=lambda n: calls.append(n),
                      _sleep=lambda s: None, _now=lambda: 0.0, _log=lambda e: None)
    assert calls == [], "a 'failed' connect must never be retried"
    assert events == []


def test_a_user_parked_connection_is_refused():
    assert cw.actionable(row(drop_class="user")) is False


def test_a_connected_connection_is_refused():
    assert cw.actionable(row(drop_class="connected", connected=True)) is False


def test_an_unwitnessed_drop_is_refused():
    # null = the connection was already down when the AddOn loaded. "Not known" is not
    # "inadvertent"; this guardian heals in-session drops only.
    assert cw.actionable(row(drop_class=None, inadvertentlyDropped=False)) is False


def test_an_inadvertent_class_that_came_back_up_is_refused():
    assert cw.actionable(row(drop_class="inadvertent", connected=True)) is False


def test_inadvertently_dropped_must_agree_with_the_class():
    # Both fields come from the AddOn and must agree; a row where they do not is not acted on.
    assert cw.actionable(row(drop_class="inadvertent", inadvertentlyDropped=False)) is False


# ── the scan ────────────────────────────────────────────────────────────────

def test_the_scan_is_scoped_to_the_allow_list():
    doc = {"connections": [row(name="Sim101 feed"), row(name="Other feed")]}
    got = cw.scan_once(["Sim101 feed"], _get=lambda: doc)
    assert [r["name"] for r in got] == ["Sim101 feed"], got


def test_the_scan_matches_names_case_insensitively():
    doc = {"connections": [row(name="Sim101 Feed")]}
    assert len(cw.scan_once(["sim101 feed"], _get=lambda: doc)) == 1


def test_an_unreadable_connections_document_raises_rather_than_reading_as_all_clear():
    for bad in ({"error": "not reachable"}, [], None):
        try:
            cw.scan_once(["Sim101 feed"], _get=lambda b=bad: b)
        except RuntimeError:
            continue
        raise AssertionError("an unreadable /connections must raise, not report 'nothing flagged'")


# ── backoff ─────────────────────────────────────────────────────────────────

def test_backoff_is_base_times_two_to_the_attempt_capped():
    assert [cw.backoff_seconds(n) for n in (1, 2, 3, 4, 5, 6, 9)] == [15, 30, 60, 120, 240, 300, 300]


def test_jitter_spreads_the_delay_around_the_schedule():
    assert cw.backoff_seconds(2, jitter_frac=0.1, rng=lambda: 0.5) == 30.0     # 0.5 = no change
    assert cw.backoff_seconds(2, jitter_frac=0.1, rng=lambda: 1.0) == 33.0     # +10 %
    assert cw.backoff_seconds(2, jitter_frac=0.1, rng=lambda: 0.0) == 27.0     # -10 %


# ── the loop ────────────────────────────────────────────────────────────────

def _loop(scans, clock, **kw):
    results = kw.pop("results", None)
    calls = []
    it_scans, it_clock = iter(scans), iter(clock)
    it_results = iter(results) if results is not None else None

    def reconnect(name):
        calls.append(name)
        return next(it_results) if it_results is not None else {"statusAfter": "Connected"}

    events = cw.watch(["Sim101 feed"], interval=0, max_iterations=len(scans),
                      _scan=lambda names: next(it_scans), _reconnect=reconnect,
                      _sleep=lambda s: None, _now=lambda: next(it_clock),
                      _log=lambda e: None, _rng=lambda: 0.5, **kw)
    return events, calls


FLAGGED = [row()]


def test_a_drop_inside_the_grace_period_is_left_to_nt8s_own_reconnect():
    events, calls = _loop([FLAGGED, FLAGGED], clock=[0, 10], grace_seconds=20)
    assert calls == [] and events == []


def test_a_drop_past_the_grace_period_is_reconnected_once_when_it_goes_green():
    events, calls = _loop([FLAGGED, FLAGGED, []], clock=[0, 30, 40], grace_seconds=20)
    assert calls == ["Sim101 feed"], calls
    assert len(events) == 1 and events[0]["wentGreen"] is True, events


def test_a_connecting_answer_is_not_counted_as_a_failed_attempt():
    events, calls = _loop([FLAGGED, FLAGGED], clock=[0, 30], grace_seconds=0,
                          results=[{"statusAfter": "Connecting"}, {"statusAfter": "Connected"}])
    assert len(events) == 2, events
    assert events[0]["inProgress"] is True and events[0]["connectingRecheck"] == 1, events[0]
    assert events[1]["wentGreen"] is True, events[1]


def test_repeated_failures_back_off_then_cool_down_instead_of_giving_up():
    fail = {"statusAfter": "Disconnected"}
    # The clock must clear each backoff, so step it far past 300 s every scan.
    events, calls = _loop([FLAGGED] * 3, clock=[0, 1000, 2000], grace_seconds=0,
                          max_attempts=3, results=[fail, fail, fail])
    assert len(calls) == 3, calls
    assert [e["attempt"] for e in events] == [1, 2, 3], events
    assert events[0]["backoffS"] == 15 and events[1]["backoffS"] == 30, events
    assert events[2].get("coolingDown") is True, events[2]


def test_a_connection_that_stops_being_flagged_resets_its_state():
    fail = {"statusAfter": "Disconnected"}
    events, calls = _loop([FLAGGED, [], FLAGGED], clock=[0, 1, 2], grace_seconds=0,
                          results=[fail, fail])
    # Two attempts, but the second starts from attempt 1 again: the drop in between was healed.
    assert [e["attempt"] for e in events] == [1, 1], events


def test_a_scan_that_throws_is_survived_and_keeps_its_state():
    logged = []

    def boom(names):
        raise RuntimeError("AddOn unreachable")

    events = cw.watch(["Sim101 feed"], grace_seconds=0, interval=0, max_iterations=2, _scan=boom,
                      _reconnect=lambda n: {"statusAfter": "Connected"}, _sleep=lambda s: None,
                      _now=lambda: 0.0, _log=logged.append)
    assert events == []
    assert len(logged) == 2 and "scan failed" in logged[0]["error"], logged


def test_connwatch_refuses_an_empty_allow_list():
    try:
        cw.watch([])
    except ValueError:
        return
    raise AssertionError("connwatch must refuse an empty allow-list")


# ── it goes through the HTTP guards ─────────────────────────────────────────

def test_reconnect_uses_the_two_step_dry_run_then_confirm_path():
    posts = []

    def post(body):
        posts.append(dict(body))
        if "confirm" not in body:
            return {"dryRun": True, "confirm": "RECONNECT Sim101 feed Disconnected inadvertent",
                    "issuedAt": 1790000000.0}
        return {"ok": True, "statusAfter": "Connected"}

    result = cw.reconnect("Sim101 feed", _post=post)
    assert result == {"ok": True, "statusAfter": "Connected"}, result
    assert len(posts) == 2 and "confirm" not in posts[0], posts
    assert posts[1]["confirm"] == "RECONNECT Sim101 feed Disconnected inadvertent", posts[1]
    assert posts[1]["issuedAt"] == 1790000000.0, posts[1]


def test_a_refused_dry_run_never_becomes_a_confirmed_post():
    # The AddOn refuses "failed"/"user"/null itself, so a Python-side bug can never widen the policy.
    posts = []

    def post(body):
        posts.append(dict(body))
        return {"error": "connection 'X' has dropClass 'failed'; reconnect acts only on 'inadvertent'"}

    result = cw.reconnect("X", _post=post)
    assert len(posts) == 1, posts
    assert result["ok"] is False and result["step"] == "dryRun", result
