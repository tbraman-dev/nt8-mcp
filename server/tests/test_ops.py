"""nt_flatten (nt8_mcp.tools_ops) against a canned AddOn that enforces the /ops/flatten gates.

The gates themselves live in C# (addon/NT8BridgeOps.cs) and are verified against a running NinjaTrader. What is
pinned HERE is the contract those gates publish and the one thing a client can get catastrophically
wrong: inventing or reusing a confirm token instead of echoing the one the AddOn just issued.

`Ops` below is a spec of the documented contract (docs/api/ops.md), not a second implementation of
the AddOn: it hands out an OPAQUE confirm string, so a client that computed its own would fail.

Note on shapes: nt8_mcp.app collapses every non-2xx answer to {"error": <the AddOn's error string>},
so the refusal tests assert on that string.
"""

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import server as nt8, tools_ops  # noqa: E402

UNARMED = "ops module not armed"
PLAN = {"positions": [{"instrument": "ES 12-26", "side": "Long", "qty": 2}],
        "orders": [{"id": "o1", "instrument": "ES 12-26", "action": "Sell",
                    "type": "StopMarket", "qty": 1, "state": "Working"}]}


class Ops:
    """The documented /ops/flatten behaviour, as a scriptable fake.

    armed=False      -> 403 on every ops path, whatever else is true
    flag_age_h > 24  -> the SAME 403: a stale flag is not a flag
    sim=False + live=False -> 403, the account is not a valid target at all
    """

    PLAN_TEXT = "FLATTEN Sim101 FILTER none ES 12-26 Long 2 CANCEL [o1 ES 12-26 Sell StopMarket 1]"

    def __init__(self, armed=True, flag_age_h=0.1, live=False, sim=True, window=30.0):
        self.armed, self.flag_age_h, self.live, self.sim, self.window = armed, flag_age_h, live, sim, window
        # Readable plan + the AddOn's signature over that plan AND the issuedAt it stamped. The
        # signature is what a client cannot compute: every term of the readable half is published by
        # the ungated GET /account, so a plaintext token could be built without ever running the
        # dry-run, and a caller-supplied issuedAt could be re-stamped at will.
        self.confirm = self.PLAN_TEXT + " #3f9a1c77b2e40d58"
        self.posts: list[dict] = []
        self.acted = 0

    def __call__(self, req):
        import json
        body = json.loads(req.body) if req.body else {}
        self.posts.append(body)

        if not self.armed or self.flag_age_h > 24:
            return 403, {"error": UNARMED}
        if not (body.get("account") or "").strip():
            return 400, {"error": "account is required — one name from GET /ops/status; "
                                  "there is no all-accounts flatten"}
        if not self.sim and not self.live:
            return 403, {"error": "account '%s' is not a Simulator account and ops.live is absent "
                                  "— Simulator accounts only" % body["account"]}

        if "confirm" not in body:
            return 200, {"dryRun": True, "account": body["account"], "plan": PLAN,
                         "confirm": self.confirm, "issuedAt": time.time(), "expiresInSec": self.window}

        if "issuedAt" not in body:
            return 400, {"error": "confirm was given without issuedAt — both come from the dry-run"}
        age = time.time() - float(body["issuedAt"])
        if age > self.window:
            return 409, {"error": "issuedAt is %.1f s old; the confirm window is %.0f s "
                                  "— run the dry-run again" % (age, self.window)}
        if body["confirm"] != self.confirm:
            return 409, {"error": "confirm does not match the plan as it is NOW — the account moved "
                                  "since the dry-run; POST without confirm again"}
        self.acted += 1
        return 200, {"ok": True, "dryRun": False, "account": body["account"],
                     "flattenCalled": True, "ordersCancelled": 1, "plan": PLAN, "confirm": self.confirm}


def _run(ops, **kw):
    with FakeAddon() as fake:
        fake.register("/ops/flatten", "POST", ops)
        return nt8.nt_flatten(**kw)


