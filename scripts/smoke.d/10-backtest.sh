# needs: -b
# Backtest checks - a full SampleMACrossOver run, several minutes. Sourced by live-smoke.sh.

echo "== Backtest checks (-b) =="
echo

# 14. /strategies contains SampleMACrossOver (ships with NinjaTrader)
get_json "/strategies"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
names = {s.get("name") for s in d} if isinstance(d, list) else set()
need = {"SampleMACrossOver"}
missing = need - names
if missing:
    print("ERR:missing %r (have %r)" % (missing, sorted(names)))
else:
    print("OK:%d strategies, SampleMACrossOver present" % len(names))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/strategies" "$R"

# 15. POST /backtest with account Sim101 -> 400
post_json "/backtest" '{"strategy":"SampleMACrossOver","instrument":"ES 12-26","barsPeriod":{"type":"Minute","value":5},"from":"2026-09-10","to":"2026-09-17","account":"Sim101"}'
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "/backtest account=Sim101" "http 400 as expected"
else
  report FAIL "/backtest account=Sim101" "expected http 400, got $HTTP_CODE"
fi

# 16. POST /backtest with unknown input name -> 400
post_json "/backtest" '{"strategy":"SampleMACrossOver","instrument":"ES 12-26","barsPeriod":{"type":"Minute","value":5},"from":"2026-09-10","to":"2026-09-17","inputs":{"NotARealInput_xyz":1}}'
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "/backtest unknown input" "http 400 as expected"
else
  report FAIL "/backtest unknown input" "expected http 400, got $HTTP_CODE"
fi

# 17. POST SampleMACrossOver full run -> 202, poll to completion
post_json "/backtest" '{"strategy":"SampleMACrossOver","instrument":"ES 12-26","barsPeriod":{"type":"Minute","value":5},"from":"2026-09-10","to":"2026-09-17","tickReplay":false}'
BT_ID=""
if [ "$HTTP_CODE" = "202" ]; then
  BT_ID=$("$PY" -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("id", ""))
' "$BODY_FILE")
fi
if [ -z "$BT_ID" ]; then
  report FAIL "POST /backtest run" "expected http 202 with id, got $HTTP_CODE body=$(cat "$BODY_FILE")"
else
  report PASS "POST /backtest run" "queued id=$BT_ID"
  DEADLINE=$((SECONDS + 300))
  STATE="queued"
  while [ "$SECONDS" -lt "$DEADLINE" ]; do
    sleep 5
    get_json "/backtest/$BT_ID"
    STATE=$("$PY" -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("state", ""))
' "$BODY_FILE")
    case "$STATE" in
      queued|running) continue ;;
      *) break ;;
    esac
  done
  if [ "$STATE" = "queued" ] || [ "$STATE" = "running" ]; then
    report FAIL "GET /backtest/$BT_ID" "timed out after 5 min, still $STATE"
  else
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
state = d.get("state")
trades = (d.get("summary") or {}).get("trades") if d.get("summary") else None
if state == "done" and isinstance(trades, (int, float)):
    print("OK:state=done trades=%s summary=%s" % (trades, json.dumps(d.get("summary"))))
else:
    print("ERR:state=%s summary=%r" % (state, d.get("summary")))
' "$BODY_FILE")
    check_result "GET /backtest/$BT_ID" "$R"
    # 17b. the finished document says which bars REALLY ran
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
missing = [k for k in ("barsFrom", "barsTo", "warnings") if k not in d]
if missing or not isinstance(d.get("warnings"), list):
    print("ERR:missing %r warnings=%r" % (missing, d.get("warnings")))
else:
    print("OK:barsFrom=%s barsTo=%s warnings=%d" % (d["barsFrom"], d["barsTo"], len(d["warnings"])))
' "$BODY_FILE")
    check_result "GET /backtest/$BT_ID loaded window keys" "$R"

    # 17c. equity is always an array, outputNote is present
    # (null on a clean capture — SampleMACrossOver does not Print()), and a real 76-trade run is not
    # "not meaningful": sharpe/profitFactor should be numbers here, not null.
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
eq = d.get("equity")
s = d.get("summary") or {}
if "outputNote" not in d:
    print("ERR:no outputNote key")
