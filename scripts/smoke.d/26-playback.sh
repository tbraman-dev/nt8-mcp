# playback module: GET /playback (read) plus seek/speed/run (write) - sourced by
# live-smoke.sh, see its header for the helpers. Contract: docs/api/playback.md.
#
# Checks 1-6 pass on a desktop where Playback has NEVER been connected: a disconnected transport is
# a valid answer, not a failure. Nothing in checks 1-6 connects, seeks or changes the replay speed,
# and no check asks for the wide coverage scan (minutes).
#
# Checks 7+ probe the WRITE side without ever moving the clock: orders.enabled (the SAME arming
# file NT8BridgeOrders.cs reads - 91-orders.sh is the one file that reports on its presence/absence)
# is never created or deleted here. While it is absent every /playback/{seek,speed,run} POST must
# answer 403 before touching NinjaTrader, and those are the only writes this file makes. If the flag
# IS present (armed), this file skips the write POSTs and reports what it found instead - actually
# moving the replay clock during a smoke run is a job for the by-hand verification steps in
# docs/api/playback.md, not for this script.

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

# 6. The bare path stays read only. POST /playback (no sub-path) must fall through to the core
#    404 - Route_Playback returns null for it; the write side lives at /playback/seek, /speed
#    and /run only, never on /playback itself.
post_json "/playback" '{}'
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "/playback bare path is read only" "POST /playback -> http 404 (writes live at /playback/seek|speed|run)"
else
  report FAIL "/playback bare path is read only" "POST /playback -> http $HTTP_CODE, expected 404"
fi

# ---------------------------------------------------------------------------
# write side: seek / speed / run. See NOTES at the top of this file - the arming file
# (orders.enabled) is never created or deleted here.
# ---------------------------------------------------------------------------

ADDONS_DIR="$HOME/Documents/NinjaTrader 8/bin/Custom/AddOns"
PB_ARMED=0
if [ -e "$ADDONS_DIR/orders.enabled" ]; then
  PB_ARMED=1
  report WARN "playback writes armed" "orders.enabled EXISTS - /playback/{seek,speed,run} may be ARMED (same flag the order module reads). Skipping the disarmed-403 checks below."
else
  report PASS "playback writes disarmed" "no orders.enabled - /playback/{seek,speed,run} are disarmed"
fi

# 7. Disarmed: all three write POSTs answer the SAME 403, before the body is even parsed - which is
#    why a body with an out-of-range time/speed is safe to send while disarmed.
if [ "$PB_ARMED" = "0" ]; then
  for CASE in 'seek:{"time":"2026-08-10T09:30:00"}' 'speed:{"speed":0}' 'run:{"to":"2026-08-10T16:00:00","speed":1}'; do
    VERB="${CASE%%:*}"; BODY="${CASE#*:}"
    post_json "/playback/$VERB" "$BODY"
    if [ "$HTTP_CODE" = "403" ]; then
      R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if "not armed" not in str(d.get("error", "")):
    print("ERR:403 body is %r, expected an armed-file error" % (d,))
else:
    print("OK:disarmed /playback/%s refused" % sys.argv[2])
' "$BODY_FILE" "$VERB")
    else
      R="ERR:http $HTTP_CODE, expected 403 (orders.enabled is absent)"
    fi
    check_result "/playback/$VERB disarmed 403" "$R"
  done
else
  report WARN "/playback/{seek,speed,run} disarmed 403" "orders.enabled is PRESENT - skipping"
fi

# 8. GET /playback/run/<unknown id> is a plain 404, armed or not - it never depends on the flag,
#    only on whether that id was ever queued.
get_json "/playback/run/NT8BRIDGE_NO_SUCH_RUN"
if [ "$HTTP_CODE" = "404" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if "no playback run" not in str(d.get("error", "")):
    print("ERR:404 body is %r" % (d,))
else:
    print("OK:unknown run id -> 404 %r" % (d.get("error"),))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE, expected 404"
fi
check_result "/playback/run/{unknown} 404" "$R"

# 9. DELETE on an unknown run id is the same 404, never a false {"ok":true} for a job that never
#    existed.
delete_json "/playback/run/NT8BRIDGE_NO_SUCH_RUN"
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "DELETE /playback/run/{unknown} 404" "http 404 as expected"
else
  report FAIL "DELETE /playback/run/{unknown} 404" "http $HTTP_CODE, expected 404"
fi
