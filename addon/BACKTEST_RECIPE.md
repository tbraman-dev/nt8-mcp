# BACKTEST_RECIPE.md — headless backtests from the NT8Bridge AddOn

Single source of truth for the implementer. Read this + `API.md` ("Backtests (v1.1)") + `NT8Bridge.cs`.
Nothing else is needed.

Target: NinjaTrader 8.1.8.2. AddOn compiles into `NinjaTrader.Custom.dll`
(`%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\NT8Bridge.cs`, listed at
`NinjaTrader.Custom.csproj:441`, net48 / LangVersion 13.0 / x64 / UseWPF).

**Label key**
- `VERIFIED` — the member exists with that exact signature/attributes, read from the decompile or from
  unobfuscated shipped source. Existence only; almost every *body* in NinjaTrader.Core is stripped by
  AgileDotNet, so behaviour is never verified this way.
- `GUESS` — inference. Says what it is inferred from.
- `PROVEN` — a third party ran it on a real NT8 and it worked (community report).

**The one thing to understand before writing code**: `StrategyBase.RunBacktest()` is public but
`[EditorBrowsable(Never)]`, undocumented (no entry in `NinjaTrader.Core.xml`), and **nobody on the
public internet has ever published a successful call to it from outside NinjaTrader's own hosts**.
NinjaTrader staff say in two forum threads there is "no supported means" to run a backtest from code.
So this build is an experiment with three ordered fallbacks. Recipe C is the only one anyone has
demonstrated working.

---

## 1. Member table

