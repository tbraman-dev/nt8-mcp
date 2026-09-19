"""Data tools (nt8_mcp.tools_data) against the canned AddOn in fake_addon.py.

The decoder itself is covered in test_nrd_offline.py; here only nt_nrd_export's wrapper
behaviour (missing folder, argument pass-through) is exercised.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import server as nt8, tools_data  # noqa: E402

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


def test_download_unarmed_403_reaches_the_caller_as_an_error():
    # The disarmed default is the normal answer, not an exception: a model must be able to read it.
    with FakeAddon() as fake:
        fake.json("/data/download", {"error": "data download not enabled"}, method="POST", status=403)
        result = nt8.nt_data_download("ES 12-26", "20260901", "20260903")
    assert result == {"error": "data download not enabled"}, result


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


def test_nrd_export_missing_folder_is_an_error_not_an_exception():
    result = tools_data.nt_nrd_export("ES *", os.path.join(_HERE, "_never"),
                                      replay_dir=os.path.join(_HERE, "_no_such_replay_dir"))
    assert "error" in result and "_no_such_replay_dir" in result["error"], result
