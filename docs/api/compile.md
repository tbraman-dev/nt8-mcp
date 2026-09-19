# `/compile` — compile NinjaScript through NinjaTrader's own compiler

Module: `addon/NT8Bridge.Compile.cs` (`Route_Compile`, `Start_Compile`) ·
`server/nt8_mcp/tools_compile.py`.

Replaces the two worst properties of the F5 path (`nt_compile_f5`, the pre-1.2 `nt_compile`): it
needs no NinjaScript Editor window, and a failure comes back as
`{file,line,column,code,message}` instead of a screenshot of the editor.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| POST | `/compile` | compile result below. `checkCompileOnly=true`: validates the tree, emits nothing, `assemblyReloaded` always `false` |
| POST | `/compile?reload=1` body `{"force":false}` | the same result, but NinjaTrader emits and **swaps** the NinjaScript assembly exactly as F5 does. `409` while a live order-routing connection is up unless `{"force":true}` |

`GET /compile` is not handled and gets the core's `404`. Both forms take an optional JSON body;
an empty body is fine (send `Content-Length: 0` — `HttpListener` answers `411` without it).

The compiler always builds the **whole** `bin\Custom` tree; there is no per-file compile. It
builds what is on disk, so install your files first.

## Result

```json
{ "ok": false,
  "assemblyReloaded": false,
  "reloadRequested": false,
  "checkCompileOnly": true,
  "seconds": 24.3,
  "errors": [{"file":"C:\\Users\\you\\Documents\\NinjaTrader 8\\bin\\Custom\\Strategies\\SampleMACrossOver.cs",
              "line":184,"column":13,"code":"CS0103",
              "message":"The name 'foo' does not exist in the current context"}],
  "warnings": [{"file":"...","line":91,"column":25,"code":"CS0168","message":"..."}],
  "warningCount": 17,
  "warningsSuppressed": 113779,
  "assemblyBuiltUtc": "2026-09-18T10:00:00Z",
  "startedAt": "2026-09-18T12:00:00",
  "compiledAtUtc": "2026-09-18T12:41:07Z" }
```

| Key | Type | Meaning |
|---|---|---|
| `ok` | bool | `EmitResult.Success` **and** no `Error` diagnostic |
| `assemblyReloaded` | bool | see **Truthfulness** below |
| `reloadRequested` | bool | `?reload=1` was passed |
| `checkCompileOnly` | bool | what was handed to the compiler (`!reloadRequested`) |
| `seconds` | number | wall clock of the compiler call |
| `errors`, `warnings` | array | `{file, line, column, code, message}`; `line` and `column` are **1-based** (Roslyn counts from 0). `Hidden`/`Info` diagnostics are dropped |
| `warningCount` | int | every real warning in the tree. `warnings` lists at most the first **200** of them; errors are never capped |
| `warningsSuppressed` | int | `CS1701`/`CS1702` assembly-unification notices, counted and **not** listed. Live on 8.1.8.2 a clean tree gives 113 779 of them (one per reference per file, no file or line) against 17 real warnings — listing them made a 38 MB response. NinjaTrader's own editor never shows them either |
| `assemblyBuiltUtc`, `startedAt` | string | of the assembly that **answered** — the interlock, below |
| `compiledAtUtc` | string | when the compiler call returned |

`warnings` is the half cli-nt-bridge threw away (`NT8BridgeServer.cs:2561` skips every diagnostic
whose severity is not `Error`). Diagnostics are **identical** for a check and for a reload.

Refusals, all `{"error": …}`:

| Status | When |
|---|---|
| 400 | the body is not JSON, or `force` is not a boolean |
| 409 | `?reload=1` while `AnyLiveConnected()` — body also carries `"anyLive": true`. Also: a compile is already running (one at a time) |
| 500 | the compiler returned no result object, or its result's shape could not be read (`Success`/`Diagnostics` missing) — we do not know whether it built, and will not guess; see `GET /compat` |
| 503 | `NinjaTrader.Code.Compiler.Compile` did not resolve; see `GET /compat` |

## Truthfulness: what `assemblyReloaded` can and cannot mean

