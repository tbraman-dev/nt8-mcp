# accounts module: GET /account, /executions, /performance - sourced by
# live-smoke.sh, see its header for the helpers. Contract: docs/api/accounts.md.
#
# Read only: nothing here POSTs anything but the return-null probe, and that path does not exist.
# Every check degrades to WARN when the desktop simply has no data (no account, no fills, no trade
# DB). An empty account is a valid state; a broken contract is not.

# A bounded window: a year-wide Execution.DbGet on a busy account can outrun curl's --max-time.
ACCT_FROM="$("$PY" -c 'import datetime; print((datetime.date.today() - datetime.timedelta(days=30)).isoformat())')"
ACCT_TO="$("$PY" -c 'import datetime; print(datetime.date.today().isoformat())')"

# Discover an account name: the first one WITH fills in the window (an empty Sim101 exercises no row
# shape and no pairing - observed on NinjaTrader 8.1.8.2), else Sim101, else the first non-Backtest one, else the first.
# Read only either way: /executions is a SELECT.
get_json "/account"
ACCT=""
if [ "$HTTP_CODE" = "200" ]; then
  ACCT="$("$PY" -c '
import json, sys, urllib.parse, urllib.request
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
if not isinstance(d, list):
    sys.exit(0)
names = [a.get("name") for a in d if isinstance(a, dict) and a.get("name")]
for n in names:
    try:
        u = "%s/executions?account=%s&from=%s&n=1" % (sys.argv[2], urllib.parse.quote(n), sys.argv[3])
        if json.load(urllib.request.urlopen(u, timeout=10)).get("total"):
            print(n)
            sys.exit(0)
    except Exception:
        pass
if "Sim101" in names:
    print("Sim101")
else:
    rest = [n for n in names if n != "Backtest"]
    print((rest or names or [""])[0])
' "$BODY_FILE" "$BASE" "$ACCT_FROM")"
fi

# 1. a position carries unrealized + hasSeenMarketData, and a failed read is null, not 0.
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if not isinstance(d, list):
    print("ERR:/account is not a list: %r" % (d,))
else:
    pos = [p for a in d if isinstance(a, dict) for p in (a.get("positions") or []) if isinstance(p, dict)]
    open_pos = [p for p in pos if "error" not in p]
    missing = [k for k in ("unrealized", "hasSeenMarketData") if open_pos and k not in open_pos[0]]
    if missing:
        print("ERR:position row is missing %r: %r" % (missing, open_pos[0]))
    elif not open_pos:
        print("WARN:no open position on any account - unrealized/hasSeenMarketData unexercised")
    else:
        bad = [p for p in open_pos if p.get("unrealized") == 0 and p.get("hasSeenMarketData") is None]
        if bad:
            print("ERR:unrealized is 0 where the instrument was never read: %r" % (bad[0],))
        else:
            print("OK:%d open position(s), unrealized+hasSeenMarketData present" % len(open_pos))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
case "$R" in
  OK:*)   report PASS "/account position keys" "${R#OK:}" ;;
  WARN:*) report WARN "/account position keys" "${R#WARN:}" ;;
  *)      report FAIL "/account position keys" "${R#ERR:}" ;;
esac