Verified members first; `GUESS` marked inline. `File.cs:NNN` line references point into the decompiled
NinjaTrader 8 assemblies (`core/NinjaTrader.Core/`, `core/NinjaTrader.NinjaScript/`, `core/NinjaTrader.Cbi/`).
A leading `@` marks a shipped, unobfuscated NinjaScript source file under
`%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\`.

### 1.1 Run entry points — `NinjaTrader.NinjaScript.StrategyBase` (core/NinjaTrader.NinjaScript/StrategyBase.cs)

| Member | Signature / notes | Line |
|---|---|---|
| `RunBacktest()` | `[MethodImpl(NoInlining)][EditorBrowsable(Never)] public void` — no args, no return, no `IProgress`, no cancel. **The target.** Body stripped. | 1867 |
| `RunBacktestInternal()` | `internal void` — internal to NinjaTrader.Core. An AddOn **cannot** bind it at compile time. Reflection only, and the obfuscator's runtime may block it. | 1872 |
| `RunOptimization(Action<StrategyBase> callback)` | `[EditorBrowsable(Never)] public void` — the async sibling. Its callback arg vs `RunBacktest()`'s void-no-callback is the single strongest signature-level hint that `RunBacktest()` **blocks**. | 1878 |
| `CreateNewGeneration(bool copyOrdersAndExecutions, bool? inclTradeHistoryInBacktest = null)` | `[EditorBrowsable(Never)] public StrategyBase` — NT's own configured-clone factory. Fallback if hand-configuration misses a hidden field. | 1645 |
| `MergePerformance(SystemPerformance)` | `[EditorBrowsable(Never)] public void` — walk-forward / multi-instrument only. | 1742 |
| `CopyOrdersAndExecutionsTo(StrategyBase)` | `[EditorBrowsable(Never)] public void` | 1639 |
| `CopyTo(NinjaScript)` | `public override void` (NinjaScript.cs:579 declares virtual, NinjaScriptBase.cs:1832 overrides) | 1519 |
| `FillOrder(Order, double fillPrice, double slippage)` | `public bool`, body stripped | 3591 |
| `EnterReadLock()` / `ExitReadLock(List<BarsSeries>)` | `[EditorBrowsable(Never)] public` — bars read locks. Only needed if you touch `BarsArray` off the worker thread. Don't. | 1692 / 1699 |
| `SetUniqueId()` | `[EditorBrowsable(Never)] public void` | 1537 |
| `DbRemove()` / `DbRemoveByCategory(Category, bool useMailDB)` | `[EditorBrowsable(Never)] public` / `public static` — cleanup if a run leaks a strategy-DB row. | 1667 / 1673 |
| `All` | `public static Collection<StrategyBase>`, body **readable**: `DbLoad(); return cacheList;` | 782 |
| ctors | `protected StrategyBase()` and `protected StrategyBase(bool isMinimal)` — **you cannot `new StrategyBase()`**; instantiate a concrete subclass. | 1541 / 1547 |
| `RestoreOrders()` | `internal` — unreachable. | 1861 |

### 1.2 Configuration you must set — `StrategyBase`

| Property | Type / attributes | Accessors | Line |
|---|---|---|---|
| `Account` | `Cbi.Account`, `[XmlIgnore][TypeConverter(AccountConverter)][RefreshProperties(All)]`, Setup Order 0 | plain auto-prop | 780 |
| `Category` | `NinjaScript.Category`, `[Browsable(false)]` — set `Category.Backtest` | plain auto-prop | 871 |
| `IncludeTradeHistoryInBacktest` | `bool`, `[XmlIgnore][Browsable(false)]` — **must be `true` or `AllTrades` is empty** | getter readable, setter **stripped** | 1059 |
| `IsTickReplay` | `bool`, `[EditorBrowsable(Never)]`, DataSeries Order 99 | **both accessors stripped** (getter stub returns `false`) | 1167 |
| `IncludeCommission` | `bool`, Setup Order 0 | setter stripped | 1042 |
| `BacktestCommissionTemplate` | `string`, Setup Order 20 | setter stripped | 838 |
| `Slippage` | `double`, `[Range(0, double.Max)]`, HistFill Order 4 | plain auto-prop | 1379 |
| `OrderFillResolution` | `OrderFillResolution`, `[RefreshProperties(All)]`, Order 0 | plain auto-prop | 1288 |
| `OrderFillResolutionType` | `BarsPeriodType`, Order 1 | plain auto-prop | 1295 |
| `OrderFillResolutionValue` | `int`, `[Range(1, double.Max)]`, Order 2 | plain auto-prop | 1302 |
| `IsFillLimitOnTouch` | `bool`, Order 3 | setter stripped | 1112 |
| `DaysToLoad` | `int`, `[Range(1, int.Max)]`, TimeFrame Order 0 | **getter stripped, setter readable** (inverse of usual) | 891 |
| `TradingHoursInstance` | `Data.TradingHours`, `[EditorBrowsable(Never)][XmlIgnore]`, TimeFrame Order 1 | both stripped | 1435 |
| `TradingHoursSerializable` | `string`, `[JsonIgnore][Browsable(false)]` — the **reliable** TH route | plain auto-prop | 1450 |
| `BarsRequiredToTrade` | `int`, `[Range(0, int.Max)]`, Setup Order 400 | setter stripped | 858 |
| `DefaultQuantity` | `int`, `[Range(1, int.Max)]`, OrderProps Order 0 | setter stripped | 914 |
| `EntriesPerDirection` | `int`, `[Range(1, int.Max)]`, OrderHandling Order 0 | setter stripped | 962 |
| `EntryHandling` | `EntryHandling`, Order 1 | setter stripped | 978 |
| `IsExitOnSessionCloseStrategy` | `bool`, `[RefreshProperties(All)]`, Order 2 | setter stripped | 1096 |
| `ExitOnSessionCloseSeconds` | `int`, `[Range(0, int.Max)]`, Order 3 | setter stripped | 995 |
| `StopTargetHandling` | `StopTargetHandling`, Order 70 | — | 1391 |
| `StartBehavior` | `StartBehavior`, Setup Order 500 | plain auto-prop | 1385 |
| `TimeInForce` | `TimeInForce`, OrderProps Order 2 | — | 827 |
| `SetOrderQuantity` | `SetOrderQuantity`, OrderProps Order 1 | — | 1372 |
| `IsUnmanaged` | `bool`, `[XmlIgnore][Browsable(false)]` | setter stripped | 1201 |
| `TraceOrders` | `bool` — order traces into Output; pairs with `Output.OutputEvent` | — | 1428 |
| `InstrumentOrInstrumentList` | `string`, `[Required][EditorBrowsable(Never)]` — the SA's instrument spec | — | 1078 |
| `IsInstantiatedOnEachOptimizationIteration` | `bool`, `[XmlIgnore][Browsable(false)]` — **must be true for tick-replay optimizations** (Resource.resx:1799); a strategy's own base class may set it false | setter stripped | 1131 |
| `IsAggregated` | `bool`, Optimize Order 10 | — | 1089 |
| `SupportsOptimizationGraph` | `bool` — `@StrategyGenerator.cs:252` sets it false | — | — |
| `PerformanceMetrics` | `PerformanceMetricBase[]`, `[XmlIgnore][Browsable(false)]` | plain auto-prop | 1314 |
| `IsTerminal` | `bool` public, undocumented, role unknown (`GUESS`: teardown flag) | — | 1163 |

### 1.3 Data series — `NinjaTrader.NinjaScript.NinjaScriptBase` (core/NinjaTrader.NinjaScript/NinjaScriptBase.cs)

| Member | Notes | Line |
|---|---|---|
| `From` / `To` | `DateTime`, plain auto-props, TimeFrame Order 0/1. **The backtest window lives here, not on StrategyBase.** | 544 / 689 |
| `Instrument` | `Cbi.Instrument` — **facade, body readable**: `get => Instruments[BarsInProgress]; set => Instruments[BarsInProgress] = value;` | 566 |
| `Instruments` | `Instrument[]`, `[XmlIgnore][Browsable(false)]`, **public setter** | 836 |
| `BarsPeriod` | `Data.BarsPeriod` — facade over `BarsPeriods[BarsInProgress]`, bodies readable, `[RefreshProperties(All)][XmlIgnore]` | 719 |
| `BarsPeriods` | `BarsPeriod[]`, `[XmlIgnore][Browsable(false)]`, **public setter** | 736 |
| `BarsPeriodSerializable` | `BarsPeriod` = `BarsPeriods[0]`, bodies readable, `[EditorBrowsable(Never)][Browsable(false)]` | 430 |
| `TradingHours` | `Data.TradingHours` — facade over `TradingHoursArray[BarsInProgress]`, bodies readable | 1041 |
| `TradingHoursArray` | `TradingHours[]`, public setter | 1055 |
| `IsTickReplays` | `bool?[]`, `[XmlIgnore][Browsable(false)]` — per-series flag | 893 |
| `IsResetOnNewTradingDays` | `bool?[]` | 886 |
| `Calculate` | `Calculate` — **both bodies readable**, reads/writes the public field `calculate2` | 464 |
| `calculate2` | genuine **public field** on NinjaScriptBase | 113 |
| `BarsToLoad` | `int`, `[Browsable(false)]`, = `barsToLoad[0]`, readable | 448 |
| `MaximumBarsLookBack` / `LookupPolicies` | run tuning | 618 / 612 |
| `BarsArray` | `Bars[]` — public get, **private set** (so `InitializeBars` is the only public way in) | 705 |
| `Bars` / `CurrentBar` / `Times` | what exists after DataLoaded | 698 / 782 / 1034 |
| `InitializeBars(Bars[] barsArray, IProgress progress, Action<NinjaScriptBase> callback)` | `[EditorBrowsable(Never)] public void` — **async (callback)**; the only public way to install loaded bars | 1688 |
| `SetState(State)` | `public override void`, body stripped. The only way to drive lifecycle. | 2141 |
| `ReloadAllHistoricalData()` | public, stripped | 1758 |
| `Dispatcher` | `Dispatcher { get; internal set; }` — **an AddOn cannot assign it**; `null` on an `Activator`-created instance | 515 |
| `BarsToDispose` | `List<Bars>`, public setter — the only `Dispose`-named member on the base type | 445 |
| `ResetForOptimizerIteration(State)` | `internal` — unreachable | 1773 |
| `AfterSetState` | `internal Action` (community report reaches it by reflection as a non-public *property*) — fires after each state change | 691 |
| `doneConfigureState` | private `bool` field — NT's own record that `OnStateChange(Configure)` ran. Reflection probe. | — |

### 1.4 Lifecycle — `NinjaTrader.NinjaScript.NinjaScript` / `State`

| Member | Notes |
|---|---|
| `NinjaScript.State { get; set; }` | `[XmlIgnore][Browsable(false)]` **plain auto-prop with a public setter** (NinjaScript.cs:428). Read it; **never assign it** — assignment does not run the transition. |
| `NinjaScript.SetState(State)` | `public abstract void` (NinjaScript.cs:618) |
| `NinjaScript.Clone()` / `CopyTo(NinjaScript)` | `public virtual` (NinjaScript.cs:567 / 579) |
| `NinjaScript.Name` / `DisplayName` / `Description` | NinjaScript.cs:382 / 414 / 398 |
| `NinjaScript.PrintTo` | `PrintTo`, `[XmlIgnore][Browsable(false)]`, defaults `OutputTab1` (NinjaScript.cs:389, default at :575) |
| `NinjaScript.Print(object)` | public, stripped (NinjaScript.cs:475) |
| `NinjaScript.Log(string, LogLevel)` | `public static`, stripped (NinjaScript.cs:589) |
| `NinjaScript.ClearOutputWindow()` | body **readable**: `Output.Reset(PrintTo);` (NinjaScript.cs:433) — **never call from the bridge** |
| `enum State` | `Undefined, SetDefaults, Configure, Active, DataLoaded, Historical, Transition, Realtime, Terminated, Finalized` (State.cs:10-24). Note `Active` sits **between** Configure and DataLoaded. |
| `enum Calculate` | `OnBarClose, OnEachTick, OnPriceChange` |
| `enum Category` | `Atm, Backtest, NinjaScript, Optimize, WalkForward, WalkForwardAnchored, MultiObjective` (Category.cs) |
| `enum PrintTo` | `OutputTab1, OutputTab2` — **only two tabs**, so concurrent runs cannot be told apart in captured output |

### 1.5 Results — `NinjaTrader.Cbi` (core/NinjaTrader.Cbi/)

`SystemPerformance` is in **`NinjaTrader.Cbi`**, not `NinjaTrader.NinjaScript`.

| Member | Notes | File:line |
|---|---|---|
| `StrategyBase.SystemPerformance` | `Cbi.SystemPerformance`, `[Browsable(false)][XmlIgnore]`. **Getter body stripped (stub returns null), setter body readable** (`systemPerformance = value`). | StrategyBase.cs:1401 |
| `SystemPerformance.AllTrades / LongTrades / ShortTrades` | `TradeCollection`, public get / internal set | SystemPerformance.cs:22 / 41 / 57 |
| `SystemPerformance.RealTimeTrades` | get only — empty in a backtest | :52 |
| `SystemPerformance.Denomination` | `Currency`, default `UsDollar` | :24 |
| `SystemPerformance.ParameterValues` / `PerformanceValue` / `State` | `object[]` / `double` / `State` | :43 / :45 / :47 |
| `SystemPerformance(bool includeTradeHistoryInBacktest)` | public ctor | :60 |
| `SystemPerformance.Calculate(ICollection<Execution>)` | `public static SystemPerformance` — **fallback**: rebuild performance from `strategy.Executions` if the property comes back null | :75 |
| `SystemPerformance.CopyPerformance(SystemPerformance target)` | snapshot before teardown | :81 |
| `SystemPerformance.GetBacktestExecutionsReverse()` / `GetBacktestOrdersReverse()` | bodies **readable** (wrap internal DataPool) | :85 / :90 |
| `TradeCollection : Collection<Trade>` | `.Count`, `[i]`, `foreach` all work | TradeCollection.cs:9 |
| `TradeCollection.TradesCount` | `int` | :225 |
| `TradeCollection.WinningTrades / LosingTrades / EvenTrades` | `TradeCollection` | :220 / :211 / :206 |
| `TradeCollection.TradesPerformance` | `TradesPerformance` | :215 |
| `TradesPerformance.GrossProfit / GrossLoss` | `double`; **GrossLoss is negative** | TradesPerformance.cs:229 / 224 |
| `TradesPerformance.NetProfit` | `=> GrossLoss + GrossProfit` (body readable) | :359 |
| `TradesPerformance.ProfitFactor / SharpeRatio / SortinoRatio / RSquared / Probability` | `double` | :392 / 429 / 441 / 413 / 380 |
| `TradesPerformance.MaxConsecutiveWinner / MaxConsecutiveLoser` | `int` — **singular**, not `...Winners` | :268 / :263 |
| `TradesPerformance.AverageBarsInTrade / AverageTimeInMarket / TradesPerDay / TotalQuantity` | | :148 / 178 / 468 / 458 |
| `TradesPerformance.TotalCommission / TotalSlippage` | `double` | :473 / :478 |
| `TradesPerformance.TradesCount` | `int` | :463 |
| `TradesPerformance.RiskFreeReturn` | `double` get/set — input to Sharpe/Sortino; default not visible (`GUESS` 0) | :408 |
| `TradesPerformance.Currency / Points / Ticks / Pips / Percent` | `TradesPerformanceValues` — the unit flavours | :219 / 375 / 453 / 370 / 361 |
| `TradesPerformanceValues.CumProfit / AverageProfit / Drawdown` | `double` | TradesPerformanceValues.cs:43 / 38 / 48 |
| `TradesPerformanceValues.LargestWinner / LargestLoser` | `double` | :58 / :53 |
| `TradesPerformanceValues.AverageMae / AverageMfe / AverageEtd / StdDev / Ulcer / ProfitPerMonth / Turnaround` | `double` | :28 / 33 / 12 / 68 / 78 / 63 / 73 |
| `Trade.TradeNumber / Quantity` | `int` | Trade.cs:276 / 259 |
| `Trade.Entry / Exit` | `Execution` | :31 / :49 |
| `Trade.ProfitCurrency / ProfitPoints / ProfitTicks / ProfitPips / ProfitPercent` | `double` | :199 / 235 / 247 / 223 / 211 |
| `Trade.MaeCurrency / MaePoints / MaeTicks / MaePips / MaePercent` | `double` | :75 / 99 / 111 / 123 / 87 |
| `Trade.MfeCurrency / MfePoints / MfeTicks / MfePips / MfePercent` | `double` | :137 / 161 / 185 / 173 / 149 |
| `Trade.Commission / Fee` | `double` | :18 / :63 |
| `Trade.EntryEfficiency / ExitEfficiency / TotalEfficiency` | `double` | :36 / 54 / 264 |
| **`Trade` has NO `BarsInTrade`** | confirmed by decompile **and** by the official help guide | — |
| `Execution.Time / Price / Quantity / Name` | `DateTime / double / int / string` (`Name` = entry or exit signal name) | Execution.cs:166 / 146 / 151 / 136 |
| `Execution.MarketPosition` | `Cbi.MarketPosition` (`Long, Short, Flat`) | :127 |
| `Execution.BarIndex` | `int` public get/set, `[EditorBrowsable(Never)]` — **`GUESS`: `Exit.BarIndex - Entry.BarIndex` is the per-trade `bars`** | :169 |
| `Execution.BarsInProgress / Commission / Fee / Slippage` | | :174 / 104 / 113 / 161 |
| `Execution.IsEntry / IsExit / IsEntryStrategy / IsExitStrategy` | `bool` | :178-184 |
| `Execution.Order / OrderId / ExecutionId / Instrument / Account` | | — |
| `StrategyBase.Orders` | `Collection<Order>`, public get / internal set | StrategyBase.cs:812 |
| `StrategyBase.Executions` | `Collection<Execution>`, public get / internal set | :796 |
| `StrategyBase.Positions / PositionsAccount` | `Position[]`, public get / private set | :1335 / :1342 |

Confirming unobfuscated call sites (`%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\OptimizationFitnesses\`):
`@MaxNetProfit.cs:9` `...AllTrades.TradesPerformance.GrossProfit + ...GrossLoss`;
`@MaxPercentProfitable.cs:10-11` `AllTrades.WinningTrades.TradesCount / AllTrades.TradesCount`;
`@MaxProfitFactor.cs:9` `...TradesPerformance.ProfitFactor`; `@MinDrawDown.cs:9` `...Percent.Drawdown`;
`@MaxWinLossRatio.cs:10-13` `AllTrades.WinningTrades.TradesPerformance.Percent.AverageProfit`.

### 1.6 Account / instrument / trading hours / bars

| Member | Notes |
|---|---|
| `Cbi.Account.BackTestAccountName` | `public static readonly string` (Account.cs:162, assigned :1111). **Decoded value = `"Backtest"`** (AgileDotNet XOR keystream `7C 02 D2 81 63 BD 5A 41 7B 82 F8`, cross-checked against three independent cribs and a dozen unrelated literals). **Use the constant, not the literal.** |
| `Cbi.Account.SimulationAccountName` / `PlaybackAccountName` | decoded `"Sim101"` / `"Playback101"` |
| `Cbi.Account.All` | `public static Collection<Account>` (Account.cs:323), stripped |
| `Cbi.Account.DbGet(string name)` / `DbGet(string, string fcm, bool)` / `DbGet(long)` | static, stripped (:851 / :686 / :845) |
| `Cbi.Account.Strategies` | `Collection<StrategyBase>`, internal set (:378) |
| `Cbi.Account.SimulatorInitialCash / SimulatorDelayExchange / SimulatorDelayInternet / Commission` | public setters (:368 / 371 / 374 / 384) |
| **There is NO `AccountType` enum and NO `Account.IsBacktest`** | the backtest account is identified only by `Name == Account.BackTestAccountName` |
| `Cbi.Instrument.GetInstrument(string name, bool create = false)` | `public static Instrument` (Instrument.cs:820) |
| `Cbi.Instrument.GetInstrumentFuzzy(string)` / `Instrument.All` | :832 / :598 |
| `Data.TradingHours.Get(string name)` | `public static TradingHours` (TradingHours.cs:183) |
| `Data.TradingHours.All` / `String2TradingHours(string)` / `SystemDefault` | :121 / :234 / :157 |
| `Data.BarsPeriod` | `[TypeConverter(BarsPeriodConverter)] public class BarsPeriod : ICloneable`; **parameterless ctor only, no copy ctor** (BarsPeriod.cs:105). `Clone()` public (:110). `ToXml(XElement)` / `static FromXml(XElement)` (:145 / :127). |
| `BarsPeriod.BarsPeriodTypeSerialize` | `int`, `[Browsable(false)]` — **the int door onto `BarsPeriodType`; use it instead of casting** (:41) |
| `BarsPeriod.Value / Value2 / BaseBarsPeriodType / BaseBarsPeriodValue / MarketDataType` | `[Range(1,int.Max)]` on Value (:96 / 100 / 69 / 72 / 87) |
| `Data.BarsType.GetSupported()` / `GetBarsPeriodTypeName(BarsPeriodType)` / `CreateInstance(BarsPeriod)` | `public static` (BarsType.cs:277 / 246 / 229) — resolve a custom bar type's name → its registered int id (add-on bar types sit in the 2000s, e.g. `2018`) at runtime instead of hard-coding |
| `Data.Bars.GetBars(...)` | `public static void`, 16 args incl. `bool isTickReplay`, `IProgress`, `Action<Bars,ErrorCode,string,object>` callback (Bars.cs:440); `GetBarsBack(...)` :445 |
| `Data.BarsRequest` | ctors `(Instrument, int barsBack)` :123 and `(Instrument, DateTime from, DateTime to)` :136; `Request(Action<BarsRequest,ErrorCode,string>)` :161; `Bars { get; private set; }` :27. **`BarsRequest` has NO `IsTickReplay` property** — it cannot produce a tick-replay series. |
| `Data.BarsSeries.RequestBarsSeries(...)` | `internal static`, the only loader that takes `isTickReplay` (BarsSeries.cs:800) — reflection only |
| `Data.Bars.IsTickReplay` | read-only confirmation on the loaded series (Bars.cs:357) |
| `Data.Bars.BarsPeriod / Instrument / TradingHours / IsResetOnNewTradingDay / FromDate / ToDate` | chart-seed values (:312 / 341 / 359 / 348 / 329 / 283) |
| `Core.IProgress` | **public interface**, trivially implementable: `bool IsAborted {get;}`, `string Message {get;set;}`, `event EventHandler Aborted`, `void PerformStep()`, `void SetUp(long,bool)`, `void TearDown()` (core/NinjaTrader.Core/IProgress.cs) |
| `Core.Globals.AssemblyRegistry` | `public static` (Globals.cs:2648) → `GetDerivedTypes(Type, bool evalBrowsable = false)` :61/:67, `GetType(string)` :73, `IsNinjaTraderCustomAssembly(Type)` :79, `Keys` :30, `this[string]` :39 |
| `Core.Globals.MainThreadDispatcher` / `RandomDispatcher` / `ActiveWorkspace` / `UserDataDir` | Globals.cs:2217 / 2339 / — / — |
| `Core.Globals.CreateProgressWindow` | `public static Func<string, IProgress>` (Globals.cs:2138) — **builds a WPF window; never call headlessly** |
| `Core.Globals.AllWindows` | `IList<Window>` — used by Recipe C |
| `Code.Output.OutputEvent` | `public static event EventHandler<OutputEventArgs>` (core/NinjaTrader.Code/Output.cs:12) — **the Print capture hook** |
| `Code.OutputEventArgs.Message / OutputTab / IsReset` | `string / PrintTo / bool` (OutputEventArgs.cs:9-13) |
| `Cbi.Log.LogEvent` | `public static event EventHandler<LogEventArgs>` (Log.cs:108) |
| `Cbi.LogEventArgs` | `ResourceType`, `Name`, `Args`, `LogLevel`, `LogCategory`, `Time`, `Account`, `User` — **no `Message` property** (LogEventArgs.cs:60-78) |
| `Cbi.Log.SyncStrategyAnalyzerLog` / `SyncWriter` | `public static object` locks — do not take them |
| `Data.BarsSeries.SyncDownloadFromProvider` | `public static List<...>` (BarsSeries.cs:59) — historical downloads are **globally queued** through this |

### 1.7 Optimizer surface — `NinjaTrader.NinjaScript.Optimizers.Optimizer`

| Member | Notes |
|---|---|
| `Strategies` | `Collection<StrategyBase> { get; internal set; }` — **an AddOn cannot populate it.** The framework does. This is the hard limit on Recipe B. |
| `Results` | `SystemPerformance[] { get; set; }` — best-first, length `KeepBestResults`, unfilled slots have `ParameterValues == null` |
| `Progress` | `IProgress { get; set; }` — the **only** NinjaScript-level progress hook, and it is on the optimizer, not the strategy |
| `RunIteration()` | `public void` — clone template, backtest on a worker thread, store into `Results` |
| `WaitForIterationsCompleted()` | `public void` — blocks |
| `Reset(int startWith = 0)` | `public void` |
| `NumberOfIterations` | `long { get; set; }` — set in `State.Configure` |
| `KeepBestResults` | `int`, `[Range(2, int.MaxValue)]` — **must be set explicitly, >= 2** |
| `GetParametersCombinationsCount(StrategyBase)` | `public static long` |
| `OnOptimize()` | `protected internal virtual void` — "must be overridden in order to optimize a strategy" |
| `IsAborted` | **protected** — an AddOn cannot read it from outside |
| `RunBacktest(StrategyBase template, Parameter[] parameters)` | `private static StrategyBase` — the per-iteration leaf. Returns a **finished** strategy by value → the backtest completes before it returns on its worker thread. |
| `SaveStrategyPerformance(StrategyBase, object[], int)` | `private int` |
| `resetStates` (static field) | initializer **readable**: `new State[4] { SetDefaults, Configure, DataLoaded, Historical }` — the authoritative per-iteration state ladder. **`State.Active` is absent.** |
| worker record | nested class holding `{int; Dispatcher; bool; object[][]; StrategyBase}` + `threads[]`, `syncThreads`, `syncResults`, `internal bool CreateThreads` → **each backtest runs on a dedicated worker thread that owns a WPF `Dispatcher`** |
| `StrategyBase.Optimizer` / `OptimizationFitness` | public props, `[EditorBrowsable(Never)][XmlIgnore]`, setters stripped (StrategyBase.cs:1240 / 1256) |
| `StrategyBase.OptimizationParameters` | `Collection<Parameter>` — **get-only**, stripped (:1270) |
| `Parameter.Name / ParameterType / Min / Max / Increment / Value / EnumValues / NumIterations` | core/NinjaTrader.NinjaScript/Parameter.cs |
| `Parameter.SetPropertyValue(StrategyBase)` | `[EditorBrowsable(Never)] public void` (:139) |
| `OptimizationFitness.CalculatePerformanceValue(StrategyBase)` / `.Value` | ctor self-runs `SetState(SetDefaults)`, so `new MaxNetProfit()` is ready to use |

### 1.8 Strategy Analyzer GUI surface (Recipe C) — `NinjaTrader.Gui.dll`

All bodies stripped; these are **PROVEN** reachable by third-party AddOn code on 8.1.6.x–8.1.8.2.

| Member | Notes |
|---|---|
| `Gui.NinjaScript.StrategyAnalyzer.StrategyAnalyzer` | the window type; `AddNewTab(object)` public instance |
| `StrategyAnalyzerViewModel` | `saWin.DataContext as StrategyAnalyzerViewModel` |
| `StrategyAnalyzerViewModel.RunCommand` | **static field**, a `RoutedCommand` — `rc.CanExecute(null, saWin)` / `rc.Execute(null, saWin)`. **This is what the Run button raises.** |
| `StrategyAnalyzerViewModel.OnRun(object, object)` | private instance — reflection fallback |
| `StrategyAnalyzerViewModel.SelectedTab` | `StrategyAnalyzerTabControl` |
| `StrategyAnalyzerTabControl.TabStrategyProperties` | holds `InstrumentOrInstrumentList` and `StrategyTemplate` |
| `...TabStrategyProperties.StrategyTemplate` | `StrategyBase` — **replaced whenever the tab's `Strategy` changes** |
| `StrategyAnalyzerTabControl.Results` | `IList<StrategyAnalyzerGridEntry>` — **a re-run REPLACES the last entry; `Count` does not change** |
| `StrategyAnalyzerTabControl.BacktestType` / `IsProgressVisible` | run mode / busy flag |
| `StrategyAnalyzerGridEntry.Results` | `SystemPerformance` (null while running); `.StrategyName` |
| `StrategyAnalyzerTabProperties.ToStrategyCategoryType()` | maps the GUI enum onto `StrategyBase.Category` (:193) |
| `enum StrategyAnalyzerGuiBacktestType` | `Backtest, Optimize, WalkForward, WalkForwardAnchored, MultiObjective, AiGenerate` |
| `StrategyRunner` | **internal**: `CreateStrategyClone(StrategyBase, Instrument)`, `RunStrategyAsync(StrategyBase, IProgress, Action<RunResult>)`, `RunStrategy(StrategyBase, IProgress)`, `RunBacktest(StrategyBase, bool includeTradeHistory, IProgress, StrategyAnalyzerGridEntry = null)`, `SetupProgressControl(StrategyBase, IProgress)`. **Never call `RunStrategyAsync` directly — documented to deadlock the SA UI thread and crash NinjaTrader.** |
| `RunResult` | public: `Category, Duration, Executions[], Error, InstancesRan[], Orders[], Parameters[], Strategy`. **No `SystemPerformance` field** — results are read off `RunResult.Strategy.SystemPerformance`. |
| `SummaryPerformance.Calculate(SystemPerformance, PerformanceUnit, SummaryPerformanceType)` | `public static`, in NinjaTrader.Gui.dll — the SA's own summary builder; its field list is a 1:1 match to `API.md`'s `summary`. Body stripped, but **callable**. |
| `Gui.NinjaScript.StrategyRenderBase.IsInStrategyAnalyzer` | `public bool` (StrategyRenderBase.cs:147) — a user `Strategy` derives `StrategyRenderBase`, which **overrides `SetState`** (:351) and carries `ChartControl`/`ChartBars`/`ChartPanel`. Set this true if `SetState` NREs inside NinjaTrader.Gui. |

---

## 2. Recipes

All three share the same **preparation** (§2.1) and the same **results read** (§3) and **teardown** (§4).
They differ only in *how the run is hosted*.

Ordering rationale:
- **A** is the shortest path, needs no GUI window, and matches the `API.md` contract exactly. One call.
  Unverified but nothing contradicts it: `RunBacktestInternal()` exists next to it, the SA's own
  `StrategyRunner.RunBacktest(strategy, includeTradeHistory, progress, entry)` takes **no bars**, and
  the community's failed lifecycle experiment concluded "a Backtest-category strategy is hosted by
  `RunBacktest()`, not the realtime engine".
- **B** hands the hosting work (cloning, bar loading, state driving, worker thread with a Dispatcher) to
  the framework via the *documented* `OnOptimize()`/`RunIteration()` extension point. It is **not**
  cleaner than A: `Optimizer.Strategies` has an `internal` setter, so the
  AddOn cannot populate the template collection, and every shipped optimizer is hosted **by the
  Strategy Analyzer**. B is a fallback, not the default.
- **C** is the only **PROVEN** path (two published AddOns, verified on 8.1.8.2), but it needs an open,
  once-configured Strategy Analyzer window in the process.

### 2.1 Shared preparation (runs on the bridge's HTTP thread, then hands a POCO to the worker)

```csharp
// 1. Resolve the type.  VERIFIED members; use type.Assembly.CreateInstance, which is what NT itself
//    does (@StrategyGenerator.cs:1952), NOT Activator.CreateInstance on a foreign load context.
Type t = Core.Globals.AssemblyRegistry.GetType(req.Strategy)
      ?? Core.Globals.AssemblyRegistry.GetDerivedTypes(typeof(StrategyBase), false)
             .FirstOrDefault(x => !x.IsAbstract && x.Name == req.Strategy);
