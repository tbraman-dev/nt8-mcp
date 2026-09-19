# feeds module: GET /feedhealth and GET /connections - sourced by live-smoke.sh,
# see its header for the helpers. Contract: docs/api/feeds.md.
#
# Both endpoints are READ ONLY and need no dispatcher, so nothing here needs a chart, a live feed
# or a connected broker: "found:false", "ageMs":null, an empty connection list and an empty event
# ring are all valid answers below. Nothing here connects, disconnects or subscribes to anything.

# 1. /feedhealth with no ?instruments= must be a 400, not an empty success.
get_json "/feedhealth"
if [ "$HTTP_CODE" = "400" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
err = d.get("error")
if not isinstance(err, str) or not err:
    print("ERR:400 body has no error string: %r" % (d,))
else:
    print("OK:400 %s" % err[:70])
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE, expected 400 for a missing ?instruments="
fi
check_result "/feedhealth needs instruments" "$R"

# 2. A name NinjaTrader cannot know degrades to one row, never a 500.
get_json "/feedhealth?instruments=NT8BRIDGE_NO_SUCH_INSTRUMENT"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
keys = ("instrument", "resolvedName", "found", "hasSeenMarketData", "lastPrice", "lastTickTime", "ageMs", "error")
feeds = d.get("feeds")
if not isinstance(feeds, list):
    print("ERR:feeds is not a list: %r" % (d,))
elif len(feeds) != 1:
    print("ERR:one name in, %d row(s) out" % len(feeds))
elif not isinstance(d.get("nowUtc"), str) or not d["nowUtc"].endswith("Z"):
    print("ERR:nowUtc is %r, expected a ...Z string" % (d.get("nowUtc"),))
else:
    row = feeds[0]
    missing = [k for k in keys if k not in row]
    if missing:
        print("ERR:row missing keys %r: %r" % (missing, row))
    elif row.get("found") is not False:
        print("ERR:a bogus name reported found=%r" % (row.get("found"),))
    elif row.get("ageMs") is not None:
        print("ERR:a bogus name reported ageMs=%r" % (row.get("ageMs"),))
    else:
        print("OK:unknown name -> found=false, ageMs=null (null age means STALE)")
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/feedhealth unknown name" "$R"

# 3. A real instrument, taken off whatever chart is open. ageMs may legitimately be null (nothing is
#    watching that instrument - /feedhealth never subscribes), but it must NEVER be negative, and a
#    non-null ageMs must come with a lastTickTime.
get_json "/charts"
FEED_INST="$("$PY" -c '
import json, sys
try:
    rows = json.load(open(sys.argv[1]))
except Exception:
    rows = []
name = ""
if isinstance(rows, list):
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("instrument"), str) and r["instrument"]:
            name = r["instrument"]
            break
print(name)
' "$BODY_FILE")"
if [ -z "$FEED_INST" ]; then
  report WARN "/feedhealth live instrument" "no chart with an instrument open - skipped"
