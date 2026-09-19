"""nt_order_submit / nt_order_change / nt_order_cancel (nt8_mcp.tools_orders) against a canned AddOn
that enforces the /orders/* gate chain.

The gates themselves live in C# (addon/NT8BridgeOrders.cs) and are verified against a running
NinjaTrader. What is pinned HERE is the contract those gates publish and the things a client can get
catastrophically wrong on the only tools in this repository that can OPEN a position:

  * calling an unarmed module and reading the 403 as "nothing to do",
  * inventing or reusing a confirm token instead of echoing the one the AddOn just issued,
  * REPLAYING a confirm that already worked — a retry after an HTTP timeout, or a model repeating
    its last tool call — which on the one verb that opens a position would double the order,
  * sending a confirm issued for a different verb (or for nt_flatten),
  * sending an argument as null instead of omitting it — `confirm` being ABSENT is what makes a
    call a dry run, so a null would turn every dry run into a confirmed one if the AddOn read it.

`Orders` below is a spec of the documented contract (docs/api/orders.md), not a second
implementation of the AddOn: it hands out an OPAQUE confirm string, so a client that computed its
own would fail. Client-side argument checking is deliberately NOT asserted where the AddOn is the
one that must refuse — a tool that pre-rejected what the AddOn allows would be a second, drifting
copy of the rules.

Note on shapes: nt8_mcp.app collapses every non-2xx answer to {"error": <the AddOn's sentence>},
so the refusal tests assert on that string.
"""

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import server as nt8, tools_orders  # noqa: E402

UNARMED = "orders module not armed"
CAPS = {"maxQuantity": 2, "maxQuantitySource": "default",
        "maxWorkingOrders": 5, "maxWorkingOrdersSource": "default",
        "maxSubmitsPerMinute": 6, "maxSubmitsPerMinuteSource": "default",
        "ceilings": {"maxQuantity": 10, "maxWorkingOrders": 20, "maxSubmitsPerMinute": 30},
        "warnings": []}