if (t == null) return Err(ref status, 400, "unknown strategy '" + req.Strategy + "'");

// 2. Snapshot the chart ONCE, here, on the HTTP thread, through the existing 5s-UiTimeout helper.
//    After this line the worker must never touch a Window, a ChartControl or Ui(...).
var chart = FindChart(req.Chart ?? "first");                       // NT8Bridge.cs:242
var seed  = OnChart(chart, cc =>                                   // NT8Bridge.cs:279
{
    var b = Primary(cc).Bars;                                      // NT8Bridge.cs:285
    return new SeedInfo {
        Instrument = b.Instrument,
        Period     = (Data.BarsPeriod)b.BarsPeriod.Clone(),        // GUESS: Clone is deep (ICloneable, body stripped)
        Th         = b.TradingHours,
        TickReplay = b.IsTickReplay,
        ResetDay   = b.IsResetOnNewTradingDay
    };
});

// 3. Coerce inputs against [NinjaScriptProperty] props NOW, so a bad name/type is a 400 before anything runs.
//    IsInput() already exists at NT8Bridge.cs:583 — reuse it verbatim.
foreach (var kv in req.Inputs) {
    var pi = t.GetProperty(kv.Key, BindingFlags.Public | BindingFlags.Instance);
    if (pi == null || !IsInput(pi) || !pi.CanWrite) return Err(ref status, 400, "unknown input '" + kv.Key + "'");
    job.Inputs.Add(new KeyValuePair<PropertyInfo, object>(pi, Coerce(kv.Value, pi.PropertyType, kv.Key)));
}
```

Then, **on the worker thread**:

```csharp
// 4. Construct.  StrategyBase ctors are protected (StrategyBase.cs:1541/1547) so only a concrete
//    subclass is constructible.  The ctor is believed to run SetDefaults already (see §7).
StrategyBase s = (StrategyBase)t.Assembly.CreateInstance(t.FullName);
if (s.State == State.Undefined) s.SetState(State.SetDefaults);   // belt and braces, harmless

// 5. Inputs on the raw instance, BEFORE any state past SetDefaults.  This is NT's own pattern:
//    `new EMA(){ Period = period }` (@EMA.cs:69) and `new MAX { Period = trendStrength }; max.SetState(Configure);`
//    (@CandleStickPattern.cs:291-318) both set inputs on a constructed instance and rely on them surviving.
foreach (var kv in job.Inputs) kv.Key.SetValue(s, kv.Value, null);

// 6. Data series.  ORDER MATTERS.  Instrument/BarsPeriod/TradingHours setters are FACADES over
//    Instruments[BarsInProgress] / BarsPeriods[BarsInProgress] / TradingHoursArray[BarsInProgress]
//    (bodies readable, NinjaScriptBase.cs:566/719/1041).  Assign the scalars; the arrays already exist
//    at length >= 1 on a fresh instance.
var th = job.Th ?? job.Instrument?.MasterInstrument?.TradingHours
                ?? Data.TradingHours.Get("Default 24 x 7");
s.Instrument             = job.Instrument;
s.BarsPeriod             = job.Period;
s.TradingHoursInstance   = th;                    // setter stripped -> READ IT BACK
s.IsTickReplay           = job.TickReplay;        // both accessors stripped -> READ IT BACK
s.From                   = job.From.Date;
s.To                     = job.To.Date.AddDays(1).AddSeconds(-1);

// Belt-and-braces per-series arrays.  These are the four things NinjaScriptBase.Setup() asserts on:
// BarsPeriods[0], Instruments[0], IsTickReplays[0].HasValue, TradingHoursArray[0].
s.Instruments            = new[] { job.Instrument };
s.BarsPeriods            = new[] { job.Period };
s.IsTickReplays          = new bool?[] { job.TickReplay };
s.IsResetOnNewTradingDays= new bool?[] { job.ResetDay };
s.TradingHours           = th;
s.TradingHoursArray      = new[] { th };

// 7. Run config.
s.Account   = Cbi.Account.All.FirstOrDefault(a => a.Name == Cbi.Account.BackTestAccountName);
s.Category  = Category.Backtest;
s.IncludeTradeHistoryInBacktest = true;           // setter stripped -> READ IT BACK; false => AllTrades empty
s.IsInstantiatedOnEachOptimizationIteration = false;   // true only on the optimize path with tick replay
s.PrintTo   = PrintTo.OutputTab2;                 // so captured output is separable from the user's tab
s.SetUniqueId();

// 8. VERIFY the stripped setters actually took.  Many NT8 setters silently no-op depending on State.
if (!s.IncludeTradeHistoryInBacktest) throw new Exception("IncludeTradeHistoryInBacktest rejected (State="+s.State+")");
if (job.TickReplay && !s.IsTickReplay) throw new Exception("IsTickReplay rejected (State="+s.State+")");
if (s.Account == null)                throw new Exception("no '"+Cbi.Account.BackTestAccountName+"' account in Account.All");
```

**Do not** set `OrderFillResolution` to anything but `Standard` while `IsTickReplay` is true —
`"Order Fill Resolution is not available when Tick Replay is enabled."` (Gui.Resource.resx:1055).

**Tick replay also requires** `BarsPeriod.MarketDataType == MarketDataType.Last`
(`"Tick replay on '{0}' only works with market data type '{1}'"`, Resource.resx:457). A fresh
`new BarsPeriod()` defaults to `Ask` (enum zero value), so an explicitly-built period must set it.

### 2.2 Recipe A — direct `RunBacktest()`  (try first)

**A1 — let `RunBacktest()` do everything** (fewest assumptions; try this first)

```csharp
// state left at SetDefaults; nothing pre-driven, no bars loaded by us
lines.Clear(); Code.Output.OutputEvent += OnOutput;    // capture Print (see §4.4)
try   { s.RunBacktest(); }                             // StrategyBase.cs:1867 — assumed BLOCKING
finally { Code.Output.OutputEvent -= OnOutput; }

