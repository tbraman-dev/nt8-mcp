# chartcontrol module: POST /chart/{id}/indicator/{add,remove}, /series, /scroll - sourced by
# live-smoke.sh, see its header for the helpers. Contract: docs/api/chartcontrol.md.
#
# Assumes at least one chart is open (00-core.sh already assumes this for /chart/first).
# READ-ONLY ON PURPOSE: this file sends only requests that must be REFUSED, so it never changes a
# chart. Adding and removing a real indicator is a by-hand check in docs/api/chartcontrol.md: a
# smoke run must never leave something on a user's chart.

# 1. Unknown indicator name -> 400, chart unchanged.
post_json "/chart/first/indicator/add" '{"indicator":"NotARealIndicator_xyz"}'
if [ "$HTTP_CODE" = "400" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if isinstance(d.get("error"), str) and d["error"]:
    print("OK:error=%r" % (d["error"],))
else:
    print("ERR:no error field: %r" % (d,))
' "$BODY_FILE")
else
  R="ERR:expected http 400, got $HTTP_CODE"
fi
check_result "/chart/first/indicator/add unknown indicator" "$R"

# 2. Remove an indicator this run never added, without force -> refused (400), nothing removed.
#    Only meaningful if the chart already carries some other indicator; WARN (not FAIL) otherwise.
get_json "/chart/first/indicators"
OTHER_NAME="$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
others = [i.get("name") for i in d if i.get("name")]
print(others[0] if others else "")
' "$BODY_FILE" 2>/dev/null)"
if [ -n "$OTHER_NAME" ]; then
  post_json "/chart/first/indicator/remove" "{\"indicator\":\"$OTHER_NAME\"}"
  if [ "$HTTP_CODE" = "400" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if "force" in (d.get("error") or ""):
    print("OK:refused without force: %r" % (d["error"],))
else:
    print("ERR:400 but wrong reason: %r" % (d,))
' "$BODY_FILE")
  else
    R="ERR:expected http 400, got $HTTP_CODE (did this remove someone else's indicator?)"
  fi
  check_result "/chart/first/indicator/remove refuses a not-mine indicator" "$R"
else
  report WARN "/chart/first/indicator/remove refuses a not-mine indicator" "chart has no other indicator to test ownership against"
fi

# 3. /series with an unknown instrument -> 400, and never touches a chart with ANY strategy on it.
get_json "/chart/first"
HAS_STRATEGY="$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
print("1" if d.get("strategies") else "0")
' "$BODY_FILE" 2>/dev/null)"
if [ "$HAS_STRATEGY" = "1" ]; then
  report WARN "/chart/first/series unknown instrument" "chart has a strategy attached; skipping series checks entirely"
else
  post_json "/chart/first/series" '{"instrument":"NotAnInstrument_xyz"}'
  if [ "$HTTP_CODE" = "400" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if isinstance(d.get("error"), str) and "NotAnInstrument_xyz" in d["error"]:
    print("OK:error=%r" % (d["error"],))
else:
    print("ERR:400 but wrong body: %r" % (d,))
' "$BODY_FILE")
  else
    R="ERR:expected http 400, got $HTTP_CODE"
  fi
  check_result "/chart/first/series unknown instrument" "$R"
fi

# 6. /scroll to the chart's OWN last bar time -> that time stays inside the visible window.
get_json "/chart/first"
LAST_TIME="$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("lastTime") or "")
' "$BODY_FILE" 2>/dev/null)"
if [ -z "$LAST_TIME" ]; then
  report WARN "/chart/first/scroll" "chart has no bars yet (lastTime is null)"
else
  post_json "/chart/first/scroll" "{\"time\":\"$LAST_TIME\"}"
  if [ "$HTTP_CODE" = "200" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
target = sys.argv[2]
first_v, last_v = d.get("firstVisibleTime"), d.get("lastVisibleTime")
if not first_v or not last_v:
    print("ERR:no visible-time fields: %r" % (d,))
elif not (first_v <= target <= last_v):
    print("ERR:%s not inside visible window [%s, %s]" % (target, first_v, last_v))
else:
    print("OK:visible window [%s, %s] contains %s" % (first_v, last_v, target))
' "$BODY_FILE" "$LAST_TIME")
  else
    R="ERR:http $HTTP_CODE"
  fi
  check_result "/chart/first/scroll to last bar" "$R"
fi

# 7. A standing modal refuses all four paths. This file does not open one itself (nothing here can
#    close it again cleanly on every NT8 version) - it only documents the expectation:
report WARN "/chart/*/{indicator,series,scroll} refused under a modal" "not exercised by this script - open a MessageBox by hand and re-POST any of the four paths above; expect 409 with standingModal set"
