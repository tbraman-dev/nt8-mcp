# data module checks (/data/coverage, /data/download) - sourced by live-smoke.sh, see its header.
#
# READ-ONLY and DISARMED. Nothing here downloads anything: the download checks prove the
# refusals (403 unarmed, 400 validation) and never create data.download.enabled. If that flag
# happens to be armed on this machine, the 403 check reports WARN instead of FAIL and the run
# still tells the truth about which guard answered.

echo "== Data module checks =="
echo

DATA_INST="ES 12-26"
DATA_INST_Q="ES%2012-26"

# 1. /data/coverage for the ES front month: scanned stores, the continuous name also scanned
get_json "/data/coverage?instrument=$DATA_INST_Q"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
stores = d.get("stores") or {}
missing = [k for k in ("tick", "minute", "day", "replay") if k not in stores]
if missing:
    print("ERR:stores missing %r (have %r)" % (missing, sorted(stores)))
elif not isinstance(d.get("resolved"), list) or d.get("instrument") != "ES 12-26":
    print("ERR:instrument=%r resolved=%r" % (d.get("instrument"), d.get("resolved")))
elif "ES ##-##" not in d["resolved"]:
    print("ERR:continuous name not scanned: resolved=%r" % (d["resolved"],))
else:
    bad = [k for k, v in stores.items()
           if v.get("scanned") is False and v.get("days") is not None]
    if bad:
        print("ERR:scanned:false store(s) %r report a days map instead of null" % (bad,))
    else:
        print("OK:%s note=%r" % (
            " ".join("%s=%s/%s" % (k, v.get("scanned"), v.get("files")) for k, v in sorted(stores.items())),
            (d.get("note") or "")[:40]))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/data/coverage" "$R"

# 2. the tick store granularity is hour and the day store is per YEAR (not per day)
get_json "/data/coverage?instrument=$DATA_INST_Q"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
s = d.get("stores") or {}
g = {k: (s.get(k) or {}).get("granularity") for k in ("tick", "minute", "day", "replay")}
want = {"tick": "hour", "minute": "day", "day": "year", "replay": "day"}
if g == want:
    print("OK:%r" % (g,))
else:
    print("ERR:granularity %r != %r" % (g, want))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/data/coverage granularity" "$R"

# 3. coverage with kind= narrows to one store
get_json "/data/coverage?instrument=$DATA_INST_Q&kind=replay"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
s = sorted((d.get("stores") or {}))
print("OK:only %r" % (s,) if s == ["replay"] else "ERR:kind=replay returned %r" % (s,))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/data/coverage?kind=replay" "$R"

# 4. coverage without instrument -> 400
get_json "/data/coverage"
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "/data/coverage no instrument" "http 400 as expected"
else
  report FAIL "/data/coverage no instrument" "expected http 400, got $HTTP_CODE"
fi

# 5. coverage with a bad kind -> 400
get_json "/data/coverage?instrument=$DATA_INST_Q&kind=nonsense"
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "/data/coverage bad kind" "http 400 as expected"
else
  report FAIL "/data/coverage bad kind" "expected http 400, got $HTTP_CODE"
fi

# 6. coverage with an inverted range (from after to) -> 400, RangeProblem
get_json "/data/coverage?instrument=$DATA_INST_Q&from=20260918&to=20260901"
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "/data/coverage inverted range" "http 400 as expected"
else
  report FAIL "/data/coverage inverted range" "expected http 400, got $HTTP_CODE"
fi

# 7. the arming flag's state is visible in /compat (Data.downloadFlag)
get_json "/compat"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
rows = {r.get("key"): r for r in (d.get("compat") or []) if isinstance(r, dict)}
row = rows.get("Data.downloadFlag")
if row is None:
    print("ERR:no Data.downloadFlag row in /compat (have %r)" % (sorted(rows),))
elif "Route_Data" not in (d.get("routes") or []):
    print("ERR:Route_Data not discovered: routes=%r" % (d.get("routes"),))
else:
    print("OK:armed=%s detail=%r" % (row.get("resolved"), row.get("detail")))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/compat Data.downloadFlag" "$R"

# 8. POST /data/download while DISARMED -> 403. This smoke run never creates the flag; if the
#    user left it armed the module answers a later guard instead, which is a WARN, not a FAIL.
post_json "/data/download" "{\"instrument\":\"$DATA_INST\",\"from\":\"20260101\",\"to\":\"20260102\",\"kinds\":[\"replay\"]}"
case "$HTTP_CODE" in
  403)
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
print("OK:%r" % (d.get("error"),) if d.get("error") == "data download not enabled"
      else "ERR:403 body is %r" % (d,))
' "$BODY_FILE")
    check_result "POST /data/download disarmed" "$R"
    ;;
  409)
    report WARN "POST /data/download disarmed" "armed on this machine; refused by a live/provider guard (409) - flag is NOT created here"
    ;;
  202)
    # Armed on this machine after all: a real 2-day ES replay is now queued on the download
    # worker. Cancel it immediately so this read-only run does not spend provider bandwidth or
    # write NinjaTrader's data store (this file's own header promise, line 3).
    ID=$(pycheck '
import json, sys
print((json.load(open(sys.argv[1])) or {}).get("id") or "")
' "$BODY_FILE")
    if [ -n "$ID" ]; then delete_json "/data/download/$ID"; fi
    report FAIL "POST /data/download disarmed" "a job was QUEUED (id=${ID:-?}) and has been cancelled - the arming flag is present and fresh; delete bin\\Custom\\AddOns\\data.download.enabled"
    ;;
  *)
    report FAIL "POST /data/download disarmed" "expected http 403, got $HTTP_CODE body=$(cat "$BODY_FILE")"
    ;;
esac

# 9. GET /data/download/<unknown id> -> 404 (a path the module owns, a resource it does not)
get_json "/data/download/d999"
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "/data/download/d999" "http 404 as expected"
else
  report FAIL "/data/download/d999" "expected http 404, got $HTTP_CODE"
fi

# 10. a path under /data the module does not own falls through to the core's 404
get_json "/data/nonsense"
if [ "$HTTP_CODE" = "404" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
print("OK:%r" % (d.get("error"),) if "no route for" in (d.get("error") or "")
      else "ERR:expected the core 404, got %r" % (d,))
' "$BODY_FILE")
else
  R="ERR:expected http 404, got $HTTP_CODE"
fi
check_result "/data/nonsense" "$R"
