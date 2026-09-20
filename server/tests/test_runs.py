"""The run registry: runs.py (writer, called from tools_backtest.nt_backtest(save_run=True)) and
tools_runs.py (nt_runs/nt_run/nt_run_compare). No FakeAddon here — this module never touches the
AddOn's HTTP API, only the filesystem under a temp stand-in for app.NT_HOME.
"""

import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from nt8_mcp import app, runs, tools_runs  # noqa: E402


class _TempNtHome:
    """Points app.NT_HOME at a scratch directory for the duration of a `with` block — runs.py reads
    app.NT_HOME through the module (never a copied name), so this is enough to isolate every call."""

    def __enter__(self):
        self._saved = app.NT_HOME
        self._dir = tempfile.mkdtemp(prefix="nt8runs_")
        app.NT_HOME = self._dir
        return self._dir

    def __exit__(self, *exc):
        app.NT_HOME = self._saved
        shutil.rmtree(self._dir, ignore_errors=True)
        return False


_RESULT_DONE = {
    "id": "b1", "state": "done", "strategy": "SampleMACrossOver", "instrument": "ES 12-26",
    "period": "5 Minute", "from": "2026-09-10T00:00:00", "to": "2026-09-17T23:59:59",
    "barsFrom": "2026-09-09T19:50:00", "barsTo": "2026-09-17T21:00:00", "warnings": [],
    "inputs": {"Fast": 10}, "settings": {"slippageTicks": 1.0},
    "summary": {"trades": 76, "netProfit": -212.5}, "equity": [{"time": "2026-09-09T20:00:00", "cumulativeNetProfit": 100.0}],
}
_REQUEST = {"strategy": "SampleMACrossOver", "instrument": "ES 12-26", "from": "2026-09-10", "to": "2026-09-17"}


def test_save_run_writes_a_file_nt_run_can_read_back():
    with _TempNtHome():
        saved = runs.save_run(_REQUEST, _RESULT_DONE)
        assert "id" in saved, saved
        assert os.path.isfile(saved["path"])
        rec = tools_runs.nt_run(saved["id"])
        assert rec["strategy"] == "SampleMACrossOver"
        assert rec["request"] == _REQUEST
        assert rec["summary"]["trades"] == 76
        assert rec["equity"] == _RESULT_DONE["equity"]
        assert rec["sourceHash"] is None  # no bin\Custom\Strategies under this scratch dir


def test_save_run_never_raises_when_the_directory_cannot_be_made():
    with _TempNtHome() as home:
        # nt8mcp is a FILE, not a directory: os.makedirs(.../nt8mcp/runs) must fail cleanly.
        blocker = os.path.join(home, "nt8mcp")
        with open(blocker, "w", encoding="utf-8"):
            pass
        saved = runs.save_run(_REQUEST, _RESULT_DONE)
        assert "error" in saved, saved


def test_strategy_source_hash_found_by_exact_filename():
    with _TempNtHome() as home:
        folder = os.path.join(home, "bin", "Custom", "Strategies")
        os.makedirs(folder)
        src = os.path.join(folder, "SampleMACrossOver.cs")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("public class SampleMACrossOver : Strategy {}")
        h = runs._strategy_source_hash("SampleMACrossOver")
        assert h is not None and len(h) == 64
        with open(src, "rb") as fh:
            import hashlib
            assert h == hashlib.sha256(fh.read()).hexdigest()


def test_strategy_source_hash_found_by_class_name_when_the_file_is_named_differently():
    with _TempNtHome() as home:
        folder = os.path.join(home, "bin", "Custom", "Strategies", "Nested")
        os.makedirs(folder)
        with open(os.path.join(folder, "Impl.cs"), "w", encoding="utf-8") as fh:
            fh.write("namespace X { public class AWLiquidityReversal : Strategy {} }")
        assert runs._strategy_source_hash("AWLiquidityReversal") is not None


def test_strategy_source_hash_none_when_not_found():
    with _TempNtHome() as home:
        os.makedirs(os.path.join(home, "bin", "Custom", "Strategies"))
        assert runs._strategy_source_hash("NoSuchStrategy_xyz") is None


def test_nt_runs_lists_both_runs_and_filters_by_strategy():
    with _TempNtHome():
        runs.save_run(_REQUEST, dict(_RESULT_DONE, id="b1"))
        runs.save_run(dict(_REQUEST, strategy="OtherStrategy"), dict(_RESULT_DONE, id="b2", strategy="OtherStrategy"))
        rows = tools_runs.nt_runs()
        assert {r["strategy"] for r in rows} == {"SampleMACrossOver", "OtherStrategy"}, rows
        filtered = tools_runs.nt_runs(strategy="otherstrategy")  # case-insensitive
        assert len(filtered) == 1 and filtered[0]["strategy"] == "OtherStrategy"


def test_nt_run_unknown_id_is_an_error():
    with _TempNtHome():
        result = tools_runs.nt_run("nope")
    assert "error" in result and "nope" in result["error"]


def test_nt_run_compare_diffs_inputs_and_summary():
    with _TempNtHome():
        a = runs.save_run(dict(_REQUEST, inputs={"Fast": 10}),
                           dict(_RESULT_DONE, id="b1", inputs={"Fast": 10}, summary={"trades": 10, "netProfit": 100.0}))
        b = runs.save_run(dict(_REQUEST, inputs={"Fast": 15}),
                           dict(_RESULT_DONE, id="b2", inputs={"Fast": 15}, summary={"trades": 10, "netProfit": 300.0}))
        diff = tools_runs.nt_run_compare(a["id"], b["id"])
    assert diff["inputs"] == {"Fast": {"a": 10, "b": 15}}, diff
    assert diff["summary"]["netProfit"] == {"a": 100.0, "b": 300.0}, diff
    assert "trades" not in diff["summary"], diff  # unchanged fields are not in the diff


def test_nt_run_compare_unknown_id_names_which_one():
    with _TempNtHome():
        a = runs.save_run(_REQUEST, dict(_RESULT_DONE, id="b1"))
        missing = tools_runs.nt_run_compare(a["id"], "nope")
        also_missing = tools_runs.nt_run_compare("nope", a["id"])
    assert "nope" in missing["error"]
    assert "nope" in also_missing["error"]
