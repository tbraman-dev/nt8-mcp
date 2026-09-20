# data module checks (/data/coverage, /data/probe, /data/download) - sourced by live-smoke.sh,
# see its header.
#
# READ-ONLY. /data/download and /data/probe carry no arming file (a download moves no money),
# so this file only ever sends requests that 400 on validation before reaching the real guards -
# it never queues a job or spends the connected provider's bandwidth.

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
elif not isinstance(d.get("cache"), dict) or "series" not in d["cache"]:
    print("ERR:cache section missing or malformed: %r" % (d.get("cache"),))
else:
    bad = [k for k, v in stores.items()
           if v.get("scanned") is False and v.get("days") is not None]
    if bad:
        print("ERR:scanned:false store(s) %r report a days map instead of null" % (bad,))
    else:
        print("OK:%s cache.series=%d note=%r" % (
            " ".join("%s=%s/%s" % (k, v.get("scanned"), v.get("files")) for k, v in sorted(stores.items())),
            len(d["cache"].get("series") or {}),
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

# 7. POST /data/download with a range past the hard cap (400) even with {"big":true} -> always a
#    validation failure, before Data_Exposure/AnyNonSimConnected are ever consulted, so this is
#    safe regardless of what is connected or armed on this machine.
post_json "/data/download" "{\"instrument\":\"$DATA_INST\",\"from\":\"20200101\",\"to\":\"20260101\",\"kinds\":[\"replay\"],\"big\":true}"
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "POST /data/download over the hard cap" "http 400 as expected"
else
  report FAIL "POST /data/download over the hard cap" "expected http 400, got $HTTP_CODE body=$(cat "$BODY_FILE")"
fi

# 8. POST /data/download with an unknown instrument -> 400 (validation, before any live guard)
post_json "/data/download" "{\"instrument\":\"NOSUCHTHING 12-26\",\"from\":\"20260101\",\"to\":\"20260102\",\"kinds\":[\"replay\"]}"
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "POST /data/download unknown instrument" "http 400 as expected"
else
  report FAIL "POST /data/download unknown instrument" "expected http 400, got $HTTP_CODE body=$(cat "$BODY_FILE")"
fi

# 9. /data/probe without instrument -> 400
get_json "/data/probe"
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "/data/probe no instrument" "http 400 as expected"
else
  report FAIL "/data/probe no instrument" "expected http 400, got $HTTP_CODE"
fi

# 10. /data/probe with a bad kind -> 400 (kind is required and must be minute|tick|day, unlike coverage)
get_json "/data/probe?instrument=$DATA_INST_Q&kind=replay"
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "/data/probe kind=replay" "http 400 as expected"
else
  report FAIL "/data/probe kind=replay" "expected http 400, got $HTTP_CODE"
fi

# 11. GET /data/download/<unknown id> -> 404 (a path the module owns, a resource it does not)
get_json "/data/download/d999"
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "/data/download/d999" "http 404 as expected"
else
  report FAIL "/data/download/d999" "expected http 404, got $HTTP_CODE"
fi

# 11b. GET /data/probe/<unknown id> -> 404 (probe polling is a path this module owns too)
get_json "/data/probe/p999"
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "/data/probe/p999" "http 404 as expected"
else
  report FAIL "/data/probe/p999" "expected http 404, got $HTTP_CODE"
fi

# 12. a path under /data the module does not own falls through to the core's 404
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