if (s.SystemPerformance == null || s.State < State.Historical)
    throw new Exception("RunBacktest returned but nothing ran (State=" + s.State + ")");
```

Reasoning: `RunBacktest()` takes **no** `IProgress`, no bars, no dates — everything it needs is already
on the instance. `Optimizer`'s private leaf `RunBacktest(template, parameters)` returns a *finished*
`StrategyBase` by value, i.e. the leaf completes before returning, on its own worker thread. The
optimizer's `resetStates = {SetDefaults, Configure, DataLoaded, Historical}` is the *reset* ladder
between iterations — evidence the framework itself drives those states, which is why A1 leaves the
instance in `SetDefaults`.

**Switch to A2 when you see:**
- `NullReferenceException` from inside `RunBacktest()` with obfuscated frames (`CBg_003D`,
  `_003CAgileDotNetRT_003E`) → bars were never installed.
- The call returns instantly (< 100 ms) with `SystemPerformance == null` or `State` still `SetDefaults`
  → the state machine never advanced.
- `SystemPerformance` non-null but `AllTrades.Count == 0` **and** `Orders.Count == 0` **and** the run
  took < 1 s → no bars were processed.

**A2 — drive the ladder and install bars yourself**

```csharp
s.SetState(State.Configure);          // 2nd entry of Optimizer.resetStates

// Load bars.  Data.Bars.GetBars is the ONLY public loader that takes isTickReplay (Bars.cs:440);
// BarsRequest has no such property, so it cannot serve a tick-replay run.
Bars loaded = null; ErrorCode ec = ErrorCode.NoError; string emsg = null;
var got = new ManualResetEventSlim(false);
Data.Bars.GetBars(job.Instrument, job.Period, s.From, s.To, th,
                  false /*isDividendAdjusted*/, false /*isSplitAdjusted*/,
                  job.TickReplay, job.ResetDay,
                  LookupPolicies.All, MergePolicy.DoNotMerge,
                  false /*isSubscribed*/, progress, false /*calculateRollovers*/, null,
                  (b, code, msg, st) => { loaded = b; ec = code; emsg = msg; got.Set(); });
if (!got.Wait(TimeSpan.FromMinutes(10))) throw new TimeoutException("bars load");
if (ec != ErrorCode.NoError || loaded == null) throw new Exception("bars load: " + ec + " " + emsg);

var installed = new ManualResetEventSlim(false);
s.InitializeBars(new[] { loaded }, progress, _ => installed.Set());   // NinjaScriptBase.cs:1688 — ASYNC
if (!installed.Wait(TimeSpan.FromMinutes(2))) throw new TimeoutException("InitializeBars");

s.SetState(State.DataLoaded);
s.SetState(State.Historical);
s.RunBacktest();
```

**Switch to Recipe B when you see:**
- `SetState(State.Configure)` throws with a stack inside `NinjaTrader.Gui`
  (`StrategyRenderBase.SetState`, StrategyRenderBase.cs:351) → the render layer touched a null chart.
  **First try `((Gui.NinjaScript.StrategyRenderBase)s).IsInStrategyAnalyzer = true;` before Configure**
  and re-run A2. Only if that still throws, go to B.
- `SetState(State.DataLoaded)` leaves `s.State == State.Finalized` → the engine refused the hand-driven
  transition and killed the object (this is exactly what the published lifecycle experiment observed on
  8.1.7.1 when jumping straight to DataLoaded). The instance is dead; a fresh one is required. Go to B.
- `RunBacktest()` returns with `SystemPerformance == null` in **both** A1 and A2. Go to B.

### 2.3 Recipe B — one-shot `Optimizer` subclass + `RunOptimization(callback)`

Uses the **documented** extension point (`OnOptimize()` "must be overridden in order to optimize a
strategy"; `RunIteration()` "runs an iteration of backtesting for the optimizer"). The framework does
the cloning, the bar loading, the state ladder and the worker thread with its Dispatcher — everything
A has to guess at. The cost: `Optimizer.Strategies` has an **internal** setter, so this only works if
`StrategyBase.RunOptimization` (or the `StrategyBase.Optimizer` setter) is what populates it.

Precedent that `RunIteration()` works with **zero** optimization parameters:
`@StrategyGenerator.cs:453-458` sets `Strategies[0].GeneratedStrategyLogic` and calls `RunIteration()`
with no parameter loop at all. (`@DefaultOptimizer`/`@GeneticOptimizer` bail at `Count == 0` only
because they have nothing to enumerate, not because `RunIteration` requires parameters.)

```csharp
// Declared inside NT8Bridge.cs — AddOn code lives in NinjaTrader.Custom.dll, which is where NT expects
// NinjaScript types.  A subclass in any other assembly is a risk (the Optimizer setter has a
// string-keyed PropertyEditor and may only accept registered types).
public class NT8BridgeOneShotOptimizer : NinjaTrader.NinjaScript.Optimizers.Optimizer
{
    protected override void OnStateChange()
    {
        if (State == State.SetDefaults) { Name = "NT8BridgeOneShot"; KeepBestResults = 2; }  // Range(2,..)
        else if (State == State.Configure) NumberOfIterations = 1;
    }
    protected override void OnOptimize()
    {
        RunIteration();
        WaitForIterationsCompleted();     // @DefaultOptimizer omits this and lets the framework wait;
                                          // @GeneticOptimizer:431 and @StrategyGenerator:458 call it.
    }
}
```

Caller, after §2.1 preparation:

```csharp
s.IncludeTradeHistoryInBacktest = true;            // @StrategyGenerator.cs:250 — "needed trade history"
s.SupportsOptimizationGraph     = false;           // :252
s.OptimizationFitness           = new NinjaTrader.NinjaScript.OptimizationFitnesses.MaxNetProfit();
s.Optimizer                     = new NT8BridgeOneShotOptimizer();   // setter stripped -> READ IT BACK
if (s.Optimizer == null) throw new Exception("Optimizer setter rejected the type");

var done = new ManualResetEventSlim(false);
SystemPerformance perf = null;
s.RunOptimization(finished =>
{
    perf = finished.Optimizer.Results
             .FirstOrDefault(r => r != null && r.AllTrades != null)
           ?? finished.SystemPerformance;
    done.Set();
});
if (!done.Wait(JobCap)) { /* timeout path, §4.3 */ }
```

Note `IsInstantiatedOnEachOptimizationIteration` must be **true** if `IsTickReplay` is true, or NT logs
`"Optimizations with Tick Replay requires the strategy setting 'IsInstantiatedOnEachOptimizationIteration'
to be true. Continuing optimization with 'true'..."` (Resource.resx:1799) and flips it for you.

**Switch to Recipe C when you see:**
- `IndexOutOfRangeException` / `ArgumentOutOfRangeException` at `Strategies[0]` inside `OnStateChange`
  or `OnOptimize` → `Optimizer.Strategies` was never populated. `RunOptimization` is not the thing that
  wires it; only the GUI `StrategyRunner` path does. **This is the expected failure mode for B and the
  reason it is not Recipe A.**
- The `RunOptimization` callback never fires and the wait times out → `WaitForIterationsCompleted` is
  blocked on worker threads that could not start (`CreateThreads` is internal and may be false outside
  the GUI host).
- `Results` all null / every `ParameterValues == null` **and** `AllTrades == null` after the wait → the
  iteration never ran.

### 2.4 Recipe C — drive the open Strategy Analyzer window in-process (PROVEN)

The only path anyone has demonstrated. Verified by a third party on NT 8.1.6.x / 8.1.7.2 / 8.1.8.2.
Requires a Strategy Analyzer **window open in the process** with a tab that has been configured once.

```csharp
// 1. Find the window.
Window saWin = null;
var all = Core.Globals.AllWindows;
for (int i = 0; i < all.Count; i++)
    if (all[i] != null && all[i].GetType().FullName.IndexOf("StrategyAnalyzer", StringComparison.Ordinal) >= 0)
    { saWin = all[i]; break; }
if (saWin == null) throw new Exception("no Strategy Analyzer window open");

// 2. EVERYTHING below runs on saWin.Dispatcher.  Touching Account.All / Connection / any NinjaScript
//    object from a worker thread trips NT's internal Debug.Assert dialogs, which block the whole
//    process (observed in practice).
string err = (string)saWin.Dispatcher.Invoke(new Func<string>(() =>
{
    var vm  = saWin.DataContext as StrategyAnalyzerViewModel;
    var tab = vm != null ? vm.SelectedTab : null;
    if (tab == null) return "no active SA tab";

    // 3. Configure by reflection.  *** WRITE "Strategy" FIRST AND RE-RESOLVE THE CHAIN PER KEY. ***
    //    Writing "Strategy" makes NT install a FRESH StrategyTemplate; a reference resolved earlier then
    //    points at a detached object.  SetValue on it does not throw and reads its own value back, so
    //    every later key silently does nothing.  Skew to watch for: the instrument looks applied (it
    //    also lives on TabStrategyProperties) while From/To/BarsPeriod/IsTickReplay silently do not.
    //    From / To / BarsPeriod / IsTickReplay are writable ONLY on the strategy template.
    Set(tab, "Strategy", req.Strategy);
    Set(tab, "InstrumentOrInstrumentList", job.InstrumentName);
    Set(tab, "From", job.From); Set(tab, "To", job.To);
    Set(tab, "BarsPeriod", job.Period); Set(tab, "IsTickReplay", job.TickReplay);
    foreach (var kv in job.Inputs) Set(tab, kv.Key.Name, kv.Value);

    // 4. Capture the CURRENT result BEFORE running.
    prevPerf = (tab.Results != null && tab.Results.Count > 0) ? tab.Results[tab.Results.Count - 1].Results : null;

    // 5. Fire the SA's own command.  NT then runs it correctly on ITS background thread.
    //    *** NEVER call StrategyRunner.RunStrategyAsync directly: on the SA UI thread it deadlocks and
    //        crashes NinjaTrader. ***
    var rc = StrategyAnalyzerViewModel.RunCommand as RoutedCommand;   // STATIC field
    if (rc == null) return "RunCommand is not a RoutedCommand";       // fallback: private OnRun(null,null)
    if (!rc.CanExecute(null, saWin)) return "SA tab not runnable (missing strategy/instrument/dates, or busy)";
    rc.Execute(null, saWin);
    return null;
}));
if (err != null) throw new Exception(err);

// 6. Poll off-thread, 2 s ticks, Dispatcher.Invoke per read.
//    *** Compare the SystemPerformance BY REFERENCE, never tab.Results.Count — a re-run REPLACES the
//        last grid entry, so the count is identical. ***
//    For optimize/walk-forward also wait until tab.IsProgressVisible has gone false.
```

Recipe C's `Set(tab, key, value)` must re-resolve `tab` → `tab.TabStrategyProperties` →
`tab.TabStrategyProperties.StrategyTemplate` on **every** key and write to whichever of the three has a
writable property of that name, then read the value back off a **freshly resolved** chain.

Type conversion at the boundary: `Instrument.GetInstrument(name)`, `DateTime.Parse(invariant)`,
`new BarsPeriod { BarsPeriodType = (BarsPeriodType)Enum.Parse(...), Value, Value2 }` — a **numeric**
type token parses through `Enum.Parse` too, which is how custom add-on bar types (2018) get through.

**Recipe C's limits** — say them in the 400/500 message rather than pretending:
- Needs a Strategy Analyzer window open. Nobody has demonstrated a fully code-created tab running.
- `PrintTo`/output capture cannot separate this run from anything else.
- An optimize/walk-forward run is refused outright when the template's `OptimizationParameters` is
  empty (`"The strategy must have at least one parameter to optimize."`).

---

## 3. Results mapping — JSON field → exact property path

Given `var perf = s.SystemPerformance; var all = perf.AllTrades; var tp = all.TradesPerformance; var cur = tp.Currency;`

### `summary`

| JSON | Path | Status |
|---|---|---|
| `trades` | `all.TradesCount` | VERIFIED (`@MaxPercentProfitable.cs:11`) |
| `winners` | `all.WinningTrades.TradesCount` | VERIFIED |
| `losers` | `all.LosingTrades.TradesCount` | VERIFIED (`@StrategyGenerator.cs:163-164`) |
| `winRate` | `all.TradesCount == 0 ? 0 : (double)all.WinningTrades.TradesCount / all.TradesCount` | VERIFIED |
| `netProfit` | `tp.NetProfit` (`== GrossLoss + GrossProfit`, body readable) | VERIFIED |
| `grossProfit` | `tp.GrossProfit` | VERIFIED |
| `grossLoss` | `tp.GrossLoss` — **negative** | VERIFIED |
| `profitFactor` | `tp.ProfitFactor` | VERIFIED |
| `commission` | `tp.TotalCommission` | VERIFIED |
| `maxDrawdown` | `cur.Drawdown` | VERIFIED path; **GUESS** sign is `<= 0` |
| `avgTrade` | `cur.AverageProfit` | VERIFIED |
| `avgWinner` | `all.WinningTrades.TradesPerformance.Currency.AverageProfit` | VERIFIED path pattern (`@MaxWinLossRatio.cs:10-13` uses `.Percent`) |
| `avgLoser` | `all.LosingTrades.TradesPerformance.Currency.AverageProfit` | same; **GUESS** negative |
| `largestWinner` | `cur.LargestWinner` | VERIFIED |
| `largestLoser` | `cur.LargestLoser` | VERIFIED; **GUESS** negative |
| `avgMae` | `cur.AverageMae` | VERIFIED; **GUESS** positive magnitude (`@MinAvgMae.cs:9` uses `1.0 - AverageMae`) |
| `avgMfe` | `cur.AverageMfe` | VERIFIED |
| `avgBarsInTrade` | `tp.AverageBarsInTrade` | VERIFIED |
| `sharpe` | `tp.SharpeRatio` | VERIFIED; expect `0` or `NaN` on a 2-day sample (monthly-return based) |
| `maxConsecWinners` | `tp.MaxConsecutiveWinner` | VERIFIED — **singular** |
| `maxConsecLosers` | `tp.MaxConsecutiveLoser` | VERIFIED — **singular** |