# 2. Unknown account name is a 404 with an error body, not an empty list and not a 500.
get_json "/account?name=NoSuchAccount_NT8Bridge"
if [ "$HTTP_CODE" = "404" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
print("OK:404 %r" % (d.get("error"),) if isinstance(d, dict) and d.get("error") else "ERR:404 with no error key: %r" % (d,))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE, expected 404"
fi
check_result "/account unknown name" "$R"

# 3. Return-null rule: the module owns GET on these paths only. POST must reach the core's 404.
post_json "/executions" ""
if [ "$HTTP_CODE" = "404" ]; then
  R="OK:404"
else
  R="ERR:http $HTTP_CODE, expected 404 (Route_Account must return null for POST)"
fi
check_result "POST /executions 404" "$R"

# 4. /executions without an account is a 400, not an unbounded query over every account.
get_json "/executions"
if [ "$HTTP_CODE" = "400" ]; then
  R="OK:400"
else
  R="ERR:http $HTTP_CODE, expected 400"
fi
check_result "/executions needs account" "$R"

# 5. date guard: a placeholder/out-of-order range is refused before any DB query is armed.
if [ -n "$ACCT" ]; then
  get_json "/executions?account=$ACCT&from=2099-12-01&to=1800-01-01"
  if [ "$HTTP_CODE" = "400" ]; then
    R="OK:400"
  else
    R="ERR:http $HTTP_CODE, expected 400 for an inverted placeholder range"
  fi
  check_result "/executions range guard" "$R"
else
  report WARN "/executions range guard" "no account found in /account"
fi

# 5b. Query-window cap: a from/to wider than 366 days is refused before Execution.DbGet ever runs
#     (a year-wide unbounded query on a busy account is hundreds of MB inside NinjaTrader itself).
if [ -n "$ACCT" ]; then
  WIDE_FROM="$("$PY" -c 'import datetime; print((datetime.date.today() - datetime.timedelta(days=400)).isoformat())')"
  get_json "/executions?account=$ACCT&from=$WIDE_FROM&to=$ACCT_TO"
  if [ "$HTTP_CODE" = "400" ]; then
    R="OK:400"
  else
    R="ERR:http $HTTP_CODE, expected 400 for a 400-day window"
  fi
  check_result "/executions range-width guard" "$R"
else
  report WARN "/executions range-width guard" "no account found in /account"
fi

# 6. /executions shape.
if [ -n "$ACCT" ]; then
  get_json "/executions?account=$ACCT&from=$ACCT_FROM&n=20"
  if [ "$HTTP_CODE" = "200" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
need = ["account","instrument","from","to","source","lookbackDays","total","capped","warnings","executions"]
missing = [k for k in need if k not in d]
ex = d.get("executions")
if missing:
    print("ERR:missing keys %r" % (missing,))
elif not isinstance(ex, list):
    print("ERR:executions is not a list: %r" % (ex,))
elif d.get("source") not in ("db", "memory"):
    print("ERR:source is %r, expected db|memory" % (d.get("source"),))
elif not isinstance(d.get("lookbackDays"), int) or isinstance(d.get("lookbackDays"), bool):
    print("ERR:lookbackDays is not an integer: %r" % (d.get("lookbackDays"),))
elif len(ex) > 20:
    print("ERR:asked for 20 rows, got %d" % len(ex))
elif not ex:
    print("WARN:0 executions in 30 days on any account (source=%s) - row shape unexercised" % d.get("source"))
else:
    row = ex[0]
    rneed = ["id","executionId","account","instrument","side","qty","price","time","commission","fee","position","orderId","orderName"]
    rmiss = [k for k in rneed if k not in row]
    if rmiss:
        print("ERR:execution row is missing %r: %r" % (rmiss, row))
    elif row.get("side") not in ("Buy", "Sell", ""):
        print("ERR:side is %r, expected Buy|Sell|empty" % (row.get("side"),))
    else:
        print("OK:%d row(s), source=%s total=%s capped=%s" % (len(ex), d.get("source"), d.get("total"), d.get("capped")))
' "$BODY_FILE")
  else
    R="ERR:http $HTTP_CODE"
  fi
  case "$R" in
    OK:*)   report PASS "/executions shape" "${R#OK:}" ;;
    WARN:*) report WARN "/executions shape" "${R#WARN:}" ;;
    *)      report FAIL "/executions shape" "${R#ERR:}" ;;
  esac
else
  report WARN "/executions shape" "no account found in /account"
fi

# 7. THE DateTimeKind regression: Execution.DbGet throws on a
#    Local bound, and the only symptom is a silent fall back to the ~3-day memory window. `source`
#    must be the SAME with and without `to`. Both "memory" is a WARN (no trade DB on this desktop);
#    db-with-`to` but memory-without is the regression itself.
if [ -n "$ACCT" ]; then
  get_json "/executions?account=$ACCT&from=$ACCT_FROM&to=$ACCT_TO&n=1"
  SRC_WITH="$("$PY" -c '