else
  FEED_Q="$("$PY" -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$FEED_INST")"
  get_json "/feedhealth?instruments=$FEED_Q"
  if [ "$HTTP_CODE" = "200" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
want = sys.argv[2]
feeds = d.get("feeds")
if not isinstance(feeds, list) or len(feeds) != 1:
    print("ERR:expected one feed row, got %r" % (feeds,))
else:
    row = feeds[0]
    age = row.get("ageMs")
    if row.get("instrument") != want:
        print("ERR:asked for %r, row says %r" % (want, row.get("instrument")))
    elif row.get("found") is not True:
        print("ERR:the chart instrument %r reported found=%r" % (want, row.get("found")))
    elif age is not None and (not isinstance(age, int) or isinstance(age, bool)):
        print("ERR:ageMs is not an integer: %r" % (age,))
    elif isinstance(age, int) and age < 0:
        print("ERR:ageMs is negative (%r) - it must clamp to 0" % (age,))
    elif age is not None and not row.get("lastTickTime"):
        print("ERR:ageMs=%r with no lastTickTime" % (age,))
    else:
        print("OK:%s found, hasSeenMarketData=%s ageMs=%s" % (want, row.get("hasSeenMarketData"), age))
' "$BODY_FILE" "$FEED_INST")
  else
    R="ERR:http $HTTP_CODE"
  fi
  check_result "/feedhealth live instrument" "$R"
fi

# 4. /connections shape: the union report plus the anyLiveConnected/anyNonSimConnected predicates and the event ring.
get_json "/connections?n=5"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
keys = ("name", "source", "provider", "canManageOrders", "status", "priceStatus",
        "connected", "dropClass", "inadvertentlyDropped", "live", "nonSim")
conns = d.get("connections")
if not isinstance(conns, list):
    print("ERR:connections is not a list: %r" % (d,))
elif not isinstance(d.get("anyLiveConnected"), bool) or not isinstance(d.get("anyNonSimConnected"), bool):
    print("ERR:anyLiveConnected/anyNonSimConnected are not booleans: %r" % (d,))
elif not isinstance(d.get("events"), list):
    print("ERR:events is not a list: %r" % (d.get("events"),))
elif len(d["events"]) > 5:
    print("ERR:asked for 5 events, got %d" % len(d["events"]))
elif not isinstance(d.get("index"), int) or isinstance(d.get("index"), bool):
    print("ERR:index is not an integer: %r" % (d.get("index"),))
elif not isinstance(d.get("dropped"), int) or isinstance(d.get("dropped"), bool):
    print("ERR:dropped is not an integer: %r" % (d.get("dropped"),))
else:
    bad = [c for c in conns if not isinstance(c, dict) or [k for k in keys if k not in c]]
    sources = set(c.get("source") for c in conns if isinstance(c, dict))
    if bad:
        print("ERR:rows missing keys %r: %r" % (keys, bad[:2]))
    elif sources - set(["configured", "live-only"]):
        print("ERR:unknown source value(s): %r" % (sources,))
    else:
        print("OK:%d connection(s) %r, anyLive=%s events=%d index=%s" % (
            len(conns), sorted(sources), d["anyLiveConnected"], len(d["events"]), d["index"]))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/connections shape" "$R"

# 5. Internal consistency: connected:true requires a status NinjaTrader actually reported, and
#    inadvertentlyDropped:true requires dropClass=="inadvertent" AND connected:false.
get_json "/connections"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
conns = d.get("connections")
if not isinstance(conns, list):
    print("ERR:connections is not a list: %r" % (d,))
else:
    problems = []
    for c in conns:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if c.get("connected") and c.get("status") != "Connected":
            problems.append("%s: connected=true but status=%r" % (name, c.get("status")))
        if c.get("dropClass") not in (None, "connected", "inadvertent", "user"):
            problems.append("%s: dropClass=%r" % (name, c.get("dropClass")))
        want = c.get("dropClass") == "inadvertent" and not c.get("connected")
        if bool(c.get("inadvertentlyDropped")) != want:
            problems.append("%s: inadvertentlyDropped=%r with dropClass=%r connected=%r"
                            % (name, c.get("inadvertentlyDropped"), c.get("dropClass"), c.get("connected")))
        if c.get("live") and not c.get("connected"):
            problems.append("%s: live=true while connected=false" % (name,))
    if problems:
        print("ERR:%s" % "; ".join(problems[:3]))
    else:
        print("OK:%d row(s) internally consistent" % len(conns))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/connections consistency" "$R"

# 6. /connections and /health must not disagree about anyLive - they are the SAME core predicate,
#    and a disagreement means one of them derived it locally instead of reading NinjaTrader's own state
#    (design rule: never re-derive a value NinjaTrader already reports).
get_json "/connections"
FEED_ANYLIVE="$("$PY" -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
v = d.get("anyLiveConnected")
print(v if isinstance(v, bool) else "")
' "$BODY_FILE")"
get_json "/health"
if [ -z "$FEED_ANYLIVE" ] || [ "$HTTP_CODE" != "200" ]; then
  report FAIL "/connections vs /health anyLive" "could not read both (health http $HTTP_CODE, connections anyLive '$FEED_ANYLIVE')"
else
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
mine = sys.argv[2] == "True"
theirs = d.get("anyLive")
if not isinstance(theirs, bool):
    print("ERR:/health.anyLive is %r" % (theirs,))
elif theirs != mine:
    print("ERR:/health.anyLive=%r but /connections.anyLiveConnected=%r" % (theirs, mine))
else:
    print("OK:both say anyLive=%r" % (theirs,))
' "$BODY_FILE" "$FEED_ANYLIVE")
  check_result "/connections vs /health anyLive" "$R"
fi

# 7. subscribed:true - the Connection.ConnectionStatusUpdate "+=" in Start_Feeds took.
#    WARN, not FAIL: the report is still correct without it, but dropClass can never fill.
get_json "/connections?n=1"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if d.get("subscribed") is True:
    print("OK:subscribed to Connection.ConnectionStatusUpdate")
elif d.get("subscribed") is False:
    print("WARN:subscribed is false - Start_Feeds did not hook the status event (see GET /compat); dropClass stays null")
else:
    print("ERR:subscribed is %r" % (d.get("subscribed"),))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
case "$R" in
  OK:*)   report PASS "/connections subscribed" "${R#OK:}" ;;
  WARN:*) report WARN "/connections subscribed" "${R#WARN:}" ;;
  *)      report FAIL "/connections subscribed" "${R#ERR:}" ;;
esac
