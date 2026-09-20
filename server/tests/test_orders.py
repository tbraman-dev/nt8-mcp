"""The six order tools (nt8_mcp.tools_orders) against a canned AddOn that enforces the /orders/*
gate chain.

The gates themselves live in C# (addon/NT8BridgeOrders.cs) and are verified against a running
NinjaTrader. What is pinned HERE is the contract those gates publish and the things a client can get
catastrophically wrong on the only tools in this repository that can OPEN a position:

  * calling an unarmed module and reading the 403 as "nothing to do",
  * inventing or reusing a confirm token instead of echoing the one the AddOn just issued,
  * REPLAYING a confirm that already worked — a retry after an HTTP timeout, or a model repeating
    its last tool call — which on a verb that opens a position would double the order,
  * sending a confirm issued for a different verb (or for nt_flatten),
  * sending an argument as null instead of omitting it — `confirm` being ABSENT is what makes a
    call a dry run, so a null would turn every dry run into a confirmed one if the AddOn read it,
  * changing or cancelling a strategy's or an ATM's order without reading whose it is,
  * treating a bracket whose entry is still resting as a protected position.

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
CAPS_TEXT = "CAPS qty=10/default working=20/default rate=60/default"
CAPS = {"maxQuantity": 10, "maxQuantitySource": "default",
        "maxWorkingOrders": 20, "maxWorkingOrdersSource": "default",
        "maxSubmitsPerMinute": 60, "maxSubmitsPerMinuteSource": "default",
        "ceilings": {"maxQuantity": 100, "maxWorkingOrders": 100, "maxSubmitsPerMinute": 600},
        "defaults": {"maxQuantity": 10, "maxWorkingOrders": 20, "maxSubmitsPerMinute": 60},
        "warnings": []}

# Every path the module owns. /orders/status has no tool; the other six do.
POST_VERBS = ("submit", "bracket", "change", "cancel", "close", "reverse")


class Orders:
    """The documented /orders/* behaviour, as a scriptable fake.

    armed=False      -> 403 on every /orders/* path, whatever else is true, body parsed or not
    flag_age_h > 24  -> the SAME 403: a stale flag is not a flag
    any_live=True    -> 409 on every POST; /orders/status still answers
    sim=False        -> 403. There is no second file that widens this, unlike the ops module.
    live_orders      -> {orderId: owner}. ANY of them can be changed or cancelled, not only the
                        ones the module placed: the plan names the owner instead of hiding the rest.
    state            -> the order's real OrderState. Only Filled/Rejected/Cancelled are refused:
                        Suspended, Initialized and AcceptedByRisk are orders that are LIVE at the
                        broker and must stay cancellable, whatever the core's WorkingStates says.
    position         -> ("Long", 2) or None, for /orders/close and /orders/reverse
    resting          -> a bracket entry that has NOT filled inside the call: its exits are not at
                        the broker yet, so the answer says exitsPending and lists no exit.
    """

    # The readable half of a confirm string always starts with the VERB, so a token issued for one
    # call cannot authorise another, and an nt_flatten token ("FLATTEN …") can never match.
    PLAN = ("orders.submit|ACCOUNT=Sim101|INSTRUMENT=ES 12-26|ACTION=Buy|TYPE=Limit|QTY=1"
            "|LIMIT=5000.25|STOP=none|TIF=Day|" + CAPS_TEXT)
    BRACKET_PLAN = ("orders.bracket|ACCOUNT=Sim101|INSTRUMENT=ES 12-26|ACTION=Buy|TYPE=Market|QTY=2"
                    "|LIMIT=none|STOP=none|TIF=Day|EXIT=Sell|SL=ticks 20"
                    "|TARGETS=T1=ticks 40 qty 1 T2=ticks 80 qty 1|" + CAPS_TEXT)
    CANCEL_PLAN = ("orders.cancel|ACCOUNT=Sim101|ORDERID=o1|OWNER=module|INSTRUMENT=ES 12-26"
                   "|ACTION=Buy|TYPE=Limit|OCO=none|QTY=1|FILLED=0|STATE=Working|" + CAPS_TEXT)
    CHANGE_PLAN = ("orders.change|ACCOUNT=Sim101|ORDERID=o1|OWNER=module|INSTRUMENT=ES 12-26"
                   "|ACTION=Buy|TYPE=Limit|OCO=none|STATE=Working|FILLED=0"
                   "|FROM qty=1 limit=5000.25 stop=none|TO qty=2 limit=5001 stop=none|" + CAPS_TEXT)
    CLOSE_PLAN = ("orders.close|ACCOUNT=Sim101|INSTRUMENT=ES 12-26|SIDE=Long|QTY=2|CANCEL=o1 o2|"
                  + CAPS_TEXT)
    REVERSE_PLAN = ("orders.reverse|ACCOUNT=Sim101|INSTRUMENT=ES 12-26|SIDE=Long|QTY=2|CANCEL=o1 o2"
                    "|TO=Short 2|" + CAPS_TEXT)
    MAC = " #3f9a1c77b2e40d58"

    # Nothing more can happen to an order in one of these. Everything else — Working, PartFilled,
    # Suspended, Initialized, AcceptedByRisk, the *Pending states — is still live at the broker.
    TERMINAL = ("Filled", "Rejected", "Cancelled")

    def __init__(self, armed=True, flag_age_h=0.1, sim=True, any_live=False, window=30.0,
                 max_quantity=10, live_orders=None, state="Working", position=("Long", 2),
                 resting=False):
        self.armed, self.flag_age_h, self.sim, self.any_live = armed, flag_age_h, sim, any_live
        self.window, self.max_quantity, self.state = window, max_quantity, state
        self.live_orders = dict(live_orders if live_orders is not None else {"o1": "module"})
        self.position, self.resting = position, resting
        self.posts: list[dict] = []
        self.paths: list[str] = []
        self.used: set[tuple[str, float]] = set()   # a confirm authorises ONE call, not a window
        self.acted = 0          # state changes that reached NinjaTrader
        self.submits = 0        # what the rate cap counts: a whole bracket is ONE

    def confirm_for(self, verb):
        return {"submit": self.PLAN, "bracket": self.BRACKET_PLAN, "change": self.CHANGE_PLAN,
                "cancel": self.CANCEL_PLAN, "close": self.CLOSE_PLAN,
                "reverse": self.REVERSE_PLAN}[verb] + self.MAC

    # -- the gate chain ------------------------------------------------------

    def __call__(self, req):
        verb = req.path.rsplit("/", 1)[-1]
        self.paths.append(req.path)

        # Gate 1 comes FIRST, before the body is parsed: an unarmed call never reaches the account
        # layer, so it is not audited and it leaks nothing but the one sentence.
        if not self.armed or self.flag_age_h > 24:
            return 403, {"error": UNARMED}
        if verb == "status":
            return 200, self._status()

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

        refusal = getattr(self, "_check_" + verb)(body)
        if refusal is not None:
            return refusal

        if "confirm" not in body:
            return 200, {"dryRun": True, "plan": self._plan(verb, body),
                         "confirm": self.confirm_for(verb), "issuedAt": time.time(),
                         "expiresInSec": self.window, "caps": CAPS}

        token = self._check_token(body, verb)
        if token is not None:
            return token

        self.acted += 1
        return 200, getattr(self, "_do_" + verb)(body)

    # -- per-verb validation (gate 4 / gate 5) -------------------------------

    def _check_submit(self, body):
        qty = body.get("quantity")
        if qty is not None and qty > self.max_quantity:
            return 403, {"error": "quantity %s is above the cap of %s (default); the hard "
                                  "ceiling in code is 100" % (qty, self.max_quantity)}
        return None

    def _check_bracket(self, body):
        if body.get("action") not in ("Buy", "SellShort"):
            return 400, {"error": "a bracket entry opens a position: action must be Buy or "
                                  "SellShort. Sell and BuyToCover close one"}
        have_price = body.get("stopLossPrice") is not None
        have_ticks = body.get("stopLossTicks") is not None
        if not have_price and not have_ticks:
            return 400, {"error": "a bracket needs a stop loss: give stopLossPrice or "
                                  "stopLossTicks (ticks are measured from the entry's real "
                                  "average fill price)"}
        if have_price and have_ticks:
            return 400, {"error": "give stopLossPrice or stopLossTicks, not both"}
        targets = body.get("targets") or []
        total = sum(t.get("quantity", 0) for t in targets)
        if targets and total != body.get("quantity"):
            return 400, {"error": "the target quantities add up to %s but the entry quantity is %s "
                                  "— a scale-out must cover the whole entry, and one stop covers "
                                  "whatever is still open" % (total, body.get("quantity"))}
        return self._check_submit(body)

    def _check_order_id(self, body):
        order_id = (body.get("orderId") or "").strip()
        if not order_id:
            return 400, {"error": "orderId is required — one of the ids GET /orders/status lists, "
                                  "or the id POST /orders/submit returned"}
        if order_id not in self.live_orders:
            return 404, {"error": "no order '%s' on '%s' — call GET /orders/status for the live "
                                  "orders of every valid account" % (order_id, body["account"])}
        if self.state in self.TERMINAL:
            return 409, {"error": "order '%s' is %s; only an order that is still live can be "
                                  "changed" % (order_id, self.state)}
        return None

    _check_change = _check_order_id
    _check_cancel = _check_order_id

    def _check_close(self, body):
        if not (body.get("instrument") or "").strip():
            return 400, {"error": 'instrument is required, e.g. "ES 12-26"'}
        if self.position is None and not self.live_orders:
            return 409, {"error": "'%s' has no position and no working order in '%s' — there is "
                                  "nothing to close" % (body["account"], body["instrument"])}
        return None

    def _check_reverse(self, body):
        if not (body.get("instrument") or "").strip():
            return 400, {"error": 'instrument is required, e.g. "ES 12-26"'}
        if self.position is None:
            return 409, {"error": "'%s' has no position in '%s' — there is nothing to reverse; use "
                                  "/orders/submit to open one" % (body["account"], body["instrument"])}
        # The quantity cap applies to the new side. A reverse sizes its entry from the POSITION, not
        # from the body, so it is the third way to reach a forbidden size — and the refusal lands at
        # the dry run, before any token exists for a plan the caps forbid.
        side, qty = self.position
        if qty > self.max_quantity:
            return 403, {"error": "'%s' is %s %s in '%s' and a reverse would open that same size on "
                                  "the other side, which is above the cap of %s (default); the hard "
                                  "ceiling in code is 100. Use POST /orders/close, which reduces "
                                  "only, or raise maxQuantity in orders.config.json"
                                  % (body["account"], side, qty, body["instrument"],
                                     self.max_quantity)}
        return None

    # -- gate 7: the token ---------------------------------------------------

    def _check_token(self, body, verb):
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
        # The pair is SPENT once it verifies. A submit and a bracket plan describe orders that do
        # not exist yet, so they read the same before and after the act and nothing else would stop
        # a replay.
        spent = (body["confirm"], float(body["issuedAt"]))
        if spent in self.used:
            return 409, {"error": "this confirm was already used — a confirm authorises ONE call, "
                                  "not every call inside the window"}
        self.used.add(spent)
        return None

    # -- the plans -----------------------------------------------------------

    def _plan(self, verb, body):
        if verb == "bracket":
            return {"account": body["account"], "instrument": body.get("instrument"),
                    "action": body.get("action"), "type": body.get("type"),
                    "quantity": body.get("quantity"), "exitAction": "Sell",
                    "stopLossPrice": body.get("stopLossPrice"),
                    "stopLossTicks": body.get("stopLossTicks"), "tickSize": 0.25,
                    "targets": body.get("targets") or []}
        if verb in ("change", "cancel"):
            return {"account": body["account"], "orderId": body["orderId"],
                    "owner": self.live_orders[body["orderId"]], "oco": None,
                    "state": self.state, "filled": 0, "quantity": 1}
        if verb in ("close", "reverse"):
            side, qty = self.position if self.position else (None, 0)
            plan = {"account": body["account"], "instrument": body["instrument"],
                    "position": {"side": side, "quantity": qty, "averagePrice": 5000.0},
                    "cancelOrders": sorted(self.live_orders)}
            plan["enterAfterFlat"] = ({"side": "Short" if side == "Long" else "Long",
                                       "quantity": qty, "type": "Market"} if verb == "reverse"
                                      else None)
            return plan
        return {"account": body["account"]}

    # -- the acts ------------------------------------------------------------

    def _do_submit(self, body):
        self.submits += 1
        return {"ok": True, "dryRun": False, "account": body["account"], "orderId": "o1",
                "state": "Filled", "quantity": 1, "filled": 1, "averageFillPrice": 5000.25,
                "rejected": False, "ninjaTraderText": None, "caps": CAPS}

    def _do_bracket(self, body):
        # ONE rate slot for the whole bracket, however many legs it sends.
        self.submits += 1
        qty = body["quantity"]
        if self.resting:
            # The entry has NOT filled: no stop and no target are at the broker, and the answer says
            # so rather than implying a protected position.
            return {"ok": True, "dryRun": False, "bracketId": "br1", "account": body["account"],
                    "instrument": body["instrument"],
                    "entry": {"kind": "entry", "orderId": "o1", "state": "Working",
                              "quantity": qty, "filled": 0},
                    "exits": [], "exitsPending": True, "oco": "NT8Bridge-abc", "caps": CAPS}
        filled, avg = qty, 5000.0
        exits = [{"kind": "stop", "orderId": "o2", "state": "Working", "quantity": filled,
                  "stopPrice": avg - 20 * 0.25, "error": None}]
        left = filled
        for n, t in enumerate(body.get("targets") or [], start=2):
            q = min(left, t["quantity"])
            left -= q
            exits.append({"kind": "target", "orderId": "o%d" % (n + 1), "state": "Working",
                          "quantity": q, "limitPrice": avg + t.get("ticks", 0) * 0.25,
                          "error": None})
        return {"ok": True, "dryRun": False, "bracketId": "br1", "account": body["account"],
                "instrument": body["instrument"],
                "entry": {"kind": "entry", "orderId": "o1", "state": "Filled", "quantity": qty,
                          "filled": filled, "averageFillPrice": avg},
                "exits": exits, "exitsPending": False, "oco": "NT8Bridge-abc", "caps": CAPS}

    def _do_change(self, body):
        return {"ok": True, "dryRun": False, "account": body["account"],
                "orderId": body["orderId"], "state": "Working", "quantity": body.get("quantity", 1),
                "filled": 0, "rejected": False, "ninjaTraderText": None, "caps": CAPS}

    def _do_cancel(self, body):
        return {"ok": True, "dryRun": False, "account": body["account"],
                "orderId": body["orderId"], "state": "Cancelled", "quantity": 1, "filled": 0,
                "rejected": False, "ninjaTraderText": None, "caps": CAPS}

    def _do_close(self, body):
        cancelled = len(self.live_orders)
        self.live_orders = {}
        self.position = None
        return {"ok": True, "dryRun": False, "account": body["account"],
                "instrument": body["instrument"], "cancelledOrders": cancelled,
                "cancelError": None, "flattenError": None,
                "positionBefore": {"side": "Long", "quantity": 2, "averagePrice": 5000.0},
                "positionAfter": {"side": None, "quantity": 0, "averagePrice": None},
                "positionAfterError": None, "entry": None, "caps": CAPS}

    def _do_reverse(self, body):
        self.submits += 1
        cancelled = len(self.live_orders)
        side, qty = self.position
        other = "Short" if side == "Long" else "Long"
        self.live_orders = {}
        self.position = (other, qty)
        return {"ok": True, "dryRun": False, "account": body["account"],
                "instrument": body["instrument"], "cancelledOrders": cancelled,
                "cancelError": None, "flattenError": None,
                "positionBefore": {"side": side, "quantity": qty, "averagePrice": 5000.0},
                "positionAfter": {"side": other, "quantity": qty, "averagePrice": 5001.0},
                "positionAfterError": None,
                "entry": {"kind": "entry", "orderId": "o9", "state": "Filled", "quantity": qty,
                          "filled": qty, "error": None},
                "caps": CAPS}

    def _status(self):
        return {"flags": {"armed": True, "flagName": "orders.enabled",
                          "flagAgeHours": self.flag_age_h, "flagMaxAgeHours": 24},
                "anyLive": self.any_live, "postsRefused": self.any_live,
                "accounts": [{"name": "Sim101", "provider": "Simulator",
                              "openPositions": 1 if self.position else 0,
                              "workingOrders": len(self.live_orders),
                              "liveOrders": len(self.live_orders),
                              "orders": [{"orderId": oid, "owner": owner, "state": self.state}
                                         for oid, owner in sorted(self.live_orders.items())],
                              "error": None}],
                "hiddenNonSimulator": 2, "backtestAccounts": 1, "caps": CAPS,
                "submitsInLastMinute": self.submits, "confirmWindowSec": self.window}


def _wire(fake, orders):
    """All SEVEN routes on one handler. fake_addon.orders() covers the four original ones; the three
    added in 1.4 are registered here, in this file, because tests do not own fake_addon.py."""
    fake.orders(orders)
    for verb in ("bracket", "close", "reverse"):
        fake.register("/orders/" + verb, "POST", orders)
    return fake


def _run(orders, fn, **kw):
    with FakeAddon() as fake:
        _wire(fake, orders)
        return fn(**kw)


SUBMIT = dict(account="Sim101", instrument="ES 12-26", action="Buy", order_type="Limit",
              quantity=1, limit_price=5000.25)
BRACKET = dict(account="Sim101", instrument="ES 12-26", action="Buy", order_type="Market",
               quantity=2, stop_loss_ticks=20,
               targets=[{"ticks": 40, "quantity": 1}, {"ticks": 80, "quantity": 1}])
CLOSE = dict(account="Sim101", instrument="ES 12-26")


def _every_tool(orders):
    """One call to each of the six tools, in a shape each of them accepts."""
    return [nt8.nt_order_submit(**SUBMIT),
            nt8.nt_order_bracket(**BRACKET),
            nt8.nt_order_change(account="Sim101", order_id="o1", quantity=2),
            nt8.nt_order_cancel(account="Sim101", order_id="o1"),
            nt8.nt_position_close(**CLOSE),
            nt8.nt_position_reverse(**CLOSE)]


# ── the arming flag ─────────────────────────────────────────────────────────

def test_unarmed_is_403_on_every_route_and_nothing_is_acted_on():
    orders = Orders(armed=False)
    with FakeAddon() as fake:
        _wire(fake, orders)
        results = _every_tool(orders)
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


def test_a_non_simulator_account_is_refused_on_every_new_verb_too():
    # The provider gate lives in the ONE door, not in each verb: a verb added later cannot forget it.
    for fn, kw in ((nt8.nt_order_bracket, dict(BRACKET, account="<non-sim account>")),
                   (nt8.nt_position_close, dict(CLOSE, account="<non-sim account>")),
                   (nt8.nt_position_reverse, dict(CLOSE, account="<non-sim account>"))):
        orders = Orders(sim=False)
        result = _run(orders, fn, **kw)
        assert "no other provider" in result.get("error", ""), result
        assert orders.acted == 0


def test_a_live_connection_refuses_every_post():
    orders = Orders(any_live=True)
    with FakeAddon() as fake:
        _wire(fake, orders)
        results = _every_tool(orders)
    for result in results:
        assert "live order-routing connection" in result.get("error", ""), result
    assert orders.acted == 0


def test_account_is_mandatory_on_every_tool():
    for fn, kw in ((nt8.nt_order_submit, dict(SUBMIT, account="   ")),
                   (nt8.nt_order_bracket, dict(BRACKET, account="   ")),
                   (nt8.nt_order_change, dict(account="  ", order_id="o1", quantity=2)),
                   (nt8.nt_order_cancel, dict(account="", order_id="o1")),
                   (nt8.nt_position_close, dict(CLOSE, account="  ")),
                   (nt8.nt_position_reverse, dict(CLOSE, account=""))):
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
    assert result["caps"]["maxQuantity"] == 10, result
    assert "confirm" not in orders.posts[0], orders.posts[0]
    assert orders.acted == 0, "a dry run must send no order"


def test_every_verb_is_a_dry_run_without_confirm():
    orders = Orders()
    with FakeAddon() as fake:
        _wire(fake, orders)
        results = _every_tool(orders)
    for result in results:
        assert result["dryRun"] is True, result
        assert result["confirm"] and result["issuedAt"], result
    assert orders.acted == 0, "six dry runs must change nothing"


def test_the_confirm_string_is_echoed_verbatim_not_computed_locally():
    orders = Orders()
    with FakeAddon() as fake:
        _wire(fake, orders)
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
    # The "orders.<verb>|" prefix is signed in, so a cancel token cannot authorise a submit and a
    # close token cannot authorise a reverse. The same prefix is what stops an nt_flatten confirm
    # ("FLATTEN …") from ever matching here.
    for fn, kw, wrong in ((nt8.nt_order_submit, SUBMIT, "cancel"),
                          (nt8.nt_order_bracket, BRACKET, "submit"),
                          (nt8.nt_position_reverse, CLOSE, "close"),
                          (nt8.nt_position_close, CLOSE, "reverse")):
        orders = Orders()
        result = _run(orders, fn, **dict(kw, confirm=Orders().confirm_for(wrong),
                                         issued_at=time.time()))
        assert "does not match the plan" in result.get("error", ""), (wrong, result)
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
    assert CAPS_TEXT in Orders.PLAN, Orders.PLAN
    assert CAPS_TEXT in Orders.BRACKET_PLAN, Orders.BRACKET_PLAN
    orders = Orders()
    moved = Orders.PLAN.replace("qty=10/default", "qty=25/config") + Orders.MAC
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
        _wire(fake, orders)
        dry = nt8.nt_order_submit(**SUBMIT)
        first = nt8.nt_order_submit(**dict(SUBMIT, confirm=dry["confirm"], issued_at=dry["issuedAt"]))
        again = nt8.nt_order_submit(**dict(SUBMIT, confirm=dry["confirm"], issued_at=dry["issuedAt"]))
    assert first["ok"] is True, first
    assert "already used" in again.get("error", ""), again
    assert orders.acted == 1, "one approved dry run must place exactly one order"


def test_a_replayed_bracket_confirm_places_no_second_bracket():
    # The bracket plan is the OTHER one that rebuilds identically after the act — it describes
    # orders that do not exist yet — and a replay of it would double an entry AND its exits.
    orders = Orders()
    with FakeAddon() as fake:
        _wire(fake, orders)
        dry = nt8.nt_order_bracket(**BRACKET)
        first = nt8.nt_order_bracket(**dict(BRACKET, confirm=dry["confirm"], issued_at=dry["issuedAt"]))
        again = nt8.nt_order_bracket(**dict(BRACKET, confirm=dry["confirm"], issued_at=dry["issuedAt"]))
    assert first["ok"] is True, first
    assert "already used" in again.get("error", ""), again
    assert orders.acted == 1


def test_a_replayed_cancel_confirm_is_refused_too():
    # The same rule on every verb — it lives in the shared token check, not in one path.
    orders = Orders()
    with FakeAddon() as fake:
        _wire(fake, orders)
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
    orders = Orders(max_quantity=10)
    result = _run(orders, nt8.nt_order_submit, **dict(SUBMIT, quantity=11))
    assert "above the cap of 10" in result.get("error", ""), result
    assert orders.posts[0]["quantity"] == 11, "the tool must not pre-filter the quantity"
    assert orders.acted == 0


def test_the_dry_run_reports_the_caps_and_their_source():
    orders = Orders()
    dry = _run(orders, nt8.nt_order_submit, **SUBMIT)
    caps = dry["caps"]
    assert caps["maxQuantitySource"] == "default", caps
    assert caps["defaults"] == {"maxQuantity": 10, "maxWorkingOrders": 20,
                                "maxSubmitsPerMinute": 60}, caps
    assert caps["ceilings"] == {"maxQuantity": 100, "maxWorkingOrders": 100,
                                "maxSubmitsPerMinute": 600}, caps


def test_a_reverse_of_a_position_over_the_cap_is_refused_at_the_dry_run():
    # A reverse is the THIRD way to reach a forbidden size, beside a submit and a change: its entry
    # is sized from the position that is there, so a 40-lot built by a strategy (uncapped by design)
    # would otherwise open a 40-lot the other way under a cap of 10. The refusal has to land on the
    # DRY RUN, or a token would exist for a plan the caps forbid.
    orders = Orders(max_quantity=10, position=("Long", 40))
    result = _run(orders, nt8.nt_position_reverse, **CLOSE)
    assert "above the cap of 10" in result.get("error", ""), result
    assert "POST /orders/close" in result["error"], result
    assert "confirm" not in result, "no token may be issued for a plan the caps forbid"
    assert orders.acted == 0


def test_a_close_of_the_same_oversized_position_is_not_capped():
    # close only REDUCES, so the quantity cap has nothing to protect there. Capping it would leave
    # an oversized position with no way out through this module, which is the worse failure.
    orders = Orders(max_quantity=10, position=("Long", 40))
    dry = _run(orders, nt8.nt_position_close, **CLOSE)
    assert dry["dryRun"] is True, dry
    assert dry["plan"]["position"]["quantity"] == 40, dry


def test_a_whole_bracket_counts_as_one_submit_for_the_rate_cap():
    # Four orders can leave the building — an entry, a stop and two targets — but the caller asked
    # once, so the rate window records one. Counting the legs would make a scale-out cost four times
    # what the same position costs through nt_order_submit.
    orders = Orders()
    with FakeAddon() as fake:
        _wire(fake, orders)
        dry = nt8.nt_order_bracket(**BRACKET)
        done = nt8.nt_order_bracket(**dict(BRACKET, confirm=dry["confirm"], issued_at=dry["issuedAt"]))
    assert len(done["exits"]) == 3, done          # one stop, two targets
    assert orders.submits == 1, orders.submits


# ── brackets ────────────────────────────────────────────────────────────────

def test_the_bracket_plan_names_the_stop_loss_and_every_target():
    orders = Orders()
    dry = _run(orders, nt8.nt_order_bracket, **BRACKET)
    plan = dry["plan"]
    assert plan["stopLossTicks"] == 20, plan
    assert [t["quantity"] for t in plan["targets"]] == [1, 1], plan
    assert plan["exitAction"] == "Sell", plan
    assert orders.acted == 0


def test_one_confirm_covers_the_entry_the_stop_and_every_target():
    orders = Orders()
    with FakeAddon() as fake:
        _wire(fake, orders)
        dry = nt8.nt_order_bracket(**BRACKET)
        done = nt8.nt_order_bracket(**dict(BRACKET, confirm=dry["confirm"], issued_at=dry["issuedAt"]))
    kinds = [leg["kind"] for leg in done["exits"]]
    assert kinds == ["stop", "target", "target"], done
    assert done["exitsPending"] is False, done
    assert done["oco"], "the stop and the targets must share an OCO id"
    assert orders.acted == 1, "one confirm, one bracket"


def test_the_exits_are_sized_to_what_the_entry_really_filled():
    # The plan asked for targets of 1 and 1 on a 2-lot entry. What the exits are sized to is the
    # FILLED quantity, so a partial fill must not leave a stop bigger than the position.
    orders = Orders()
    with FakeAddon() as fake:
        _wire(fake, orders)
        dry = nt8.nt_order_bracket(**BRACKET)
        done = nt8.nt_order_bracket(**dict(BRACKET, confirm=dry["confirm"], issued_at=dry["issuedAt"]))
    filled = done["entry"]["filled"]
    stop = next(leg for leg in done["exits"] if leg["kind"] == "stop")
    targets = [leg for leg in done["exits"] if leg["kind"] == "target"]
    assert stop["quantity"] == filled, done
    assert sum(t["quantity"] for t in targets) == filled, done


def test_a_resting_entry_comes_back_with_no_exits_and_says_so():
    # The failure this flag exists to prevent: reading "ok": true as "the position is protected".
    # Until the entry fills there is nothing to protect and no stop at the broker.
    orders = Orders(resting=True)
    with FakeAddon() as fake:
        _wire(fake, orders)
        dry = nt8.nt_order_bracket(**dict(BRACKET, order_type="Limit", limit_price=4900.0))
        done = nt8.nt_order_bracket(**dict(BRACKET, order_type="Limit", limit_price=4900.0,
                                           confirm=dry["confirm"], issued_at=dry["issuedAt"]))
    assert done["exitsPending"] is True, done
    assert done["exits"] == [], done
    assert done["entry"]["filled"] == 0, done


def test_a_bracket_entry_must_open_a_position():
    # Sell and BuyToCover CLOSE one; a stop and a target hung off them would face the wrong way.
    for action in ("Sell", "BuyToCover"):
        orders = Orders()
        result = _run(orders, nt8.nt_order_bracket, **dict(BRACKET, action=action))
        assert "must be Buy or SellShort" in result.get("error", ""), (action, result)
        assert orders.acted == 0


def test_a_bracket_needs_exactly_one_form_of_stop_loss():
    orders = Orders()
    none_given = dict(BRACKET)
    none_given.pop("stop_loss_ticks")
    result = _run(orders, nt8.nt_order_bracket, **none_given)
    assert "needs a stop loss" in result.get("error", ""), result

    orders = Orders()
    result = _run(orders, nt8.nt_order_bracket, **dict(BRACKET, stop_loss_price=4990.0))
    assert "not both" in result.get("error", ""), result
    assert orders.acted == 0


def test_the_target_quantities_must_add_up_to_the_entry_quantity():
    orders = Orders()
    result = _run(orders, nt8.nt_order_bracket,
                  **dict(BRACKET, targets=[{"ticks": 40, "quantity": 1}]))
    assert "add up to 1" in result.get("error", ""), result
    assert "entry quantity is 2" in result["error"], result
    assert orders.acted == 0


def test_the_bracket_arguments_map_to_the_addon_json_keys():
    orders = Orders()
    _run(orders, nt8.nt_order_bracket, **dict(BRACKET, order_type="StopLimit", limit_price=5001.0,
                                              stop_price=5000.0, stop_loss_ticks=None,
                                              stop_loss_price=4990.0, tif="Gtc"))
    body = orders.posts[0]
    assert body["type"] == "StopLimit", body                # order_type -> type
    assert body["stopLossPrice"] == 4990.0, body            # stop_loss_price -> stopLossPrice
    assert "stopLossTicks" not in body, body                # None is omitted, never sent as null
    assert body["targets"] == BRACKET["targets"], body
    assert body["tif"] == "Gtc", body


# ── change / cancel act on ANY live order of the account ────────────────────

def test_change_and_cancel_reach_an_order_this_module_did_not_place():
    # A running strategy's stop and an ATM's target are exactly the orders a test needs to move.
    # What protects the caller is the plan naming the owner, not a shorter list of ids.
    for owner in ("strategy SampleMACrossOver", "atm", "manual"):
        orders = Orders(live_orders={"o7": owner})
        with FakeAddon() as fake:
            _wire(fake, orders)
            dry = nt8.nt_order_cancel(account="Sim101", order_id="o7")
            done = nt8.nt_order_cancel(account="Sim101", order_id="o7",
                                       confirm=dry["confirm"], issued_at=dry["issuedAt"])
        assert dry["plan"]["owner"] == owner, dry
        assert done["state"] == "Cancelled", done
        assert orders.acted == 1, owner


def test_the_change_and_cancel_plans_name_the_owner_before_anything_is_signed():
    # OWNER= is a signed term, so an order that changed hands (a strategy picking up an order that
    # read as manual a moment ago) refuses the token instead of being moved blind.
    assert "|OWNER=" in Orders.CHANGE_PLAN, Orders.CHANGE_PLAN
    assert "|OWNER=" in Orders.CANCEL_PLAN, Orders.CANCEL_PLAN
    orders = Orders()
    moved = Orders.CANCEL_PLAN.replace("OWNER=module", "OWNER=atm") + Orders.MAC
    result = _run(orders, nt8.nt_order_cancel, account="Sim101", order_id="o1",
                  confirm=moved, issued_at=time.time())
    assert "does not match the plan" in result.get("error", ""), result
    assert orders.acted == 0


def test_an_unknown_order_id_is_a_404_not_a_silent_no_op():
    for fn, kw in ((nt8.nt_order_change, dict(account="Sim101", order_id="o999", quantity=2)),
                   (nt8.nt_order_cancel, dict(account="Sim101", order_id="o999"))):
        orders = Orders(live_orders={"o1": "module"})
        result = _run(orders, fn, **kw)
        assert "no order 'o999'" in result.get("error", ""), result
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
            _wire(fake, orders)
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
        _wire(fake, orders)
        dry = nt8.nt_order_cancel(account="Sim101", order_id="o1")
        done = nt8.nt_order_cancel(account="Sim101", order_id="o1",
                                   confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert done["state"] == "Cancelled", done
    assert orders.acted == 1


# ── close and reverse ───────────────────────────────────────────────────────

def test_the_close_plan_lists_the_position_and_every_order_it_will_cancel():
    orders = Orders(live_orders={"o1": "module", "o2": "strategy SampleMACrossOver"})
    dry = _run(orders, nt8.nt_position_close, **CLOSE)
    assert dry["plan"]["position"] == {"side": "Long", "quantity": 2, "averagePrice": 5000.0}, dry
    assert dry["plan"]["cancelOrders"] == ["o1", "o2"], dry
    assert dry["plan"]["enterAfterFlat"] is None, "a close enters nothing"
    assert orders.acted == 0


def test_close_reports_the_position_it_observed_afterwards():
    orders = Orders()
    with FakeAddon() as fake:
        _wire(fake, orders)
        dry = nt8.nt_position_close(**CLOSE)
        done = nt8.nt_position_close(**dict(CLOSE, confirm=dry["confirm"], issued_at=dry["issuedAt"]))
    assert done["positionAfter"]["side"] is None, done
    assert done["cancelledOrders"] == 1, done
    assert orders.acted == 1


def test_closing_an_account_with_nothing_to_close_is_refused():
    orders = Orders(position=None, live_orders={})
    result = _run(orders, nt8.nt_position_close, **CLOSE)
    assert "nothing to close" in result.get("error", ""), result
    assert orders.acted == 0


def test_reversing_without_a_position_is_refused():
    orders = Orders(position=None)
    result = _run(orders, nt8.nt_position_reverse, **CLOSE)
    assert "nothing to reverse" in result.get("error", ""), result
    assert orders.acted == 0


def test_the_reverse_plan_names_the_side_it_will_end_on():
    orders = Orders(position=("Long", 2))
    dry = _run(orders, nt8.nt_position_reverse, **CLOSE)
    assert dry["plan"]["enterAfterFlat"] == {"side": "Short", "quantity": 2, "type": "Market"}, dry
    assert orders.acted == 0


def test_reverse_reports_the_position_it_observed_afterwards():
    orders = Orders(position=("Long", 2))
    with FakeAddon() as fake:
        _wire(fake, orders)
        dry = nt8.nt_position_reverse(**CLOSE)
        done = nt8.nt_position_reverse(**dict(CLOSE, confirm=dry["confirm"], issued_at=dry["issuedAt"]))
    assert done["positionBefore"]["side"] == "Long", done
    assert done["positionAfter"] == {"side": "Short", "quantity": 2, "averagePrice": 5001.0}, done
    assert done["entry"]["filled"] == 2, done
    assert orders.submits == 1, "the reverse entry is one submit"


# ── the wire body ───────────────────────────────────────────────────────────

def test_optional_arguments_are_omitted_from_the_body_not_sent_as_null():
    # `confirm` ABSENT is what makes a call a dry run. If the tool sent confirm:null the AddOn would
    # see the key and a future reader of either side could not tell the two calls apart.
    orders = Orders()
    _run(orders, nt8.nt_order_submit, account="Sim101", instrument="ES 12-26", action="Buy",
         order_type="Market", quantity=1)
    assert orders.posts[0] == {"account": "Sim101", "instrument": "ES 12-26", "action": "Buy",
                               "type": "Market", "quantity": 1}, orders.posts[0]

    orders = Orders()
    _run(orders, nt8.nt_position_close, account="Sim101", instrument="ES 12-26")
    assert orders.posts[0] == {"account": "Sim101", "instrument": "ES 12-26"}, orders.posts[0]


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
        _wire(fake, orders)
        _every_tool(orders)
    assert orders.paths == ["/orders/" + v for v in POST_VERBS], orders.paths


# ── the tools' own surface ──────────────────────────────────────────────────

def test_there_are_exactly_six_order_tools():
    exported = sorted(k for k in vars(tools_orders) if k.startswith("nt_"))
    assert exported == ["nt_order_bracket", "nt_order_cancel", "nt_order_change",
                        "nt_order_submit", "nt_position_close", "nt_position_reverse"], exported


def test_the_tools_are_registered_under_their_documented_names():
    names = sorted(t.name for t in nt8.mcp._tool_manager.list_tools()
                   if t.name.startswith(("nt_order", "nt_position_")))
    assert names == ["nt_order_bracket", "nt_order_cancel", "nt_order_change", "nt_order_submit",
                     "nt_position_close", "nt_position_reverse"], names


def test_account_and_the_order_identity_are_mandatory_arguments():
    for fn in (nt8.nt_order_submit, nt8.nt_order_bracket, nt8.nt_order_change,
               nt8.nt_order_cancel, nt8.nt_position_close, nt8.nt_position_reverse):
        try:
            fn()  # type: ignore[call-arg]
        except TypeError:
            continue
        raise AssertionError("%s must require its identifying arguments" % fn.__name__)


ALL_TOOLS = (tools_orders.nt_order_submit, tools_orders.nt_order_bracket,
             tools_orders.nt_order_change, tools_orders.nt_order_cancel,
             tools_orders.nt_position_close, tools_orders.nt_position_reverse)


def test_every_docstring_states_what_a_caller_must_know():
    for fn in ALL_TOOLS:
        doc = fn.__doc__ or ""
        for phrase in ("simulator", "playback", "two steps", "30 seconds", "orders.enabled", "403"):
            assert phrase in doc.lower(), "%s's docstring must state %r" % (fn.__name__, phrase)
        # These two carry their emphasis: a model skims, and the capitals are the signal.
        assert "DATA" in doc, "%s must say NinjaTrader text is DATA" % fn.__name__
        assert "ONLY" in doc, "%s must say Simulator/Playback ONLY" % fn.__name__


def test_the_shared_paragraphs_are_spliced_in_before_the_tool_is_registered():
    # They are spliced by a decorator UNDER @mcp.tool, so the registered description carries them
    # too. Done after registration the tool a model actually reads would hold "{gates}".
    for tool in nt8.mcp._tool_manager.list_tools():
        if not tool.name.startswith(("nt_order", "nt_position_")):
            continue
        assert "{gates}" not in (tool.description or ""), tool.name
        assert "{caps}" not in (tool.description or ""), tool.name
        assert "TWO STEPS" in (tool.description or ""), tool.name


def test_the_docstrings_state_the_caps_in_force_and_that_ok_is_not_filled():
    for fn in ALL_TOOLS:
        doc = fn.__doc__ or ""
        for phrase in ("orders.config.json", "100 / 100 / 600", "default 10", "default 20",
                       "default 60"):
            assert phrase in doc, "%s's docstring must state %r" % (fn.__name__, phrase)
    assert "NEVER means filled" in (tools_orders.nt_order_submit.__doc__ or "")


def test_the_bracket_docstring_warns_that_a_resting_entry_is_not_protected():
    doc = tools_orders.nt_order_bracket.__doc__ or ""
    assert "NOT PROTECTED" in doc, doc
    assert "exitsPending" in doc, doc
    assert "REAL AVERAGE FILL PRICE" in doc, doc


def test_the_bracket_docstring_says_the_exits_outlive_the_arming_file():
    # The one thing disarming does NOT stop: a bracket whose entry was already accepted still gets
    # its stop, because a filled entry with no stop is worse. A caller told "403 on everything"
    # would believe the module had gone quiet while it was still sending orders.
    doc = tools_orders.nt_order_bracket.__doc__ or ""
    assert "AFTER THE MODULE IS DISARMED" in doc, doc
    assert "bracketDisarmed" in doc, doc
    assert "cancel the" in doc.lower(), "it must say how to leave nothing pending"
    # and the shared OFF BY DEFAULT paragraph, on every tool, names the one exception
    assert "the arming file: the exits of a bracket" in (tools_orders.nt_order_submit.__doc__ or "")


def test_the_change_and_cancel_docstrings_say_the_order_may_not_be_ours():
    for fn in (tools_orders.nt_order_change, tools_orders.nt_order_cancel):
        doc = fn.__doc__ or ""
        assert "EVERY LIVE ORDER OF THE ACCOUNT" in doc, fn.__name__
        assert "plan.owner" in doc or "OWNER" in doc, fn.__name__
        assert "atm" in doc.lower(), fn.__name__


def test_the_position_docstrings_say_one_account_one_instrument():
    for fn in (tools_orders.nt_position_close, tools_orders.nt_position_reverse):
        doc = fn.__doc__ or ""
        assert "ONE ACCOUNT, ONE INSTRUMENT" in doc, fn.__name__
        assert "FlattenEverything() is never called" in doc, fn.__name__
        assert "OBSERVED" in doc, fn.__name__


def test_the_reverse_docstring_says_the_quantity_cap_applies_to_the_new_side():
    doc = tools_orders.nt_position_reverse.__doc__ or ""
    assert "THE QUANTITY CAP APPLIES TO THE NEW SIDE" in doc, doc
    assert "nt_position_close" in doc, "it must name the way out of an oversized position"


def test_no_docstring_promises_a_live_account_or_a_widening_file():
    # The one sentence that must never appear: this module has no ops.live equivalent, and a
    # docstring hinting at one would invite a model to go looking for the file.
    for fn in ALL_TOOLS:
        doc = (fn.__doc__ or "").lower()
        assert "never a live" in doc or "no other provider" in doc or "no switch" in doc, fn.__name__
        assert "ops.live" not in doc.replace("no ops.live equivalent", ""), fn.__name__


def test_the_docstring_does_not_promise_a_plan_the_passthrough_throws_away():
    # app.py collapses every non-2xx to {"error": <sentence>}: the status code and the 409's new
    # plan do not reach the model. A docstring that promised the plan invited a blind retry on the
    # tools that can open a position.
    for fn in ALL_TOOLS:
        doc = fn.__doc__ or ""
        assert '{"error"' in doc, "%s must state the shape a refusal really arrives in" % fn.__name__
        assert "do not survive the passthrough" in doc.lower(), fn.__name__