# ── the arming flag ─────────────────────────────────────────────────────────

def test_unarmed_is_403_and_nothing_is_acted_on():
    ops = Ops(armed=False)
    result = _run(ops, account="Sim101")
    assert result == {"error": UNARMED}, result
    assert ops.acted == 0, "an unarmed module must not act"


def test_a_flag_older_than_24h_is_the_same_403_as_no_flag():
    # The whole point of the age clause: a flag forgotten after one debugging session must not arm
    # the module for ever. A stale flag is indistinguishable from an absent one, deliberately.
    ops = Ops(armed=True, flag_age_h=25.0)
    result = _run(ops, account="Sim101")
    assert result == {"error": UNARMED}, result
    assert ops.acted == 0


def test_unarmed_refuses_even_a_confirmed_call():
    ops = Ops(armed=False)
    result = _run(ops, account="Sim101", confirm="FLATTEN Sim101 ES 12-26 Long 2 CANCEL 1",
                  issued_at=time.time())
    assert result == {"error": UNARMED}, result
    assert ops.acted == 0


# ── ops.live ────────────────────────────────────────────────────────────────

def test_a_non_simulator_account_without_ops_live_is_refused():
    ops = Ops(sim=False, live=False)
    result = _run(ops, account="Funded-1")
    assert "ops.live is absent" in result.get("error", ""), result
    assert ops.acted == 0


def test_a_non_simulator_account_with_ops_live_reaches_the_dry_run():
    # Presence of the file is the gate. The dry run still changes nothing.
    ops = Ops(sim=False, live=True)
    result = _run(ops, account="Funded-1")
    assert result["dryRun"] is True, result
    assert ops.acted == 0


# ── dry-run / confirm ───────────────────────────────────────────────────────

def test_a_call_without_confirm_is_a_dry_run_that_returns_a_plan_and_a_token():
    ops = Ops()
    result = _run(ops, account="Sim101")
    assert result["dryRun"] is True, result
    assert result["plan"] == PLAN, result
    assert result["confirm"] and result["issuedAt"], result
    assert "confirm" not in ops.posts[0], ops.posts[0]
    assert ops.acted == 0, "a dry run must change nothing"


