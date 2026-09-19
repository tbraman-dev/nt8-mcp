# Portions of this file are derived from cli-nt-bridge
# (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
# MIT License. The full notice is in NOTICE at the repository root.
#
# The synthetic .nrd builder is cli-nt-bridge: tests/nrd_helpers.py and the decoder assertions are
# cli-nt-bridge: tests/test_nrd_offline.py, converted from pytest to this repo's plain-assert runner.
"""nt8_mcp.nrd_offline against a hand-encoded synthetic .nrd — no market data shipped.

Every test here is data-free, so it runs anywhere. There is no real-.nrd fixture in this
repo: the live check is nt_nrd_export against db\\replay, done by hand against a real replay store.
"""

import os
import shutil
import struct
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

try:
    from nt8_mcp import nrd_offline as N
except ImportError as _e:       # numpy / pyarrow are not declared dependencies of nt8-mcp
    N = None
    _WHY = str(_e)

_EPOCH_TICKS = 621355968000000000
_T0 = _EPOCH_TICKS + 10_000_000_000     # EPOCH + 1000s (clean round number)
HEADER_LEN = 44 * 80


def _slot(count=0, first=0.0, tick=0.0, t0=0, pmin=0.0, pmax=0.0, volsum=0):
    # struct: last, count, pmax, pmin, first, one, tick, flag, t0, t1, volsum
    return struct.pack("<diddddd i qq q", 0.0, count, pmax, pmin, first, 1.0, tick, 0, t0, t0, volsum)


def synthetic_nrd() -> bytes:
    """A valid minimal .nrd: header + 2 L1 (Last) + 2 L2 (ask/bid) events.

    Header volsum/price-range fields match the events so the integrity check passes clean."""
    slots = [_slot() for _ in range(44)]
    slots[2] = _slot(10, 21800.0, 0.25, _T0, pmin=21800.25, pmax=21800.25, volsum=3)    # Last (vol 1+2)
    slots[10] = _slot(10, 21810.0, 0.25, _T0, pmin=21810.0, pmax=21810.0, volsum=5)     # ask (vol 5)
    slots[11] = _slot(10, 21790.0, 0.25, _T0, pmin=21789.5, pmax=21789.5, volsum=3)     # bid (vol 3)
    events = bytes([
        0x20, 0x4F, 0xC0, 0x01,                  # L1 Last: price +1 tick, vol 1 -> 21800.25
        0x20, 0x00, 0x00, 0x05,                  # L2 ask add pos0, vol 5 -> 21810.0
        0x28, 0x80, 0x01, 0x7E, 0x03,            # L2 bid add pos1, price -2 ticks, vol 3 -> 21789.5
        0x21, 0x40, 0xC0, 0x64, 0x02,            # L1 Last: ts +100 (x100ns), vol 2 -> 21800.25
    ])
    return b"".join(slots) + events


# decoded expectations (hand-verified against the .nrd format spec)
EXPECTED_L1 = [                 # (ts_ns_utc, mdt, price, vol)
    (1000000000000, 2, 21800.25, 1),
    (1000000010000, 2, 21800.25, 2),
]
EXPECTED_L2 = [                 # (ts_ns_utc, side, op, pos, price, vol)
    (1000000000000, 0, 0, 0, 21810.0, 5),
    (1000000000000, 1, 0, 1, 21789.5, 3),
]


def _skip() -> bool:
    if N is None:
        print(f"      (nrd_offline unavailable: {_WHY}) — skipped")
        return True
    return False


def _decode(cut: int = 0, **kw):
    raw = synthetic_nrd()
    events = raw[HEADER_LEN:len(raw) - cut] if cut else raw[HEADER_LEN:]
    return N.decode(events, N.parse_headers(raw[:HEADER_LEN]), **kw)


def test_decode_synthetic():
    if _skip():
        return
    d = _decode()
    assert d["truncated"] is False
    l1 = list(zip(d["L1"][0].tolist(), d["L1"][1].tolist(), d["L1"][2].tolist(), d["L1"][3].tolist()))
    l2 = list(zip(d["L2"][0].tolist(), d["L2"][1].tolist(), d["L2"][2].tolist(),
                  d["L2"][3].tolist(), d["L2"][4].tolist(), d["L2"][5].tolist()))
    assert l1 == EXPECTED_L1, l1
    assert l2 == EXPECTED_L2, l2


def test_salvage_drops_only_the_incomplete_record():
    if _skip():
        return
    d = _decode(cut=1, salvage=True)        # cut the last byte -> final record incomplete
    assert d["truncated"] is True
    assert len(d["L1"][0]) == 1 and len(d["L2"][0]) == 2


def test_salvage_off_raises():
    if _skip():
        return
    try:
        _decode(cut=1, salvage=False)
    except N.FormatError:
        return
    raise AssertionError("salvage=False must raise FormatError on a truncated stream")


