# Portions of this file are derived from cli-nt-bridge
# (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
# MIT License. The full notice is in NOTICE at the repository root.
#
# Copied near-verbatim from cli-nt-bridge's nt8bridge/nrd_offline.py. The only
# addition is export() at the bottom, which folds in the atomic-commit and corrupt/empty
# rules cli-nt-bridge's histdump.py applied around this decoder.
#
# numpy and pyarrow are imported at module scope: import this module lazily (inside the
# tool function), never from a tools_*.py at import time — server.py imports every
# tools_*.py, so a missing wheel would take the whole MCP server down with it.
"""Offline NinjaTrader .nrd MarketReplay decoder -> L1/L2 UTC parquet (no NinjaTrader).

Decodes the .nrd binary directly and is verified byte-exact against NT8's own
MarketReplay.DumpMarketDepth across MNQ (0.25 tick) and MGC (0.10 tick), ~20 days /
tens of millions of events. The only behavioral difference vs the NT8 engine is SALVAGE:
on end-of-stream truncation, emit the clean prefix instead of failing (NT8 instead keeps
one garbage trailing row). Decoder logic derived from a community .nrd converter.

.NRD FORMAT: 44-slot x 80-byte header (per slot: f64 last, i32 count, f64 max, f64 min,
f64 first, f64 (1.0), f64 tick, i32 flag, i64 t0, i64 t1, i64 volsum; slots 0-9 = L1
MarketDataType, 10/11 = L2 ask/bid depth). Then a merged, time-ordered, variable-length
event stream; timestamps are .NET DateTime ticks (100 ns since 0001-01-01), already UTC.
Full field spec in the source converter docstring.
"""
from __future__ import annotations

import os
import re
import struct
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pyarrow as pa

EPOCH_TICKS = 621355968000000000        # .NET ticks at 1970-01-01T00:00Z
_ONE_DAY = timedelta(days=1)
HDR = struct.Struct("<diddddd i qq q")  # 80 bytes
N_SLOTS = 44
TSSIZE = (0, 1, 2, 4)
VOLMAP = {0: (0, 1), 1: (1, 1), 2: (1, 100), 3: (1, 500), 4: (1, 1000),
          5: (2, 1), 6: (4, 1)}

L1_TYPES = ("Ask", "Bid", "Last", "DailyHigh", "DailyLow", "DailyVolume",
            "LastClose", "Opening", "OpenInterest", "Settlement")

# Symbols that roll on the December quarterly cycle; their Parquet "season" year starts on
# the Monday before the 3rd Friday of December. Everything else = plain calendar year.
QUARTERLY_FINANCIALS = frozenset(
    ("ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K"))


class FormatError(RuntimeError):
    pass


def _third_friday(year: int, month: int) -> datetime:
    d = datetime(year, month, 1)
    return datetime(year, month, 1 + ((4 - d.weekday()) % 7) + 14)


def season_year(symbol: str, date: str) -> str:
    """Roll-season year (4-char string) for a YYYYMMDD file date. Quarterly financials roll
    into the next season on the Monday before December's 3rd Friday; others use calendar year."""
    y = int(date[:4])
    if symbol.upper() in QUARTERLY_FINANCIALS:
        boundary = _third_friday(y, 12).replace(hour=0) - _ONE_DAY * 4  # Mon before 3rd Fri
        if datetime(y, int(date[4:6]), int(date[6:8])) >= boundary:
            return str(y + 1)
    return str(y)


def parse_headers(head: bytes):
    slots = []
    for s in range(N_SLOTS):
        (last, cnt, pmax, pmin, pfirst, _one, ticksz, _flag,
         t0, t1, volsum) = HDR.unpack_from(head, s * 80)
        slots.append(dict(count=cnt, first=pfirst, tick=ticksz, t0=t0, t1=t1,
                          pmax=pmax, pmin=pmin, volsum=volsum))
    return slots