class Orders:
    """The documented /orders/* behaviour, as a scriptable fake.

    armed=False      -> 403 on every /orders/* path, whatever else is true, body parsed or not
    flag_age_h > 24  -> the SAME 403: a stale flag is not a flag
    any_live=True    -> 409 on all three POSTs; /orders/status still answers
    sim=False        -> 403. There is no second file that widens this, unlike the ops module.
    state            -> the order's real OrderState. Only Filled/Rejected/Cancelled are refused:
                        Suspended, Initialized and AcceptedByRisk are orders that are LIVE at the
                        broker and must stay cancellable, whatever the core's WorkingStates says.
    """

    # The readable half of a confirm string always starts with the VERB, so a token issued for one
    # call cannot authorise another, and an nt_flatten token ("FLATTEN …") can never match.
    PLAN = ("orders.submit|ACCOUNT=Sim101|INSTRUMENT=ES 12-26|ACTION=Buy|TYPE=Limit|QTY=1"
            "|LIMIT=5000.25|STOP=none|TIF=Day|CAPS qty=2/default working=5/default rate=6/default")
    CANCEL_PLAN = ("orders.cancel|ACCOUNT=Sim101|ORDERID=o1|INSTRUMENT=ES 12-26|ACTION=Buy"
                   "|TYPE=Limit|QTY=1|FILLED=0|STATE=Working"
                   "|CAPS qty=2/default working=5/default rate=6/default")
    CHANGE_PLAN = ("orders.change|ACCOUNT=Sim101|ORDERID=o1|INSTRUMENT=ES 12-26|ACTION=Buy"
                   "|TYPE=Limit|STATE=Working|FILLED=0|FROM qty=1 limit=5000.25 stop=none"
                   "|TO qty=2 limit=5001 stop=none|CAPS qty=2/default working=5/default rate=6/default")
    MAC = " #3f9a1c77b2e40d58"

    # Nothing more can happen to an order in one of these. Everything else — Working, PartFilled,
    # Suspended, Initialized, AcceptedByRisk, the *Pending states — is still live at the broker.
    TERMINAL = ("Filled", "Rejected", "Cancelled")

    def __init__(self, armed=True, flag_age_h=0.1, sim=True, any_live=False, window=30.0,
                 max_quantity=2, owned=("o1",), state="Working"):
        self.armed, self.flag_age_h, self.sim, self.any_live = armed, flag_age_h, sim, any_live
        self.window, self.max_quantity, self.owned, self.state = window, max_quantity, set(owned), state
        self.posts: list[dict] = []
        self.paths: list[str] = []
        self.used: set[tuple[str, float]] = set()   # a confirm authorises ONE call, not a window
        self.acted = 0

    def confirm_for(self, verb):
        return {"submit": self.PLAN, "change": self.CHANGE_PLAN, "cancel": self.CANCEL_PLAN}[verb] + self.MAC

    def __call__(self, req):
        verb = req.path.rsplit("/", 1)[-1]
        self.paths.append(req.path)

        # Gate 1 comes FIRST, before the body is parsed: an unarmed call never reaches the account
        # layer, so it is not audited and it leaks nothing but the one sentence.
        if not self.armed or self.flag_age_h > 24:
            return 403, {"error": UNARMED}
        if verb == "status":
            return 200, {"flags": {"armed": True, "flagName": "orders.enabled",
                                   "flagAgeHours": self.flag_age_h, "flagMaxAgeHours": 24},
                         "anyLive": self.any_live, "postsRefused": self.any_live,
                         "accounts": [{"name": "Sim101", "provider": "Simulator",
                                       "openPositions": 0, "workingOrders": 1, "error": None}],
                         "hiddenNonSimulator": 2, "backtestAccounts": 1, "caps": CAPS,
                         "submitsInLastMinute": 0, "confirmWindowSec": self.window}

        body = json.loads(req.body) if req.body else {}
        self.posts.append(body)

        if self.any_live:
            return 409, {"error": "orders %s refused: a live order-routing connection is up "
                                  "(see /health.connections)" % verb, "anyLive": True}
        if not (body.get("account") or "").strip():
            return 400, {"error": "account is required — one name from GET /orders/status; "
                                  "there is no all-accounts form"}
        if not self.sim:
            return 403, {"error": "account '%s' is not a Provider.Simulator or Provider.Playback "
                                  "account — this module accepts no other provider, has no live "
                                  "switch, and never reads ops.live" % body["account"]}

        if verb in ("change", "cancel"):
            order_id = (body.get("orderId") or "").strip()
            if not order_id:
                return 400, {"error": "orderId is required — the id POST /orders/submit returned"}
            if order_id not in self.owned:
                return 403, {"error": "order '%s' on '%s' was not submitted by this module in this "
                                      "process" % (order_id, body["account"])}
            if self.state in self.TERMINAL:
                return 409, {"error": "order '%s' is %s; only an order that is still live can be "
                                      "changed" % (order_id, self.state)}
        if verb == "submit":
            qty = body.get("quantity")
            if qty is not None and qty > self.max_quantity:
                return 403, {"error": "quantity %s is above the cap of %s (default); the hard "
                                      "ceiling in code is 10" % (qty, self.max_quantity)}

        if "confirm" not in body:
            return 200, {"dryRun": True, "plan": {"account": body["account"]},
                         "confirm": self.confirm_for(verb), "issuedAt": time.time(),
                         "expiresInSec": self.window, "caps": CAPS}

        if "issuedAt" not in body:
            return 400, {"error": "confirm was given without issuedAt — both come from the dry run"}
        age = time.time() - float(body["issuedAt"])
        if age > self.window:
            return 409, {"error": "issuedAt is %.1f s old; the confirm window is %.0f s — run the "
                                  "dry-run again and use the token it returns" % (age, self.window)}
        if body["confirm"] != self.confirm_for(verb):
            return 409, {"error": "confirm does not match the plan as it is NOW — the order, the "
                                  "account or the caps moved since the dry run, or the token was "
                                  "issued for a different call"}

        # The pair is SPENT once it verifies. A submit plan describes an order that does not exist
        # yet, so it reads the same before and after the act and nothing else would stop a replay.
        spent = (body["confirm"], float(body["issuedAt"]))
        if spent in self.used:
            return 409, {"error": "this confirm was already used — a confirm authorises ONE call, "
                                  "not every call inside the window"}
        self.used.add(spent)

        self.acted += 1
        return 200, {"ok": True, "dryRun": False, "account": body["account"], "orderId": "o1",
                     "state": "Filled" if verb == "submit" else "Cancelled",
                     "quantity": 1, "filled": 1 if verb == "submit" else 0,
                     "averageFillPrice": 5000.25, "rejected": False,
                     "ninjaTraderText": None, "caps": CAPS}