import json, sys
try: print(json.load(open(sys.argv[1])).get("source", ""))
except Exception: print("")
' "$BODY_FILE")"
  get_json "/executions?account=$ACCT&from=$ACCT_FROM&n=1"
  SRC_WITHOUT="$("$PY" -c '
import json, sys
try: print(json.load(open(sys.argv[1])).get("source", ""))
except Exception: print("")
' "$BODY_FILE")"
  if [ -z "$SRC_WITH" ] || [ -z "$SRC_WITHOUT" ]; then
    report FAIL "/executions to-less DbGet" "no source key in one of the two responses"
  elif [ "$SRC_WITH" != "$SRC_WITHOUT" ]; then
    report FAIL "/executions to-less DbGet" "source differs: with to=$SRC_WITH, without to=$SRC_WITHOUT (the DateTimeKind regression)"
  elif [ "$SRC_WITH" = "memory" ]; then
    report WARN "/executions to-less DbGet" "both memory - no trade DB answered, so the DB path is unexercised"
  else
    report PASS "/executions to-less DbGet" "source=db with and without to"
  fi
else
  report WARN "/executions to-less DbGet" "no account found in /account"
fi

# 8. /performance is a status document v1: 21 summary keys in order, and the trades sum to netProfit.
if [ -n "$ACCT" ]; then
  get_json "/performance?account=$ACCT&from=$ACCT_FROM"
  if [ "$HTTP_CODE" = "200" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
SUM = ["trades","winners","losers","winRate","netProfit","grossProfit","grossLoss","profitFactor",
       "commission","maxDrawdown","avgTrade","avgWinner","avgLoser","largestWinner","largestLoser",
       "avgMae","avgMfe","avgBarsInTrade","sharpe","maxConsecWinners","maxConsecLosers"]
TR = ["n","side","qty","entryName","exitName","entryTime","exitTime","entryPrice","exitPrice",
      "pnl","pnlPoints","mae","mfe","bars"]
need = ["account","source","lookbackDays","executions","padTrimmed","padDays","capped","warnings","summary","trades","commissionInfo"]
missing = [k for k in need if k not in d]
s, tr = d.get("summary"), d.get("trades")
if missing:
    print("ERR:missing keys %r" % (missing,))
elif not isinstance(tr, list):
    print("ERR:trades is not a list: %r" % (tr,))
elif s is None and not tr:
    print("WARN:no trades in range on this account - summary is null, shape unexercised")
elif not isinstance(s, dict) or list(s.keys()) != SUM:
    print("ERR:summary keys are not status document v1: %r" % (list(s.keys()) if isinstance(s, dict) else s,))
elif tr and list(tr[0].keys())[:14] != TR:
    print("ERR:trade keys are not status document v1: %r" % (list(tr[0].keys()),))
else:
    net = s.get("netProfit")
    pnls = [t.get("pnl") for t in tr if isinstance(t, dict) and isinstance(t.get("pnl"), (int, float))]
    if net is None and tr:
        print("ERR:netProfit is null with %d trades" % len(tr))
    elif d.get("capped"):
        print("OK:%d trade(s) (capped, sum not compared), padTrimmed=%s" % (len(tr), d.get("padTrimmed")))
    elif net is not None and abs(sum(pnls) - net) > 0.01:
        print("ERR:sum(trades[].pnl)=%r != summary.netProfit=%r (padTrimmed=%r)" % (sum(pnls), net, d.get("padTrimmed")))
    else:
        print("OK:%d trade(s), netProfit=%r padTrimmed=%s source=%s" % (len(tr), net, d.get("padTrimmed"), d.get("source")))
' "$BODY_FILE")
  else
    R="ERR:http $HTTP_CODE"
  fi
  case "$R" in
    OK:*)   report PASS "/performance document" "${R#OK:}" ;;
    WARN:*) report WARN "/performance document" "${R#WARN:}" ;;
    *)      report FAIL "/performance document" "${R#ERR:}" ;;
  esac