elif not isinstance(eq, list):
    print("ERR:equity is %r" % (eq,))
elif eq and not ("time" in eq[0] and "cumulativeNetProfit" in eq[0]):
    print("ERR:equity row missing keys: %r" % (eq[0],))
elif s.get("trades", 0) >= 2 and s.get("sharpe") is None:
    print("ERR:sharpe null with %s trades" % s.get("trades"))
else:
    print("OK:equity rows=%d outputNote=%r sharpe=%r" % (len(eq), d.get("outputNote"), s.get("sharpe")))
' "$BODY_FILE")
    check_result "GET /backtest/$BT_ID equity/outputNote" "$R"
  fi

  # 18. GET /backtests lists it
  get_json "/backtests"
  R=$(pycheck "
import json, sys
d = json.load(open(sys.argv[1]))
ids = [b.get('id') for b in d] if isinstance(d, list) else []
if '$BT_ID' in ids:
    print('OK:found among %d backtests' % len(ids))
else:
    print('ERR:id $BT_ID not in %r' % (ids,))
" "$BODY_FILE")
  check_result "/backtests" "$R"

  # 19. DELETE it -> ok
  delete_json "/backtest/$BT_ID"
  if [ "$HTTP_CODE" = "200" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if d.get("ok") is True:
    print("OK:deleted")
else:
    print("ERR:ok field is %r" % (d.get("ok"),))
' "$BODY_FILE")
  else
    R="ERR:http $HTTP_CODE"
  fi
  check_result "DELETE /backtest/$BT_ID" "$R"
fi

# ── instrument resolution & the never-silently-done fixes ────────────

# 13b. Unknown instrument -> 400 at request time, never a queued job.
post_json "/backtest" '{"strategy":"SampleMACrossOver","instrument":"NotAnInstrument_xyz","barsPeriod":{"type":"Minute","value":5},"from":"2026-09-10","to":"2026-09-17"}'
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "/backtest unknown instrument" "http 400 as expected"
else
  report FAIL "/backtest unknown instrument" "expected http 400, got $HTTP_CODE"
fi

# 13c. A continuous-contract style instrument name -> 400 naming the problem, not a silent 0-trade
# "done" ("ES" and "ES ##-##" both resolve and both would otherwise silently produce barsFrom:null).
post_json "/backtest" '{"strategy":"SampleMACrossOver","instrument":"ES ##-##","barsPeriod":{"type":"Minute","value":5},"from":"2026-09-10","to":"2026-09-17"}'
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "/backtest continuous-contract instrument" "http 400 as expected"
else
  report FAIL "/backtest continuous-contract instrument" "expected http 400, got $HTTP_CODE body=$(cat "$BODY_FILE")"
fi

# ── templates / settings ──────────────────────────────────────────────────────

# 20. GET /templates?strategy= -> 200 with the three keys. `templates` may be a list (possibly
#     empty) or null + a note; both are valid answers, [] for an unreadable folder is not.
get_json "/templates?strategy=SampleMACrossOver"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
missing = [k for k in ("strategy", "folder", "templates") if k not in d]
t = d.get("templates")
if missing:
    print("ERR:missing keys %r" % (missing,))
elif t is None and not d.get("note"):
    print("ERR:templates null without a note")
elif t is not None and not isinstance(t, list):
    print("ERR:templates is %r" % (type(t).__name__,))
else:
    print("OK:folder=%r templates=%r" % (d.get("folder"), t))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/templates" "$R"

# 21. GET /templates without ?strategy= -> 400 (never an empty list)
get_json "/templates"
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "/templates no strategy" "http 400 as expected"
else
  report FAIL "/templates no strategy" "expected http 400, got $HTTP_CODE"
fi

# 22. GET /templates for a strategy that does not exist -> 400
get_json "/templates?strategy=NotAStrategy_xyz"
if [ "$HTTP_CODE" = "400" ]; then
  report PASS "/templates unknown strategy" "http 400 as expected"
else
  report FAIL "/templates unknown strategy" "expected http 400, got $HTTP_CODE"
fi

# 23-26. Every bad setting is refused BEFORE the job is armed: 400, never a queued 202.
#        (tickReplay + High is the one NinjaTrader itself refuses.)
BT_BASE='"strategy":"SampleMACrossOver","instrument":"ES 12-26","barsPeriod":{"type":"Minute","value":5},"from":"2026-09-10","to":"2026-09-17"'
for bad in \
  '"tickReplay":true,"fillResolution":"High"|tickReplay+High' \
  '"fillResolution":"Bogus"|bad fillResolution' \
  '"slippageTicks":-1|negative slippage' \
  '"fillResolutionValue":0|fillResolutionValue 0' \
  '"maxTrades":-5|negative maxTrades' \
  '"commissionTemplate":"NT8Bridge_NoSuchTemplate_xyz"|unknown commissionTemplate' \
  '"template":"NotATemplate_xyz"|unknown template' ; do
  FRAG="${bad%%|*}"
  NAME="${bad##*|}"
  post_json "/backtest" "{$BT_BASE,$FRAG}"
  if [ "$HTTP_CODE" = "400" ]; then
    report PASS "/backtest $NAME" "http 400 as expected"
  else
    report FAIL "/backtest $NAME" "expected http 400, got $HTTP_CODE body=$(cat "$BODY_FILE")"
  fi
done

# 27. A real run carrying settings: the status document must echo what it RAN with, and maxTrades
#     must trim trades[] only -- summary keeps counting every trade.
post_json "/backtest" "{$BT_BASE,\"tickReplay\":false,\"slippageTicks\":2,\"maxTrades\":3}"
BT_ID2=""
if [ "$HTTP_CODE" = "202" ]; then
  BT_ID2=$("$PY" -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("id", ""))
' "$BODY_FILE")
fi
if [ -z "$BT_ID2" ]; then
  report FAIL "POST /backtest settings" "expected http 202 with id, got $HTTP_CODE body=$(cat "$BODY_FILE")"
else
  report PASS "POST /backtest settings" "queued id=$BT_ID2"
  DEADLINE=$((SECONDS + 300))
  STATE="queued"
  while [ "$SECONDS" -lt "$DEADLINE" ]; do
    sleep 5
    get_json "/backtest/$BT_ID2"
    STATE=$("$PY" -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("state", ""))
' "$BODY_FILE")
    case "$STATE" in
      queued|running) continue ;;
      *) break ;;
    esac
  done
  if [ "$STATE" = "queued" ] || [ "$STATE" = "running" ]; then
    report FAIL "GET /backtest/$BT_ID2" "timed out after 5 min, still $STATE"
  else
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
s = d.get("settings")
if d.get("state") != "done":
    print("ERR:state=%s error=%r" % (d.get("state"), d.get("error")))
elif not isinstance(s, dict):
    print("ERR:settings is %r" % (s,))
elif s.get("slippageTicks") != 2:
    print("ERR:settings.slippageTicks=%r, asked 2" % (s.get("slippageTicks"),))
elif s.get("maxTrades") != 3:
    print("ERR:settings.maxTrades=%r, asked 3" % (s.get("maxTrades"),))
elif s.get("fillResolution") is None or s.get("includeTradeHistory") is None:
    print("ERR:settings not read back off the run: %s" % json.dumps(s))
elif len(d.get("trades") or []) > 3:
    print("ERR:maxTrades=3 but trades[] has %d" % len(d["trades"]))
elif ((d.get("summary") or {}).get("trades") or 0) < len(d.get("trades") or []):
    print("ERR:summary.trades=%r < len(trades)=%d" % ((d.get("summary") or {}).get("trades"), len(d.get("trades") or [])))
else:
    print("OK:settings=%s trades=%d of summary %s" % (json.dumps(s), len(d.get("trades") or []), (d.get("summary") or {}).get("trades")))
' "$BODY_FILE")
    check_result "GET /backtest/$BT_ID2 settings" "$R"
  fi
  delete_json "/backtest/$BT_ID2"
fi