def _run(orders, fn, **kw):
    with FakeAddon() as fake:
        fake.orders(orders)
        return fn(**kw)


SUBMIT = dict(account="Sim101", instrument="ES 12-26", action="Buy", order_type="Limit",
              quantity=1, limit_price=5000.25)


# ── the arming flag ─────────────────────────────────────────────────────────

def test_unarmed_is_403_on_all_four_routes_and_nothing_is_acted_on():
    orders = Orders(armed=False)
    with FakeAddon() as fake:
        fake.orders(orders)
        results = [nt8.nt_order_submit(**SUBMIT),
                   nt8.nt_order_change(account="Sim101", order_id="o1", quantity=2),
                   nt8.nt_order_cancel(account="Sim101", order_id="o1")]
    for result in results:
        assert result == {"error": UNARMED}, result
    assert orders.acted == 0, "an unarmed module must not act"


def test_a_flag_older_than_24h_is_the_same_403_as_no_flag():
    # A flag forgotten after one debugging session must not arm order entry for ever. A stale flag
    # is indistinguishable from an absent one, deliberately.
    orders = Orders(flag_age_h=25.0)
    result = _run(orders, nt8.nt_order_submit, **SUBMIT)
    assert result == {"error": UNARMED}, result
    assert orders.acted == 0


def test_unarmed_refuses_even_a_confirmed_call():
    orders = Orders(armed=False)
    result = _run(orders, nt8.nt_order_cancel, account="Sim101", order_id="o1",
                  confirm=Orders().confirm_for("cancel"), issued_at=time.time())
    assert result == {"error": UNARMED}, result
    assert orders.acted == 0


# ── the account gate: Simulator only, and no second file ────────────────────

def test_a_non_simulator_account_is_refused_and_there_is_no_ops_live_equivalent():
    orders = Orders(sim=False)
    result = _run(orders, nt8.nt_order_submit, **dict(SUBMIT, account="<non-sim account>"))
    assert "no other provider" in result.get("error", ""), result
    assert "never reads ops.live" in result["error"], result
    assert orders.acted == 0


def test_a_live_connection_refuses_every_post():
    orders = Orders(any_live=True)
    with FakeAddon() as fake:
        fake.orders(orders)
        results = [nt8.nt_order_submit(**SUBMIT),
                   nt8.nt_order_change(account="Sim101", order_id="o1", quantity=2),
                   nt8.nt_order_cancel(account="Sim101", order_id="o1")]
    for result in results:
        assert "live order-routing connection" in result.get("error", ""), result
    assert orders.acted == 0


def test_account_is_mandatory_on_every_tool():
    for fn, kw in ((nt8.nt_order_submit, dict(SUBMIT, account="   ")),
                   (nt8.nt_order_change, dict(account="  ", order_id="o1", quantity=2)),
                   (nt8.nt_order_cancel, dict(account="", order_id="o1"))):
        orders = Orders()
        result = _run(orders, fn, **kw)
        assert "account is required" in result.get("error", ""), result
        assert orders.acted == 0


# ── dry run / confirm ───────────────────────────────────────────────────────