`assemblyReloaded` is a **claim**, and the strongest one the compiling process can make: only a
non-check build that emitted successfully swapped the assembly. It cannot be an observation,
because the code writing the response *is* the pre-swap assembly — its own `Assembly.Location`
never changes. `bin\Custom\NinjaTrader.Custom.dll` is no help either: after a hot reload its mtime
**and** its `ModuleVersionId` both lie, because the running code is
`Documents\NinjaTrader 8\tmp\<guid>.dll`. cli-nt-bridge first hardcoded the field `false`
(`CHANGELOG:633-637`), which made a real reload indistinguishable from a check.

So the response also carries `assemblyBuiltUtc` and `startedAt` **of the assembly that answered**.
A client that re-reads `GET /health` afterwards must compare **`startedAt` only**. `startedAt` is
set once, in `TryBind()`, when a process binds `:7891`, so a new instance always reports a new one
and an old instance that never restarted never does. `assemblyBuiltUtc` is **not** safe for this:
it is the mtime of the file the running assembly loaded from, and on a cold start that file *is*
`bin\Custom\NinjaTrader.Custom.dll` — the exact file a reload's emit overwrites. An old listener
still holding the port (a hung `Start()`, a failed bind) therefore reports a brand-new
`assemblyBuiltUtc` while still running the old code — the false confirmation this interlock exists
to prevent. That `startedAt` comparison, not the field, is the interlock — the same reasoning as
`/ntstatus`. `compile_via_addon(reload=True)` does exactly this and rewrites the
field:

| `assemblyReloadedSource` | Meaning |
|---|---|
| `observed: /health reports a different startedAt` | verified |
| `claimed by the AddOn, not observed` | `assemblyReloadedNote` says why. The value is forced to `false` **only** when `/health` answered both times and `startedAt` did not move. If `/health` could not be read before the compile, or does not answer within the timeout after, that is not disproof — the AddOn's claim is left standing and only the source/note carry the doubt |

## Surviving the swap

`?reload=1` replaces the assembly the listener lives in, so the socket can drop before the response
is written. The same JSON is written to
`<UserDataDir>\NT8Bridge\compile_last.json` (best effort) just before the AddOn answers.
`compile_via_addon` falls back to that file when the socket drops, but only accepts it if its mtime
is newer than the moment the request was sent — a stale file is never reported as this run. The
result then carries `"source": "compile_last.json"`; a normal answer carries `"source": "http"`.

**A timeout is not proof of failure.** The compile keeps going. When neither the socket nor the file
answers, the result is a `hint` separating "NinjaTrader is still compiling" from "the AddOn is not
loaded" — there is no `ok` field at all, so nothing can read it as a failed build.

## Python

| Name | Kind | Notes |
|---|---|---|
| `nt_compile(timeout_s=120)` | MCP tool | check-only, via `compile_via_addon(reload=False)`; falls back to `nt_compile_f5` (`tools_local.py`) when the AddOn does not answer `/health` at all. Takes **no** reload flag, by design: a validate step must not be able to disrupt a live session by flipping an argument |
| `nt_reload_assembly(timeout_s=240)` | MCP tool | the separate reload tool, via `compile_via_addon(reload=True)`. No `force` argument is exposed — the AddOn's 409 while `AnyLiveConnected()` cannot be bypassed from here |
| `compile_via_addon(reload=False, force=False, timeout_s=None)` | plain function | the full capability behind both tools above |

Timeouts default to 120 s for a compile and 240 s for a reload (`app.HTTP_TIMEOUT` is 5 s and is
far too short, so this module makes its own request).

`duplicatedRegions` is attached to a failing result only: a Python-side scan of `bin\Custom` for
`.cs` files carrying more than one anchored `#region NinjaScript generated code`. They all compile
into one assembly, so duplicates surface as `CS0111`/`CS0102` against code nobody edited. The scan
is wrapped so that a scan failure (`duplicatedRegionsError`) can never mask the compile result.

## Threading

No dispatcher, no `Ui()`, no chart access. The compiler runs inline on the `HttpListener` worker
thread — cli-nt-bridge runs it on a plain Timer callback thread, so an off-UI thread is proven.
One compile at a time: the module takes `Compile_gate` with `Monitor.TryEnter` and refuses a second
request with `409` rather than queueing it, because a compile holds that lock for 20-30 s and the
core's `Route` must not block. The module subscribes to no event and starts no thread, so it has no
`Stop_Compile`.
