# playback module (read side only): GET /playback - sourced by live-smoke.sh,
# see its header for the helpers. Contract: docs/api/playback.md.
#
# Every check here passes on a desktop where Playback has NEVER been connected: a disconnected
# transport is a valid answer, not a failure. Nothing here connects, seeks or changes the replay
# speed, and no check asks for the wide coverage scan (minutes).

# 1. /playback shape, no coverage. The endpoint always takes >= 1100 ms: the two clock samples
#    ARE the answer, and one reading cannot tell a parked transport from a running one.
get_json "/playback"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
def isbool(x): return isinstance(x, bool)
def isint(x): return isinstance(x, int) and not isinstance(x, bool)
if d.get("ok") is not True:
    print("ERR:ok is %r" % (d.get("ok"),))
elif not isbool(d.get("transportResolved")):
    print("ERR:transportResolved is not a bool: %r" % (d.get("transportResolved"),))
elif not isinstance(d.get("connection"), dict) or not isbool(d["connection"].get("connected")):
    print("ERR:connection.connected is not a bool: %r" % (d.get("connection"),))
elif d.get("sampleMs") != 1100:
    print("ERR:sampleMs is %r, expected 1100" % (d.get("sampleMs"),))
elif not isinstance(d.get("resolved"), dict):
    print("ERR:resolved table missing: %r" % (d.get("resolved"),))
elif d.get("coverageScanned") is not False:
    print("ERR:coverageScanned is %r - a plain call must not scan the store" % (d.get("coverageScanned"),))
elif d.get("coverage") != []:
    print("ERR:coverage is %r - an unscanned store must report []" % (d.get("coverage"),))
else:
    print("OK:transportResolved=%s connected=%s clockEst=%s moving=%s speed=%s" % (
        d["transportResolved"], d["connection"]["connected"], d.get("clockEst"),
        d.get("moving"), d.get("speed")))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/playback shape" "$R"

# 2. transportResolved - the reflection in Start_Playback took. WARN, not FAIL: an NT8 that moved
#    PlaybackAdapter is a real finding but not a broken bridge, and /compat carries the detail.
get_json "/playback"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
res = d.get("resolved") or {}
missing = sorted(k for k, v in res.items() if v is not True)
if d.get("transportResolved") is True and not missing:
    print("OK:every PlaybackAdapter member resolved (%d)" % len(res))
elif d.get("transportResolved") is True:
    print("WARN:resolved but these members did not: %r - see GET /compat" % (missing,))
else:
    print("WARN:NinjaTrader.Adapter.PlaybackAdapter did not resolve - every playback field reads null (unknown, not parked)")
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
case "$R" in
  OK:*)   report PASS "/playback reflection" "${R#OK:}" ;;
  WARN:*) report WARN "/playback reflection" "${R#WARN:}" ;;
  *)      report FAIL "/playback reflection" "${R#ERR:}" ;;
esac

# 3. Nulls are honest. moving/movingSec are null TOGETHER when the clock could not be read; a
#    resolved-but-unreadable clock must never read as a parked transport (moving:false).
get_json "/playback"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
mv, ms, clk = d.get("moving"), d.get("movingSec"), d.get("clockEst")
if (mv is None) != (ms is None):
    print("ERR:moving=%r but movingSec=%r - they must be null together" % (mv, ms))
elif mv is None and clk is not None:
    print("ERR:moving is null but clockEst=%r" % (clk,))
elif mv is not None and clk is None:
    print("ERR:moving=%r but clockEst is null" % (mv,))
elif mv is not None and not isinstance(mv, bool):
    print("ERR:moving is not a bool: %r" % (mv,))
else:
    print("OK:clockEst=%s moving=%s movingSec=%s" % (clk, mv, ms))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/playback null discipline" "$R"

# 4. Named instrument -> coverageScanned true. The folder need not exist: the contract is that the
#    store was LOOKED AT, not that it holds anything. Budget kept small so a big store cannot stall
#    the smoke run - a truncated scan is still a scanned one.
get_json "/playback?instrument=ES%20%23%23-%23%23&budgetSec=5"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
cov = d.get("coverage")
if d.get("coverageScanned") is not True:
    print("ERR:coverageScanned is %r for a named instrument" % (d.get("coverageScanned"),))
elif not isinstance(cov, list):
    print("ERR:coverage is not a list: %r" % (cov,))
elif d.get("instrument") != "ES ##-##":
    print("ERR:instrument echoed as %r" % (d.get("instrument"),))
else:
    bad = [c for c in cov if not isinstance(c, dict)
           or [k for k in ("instrument","files","readable","unreadable","skipped","from","to","days") if k not in c]]
    if bad:
        print("ERR:coverage rows missing keys: %r" % (bad[:1],))
    else:
        tot = sum(c.get("files", 0) for c in cov)
        print("OK:%d folder(s), %d .nrd file(s), truncated=%s" % (len(cov), tot, d.get("coverageTruncated")))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/playback coverage (named)" "$R"

# 5. ?instrument= is a FOLDER NAME, not a path. A traversal attempt must be refused with 400
#    before it reaches Path.Combine.
get_json "/playback?instrument=..%2F..%2Fbin"
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "/playback instrument guard" "http 400 as expected"
else
  report FAIL "/playback instrument guard" "http $HTTP_CODE, expected 400"
fi

# 6. There is NO write side. POST /playback must fall through to the core 404 - Route_Playback
#    returns null for it. Writing the replay speed IS the play button; it is not in this repo.
post_json "/playback" '{}'
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "/playback is read only" "POST /playback -> http 404 (no write side)"
else
  report FAIL "/playback is read only" "POST /playback -> http $HTTP_CODE, expected 404"
fi