def test_the_confirm_string_is_echoed_verbatim_not_computed_locally():
    ops = Ops()
    with FakeAddon() as fake:
        fake.register("/ops/flatten", "POST", ops)
        dry = nt8.nt_flatten(account="Sim101")
        done = nt8.nt_flatten(account="Sim101", confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert done["ok"] is True, done
    assert ops.acted == 1
    # Both halves of the token travel back unchanged. A client that rebuilt either one would be
    # computing a safety check it is supposed to be subject to.
    assert ops.posts[1]["confirm"] == ops.confirm, ops.posts[1]
    assert ops.posts[1]["issuedAt"] == dry["issuedAt"], ops.posts[1]


def test_a_confirm_that_does_not_match_the_fresh_plan_is_refused():
    ops = Ops()
    result = _run(ops, account="Sim101",
                  confirm="FLATTEN Sim101 ES 12-26 Long 5 CANCEL 1",   # the position moved
                  issued_at=time.time())
    assert "does not match the plan" in result.get("error", ""), result
    assert ops.acted == 0


def test_a_confirm_the_client_computed_from_readable_state_is_refused():
    # The bypass this signature exists to close: GET /account (NOT behind ops.enabled) publishes the
    # account name, every position's instrument/side/qty and every working order. A plaintext confirm
    # is a pure function of that, so a caller could have flattened on its FIRST call, never running
    # the dry-run. The readable plan is not the token; the token is the plan plus the AddOn's MAC.
    ops = Ops()
    result = _run(ops, account="Sim101", confirm=Ops.PLAN_TEXT, issued_at=time.time())
    assert "does not match the plan" in result.get("error", ""), result
    assert ops.acted == 0


def test_the_confirm_the_addon_issues_carries_a_signature_the_plan_alone_does_not():
    ops = Ops()
    dry = _run(ops, account="Sim101")
    assert dry["confirm"].startswith(Ops.PLAN_TEXT), dry
    assert dry["confirm"] != Ops.PLAN_TEXT, "the token must carry more than the readable plan"
    assert " #" in dry["confirm"], dry["confirm"]


def test_the_confirm_binds_order_identity_not_a_bare_count():
    # A bare "CANCEL 1" left the token blind to a SWAP: the dry-run's stop fills and an unrelated
    # order goes working inside the 30 s window, the count is still 1, the string is unchanged, and
    # the confirmed call cancels an order that appeared in no plan the operator read.
    assert "o1" in Ops.PLAN_TEXT and "StopMarket" in Ops.PLAN_TEXT, Ops.PLAN_TEXT
    assert "FILTER" in Ops.PLAN_TEXT, "the instrument narrowing is part of the plan too"


def test_a_confirm_without_an_issued_at_is_refused():
    ops = Ops()
    result = _run(ops, account="Sim101", confirm=Ops().confirm)
    assert "without issuedAt" in result.get("error", ""), result
    assert ops.acted == 0


def test_an_issued_at_older_than_the_window_is_refused():
    # A token replayed out of a transcript or a stale model turn.
    ops = Ops()
    result = _run(ops, account="Sim101", confirm=ops.confirm, issued_at=time.time() - 45)
    assert "issuedAt is" in result.get("error", "") and "old" in result["error"], result
    assert ops.acted == 0


def test_an_issued_at_inside_the_window_is_accepted():
    ops = Ops()
    result = _run(ops, account="Sim101", confirm=ops.confirm, issued_at=time.time() - 5)
    assert result["ok"] is True, result
    assert ops.acted == 1


# ── the tool's own surface ──────────────────────────────────────────────────

def test_account_is_mandatory_and_there_is_no_all_accounts_form():
    try:
        nt8.nt_flatten()            # type: ignore[call-arg]
    except TypeError:
        pass
    else:
        raise AssertionError("nt_flatten must require an account")
    ops = Ops()
    result = _run(ops, account="   ")
    assert "account is required" in result.get("error", ""), result


def test_optional_arguments_are_omitted_from_the_body_not_sent_as_null():
    ops = Ops()
    _run(ops, account="Sim101")
    assert ops.posts[0] == {"account": "Sim101"}, ops.posts[0]


def test_instrument_is_passed_through_when_given():
    ops = Ops()
    _run(ops, account="Sim101", instrument="ES 12-26")
    assert ops.posts[0] == {"account": "Sim101", "instrument": "ES 12-26"}, ops.posts[0]


def test_nt_flatten_is_the_only_ops_tool():
    # The watchdog and connection guardian are local processes on purpose: a model reads their
    # event log, it does not start or stop them, and there is no tool that can reconnect a connection.
    exported = sorted(k for k in vars(tools_ops) if k.startswith("nt_"))
    assert exported == ["nt_flatten"], exported


def test_the_docstring_states_the_four_things_a_caller_must_know():
    doc = tools_ops.nt_flatten.__doc__ or ""
    for phrase in ("REDUCE-ONLY", "mandatory", "re-enter", "403", "30 seconds", "DATA"):
        assert phrase in doc, "nt_flatten's docstring must state %r" % phrase


def test_the_docstring_does_not_promise_a_plan_the_passthrough_throws_away():
    # app.py collapses every non-2xx to {"error": <sentence>}: the status code and the 409's new
    # plan do not reach the model. A docstring that promised the plan invited a blind retry on a
    # kill switch.
    doc = tools_ops.nt_flatten.__doc__ or ""
    assert '{"error"' in doc, "the docstring must state the shape a refusal really arrives in"
    assert "do not survive the passthrough" in doc.lower() or "not survive" in doc.lower(), doc