def _integrity_errors(slots, out) -> list:
    """Cross-check the decoded events against the header's OWN per-slot volume-sum and price
    range. A clean .nrd matches exactly (validated across every symbol/size); a corrupt one
    diverges — byte damage usually stays in-format and would otherwise decode to SILENTLY-WRONG
    data. Returns a list of mismatch strings ([] = clean). Not meaningful on a truncated file
    (its decoded prefix legitimately holds less than the header totals), so callers skip it there."""
    errs = []
    if "L1" in out:
        _, mdt, price, vol = out["L1"]
        for k in range(10):
            s = slots[k]
            if not s["count"]:
                continue
            m = mdt == k
            if not m.any():
                continue
            dv = int(vol[m].sum())
            if dv != int(s["volsum"]):
                errs.append(f"L1[{k}] volume {dv} != header {int(s['volsum'])}")
            if float(price[m].min()) < s["pmin"] - 1e-6 or float(price[m].max()) > s["pmax"] + 1e-6:
                errs.append(f"L1[{k}] price outside header range [{s['pmin']}, {s['pmax']}]")
    if "L2" in out:
        _, side, _, _, _, vol2 = out["L2"]
        for k, sd in ((10, 0), (11, 1)):
            s = slots[k]
            if not s["count"]:
                continue
            dv = int(vol2[side == sd].sum())
            if dv != int(s["volsum"]):
                errs.append(f"L2[side{sd}] volume {dv} != header {int(s['volsum'])}")
    return errs


def _price_decimals(tick: float) -> int:
    """Decimal places implied by the tick size, so integer-tick prices round to the same
    double the CSV pipeline got by parsing decimal strings (e.g. a 0.1 tick -> 4342.9)."""
    return max(0, -Decimal(str(tick)).as_tuple().exponent)


def _grow(a: np.ndarray, cap: int) -> np.ndarray:
    b = np.empty(cap, a.dtype)
    b[:len(a)] = a
    return b


class _Trunc(Exception):
    """Internal: end-of-stream ran out of bytes mid-record."""


