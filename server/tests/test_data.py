"""Data tools (nt8_mcp.tools_data) against the canned AddOn in fake_addon.py.

The decoder itself is covered in test_nrd_offline.py; here only nt_nrd_export's wrapper
behaviour (missing folder, argument pass-through) is exercised.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import app, server as nt8, tools_data  # noqa: E402

COVERAGE = {
    "instrument": "ES 12-26",
    "resolved": ["ES 12-26", "ES ##-##"],
    "stores": {
        "tick": {"scanned": True, "granularity": "hour", "files": 475,
                 "days": {"20260910": {"files": 71, "bytes": 1, "last": 24, "bid": 24, "ask": 23}}},
        "replay": {"scanned": False, "days": None},
    },
    "missingWeekdays": ["20260916"],
    "daysLackingBidAsk": ["20260910"],
}


def test_coverage_passes_every_filter_through():
    with FakeAddon() as fake:
        seen = {}

        def handler(req):
            seen.update({k: v[0] for k, v in req.query.items()})
            return COVERAGE

        fake.register("/data/coverage", "GET", handler)
        result = nt8.nt_data_coverage("ES 12-26", kind="tick", from_date="20260901", to_date="20260918")
    assert result["resolved"] == ["ES 12-26", "ES ##-##"], result
    # `from`/`to` are the wire names; the tool arguments are from_date/to_date.
    assert seen == {"instrument": "ES 12-26", "kind": "tick", "from": "20260901", "to": "20260918"}, seen


def test_coverage_omits_empty_filters():
    with FakeAddon() as fake:
        seen = {}
        fake.register("/data/coverage", "GET",
                      lambda req: (seen.update({k: v[0] for k, v in req.query.items()}), COVERAGE)[1])
        nt8.nt_data_coverage("ES 12-26")
    assert seen == {"instrument": "ES 12-26"}, seen


def test_coverage_cache_section_passes_through():
    # cache = what a backtest can use without a provider (NT8's own bars cache) — a pure pass-through field,
    # never reshaped by the tool.
    doc = dict(COVERAGE, cache={"scanned": True, "capReached": False,
               "series": {"ES 12-25/Minute_1_1_Last_Close_Tick_BidAsk_Minute_1.Last":
                          {"contract": "ES 12-25", "firstDay": "20250915", "lastDay": "20251212"}}})
    with FakeAddon() as fake:
        fake.json("/data/coverage", doc)
        result = nt8.nt_data_coverage("ES 12-26")
    assert result["cache"]["scanned"] is True
    assert "ES 12-25" in next(iter(result["cache"]["series"].values()))["contract"], result


def test_coverage_scanned_false_is_carried_through_not_flattened_to_empty():
    # A folder that does not exist must reach the caller as scanned:false with days:null — never as
    # an empty day map, which reads exactly like "NinjaTrader has nothing for this instrument".
    with FakeAddon() as fake:
        fake.json("/data/coverage", COVERAGE)
        result = nt8.nt_data_coverage("ES 12-26")
    assert result["stores"]["replay"]["scanned"] is False
    assert result["stores"]["replay"]["days"] is None


def test_download_defaults_replay_and_all_three_types():
    with FakeAddon() as fake:
        sent = {}
        fake.register("/data/download", "POST",
                      lambda req: (sent.update({"body": req.body}), (202, {"id": "d1", "state": "queued", "days": 3}))[1])
        result = nt8.nt_data_download("ES 12-26", "20260901", "20260903")
    assert result == {"id": "d1", "state": "queued", "days": 3}, result
    import json
    body = json.loads(sent["body"])
    assert body["kinds"] == ["replay"], body
    assert body["types"] == ["Last", "Bid", "Ask"], body
    assert body["overwrite"] is False and body["big"] is False, body
    assert body["from"] == "20260901" and body["to"] == "20260903", body


def test_download_passes_kinds_types_and_flags():
    with FakeAddon() as fake:
        sent = {}
        fake.register("/data/download", "POST",
                      lambda req: (sent.update({"body": req.body}), (202, {"id": "d2", "state": "queued"}))[1])
        nt8.nt_data_download("ES 12-26", "20260901", "20260930",
                             kinds=["tick"], types=["Bid"], overwrite=True, big=True)
    import json
    body = json.loads(sent["body"])
    assert body["kinds"] == ["tick"] and body["types"] == ["Bid"], body
    assert body["overwrite"] is True and body["big"] is True, body


def test_download_exposure_refusal_reaches_the_caller_as_an_error():
    with FakeAddon() as fake:
        fake.json("/data/download",
                  {"error": "data download refused: account 'Sim101' has an open position — flatten and cancel first",
                   "exposure": True}, method="POST", status=409)
        result = nt8.nt_data_download("ES 12-26", "20260901", "20260903")
    assert "refused" in result["error"], result


def test_download_status_keeps_the_four_buckets_separate():
    doc = {"id": "d1", "state": "done", "downloaded": ["20260901/replay"], "skipped": ["20260902/replay"],
           "skippedCurrent": ["20260918"], "failed": [{"date": "20260903", "kind": "replay", "error": "boom"}],
           "anyLive": False, "anyNonSim": True}
    with FakeAddon() as fake:
        fake.json("/data/download/d1", doc)
        result = nt8.nt_data_download_status("d1")
    assert result["downloaded"] == ["20260901/replay"]
    assert result["skippedCurrent"] == ["20260918"]
    assert result["failed"][0]["error"] == "boom"


def test_download_cancel():
    with FakeAddon() as fake:
        fake.json("/data/download/d1", {"ok": True, "state": "cancelled"}, method="DELETE")
        assert nt8.nt_data_download_cancel("d1") == {"ok": True, "state": "cancelled"}


def test_probe_passes_instrument_and_kind_through_and_polls_to_done():
    # /data/probe is queue-and-poll, same shape as /data/download.
    saved = app.POLL_S
    app.POLL_S = 0
    try:
        with FakeAddon() as fake:
            seen = {}

            def start(req):
                seen.update({k: v[0] for k, v in req.query.items()})
                return (202, {"id": "p1", "state": "queued"})

            fake.register("/data/probe", "GET", start)
            fake.json("/data/probe/p1", {"id": "p1", "state": "done", "instrument": "ES 12-26", "kind": "minute",
                                          "requestsUsed": 9, "capRequests": 14, "capReached": False,
                                          "earliestDate": "20240115", "depthDays": 613, "note": "…"})
            result = nt8.nt_data_probe("ES 12-26", kind="minute")
    finally:
        app.POLL_S = saved
    assert seen == {"instrument": "ES 12-26", "kind": "minute"}, seen
    assert result["earliestDate"] == "20240115", result


def test_probe_polls_while_queued_then_running_before_done():
    saved = app.POLL_S
    app.POLL_S = 0
    try:
        with FakeAddon() as fake:
            fake.json("/data/probe", {"id": "p1", "state": "queued"}, status=202)
            fake.register("/data/probe/p1", "GET", lambda req: (
                {"id": "p1", "state": "running"} if req.n < 2
                else {"id": "p1", "state": "done", "instrument": "ES 12-26", "kind": "minute",
                      "requestsUsed": 1, "capRequests": 14, "capReached": False,
                      "earliestDate": "20260101", "depthDays": 10, "note": "…"}
            ))
            result = nt8.nt_data_probe("ES 12-26", kind="minute")
    finally:
        app.POLL_S = saved
    assert result["state"] == "done" and result["earliestDate"] == "20260101", result


def test_probe_cap_reached_reaches_the_caller_as_is():
    # capReached:true means "at least N days" — the tool must not editorialise the null fields into anything else.
    saved = app.POLL_S
    app.POLL_S = 0
    try:
        with FakeAddon() as fake:
            fake.json("/data/probe", {"id": "p1", "state": "queued"}, status=202)
            fake.json("/data/probe/p1", {"id": "p1", "state": "done", "instrument": "ES 12-26", "kind": "day",
                                          "requestsUsed": 2, "capRequests": 14,
                                          "capReached": True, "earliestDate": None, "depthDays": None,
                                          "note": "at least 3650 days back (search cap reached)"})
            result = nt8.nt_data_probe("ES 12-26", kind="day")
    finally:
        app.POLL_S = saved
    assert result["capReached"] is True and result["earliestDate"] is None, result


def test_probe_refusal_reaches_the_caller_as_an_error():
    # A refusal (409) comes back from the queueing call itself, with no "id" — never polled.
    with FakeAddon() as fake:
        fake.json("/data/probe", {"error": "data probe needs a real data provider connected (see /health.connections)",
                                   "anyLive": False, "anyNonSim": False}, status=409)
        result = nt8.nt_data_probe("ES 12-26")
    assert "real data provider" in result["error"], result


def test_probe_still_running_note():
    saved = app.POLL_S
    app.POLL_S = 0
    try:
        with FakeAddon() as fake:
            fake.json("/data/probe", {"id": "p1", "state": "queued"}, status=202)
            fake.json("/data/probe/p1", {"id": "p1", "state": "running"})
            result = nt8.nt_data_probe("ES 12-26", wait_s=0)
    finally:
        app.POLL_S = saved
    assert result["state"] in ("queued", "running"), result
    assert "p1" in result["note"], result


def test_nrd_export_missing_folder_is_an_error_not_an_exception():
    result = tools_data.nt_nrd_export("ES *", os.path.join(_HERE, "_never"),
                                      replay_dir=os.path.join(_HERE, "_no_such_replay_dir"))
    assert "error" in result and "_no_such_replay_dir" in result["error"], result
