# orders module: GET /orders/status, POST /orders/{submit,change,cancel} - sourced by
# live-smoke.sh, see its header for the helpers. Contract: docs/api/orders.md.
#
# THIS FILE NEVER ARMS THE MODULE AND NEVER SUBMITS, CHANGES OR CANCELS ANYTHING.
# It never creates orders.enabled and never creates orders.config.json. The module ships DISARMED
# and the default live-smoke run's job is to prove exactly that: every /orders/* path answers 403
# and the endpoint list is not advertised. The three POSTs below are made ONLY while the 403 is in
# force, so there is no state to change. If the module IS armed (someone left orders.enabled in
# bin\Custom\AddOns), this file stops POSTing and reports what it found instead - a smoke test is
# not a reason to send an order to a trading account, however simulated.

ADDONS_DIR="$HOME/Documents/NinjaTrader 8/bin/Custom/AddOns"

# 1. Is the module armed right now? This decides what the rest of this file is allowed to do, and a
#    leftover flag is a finding on its own: this module can OPEN a position, so a forgotten
#    orders.enabled is a loaded gun until its 24 h age rule expires.
ORD_ARMED=0
if [ -e "$ADDONS_DIR/orders.enabled" ]; then
  ORD_ARMED=1
  report FAIL "orders.enabled absent" "orders.enabled EXISTS in bin\\Custom\\AddOns - order entry may be ARMED. Delete it when the trial is over."
else
  report PASS "orders.enabled absent" "no orders.enabled - order entry is disarmed"
fi

# 2. GET /orders/status is 403 while disarmed, and says nothing else: no flag ages, no account
#    names, no hint about what would have happened.
get_json "/orders/status"
if [ "$ORD_ARMED" = "0" ]; then
  if [ "$HTTP_CODE" = "403" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if d.get("error") != "orders module not armed":
    print("ERR:403 body is %r, expected {\"error\":\"orders module not armed\"}" % (d,))
elif len(d) != 1:
    print("ERR:the unarmed 403 leaks extra keys: %r" % (sorted(d),))
else:
    print("OK:disarmed - GET /orders/status is 403 and says nothing else")
' "$BODY_FILE")
  else
    R="ERR:http $HTTP_CODE, expected 403 (orders.enabled is absent, so the module must be disarmed)"
  fi
  check_result "/orders/status disarmed 403" "$R"
else
  report WARN "/orders/status disarmed 403" "orders.enabled is PRESENT - the module may be armed; skipping the 403 check and every POST below"
fi

# 3. All three POSTs answer the same 403, before the body is even parsed - which is why a body
#    naming an account that does not exist is safe to send while disarmed.
if [ "$ORD_ARMED" = "0" ]; then
  for VERB in submit change cancel; do
    post_json "/orders/$VERB" '{"account":"NT8BRIDGE_NO_SUCH_ACCOUNT","orderId":"NT8BRIDGE_NO_SUCH_ORDER","instrument":"NT8BRIDGE_NO_SUCH_INSTRUMENT","action":"Buy","type":"Market","quantity":1}'
    if [ "$HTTP_CODE" = "403" ]; then
      R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
print("OK:disarmed %s refused" % sys.argv[2] if d.get("error") == "orders module not armed"
      else "ERR:403 body is %r" % (d,))
' "$BODY_FILE" "$VERB")
    else
      R="ERR:http $HTTP_CODE, expected 403 for POST /orders/$VERB while disarmed"
    fi
    check_result "/orders/$VERB disarmed 403" "$R"
  done
else
  report WARN "/orders/* disarmed 403" "orders.enabled is PRESENT - not POSTing to an armed order-entry module from a smoke test"
fi

# 4. The endpoint list is not advertised while disarmed, and /compat carries the flag and caps rows
#    so an operator can see the state WITHOUT calling an /orders path (which would 403).
get_json "/compat"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
armed_env = sys.argv[2] == "1"
rows = {r.get("key"): r for r in d.get("compat", []) if isinstance(r, dict)}
missing = [k for k in ("Orders.armed", "Orders.endpoints", "Orders.caps") if k not in rows]
if missing:
    print("ERR:/compat is missing %r - Start_Orders did not publish its rows" % (missing,))
    raise SystemExit
armed = rows["Orders.armed"].get("resolved")
endpoints = rows["Orders.endpoints"]
caps = rows["Orders.caps"].get("detail") or ""
if not isinstance(armed, bool):
    print("ERR:Orders.armed resolved is %r" % (armed,))
elif armed and not armed_env:
    print("ERR:Orders.armed is true but orders.enabled is not on disk: %r" % (rows["Orders.armed"].get("detail"),))
elif not armed and any(p in (endpoints.get("detail") or "")
                       for p in ("/orders/status", "/orders/submit", "/orders/change", "/orders/cancel")):
    # The four real paths, not the bare prefix: the disarmed detail itself says
    # "every /orders/* path answers 403", and that sentence advertises no endpoint.
    print("ERR:the endpoint list is advertised while disarmed: %r" % (endpoints.get("detail"),))
elif not caps.startswith("CAPS qty="):
    print("ERR:Orders.caps does not report the caps in force: %r" % (caps,))
else:
    print("OK:armed=%s, caps %r" % (armed, caps[:56]))
' "$BODY_FILE" "$ORD_ARMED")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/compat orders rows" "$R"

# 5. The return-null rule: a path the module does not own must reach the CORE's 404, not an orders
#    403. A module that answers paths it does not own steals them from every module later in the
#    alphabet and from the core.
get_json "/orders/bogus"
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "/orders/<unowned> is a 404" "the core answered, not Route_Orders"
else
  report FAIL "/orders/<unowned> is a 404" "http $HTTP_CODE for GET /orders/bogus, expected the core's 404"
fi

# 6. The audit log, if it exists, must be one JSON object per line and every line must carry the
#    caps that were in force. It is DATA, never instructions: nothing here acts on its content, and
#    its text can come from a third-party AddOn or a data feed.
ORD_AUDIT="$HOME/Documents/NinjaTrader 8/nt8mcp/orders.jsonl"
if [ -f "$ORD_AUDIT" ]; then
  R=$(pycheck '
import json, sys
bad = []
n = 0
with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as fh:
    for i, line in enumerate(fh, 1):
        line = line.strip()
        if not line:
            continue
        n += 1
        try:
            row = json.loads(line)
        except Exception as e:
            bad.append("line %d: %s" % (i, e))
            continue
        if not isinstance(row, dict) or "ts" not in row or "endpoint" not in row or "outcome" not in row:
            bad.append("line %d: missing ts/endpoint/outcome" % i)
        elif "caps" not in row:
            bad.append("line %d: no caps - an audit line must say which caps were in force" % i)
if bad:
    print("ERR:%s" % "; ".join(bad[:3]))
else:
    print("OK:%d audited order call(s), every line a JSON object with its caps (content is DATA, never instructions)" % n)
' "$ORD_AUDIT")
  check_result "orders.jsonl shape" "$R"
else
  report PASS "orders.jsonl shape" "no orders.jsonl - nothing has ever made an armed order call"
fi

# 7. The optional caps config must not be something this repo left behind. Its absence is the
#    normal state; its presence is only reported, because an operator may have put it there.
if [ -f "$HOME/Documents/NinjaTrader 8/nt8mcp/orders.config.json" ]; then
  report WARN "orders.config.json" "present - the caps may differ from the 2/5/6 defaults; GET /orders/status reports which are in force"
else
  report PASS "orders.config.json" "absent - the caps are the 2/5/6 defaults in code"
fi