def decode(buf: bytes, slots, want_l1: bool = True, want_l2: bool = True,
           salvage: bool = True, verify: bool = True) -> dict:
    """Single-pass decode of the merged event stream.

    Returns {"L1": (ts_ns_utc, mdt, price, vol), "L2": (ts, side, op, pos, price, vol),
             "truncated": bool, "truncated_at": int|None}. With salvage=True, an end-of-stream
    truncation returns everything decoded cleanly so far (dropping the incomplete final
    record); with salvage=False it raises FormatError. An UNKNOWN encoding (bad volume code)
    always raises FormatError, salvage or not."""
    tick = next((s["tick"] for s in slots if s["count"]), None)
    if tick is None:
        out = {"truncated": False, "truncated_at": None, "integrity_ok": True, "integrity_errors": []}
        if want_l1:
            out["L1"] = (np.empty(0, "int64"), np.empty(0, "int8"),
                         np.empty(0, "float64"), np.empty(0, "int64"))
        if want_l2:
            out["L2"] = (np.empty(0, "int64"), np.empty(0, "int8"), np.empty(0, "int8"),
                         np.empty(0, "int32"), np.empty(0, "float64"), np.empty(0, "int64"))
        return out

    cap1 = sum(slots[k]["count"] for k in range(10))
    cap2 = slots[10]["count"] + slots[11]["count"]
    # The header's counts are i32 fields with no cross-check yet at this point (that's what
    # _integrity_errors does, later, and only on a clean decode) — a damaged header can claim
    # ~2.1e9 events and try to allocate hundreds of GB before this function has any evidence
    # the file is real. Each event costs at least 3 bytes on the wire, so bound the claim by
    # what the stream can physically hold and treat an impossible claim as a format error
    # (caught by export()'s except, same as any other corrupt file) rather than an OOM.
    if cap1 + cap2 > len(buf) // 3 + N_SLOTS:
        raise FormatError(f"header claims {cap1 + cap2} events but the stream is only {len(buf)} bytes")
    a_ts = np.empty(cap1, "int64"); a_mdt = np.empty(cap1, "int8")
    a_price = np.empty(cap1, "float64"); a_vol = np.empty(cap1, "int64")
    b_ts = np.empty(cap2, "int64"); b_side = np.empty(cap2, "int8")
    b_op = np.empty(cap2, "int8"); b_pos = np.empty(cap2, "int32")
    b_price = np.empty(cap2, "float64"); b_vol = np.empty(cap2, "int64")

    cur = [round(slots[k]["first"] / tick) for k in range(10)]
    l2cur = [round(slots[10]["first"] / tick), round(slots[11]["first"] / tick)]
    t = min(s["t0"] for s in slots if s["count"])
    n = len(buf); i = 0; k1 = 0; k2 = 0
    truncated_at = None

    def rd(j, sz):                     # bounded read; signals truncation past EOF
        if j + sz > n:
            raise _Trunc()
        return buf[j:j + sz]

    try:
        while i < n:
            if i + 3 > n:
                raise _Trunc()
            m1 = buf[i]; m2 = buf[i + 1]; info = buf[i + 2]; j = i + 3
            tscode = m1 & 3
            if m1 & 4:
                if tscode == 0:
                    t += int.from_bytes(rd(j, 8), "big"); j += 8
                else:
                    sz = TSSIZE[tscode]; t += int.from_bytes(rd(j, sz), "big") * 10_000_000; j += sz
            elif tscode == 1:
                t += rd(j, 1)[0]; j += 1
            elif tscode == 2:
                bb = rd(j, 2); t += (bb[0] << 8) | bb[1]; j += 2
            elif tscode == 3:
                t += int.from_bytes(rd(j, 4), "big"); j += 4

            pbits = m1 & 0x18
            if info & 0xC0 == 0xC0:                     # ---- L1 event
                mdt = m2 >> 5; nib = m2 & 0x1F
                if nib == 0x1F: mdt += 8; nib = 0
                c = cur[mdt]
                if nib:
                    c += nib - 15 if nib <= 14 else nib - 14
                elif pbits == 0x08:
                    c -= 15
                elif pbits == 0x10:
                    c += rd(j, 1)[0] - 0x80; j += 1
                elif pbits == 0x18:
                    c += int.from_bytes(rd(j, 4), "big") - 0x80000000; j += 4
                cur[mdt] = c
                sz, mult = VOLMAP[(m1 >> 5) & 7]        # KeyError -> unknown encoding (raises below)
                vol = int.from_bytes(rd(j, sz), "big") * mult if sz else 0; j += sz
                if k1 >= cap1:
                    cap1 = max(cap1 * 2, 1024)
                    a_ts = _grow(a_ts, cap1); a_mdt = _grow(a_mdt, cap1)
                    a_price = _grow(a_price, cap1); a_vol = _grow(a_vol, cap1)
                a_ts[k1] = (t - EPOCH_TICKS) * 100; a_mdt[k1] = mdt
                a_price[k1] = c * tick; a_vol[k1] = vol; k1 += 1
            else:                                       # ---- L2 depth op
                side = 1 if (m2 & 0x80) else 0; c = l2cur[side]
                if pbits == 0x08:
                    c += rd(j, 1)[0] - 0x80; j += 1
                elif pbits == 0x10:
                    c += int.from_bytes(rd(j, 2), "big") - 0x8000; j += 2
                elif pbits == 0x18:
                    c += int.from_bytes(rd(j, 4), "big") - 0x80000000; j += 4
                l2cur[side] = c
                sz, mult = VOLMAP[(m1 >> 5) & 7]
                vol = int.from_bytes(rd(j, sz), "big") * mult if sz else 0; j += sz
                if k2 >= cap2:
                    cap2 = max(cap2 * 2, 1024)
                    b_ts = _grow(b_ts, cap2); b_side = _grow(b_side, cap2); b_op = _grow(b_op, cap2)
                    b_pos = _grow(b_pos, cap2); b_price = _grow(b_price, cap2); b_vol = _grow(b_vol, cap2)
                b_ts[k2] = (t - EPOCH_TICKS) * 100; b_side[k2] = side
                b_op[k2] = 2 if (info & 0x80) else (1 if (info & 0x40) else 0)
                b_pos[k2] = info & 0x3F; b_price[k2] = c * tick; b_vol[k2] = vol; k2 += 1
            i = j
    except _Trunc:
        if not salvage:
            raise FormatError(f"truncated record at offset {i}")
        truncated_at = i
    except KeyError:
        raise FormatError(
            f"unknown volume code at offset {i}: {buf[i:i+16].hex(' ')}") from None

    if truncated_at is None and i != n:
        if salvage:
            truncated_at = i
        else:
            raise FormatError(f"stream not fully consumed: stopped at offset {i} of {n}")

    ndp = _price_decimals(tick)
    out = {"truncated": truncated_at is not None, "truncated_at": truncated_at}
    if want_l1:
        p = a_price[:k1].copy(); np.round(p, ndp, out=p)
        out["L1"] = (a_ts[:k1], a_mdt[:k1], p, a_vol[:k1])
    if want_l2:
        p = b_price[:k2].copy(); np.round(p, ndp, out=p)
        out["L2"] = (b_ts[:k2], b_side[:k2], b_op[:k2], b_pos[:k2], p, b_vol[:k2])
    # Header-integrity cross-check (skip on a truncated file — its prefix legitimately falls short).
    errs = _integrity_errors(slots, out) if (verify and not out["truncated"]) else []
    out["integrity_ok"] = not errs
    out["integrity_errors"] = errs
    return out


