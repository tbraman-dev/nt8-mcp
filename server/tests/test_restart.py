"""The restart CLI (nt8_mcp.restart) — its LOGIC only; nothing here starts or stops a process.

Three refusals are the whole point of the module and each one is pinned:
  * neither --task nor --exe -> refuse BEFORE stopping anything;
  * an open position (or one that could not be read) -> refuse, unless explicitly forced;
  * tasklist read three-valued, so "could not be read" never passes as "not running".
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from nt8_mcp import restart as r  # noqa: E402


def account(name="Sim101", qty=0, orders=None):
    return {"name": name, "cash": 1000.0, "realized": 0.0, "unrealized": 0.0,
            "positions": ([{"instrument": "ES 12-26", "qty": qty, "avg": 5800.0,
                            "unrealized": 0.0, "hasSeenMarketData": True}] if qty else []),
            "orders": list(orders or [])}


def working_order(instrument="ES 12-26", action="Sell", type_="StopLimit"):
    return {"id": "o1", "instrument": instrument, "action": action, "type": type_,
            "qty": 1, "price": 5800.0, "state": "Working"}


# ── no defaults ─────────────────────────────────────────────────────────────

def test_there_is_no_default_task_and_no_default_exe():
    # A task name is site-specific and a bare exe can start a process that stops at a login
    # screen — a running-check then reports healthy while every headless step fails.
    assert r.DEFAULT_TASK == "" and r.DEFAULT_EXE == ""


def test_choose_start_refuses_when_neither_flag_is_given():
    kind, detail = r.choose_start("", "")
    assert kind == "none", (kind, detail)
    assert "no safe default" in detail


def test_choose_start_prefers_the_task():
    assert r.choose_start("NT8 interactive", "C:\\nt\\NinjaTrader.exe")[0] == "task"


def test_choose_start_falls_back_to_the_exe():
    assert r.choose_start("", "C:\\nt\\NinjaTrader.exe")[0] == "exe"


def test_restart_refuses_before_stopping_anything():
    stopped = []
    report = r.restart(task="", exe="", _stop=lambda: (stopped.append(1), (True, "x"))[1],
                       _positions=lambda: (False, "flat"))
    assert report["ok"] is False and report["step"] == "choose", report
    assert stopped == [], "the refusal must come BEFORE anything is stopped"


# ── the open-position guard ─────────────────────────────────────────────────

def test_a_flat_desk_reads_not_exposed():
    exposed, detail = r.open_exposure(_get=lambda: [account(qty=0), account("Sim102", 0)])
    assert exposed is False, (exposed, detail)


def test_an_open_position_reads_exposed():
    exposed, detail = r.open_exposure(_get=lambda: [account(qty=-2)])
    assert exposed is True
    assert "ES 12-26" in detail and "-2" in detail, detail


def test_a_working_order_on_a_flat_account_reads_exposed():
    # A flat account with a resting bracket is NOT safe to taskkill under: the order stays live at
    # the broker, fills while NinjaTrader is down, and the position has nothing managing it.
    exposed, detail = r.open_exposure(_get=lambda: [account(qty=0, orders=[working_order()])])
    assert exposed is True, (exposed, detail)
    assert "working" in detail and "ES 12-26" in detail, detail


def test_an_unreadable_account_list_is_neither_exposed_nor_clear():
    # Three-valued on purpose: "we could not look" must not pass as "there is nothing there".
    for bad in ({"error": "NT8Bridge not reachable on :7891"}, None, "nope"):
        exposed, detail = r.open_exposure(_get=lambda b=bad: b)
        assert exposed is None, (bad, exposed, detail)


def test_a_position_row_that_could_not_be_read_is_unknown_not_flat():
    doc = [{"name": "Sim101", "positions": [{"error": "read failed"}], "orders": []}]
    exposed, detail = r.open_exposure(_get=lambda: doc)
    assert exposed is None, (exposed, detail)


def test_an_order_row_that_could_not_be_read_is_unknown_not_flat():
    doc = [{"name": "Sim101", "positions": [], "orders": [{"error": "read failed"}]}]
    exposed, detail = r.open_exposure(_get=lambda: doc)
    assert exposed is None, (exposed, detail)


def test_restart_refuses_with_an_open_position_and_stops_nothing():
    stopped = []
    report = r.restart(task="NT8", _positions=lambda: (True, "open position(s): Sim101 ES 12-26 +2"),
                       _stop=lambda: (stopped.append(1), (True, "x"))[1])
    assert report["ok"] is False and report["step"] == "positions", report
    assert "leaves it live with nothing managing it" in report["refused"], report
    assert stopped == [], "a restart with a position open must stop nothing"


def test_restart_refuses_when_the_positions_could_not_be_read():
    report = r.restart(task="NT8", _positions=lambda: (None, "could not read /account: unreachable"),
                       _stop=lambda: (True, "x"))
    assert report["ok"] is False and report["step"] == "positions", report
    assert report["exposed"] is None, report


def test_force_with_open_position_is_the_only_way_past():
    report = r.restart(task="NT8", force_with_open_position=True,
                       _positions=lambda: (True, "open position(s): Sim101 ES 12-26 +2"),
                       _stop=lambda: (True, "closed gracefully"),
                       _start=lambda: (True, "task fired"), _wait=lambda state, t: True)
    assert report["ok"] is True and report["step"] == "done", report


# ── stop -> confirm stopped -> start -> confirm started ─────────────────────

def test_a_stop_that_did_not_take_never_reaches_the_start_step():
    started = []
    report = r.restart(task="NT8", _positions=lambda: (False, "flat"),
                       _stop=lambda: (False, "still running after graceful close and terminate"),
                       _start=lambda: (started.append(1), (True, "x"))[1])
    assert report["ok"] is False and report["step"] == "stop", report
    assert started == [], "nothing may be started before the old process is confirmed gone"


def test_a_start_that_did_not_take_is_reported_as_a_failure():
    report = r.restart(task="NT8", _positions=lambda: (False, "flat"),
                       _stop=lambda: (True, "closed gracefully"),
                       _start=lambda: (False, "ERROR: the task does not exist"),
                       _wait=lambda state, t: True)
    assert report["ok"] is False and report["step"] == "start", report


def test_a_process_that_never_comes_back_is_not_ok():
    report = r.restart(task="NT8", _positions=lambda: (False, "flat"),
                       _stop=lambda: (True, "closed gracefully"),
                       _start=lambda: (True, "task fired"), _wait=lambda state, t: False)
    assert report["ok"] is False and report["step"] == "waitForStart", report


def test_a_completed_restart_says_the_process_is_up_not_that_the_platform_is_ready():
    report = r.restart(task="NT8", _positions=lambda: (False, "flat"),
                       _stop=lambda: (True, "closed gracefully"),
                       _start=lambda: (True, "task fired"), _wait=lambda state, t: True)
    assert report["ok"] is True, report
    assert "login or workspace" in report["note"], report


def _with_listed(value, fn):
    real = r.process_listed
    try:
        r.process_listed = lambda: (value, "canned")
        return fn()
    finally:
        r.process_listed = real


def test_stop_escalates_to_force_only_after_the_graceful_close_failed():
    # A graceful close can BLOCK on NinjaTrader's own save-workspace prompt, which nothing here
    # can answer, so the force step is not an option.
    import subprocess
    calls = []
    real_run = subprocess.run
    try:
        subprocess.run = lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        waits = iter([False, True])          # graceful window expires, then the force takes
        stopped, detail = _with_listed(True, lambda: r.stop(_wait=lambda state, t: next(waits)))
    finally:
        subprocess.run = real_run
    assert stopped is True and "terminated" in detail, detail
    assert calls == [["taskkill", "/IM", "NinjaTrader.exe"],
                     ["taskkill", "/F", "/IM", "NinjaTrader.exe"]], calls


def test_stop_does_nothing_when_it_is_not_running():
    assert _with_listed(False, r.stop) == (True, "not running")


def test_an_unreadable_process_list_refuses_the_stop_instead_of_reading_as_not_running():
    # The collapse this module exists to refuse. "not running" here dispatched no taskkill and let
    # restart() start a SECOND NinjaTrader against the same user data dir.
    import subprocess
    calls = []
    real_run = subprocess.run
    try:
        subprocess.run = lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        stopped, detail = _with_listed(None, r.stop)
    finally:
        subprocess.run = real_run
    assert stopped is False, (stopped, detail)
    assert "never measured" in detail, detail
    assert calls == [], "nothing may be killed on a verdict that was never measured"


def test_a_restart_refuses_at_the_stop_step_when_the_process_list_is_unreadable():
    started = []
    report = _with_listed(None, lambda: r.restart(
        task="NT8", _positions=lambda: (False, "flat"),
        _start=lambda: (started.append(1), (True, "x"))[1]))
    assert report["ok"] is False and report["step"] == "stop", report
    assert started == [], "nothing may be started when the stop was never measured"


def test_a_wait_never_succeeds_on_a_list_that_could_not_be_read():
    # An unreadable tasklist during the graceful window must not yield "closed gracefully".
    assert _with_listed(None, lambda: r.wait_for(False, 2.0, _sleep=lambda s: None,
                                                 _now=iter([0.0, 1.0, 3.0]).__next__)) is False


# ── tasklist is read three-valued ───────────────────────────────────────────

def _with_run(fake, fn):
    import subprocess
    real = subprocess.run
    try:
        subprocess.run = fake
        return fn()
    finally:
        subprocess.run = real


def _result(returncode=0, stdout="", stderr=""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


def test_a_listed_process_reads_true():
    out = "NinjaTrader.exe                8124 Console                    1    512,000 K"
    listed, detail = _with_run(lambda c, **k: _result(stdout=out), r.process_listed)
    assert listed is True and "8124" in detail, (listed, detail)


def test_an_unlisted_process_reads_false():
    out = "INFO: No tasks are running which match the specified criteria."
    listed, detail = _with_run(lambda c, **k: _result(stdout=out), r.process_listed)
    assert listed is False, (listed, detail)


def test_a_list_that_could_not_be_read_reads_none_not_false():
    listed, detail = _with_run(lambda c, **k: _result(returncode=1, stderr="ERROR: Invalid argument"),
                               r.process_listed)
    assert listed is None, (listed, detail)

    def boom(cmd, **kw):
        raise FileNotFoundError("tasklist")
    listed, detail = _with_run(boom, r.process_listed)
    assert listed is None and "could not be run" in detail, (listed, detail)


def test_nothing_in_this_module_collapses_the_unknown_read_to_a_bool():
    # There is deliberately no is_running() helper any more: every caller that had one collapsed
    # None to False, which is the verdict this module refuses to pass.
    assert not hasattr(r, "is_running"), "a bool-valued process check invites the collapse back"