def test_a_call_without_confirm_is_a_dry_run_that_returns_a_plan_and_a_token():
    orders = Orders()
    result = _run(orders, nt8.nt_order_submit, **SUBMIT)
    assert result["dryRun"] is True, result
    assert result["confirm"] and result["issuedAt"], result
    assert result["caps"]["maxQuantity"] == 2, result
    assert "confirm" not in orders.posts[0], orders.posts[0]
    assert orders.acted == 0, "a dry run must send no order"


def test_the_confirm_string_is_echoed_verbatim_not_computed_locally():
    orders = Orders()
    with FakeAddon() as fake:
        fake.orders(orders)
        dry = nt8.nt_order_submit(**SUBMIT)
        done = nt8.nt_order_submit(**dict(SUBMIT, confirm=dry["confirm"], issued_at=dry["issuedAt"]))
    assert done["ok"] is True, done
    assert orders.acted == 1
    # Both halves of the token travel back unchanged. A client that rebuilt either one would be
    # computing a safety check it is supposed to be subject to.
    assert orders.posts[1]["confirm"] == dry["confirm"], orders.posts[1]
    assert orders.posts[1]["issuedAt"] == dry["issuedAt"], orders.posts[1]


def test_the_token_carries_a_signature_the_readable_plan_alone_does_not():
    orders = Orders()
    dry = _run(orders, nt8.nt_order_submit, **SUBMIT)
    assert dry["confirm"].startswith(Orders.PLAN), dry
    assert dry["confirm"] != Orders.PLAN, "the token must carry more than the readable plan"
    assert " #" in dry["confirm"], dry["confirm"]


def test_a_confirm_the_client_computed_from_readable_state_is_refused():
    # The bypass the signature closes: every term of the readable half (account, instrument, action,
    # type, quantity, prices, and the caps) is something the caller supplied or can read from the
    # ungated GET /account, so a plaintext token would be a pure function of what it already knows —
    # it could submit on its FIRST call, never running the dry run.
    orders = Orders()
    result = _run(orders, nt8.nt_order_submit, **dict(SUBMIT, confirm=Orders.PLAN, issued_at=time.time()))
    assert "does not match the plan" in result.get("error", ""), result
    assert orders.acted == 0


def test_a_confirm_issued_for_another_verb_is_refused():
    # The "orders.<verb>|" prefix is signed in, so a cancel token cannot authorise a submit. The
    # same prefix is what stops an nt_flatten confirm ("FLATTEN …") from ever matching here.
    orders = Orders()
    result = _run(orders, nt8.nt_order_submit,
                  **dict(SUBMIT, confirm=Orders().confirm_for("cancel"), issued_at=time.time()))
    assert "does not match the plan" in result.get("error", ""), result
    assert orders.acted == 0


def test_a_flatten_confirm_never_authorises_an_order():
    orders = Orders()
    flatten_token = "FLATTEN Sim101 FILTER none ES 12-26 Long 2 CANCEL nothing #3f9a1c77b2e40d58"
    result = _run(orders, nt8.nt_order_cancel, account="Sim101", order_id="o1",
                  confirm=flatten_token, issued_at=time.time())
    assert "does not match the plan" in result.get("error", ""), result
    assert orders.acted == 0


def test_the_caps_in_force_are_part_of_the_signed_plan():
    # A config change between the dry run and the confirm must invalidate the token: the caps are
    # terms of the plan string, not metadata beside it.
    assert "CAPS qty=2/default working=5/default rate=6/default" in Orders.PLAN, Orders.PLAN
    orders = Orders()
    moved = Orders.PLAN.replace("qty=2/default", "qty=3/config") + Orders.MAC
    result = _run(orders, nt8.nt_order_submit, **dict(SUBMIT, confirm=moved, issued_at=time.time()))
    assert "does not match the plan" in result.get("error", ""), result
    assert orders.acted == 0


def test_a_confirm_without_an_issued_at_is_refused():
    orders = Orders()
    result = _run(orders, nt8.nt_order_submit, **dict(SUBMIT, confirm=Orders().confirm_for("submit")))
    assert "without issuedAt" in result.get("error", ""), result
    assert orders.acted == 0