else
  report WARN "/performance document" "no account found in /account"
fi

# 8b. Pairing must not depend on where the window starts. Observed on NinjaTrader 8.1.8.2: a position held longer
#     than the 2-day pad made its closing fills pair as NEW entries (55 trades / +24583 became 27 / +825,
#     silently). A narrow window must reproduce the wide window's trades, or carry a "not flat" warning.
if [ -n "$ACCT" ] && [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys, urllib.request
wide = json.load(open(sys.argv[1]))
key = lambda t: (t.get("entryTime"), t.get("exitTime"), t.get("side"), t.get("qty"), t.get("pnl"))
tr = [t for t in (wide.get("trades") or []) if isinstance(t, dict) and t.get("exitTime")]
if not tr or wide.get("capped"):
    print("WARN:no closed trades in range (or capped) - pairing unexercised")
else:
    out, pads = "OK", []
    for day in sorted(set(t["exitTime"][:10] for t in tr))[-6:]:      # every exit day, newest six
        u = "%s/performance?account=%s&from=%s" % (sys.argv[2], sys.argv[3], day)
        narrow = json.load(urllib.request.urlopen(u, timeout=30))
        want = sorted(key(t) for t in tr if t["exitTime"] >= narrow["from"])
        got = sorted(key(t) for t in narrow.get("trades") or [])
        pads.append(narrow.get("padDays"))
        if got == want:
            continue
        if any("not flat" in w for w in narrow.get("warnings") or []):
            out = "WARN:from=%s pairs differently (%d vs %d trades) and SAYS so: %s" % (day, len(got), len(want), narrow["warnings"])
        else:
            out = "ERR:from=%s gives %d trade(s), the 30-day window has %d there, and no warning says why" % (day, len(got), len(want))
            break
    print(out if out != "OK" else "OK:%d window start(s) all reproduce the 30-day pairing (padDays %r)" % (len(pads), pads))
' "$BODY_FILE" "$BASE" "$ACCT")
  case "$R" in
    OK:*)   report PASS "/performance pairing" "${R#OK:}" ;;
    WARN:*) report WARN "/performance pairing" "${R#WARN:}" ;;
    *)      report FAIL "/performance pairing" "${R#ERR:}" ;;
  esac
else
  report WARN "/performance pairing" "no account, or no /performance document"
fi

# 9. Commission is never an unlabelled reconstruction.
if [ -n "$ACCT" ]; then
  get_json "/performance?account=$ACCT&from=$ACCT_FROM"
  if [ "$HTTP_CODE" = "200" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
info = d.get("commissionInfo")
tr = d.get("trades") or []
need = ["template","source","total","tradesFromStored","tradesFromTemplate","tradesNoCommission",
        "tradeCommissionTotal","tradeFeeTotal","serverCommissionTotal","serverFeeTotal"]
if not isinstance(info, dict):
    print("ERR:commissionInfo is not an object: %r" % (info,))
elif [k for k in need if k not in info]:
    print("ERR:commissionInfo is missing %r" % ([k for k in need if k not in info],))
elif info.get("source") not in ("stored", "template", "mixed", "none"):
    print("ERR:commissionInfo.source is %r" % (info.get("source"),))
elif tr and info["tradesFromStored"] + info["tradesFromTemplate"] + info["tradesNoCommission"] != len(tr):
    print("ERR:per-trade commission source counts %r do not add up to %d trades" % (
        [info["tradesFromStored"], info["tradesFromTemplate"], info["tradesNoCommission"]], len(tr)))
elif tr and any(t.get("commissionSource") not in ("stored", "template", "none") for t in tr if isinstance(t, dict)):
    print("ERR:a trade carries an unlabelled commission")
else:
    print("OK:source=%s total=%r template=%r (%d trades)" % (
        info.get("source"), info.get("total"), info.get("template"), len(tr)))
' "$BODY_FILE")
  else
    R="ERR:http $HTTP_CODE"
  fi
  check_result "/performance commission" "$R"
else
  report WARN "/performance commission" "no account found in /account"
fi
