# events module: GET /output and GET /nt-log rings - sourced by live-smoke.sh,
# see its header for the helpers. Contract: docs/api/events.md.
# Both endpoints must answer with NO dispatcher hop, so none of these checks needs a chart,
# a window or a strategy: an empty ring is a valid answer everywhere below.

# 1. /output ring shape
get_json "/output?n=5"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if not isinstance(d.get("lines"), list):
    print("ERR:lines is not a list: %r" % (d,))
elif not isinstance(d.get("index"), int) or isinstance(d.get("index"), bool):
    print("ERR:index is not an integer: %r" % (d.get("index"),))
elif not isinstance(d.get("dropped"), int) or isinstance(d.get("dropped"), bool):
    print("ERR:dropped is not an integer: %r" % (d.get("dropped"),))
elif not isinstance(d.get("subscribed"), bool):
    print("ERR:subscribed is not a bool: %r" % (d.get("subscribed"),))
elif d.get("source") != "ring":
    print("ERR:source is %r, expected ring" % (d.get("source"),))
elif len(d["lines"]) > 5:
    print("ERR:asked for 5 lines, got %d" % len(d["lines"]))
else:
    print("OK:%d lines, index=%s dropped=%s subscribed=%s%s" % (len(d["lines"]), d["index"], d["dropped"], d["subscribed"], (" note=%r" % d["note"]) if "note" in d else ""))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/output ring" "$R"

# 2. /output cursor: since=<index of the newest line> must not hand the same lines back.
#    A sequence-number cursor keeps working after the ring wraps; a slot index does not.
get_json "/output?n=1"
EV_INDEX="$("$PY" -c '
import json, sys
d = json.load(open(sys.argv[1]))
i = d.get("index")
print(i if isinstance(i, int) and not isinstance(i, bool) else "")
' "$BODY_FILE")"
if [ -z "$EV_INDEX" ]; then
  report FAIL "/output cursor" "no usable index in /output?n=1"
else
  get_json "/output?since=$EV_INDEX&n=50"
  if [ "$HTTP_CODE" = "200" ]; then
    # NOT pycheck: that helper forwards one argument only, and this check needs the cursor as argv[2]
    # (observed: IndexError -> FAIL on a healthy ring).
    R=$("$PY" -c '
import json, sys
d = json.load(open(sys.argv[1]))
since = int(sys.argv[2])
if not isinstance(d.get("lines"), list):
    print("ERR:lines is not a list: %r" % (d,))
elif not isinstance(d.get("index"), int) or isinstance(d.get("index"), bool):
    print("ERR:index is not an integer: %r" % (d.get("index"),))
elif d["index"] < since:
    print("ERR:index went backwards: %s < %s" % (d["index"], since))
elif len(d["lines"]) > d["index"] - since:
    print("ERR:index advanced %s but %d lines came back - the cursor is not a sequence number" % (d["index"] - since, len(d["lines"])))
elif len(d["lines"]) > 50:
    print("ERR:asked for 50 lines, got %d" % len(d["lines"]))
else:
    print("OK:since=%s -> %d new line(s), index=%s" % (since, len(d["lines"]), d["index"]))
' "$BODY_FILE" "$EV_INDEX" 2>&1)
  else
    R="ERR:http $HTTP_CODE"
  fi
  check_result "/output cursor" "$R"
fi

# 3. /output?tab=1 returns only tab-1 lines (each line is tagged "[<tab>] ...")
get_json "/output?tab=1&n=50"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
lines = d.get("lines")
if not isinstance(lines, list):
    print("ERR:lines is not a list: %r" % (d,))
else:
    bad = [x for x in lines if not str(x).startswith("[1] ")]
    if bad:
        print("ERR:tab=1 returned non-tab-1 lines: %r" % (bad[:3],))
    else:
        print("OK:%d tab-1 line(s)" % len(lines))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/output tab filter" "$R"

# 3b. /output?tab=3 (NT8 has exactly two Output tabs) must fail CLOSED: empty lines, not both tabs.
get_json "/output?tab=3&n=50"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
lines = d.get("lines")
if not isinstance(lines, list):
    print("ERR:lines is not a list: %r" % (d,))
elif lines:
    print("ERR:tab=3 is out of range but returned %d line(s): %r" % (len(lines), lines[:3]))
else:
    print("OK:tab=3 returned no lines (fails closed)")
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/output out-of-range tab fails closed" "$R"

# 4. /nt-log ring shape and entry keys
get_json "/nt-log?n=5"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
keys = ("t", "level", "category", "name", "resource", "msg")
entries = d.get("entries")
if not isinstance(entries, list):
    print("ERR:entries is not a list: %r" % (d,))
elif not isinstance(d.get("index"), int) or isinstance(d.get("index"), bool):
    print("ERR:index is not an integer: %r" % (d.get("index"),))
elif d.get("source") != "ring":
    print("ERR:source is %r, expected ring" % (d.get("source"),))
elif len(entries) > 5:
    print("ERR:asked for 5 entries, got %d" % len(entries))
else:
    bad = [e for e in entries if not isinstance(e, dict) or [k for k in keys if k not in e]]
    if bad:
        print("ERR:entries missing keys %r: %r" % (keys, bad[:2]))
    else:
        print("OK:%d entries, index=%s dropped=%s subscribed=%s" % (len(entries), d["index"], d.get("dropped"), d.get("subscribed")))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/nt-log ring" "$R"

# 5. subscribed:true - the Cbi.Log.LogEvent "+=" in Start_Events really took.
#    WARN, not FAIL: a bridge that just rebound on a retry may report an empty ring for a while,
#    but "subscribed" itself is a fact and false here means /nt-log can never fill.
get_json "/nt-log?n=1"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if d.get("subscribed") is True:
    print("OK:subscribed to Cbi.Log.LogEvent")
elif d.get("subscribed") is False:
    print("WARN:subscribed is false - Start_Events did not hook Cbi.Log.LogEvent (see GET /compat)")
else:
    print("ERR:subscribed is %r" % (d.get("subscribed"),))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
case "$R" in
  OK:*)   report PASS "/nt-log subscribed" "${R#OK:}" ;;
  WARN:*) report WARN "/nt-log subscribed" "${R#WARN:}" ;;
  *)      report FAIL "/nt-log subscribed" "${R#ERR:}" ;;
esac

# 6. /nt-log?level=Error returns only Error entries (vacuously true on a clean desktop)
get_json "/nt-log?level=Error&n=50"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
entries = d.get("entries")
if not isinstance(entries, list):
    print("ERR:entries is not a list: %r" % (d,))
else:
    bad = [e.get("level") for e in entries if isinstance(e, dict) and e.get("level") != "Error"]
    if bad:
        print("ERR:level=Error returned other levels: %r" % (bad[:3],))
    else:
        print("OK:%d Error entr(y|ies)" % len(entries))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/nt-log level filter" "$R"