def test_an_issued_at_older_than_the_window_is_refused():
    # A token replayed out of a transcript or a stale model turn.
    orders = Orders()
    result = _run(orders, nt8.nt_order_submit,
                  **dict(SUBMIT, confirm=orders.confirm_for("submit"), issued_at=time.time() - 45))
    assert "issuedAt is" in result.get("error", "") and "old" in result["error"], result
    assert orders.acted == 0


def test_a_confirm_is_one_shot_and_a_replay_places_no_second_order():
    # THE replay: the caller keeps a working confirm and POSTs it again inside the 30 s window — a
    # retry after an HTTP timeout, a model repeating its last tool call, a transcript replayed by
    # anything that saw the body. Unlike a change or a cancel plan, a submit plan carries no term
    # that moves once the order is placed (no STATE, no FROM, no FILLED): it rebuilds byte-identical
    # from fresh state, so only the AddOn's consumed-token set stands between one approval and as
    # many orders as the rate cap allows.
    orders = Orders()
    with FakeAddon() as fake:
        fake.orders(orders)
        dry = nt8.nt_order_submit(**SUBMIT)
        first = nt8.nt_order_submit(**dict(SUBMIT, confirm=dry["confirm"], issued_at=dry["issuedAt"]))
        again = nt8.nt_order_submit(**dict(SUBMIT, confirm=dry["confirm"], issued_at=dry["issuedAt"]))
    assert first["ok"] is True, first
    assert "already used" in again.get("error", ""), again
    assert orders.acted == 1, "one approved dry run must place exactly one order"