Two integrity identities to assert before trusting anything:
`summary.trades == trades.Length` and `round(grossProfit + grossLoss, 2) == round(netProfit, 2)`.

Optional alternative: `Gui.TradePerformance`'s own
`SummaryPerformance.Calculate(perf, PerformanceUnit.Currency, SummaryPerformanceType.AllTrades)` is
`public static` and its field list is a 1:1 match to this table (`AverageBarsInTrade`, `AverageMae`,
`AverageMfe`, `AverageLosingTrade`, `AverageTrade`, `AverageWinningTrade`, `Commission`, `GrossLoss`,
`GrossProfit`, `NumWinningTrades`, `NumLosingTrades`, `NumEvenTrades`, `MaxDrawdown`, `MaxConsecWinners`,
`MaxConsecLosers`, `LargestWinningTrade`, `LargestLosingTrade`, `PercentProfitable`, `ProfitFactor`,
`SharpeRatio`, `SortinoRatio`, `TotalNumTrades`, `TotalNetProfit`). Use it only if the hand-built
summary disagrees with the SA grid — it adds a NinjaTrader.Gui dependency to the results path.

### `trades[]` — `foreach (Trade t in all)` in collection order

| JSON | Path | Status |
|---|---|---|
| `n` | `t.TradeNumber` | VERIFIED |
| `side` | `t.Entry.MarketPosition.ToString()` → `"Long"` / `"Short"` | VERIFIED (help guide example) |
| `qty` | `t.Quantity` | VERIFIED |
| `entryName` | `t.Entry.Name` | VERIFIED |
| `exitName` | `t.Exit.Name` | VERIFIED |
| `entryTime` | `t.Entry.Time` | VERIFIED |
| `exitTime` | `t.Exit.Time` | VERIFIED |
| `entryPrice` | `t.Entry.Price` | VERIFIED |
| `exitPrice` | `t.Exit.Price` | VERIFIED |
| `pnl` | `t.ProfitCurrency` | VERIFIED |
| `pnlPoints` | `t.ProfitPoints` (doc-comment: "for one traded unit") | VERIFIED |
| `mae` | `t.MaePoints` | VERIFIED path. **Decision: use POINTS.** `API.md`'s example row has `pnlPoints 2.0, mae 0.5, mfe 2.25` — those are points, not dollars. `MaeCurrency` is the alternative. |
| `mfe` | `t.MfePoints` | same |
| `bars` | `t.Exit.BarIndex - t.Entry.BarIndex` | **GUESS.** `Trade` has no `BarsInTrade` (decompile + help guide both confirm). `Execution.BarIndex` is public but `[EditorBrowsable(Never)]` and undocumented. **If every execution reports `BarIndex == 0`, fall back to counting primary bars between `Entry.Time` and `Exit.Time`, or emit `null`.** |

Use the existing serializer helpers for every value: `D(double)` (NT8Bridge.cs:923 — `NaN`/`Inf` → `null`),
`Tm(DateTime)` (:929 — `MinValue` → `null`), `Q(string)` (:897), `Scalar(object)` (:935).
`D()` already handles the `NaN` Sharpe and `Infinity` profit factor cases correctly.

### `output`

`[]` is contractually allowed (`API.md`: "if capturable, else `[]`"). If you wire it: subscribe
`Code.Output.OutputEvent` before the run, set `s.PrintTo = PrintTo.OutputTab2`, filter
`e.OutputTab == PrintTo.OutputTab2 && !e.IsReset`. See §4.4 for the thread rules. **Never call
`Output.Reset` / `ClearOutputWindow()`** — it wipes the user's real Output window.

### `error`

`Cbi.Log.LogEvent` (Log.cs:108) is the strategy-error channel (`LogAndPrint` lands in both channels).
`LogEventArgs` has **no `Message`** — format `ResourceType` + `Name` + `Args` yourself, filter by
`LogLevel >= Warning` and by the run's `Time` window. The feed is global and unfiltered.

---

## 4. Threading, cleanup, and the 15-minute cap

### 4.1 Thread topology

Three threads, and the run belongs to none of the first two.

| Thread | Owner | Rule |
|---|---|---|
| `NT8Bridge` listener | `NT8Bridge.cs:120` | accepts, queues to ThreadPool. Never blocks. |
| ThreadPool worker per request | `NT8Bridge.cs:156` | parse, validate, chart snapshot, enqueue, return in single-digit ms. **A 15-minute handler here starves the pool and stalls the MCP client.** |
| **`NT8Bridge-bt`** (new) | dedicated, one job at a time | builds the strategy, runs it, reads `SystemPerformance`, tears down. **Never touches a `Window`, a `ChartControl`, or `Ui(...)`.** |

- **Never** run the backtest on `Application.Current.Dispatcher` / `Globals.MainThreadDispatcher` — it
  freezes all of NT8. `StrategyBase`/`NinjaScriptBase` contain zero `Application.Current` and zero
  `SynchronizationContext` references; nothing in the backtest path needs the UI thread.
- Create the worker with `IsBackground = true` so a stuck run cannot hold NT8 open.
- **`GUESS`: set `ApartmentState.STA`.** Nothing in the source demands it; the reason is that NT's own
  optimizer worker record carries a WPF `Dispatcher`, and a Dispatcher belongs to the thread that made
  it. Start MTA; switch to STA on the first `"The calling thread must be STA"` or Dispatcher error.
- `NinjaScriptBase.Dispatcher` has an **internal** setter — an AddOn cannot assign it and it is `null`
  on a freshly constructed instance. If that turns out to matter, the reflection escape is
  `typeof(NinjaScriptBase).GetProperty("Dispatcher").GetSetMethod(true).Invoke(s, new object[]{ Core.Globals.RandomDispatcher })`.
- **One backtest at a time.** There is no NT8-side lock: grep for `IsBacktesting` / `BacktestLock` /
  `IsOptimizing` across Core returns nothing. The single-worker queue is *your* policy (`API.md:110`),
  and it is mandatory — `StrategyBase.All` is a shared static collection and the DB helpers are
  instance-level.
- Historical downloads serialize globally behind `BarsSeries.SyncDownloadFromProvider` (BarsSeries.cs:59).
  If the user is loading a chart at the same time, your bars load stalls. Give the load a minutes-scale
  timeout and report `"state":"timeout"` rather than hanging.
- `jobGate` must only ever be held for a dictionary/queue operation, **never across a run**. It is a
  separate lock from the existing `gate` (`NT8Bridge.cs:45`), and the two must never be nested.

### 4.2 Cleanup — in a `finally`, on the worker thread, in this order

```csharp
finally
{
    try { snapshot = Snapshot(s.SystemPerformance); } catch { }  // COPY OUT BEFORE TEARDOWN
    try { s.SetState(State.Terminated); } catch { }
    try { s.SetState(State.Finalized);  } catch { }              // State.cs:21-23
    try { if (s.Id != 0) s.DbRemove(); } catch { }               // StrategyBase.cs:1667
    job.Strat = null; s = null;
}
```

- There is **no `Dispose()`** and **no `IDisposable`** on `NinjaScriptBase` or `StrategyBase`. The only
  `Dispose`-named member on the whole base type is `BarsToDispose` (NinjaScriptBase.cs:445). Teardown is
  exclusively `SetState(Terminated)` then `SetState(Finalized)`.
- Build the result JSON **before** clearing `job.Strat`. `SystemPerformance.AllTrades` has an internal
  setter and an `internal void Reset()`; after Finalized the collection may be gone.
- `DbAdd()` is internal, so it is unprovable whether a headless run registers itself in the strategy DB.
  The `Id != 0` guard makes `DbRemove()` safe either way. Symptom of a leak: new rows in the NT8
  Strategies grid, or `StrategyBase.All` growing every run (`All` calls `DbLoad()` on every read, body
  readable at :782). Nuclear option: `StrategyBase.DbRemoveByCategory(Category.Backtest, false)`.
- **Never reuse an instance.** Fresh instance per POST. `IsInstantiatedOnEachOptimizationIteration`
  exists precisely because in-place reset is unreliable.

### 4.3 The 15-minute cap (`API.md:111`)

```csharp
private static readonly TimeSpan JobCap = TimeSpan.FromMinutes(15);

// Preferred shape: the worker owns the wait, so the cap fires on a thread that is allowed to act.
if (!job.Done.WaitOne(JobCap)) { job.State = "timeout"; Terminate(job); return; }   // do NOT wait for the unwind

private static void Terminate(Job job)     // shared by DELETE, the cap, and shutdown
{
    job.Cancel = true;                      // volatile
    var s = job.Strat;
    try { if (s != null) s.SetState(State.Terminated); }
    catch (Exception ex) { Log("terminate " + job.Id + ": " + ex.Message); }
}
```

Honest caveat to put in `NOTES.md`: if `RunBacktest()` is synchronous (the working assumption), the
worker is **inside** it and cannot observe the cap. Then the cap can only fire from the timer thread,
and `SetState(State.Terminated)` from a foreign thread is unverified for safety. If that is unsafe, the
cap degrades to "mark `timeout`, abandon the run, refuse further jobs until it unwinds" — still better
than blocking, but say so rather than pretending the run was killed. `DELETE /backtest/{id}` has the
same limitation: return `{"ok":true}` for the *record*, and only report `cancelled` once the worker
actually unwinds.

`Core.IProgress.IsAborted` is the polite abort channel, but **`RunBacktest()` takes no `IProgress`**, so
in Recipe A there is nothing to poll it. Only the bar load (`Bars.GetBars`, `InitializeBars`) and
Recipe B's `Optimizer.Progress` consume it.

### 4.4 Print capture rules

```csharp
private static void OnOutput(object sender, Code.OutputEventArgs e)
{
    try
    {
        if (e == null || e.IsReset || e.OutputTab != PrintTo.OutputTab2) return;
        lock (outGate) { if (outBuf != null && outBuf.Count < 20000) outBuf.Add(e.Message); }
    }
    catch { }        // this runs on NT8's producer thread — never throw, never do I/O or JSON here
}
```

`OutputEvent` fires on the **producer's** thread. Proof: NT's own Output window subscribes to the same
event and immediately batches behind a `lockCookie` + a `System.Timers.Timer`
(`NinjaScriptOutput.cs:42/54/261`). Subscribe before the run, unsubscribe in the `finally`.

### 4.5 Shutdown

`Stop()` (`NT8Bridge.cs:135`) is called from `OnWindowDestroyed(ControlCenter)` (:102) and from
`State.Terminated` (:79) — i.e. **on the Control Center's UI thread**. So:

```csharp
// in Stop(), after the listener is closed, OUTSIDE lock (gate):
CancelAll("shutdown");
jobSignal.Set();
try { if (jobThread != null) jobThread.Join(3000); } catch { }   // BOUNDED, or closing NT8 hangs
jobThread = null;
```

Reuse the existing `volatile bool running` (`NT8Bridge.cs:52`) as the worker loop's exit condition so
one `Stop()` kills both threads.

---

## 5. Integration into `NT8Bridge.cs`

### 5.1 Checklist

1. `Version` (`:40`) `"1.0.0"` → `"1.1.0"`. `/health` reports it as `addonVersion` (`:307`); leaving it
   at 1.0.0 makes `/health` lie and destroys the only baseline check that the new build is loaded.
2. Header comment (`:8-13`): add the five routes; **delete the blanket "no code path submits an order"
   sentence**. Backtests submit simulated orders into the Backtest account. The no-live-order guarantee
   still holds (account name validated against `Account.BackTestAccountName`), but say it accurately.
3. Paste the JSON parser (§5.2) after `Scalar()` at `:945`, inside the existing `// ── JSON ──` region.
4. Repoint `Screenshot()`'s regex body parse (`:740`) at the new parser — one body path, not two.
5. Add the job state block + `Job` class beside the existing state fields (`:44-57`).
6. Add the five route lines to `Route()` before the `status = 404` at `:223`.
7. Start `jobThread` in `Start()` after `listenThread.Start()` (`:121`); stop it in `Stop()` beside
   `listenThread.Join(2000)` (`:144`), **outside** `lock (gate)`.
8. Call `JsonSelfTest()` once from `Start()` inside the existing `try`, before `listener.Start()`.
9. `using System.Collections.Concurrent;` for `BlockingCollection`. Everything else is already imported
   (`:16-33` — `Reflection`, `Threading`, `Globalization`, `Text`, `NinjaTrader.Cbi`,
   `NinjaTrader.NinjaScript`, `NinjaTrader.Gui.Chart` are all there).

