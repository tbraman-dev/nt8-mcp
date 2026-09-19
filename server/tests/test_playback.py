"""Playback tools (nt8_mcp.tools_playback) against the canned AddOn in fake_addon.py.

The verdict rules are the point: every one of them exists because a live run got a
success-shaped nothing out of a transport that was not ready.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import app  # noqa: E402
from nt8_mcp import server as nt8  # noqa: E402
from nt8_mcp import tools_playback  # noqa: E402


def _body(**over) -> dict:
    d = {
        "ok": True,
        "nowUtc": "2026-09-18T14:00:00Z",
        "transportResolved": True,
        "connection": {"status": "Connected", "connected": True, "resolved": True},
        "clockEstFirst": "2026-08-10T09:30:00",
        "clockEst": "2026-08-10T09:30:00",
        "clockBasis": "PlaybackAdapter.NowEst — US Eastern, NOT NT8-local and NOT UTC",
        "sampleMs": 1100,
        "movingSec": 0.0,
        "moving": False,
        "speed": 1,
        "maxSpeedValue": 2147483647,
        "fromEst": "2026-08-10T00:00:00",
        "toEst": "2026-08-10T23:59:59",
        "isSourceHistoricalData": False,
        "resolved": {"PlaybackAdapter": True, "NowEst": True},
        "instrument": "ES 12-26",
        "coverageScanned": True,
        "coverageTruncated": False,
        "coverageBudgetSec": 20,
        "coverage": [{"instrument": "ES 12-26", "files": 2, "readable": 2, "unreadable": 0,
                      "skipped": 0, "from": "2026-08-10T00:00:00", "to": "2026-08-10T23:59:59",
                      "days": []}],
        "note": "read only",
    }
    d.update(over)
    return d


def _call(body, **kw) -> dict:
    with FakeAddon() as fake:
        fake.json("/playback", body)
        return nt8.nt_playback(**kw)


def test_parked_with_data_is_ready():
    out = _call(_body())
    assert out["state"] == "parked" and out["ready"] is True
    assert out["speed"] == 1 and out["sampleMs"] == 1100      # AddOn fields pass through untouched


def test_running_clock_is_not_ready():
    out = _call(_body(moving=True, movingSec=41.3, clockEst="2026-08-10T09:30:41"))
    assert out["state"] == "running" and out["ready"] is False
    assert "advancing" in out["why"]


def test_sentinel_clock_is_empty_not_ready():
    # The defect this whole endpoint exists for: connected, stationary, speed 0 — and nothing loaded.
    out = _call(_body(clockEst="2099-12-01T00:00:00", clockEstFirst="2099-12-01T00:00:00", speed=0))
    assert out["state"] == "empty" and out["ready"] is False
    assert "2099-12-01" in out["why"]


def test_disconnected():
    out = _call(_body(connection={"status": None, "connected": False, "resolved": True}))
    assert out["state"] == "disconnected" and out["ready"] is False


def test_unresolved_transport_is_not_parked():
    out = _call(_body(transportResolved=False, clockEst=None, moving=None, speed=None))
    assert out["state"] == "unresolved" and out["ready"] is False


def test_unreadable_clock_is_unknown_not_parked():
    # moving null = the clock could not be read. Parked and running cannot be told apart.
    out = _call(_body(moving=None, movingSec=None, clockEst=None, clockEstFirst=None))
    assert out["state"] == "unknown" and out["ready"] is False


def test_unscanned_store_is_not_an_empty_store():
    out = _call(_body(coverageScanned=False, coverage=[], instrument=None))
    assert out["state"] == "parked" and out["ready"] is False
    assert "not scanned" in out["why"]


def test_truncated_scan_is_not_an_empty_store():
    # The scan hit its budget and stopped early: readable stayed 0 because most folders were never
    # reached, not because the store is empty. Must not fall through to "nothing to replay".
    out = _call(_body(coverageTruncated=True, coverageBudgetSec=1,
                       coverage=[{"instrument": "ES 12-26", "files": 3, "readable": 0,
                                  "unreadable": 0, "skipped": 3, "from": None, "to": None, "days": []}]))
    assert out["state"] == "parked" and out["ready"] is False
    assert "budget" in out["why"] and "not proven empty" in out["why"]


def test_scanned_but_no_readable_files():
    out = _call(_body(coverage=[{"instrument": "ES 12-26", "files": 3, "readable": 0,
                                 "unreadable": 3, "skipped": 0, "from": None, "to": None, "days": []}]))
    assert out["state"] == "parked" and out["ready"] is False
    assert "nothing to replay" in out["why"]


def test_query_params_reach_the_addon_and_the_shared_timeout_is_never_moved():
    seen = {}
    saved = app.HTTP_TIMEOUT
    with FakeAddon() as fake:
        def handler(req):
            seen.update(req.query)
            seen["global"] = app.HTTP_TIMEOUT        # a per-call deadline never moves the shared one
            return _body()
        fake.register("/playback", "GET", handler)
        nt8.nt_playback(instrument="ES 12-26", coverage=True, budget_s=30)
    assert seen["instrument"] == ["ES 12-26"] and seen["coverage"] == ["1"]
    assert seen["budgetSec"] == ["30"]
    assert seen["global"] == saved and app.HTTP_TIMEOUT == saved


def _timeout_of(**kwargs):
    """The _timeout nt_playback hands _addon_get for this call."""
    seen = {}

    def spy(path, _timeout=None, **params):
        seen["timeout"] = _timeout
        seen["params"] = {k: v for k, v in params.items() if v is not None}
        return _body(coverageScanned=False, coverage=[], instrument=None)

    from nt8_mcp import tools_playback
    saved = tools_playback._addon_get
    tools_playback._addon_get = spy
    try:
        nt8.nt_playback(**kwargs)
    finally:
        tools_playback._addon_get = saved
    return seen


def test_a_coverage_scan_gets_its_own_deadline():
    # The scan runs far past the 5 s default, so this ONE call carries a wider deadline.
    seen = _timeout_of(instrument="ES 12-26", coverage=True, budget_s=30)
    assert seen["timeout"] >= 30


def test_a_plain_call_sends_no_params_and_no_special_deadline():
    seen = _timeout_of()
    assert seen == {"timeout": None, "params": {}}   # no instrument, no coverage, no budgetSec


def test_addon_error_passes_through_without_a_verdict():
    with FakeAddon() as fake:
        fake.json("/playback", {"error": "boom"}, status=400)
        out = nt8.nt_playback()
    assert out == {"error": "boom"}


def test_clock_unset_boundary():
    unset = tools_playback._clock_unset
    assert unset("2099-12-01T00:00:00") is True
    assert unset("2090-01-01T00:00:00") is True
    assert unset("2026-08-10T09:30:00") is False
    assert unset(None) is False and unset("") is False


def test_a_clock_on_a_day_with_no_recording_says_so():
    # Observed: slider range 09-10..09-17, first .nrd 09-11, clock parked at 09-10 00:00.
    day = {"file": "20260911", "readable": True, "from": "2026-09-11T00:00:00", "to": "2026-09-11T17:00:00"}
    cov = [{"instrument": "ES 12-26", "files": 1, "readable": 1, "days": [day]}]
    off = _call(_body(clockEst="2026-09-10T00:00:00", coverage=cov))
    on = _call(_body(clockEst="2026-09-11T09:30:00", coverage=cov))
    assert off["ready"] is True and "no readable .nrd covers" in off["why"], off["why"]
    assert on["ready"] is True and "no readable .nrd covers" not in on["why"], on["why"]
