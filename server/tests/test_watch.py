"""The naked-position watchdog (nt8_mcp.watch).

The centre of gravity is the CORRECTED protective-stop test. cli-nt-bridge's version
(watch.py:27-31) accepted any working order on the instrument whose type contained "Stop",
regardless of quantity and regardless of side. Both halves of that are tested here:
a 1-lot stop under a 5-lot position is NOT protection, and a BUY stop above a LONG position is an
entry stop, not protection.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from nt8_mcp import watch as w  # noqa: E402


def order(instrument="ES 12-26", action="Sell", type_="StopMarket", qty=1, id_="o1"):
    return {"id": id_, "instrument": instrument, "action": action, "type": type_,
            "qty": qty, "price": 5800.0, "state": "Working"}


def block(name="Sim101", qty=2, orders=None):
    return {"name": name, "cash": 100000.0, "realized": 0.0, "unrealized": 0.0,
            "positions": ([{"instrument": "ES 12-26", "qty": qty, "avg": 5800.0,
                            "unrealized": 0.0, "hasSeenMarketData": True}] if qty else []),
            "orders": list(orders or [])}


# ── quantity ────────────────────────────────────────────────────────────────

def test_a_stop_smaller_than_the_position_is_not_protection():
    # THE upstream bug: a 1-lot stop under a 5-lot position read "protected".
    assert w.is_protected([order(qty=1)], "ES 12-26", 5) is False


def test_a_stop_equal_to_the_position_is_protection():
    assert w.is_protected([order(qty=5)], "ES 12-26", 5) is True


def test_a_stop_larger_than_the_position_is_protection():
    assert w.is_protected([order(qty=9)], "ES 12-26", 5) is True


def test_several_stops_add_up_to_cover_a_scaled_position():
    # A legitimate scaled bracket: 3 + 2 covers 5. Reading either stop alone would call it naked.
    orders = [order(qty=3, id_="o1"), order(qty=2, id_="o2")]
    assert w.protective_stop_qty(orders, "ES 12-26", 5) == 5
    assert w.is_protected(orders, "ES 12-26", 5) is True


def test_stops_on_another_instrument_do_not_count():
    assert w.is_protected([order(instrument="NQ 12-26", qty=10)], "ES 12-26", 2) is False


# ── side ────────────────────────────────────────────────────────────────────

def test_a_buy_stop_above_a_long_position_is_an_entry_not_protection():
    # The other half of the upstream bug: side was never checked at all.
    assert w.opposes("Buy", 5) is False
    assert w.is_protected([order(action="Buy", qty=5)], "ES 12-26", 5) is False


def test_a_sell_stop_protects_a_long():
    assert w.opposes("Sell", 5) is True
    assert w.is_protected([order(action="Sell", qty=5)], "ES 12-26", 5) is True


def test_a_buy_to_cover_stop_protects_a_short():
    assert w.opposes("BuyToCover", -3) is True
    assert w.is_protected([order(action="BuyToCover", qty=3)], "ES 12-26", -3) is True


def test_a_sell_stop_under_a_short_position_is_not_protection():
    assert w.opposes("Sell", -3) is False
    assert w.is_protected([order(action="Sell", qty=3)], "ES 12-26", -3) is False


def test_a_sell_short_stop_protects_a_long():
    assert w.opposes("SellShort", 2) is True


# ── order type ──────────────────────────────────────────────────────────────

def test_a_limit_is_a_profit_target_not_protection():
    assert w.is_stop(order(type_="Limit")) is False
    assert w.is_protected([order(type_="Limit", qty=9)], "ES 12-26", 2) is False


def test_both_stop_types_count():
    assert w.is_stop(order(type_="StopMarket")) is True
    assert w.is_stop(order(type_="StopLimit")) is True


# ── the scan ────────────────────────────────────────────────────────────────

def test_findings_report_side_quantity_and_the_stop_cover():
    rows = w.findings_for(block(qty=-4, orders=[order(action="BuyToCover", qty=1)]))
    assert rows == [{"account": "Sim101", "instrument": "ES 12-26", "side": "Short",
                     "qty": -4, "stopQty": 1, "protected": False}], rows


def test_a_flat_account_produces_no_rows():
    assert w.findings_for(block(qty=0)) == []


def test_an_account_that_cannot_be_read_is_reported_never_treated_as_flat():
    rows = w.scan_once(["Sim101"], _get=lambda name: {"error": "no account 'Sim101'"})
    assert rows == [{"account": "Sim101", "instrument": None, "error": "no account 'Sim101'"}], rows


def test_an_unreadable_order_row_makes_the_whole_account_unknown():
    # THE hole this closes: the degraded row is the protective stop. Skipping it made a fully
    # covered 5-lot position read naked, and the watchdog flattened it and cancelled its bracket.
    b = block(qty=5, orders=[{"error": "Instrument read failed"}])
    rows = w.findings_for(b)
    assert len(rows) == 1 and rows[0]["error"], rows
    assert rows[0]["instrument"] is None and "not flat" in rows[0]["error"], rows


def test_an_unreadable_position_row_is_unknown_not_nothing():
    b = block(qty=0)
    b["positions"] = [{"error": "Instrument read failed"}]
    rows = w.findings_for(b)
    assert len(rows) == 1 and rows[0]["error"], rows


def test_a_position_with_an_unreadable_quantity_is_unknown_not_flat():
    b = block(qty=2)
    b["positions"][0]["qty"] = "?"
    rows = w.findings_for(b)
    assert len(rows) == 1 and rows[0]["error"], rows


# ── the loop ────────────────────────────────────────────────────────────────

def _loop(findings_per_scan, grace=20.0, clock=None):
    """Drive watch() over scripted scans with a scripted clock. Returns (events, flatten calls)."""
    calls = []
    ticks = iter(clock if clock is not None else range(0, 1000, 10))
    scans = iter(findings_per_scan)
    events = w.watch(["Sim101"], grace_seconds=grace, interval=0,
                     max_iterations=len(findings_per_scan),
                     _scan=lambda accounts: next(scans),
                     _flatten=lambda a, i, s=None: (calls.append((a, i)), {"ok": True})[1],
                     _sleep=lambda s: None, _now=lambda: next(ticks),
                     _log=lambda e: None)
    return events, calls


NAKED = [{"account": "Sim101", "instrument": "ES 12-26", "side": "Long", "qty": 5,
          "stopQty": 1, "protected": False}]
SAFE = [{"account": "Sim101", "instrument": "ES 12-26", "side": "Long", "qty": 5,
         "stopQty": 5, "protected": True}]


def test_a_protected_position_is_never_flattened():
    events, calls = _loop([SAFE, SAFE, SAFE])
    assert events == [] and calls == []


def test_a_naked_position_inside_the_grace_period_is_left_alone():
    # A freshly entered position has a moment before its bracket lands. Killing it there kills
    # good trades mid-bracket-placement, which is why the grace period is not optional.
    events, calls = _loop([NAKED, NAKED], grace=20.0, clock=[0, 0, 10, 10])
    assert events == [] and calls == []


def test_a_naked_position_past_the_grace_period_is_flattened_once():
    events, calls = _loop([NAKED, NAKED, NAKED], grace=20.0, clock=[0, 0, 30, 30, 0, 0])
    assert calls == [("Sim101", "ES 12-26")], calls
    assert len(events) == 1, events
    assert events[0]["reason"].startswith("no protective stop"), events[0]
    assert events[0]["qty"] == 5 and events[0]["stopQty"] == 1, events[0]


def test_a_bracket_that_lands_during_the_grace_period_resets_the_timer():
    events, calls = _loop([NAKED, SAFE, NAKED], grace=20.0, clock=[0, 0, 10, 10, 30, 30])
    assert events == [] and calls == [], "protection arriving must clear the naked timer"


def test_a_scan_that_throws_is_survived_not_fatal():
    logged = []

    def boom(accounts):
        raise RuntimeError("AddOn unreachable")

    events = w.watch(["Sim101"], grace_seconds=0, interval=0, max_iterations=2, _scan=boom,
                     _flatten=lambda a, i, s=None: {"ok": True}, _sleep=lambda s: None,
                     _now=lambda: 0.0, _log=logged.append)
    assert events == []
    assert len(logged) == 2 and "scan failed" in logged[0]["error"], logged


def test_watch_refuses_an_empty_account_list():
    # There is no all-accounts watchdog, in the loop or in the AddOn.
    try:
        w.watch([])
    except ValueError:
        return
    raise AssertionError("watch must refuse an empty allow-list")


# ── it goes through the HTTP guards ─────────────────────────────────────────

PLAN_LONG_5 = {"positions": [{"instrument": "ES 12-26", "side": "Long", "qty": 5}], "orders": []}


def test_flatten_uses_the_two_step_dry_run_then_confirm_path():
    posts = []

    def post(body):
        posts.append(dict(body))
        if "confirm" not in body:
            return {"dryRun": True, "confirm": "FLATTEN Sim101 ES 12-26 Long 5 CANCEL 1 #abc",
                    "issuedAt": 1790000000.0, "plan": PLAN_LONG_5}
        return {"ok": True, "flattenCalled": True}

    result = w.flatten("Sim101", "ES 12-26", "Long", _post=post)
    assert result == {"ok": True, "flattenCalled": True}, result
    assert len(posts) == 2, posts
    assert "confirm" not in posts[0], posts[0]
    assert posts[1]["confirm"] == "FLATTEN Sim101 ES 12-26 Long 5 CANCEL 1 #abc", posts[1]
    assert posts[1]["issuedAt"] == 1790000000.0, posts[1]


def test_a_plan_that_no_longer_holds_the_position_is_not_confirmed():
    # The position closed between the scan and the dry-run and a fresh ENTRY order took its place.
    # Confirming here cancels an order this loop has no mandate over, and writes a watch.jsonl line
    # about a position that no longer existed.
    posts = []

    def post(body):
        posts.append(dict(body))
        return {"dryRun": True, "confirm": "FLATTEN Sim101 nothing CANCEL [o2 …] #abc",
                "issuedAt": 1790000000.0,
                "plan": {"positions": [], "orders": [{"id": "o2", "instrument": "ES 12-26"}]}}

    result = w.flatten("Sim101", "ES 12-26", "Long", _post=post)
    assert len(posts) == 1, posts
    assert result["ok"] is False and result["step"] == "planMoved", result


def test_a_plan_whose_side_flipped_is_not_confirmed():
    def post(body):
        return {"dryRun": True, "confirm": "x #abc", "issuedAt": 1790000000.0,
                "plan": {"positions": [{"instrument": "ES 12-26", "side": "Short", "qty": 5}],
                         "orders": []}}

    assert w.flatten("Sim101", "ES 12-26", "Long", _post=post)["step"] == "planMoved"


def test_a_flatten_that_raises_is_logged_and_the_loop_survives():
    # http.client.HTTPException is not an OSError, so it escaped _addon_request and killed the
    # watchdog process while the position it was about to flatten stayed open.
    logged = []

    def boom(account, instrument, side=None):
        raise RuntimeError("IncompleteRead(0 bytes read)")

    events = w.watch(["Sim101"], grace_seconds=0, interval=0, max_iterations=1,
                     _scan=lambda accounts: NAKED, _flatten=boom, _sleep=lambda s: None,
                     _now=lambda: 0.0, _log=logged.append)
    assert len(events) == 1, events
    assert events[0]["flattenResult"]["step"] == "flattenRaised", events[0]
    assert "IncompleteRead" in events[0]["flattenResult"]["error"], events[0]


def test_a_refused_dry_run_never_becomes_a_confirmed_post():
    # Unarmed, 409-live, no ops.live, AddOn unreachable: all arrive with no token, and the
    # watchdog must stop there rather than inventing one.
    posts = []

    def post(body):
        posts.append(dict(body))
        return {"error": "ops module not armed"}

    result = w.flatten("Sim101", "ES 12-26", "Long", _post=post)
    assert len(posts) == 1, posts
    assert result["ok"] is False and result["step"] == "dryRun", result
    assert result["response"] == {"error": "ops module not armed"}, result