def test_l2_dtypes():
    if _skip():
        return
    _, side, op, pos, _, _ = _decode()["L2"]
    assert (side.dtype.name, op.dtype.name, pos.dtype.name) == ("int8", "int8", "int32")


def test_integrity_clean():
    if _skip():
        return
    d = _decode()
    assert d["integrity_ok"] is True and d["integrity_errors"] == []


def test_integrity_catches_corruption():
    # Byte damage stays in-format and would otherwise decode to silently-wrong data: the header's
    # own volume sums are the only thing that catches it.
    if _skip():
        return
    raw = bytearray(synthetic_nrd())
    raw[HEADER_LEN + 3] = 0xFF              # first L1 event's volume byte: 1 -> 255
    d = N.decode(bytes(raw[HEADER_LEN:]), N.parse_headers(bytes(raw[:HEADER_LEN])))
    assert d["integrity_ok"] is False
    assert any("volume" in e for e in d["integrity_errors"]), d["integrity_errors"]


def test_integrity_skipped_when_truncated():
    # A truncated file legitimately falls short of the header totals -> not flagged as corrupt.
    if _skip():
        return
    d = _decode(cut=1, salvage=True)
    assert d["truncated"] is True and d["integrity_ok"] is True


def test_decode_refuses_a_header_claiming_more_events_than_the_stream_can_hold():
    # A damaged header's i32 count can claim ~2.1e9 events. Without a bound check that tries to
    # np.empty() hundreds of GB (MemoryError) instead of failing loud and cheap as a corrupt file.
    if _skip():
        return
    raw = bytearray(synthetic_nrd())
    slots = N.parse_headers(bytes(raw[:HEADER_LEN]))
    slots[2]["count"] = 2_000_000_000        # header claims 2B Last events; the stream holds 4 bytes
    try:
        N.decode(bytes(raw[HEADER_LEN:]), slots)
    except N.FormatError as e:
        assert "header claims" in str(e), e
        return
    raise AssertionError("an impossible header count must raise FormatError, not attempt the allocation")


def test_season_year_dec_roll_vs_calendar():
    if _skip():
        return
    assert N.season_year("MNQ", "20251216") == "2026"   # quarterly: after the Dec roll
    assert N.season_year("MNQ", "20251201") == "2025"   # quarterly: before it
    assert N.season_year("MGC", "20251231") == "2025"   # non-quarterly: calendar year


def test_symbol_of_rejects_half_renamed_dirs():
    if _skip():
        return
    assert N.symbol_of("MNQ 09-25") == "MNQ"
    assert N.symbol_of("MGC ##-##") == "MGC"
    assert N.symbol_of("NQ ##-26") is None


def _tree(root: str, name: str, date: str, payload: bytes):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{date}.nrd"), "wb") as fh:
        fh.write(payload)


def test_export_writes_parquet_and_skips_on_rerun():
    if _skip():
        return
    tmp = tempfile.mkdtemp()
    try:
        replay, out = os.path.join(tmp, "replay"), os.path.join(tmp, "out")
        _tree(replay, "MNQ 12-25", "20251215", synthetic_nrd())
        res = N.export(replay, "MNQ *", out, levels=("L1", "L2"))
        assert res["exported"] == ["MNQ/20251215"], res
        assert res["corrupt"] == [] and res["failed"] == [], res
        assert os.path.isfile(N.target(out, "MNQ", "20251215", "L1")), os.listdir(out)
        again = N.export(replay, "MNQ *", out)
        assert again["exported"] == [] and again["count"] == 0, again
        forced = N.export(replay, "MNQ *", out, force=True)
        assert forced["exported"] == ["MNQ/20251215"], forced
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_writes_nothing_for_a_corrupt_nrd():
    # The whole point of the header cross-check: a corrupt day must leave NO parquet behind, or the
    # next run skips it forever and the gap is permanent and silent.
    if _skip():
        return
    tmp = tempfile.mkdtemp()
    try:
        replay, out = os.path.join(tmp, "replay"), os.path.join(tmp, "out")
        bad = bytearray(synthetic_nrd())
        bad[HEADER_LEN + 3] = 0xFF
        _tree(replay, "MNQ 12-25", "20251215", bytes(bad))
        res = N.export(replay, "MNQ *", out)
        assert res["exported"] == [] and len(res["corrupt"]) == 1, res
        assert not os.path.exists(N.target(out, "MNQ", "20251215", "L1"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_never_commits_a_zero_row_level():
    # An all-empty header decodes to 0 rows. A committed empty parquet would make discovery skip
    # that date forever, so nothing is written and the date lands in failed[].
    if _skip():
        return
    tmp = tempfile.mkdtemp()
    try:
        replay, out = os.path.join(tmp, "replay"), os.path.join(tmp, "out")
        _tree(replay, "MNQ 12-25", "20251215", b"".join(_slot() for _ in range(44)))
        res = N.export(replay, "MNQ *", out)
        assert res["exported"] == [] and len(res["failed"]) == 1, res
        assert not os.path.exists(N.target(out, "MNQ", "20251215", "L1"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