### 5.2 JSON parser — paste verbatim after `NT8Bridge.cs:945`

No Newtonsoft anywhere in `NinjaTrader.Custom.csproj` (grep returns zero hits), and none is being added.
`System.Web.Extensions` **is** referenced (`NinjaTrader.Custom.csproj:33`) so
`JavaScriptSerializer.DeserializeObject` is a zero-dependency alternative — but its shape differs
(`object[]` not `List<object>`, `Int32`/`Decimal` not `double`, **case-sensitive** keys), which would
ripple through every accessor. Use this parser; it matches the serializer's style and stays self-contained.

```csharp
// ── JSON parse (POST bodies; everything above is the other direction) ──────
// Minimal recursive descent. object -> Dictionary<string,object> (case-insensitive keys),
// array -> List<object>, number -> double, string -> string, true/false -> bool, null -> null.
// Anything malformed throws, which Handle() turns into a 500 (BacktestStart turns it into a 400).
private static object ParseJson(string s)
{
	if (string.IsNullOrEmpty(s)) return null;
	int i = 0;
	object v = JVal(s, ref i);
	JWs(s, ref i);
	if (i < s.Length) throw new Exception("trailing JSON at " + i);
	return v;
}

private static void JWs(string s, ref int i) { while (i < s.Length && char.IsWhiteSpace(s[i])) i++; }

private static object JVal(string s, ref int i)
{
	JWs(s, ref i);
	if (i >= s.Length) throw new Exception("JSON ended early");
	switch (s[i])
	{
		case '{': return JObj(s, ref i);
		case '[': return JArr(s, ref i);
		case '"': return JStr(s, ref i);
		case 't': JLit(s, ref i, "true");  return true;
		case 'f': JLit(s, ref i, "false"); return false;
		case 'n': JLit(s, ref i, "null");  return null;
		default:  return JNum(s, ref i);
	}
}

private static void JLit(string s, ref int i, string lit)
{
	if (i + lit.Length > s.Length || string.CompareOrdinal(s, i, lit, 0, lit.Length) != 0)
		throw new Exception("bad JSON literal at " + i);
	i += lit.Length;
}

private static Dictionary<string, object> JObj(string s, ref int i)
{
	var map = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
	i++;                                            // '{'
	JWs(s, ref i);
	if (i < s.Length && s[i] == '}') { i++; return map; }
	while (true)
	{
		JWs(s, ref i);
		if (i >= s.Length || s[i] != '"') throw new Exception("expected JSON key at " + i);
		string key = JStr(s, ref i);
		JWs(s, ref i);
		if (i >= s.Length || s[i] != ':') throw new Exception("expected ':' at " + i);
		i++;
		map[key] = JVal(s, ref i);
		JWs(s, ref i);
		if (i >= s.Length) throw new Exception("JSON ended early");
		if (s[i] == ',') { i++; continue; }
		if (s[i] == '}') { i++; return map; }
		throw new Exception("expected ',' or '}' at " + i);
	}
}

private static List<object> JArr(string s, ref int i)
{
	var list = new List<object>();
	i++;                                            // '['
	JWs(s, ref i);
	if (i < s.Length && s[i] == ']') { i++; return list; }
	while (true)
	{
		list.Add(JVal(s, ref i));
		JWs(s, ref i);
		if (i >= s.Length) throw new Exception("JSON ended early");
		if (s[i] == ',') { i++; continue; }
		if (s[i] == ']') { i++; return list; }
		throw new Exception("expected ',' or ']' at " + i);
	}
}

private static string JStr(string s, ref int i)
{
	var sb = new StringBuilder();
	i++;                                            // opening quote
	while (i < s.Length)
	{
		char c = s[i++];
		if (c == '"') return sb.ToString();
		if (c != '\\') { sb.Append(c); continue; }
		if (i >= s.Length) break;
		char e = s[i++];
		switch (e)
		{
			case '"':  sb.Append('"');  break;
			case '\\': sb.Append('\\'); break;
			case '/':  sb.Append('/');  break;
			case 'b':  sb.Append('\b'); break;
			case 'f':  sb.Append('\f'); break;
			case 'n':  sb.Append('\n'); break;
			case 'r':  sb.Append('\r'); break;
			case 't':  sb.Append('\t'); break;
			case 'u':
				if (i + 4 > s.Length) throw new Exception("bad \\u at " + i);
				sb.Append((char)int.Parse(s.Substring(i, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture));
				i += 4;
				break;
			default: throw new Exception("bad escape at " + i);
		}
	}
	throw new Exception("unterminated JSON string");
}

private static object JNum(string s, ref int i)
{
	int start = i;
	if (i < s.Length && (s[i] == '-' || s[i] == '+')) i++;
	while (i < s.Length && (char.IsDigit(s[i]) || s[i] == '.' || s[i] == 'e' || s[i] == 'E' || s[i] == '-' || s[i] == '+')) i++;
	double d;
	if (!double.TryParse(s.Substring(start, i - start), NumberStyles.Float, CultureInfo.InvariantCulture, out d))
		throw new Exception("bad JSON number at " + start);
	return d;
}

// typed reads off a parsed body
private static object JGet(Dictionary<string, object> m, string key) { object v; return m != null && m.TryGetValue(key, out v) ? v : null; }
private static string JGetStr(Dictionary<string, object> m, string key, string dflt) { var v = JGet(m, key) as string; return string.IsNullOrEmpty(v) ? dflt : v; }
private static bool JGetBool(Dictionary<string, object> m, string key, bool dflt) { object v = JGet(m, key); return v is bool ? (bool)v : dflt; }
private static int JGetInt(Dictionary<string, object> m, string key, int dflt) { object v = JGet(m, key); return v is double ? (int)(double)v : dflt; }
private static Dictionary<string, object> JGetMap(Dictionary<string, object> m, string key) { return JGet(m, key) as Dictionary<string, object>; }

/// <summary>Parsed JSON back to a JSON string, so the status document can echo `inputs` verbatim.
/// Scalars go through the existing Scalar().</summary>
private static string Ser(object v)
{
	var map = v as Dictionary<string, object>;
	if (map != null)
	{
		var ps = new List<string>();
		foreach (var kv in map) ps.Add(P(kv.Key, Ser(kv.Value)));
		return Obj(ps.ToArray());
	}
	var list = v as List<object>;
	if (list != null) return Arr(list.Select(Ser));
	return Scalar(v);
}

/// <summary>Every JSON number arrives as double; a [NinjaScriptProperty] is usually int, long, bool or
/// an enum. Throws with the property name so BacktestStart can 400 with something readable.</summary>
private static object Coerce(object v, Type t, string name)
{
	try
	{
		if (t.IsEnum) return v is string ? Enum.Parse(t, (string)v, true) : Enum.ToObject(t, Convert.ToInt32(v, CultureInfo.InvariantCulture));
		var nn = Nullable.GetUnderlyingType(t);
		if (nn != null) return v == null ? null : Coerce(v, nn, name);
		return Convert.ChangeType(v, t, CultureInfo.InvariantCulture);
	}
	catch { throw new Exception("input '" + name + "' is not a " + t.Name); }
}

private static string Err(ref int status, int code, string msg) { status = code; return Obj(P("error", Q(msg))); }

/// <summary>One assert of the parser against the serializer. Costs microseconds at load; a broken
/// parser says so in the log and in /log instead of failing the first POST.</summary>
private static void JsonSelfTest()
{
	const string src = "{\"a\":[1,-2.5e3,true,null,\"q\\\"\\u0041\\n\"],\"b\":{\"c\":0.25}}";
	var m = ParseJson(src) as Dictionary<string, object>;
	var a = JGet(m, "a") as List<object>;
	bool ok = m != null && a != null && a.Count == 5
		&& (double)a[0] == 1 && (double)a[1] == -2500 && (bool)a[2] && a[3] == null && (string)a[4] == "q\"A\n"
		&& JGetMap(m, "B") != null                               // keys are case-insensitive
		&& (double)JGet(JGetMap(m, "b"), "c") == 0.25
		&& ParseJson(Ser(m)) is Dictionary<string, object>;      // round trip
	Log("json self-test " + (ok ? "ok" : "FAILED"));
}
```

### 5.3 Route additions — `NT8Bridge.cs:203-224`

`ref status` is legal: `status` is definitely assigned at `:200`.

```csharp
if (method == "GET" && seg.Length == 1)
{
	...                                                  // existing health/windows/charts/output/account/log
	if (seg[0] == "strategies")	return Strategies();
	if (seg[0] == "backtests")	return Backtests();
}
if (seg.Length >= 1 && seg[0] == "backtest")
{
	if (seg.Length == 1 && method == "POST")	return BacktestStart(body, ref status);
	if (seg.Length == 2 && method == "GET")		return BacktestStatus(seg[1], ref status);
	if (seg.Length == 2 && method == "DELETE")	return BacktestCancel(seg[1], ref status);
}

status = 404;                                            // existing line 223
```

### 5.4 Job state — paste beside `NT8Bridge.cs:44-57`

```csharp
// ── backtests ──────────────────────────────────────────────────────────
private static readonly object jobGate = new object();
private static readonly Dictionary<string, Job> jobs = new Dictionary<string, Job>(StringComparer.OrdinalIgnoreCase);
private static readonly Queue<Job> jobQ = new Queue<Job>();
private static readonly AutoResetEvent jobSignal = new AutoResetEvent(false);
private static Thread jobThread;
private static int jobCounter;
private static readonly TimeSpan JobCap = TimeSpan.FromMinutes(15);   // API.md: cap a run, then terminate

/// <summary>One queued or finished backtest. Written by the worker, read by HTTP threads: every field
/// a reader touches is either volatile or only written before State leaves "running".</summary>
private sealed class Job
{
	public string			Id;
	public volatile string	State = "queued";        // queued|running|done|error|timeout|cancelled
	public volatile bool	Cancel;

	// request, echoed back in the status document
	public string			Strategy;
	public string			InstrumentName = "";
	public string			PeriodText = "";
	public DateTime			From, To;
	public bool				TickReplay;
	public string			InputsJson = "{}";       // Ser() of the parsed inputs map

	// resolved on the HTTP thread (the only chart touch), consumed off any thread
	public Cbi.Instrument	Instrument;
	public Data.BarsPeriod	BarsPeriod;
	public Data.TradingHours TradingHours;
	public bool				ResetDay;
	public List<KeyValuePair<PropertyInfo, object>> Inputs = new List<KeyValuePair<PropertyInfo, object>>();
	public Type				Type;

	public DateTime			QueuedAt, StartedAt, FinishedAt;
	public string			ResultJson;              // "summary"+"trades"+"output" pairs, built on the worker
	public string			Error;
	public NinjaScript.StrategyBase Strat;           // live only while running, for the Terminated path
	public readonly ManualResetEvent Done = new ManualResetEvent(false);
}
```

### 5.5 Worker

```csharp
// in Start(), after listenThread.Start():
jobThread = new Thread(Worker) { IsBackground = true, Name = "NT8Bridge-bt" };
// jobThread.SetApartmentState(ApartmentState.STA);   // GUESS: only if MTA throws Dispatcher/STA errors
jobThread.Start();

/// <summary>One backtest at a time, forever, on one thread that is neither the listener nor any
/// dispatcher. Nothing in here may touch a Window, a ChartControl or Ui(...).</summary>
private static void Worker()
{
	while (running)
	{
		Job job = null;
		lock (jobGate) if (jobQ.Count > 0) job = jobQ.Dequeue();
		if (job == null) { jobSignal.WaitOne(500); continue; }
		if (job.Cancel) { job.State = "cancelled"; job.FinishedAt = DateTime.Now; job.Done.Set(); continue; }

		job.StartedAt	= DateTime.Now;
		job.State		= "running";
		Log("backtest " + job.Id + " start " + job.Strategy + " " + job.InstrumentName);
		try
		{
			RunOne(job);
			if (job.State == "running") job.State = job.Cancel ? "cancelled" : "done";
		}
		catch (Exception ex)
		{
			job.Error = ex.Message;
			job.State = "error";
			Log("backtest " + job.Id + ": " + ex);
		}
		finally
		{
			job.FinishedAt	= DateTime.Now;
			job.Strat		= null;
			job.Done.Set();
			Log("backtest " + job.Id + " " + job.State + " in "
				+ (job.FinishedAt - job.StartedAt).TotalSeconds.ToString("F1", CultureInfo.InvariantCulture) + "s");
		}
	}
}

private static void CancelAll(string why)
{
	Job[] all;
	lock (jobGate) { all = jobs.Values.ToArray(); jobQ.Clear(); }
	foreach (var j in all)
		if (j.State == "queued" || j.State == "running") { Terminate(j); j.State = "cancelled"; j.Error = why; j.Done.Set(); }
}
```

### 5.6 Handler signatures

