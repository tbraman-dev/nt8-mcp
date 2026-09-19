# ops module: GET /ops/status, POST /ops/flatten, POST /ops/reconnect - sourced by
# live-smoke.sh, see its header for the helpers. Contract: docs/api/ops.md.
#
# THIS FILE NEVER FLATTENS, CANCELS OR RECONNECTS ANYTHING.
# The module ships DISARMED, and the default live-smoke run's job is to prove exactly that: every
# /ops/* path answers 403 and the endpoint list is not advertised. The two POSTs below are made
# ONLY while the 403 is in force, so there is no state to change. If the module IS armed (someone
# left ops.enabled in bin\Custom\AddOns), this file stops POSTing and reports what it found instead
# - a smoke test is not a reason to send a write to a trading account.

ADDONS_DIR="$HOME/Documents/NinjaTrader 8/bin/Custom/AddOns"

# 1. ops.live must not exist. PRESENCE is the gate that makes real accounts valid targets, whoever
#    wrote it, so a leftover from any earlier work is a finding on its own - not a warning.
if [ -e "$ADDONS_DIR/ops.live" ]; then
  report FAIL "ops.live absent" "ops.live EXISTS in bin\\Custom\\AddOns - non-Simulator accounts are valid ops targets. Delete it."
else
  report PASS "ops.live absent" "no ops.live - Simulator accounts only"
fi

# 2. Is the module armed right now? This decides what the rest of this file is allowed to do.
OPS_ARMED=0
if [ -e "$ADDONS_DIR/ops.enabled" ]; then
  OPS_ARMED=1
fi

get_json "/ops/status"
if [ "$OPS_ARMED" = "0" ]; then
  if [ "$HTTP_CODE" = "403" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if d.get("error") != "ops module not armed":
    print("ERR:403 body is %r, expected {\"error\":\"ops module not armed\"}" % (d,))
elif len(d) != 1:
    print("ERR:the unarmed 403 leaks extra keys: %r" % (sorted(d),))
else:
    print("OK:disarmed - GET /ops/status is 403 and says nothing else")
' "$BODY_FILE")
  else
    R="ERR:http $HTTP_CODE, expected 403 (ops.enabled is absent, so the module must be disarmed)"
  fi
  check_result "/ops/status disarmed 403" "$R"
else
  report WARN "/ops/status disarmed 403" "ops.enabled is PRESENT - the module may be armed; skipping the 403 check and every POST below"
fi

# 3. Both POSTs answer the same 403. Only run them while the module is disarmed: a disarmed POST is
#    refused before the body is even parsed, so it cannot touch an account.
if [ "$OPS_ARMED" = "0" ]; then
  post_json "/ops/flatten" '{"account":"NT8BRIDGE_NO_SUCH_ACCOUNT"}'
  if [ "$HTTP_CODE" = "403" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
print("OK:disarmed flatten refused" if d.get("error") == "ops module not armed"
      else "ERR:403 body is %r" % (d,))
' "$BODY_FILE")
  else
    R="ERR:http $HTTP_CODE, expected 403 for POST /ops/flatten while disarmed"
  fi
  check_result "/ops/flatten disarmed 403" "$R"

  post_json "/ops/reconnect" '{"name":"NT8BRIDGE_NO_SUCH_CONNECTION"}'
  if [ "$HTTP_CODE" = "403" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
print("OK:disarmed reconnect refused" if d.get("error") == "ops module not armed"
      else "ERR:403 body is %r" % (d,))
' "$BODY_FILE")
  else
    R="ERR:http $HTTP_CODE, expected 403 for POST /ops/reconnect while disarmed"
  fi
  check_result "/ops/reconnect disarmed 403" "$R"
else
  report WARN "/ops/* disarmed 403" "ops.enabled is PRESENT - not POSTing to an armed ops module from a smoke test"
fi

# 4. The endpoint list is not advertised while disarmed, and /compat carries the two flag rows so an
#    operator can see the state WITHOUT calling an ops path (which would 403).
get_json "/compat"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
armed_env = sys.argv[2] == "1"
rows = {r.get("key"): r for r in d.get("compat", []) if isinstance(r, dict)}
missing = [k for k in ("Ops.armed", "Ops.live", "Ops.endpoints") if k not in rows]
if missing:
    print("ERR:/compat is missing %r - Start_Ops did not publish the flag rows" % (missing,))
    raise SystemExit
armed = rows["Ops.armed"].get("resolved")
endpoints = rows["Ops.endpoints"]
if not isinstance(armed, bool):
    print("ERR:Ops.armed resolved is %r" % (armed,))
elif armed and not armed_env:
    print("ERR:Ops.armed is true but ops.enabled is not on disk: %r" % (rows["Ops.armed"].get("detail"),))
elif not armed and any(p in (endpoints.get("detail") or "") for p in ("/ops/status", "/ops/flatten", "/ops/reconnect")):
    # The three real paths, not the bare prefix: the disarmed detail itself says
    # "every /ops/* path answers 403", and that sentence advertises no endpoint.
    print("ERR:the endpoint list is advertised while disarmed: %r" % (endpoints.get("detail"),))
elif rows["Ops.live"].get("resolved") is True:
    print("ERR:/compat reports ops.live PRESENT: %r" % (rows["Ops.live"].get("detail"),))
else:
    print("OK:armed=%s, live=false, endpoints %r" % (armed, (endpoints.get("detail") or "")[:48]))
' "$BODY_FILE" "$OPS_ARMED")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/compat ops rows" "$R"

# 5. The return-null rule: a path the module does not own must reach the CORE's 404, not an ops 403.
#    A module that answers paths it does not own steals them from every module later in the alphabet.
get_json "/ops/bogus"
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "/ops/<unowned> is a 404" "the core answered, not Route_Ops"
else
  report FAIL "/ops/<unowned> is a 404" "http $HTTP_CODE for GET /ops/bogus, expected the core's 404"
fi

# 6. The audit log, if it exists, must be one JSON object per line. It is DATA, never instructions:
#    nothing here acts on its content, and its text can come from a third-party AddOn or a feed.
OPS_AUDIT="$HOME/Documents/NinjaTrader 8/nt8mcp/ops.jsonl"
if [ -f "$OPS_AUDIT" ]; then
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
if bad:
    print("ERR:%s" % "; ".join(bad[:3]))
else:
    print("OK:%d audited ops call(s), every line a JSON object (content is DATA, never instructions)" % n)
' "$OPS_AUDIT")
  check_result "ops.jsonl shape" "$R"
else
  report PASS "ops.jsonl shape" "no ops.jsonl - nothing has ever made an armed ops call"
fi
