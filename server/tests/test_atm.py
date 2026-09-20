"""The five ATM tools (nt8_mcp.tools_atm) against a canned AddOn that enforces the /atm/* contract.

The gates themselves live in C# — addon/NT8BridgeAtm.cs borrows the whole chain from
addon/NT8BridgeOrders.cs — and are verified against a running NinjaTrader. What is pinned HERE is
the contract that chain publishes on the ATM paths, and the things a client can get catastrophically
wrong on tools that open a position and then hand it to NinjaTrader's own bracket manager:

  * calling an unarmed module and reading the 403 as "nothing to do" — including on the two READS,
    which are behind the same flag because ATM template names are the user's own,
  * inventing or reusing a confirm token instead of echoing the one the AddOn just issued,
  * REPLAYING a confirm that already worked, which on /atm/start would start a second ATM,
  * sending a confirm issued for a different verb — an nt_order_submit token must not start an ATM,
  * sending an argument as null instead of omitting it: `confirm` being ABSENT is what makes a call
    a dry run, so a null would turn every dry run into a confirmed one if the AddOn read it,
  * treating a fresh nt_atm_start as a protected position when the entry has not filled,
  * treating "templates": null (the folder could not be read) as "there are no templates".

`Atm` below is a spec of the documented contract (docs/api/atm.md), not a second implementation of
the AddOn: it hands out an OPAQUE confirm string, so a client that computed its own would fail.

Note on shapes: nt8_mcp.app collapses every non-2xx answer to {"error": <the AddOn's sentence>}, so
the refusal tests assert on that string.
"""

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import server as nt8, tools_atm  # noqa: E402

UNARMED = "orders module not armed"
CAPS_TEXT = "CAPS qty=10/default working=20/default rate=60/default"
CAPS = {"maxQuantity": 10, "maxWorkingOrders": 20, "maxSubmitsPerMinute": 60,
        "ceilings": {"maxQuantity": 100, "maxWorkingOrders": 100, "maxSubmitsPerMinute": 600},
        "warnings": []}

# A stand-in template name. Real ones are the user's own and are in no file of this project.
TEMPLATE = "MyAtmTemplate"
TEMPLATE_PARAMS = "brackets=1 mode=Ticks B0 sl=8 t=16 q=1"

GET_PATHS = ("/atm/templates", "/atm/status")
POST_VERBS = ("start", "close", "change")