```csharp
/// <summary>Every non-abstract StrategyBase in NinjaTrader.Custom, with its [NinjaScriptProperty]
/// inputs and their SetDefaults values. No chart, no dispatcher: pure reflection on the HTTP thread.
/// KEY ON THE TYPE NAME, NOT ON instance.Name — SampleMACrossOver's Name is the localized resource
/// string "Sample MA crossover" (Resource.resx / @SampleMACrossOver.cs:25), which no POST will send.
/// Emit both if you want: "name" = Type.Name, "displayName" = instance.Name.</summary>
private static string Strategies() { ... return Arr(items); }

/// <summary>Validate everything here, on the HTTP thread, then queue and return 202. The only slow
/// thing allowed is the single OnChart() snapshot, which carries the existing 5s UiTimeout.</summary>
private static string BacktestStart(string body, ref int status)
{
	Dictionary<string, object> req;
	try { req = ParseJson(body) as Dictionary<string, object>; }
	catch (Exception ex) { return Err(ref status, 400, "bad JSON body: " + ex.Message); }
	if (req == null) return Err(ref status, 400, "body must be a JSON object");

	string account = JGetStr(req, "account", "Backtest");
	if (!string.Equals(account, Cbi.Account.BackTestAccountName, StringComparison.OrdinalIgnoreCase))
		return Err(ref status, 400, "backtests run on the " + Cbi.Account.BackTestAccountName + " account only");

	// ... resolve strategy type, chart snapshot, instrument, barsPeriod, from/to, inputs — 400 on each miss

	var job = new Job { Id = "b" + Interlocked.Increment(ref jobCounter), QueuedAt = DateTime.Now, /* ... */ };
	lock (jobGate) { jobs[job.Id] = job; jobQ.Enqueue(job); }
	jobSignal.Set();
	Log("backtest " + job.Id + " queued " + job.Strategy);
	status = 202;
	return Obj(P("id", Q(job.Id)), P("state", Q(job.State)));
}

private static string BacktestStatus(string id, ref int status) { /* dict lookup + JobJson(job, true) */ }
private static string Backtests()                               { /* jobs ordered by QueuedAt, JobJson(j, false) */ }
private static string BacktestCancel(string id, ref int status) { /* Terminate(job); jobs.Remove(id); {"ok":true} */ }

/// <summary>full=false is the /backtests row; full=true adds inputs, summary, trades, output.</summary>
private static string JobJson(Job job, bool full)
{
	var pairs = new List<string>
	{
		P("id", Q(job.Id)), P("state", Q(job.State)), P("strategy", Q(job.Strategy)),
		P("instrument", Q(job.InstrumentName)), P("period", Q(job.PeriodText)),
		P("from", Tm(job.From)), P("to", Tm(job.To)), P("tickReplay", job.TickReplay ? "true" : "false"),
		P("startedAt", Tm(job.StartedAt)), P("finishedAt", Tm(job.FinishedAt)),
		P("seconds", D(job.FinishedAt == DateTime.MinValue ? double.NaN : (job.FinishedAt - job.StartedAt).TotalSeconds)),
		P("error", Q(job.Error))
	};
	if (!full) return Obj(pairs.ToArray());
	pairs.Add(P("inputs", job.InputsJson));
	pairs.Add(job.State == "done" && job.ResultJson != null
		? job.ResultJson                                    // already "summary":{..},"trades":[..],"output":[]
		: P("summary", "null") + "," + P("trades", "null") + "," + P("output", "[]"));
	return Obj(pairs.ToArray());
}
```

`server.py:195` treats a response with no `"id"` key as an error document, so `POST /backtest` must
return `{"id":"b1","state":"queued"}` with 202 on success. It never sends `account`, so `"Backtest"`
must be the default. It polls `GET /backtest/{id}` every `POLL_S` until `state` leaves `queued|running`.

**Known slow leak, deliberately not fixed:** `jobs` only shrinks on DELETE. Cap it (drop the oldest
finished job past 50) if it ever matters — three lines.

---

## 6. Bring-up and verification

### 6.1 Step 0 — baseline that the new build is loaded

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:7891/health     # must be 200
curl -sS http://localhost:7891/health                                       # addonVersion must be 1.1.x
```

`{"error":"no route for POST /backtest"}` with 404 = the old 1.0.0 AddOn is still in memory. That 404 is
the baseline proof the new build is **not** loaded.

Deployment note: writing the `.cs` into `bin\Custom` while NinjaTrader runs makes it recompile and
hot-reload `NinjaTrader.Custom.dll` by itself (~19 s, measured on 8.1.7.2). Two teeth: it restarts
running strategies/indicators mid-flight, and it does **not** close already-open AddOn windows — those
keep executing the **old** assembly while the new one's statics start empty, and neither statics nor
`Type` identity can detect it.

### 6.2 Step 1 — the strategy is visible

```bash
curl -sS http://localhost:7891/strategies
```

Must list `SampleMACrossOver` (inputs `Fast`=10, `Slow`=25) **and** your own strategy, including any
`[NinjaScriptProperty]` inputs it **inherits from a base class**. If `SampleMACrossOver` shows as
`"Sample MA crossover"`, the lookup keyed on the instance `Name` (a localized resource string) instead
of the type name — fix that before the smoke test, don't work around it.

### 6.3 Step 2 — confirm the live chart

```bash
curl -sS http://localhost:7891/charts
```

`/charts` reports the chart's live instrument and period (e.g. `instrument "ES 12-26"`,
`period "2 Renko"`). A saved workspace XML can be **stale** — trust this call, not the workspace file.

### 6.4 Smoke test — `SampleMACrossOver`

`%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\Strategies\@SampleMACrossOver.cs` (note the `@`
prefix on shipped files). Two `[NinjaScriptProperty]` inputs, `Fast` 10 / `Slow` 25. No dependency on
tick-by-tick tape, so it is the only target that can actually prove the endpoint works. Before running,
confirm minute data for **every** trading day in the window is on disk under
`%USERPROFILE%\Documents\NinjaTrader 8\db\minute\<instrument>\`.

```bash
curl -sS -X POST http://localhost:7891/backtest \
  -H 'Content-Type: application/json' \
  -d '{"strategy":"SampleMACrossOver","instrument":"ES 12-26","barsPeriod":{"type":"Minute","value":5},"from":"2026-09-10","to":"2026-09-17","tickReplay":false,"inputs":{"Fast":10,"Slow":25},"account":"Backtest"}'
# expect: 202 {"id":"b1","state":"queued"}
```

Poll (no `jq` needed; `python` does the job):

```bash
ID=b1
while :; do
  S=$(curl -sS http://localhost:7891/backtest/$ID)
  ST=$(printf '%s' "$S" | python -c 'import sys,json; d=json.load(sys.stdin); print(d["state"], d.get("seconds"), d.get("error"))')
  echo "$(date +%H:%M:%S) $ST"
  case "$ST" in queued*|running*) sleep 2;; *) printf '%s' "$S" | python -m json.tool | head -60; break;; esac
done
```

Success check — the two identities nobody can fake:

```bash
curl -sS http://localhost:7891/backtest/b1 | python -c '
import sys,json
d=json.load(sys.stdin); s=d.get("summary") or {}; t=d.get("trades") or []
print("state       ", d["state"], "error:", d.get("error"))
print("period      ", d.get("instrument"), d.get("period"), d.get("from"), "->", d.get("to"))
print("tickReplay  ", d.get("tickReplay"), "seconds:", d.get("seconds"))
print("trades      ", s.get("trades"), "len(trades[]):", len(t))
print("w+l==trades ", (s.get("winners",0)+s.get("losers",0))==s.get("trades"))
print("gp+gl==net  ", round(s.get("grossProfit",0)+s.get("grossLoss",0),2)==round(s.get("netProfit",0),2))
if t: print("first/last  ", t[0]["entryTime"], t[-1]["exitTime"], t[0]["entryPrice"], "bars:", t[0].get("bars"))
'
```

Expected (`GUESS` on the numbers, not measured): `state "done"`, `error null`, run under ~10 s,
`trades` roughly 30–90 (always-in-market, reverses on each SMA(10)/SMA(25) cross over ~1,400–1,800
5-minute bars), `winners+losers == trades`, `len(trades) == summary.trades`, first `entryTime` on
2026-09-10 and last `exitTime` on 2026-09-17, `entryPrice` in the 7,500–7,800 band, `pnlPoints * 50 == pnl`,
`commission 0.0`. **Net P/L is noise — it is not a pass criterion.**
`summary.trades == 0` here means no bars loaded, **not** a quiet strategy.

Also check `trades[0].bars`: if every trade reports `0`, `Execution.BarIndex` is not populated for
backtest executions (§7) and `bars` needs the time-based fallback.

### 6.5 Plumbing check — tick replay on a custom bar type

```bash
curl -sS -X POST http://localhost:7891/backtest \
  -H 'Content-Type: application/json' \
  -d '{"strategy":"MyStrategy","chart":"first","from":"2026-09-16","to":"2026-09-17","tickReplay":true,"account":"Backtest","inputs":{"StopTicks":12,"TargetTicks":12}}'
```

**`"chart":"first"` is required here.** `API.md`'s `barsPeriod` defines only `type`, `value`, `value2`,
`baseType` — there is **no field for a custom bar type's Open Offset**, which lives in
`BarsPeriod.BaseBarsPeriodValue`. Either add a `baseValue` field to the contract, or clone the chart's
period wholesale. Also an add-on bar type's int id (e.g. `2018`) is not a named `BarsPeriodType` member,
so `Enum.Parse(typeof(BarsPeriodType), "<custom name>")` **will throw** — the bridge must accept the
integer. Fallback shape if `chart:"first"` is unusable, after adding `baseValue`:
`"barsPeriod":{"type":2018,"value":2,"value2":4,"baseType":"Minute","baseValue":2}`.

This check proves only: `state` reaches `done`, `tickReplay` really was set, the custom bar type built,
and the run did not deadlock or blow the cap. Treat zero trades as a pass of the **endpoint**; judge the
strategy separately.

**Historical tape trap.** A helper that files each tick with `OnPrint(e, Bars.Count - 1)` works live but
is wrong in a historical run: `Bars.Count - 1` is the *last* bar of the loaded series, so **every tick of
the whole backtest lands in one bar slot**. Per-bar flow then reads null on every bar and any signal
gated on it short-circuits, producing zero trades with no error. Index by `CurrentBar` instead.

**Defaults that guarantee zero trades**, independent of any tape bug:
- a narrow session filter (e.g. 09:30–15:55) discards roughly 85 % of the Globex day;
- a high minimum-volume or minimum-delta floor on a small brick;
- a stacked-imbalance requirement of 3 is geometrically impossible on a 2-tick brick;
- per-day counters (max trades per day, daily loss stop) never reset if the custom bar type does not
  flag `IsFirstBarOfSession`.

**Type trap:** inputs declared `long` (typically with `[Range(0,int.MaxValue)]`) are the common case that
breaks a naive binder. `Coerce()` handles it via `Convert.ChangeType`; a binder that assumes `int` throws.

Expected cost (`GUESS`): ~400–800k Last ticks/day plus Bid+Ask replay, 2 days → 1–5 minutes, several
thousand custom bars/day. If it returns `"timeout"`, shrink to a single day before blaming the code.
Tick replay needs **real Bid and Ask** tick files on disk, not just Last — a complete day is about
24 Ask / 24 Bid / 23 Last hourly files. Check both stores before the run:

```bash
ls "$NT8/db/minute/<instrument>/" | sed 's/\..*//' | sort -u
ls "$NT8/db/tick/<instrument>/"   | awk -F. '{print substr($1,1,8), $2}' | sort | uniq -c
```

### 6.6 After any failure — read three logs, newest first

`$NT8` = `%USERPROFILE%\Documents\NinjaTrader 8`.

```bash
# 1. the bridge itself — did the request arrive, what status, any 500 stack
grep -nE 'backtest|500 |Exception' "$NT8/NT8Bridge.log" | tail -40

# 2. NT8 application log, newest English file, errors/warnings only (field 2 == 3)
LOG=$(ls -t "$NT8/log/"*.en.txt | head -1); echo "$LOG"
grep -nE '\|3\||Strategy |Backtest|Tick Replay|historical data|NT8Bridge' "$LOG" | tail -40

# 3. NT8 trace, newest file, full .NET exceptions with stacks
TRC=$(ls -t "$NT8/trace/"*.txt | head -1); echo "$TRC"
grep -n -A8 -iE 'exception|NT8Bridge|RunBacktest|<your strategy>' "$TRC" | tail -80