def test_a_replayed_cancel_confirm_is_refused_too():
    # The same rule on all three verbs — it lives in the shared token check, not in the submit path.
    orders = Orders()
    with FakeAddon() as fake:
        fake.orders(orders)
        dry = nt8.nt_order_cancel(account="Sim101", order_id="o1")
        nt8.nt_order_cancel(account="Sim101", order_id="o1",
                            confirm=dry["confirm"], issued_at=dry["issuedAt"])
        again = nt8.nt_order_cancel(account="Sim101", order_id="o1",
                                    confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert "already used" in again.get("error", ""), again
    assert orders.acted == 1


def test_an_issued_at_inside_the_window_is_accepted():
    orders = Orders()
    result = _run(orders, nt8.nt_order_submit,
                  **dict(SUBMIT, confirm=orders.confirm_for("submit"), issued_at=time.time() - 5))
    assert result["ok"] is True, result
    assert orders.acted == 1


# ── the caps ────────────────────────────────────────────────────────────────

def test_a_quantity_over_the_cap_is_refused_by_the_addon_not_by_the_tool():
    # The tool passes the quantity through: the cap lives in ONE place, in the AddOn, where the
    # config file and the hard ceiling are. A client-side copy would drift the moment either moved.
    orders = Orders(max_quantity=2)
    result = _run(orders, nt8.nt_order_submit, **dict(SUBMIT, quantity=3))
    assert "above the cap of 2" in result.get("error", ""), result
    assert orders.posts[0]["quantity"] == 3, "the tool must not pre-filter the quantity"
    assert orders.acted == 0


def test_the_dry_run_reports_the_caps_and_their_source():
    orders = Orders()
    dry = _run(orders, nt8.nt_order_submit, **SUBMIT)
    caps = dry["caps"]
    assert caps["maxQuantitySource"] == "default", caps
    assert caps["ceilings"] == {"maxQuantity": 10, "maxWorkingOrders": 20,
                                "maxSubmitsPerMinute": 30}, caps


# ── change / cancel act only on orders this module submitted ────────────────

def test_change_and_cancel_refuse_an_order_this_module_did_not_submit():
    for fn, kw in ((nt8.nt_order_change, dict(account="Sim101", order_id="o999", quantity=2)),
                   (nt8.nt_order_cancel, dict(account="Sim101", order_id="o999"))):
        orders = Orders(owned=("o1",))
        result = _run(orders, fn, **kw)
        assert "was not submitted by this module" in result.get("error", ""), result
        assert orders.acted == 0


def test_change_and_cancel_require_an_order_id():
    for fn, kw in ((nt8.nt_order_change, dict(account="Sim101", order_id="  ", quantity=2)),
                   (nt8.nt_order_cancel, dict(account="Sim101", order_id=""))):
        orders = Orders()
        result = _run(orders, fn, **kw)
        assert "orderId is required" in result.get("error", ""), result


def test_an_order_that_is_no_longer_working_cannot_be_changed_or_cancelled():
    for state in ("Filled", "Cancelled", "Rejected"):
        orders = Orders(state=state)
        result = _run(orders, nt8.nt_order_change, account="Sim101", order_id="o1", quantity=2)
        assert "is %s" % state in result.get("error", ""), result
        assert orders.acted == 0


def test_a_suspended_or_initialized_order_is_still_live_and_must_stay_cancellable():
    # NinjaTrader defines OrderState.Suspended, Initialized and AcceptedByRisk, and the core's
    # WorkingStates whitelist (built for GET /account) names none of them. An order the AddOn
    # suspends between sessions is LIVE at the broker: judged by that whitelist it would be
    # invisible to the working-order cap AND refused for cancellation — leaving the one module that
    # can open a position with an order nothing here can take back.
    for state in ("Suspended", "Initialized", "AcceptedByRisk", "PartFilled"):
        orders = Orders(state=state)
        with FakeAddon() as fake:
            fake.orders(orders)
            dry = nt8.nt_order_cancel(account="Sim101", order_id="o1")
            done = nt8.nt_order_cancel(account="Sim101", order_id="o1",
                                       confirm=dry["confirm"], issued_at=dry["issuedAt"])
        assert "error" not in done, (state, done)
        assert orders.acted == 1, state


def test_the_change_and_cancel_plans_name_how_much_has_already_filled():
    # Order.Quantity is the order's ORIGINAL size and never shrinks as it fills. Without FILLED
    # beside it a PartFilled order reads as if its whole size were still working, and "cut qty 2 to
    # 1" on an order that has already done 1 leaves nothing working — the opposite of the plan.
    # It is a signed term too, so a fill between the dry run and the confirm refuses the token.
    assert "|FILLED=" in Orders.CHANGE_PLAN, Orders.CHANGE_PLAN
    assert "|FILLED=" in Orders.CANCEL_PLAN, Orders.CANCEL_PLAN
    orders = Orders()
    moved = Orders.CANCEL_PLAN.replace("FILLED=0", "FILLED=1") + Orders.MAC
    result = _run(orders, nt8.nt_order_cancel, account="Sim101", order_id="o1",
                  confirm=moved, issued_at=time.time())
    assert "does not match the plan" in result.get("error", ""), result
    assert orders.acted == 0


def test_cancel_round_trip_reports_the_state_the_addon_measured():
    orders = Orders()
    with FakeAddon() as fake:
        fake.orders(orders)
        dry = nt8.nt_order_cancel(account="Sim101", order_id="o1")
        done = nt8.nt_order_cancel(account="Sim101", order_id="o1",
                                   confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert done["state"] == "Cancelled", done
    assert orders.acted == 1


# ── the wire body ───────────────────────────────────────────────────────────

def test_optional_arguments_are_omitted_from_the_body_not_sent_as_null():
    # `confirm` ABSENT is what makes a call a dry run. If the tool sent confirm:null the AddOn would
    # see the key and a future reader of either side could not tell the two calls apart.
    orders = Orders()
    _run(orders, nt8.nt_order_submit, account="Sim101", instrument="ES 12-26", action="Buy",
         order_type="Market", quantity=1)
    assert orders.posts[0] == {"account": "Sim101", "instrument": "ES 12-26", "action": "Buy",
                               "type": "Market", "quantity": 1}, orders.posts[0]


def test_the_python_argument_names_map_to_the_addon_json_keys():
    orders = Orders()
    _run(orders, nt8.nt_order_submit, **dict(SUBMIT, order_type="StopLimit", stop_price=4999.75,
                                             tif="Gtc"))
    body = orders.posts[0]
    assert body["type"] == "StopLimit", body           # order_type -> type
    assert body["limitPrice"] == 5000.25, body         # limit_price -> limitPrice
    assert body["stopPrice"] == 4999.75, body          # stop_price -> stopPrice
    assert body["tif"] == "Gtc", body

    orders = Orders()
    _run(orders, nt8.nt_order_change, account="Sim101", order_id="o1", limit_price=5001.0)
    assert orders.posts[0] == {"account": "Sim101", "orderId": "o1", "limitPrice": 5001.0}, orders.posts[0]


def test_each_tool_posts_to_its_own_path():
    orders = Orders()
    with FakeAddon() as fake:
        fake.orders(orders)
        nt8.nt_order_submit(**SUBMIT)
        nt8.nt_order_change(account="Sim101", order_id="o1", quantity=2)
        nt8.nt_order_cancel(account="Sim101", order_id="o1")
    assert orders.paths == ["/orders/submit", "/orders/change", "/orders/cancel"], orders.paths


# ── the tools' own surface ──────────────────────────────────────────────────

def test_there_are_exactly_three_order_tools():
    exported = sorted(k for k in vars(tools_orders) if k.startswith("nt_"))
    assert exported == ["nt_order_cancel", "nt_order_change", "nt_order_submit"], exported


def test_the_tools_are_registered_under_their_documented_names():
    names = sorted(t.name for t in nt8.mcp._tool_manager.list_tools()
                   if t.name.startswith("nt_order"))
    assert names == ["nt_order_cancel", "nt_order_change", "nt_order_submit"], names


def test_account_and_the_order_identity_are_mandatory_arguments():
    for fn in (nt8.nt_order_submit, nt8.nt_order_change, nt8.nt_order_cancel):
        try:
            fn()  # type: ignore[call-arg]
        except TypeError:
            continue
        raise AssertionError("%s must require its identifying arguments" % fn.__name__)


def test_every_docstring_states_what_a_caller_must_know():
    for fn in (tools_orders.nt_order_submit, tools_orders.nt_order_change,
               tools_orders.nt_order_cancel):
        doc = fn.__doc__ or ""
        for phrase in ("simulator", "playback", "two steps", "30 seconds", "orders.enabled", "403"):
            assert phrase in doc.lower(), "%s's docstring must state %r" % (fn.__name__, phrase)
        # These two carry their emphasis: a model skims, and the capitals are the signal.
        assert "DATA" in doc, "%s must say NinjaTrader text is DATA" % fn.__name__
        assert "ONLY" in doc, "%s must say Simulator/Playback ONLY" % fn.__name__


def test_the_submit_docstring_states_the_caps_and_that_ok_is_not_filled():
    doc = tools_orders.nt_order_submit.__doc__ or ""
    for phrase in ("orders.config.json", "10 / 20 / 30", "NEVER means filled"):
        assert phrase in doc, "nt_order_submit's docstring must state %r" % phrase


def test_no_docstring_promises_a_live_account_or_a_widening_file():
    # The one sentence that must never appear: this module has no ops.live equivalent, and a
    # docstring hinting at one would invite a model to go looking for the file.
    for fn in (tools_orders.nt_order_submit, tools_orders.nt_order_change,
               tools_orders.nt_order_cancel):
        doc = (fn.__doc__ or "").lower()
        assert "never a live" in doc or "no other provider" in doc or "no switch" in doc, fn.__name__
        assert "ops.live" not in doc.replace("no ops.live equivalent", ""), fn.__name__


def test_the_docstring_does_not_promise_a_plan_the_passthrough_throws_away():
    # app.py collapses every non-2xx to {"error": <sentence>}: the status code and the 409's new
    # plan do not reach the model. A docstring that promised the plan invited a blind retry on the
    # one tool in this repository that can open a position.
    doc = tools_orders.nt_order_submit.__doc__ or ""
    assert '{"error"' in doc, "the docstring must state the shape a refusal really arrives in"
    assert "do not survive the passthrough" in doc.lower(), doc
