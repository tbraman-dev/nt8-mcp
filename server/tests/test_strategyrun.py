"""The three strategy-run tools (nt8_mcp.tools_strategyrun) against a canned AddOn that enforces
the /strategy/* gate chain.

The gates themselves live in C# (addon/NT8BridgeStrategyRun.cs, on top of the one door in
addon/NT8BridgeOrders.cs) and are verified against a running NinjaTrader. What is pinned HERE is
the contract those gates publish, and the things a client can get catastrophically wrong on a verb
that hands a Simulator account over to a strategy that places its own orders:

  * calling an unarmed module and reading the 403 as "nothing to do",
  * inventing, reusing or REPLAYING a confirm — a replayed start would run the same strategy twice
    on one account, and a strategy plan describes something that does not exist yet, so nothing but
    the consumed-token set stops it,
  * sending a confirm issued for an order verb (or for nt_flatten),
  * sending an argument as null instead of omitting it — `confirm` being ABSENT is what makes a
    call a dry run,
  * reading `ok` as "it is trading" when only state == "Realtime" is,
  * reading nt_strategy_stop as a flatten: it stops the strategy MANAGING the position and leaves
    the position and the working orders on the account,
  * assuming a strategy this server never started can be stopped through it.

`Runner` below is a spec of the documented contract (docs/api/strategyrun.md), not a second
implementation of the AddOn: it hands out an OPAQUE confirm string, so a client that computed its
own would fail.

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
from nt8_mcp import server as nt8, tools_strategyrun  # noqa: E402

UNARMED = "orders module not armed"
CAPS_TEXT = "CAPS qty=10/default working=20/default rate=60/default"
PERIOD = {"type": "Minute", "value": 5}

ALL_TOOLS = (tools_strategyrun.nt_strategy_start, tools_strategyrun.nt_strategy_stop,
             tools_strategyrun.nt_strategy_runs)


class Runner:
    """The documented /strategy/* behaviour, as a scriptable fake.

    armed=False    -> 403 on all three paths, whatever else is true, body parsed or not
    can_start=False-> 501 on start and stop: the Control Center's add/enable/disable path is not on
                      this NinjaTrader build, so nothing is started rather than started invisibly
    any_live=True  -> 409 on start and stop; /strategy/running still answers and reports anyLive
    sim=False      -> 403. There is no second file that widens this.
    state          -> what the instance reports after the enable settles. "Realtime" is running;
                      "Historical" is a strategy still loading bars, which is NOT a failure.
    position       -> ("Long", 2) or None: what a stop LEAVES BEHIND. Stop never flattens.
    moved_to       -> the account the INSTANCE reads back as. Not None means the strategy assigned
                      its own StrategyBase.Account, so the start is refused 502 accountMoved after
                      the read-back and the row reports both names.
    stop_hangs     -> the first confirmed stop stays in flight, so a retry for the same id is
                      refused 409 stopInFlight instead of dispatching a second disable + remove.
    """

    # The readable half of a confirm string always starts with the VERB, so an order token can
    # never authorise a strategy start.
    START_PLAN = ("strategy.start|ACCOUNT=Sim101|STRATEGY=SampleMACrossOver|INSTRUMENT=ES 12-26"
                  "|PERIOD=5 Minute|DAYSTOLOAD=default|INPUTS=Fast=10 Slow=25|" + CAPS_TEXT)
    STOP_PLAN = ("strategy.stop|ACCOUNT=Sim101|ID=s1|STRATEGY=SampleMACrossOver"
                 "|INSTRUMENT=ES 12-26|PERIOD=5 Minute|STATE=Realtime|POSITION=Long 2"
                 "|WORKING=o7|NOFLATTEN|" + CAPS_TEXT)
    MAC = " #a1b2c3d4e5f60718"

    INPUTS = {"Fast": 10, "Slow": 25}

    def __init__(self, armed=True, can_start=True, sim=True, any_live=False, window=30.0,
                 state="Realtime", position=("Long", 2), working=("o7",), runs=None,
                 moved_to=None, stop_hangs=False):
        self.armed, self.can_start, self.sim, self.any_live = armed, can_start, sim, any_live
        self.window, self.state, self.position, self.working = window, state, position, list(working)
        self.moved_to, self.stop_hangs = moved_to, stop_hangs
        self.stopping: set[str] = set()
        self.runs = dict(runs if runs is not None else {"s1": "Sim101"})
        self.posts: list[dict] = []
        self.paths: list[str] = []
        self.used: set[tuple[str, float]] = set()
        self.started = 0        # enables that reached NinjaTrader
        self.stopped = 0
        self.flattens = 0       # must stay 0 forever: /strategy/stop never flattens

    def confirm_for(self, verb):
        return {"start": self.START_PLAN, "stop": self.STOP_PLAN}[verb] + self.MAC

    # -- the gate chain ------------------------------------------------------

    def __call__(self, req):
        verb = req.path.rsplit("/", 1)[-1]
        self.paths.append(req.path)

        # Gate 1 first, before the body is parsed: an unarmed call never reaches the account layer.
        if not self.armed:
            return 403, {"error": UNARMED}
        if verb == "running":
            return 200, self._running()

        body = json.loads(req.body) if req.body else {}
        self.posts.append(body)

        if self.any_live:
            return 409, {"error": "orders strategy.%s refused: a live order-routing connection is "
                                  "up (see /health.connections)" % verb, "anyLive": True}
        if not (body.get("account") or "").strip():
            return 400, {"error": "account is required — one name from GET /orders/status; "
                                  "there is no all-accounts form"}
        if not self.sim:
            return 403, {"error": "account '%s' is not a Provider.Simulator or Provider.Playback "
                                  "account — this module accepts no other provider, has no live "
                                  "switch, and never reads ops.live" % body["account"]}
        if not self.can_start:
            return 501, {"error": "this NinjaTrader build does not expose "
                                  "NinjaTrader.Gui.NinjaScript.StrategiesGrid.StrategyAdd"
                                  "(StrategyBase) — the Control Center's own add / enable / "
                                  "disable path. Nothing here starts a strategy without it"}

        refusal = getattr(self, "_check_" + verb)(body)
        if refusal is not None:
            return refusal

        if "confirm" not in body:
            return 200, {"dryRun": True, "plan": self._plan(verb, body),
                         "confirm": self.confirm_for(verb), "issuedAt": time.time(),
                         "expiresInSec": self.window}

        token = self._check_token(body, verb)
        if token is not None:
            return token
        # A _do_ may answer with its own status: the account read-back happens AFTER the token has
        # been spent, because it is a read of the instance the confirmed call just built.
        result = getattr(self, "_do_" + verb)(body)
        return result if isinstance(result, tuple) else (200, result)

    # -- per-verb validation -------------------------------------------------

    def _check_start(self, body):
        if not (body.get("strategy") or "").strip():
            return 400, {"error": "strategy is required — a type name from GET /strategies"}
        if body["strategy"] != "SampleMACrossOver":
            return 400, {"error": "no strategy '%s' in this AddOn's assembly — call GET /strategies "
                                  "for the names" % body["strategy"]}
        if not (body.get("instrument") or "").strip():
            return 400, {"error": 'instrument is required, e.g. "ES 12-26"'}
        if body.get("barsPeriod") is None:
            return 400, {"error": 'barsPeriod is required, e.g. {"type":"Minute","value":5}'}
        unknown = [k for k in (body.get("inputs") or {}) if k not in self.INPUTS]
        if unknown:
            return 400, {"error": "'SampleMACrossOver' has no [NinjaScriptProperty] input named "
                                  "'%s' — it takes Fast, Slow" % unknown[0]}
        return None

    def _check_stop(self, body):
        run_id = (body.get("id") or "").strip()
        if not run_id:
            return 400, {"error": "id is required — an id from GET /strategy/running"}
        if run_id not in self.runs:
            return 404, {"error": "no strategy run '%s' — this module can only stop instances it "
                                  "started itself, and a NinjaScript recompile empties that list"
                                  % run_id}
        if self.runs[run_id] != body["account"]:
            return 409, {"error": "run '%s' runs on '%s', not on '%s' — name the account the run "
                                  "was started on" % (run_id, self.runs[run_id], body["account"])}
        # The per-run hold, taken BEFORE the token is spent so the loser keeps its confirm.
        if "confirm" in body and run_id in self.stopping:
            return 409, {"error": "another stop for run '%s' is already in flight; wait for it, "
                                  "then read GET /strategy/running — it will show where the "
                                  "strategy really is" % run_id}
        return None

    # -- the token -----------------------------------------------------------

    def _check_token(self, body, verb):
        if "issuedAt" not in body:
            return 400, {"error": "confirm was given without issuedAt — both come from the dry run"}
        age = time.time() - float(body["issuedAt"])
        if age > self.window:
            return 409, {"error": "issuedAt is %.1f s old; the confirm window is %.0f s — run the "
                                  "dry-run again and use the token it returns" % (age, self.window)}
        if body["confirm"] != self.confirm_for(verb):
            return 409, {"error": "confirm does not match the plan as it is NOW — the strategy, the "
                                  "account or the caps moved since the dry run, or the token was "
                                  "issued for a different call"}
        spent = (body["confirm"], float(body["issuedAt"]))
        if spent in self.used:
            return 409, {"error": "this confirm was already used — a confirm authorises ONE call, "
                                  "not every call inside the window"}
        self.used.add(spent)
        return None

    # -- the plans -----------------------------------------------------------

    def _plan(self, verb, body):
        if verb == "start":
            merged = dict(self.INPUTS)
            merged.update(body.get("inputs") or {})
            return {"account": body["account"], "strategy": body["strategy"],
                    "instrument": body["instrument"], "barsPeriod": body["barsPeriod"],
                    "daysToLoad": body.get("daysToLoad"), "inputs": merged,
                    "placesItsOwnOrders": True,
                    "appearsIn": "the Control Center Strategies grid"}
        side, qty = self.position if self.position else (None, 0)
        return {"account": body["account"], "accountObserved": self.moved_to or body["account"],
                "id": body["id"], "strategy": "SampleMACrossOver",
                "instrument": "ES 12-26", "state": self.state,
                "positionLeftBehind": {"side": side, "quantity": qty, "averagePrice": 5000.0},
                "workingOrdersLeftBehind": list(self.working), "flattens": False}

    # -- the acts ------------------------------------------------------------

    def _do_start(self, body):
        # The read-back, after the add and after the enable settles. An instance that carries
        # another account is DISABLED and refused — never reported as running on the account that
        # was asked for, which is the only field that proves where its orders go.
        if self.moved_to is not None:
            return 502, {"error": "the strategy's own Account reads back as '%s', not the requested "
                                  "'%s' — the instance moved itself to another account, so its "
                                  "orders would NOT route to the account this call gated — it was "
                                  "NOT enabled. A disable was dispatched to the Control Center"
                                  % (self.moved_to, body["account"])}
        self.started += 1
        self.runs["s%d" % (len(self.runs) + 1)] = body["account"]
        return {"ok": self.state not in (None, "Terminated", "Finalized"), "dryRun": False,
                "id": "s%d" % len(self.runs), "account": body["account"],
                "accountObserved": body["account"],
                "strategy": body["strategy"], "instrument": body["instrument"],
                "barsPeriod": body["barsPeriod"], "inputs": self._plan("start", body)["inputs"],
                "state": self.state, "running": self.state == "Realtime",
                "inStrategiesGrid": True, "plan": self._plan("start", body),
                "note": "`ok` means the row is in NinjaTrader's Strategies grid and the enable was "
                        "dispatched — it NEVER means the strategy is trading. Believe `state`."}

    def _do_stop(self, body):
        self.stopped += 1
        if self.stop_hangs:
            self.stopping.add(body["id"])
        side, qty = self.position if self.position else (None, 0)
        return {"ok": True, "dryRun": False, "id": body["id"], "account": body["account"],
                "accountObserved": self.moved_to or body["account"],
                "strategy": "SampleMACrossOver", "instrument": "ES 12-26",
                "stateBefore": self.state, "state": "Terminated", "removedFromGrid": True,
                "flattened": False,
                "positionLeftBehind": {"side": side, "quantity": qty, "averagePrice": 5000.0},
                "workingOrdersLeftBehind": list(self.working),
                "plan": self._plan("stop", body),
                "note": "THIS DID NOT FLATTEN ANYTHING."}

    # -- the read ------------------------------------------------------------

    def _running(self):
        side, qty = self.position if self.position else (None, 0)
        return {"runs": [{"id": run_id, "strategy": "SampleMACrossOver", "account": account,
                          "accountObserved": self.moved_to or account,
                          "accountMatches": self.moved_to in (None, account),
                          "accountError": None,
                          "instrument": "ES 12-26", "barsPeriod": "5 Minute",
                          "inputs": dict(self.INPUTS), "state": self.state,
                          "running": self.state == "Realtime", "inStrategiesGrid": True,
                          "position": {"side": side, "quantity": qty, "averagePrice": 5000.0},
                          "workingOrders": [{"orderId": o, "owner": "strategy SampleMACrossOver"}
                                            for o in self.working],
                          "realizedPnL": 112.5, "realtimeTrades": 3}
                         for run_id, account in sorted(self.runs.items())],
                "count": len(self.runs), "canStart": self.can_start,
                "anyLive": self.any_live}


def _addon(runner):
    fake = FakeAddon()
    for path in ("/strategy/start", "/strategy/stop"):
        fake.register(path, "POST", runner)
    fake.register("/strategy/running", "GET", runner)
    return fake


def _start(**kw):
    return tools_strategyrun.nt_strategy_start(
        strategy=kw.pop("strategy", "SampleMACrossOver"), account=kw.pop("account", "Sim101"),
        instrument=kw.pop("instrument", "ES 12-26"), bars_period=kw.pop("bars_period", PERIOD), **kw)


# ---------------------------------------------------------------------------
# gate 1: the arming file
# ---------------------------------------------------------------------------

def test_every_path_is_403_while_the_module_is_disarmed():
    runner = Runner(armed=False)
    with _addon(runner):
        assert _start()["error"] == UNARMED
        assert tools_strategyrun.nt_strategy_stop(id="s1", account="Sim101")["error"] == UNARMED
        assert tools_strategyrun.nt_strategy_runs()["error"] == UNARMED
    # The 403 lands before the body is parsed, so nothing about the account was consulted.
    assert runner.posts == []
    assert runner.started == 0


def test_the_disarmed_refusal_is_the_same_sentence_the_order_tools_use():
    # One arming file, one sentence: a strategy on a Simulator account places real simulated
    # orders, so it is armed by orders.enabled and not by a second file of its own.
    with _addon(Runner(armed=False)):
        assert tools_strategyrun.nt_strategy_runs()["error"] == "orders module not armed"


# ---------------------------------------------------------------------------
# gate 2 / gate 3: live routing, and the provider
# ---------------------------------------------------------------------------

def test_a_live_routing_connection_refuses_start_and_stop_but_not_the_read():
    runner = Runner(any_live=True)
    with _addon(runner):
        assert "live order-routing connection is up" in _start()["error"]
        assert "live order-routing connection is up" in tools_strategyrun.nt_strategy_stop(
            id="s1", account="Sim101")["error"]
        read = tools_strategyrun.nt_strategy_runs()
        assert read["anyLive"] is True
        assert read["count"] == 1
    assert runner.started == 0


def test_a_non_simulator_account_is_refused_and_there_is_no_file_that_widens_it():
    runner = Runner(sim=False)
    with _addon(runner):
        err = _start(account="MyBrokerAccount")["error"]
    assert "Provider.Simulator or Provider.Playback" in err
    assert "never reads ops.live" in err
    assert runner.started == 0


# ---------------------------------------------------------------------------
# the grid path: nothing starts a strategy it could not show or could not stop
# ---------------------------------------------------------------------------

def test_a_build_without_the_control_center_path_starts_nothing():
    runner = Runner(can_start=False)
    with _addon(runner):
        err = _start()["error"]
        assert "Nothing here starts a strategy without it" in err
        assert "StrategiesGrid" in err
        # And it refuses the stop as well: a strategy that could be started and not stopped is the
        # one thing this module must never create.
        assert "StrategiesGrid" in tools_strategyrun.nt_strategy_stop(
            id="s1", account="Sim101")["error"]
    assert runner.started == 0


def test_the_read_reports_that_starting_is_unavailable():
    with _addon(Runner(can_start=False)):
        assert tools_strategyrun.nt_strategy_runs()["canStart"] is False


# ---------------------------------------------------------------------------
# the two steps
# ---------------------------------------------------------------------------

def test_a_start_without_confirm_is_a_dry_run_and_sends_nothing():
    runner = Runner()
    with _addon(runner):
        dry = _start()
    assert dry["dryRun"] is True
    assert dry["confirm"] and dry["issuedAt"]
    assert runner.started == 0
    # The plan names what is being approved, inputs included.
    assert dry["plan"]["strategy"] == "SampleMACrossOver"
    assert dry["plan"]["inputs"] == {"Fast": 10, "Slow": 25}
    assert dry["plan"]["placesItsOwnOrders"] is True


def test_the_plan_shows_every_input_not_only_the_ones_that_were_sent():
    with _addon(Runner()):
        dry = _start(inputs={"Fast": 3})
    assert dry["plan"]["inputs"] == {"Fast": 3, "Slow": 25}, dry["plan"]["inputs"]


def test_the_confirm_from_the_dry_run_starts_it_and_returns_an_id():
    runner = Runner()
    with _addon(runner):
        dry = _start()
        done = _start(confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert runner.started == 1
    assert done["ok"] is True and done["dryRun"] is False
    assert done["id"] == "s2"
    assert done["inStrategiesGrid"] is True


def test_replaying_a_confirm_cannot_start_the_strategy_twice():
    # A start plan describes something that does not exist yet, so it reads the same before and
    # after the act: only the consumed-token set stops a retry after a timeout from running the
    # same strategy twice on one account.
    runner = Runner()
    with _addon(runner):
        dry = _start()
        first = _start(confirm=dry["confirm"], issued_at=dry["issuedAt"])
        again = _start(confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert first["ok"] is True
    assert "already used" in again["error"]
    assert runner.started == 1


def test_an_invented_confirm_is_refused():
    runner = Runner()
    with _addon(runner):
        err = _start(confirm=Runner.START_PLAN, issued_at=time.time())["error"]
    assert "does not match the plan as it is NOW" in err
    assert runner.started == 0


def test_a_confirm_issued_for_another_verb_is_refused():
    runner = Runner()
    with _addon(runner):
        dry = _start()
        err = tools_strategyrun.nt_strategy_stop(
            id="s1", account="Sim101", confirm=dry["confirm"], issued_at=dry["issuedAt"])["error"]
    assert "does not match the plan as it is NOW" in err
    assert runner.stopped == 0


def test_a_stale_issued_at_is_refused():
    runner = Runner()
    with _addon(runner):
        dry = _start()
        err = _start(confirm=dry["confirm"], issued_at=dry["issuedAt"] - 120)["error"]
    assert "confirm window" in err
    assert runner.started == 0


def test_a_confirm_without_issued_at_is_refused():
    with _addon(Runner()) as fake:
        dry = _start()
        err = _start(confirm=dry["confirm"])["error"]
        assert "both come from the dry run" in err
        assert fake  # the call did reach the AddOn


def test_absent_arguments_are_omitted_not_sent_as_null():
    # `confirm` being ABSENT is what makes a call a dry run. A tool that sent confirm=None would
    # make every dry run look confirmed to an AddOn that read the key rather than its type.
    runner = Runner()
    with _addon(runner):
        _start()
        tools_strategyrun.nt_strategy_stop(id="s1", account="Sim101")
    for body in runner.posts:
        assert "confirm" not in body, body
        assert "issuedAt" not in body, body
    assert "inputs" not in runner.posts[0], runner.posts[0]
    assert "daysToLoad" not in runner.posts[0], runner.posts[0]


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_an_unknown_strategy_name_is_refused_with_the_real_ones():
    with _addon(Runner()):
        err = _start(strategy="NoSuchStrategy")["error"]
    assert "no strategy 'NoSuchStrategy'" in err
    assert "GET /strategies" in err


def test_an_unknown_input_name_is_refused_rather_than_silently_dropped():
    runner = Runner()
    with _addon(runner):
        err = _start(inputs={"Fastt": 10})["error"]
    assert "has no [NinjaScriptProperty] input named 'Fastt'" in err
    assert "Fast, Slow" in err
    assert runner.started == 0


def test_the_bars_period_is_required():
    with _addon(Runner()):
        err = tools_strategyrun.nt_strategy_start(
            strategy="SampleMACrossOver", account="Sim101", instrument="ES 12-26",
            bars_period=None)["error"]
    assert "barsPeriod is required" in err


# ---------------------------------------------------------------------------
# ok is not "trading"
# ---------------------------------------------------------------------------

def test_a_strategy_still_loading_bars_is_ok_but_not_running():
    # Enabling is asynchronous: Configure -> DataLoaded -> Historical -> Realtime. A start that
    # answers while the strategy is still "Historical" has NOT failed, and a client that read `ok`
    # as "trading" would act on a strategy that has not taken a bar yet.
    runner = Runner(state="Historical")
    with _addon(runner):
        dry = _start()
        done = _start(confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert done["ok"] is True
    assert done["running"] is False
    assert done["state"] == "Historical"


def test_an_unreadable_state_is_null_and_not_ok():
    runner = Runner(state=None)
    with _addon(runner):
        dry = _start()
        done = _start(confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert done["state"] is None
    assert done["ok"] is False
    # And the strategy may well be live: the id is still handed back so it can be stopped.
    assert done["id"]


# ---------------------------------------------------------------------------
# the account is read back off the instance, not echoed from the request
# ---------------------------------------------------------------------------

def test_a_strategy_that_moved_itself_to_another_account_is_refused_not_reported():
    # StrategyBase.Account is a plain settable property: a strategy that assigns it in
    # OnStateChange routes every order it places to an account this module never gated. The one
    # field that proves the one rule for a strategy's orders has to be READ, never echoed from the
    # request — a start that answered ok with the account that was ASKED FOR would be a lie.
    runner = Runner(moved_to="SomeOtherAccount")
    with _addon(runner):
        dry = _start()
        done = _start(confirm=dry["confirm"], issued_at=dry["issuedAt"])
    err = done["error"]
    assert "reads back as 'SomeOtherAccount'" in err, err
    assert "not the requested 'Sim101'" in err, err        # both accounts, so a reader can tell
    assert "disable was dispatched" in err, err
    assert runner.started == 0, "a moved instance must never be left enabled"


def test_a_normal_start_reports_the_account_it_read_back_beside_the_one_asked_for():
    runner = Runner()
    with _addon(runner):
        dry = _start()
        done = _start(confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert done["account"] == "Sim101"
    assert done["accountObserved"] == "Sim101", done
    assert runner.started == 1


def test_the_read_carries_the_observed_account_on_every_row():
    with _addon(Runner(moved_to="SomeOtherAccount")):
        row = tools_strategyrun.nt_strategy_runs()["runs"][0]
    assert row["account"] == "Sim101"                      # what it was started on
    assert row["accountObserved"] == "SomeOtherAccount"    # where its orders go now
    assert row["accountMatches"] is False, row


# ---------------------------------------------------------------------------
# stop: it does not flatten
# ---------------------------------------------------------------------------

def test_stop_reports_the_position_it_leaves_behind_and_never_flattens():
    runner = Runner(position=("Long", 2), working=("o7", "o8"))
    with _addon(runner):
        dry = tools_strategyrun.nt_strategy_stop(id="s1", account="Sim101")
        assert dry["plan"]["flattens"] is False
        assert dry["plan"]["positionLeftBehind"] == {"side": "Long", "quantity": 2,
                                                     "averagePrice": 5000.0}
        assert dry["plan"]["workingOrdersLeftBehind"] == ["o7", "o8"]
        done = tools_strategyrun.nt_strategy_stop(
            id="s1", account="Sim101", confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert done["flattened"] is False
    assert done["positionLeftBehind"]["quantity"] == 2
    assert done["workingOrdersLeftBehind"] == ["o7", "o8"]
    assert runner.flattens == 0
    assert runner.stopped == 1


def test_stop_refuses_a_run_this_server_did_not_start():
    runner = Runner()
    with _addon(runner):
        err = tools_strategyrun.nt_strategy_stop(id="s99", account="Sim101")["error"]
    assert "no strategy run 's99'" in err
    assert "recompile empties that list" in err
    assert runner.stopped == 0


def test_a_second_stop_for_the_same_run_is_refused_while_the_first_is_in_flight():
    # A stop dispatches StrategyDisable and then StrategiesGrid.StrategyRemove against ONE live
    # instance. A client that retried after a slow answer would send both a second time — the same
    # double-actor race /orders/change and /orders/cancel take a per-order hold against.
    runner = Runner(stop_hangs=True)
    with _addon(runner):
        first = tools_strategyrun.nt_strategy_stop(id="s1", account="Sim101")
        tools_strategyrun.nt_strategy_stop(id="s1", account="Sim101",
                                           confirm=first["confirm"], issued_at=first["issuedAt"])
        # the retry: a fresh dry run is allowed (it changes nothing), the confirm behind it is not
        retry = tools_strategyrun.nt_strategy_stop(id="s1", account="Sim101")
        again = tools_strategyrun.nt_strategy_stop(id="s1", account="Sim101",
                                                   confirm=retry["confirm"],
                                                   issued_at=retry["issuedAt"])
    assert "already in flight" in again["error"], again
    assert "GET /strategy/running" in again["error"], again
    assert runner.stopped == 1, "the second stop must not dispatch a second disable"


def test_the_stop_plan_names_the_account_the_instance_itself_carries():
    # The position and the working orders in the plan are read off the account the run was STARTED
    # on. When the instance moved, "flat, no working orders" is true of that account and says
    # nothing about the other one, so the plan has to name both.
    runner = Runner(moved_to="SomeOtherAccount")
    with _addon(runner):
        dry = tools_strategyrun.nt_strategy_stop(id="s1", account="Sim101")
    assert dry["plan"]["account"] == "Sim101"
    assert dry["plan"]["accountObserved"] == "SomeOtherAccount", dry["plan"]


def test_stop_refuses_an_id_that_belongs_to_another_account():
    runner = Runner(runs={"s1": "Playback101"})
    with _addon(runner):
        err = tools_strategyrun.nt_strategy_stop(id="s1", account="Sim101")["error"]
    assert "not on 'Sim101'" in err
    assert runner.stopped == 0


# ---------------------------------------------------------------------------
# the read
# ---------------------------------------------------------------------------

def test_the_read_lists_the_runs_with_state_position_orders_and_pnl():
    with _addon(Runner()):
        got = tools_strategyrun.nt_strategy_runs()
    row = got["runs"][0]
    assert row["id"] == "s1"
    assert row["state"] == "Realtime" and row["running"] is True
    assert row["position"]["side"] == "Long"
    assert row["workingOrders"][0]["owner"] == "strategy SampleMACrossOver"
    assert row["realizedPnL"] == 112.5
    assert row["realtimeTrades"] == 3


def test_the_read_is_a_get_and_the_acts_are_posts():
    runner = Runner()
    with _addon(runner):
        tools_strategyrun.nt_strategy_runs()
        _start()
        tools_strategyrun.nt_strategy_stop(id="s1", account="Sim101")
    assert runner.paths == ["/strategy/running", "/strategy/start", "/strategy/stop"]


# ---------------------------------------------------------------------------
# what the tool descriptions promise
# ---------------------------------------------------------------------------

def test_the_shared_paragraph_is_spliced_in_before_the_tool_is_registered():
    # It is spliced by a decorator UNDER @mcp.tool, so the registered description carries it too.
    # Done after registration, the description a model actually reads would hold "{gates}".
    for tool in nt8.mcp._tool_manager.list_tools():
        if not tool.name.startswith("nt_strategy_"):
            continue
        assert "{gates}" not in (tool.description or ""), tool.name
    for name in ("nt_strategy_start", "nt_strategy_stop"):
        tool = next(t for t in nt8.mcp._tool_manager.list_tools() if t.name == name)
        assert "TWO STEPS" in (tool.description or ""), name


def test_the_tool_names_do_not_collide_with_the_existing_ones():
    names = [t.name for t in nt8.mcp._tool_manager.list_tools()]
    assert len(names) == len(set(names)), sorted(names)
    for name in ("nt_strategy_start", "nt_strategy_stop", "nt_strategy_runs"):
        assert name in names, name


def test_the_start_docstring_says_ok_is_not_trading_and_the_strategy_places_its_own_orders():
    doc = tools_strategyrun.nt_strategy_start.__doc__ or ""
    assert "PLACES ITS OWN ORDERS" in doc
    assert "NEVER means the strategy is trading" in doc
    assert '"Realtime"' in doc


def test_the_stop_docstring_says_it_does_not_flatten():
    doc = tools_strategyrun.nt_strategy_stop.__doc__ or ""
    assert "IT DOES NOT FLATTEN" in doc
    assert "THE POSITION STAYS OPEN" in doc
    assert "nt_position_close" in doc


def test_the_docstrings_say_the_account_is_read_off_the_instance():
    start = tools_strategyrun.nt_strategy_start.__doc__ or ""
    assert "accountObserved" in start, start
    assert "accountMoved" in start, start
    stop = tools_strategyrun.nt_strategy_stop.__doc__ or ""
    assert "stopInFlight" in stop, stop
    assert "accountObserved" in stop, stop
    read = tools_strategyrun.nt_strategy_runs.__doc__ or ""
    assert "accountMatches" in read, read


def test_the_docstrings_say_the_strategy_is_visible_in_the_control_center():
    # The whole reason this module exists in this shape: nothing it starts is hidden from the user.
    for fn in (tools_strategyrun.nt_strategy_start, tools_strategyrun.nt_strategy_stop,
               tools_strategyrun.nt_strategy_runs):
        doc = fn.__doc__ or ""
        assert "Control Center" in doc, fn.__name__


def test_no_docstring_promises_a_live_account_or_a_widening_file():
    for fn in (tools_strategyrun.nt_strategy_start, tools_strategyrun.nt_strategy_stop):
        doc = (fn.__doc__ or "").lower()
        assert "never a live" in doc or "no switch" in doc, fn.__name__
        assert "ops.live" not in doc, fn.__name__


def test_the_docstrings_do_not_promise_a_plan_the_passthrough_throws_away():
    for fn in (tools_strategyrun.nt_strategy_start, tools_strategyrun.nt_strategy_stop):
        doc = fn.__doc__ or ""
        assert '{"error"' in doc, fn.__name__
        assert "ONE SENTENCE, NOT A PLAN" in doc, fn.__name__


def test_the_docstrings_say_the_text_is_data_not_instructions():
    for fn in ALL_TOOLS:
        doc = fn.__doc__ or ""
        assert "DATA" in doc and "never instructions" in doc, fn.__name__