# 4. anything the strategy Print()ed
curl -sS 'http://localhost:7891/output?n=200' | python -m json.tool | tail -40
```

A request that never appears in `NT8Bridge.log` never reached the AddOn (wrong port/host, listener dead).

### 6.7 Cleanup

`DELETE /backtest/{id}` terminates a stuck run. Never point a backtest at any account other than
`Backtest`. Everything above is read-only against NT8 except the POST itself; none of it places a live order.

### 6.8 Consolidated failure-signature table

| Symptom | Cause | Action |
|---|---|---|
| 404 `no route for POST /backtest` | an older AddOn build is still in memory | recopy + F5; check `/health` |
| Connection refused, nothing new in `NT8Bridge.log` | listener never started | `NT8Bridge.cs:126-130` logs the port/ACL reason |
| 500 `no chart 'first' — call /charts first` | chart window opened **before** the AddOn loaded, so it has no id | reopen the chart, or pass explicit instrument+barsPeriod |
| 400 `unknown input 'X'` | reflection walk missed a `[NinjaScriptProperty]` declared on a **base class**, not on the strategy | walk the full hierarchy |
| `ArgumentException: Requested value '<bar type>' was not found` | `Enum.Parse` on a bar-type **name**; an add-on type id such as 2018 is unnamed | accept the int, or clone from the chart |
| `period` comes back as the base period only (e.g. `"2 Minute"`) instead of the full custom period | the `BarsPeriod` clone dropped `Value2`/`BaseBarsPeriodValue` | result is meaningless; check `period` before reading anything else |
| NRE from inside `RunBacktest()`, obfuscated frames | bars never installed | go A1 → A2 |
| NRE on `s.Instrument = ...` / `s.BarsPeriod = ...` | facade over a null backing array | assign `Instruments` / `BarsPeriods` first |
| `IndexOutOfRangeException` on the same assignments | `BarsInProgress != 0`, or a zero-length array | multi-series setup ran out of order |
| `ArgumentNullException`/NRE inside `SetState(Configure)` | one of `BarsPeriods[0]`, `Instruments[0]`, `IsTickReplays[0].HasValue`, `TradingHoursArray[0]` unset | set all four **before** Configure |
| NRE with a stack in **NinjaTrader.Gui** (`StrategyRenderBase.SetState`) | render layer touched a null chart | `IsInStrategyAnalyzer = true` before Configure |
| `SetState(DataLoaded)` leaves `State == Finalized` | engine refused the hand-driven transition; object is dead | fresh instance; go to Recipe B |
| property read-back ≠ what you wrote | a stripped setter silently rejected it because `State` was wrong | write it while `State == SetDefaults` or `Configure` |
| `SystemPerformance` null after the call | run never reached `State.Historical`, or `RunBacktest` is **not** synchronous | do not report `done`; poll + timeout |
| `AllTrades.Count == 0` but `TradesCount > 0` | `includeTradeHistoryInBacktest` was false | summary works, per-trade list does not |
| `AllTrades` empty while `Orders` populated | `IncludeTradeHistoryInBacktest` left false | set it and read it back |
| the `SampleMACrossOver` smoke test returns 0 trades | no bars loaded — wrong instrument string, a window with no data, or an empty-session trading-hours template | **not** expected; investigate |
| the tick-replay plumbing check returns 0 trades | usually the strategy, not the bridge (see the historical tape trap in §6.5) | pass of the endpoint, if the smoke test traded |
| `tickReplay:false` in the status doc, or no `"Backtesting with Tick Replay was not designed..."` log line | `IsTickReplay` never took; `OnMarketData` never fired | read back `Bars.IsTickReplay` on the loaded series |
| `Tick replay on '{0}' only works with market data type 'Last'` | `BarsPeriod.MarketDataType` left at `Ask` (enum zero) | set `MarketDataType.Last` |
| `Order Fill Resolution is not available when Tick Replay is enabled.` | both set | leave `OrderFillResolution = Standard` |
| `IndexOutOfRangeException` at `Strategies[0]` | `Optimizer.Strategies` never populated | the expected Recipe B failure → go to C |
| `RunOptimization` callback never fires | `WaitForIterationsCompleted` blocked on workers that could not start | go to C |
| `CanExecute=false` on the SA RunCommand | tab missing strategy/instrument/dates, or already running | do **not** Execute anyway |
| SA config echoes fine but the run uses old settings | wrote `"Strategy"` mid-loop → detached template | write `Strategy` first, re-resolve per key |
| poll never fires although the run finished | compared `tab.Results.Count` | compare `SystemPerformance` **by reference** |
| NT8 UI freezes right after the run starts | called `StrategyRunner.RunStrategyAsync` directly, or ran on a dispatcher | documented deadlock+crash |
| NT internal `Debug.Assert` dialogs, pipeline stops | touched `Account.All`/`Connection`/NinjaScript from a worker thread in the Recipe C path | marshal to `saWin.Dispatcher` |
| `/charts` returns a document of nulls/zeros during a run | `Ui()` hit its 5 s `UiTimeout` and returned `default(T)` **silently** | the run is on a chart dispatcher |
| `POST /backtest -> 202 900000ms` in the log | the run executed inline instead of enqueueing | 202 must log in single-digit ms + at most the 5 s snapshot |
| closing the Control Center hangs NT8 | `jobThread.Join` unbounded, or inside `lock (gate)` | bound to 3000, outside the lock |
| `{"error":"trailing JSON at N"}` | two documents concatenated, or a truncated read | check `Content-Length` handling at `:171-174` |
| `Object of type System.Int32 cannot be converted to System.Int64` | boxed `int` against a strategy's `long` inputs | use `Coerce()` |
| `ValidationException` applying inputs | `[Range(0,int.MaxValue)]` on `long` props | 500 on POST before any bar loads |
| memory climbs run after run | never reached `Finalized`, or `DbRemove()` skipped | pinned in `cacheById`/`cacheList` |
| new rows in the Strategies grid / `StrategyBase.All` grows | a run persisted a `Category.Backtest` row | `DbRemove()` / `DbRemoveByCategory` |
| CS0117/CS1061 on `RunBacktest` at compile time | obfuscated signature differs from the decompile | **the whole NinjaTrader.Custom assembly fails, taking every indicator down** — compile-check before F5 |

---

## 7. Open questions

Each with the concrete experiment that settles it. The first three decide which recipe survives; run
them first.

**Open: is `RunBacktest()` synchronous, and does it load its own bars?**
*The whole design hangs on this.* Evidence both ways: it takes no `IProgress` while every bar loader in
Core does (suggests it is not the loader); but `StrategyRunner.RunBacktest(strategy, includeTradeHistory,
progress, entry)` takes no bars either and the GUI's async boundary is one level above it.
**Experiment:** the §6.4 smoke test with Recipe A1, logging `DateTime.Now`, `s.State` and `s.SystemPerformance == null`
immediately before and after the call. Synchronous + self-loading → elapsed seconds > 1 and
`State == Historical` with a non-null `SystemPerformance`. Returns in < 100 ms with `State == SetDefaults`
→ it did nothing; try A2. Returns fast but `SystemPerformance` fills in a second later → it is async and
the design needs a completion signal (there is none; fall to B or C).

**Open: does the default constructor run `SetState(State.SetDefaults)`?**
Strong circumstantial evidence yes: the framework's own error text blames "the `OnStateChange`
implementation for State=SetDefaults" on *instance creation* (`Resource.resx:1270`), and NT's
`new EMA(){ Period = period }` / `new MAX { Period = ... }; SetState(Configure);` patterns would be
broken if SetDefaults ran later and overwrote them.
**Experiment:** one line — `Log("fresh state = " + s.State);` right after `CreateInstance`. If it is
`SetDefaults`, do not call `SetState(SetDefaults)` again. If it is `Undefined`, the belt-and-braces call
in §2.1 step 4 is doing real work.

**Open: does the `"Backtest"` account exist in `Account.All`?**
Its creation site is invisible (all `Account`/`Connection` bodies stripped); it may be created lazily the
first time the Strategy Analyzer runs. Also unknown whether a `Connection` must be up.
**Experiment:** `GET /account?name=Backtest`, or log
`string.Join(",", Account.All.Select(a => a.Name))` at the top of `BacktestStart`. If absent, open the
Strategy Analyzer once and re-check — that tells you whether it is lazily created, and whether the whole
headless path has a hidden GUI precondition.

**Open: which stripped setters silently reject values, and in which state?**
Candidates: `IncludeTradeHistoryInBacktest`, `IsTickReplay`, `BacktestCommissionTemplate`,
`IncludeCommission`, `TradingHoursInstance`, `IsUnmanaged`, `BarsRequiredToTrade`, `DefaultQuantity`,
`EntriesPerDirection`, `Optimizer`, `OptimizationFitness`.
**Experiment:** write, then immediately read back, each one, and log `name=wrote/read/State`. §2.1 step 8
already hard-fails on the three that matter. Run the full sweep once and record the table in `NOTES.md`.

**Open: does `IsTickReplay` on the strategy actually reach the loaded series?**
Both accessors are stripped (the getter stub returns `false`), and `NinjaScriptBase.IsTickReplays[]` has
zero non-declaration references in the decompile.
**Experiment:** after `State.DataLoaded`, log `s.BarsArray[0].IsTickReplay` (read-only, from
`BarsSeries`). That is ground truth. Also watch for the informational log line `"Backtesting with Tick
Replay was not designed to provide higher accuracy..."` (`Resource.resx:1565`) — **its absence means tick
replay was never switched on.** Cross-check with a `Print` counter in `OnMarketData`.

**Open: is `OnMarketData` delivered during a headless backtest at `Calculate.OnBarClose`?**
Relevant to any strategy that sets `Calculate = Calculate.OnBarClose`. No Core-side gate tying `OnMarketData` delivery to
`Calculate` mode was found, and NT's documented behaviour is that Tick Replay drives `OnMarketData`
independently — but `NinjaScriptBase.Process(MarketDataEventArgs)` (:1741) is stripped.
**Experiment:** the §6.5 plumbing check with a tick counter. Zero ticks → flip `Calculate` to `OnEachTick` and re-run.

**Open: is `Execution.BarIndex` populated for backtest executions?**
It is the only candidate for the per-trade `bars` field (`Trade` has no `BarsInTrade`).
**Experiment:** the smoke test, then check `trades[0].bars` and log
`t.Entry.BarIndex, t.Exit.BarIndex, t.Entry.BarsInProgress` for the first five trades. All zeros → fall
back to counting primary bars between `Entry.Time` and `Exit.Time`, or emit `null`. Also confirms whether
it is the primary-series index or the `BarsInProgress` series index.

**Open: the signs of `maxDrawdown`, `largestLoser`, `avgLoser`, `avgMae`.**
`GrossLoss` is provably negative (`NetProfit = GrossLoss + GrossProfit`). The rest is inferred from
`@MinDrawDown.cs:9` returning `Percent.Drawdown` as a *maximised* fitness value.
**Experiment:** a run with a losing trade in the set; compare the JSON against the Strategy Analyzer's
own grid for the same config. Fix the doc, not the data.

**Open: `MaePoints` vs `MaeCurrency` for the per-trade `mae`/`mfe`.**
`API.md`'s example (`pnlPoints 2.0, mae 0.5, mfe 2.25`) reads as points; the prose says "Currency figures".
**Experiment:** emit both for the first run (`mae`, `maeCurrency`) and compare against the SA Trades grid;
keep one and delete the other. Related: does collection-level `Currency.AverageMae` multiply by quantity
(docs say collection-level Currency is "per trade", `Trade`'s doc-comment says "for one traded unit")?
Answerable with a single quantity-2 trade.

**Open: does a headless run leak a strategy-DB row?**
`DbAdd()` is internal and could be called from inside `RunBacktest`.
**Experiment:** log `StrategyBase.All.Count` before and after three consecutive runs, and look at the NT8
Strategies grid. If it grows, keep the `DbRemove()` in the `finally`; if not, drop it.

**Open: does the worker thread need STA / a non-null `Dispatcher`?**
NT's optimizer worker record carries a `Dispatcher`; `NinjaScriptBase.Dispatcher` is `null` on an
`Activator`-created instance and its setter is internal.
**Experiment:** run the smoke test on a plain MTA background thread. `"The calling thread must be STA"` or a
Dispatcher error → set `ApartmentState.STA`; still failing → reflect
`Globals.RandomDispatcher` into `NinjaScriptBase.Dispatcher` (community-tested reflection path) and retry.

**Open: is `SetState(State.Terminated)` safe from a foreign thread?**
Needed for both `DELETE` and the 15-minute cap. No lock or thread-affinity assertion is visible.
**Experiment:** start a minutes-long run, `DELETE` it at ~30 s, and watch the trace for a
cross-thread exception and the worker for a clean unwind. If unsafe, the cap degrades as described in
§4.3 and that must be written into `NOTES.md`, not hidden.

**Open: does `Code.Output.OutputEvent` fire for a strategy with no window?**
**Experiment:** a run with `s.PrintTo = PrintTo.OutputTab2` and a `Print("bt-probe " + CurrentBar)` in a
throwaway test strategy. Empty `output[]` while the Output window shows the lines → you subscribed too
late or filtered on the wrong tab. Empty in **both** → the feed is suppressed during backtests; `[]`
stands, per `API.md:141`.

**Open: is `BarsPeriod.Clone()` a deep copy?**
`ICloneable` with a stripped body. A shallow clone means a chart-seeded custom period still
shares state with the live chart.
**Experiment:** clone the chart's period, mutate `Value2` on the clone, and re-read the chart's
`BarsPeriod.Value2` through `/charts`. If it changed, copy field by field on the chart thread instead.

**Open: does a custom `Optimizer` subclass compiled inside `NT8Bridge.cs` bind?**
Only relevant to Recipe B. The `StrategyBase.Optimizer` setter is stripped, has
`[RefreshProperties(All)]` and a string-keyed `PropertyEditor`, so it may accept only optimizer types
registered in the NinjaScript type cache. AddOn code lives in `NinjaTrader.Custom.dll`, which should be
fine.
**Experiment:** `s.Optimizer = new NT8BridgeOneShotOptimizer(); Log("optimizer = " + (s.Optimizer == null ? "REJECTED" : s.Optimizer.GetType().Name));`