class Atm:
    """The documented /atm/* behaviour, as a scriptable fake.

    armed=False      -> 403 on ALL FIVE paths, the reads included, before any body is parsed
    flag_age_h > 24  -> the SAME 403: a stale flag is not a flag
    any_live=True    -> 409 on every POST; the two reads still answer and report anyLive
    sim=False        -> 403. There is no second file that widens this.
    atms             -> {atmId: {"filled": bool}}. An ATM with no live order and no position is
                        not listed and cannot be closed.
    """

    PLANS = {
        "start": ("atm.start|ACCOUNT=Sim101|INSTRUMENT=ES 12-26|ACTION=Buy|TYPE=Market|QTY=1"
                  "|LIMIT=none|STOP=none|TIF=Day|TEMPLATE=" + TEMPLATE
                  + "|TEMPLATEPARAMS=" + TEMPLATE_PARAMS + "|" + CAPS_TEXT),
        "close": ("atm.close|ACCOUNT=Sim101|ATMID=a1|TEMPLATE=" + TEMPLATE
                  + "|INSTRUMENT=ES 12-26|POSITION=Long 1|CANCEL=o2 o3|" + CAPS_TEXT),
        "change": ("atm.change|ACCOUNT=Sim101|ATMID=a1|TEMPLATE=" + TEMPLATE
                   + "|INSTRUMENT=ES 12-26|BRACKET=0|STOP o2 StopMarket state=Working qty=1 "
                   "FROM limit=none stop=4998 TO limit=none stop=4999|" + CAPS_TEXT),
    }
    MAC = " #71c0de44aa19b3f2"

    def __init__(self, armed=True, flag_age_h=0.1, sim=True, any_live=False, window=30.0,
                 atms=None, templates=("MyAtmTemplate",), filled=True, brackets=1,
                 brackets_readable=True):
        self.armed, self.flag_age_h, self.sim, self.any_live = armed, flag_age_h, sim, any_live
        self.window = window
        # brackets_readable=False is AtmStrategy.Brackets throwing or reading null: the bracket
        # count is then unknown, so targetIndex cannot be checked against anything.
        self.brackets, self.brackets_readable = brackets, brackets_readable
        self.templates = None if templates is None else list(templates)
        self.atms = dict(atms if atms is not None else {"a1": {"filled": filled}})
        self.filled = filled
        self.posts: list[dict] = []
        self.paths: list[str] = []
        self.used: set[tuple[str, float]] = set()
        self.acted = 0          # state changes that reached NinjaTrader
        self.started = 0        # ATM strategies actually started

    def confirm_for(self, verb):
        return self.PLANS[verb] + self.MAC

    # -- the gate chain ------------------------------------------------------

    def __call__(self, req):
        self.paths.append(req.path)
        verb = req.path.rsplit("/", 1)[-1]

        # Gate 1 first, before the body is parsed. The READS are behind it too: a disarmed module
        # does not publish the user's ATM template names.
        if not self.armed or self.flag_age_h > 24:
            return 403, {"error": UNARMED}
        if req.method == "GET":
            return 200, (self._templates() if verb == "templates" else self._status(req))

        body = json.loads(req.body) if req.body else {}
        self.posts.append(body)

        if self.any_live:
            return 409, {"error": "atm.%s refused: a live order-routing connection is up "
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

    # -- per-verb validation -------------------------------------------------

    def _check_start(self, body):
        if body.get("action") not in ("Buy", "SellShort"):
            return 400, {"error": "an ATM entry opens a position: action must be Buy or SellShort. "
                                  "Use /orders/submit, /orders/close or /atm/close to reduce one"}
        name = (body.get("template") or "").strip()
        if not name:
            return 400, {"error": "template is required — one name from GET /atm/templates"}
        if name != os.path.basename(name) or ".." in name or "/" in name or "\\" in name:
            return 400, {"error": "template '%s' is not a template name — it must be one plain "
                                  "file name, as GET /atm/templates lists it, with no path in it"
                                  % name}
        if self.templates is not None and name not in self.templates:
            return 404, {"error": "no ATM strategy template '%s' — call GET /atm/templates for the "
                                  "saved ones" % name}
        if (body.get("quantity") or 0) > CAPS["maxQuantity"]:
            return 403, {"error": "quantity %s is above the cap of %s (default); the hard ceiling "
                                  "in code is 100" % (body["quantity"], CAPS["maxQuantity"])}
        return None

    def _check_atm_id(self, body):
        atm_id = (body.get("atmId") or "").strip()
        if not atm_id:
            return 400, {"error": "atmId is required — one of the ids GET /atm/status lists, or "
                                  "the id POST /atm/start returned"}
        if atm_id not in self.atms:
            return 404, {"error": "no ATM strategy '%s' on '%s' — call GET /atm/status for the "
                                  "active ones" % (atm_id, body["account"])}
        return None

    _check_close = _check_atm_id

    def _check_change(self, body):
        bad = self._check_atm_id(body)
        if bad is not None:
            return bad
        if body.get("stopPrice") is None and body.get("targetPrice") is None:
            return 400, {"error": "nothing to change — give stopPrice, targetPrice, or both"}
        index = body.get("targetIndex") or 0
        # An unreadable bracket list REFUSES. Letting the index check fall away would make "could
        # not read" mean "allow", and an arbitrary index would then be handed to the ATM's own legs.
        if not self.brackets_readable:
            return 500, {"error": "the brackets of ATM strategy '%s' could not be read "
                                  "(AtmStrategy.Brackets is null) — targetIndex cannot be checked "
                                  "against them" % body["atmId"]}
        if index >= self.brackets:
            return 400, {"error": "ATM strategy '%s' has %s bracket(s); targetIndex %s is past the "
                                  "last one" % (body["atmId"], self.brackets, index)}
        if not self.atms[body["atmId"]]["filled"]:
            return 409, {"error": "bracket %s of this ATM strategy has no live stop order — the "
                                  "entry may not have filled yet; read GET /atm/status"
                                  % (body.get("targetIndex") or 0)}
        return None

    # -- the token -----------------------------------------------------------

    def _check_token(self, body, verb):
        if "issuedAt" not in body:
            return 400, {"error": "confirm was given without issuedAt — both come from the dry run"}
        if time.time() - float(body["issuedAt"]) > self.window:
            return 409, {"error": "issuedAt is too old; the confirm window is %.0f s — run the dry "
                                  "run again and use the token it returns" % self.window}
        if body["confirm"] != self.confirm_for(verb):
            return 409, {"error": "confirm does not match the plan as it is NOW — the ATM, the "
                                  "template, the account or the caps moved since the dry run, or "
                                  "the token was issued for a different call"}
        spent = (body["confirm"], float(body["issuedAt"]))
        if spent in self.used:
            return 409, {"error": "this confirm was already used — a confirm authorises ONE call, "
                                  "not every call inside the window"}
        self.used.add(spent)
        return None

    # -- the plans -----------------------------------------------------------

    def _plan(self, verb, body):
        if verb == "start":
            return {"account": body["account"], "instrument": body.get("instrument"),
                    "action": body.get("action"), "type": body.get("type"),
                    "quantity": body.get("quantity"), "limitPrice": body.get("limitPrice"),
                    "stopPrice": body.get("stopPrice"), "tif": body.get("tif") or "Day",
                    "template": body["template"], "templateParams": TEMPLATE_PARAMS}
        if verb == "close":
            return {"account": body["account"], "atmId": body["atmId"], "template": TEMPLATE,
                    "instrument": "ES 12-26",
                    "position": {"side": "Long", "quantity": 1, "averagePrice": 5000.0},
                    "cancelOrders": ["o2", "o3"]}
        return {"account": body["account"], "atmId": body["atmId"], "template": TEMPLATE,
                "instrument": "ES 12-26", "targetIndex": body.get("targetIndex") or 0,
                "orders": [{"kind": "stop", "orderId": "o2", "type": "StopMarket",
                            "state": "Working", "quantity": 1,
                            "from": {"limitPrice": None, "stopPrice": 4998.0},
                            "to": {"limitPrice": None, "stopPrice": body.get("stopPrice")}}]}

    # -- the acts ------------------------------------------------------------

    def _do_start(self, body):
        self.started += 1
        atm_id = "a%d" % (len(self.atms) + 1)
        self.atms[atm_id] = {"filled": self.filled}
        brackets = ([{"index": 0, "quantity": 1, "stopLoss": 8.0, "target": 16.0,
                      "stops": [{"orderId": "o2", "state": "Working"}],
                      "targets": [{"orderId": "o3", "state": "Working"}], "error": None}]
                    if self.filled else
                    [{"index": 0, "quantity": 1, "stopLoss": 8.0, "target": 16.0,
                      "stops": [], "targets": [], "error": None}])
        return {"ok": True, "dryRun": False, "account": body["account"],
                "instrument": body["instrument"], "atmId": atm_id, "template": body["template"],
                "entry": {"orderId": "o1", "state": "Filled" if self.filled else "Working",
                          "quantity": body["quantity"],
                          "filled": body["quantity"] if self.filled else 0,
                          "averageFillPrice": 5000.0 if self.filled else None,
                          "rejected": False, "ninjaTraderText": None, "readError": None},
                "brackets": brackets, "exitsPending": not self.filled,
                "error": None, "caps": CAPS}

    def _do_close(self, body):
        self.atms.pop(body["atmId"], None)
        return {"ok": True, "dryRun": False, "account": body["account"], "atmId": body["atmId"],
                "instrument": "ES 12-26",
                "positionBefore": {"side": "Long", "quantity": 1, "averagePrice": 5000.0},
                "positionAfter": {"side": None, "quantity": 0, "averagePrice": None},
                "positionAfterError": None, "ordersBefore": 2, "ordersStillLive": [],
                "error": None, "caps": CAPS}

    def _do_change(self, body):
        return {"ok": True, "dryRun": False, "account": body["account"], "atmId": body["atmId"],
                "instrument": "ES 12-26", "landed": 1,
                "orders": [{"kind": "stop", "landed": True,
                            "order": {"orderId": "o2", "type": "StopMarket", "state": "Working",
                                      "stopPrice": body.get("stopPrice"), "owner": "atm"}}],
                "error": None, "caps": CAPS}

    # -- the reads -----------------------------------------------------------

    def _templates(self):
        if self.templates is None:
            # The folder could not be listed. null, never [] — "none" and "could not look" are
            # different answers and only one of them is safe to act on.
            return {"folder": "<userdata>\\templates\\AtmStrategy", "folderExists": False,
                    "templates": None, "error": "The device is not ready.", "note": "..."}
        return {"folder": "<userdata>\\templates\\AtmStrategy", "folderExists": True,
                "templates": [{"name": t, "file": "<userdata>\\templates\\AtmStrategy\\%s.xml" % t,
                               "nameInFile": t, "calculationMode": "Ticks",
                               "brackets": [{"index": 0, "quantity": 1, "stopLoss": 8.0,
                                             "target": 16.0, "stopStrategy": None}],
                               "unreadKeys": [], "error": None}
                              for t in self.templates],
                "error": None, "note": "..."}

    def _status(self, req):
        wanted = (req.query.get("account") or [None])[0]
        rows = [{"atmId": atm_id, "template": TEMPLATE, "account": "Sim101",
                 "instrument": "ES 12-26", "state": "Realtime", "entryQuantity": 1,
                 "calculationMode": "Ticks", "active": True,
                 "position": {"side": "Long", "quantity": 1, "averagePrice": 5000.0},
                 "positionError": None,
                 "entry": {"orderId": "o1", "state": "Filled", "owner": "atm"},
                 "brackets": [{"index": 0, "quantity": 1, "stopLoss": 8.0, "target": 16.0,
                               "stops": [{"orderId": "o2", "state": "Working"}]
                                        if row["filled"] else [],
                               "targets": [{"orderId": "o3", "state": "Working"}]
                                          if row["filled"] else [],
                               "error": None}],
                 "orders": []}
                for atm_id, row in sorted(self.atms.items())]
        if wanted:
            rows = [r for r in rows if r["account"].lower() == wanted.lower()]
        return {"anyLive": self.any_live, "postsRefused": self.any_live, "account": wanted,
                "atms": rows, "finished": 3, "hiddenNonSimulator": 2, "error": None, "note": "..."}


def _wire(fake, atm):
    """All FIVE routes on ONE handler: the arming gate and the confirm gate are shared, and a fake
    that split them per route could pass while the real module enforced them in one place only."""
    for path in GET_PATHS:
        fake.register(path, "GET", atm)
    for verb in POST_VERBS:
        fake.register("/atm/" + verb, "POST", atm)
    return fake


def _run(atm, fn, **kw):
    with FakeAddon() as fake:
        _wire(fake, atm)
        return fn(**kw)


START = dict(account="Sim101", instrument="ES 12-26", action="Buy", order_type="Market",
             quantity=1, template=TEMPLATE)

ALL_TOOLS = (tools_atm.nt_atm_templates, tools_atm.nt_atm_status, tools_atm.nt_atm_start,
             tools_atm.nt_atm_close, tools_atm.nt_atm_change)
WRITE_TOOLS = (tools_atm.nt_atm_start, tools_atm.nt_atm_close, tools_atm.nt_atm_change)


def _every_tool():
    """One call to each of the five tools, in a shape each of them accepts."""
    return [nt8.nt_atm_templates(),
            nt8.nt_atm_status(),
            nt8.nt_atm_start(**START),
            nt8.nt_atm_close(account="Sim101", atm_id="a1"),
            nt8.nt_atm_change(account="Sim101", atm_id="a1", stop_price=4999.0)]


# ── the arming flag ─────────────────────────────────────────────────────────

def test_unarmed_is_403_on_every_route_including_the_reads():
    atm = Atm(armed=False)
    with FakeAddon() as fake:
        _wire(fake, atm)
        results = _every_tool()
    for result in results:
        assert result == {"error": UNARMED}, result
    assert atm.acted == 0, "an unarmed module must not act"
    assert len(atm.paths) == 5, "every tool must reach the AddOn and be refused there"


def test_a_flag_older_than_24h_is_the_same_403_as_no_flag():
    # A flag forgotten after one debugging session must not arm ATM entry for ever.
    atm = Atm(flag_age_h=25.0)
    assert _run(atm, nt8.nt_atm_start, **START) == {"error": UNARMED}
    assert _run(Atm(flag_age_h=25.0), nt8.nt_atm_templates) == {"error": UNARMED}


# ── the provider rule and the live-routing guard ────────────────────────────

def test_a_non_simulator_account_is_refused_and_nothing_is_started():
    atm = Atm(sim=False)
    with FakeAddon() as fake:
        _wire(fake, atm)
        for fn, kw in ((nt8.nt_atm_start, START),
                       (nt8.nt_atm_close, dict(account="Sim101", atm_id="a1")),
                       (nt8.nt_atm_change, dict(account="Sim101", atm_id="a1", stop_price=4999.0))):
            result = fn(**kw)
            assert "no other provider" in result["error"], result
    assert atm.acted == 0 and atm.started == 0


def test_a_live_routing_connection_refuses_the_writes_but_not_the_reads():
    atm = Atm(any_live=True)
    with FakeAddon() as fake:
        _wire(fake, atm)
        assert "live order-routing connection" in nt8.nt_atm_start(**START)["error"]
        assert "live order-routing connection" in nt8.nt_atm_close(account="Sim101",
                                                                   atm_id="a1")["error"]
        # Listing templates and reading state routes nothing, so it answers and REPORTS anyLive.
        status = nt8.nt_atm_status()
        assert status["anyLive"] is True and status["postsRefused"] is True, status
        assert nt8.nt_atm_templates()["templates"] is not None
    assert atm.started == 0


# ── the two steps ───────────────────────────────────────────────────────────

def test_a_call_without_confirm_is_a_dry_run_and_starts_nothing():
    atm = Atm()
    result = _run(atm, nt8.nt_atm_start, **START)
    assert result["dryRun"] is True, result
    assert result["plan"]["template"] == TEMPLATE, result
    assert result["confirm"] and result["issuedAt"], result
    assert atm.acted == 0 and atm.started == 0, "a dry run must reach no account"


def test_the_absent_arguments_are_omitted_from_the_body_not_sent_as_null():
    # `confirm` being ABSENT is what makes a call a dry run. A client that sent confirm=None would
    # turn every dry run into a confirmed call the moment an AddOn read the key as present.
    atm = Atm()
    _run(atm, nt8.nt_atm_start, **START)
    body = atm.posts[-1]
    for key in ("confirm", "issuedAt", "limitPrice", "stopPrice", "tif"):
        assert key not in body, "%s must be omitted, not sent as null: %r" % (key, body)
    atm = Atm()
    _run(atm, nt8.nt_atm_change, account="Sim101", atm_id="a1", stop_price=4999.0)
    assert "targetPrice" not in atm.posts[-1], atm.posts[-1]
    assert "targetIndex" not in atm.posts[-1], atm.posts[-1]


def test_the_confirm_from_the_dry_run_acts_exactly_once():
    atm = Atm()
    with FakeAddon() as fake:
        _wire(fake, atm)
        dry = nt8.nt_atm_start(**START)
        done = nt8.nt_atm_start(confirm=dry["confirm"], issued_at=dry["issuedAt"], **START)
        assert done["ok"] is True and done["atmId"], done
        # THE REPLAY. A retry after an HTTP timeout, or a model repeating its last tool call, would
        # start a SECOND ATM strategy on the same account.
        again = nt8.nt_atm_start(confirm=dry["confirm"], issued_at=dry["issuedAt"], **START)
        assert "already used" in again["error"], again
    assert atm.started == 1, "one confirm authorises ONE start"


def test_an_invented_or_edited_confirm_is_refused():
    atm = Atm()
    with FakeAddon() as fake:
        _wire(fake, atm)
        dry = nt8.nt_atm_start(**START)
        for token in (Atm.PLANS["start"], dry["confirm"] + "x", "yes", Atm.PLANS["start"] + " #0"):
            result = nt8.nt_atm_start(confirm=token, issued_at=dry["issuedAt"], **START)
            assert "does not match" in result["error"], (token, result)
    assert atm.started == 0


def test_a_confirm_issued_for_another_verb_cannot_start_an_atm():
    # The readable half of every plan starts with its own verb, so an /orders/submit token, an
    # nt_flatten token and an atm.close token can never authorise an atm.start.
    atm = Atm()
    with FakeAddon() as fake:
        _wire(fake, atm)
        close_dry = nt8.nt_atm_close(account="Sim101", atm_id="a1")
        result = nt8.nt_atm_start(confirm=close_dry["confirm"], issued_at=close_dry["issuedAt"],
                                  **START)
        assert "does not match" in result["error"], result
        foreign = ("orders.submit|ACCOUNT=Sim101|INSTRUMENT=ES 12-26|ACTION=Buy|TYPE=Market|QTY=1"
                   "|" + CAPS_TEXT + Atm.MAC)
        result = nt8.nt_atm_start(confirm=foreign, issued_at=close_dry["issuedAt"], **START)
        assert "does not match" in result["error"], result
    assert atm.started == 0


def test_a_stale_issued_at_is_refused():
    atm = Atm()
    with FakeAddon() as fake:
        _wire(fake, atm)
        dry = nt8.nt_atm_start(**START)
        result = nt8.nt_atm_start(confirm=dry["confirm"], issued_at=dry["issuedAt"] - 120.0,
                                  **START)
        assert "too old" in result["error"], result
    assert atm.started == 0


# ── what the AddOn refuses, and the tool does not second-guess ──────────────

def test_an_atm_entry_must_open_a_position():
    atm = Atm()
    with FakeAddon() as fake:
        _wire(fake, atm)
        for action in ("Sell", "BuyToCover"):
            result = nt8.nt_atm_start(**dict(START, action=action))
            assert "must be Buy or SellShort" in result["error"], (action, result)
    assert atm.started == 0


def test_a_template_name_with_a_path_in_it_is_refused():
    # The name becomes a file path in the AddOn, so it is checked at that boundary.
    atm = Atm()
    with FakeAddon() as fake:
        _wire(fake, atm)
        for name in ("../../evil", "sub/MyAtmTemplate", "sub\\MyAtmTemplate"):
            result = nt8.nt_atm_start(**dict(START, template=name))
            assert "not a template name" in result["error"], (name, result)
        assert "no ATM strategy template" in nt8.nt_atm_start(
            **dict(START, template="NoSuchTemplate"))["error"]
    assert atm.started == 0


def test_a_change_with_no_price_and_an_unknown_atm_are_both_refused():
    atm = Atm()
    with FakeAddon() as fake:
        _wire(fake, atm)
        assert "nothing to change" in nt8.nt_atm_change(account="Sim101", atm_id="a1")["error"]
        assert "no ATM strategy" in nt8.nt_atm_change(account="Sim101", atm_id="zz",
                                                      stop_price=4999.0)["error"]
        assert "no ATM strategy" in nt8.nt_atm_close(account="Sim101", atm_id="zz")["error"]
    assert atm.acted == 0


def test_a_bracket_whose_entry_has_not_filled_has_nothing_to_move():
    atm = Atm(filled=False)
    result = _run(atm, nt8.nt_atm_change, account="Sim101", atm_id="a1", stop_price=4999.0)
    assert "has no live stop order" in result["error"], result
    assert atm.acted == 0


def test_a_target_index_past_the_last_bracket_is_refused():
    atm = Atm(brackets=1)
    result = _run(atm, nt8.nt_atm_change, account="Sim101", atm_id="a1", stop_price=4999.0,
                  target_index=3)
    assert "is past the last one" in result["error"], result
    assert atm.acted == 0


def test_an_unreadable_bracket_list_refuses_the_change_rather_than_skipping_the_check():
    # "Could not read" must never mean "allow". With the bracket count unknown there is nothing to
    # check targetIndex against, so an arbitrary index would otherwise be handed straight to the
    # ATM's own stop and target legs — an unreadable read WIDENING what a confirmed write may
    # address, which is the opposite of what every other read in this module does.
    atm = Atm(brackets_readable=False)
    result = _run(atm, nt8.nt_atm_change, account="Sim101", atm_id="a1", stop_price=4999.0,
                  target_index=7)
    assert "could not be read" in result["error"], result
    assert "targetIndex cannot be checked" in result["error"], result
    assert atm.acted == 0
    # and the same is true of the default index 0, which is not more readable than any other
    atm = Atm(brackets_readable=False)
    assert "could not be read" in _run(atm, nt8.nt_atm_change, account="Sim101", atm_id="a1",
                                       stop_price=4999.0)["error"]
    assert atm.acted == 0


# ── what comes back is a measurement ────────────────────────────────────────

def test_a_resting_entry_comes_back_as_pending_exits_and_empty_brackets():
    # The one result a client must not read as "protected": the ATM arms its stop and target on the
    # fill, so until then nothing stands between the position and the market.
    atm = Atm(filled=False)
    with FakeAddon() as fake:
        _wire(fake, atm)
        dry = nt8.nt_atm_start(**START)
        done = nt8.nt_atm_start(confirm=dry["confirm"], issued_at=dry["issuedAt"], **START)
    assert done["exitsPending"] is True, done
    assert done["entry"]["filled"] == 0, done
    assert done["brackets"][0]["stops"] == [] and done["brackets"][0]["targets"] == [], done


def test_a_filled_entry_carries_its_stop_and_target_and_an_atm_id():
    atm = Atm(filled=True)
    with FakeAddon() as fake:
        _wire(fake, atm)
        dry = nt8.nt_atm_start(**START)
        done = nt8.nt_atm_start(confirm=dry["confirm"], issued_at=dry["issuedAt"], **START)
        status = nt8.nt_atm_status(account="Sim101")
    assert done["exitsPending"] is False, done
    assert done["brackets"][0]["stops"][0]["orderId"] == "o2", done
    assert done["atmId"] in [row["atmId"] for row in status["atms"]], (done, status)


def test_close_reports_the_position_it_observed_afterwards():
    atm = Atm()
    with FakeAddon() as fake:
        _wire(fake, atm)
        dry = nt8.nt_atm_close(account="Sim101", atm_id="a1")
        assert dry["plan"]["cancelOrders"] == ["o2", "o3"], dry
        done = nt8.nt_atm_close(account="Sim101", atm_id="a1",
                                confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert done["positionAfter"]["side"] is None, done
    assert done["ordersStillLive"] == [], done
    assert "a1" not in atm.atms


def test_change_reports_whether_each_order_landed_and_who_owns_it():
    atm = Atm()
    with FakeAddon() as fake:
        _wire(fake, atm)
        dry = nt8.nt_atm_change(account="Sim101", atm_id="a1", stop_price=4999.0)
        assert dry["plan"]["orders"][0]["from"]["stopPrice"] == 4998.0, dry
        assert dry["plan"]["orders"][0]["to"]["stopPrice"] == 4999.0, dry
        done = nt8.nt_atm_change(account="Sim101", atm_id="a1", stop_price=4999.0,
                                 confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert done["landed"] == 1, done
    assert done["orders"][0]["order"]["owner"] == "atm", done


def test_the_account_filter_reaches_the_addon_as_a_query_parameter():
    atm = Atm()
    with FakeAddon() as fake:
        _wire(fake, atm)
        assert nt8.nt_atm_status(account="Sim101")["account"] == "Sim101"
        assert nt8.nt_atm_status()["account"] is None
    assert atm.paths == ["/atm/status", "/atm/status"]


def test_an_unreadable_template_folder_is_null_not_an_empty_list():
    # "there are no templates" and "I could not look" must never read the same.
    result = _run(Atm(templates=None), nt8.nt_atm_templates)
    assert result["templates"] is None, result
    assert result["error"], result


def test_the_templates_read_carries_the_calculation_mode_beside_the_numbers():
    # 8 stop loss means nothing without "Ticks" beside it.
    result = _run(Atm(), nt8.nt_atm_templates)
    row = result["templates"][0]
    assert row["calculationMode"] == "Ticks", row
    assert row["brackets"][0]["stopLoss"] == 8.0, row
    assert row["name"] == TEMPLATE, row


# ── the docstrings a model actually reads ───────────────────────────────────

def test_the_shared_paragraphs_are_spliced_in_before_the_tool_is_registered():
    for tool in nt8.mcp._tool_manager.list_tools():
        if not tool.name.startswith("nt_atm_"):
            continue
        assert "{gates}" not in (tool.description or ""), tool.name
        assert "{caps}" not in (tool.description or ""), tool.name
    assert "TWO STEPS" in (tools_atm.nt_atm_start.__doc__ or "")


def test_the_write_docstrings_state_the_gates_and_the_caps():
    for fn in WRITE_TOOLS:
        doc = fn.__doc__ or ""
        for phrase in ("TWO STEPS", "ONE CALL, NOT A 30-SECOND WINDOW", "orders.enabled",
                       "orders.config.json", "100 / 100 / 600", "default 10"):
            assert phrase in doc, "%s's docstring must state %r" % (fn.__name__, phrase)


def test_no_docstring_promises_a_live_account_or_a_widening_file():
    # The one sentence that must never appear: this module has no ops.live equivalent, and a
    # docstring hinting at one would invite a model to go looking for the file.
    for fn in WRITE_TOOLS:
        doc = (fn.__doc__ or "").lower()
        assert "never a live" in doc or "no other provider" in doc or "no switch" in doc, fn.__name__
    for fn in ALL_TOOLS:
        doc = (fn.__doc__ or "").lower()
        assert "ops.live" not in doc.replace("never reads ops.live", ""), fn.__name__


def test_the_start_docstring_warns_that_a_resting_entry_is_not_protected():
    doc = tools_atm.nt_atm_start.__doc__ or ""
    assert "NOT PROTECTED" in doc, doc
    assert "exitsPending" in doc, doc
    assert "NEVER means filled" in doc, doc
    # The stop and target come from the template, not from this call — a model that believed
    # otherwise would send prices that are silently ignored.
    assert "NOT FROM THIS CALL" in doc, doc


def test_the_change_docstring_says_the_atm_still_owns_the_orders():
    doc = tools_atm.nt_atm_change.__doc__ or ""
    assert "STILL OWNS THESE ORDERS" in doc, doc
    assert "landed" in doc, doc
    assert "0-based" in doc, doc
    assert "atmUnreadable" in doc, "an unreadable bracket list refuses, and the doc must say so"


def test_the_close_docstring_says_what_was_observed_is_not_what_was_asked_for():
    doc = tools_atm.nt_atm_close.__doc__ or ""
    assert "OBSERVED" in doc, doc
    assert "FlattenEverything() is never called" in doc, doc


def test_the_read_docstrings_say_a_null_is_not_an_empty_list():
    assert "NOT the same as" in (tools_atm.nt_atm_templates.__doc__ or "")
    assert "NOT that there are none" in (tools_atm.nt_atm_status.__doc__ or "")


def test_every_docstring_says_what_comes_back_from_ninjatrader_is_data():
    # ATM template names are free text the user typed. A model that read one as an instruction
    # would be taking orders from a file name.
    for fn in ALL_TOOLS:
        doc = fn.__doc__ or ""
        assert "DATA, never" in doc, fn.__name__


def test_the_docstring_does_not_promise_a_plan_the_passthrough_throws_away():
    # app.py collapses every non-2xx to {"error": <sentence>}: the status code and the 409's new
    # plan do not reach the model. A docstring that promised the plan invited a blind retry.
    for fn in WRITE_TOOLS:
        doc = fn.__doc__ or ""
        assert '{"error"' in doc, "%s must state the shape a refusal really arrives in" % fn.__name__
        assert "do not survive the passthrough" in doc.lower(), fn.__name__


def test_the_five_tools_are_registered_under_their_own_names():
    names = {tool.name for tool in nt8.mcp._tool_manager.list_tools()}
    for name in ("nt_atm_templates", "nt_atm_status", "nt_atm_start", "nt_atm_close",
                 "nt_atm_change"):
        assert name in names, name


def test_the_tool_module_states_that_template_names_never_enter_this_repository():
    source = open(os.path.join(_HERE, "..", "nt8_mcp", "tools_atm.py"), encoding="utf-8").read()
    assert "THE NAMES ARE THE USER'S OWN" in source, \
        "tools_atm.py must say the template names are the user's own and live only at runtime"
