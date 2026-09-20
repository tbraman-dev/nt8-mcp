# `/api` — NinjaScript API lookup by reflection

Module: `addon/NT8Bridge.Api.cs` (`Route_Api`) · `server/nt8_mcp/tools_api.py`.

Stops a model from inventing a NinjaScript API call: look the member up here first, and get its
real signature back instead of a guess that fails at compile time. Pure reflection over the
already-**loaded** assemblies (`NinjaTrader.Core`, `NinjaTrader.Gui`, `NinjaTrader.Custom`, and
`NinjaTrader.Vendor` when it is loaded) — read-only, no instance is ever created and nothing is
ever invoked. Runs inline on the `HttpListener` worker thread: no `Ui()`, no dispatcher, nothing
that can block a chart.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/api/search?q=<keyword>&limit=30` | ranked type/member name matches, below |
| GET | `/api/type?name=<Type or Namespace.Type>&member=<optional filter>` | one type's shape, below |

Both are `GET`-only; the other verb on either path falls through to the core's `404`.

### `GET /api/search`

`q` is required (`400` when missing or blank). `limit` defaults to 30 and is capped at 200
server-side, however large a value is passed. Ranking, case-insensitive: **0** exact name match,
**1** name starts with `q`, **2** name contains `q` — a candidate that matches none of the three is
not a hit at all. Ties break by name, ordinal, case-insensitive.

```json
{ "query": "SMA", "matched": 3, "truncated": false,
  "results": [
    { "rank": 0, "resultKind": "Type", "typeKind": "Class",
      "name": "NinjaTrader.NinjaScript.Indicators.SMA", "assembly": "NinjaTrader.Custom" },
    { "rank": 2, "resultKind": "Method", "name": "SMA",
      "declaringType": "NinjaTrader.NinjaScript.Strategy", "assembly": "NinjaTrader.Custom" } ] }
```

`matched` is the true count of everything that matched, before the `limit` cap; `truncated` is
`true` when `matched > limit`. Every public type is searched by its simple name; every **public**
member of every public type is searched too (declared members only per type — an inherited member
is a hit once, on the type that declares it, not again on every type that inherits it).
`resultKind` is `"Type"` or one of `"Method"`, `"Property"`, `"Field"`, `"Event"`, `"Constructor"`.
A `Type` result also carries `typeKind` (`Class`/`Interface`/`Struct`/`Enum`/`Delegate`); a member
result carries `declaringType` — pass that as `name` to `/api/type` for the full signature.

### `GET /api/type`

`name` is required (`400` when missing or blank): a simple name (`"StrategyBase"`) or a full one
(`"NinjaTrader.NinjaScript.StrategyBase"`). An exact `FullName` match wins outright; otherwise every
publicly visible type whose simple name matches, case-insensitive:

| Match count | Response |
|---|---|
| 0 | `404 {"error": "no type '<name>' in the loaded NinjaTrader assemblies"}` |
| 1 | the type, below |
| 2+ | `200 {"ambiguous": true, "candidates": ["Full.Name.One", "Full.Name.Two", ...]}` — never a guess |

```json
{ "name": "NinjaTrader.NinjaScript.StrategyBase", "assembly": "NinjaTrader.Custom",
  "kind": "Class", "baseType": "NinjaScriptBase", "interfaces": ["INotifyPropertyChanged"],
  "memberCount": 214, "truncated": false,
  "members": [
    { "kind": "Method", "name": "EnterLong", "declaringType": "NinjaTrader.NinjaScript.StrategyBase",
      "static": false, "signature": "public void EnterLong(int quantity, string signalName)" },
    { "kind": "Property", "name": "Position", "declaringType": "NinjaTrader.NinjaScript.StrategyBase",
      "static": false, "propertyType": "Position", "get": true, "set": false,
      "signature": "public Position Position { get; }" } ] }
```

`kind` is `Class`/`Interface`/`Struct`/`Enum`/`Delegate`. `members` lists the type's **public and
protected** fields, properties, events, constructors and methods — inherited ones included, static
ones included (an inherited static member does not disappear on a derived type). **Overloads are
never collapsed**: two `EnterLong` entries with different `signature`s means two overloads.
`signature` is full C# style: parameter names and types, return type, and `static`/`virtual`/
`override` where they apply; a property's `signature` shows which accessor(s) it has and their own
accessibility when narrower than the property's own (`{ get; protected set; }`). `member=<text>`
filters the list to members whose name contains it, case-insensitive — **use it** for anything with
a deep base-class chain (`StrategyBase`, `Strategy`, …): the list is capped at 500 members
(`truncated: true` past that), so the member you want can be cut off before you ever get to `member`
filtering client-side.

An enum gets `values` instead of `members`: `"values": [{"name": "Long", "value": 1}, ...]`.

## Python

| Name | Kind | Notes |
|---|---|---|
| `nt_api_search(query, limit=30)` | MCP tool | thin `GET /api/search` passthrough |
| `nt_api(type_name, member="")` | MCP tool | thin `GET /api/type` passthrough |

Both docstrings tell a model to call them **before** writing NinjaScript against a member it is not
sure of. No local state, no caching — every call re-reflects the assemblies as they are loaded
right now, which matters after `nt_reload_assembly` swaps them.

## Threading and safety

No dispatcher, no chart, no `Draw.*`, no order path — this module cannot touch any of them, by
construction: it only calls `Type`/`MemberInfo` reflection APIs on assemblies already resident in
the process. `Assembly.GetTypes()` throws `ReflectionTypeLoadException` when even one type in that
assembly fails to load; it is caught **per assembly** and the types that did load are kept, so one
broken assembly among the four never blanks out search or type lookup in the other three.