def convert_file(nrd_path, want_l1: bool = True, want_l2: bool = True,
                 salvage: bool = True) -> dict:
    raw = Path(nrd_path).read_bytes()
    if len(raw) < N_SLOTS * 80:
        raise FormatError(f"{nrd_path}: too small for header table")
    slots = parse_headers(raw[:N_SLOTS * 80])
    return decode(raw[N_SLOTS * 80:], slots, want_l1, want_l2, salvage)


def _meta(nrd_path: Path) -> dict:
    p = Path(nrd_path); st = p.stat()
    return {
        b"replay_importer.timestamps": b"UTC",
        b"nrd2parquet.version": b"2",
        b"nrd2parquet.source_name": str(p.name).encode(),
        b"nrd2parquet.source_contract": p.parent.name.encode(),
        b"nrd2parquet.source_size": str(st.st_size).encode(),
        b"nrd2parquet.source_mtime_ns": str(st.st_mtime_ns).encode(),
    }


def build_table(arrays, nrd_path) -> pa.Table:
    ts, mdt, price, vol = arrays
    return pa.table({
        "Timestamp": pa.array(ts, pa.timestamp("ns", tz="UTC")),
        "MarketDataType": pa.array(mdt, pa.int8()),
        "Price": pa.array(price, pa.float64()),
        "Volume": pa.array(vol, pa.int64()),
    }).replace_schema_metadata(_meta(nrd_path))


def build_table_l2(arrays, nrd_path) -> pa.Table:
    ts, side, op, pos, price, vol = arrays
    return pa.table({
        "Timestamp": pa.array(ts, pa.timestamp("ns", tz="UTC")),
        "MarketDataType": pa.array(side, pa.int8()),
        "Operation": pa.array(op, pa.int8()),
        "Position": pa.array(pos, pa.int32()),
        "MarketMaker": pa.nulls(len(ts), pa.string()),
        "Price": pa.array(price, pa.float64()),
        "Volume": pa.array(vol, pa.int64()),
    }).replace_schema_metadata(_meta(nrd_path))


# A valid replay contract folder is "<SYM> MM-YY" (front month) or "<SYM> ##-##" (continuous).
CONTRACT_DIR_RE = re.compile(r"^\S+ (\d{2}-\d{2}|##-##)$")


def symbol_of(dirname: str):
    """Symbol from a contract folder name, or None if it isn't a valid replay contract dir."""
    return dirname.split(" ")[0].upper() if CONTRACT_DIR_RE.fullmatch(dirname) else None


