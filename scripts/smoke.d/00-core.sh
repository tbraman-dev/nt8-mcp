# core (non-backtest) checks - sourced by live-smoke.sh, see its header for the helpers.

# 1. /health
get_json "/health"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if d.get("ok") is True:
    print("OK:addonVersion=%s nt8Version=%s charts=%s" % (d.get("addonVersion"), d.get("nt8Version"), d.get("charts")))
else:
    print("ERR:ok field is %r" % (d.get("ok"),))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/health" "$R"

# 2. /windows is a list
get_json "/windows"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if isinstance(d, list):
    print("OK:%d windows" % len(d))
else:
    print("ERR:not a list: %r" % (type(d),))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/windows" "$R"

# 3. /charts list, >=1 chart with id, instrument, period, barsCount>0
get_json "/charts"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if not isinstance(d, list) or len(d) == 0:
    print("ERR:not a non-empty list: %r" % (d,))
else:
    ok = [c for c in d if c.get("id") and c.get("instrument") and c.get("period") and (c.get("barsCount") or 0) > 0]
    if ok:
        c = ok[0]
        print("OK:%d charts, e.g. id=%s instrument=%s period=%s barsCount=%s" % (len(d), c["id"], c["instrument"], c["period"], c["barsCount"]))
    else:
        print("ERR:no chart has id+instrument+period+barsCount>0: %r" % (d,))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/charts" "$R"

# 4. /chart/first has tickSize, lastTime, panels
get_json "/chart/first"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
missing = [k for k in ("tickSize", "lastTime", "panels") if k not in d]
if missing:
    print("ERR:missing keys %r" % (missing,))
elif not isinstance(d["panels"], list):
    print("ERR:panels is not a list")
else:
    print("OK:tickSize=%s lastTime=%s panels=%d" % (d["tickSize"], d["lastTime"], len(d["panels"])))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/chart/first" "$R"

# 5. /chart/first/bars?n=3 -> 3 bars oldest-first with numeric o/h/l/c/v
get_json "/chart/first/bars?n=3"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if not isinstance(d, list) or len(d) != 3:
    print("ERR:expected list of 3 bars, got %r" % (d,))
else:
    bad = [b for b in d if not all(isinstance(b.get(k), (int, float)) for k in ("o", "h", "l", "c", "v"))]
    times = [b.get("time") for b in d]
    ordered = times == sorted(times)
    if bad:
        print("ERR:non-numeric ohlcv in %r" % (bad,))
    elif not ordered:
        print("ERR:not oldest-first: times=%r" % (times,))
    else:
        print("OK:3 bars oldest-first, last close=%s" % (d[-1]["c"],))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/chart/first/bars" "$R"

# 6. /chart/first/indicators?n=2 -> each entry name non-empty, panel>=0, inputs object, plots list
get_json "/chart/first/indicators?n=2"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if not isinstance(d, list):
    print("ERR:not a list: %r" % (d,))
elif len(d) == 0:
    print("OK:0 indicators on first chart (vacuously valid)")
else:
    bad = []
    for e in d:
        if not e.get("name"):
            bad.append(("name", e))
        elif not isinstance(e.get("panel"), int) or e["panel"] < 0:
            bad.append(("panel", e))
        elif not isinstance(e.get("inputs"), dict):
            bad.append(("inputs", e))
        elif not isinstance(e.get("plots"), list):
            bad.append(("plots", e))
    if bad:
        print("ERR:bad entries %r" % (bad,))
    else:
        print("OK:%d indicators, e.g. %s (panel %d)" % (len(d), d[0]["name"], d[0]["panel"]))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/chart/first/indicators" "$R"

# 7. /chart/first/drawings is a list
get_json "/chart/first/drawings"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if isinstance(d, list):
    print("OK:%d drawings" % len(d))
else:
    print("ERR:not a list: %r" % (d,))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/chart/first/drawings" "$R"

# 8. /output has lines
get_json "/output?n=200"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if isinstance(d.get("lines"), list):
    print("OK:%d lines%s" % (len(d["lines"]), (" note=%r" % d["note"]) if "note" in d else ""))
else:
    print("ERR:lines is not a list: %r" % (d,))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/output" "$R"

# 9. /account list with a Backtest account present (WARN not FAIL if absent)
get_json "/account"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if not isinstance(d, list):
    print("ERR:not a list: %r" % (d,))
else:
    names = [a.get("name") for a in d]
    if "Backtest" in names:
        print("OK:%d accounts, Backtest present" % len(d))
    else:
        print("WARN:%d accounts, no Backtest account: %r" % (len(d), names))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
case "$R" in
  OK:*)   report PASS "/account" "${R#OK:}" ;;
  WARN:*) report WARN "/account" "${R#WARN:}" ;;
  *)      report FAIL "/account" "${R#ERR:}" ;;
esac

# 10. /log has lines
get_json "/log?n=100"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if isinstance(d.get("lines"), list):
    print("OK:%d lines" % len(d["lines"]))
else:
    print("ERR:lines is not a list: %r" % (d,))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/log" "$R"

# 11. POST /chart/first/screenshot -> ok, path exists and > 10 KB
post_json "/chart/first/screenshot" "{}"
if [ "$HTTP_CODE" = "200" ]; then
  SHOT_PATH="$("$PY" -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("path", "") if d.get("ok") is True else "")
' "$BODY_FILE")"
  if [ -z "$SHOT_PATH" ]; then
    report FAIL "/chart/first/screenshot" "ok!=true or no path in response"
  else
    # Windows path -> forward slashes so Git Bash can stat it.
    SHOT_PATH_FS="$(echo "$SHOT_PATH" | sed 's#\\#/#g')"
    if [ ! -f "$SHOT_PATH_FS" ]; then
      report FAIL "/chart/first/screenshot" "path does not exist on disk: $SHOT_PATH"
    else
      SIZE=$(wc -c < "$SHOT_PATH_FS" | tr -d ' ')
      if [ "$SIZE" -gt 10240 ]; then
        report PASS "/chart/first/screenshot" "path=$SHOT_PATH size=${SIZE}B"
      else
        report FAIL "/chart/first/screenshot" "file too small: ${SIZE}B ($SHOT_PATH)"
      fi
    fi
  fi
else
  report FAIL "/chart/first/screenshot" "http $HTTP_CODE"
fi

# 12. /chart/nope -> 404 JSON error
get_json "/chart/nope"
if [ "$HTTP_CODE" = "404" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if isinstance(d.get("error"), str) and d["error"]:
    print("OK:error=%r" % (d["error"],))
else:
    print("ERR:no error field: %r" % (d,))
' "$BODY_FILE")
else
  R="ERR:expected http 404, got $HTTP_CODE"
fi
check_result "/chart/nope" "$R"

# 13. POST /chart/first/reload -> ok
post_json "/chart/first/reload" ""
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if d.get("ok") is True:
    print("OK:reloaded")
else:
    print("ERR:ok field is %r" % (d.get("ok"),))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/chart/first/reload" "$R"

# 14. /ntstatus has verdict and sourcesScanned
get_json "/ntstatus"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if "verdict" in d and "sourcesScanned" in d:
    print("OK:verdict=%s sourcesScanned=%s" % (d.get("verdict"), d.get("sourcesScanned")))
else:
    print("ERR:missing verdict/sourcesScanned: %r" % (sorted(d.keys()),))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/ntstatus" "$R"