def discover(replay_root, instrument_glob: str) -> dict:
    """Map (symbol, YYYYMMDD) -> chosen .nrd path (largest file = front contract) for every
    contract folder under replay_root matching instrument_glob."""
    work: dict = {}
    for contract_dir in sorted(Path(replay_root).glob(instrument_glob)):
        if not contract_dir.is_dir():
            continue
        sym = symbol_of(contract_dir.name)
        if sym is None:
            continue
        for f in contract_dir.glob("*.nrd"):
            if not re.fullmatch(r"\d{8}", f.stem):
                continue
            key = (sym, f.stem)
            if key not in work or f.stat().st_size > work[key].stat().st_size:
                work[key] = f
    return work


# ---------------------------------------------------------------------------
# export(): the batch driver. Their histdump.py:82-126 and :245-248, folded in here so the
# decoder ships with the write rules that make its output safe to skip on a re-run.
# ---------------------------------------------------------------------------


def target(out_dir, sym: str, date: str, level: str) -> Path:
    """Parquet path for one (sym, date, level) in the season-year layout:
    <out>/<SEASON>/<SYM>-<SEASON>_<L1|L2>/<YYYYMMDD>.parquet."""
    season = season_year(sym, date)
    return Path(out_dir) / season / f"{sym}-{season}_{level}" / f"{date}.parquet"


def export(replay_root, instrument_glob: str, out_dir, levels=("L1", "L2"),
           force: bool = False) -> dict:
    """Decode every discovered .nrd under replay_root to per-day L1/L2 UTC parquet.

    Skips a date whose targets all exist unless force. A corrupt file (header cross-check
    failed) writes NOTHING and lands in corrupt[]; a truncated one writes its clean prefix and
    is listed in truncated[]. Each parquet is committed atomically (temp sibling + os.replace)
    and a 0-row level is never committed — a silent empty file would make every later run skip
    that date forever. Returns {exported, failed, truncated, corrupt, empty, count}."""
    import pyarrow.parquet as pq

    levels = [lv for lv in levels if lv in ("L1", "L2")]
    if not levels:
        raise ValueError("levels must contain L1 and/or L2")
    out_dir = Path(out_dir)
    want_l1, want_l2 = "L1" in levels, "L2" in levels

    exported: list = []
    failed: list = []
    truncated: list = []
    corrupt: list = []
    empty: list = []

    for (sym, date), nrd in sorted(discover(replay_root, instrument_glob).items()):
        tag = f"{sym}/{date}"
        if not (force or any(not target(out_dir, sym, date, lv).exists() for lv in levels)):
            continue
        try:
            dec = convert_file(nrd, want_l1=want_l1, want_l2=want_l2, salvage=True)
        except (FormatError, OSError, ValueError, MemoryError) as e:
            # A genuinely unknown encoding / unreadable file / (belt-and-braces) a header so
            # damaged the bound check above still let an oversized-but-plausible allocation
            # through fails this day only; run continues.
            failed.append({"file": tag, "error": str(e)})
            continue
        if not dec.get("integrity_ok", True):
            # Header cross-check failed -> the .nrd is corrupt and would decode to silently-wrong
            # data. Surface it loudly and write NOTHING (re-download the .nrd).
            corrupt.append({"file": tag, "errors": dec.get("integrity_errors", [])})
            continue
        if dec.get("truncated"):
            rows = (len(dec["L1"][0]) if want_l1 else 0) + (len(dec["L2"][0]) if want_l2 else 0)
            truncated.append({"file": tag, "rows": rows})

        wrote = []
        for lv in levels:
            tbl = build_table(dec[lv], nrd) if lv == "L1" else build_table_l2(dec[lv], nrd)
            if tbl.num_rows == 0:
                empty.append({"file": tag, "level": lv})
                continue
            out = target(out_dir, sym, date, lv)
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(f".tmp{os.getpid()}")   # sibling of final -> atomic replace
            pq.write_table(tbl, tmp, compression="zstd")
            os.replace(tmp, out)
            wrote.append(lv)
        if wrote:
            exported.append(tag)
        else:
            failed.append({"file": tag, "error": "every requested level decoded to 0 rows — nothing committed"})

    return {"engine": "offline", "exported": exported, "failed": failed,
            "truncated": truncated, "corrupt": corrupt, "empty": empty,
            "count": len(exported)}
