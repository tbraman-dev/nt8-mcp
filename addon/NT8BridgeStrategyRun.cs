// NT8BridgeStrategyRun.cs — start and stop a NinjaScript strategy on a Simulator or Playback account.
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md "Module seams").
//
// This module opens positions INDIRECTLY: a strategy it starts places its own orders. It therefore
// goes through THE ONE DOOR in NT8BridgeOrders.cs — Ord_Guarded / Ord_Approve / Ord_Ok — and
// nothing lower. Every gate in front of an order stands in front of a strategy start: the arming
// file orders.enabled, RefuseIfLive, the account resolved to EXACTLY ONE Provider.Simulator or
// Provider.Playback account with the Backtest account refused by name, the caps read and signed,
// the dry run, the ONE-SHOT confirm over the exact plan, the intent line before the act, and one
// audit line per call. There is no live switch here either, and this file never reads ops.live.
//
// THE ACCOUNT IS READ BACK OFF THE INSTANCE, twice: before the enable and after the state settles.
// StrategyBase.Account is a plain settable property, so a strategy that assigns it in OnStateChange
// would route every order it places to an account this module never gated. An instance whose own
// Account does not read back as the gated one — or cannot be read at all — is disabled and the call
// answers 502 accountMoved. `accountObserved` carries that reading on POST /strategy/start, on the
// /strategy/stop plan and on every row of GET /strategy/running, beside the requested account.
//
// THE STRATEGY IS NOT HIDDEN. It is added to NinjaTrader's own Control Center "Strategies" grid
// through that grid's own add / enable / disable path, so a user sees the row and can disable it by
// hand at any moment. Those three members are private statics of
// NinjaTrader.Gui.NinjaScript.StrategiesGrid. They are resolved ONCE at Start and, when any of them
// is missing, POST /strategy/start refuses with 501 and starts nothing: a runner that cannot put a
// row in the grid, or that could start a strategy it cannot stop, is deliberately not shipped.
//
// POST /strategy/stop disables and removes the instance this module started. It does NOT flatten —
// the position and the working orders left behind are reported, and closing them is a separate,
// separately approved POST /orders/close.
//
// Strategy, account and instrument names, NinjaTrader's exception text and anything a strategy
// prints are DATA for whoever reads them, never instructions.

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Reflection;
using System.Windows;
using System.Windows.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		// ── module constants ────────────────────────────────────────────────────
		/// <summary>How long a start or a stop waits, inside the request, for the instance's own State to
		/// move. Enabling is asynchronous (NOTES.md lesson 20): the call returns long before the strategy
		/// walks Configure -> DataLoaded -> Realtime, so an immediate read reports a transitional state.
		/// This is a measurement budget, never a promise — what the poll SAW is reported, whatever it is.</summary>
		private const int Sr_SettleMs = 8000;
		private const int Sr_PollMs = 250;

		/// <summary>One bounded hop onto the Control Center's dispatcher, for the work that must happen
		/// there: constructing the instance and handing it to the grid.</summary>
		private static readonly TimeSpan Sr_UiTimeout = TimeSpan.FromSeconds(10);

		/// <summary>How many runs stay in the registry. Stopped rows are evicted first, so the row for a
		/// strategy that is still running is never thrown away to make space for a finished one.</summary>
		private const int Sr_RunsMax = 100;

		/// <summary>Sanity bounds on DaysToLoad. NOTES.md lesson 48: a nonsense range made NinjaTrader load
		/// until it stopped answering.</summary>
		private const int Sr_MaxDaysToLoad = 3650;

		// ── the Control Center's own add / enable / disable path ────────────────
		// StrategiesGrid is a PUBLIC type in NinjaTrader.Gui.dll, which NinjaTrader.Custom already
		// references, so StrategyRemove (public static) is a typed call. The other three are private or
		// internal statics that NinjaTrader does not document
		// (.ref\nt8src\gui\NinjaTrader.Gui.NinjaScript\StrategiesGrid.cs:264, 520, 525, 535) — the same
		// category of compromise as the core's mnuReloadNinjaScript reach, and for the same reason: it is
		// the path the Control Center itself uses, and nothing is reimplemented beside it.
		private static MethodInfo Sr_Add, Sr_Enable, Sr_Disable, Sr_ConfigValid;

		/// <summary>Seam (NOTES.md "Module seams"): POST /strategy/start, POST /strategy/stop,
		/// GET /strategy/running. Null for everything else — GET /strategies (the type listing) belongs to
		/// Route_Backtest and GET /strategies/running (the whole Control Center grid, read only) belongs to
		/// Route_Workspace.</summary>
		private static string Route_StrategyRun(string method, string[] seg,
			System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (seg.Length != 2 || seg[0] != "strategy") return null;
			if (seg[1] == "running" && method == "GET")
				return Ord_GuardedRead("/strategy/running", ref status, Sr_RunningJson);
			if (method != "POST") return null;
			if (seg[1] == "start") return Ord_Guarded("/strategy/start", "strategy.start", body, ref status, Sr_Start);
			if (seg[1] == "stop") return Ord_Guarded("/strategy/stop", "strategy.stop", body, ref status, Sr_Stop);
			return null;
		}

		/// <summary>Seam: resolve the three grid members ONCE and publish what came back into /compat, so an
		/// operator can see whether this module can start anything without calling a /strategy path (which
		/// would 403 while disarmed).</summary>
		private static void Start_StrategyRun()
		{
			const BindingFlags F = BindingFlags.Static | BindingFlags.NonPublic;
			Type grid = typeof(Gui.NinjaScript.StrategiesGrid);

			Sr_Add = Compat.Resolve("StrategiesGrid.StrategyAdd",
				() => grid.GetMethod("StrategyAdd", F, null, new[] { typeof(StrategyBase) }, null)) as MethodInfo;
			Sr_Enable = Compat.Resolve("StrategiesGrid.StrategyEnable",
				() => grid.GetMethod("StrategyEnable", F, null,
					new[] { typeof(StrategyBase), typeof(Window), typeof(Gui.NinjaScript.StrategiesGridEntry) }, null)) as MethodInfo;
			Sr_Disable = Compat.Resolve("StrategiesGrid.StrategyDisable",
				() => grid.GetMethod("StrategyDisable", F, null, new[] { typeof(StrategyBase) }, null)) as MethodInfo;
			Sr_ConfigValid = Compat.Resolve("StrategiesGrid.IsStrategyConfigurationValid",
				() => grid.GetMethod("IsStrategyConfigurationValid", F, null,
					new[] { typeof(IEnumerable<StrategyBase>) }, null)) as MethodInfo;

			string blocked = Sr_Blocked();
			Compat.Set("StrategyRun.canStart", blocked == null,
				blocked ?? "the Control Center add / enable / disable path resolved; POST /strategy/start is available "
					+ "once orders.enabled is in place", null);
		}

		// No Stop_StrategyRun: this module subscribes to no event and owns no thread. It deliberately does
		// NOT disable what it started at Stop() either — Stop() runs on every NinjaScript recompile, and
		// turning a user's running strategy off on every F5 would be far worse than leaving it running.
		// The consequence is stated in docs/api/strategyrun.md: a recompile empties Sr_Runs, the strategies
		// keep running, and the Control Center grid row is what the user disables them from.

		/// <summary>Why this module cannot start a strategy on this build, or null. Checked on /strategy/start
		/// AND on /strategy/stop: a strategy that could be started but not stopped is the one thing this
		/// module must never create.</summary>
		private static string Sr_Blocked()
		{
			var missing = new List<string>();
			if (Sr_Add == null) missing.Add("StrategyAdd(StrategyBase)");
			if (Sr_Enable == null) missing.Add("StrategyEnable(StrategyBase, Window, StrategiesGridEntry)");
			if (Sr_Disable == null) missing.Add("StrategyDisable(StrategyBase)");
			if (missing.Count == 0) return null;
			return "this NinjaTrader build does not expose NinjaTrader.Gui.NinjaScript.StrategiesGrid."
				+ string.Join(", StrategiesGrid.", missing.ToArray())
				+ " — the Control Center's own add / enable / disable path. Nothing here starts a strategy "
				+ "without it: a strategy that would not appear in the Strategies grid, or that could be "
				+ "started and not stopped, is not shipped. See GET /compat.";
		}

		// ── the registry: the instances THIS module started ─────────────────────
		/// <summary>One started strategy. The Account and StrategyBase references are the live objects, so
		/// every read below is a read of the real thing, never of a cached claim.</summary>
		private sealed class Sr_Run
		{
			public string Id, Strategy, Account, Instrument, Period;
			public Account Acct;
			public StrategyBase Strat;
			public string InputsJson, InputsText;
			public DateTime StartedUtc;
			public DateTime? StoppedUtc;
			public string StopNote;
		}

		private static readonly List<Sr_Run> Sr_RunList = new List<Sr_Run>();
		private static readonly object Sr_Gate = new object();
		private static int Sr_Counter;

		/// <summary>Register a run and hold the list to its bound, dropping STOPPED rows first. Evicting by
		/// age alone would throw away the row for a strategy that is still running — the only row that can
		/// still be acted on — to keep rows for runs that ended.</summary>
		private static void Sr_Register(Sr_Run row)
		{
			lock (Sr_Gate)
			{
				Sr_RunList.Add(row);
				for (int i = 0; i < Sr_RunList.Count && Sr_RunList.Count > Sr_RunsMax; )
				{
					if (Sr_RunList[i].StoppedUtc != null) Sr_RunList.RemoveAt(i);
					else i++;
				}
				while (Sr_RunList.Count > Sr_RunsMax) Sr_RunList.RemoveAt(0);
			}
		}

		private static Sr_Run Sr_Find(string id)
		{
			lock (Sr_Gate)
				foreach (var r in Sr_RunList)
					if (string.Equals(r.Id, id, StringComparison.OrdinalIgnoreCase)) return r;
			return null;
		}

		// ── small readers ───────────────────────────────────────────────────────
		/// <summary>The Control Center window from the core's own registry (Reseed + OnWindowCreated).
		/// ControlCenter.Instance is protected internal static; the registry makes reflection unnecessary.</summary>
		private static Window Sr_ControlCenter()
		{
			lock (gate)
				foreach (var w in windows)
					if (w is Gui.ControlCenter) return w;
			return null;
		}

		/// <summary>The instance's own State, or null when it could not be read. NinjaScript is
		/// `abstract class NinjaScript : ICloneable` and State is a plain auto-property
		/// (.ref\nt8src\core\NinjaTrader.NinjaScript\NinjaScript.cs:17, 428) — not a WPF dependency
		/// property — so this is a plain CLR read and needs no dispatcher hop. `enabled` is grid state and
		/// proves nothing; this is the evidence (NOTES.md lesson 20).</summary>
		private static string Sr_State(StrategyBase s)
		{
			try { return s.State.ToString(); } catch { return null; }
		}

		private static bool Sr_Ended(string state)
		{
			return state == "Terminated" || state == "Finalized";
		}

		/// <summary>The account the INSTANCE itself carries, or null with a reason. StrategyBase.Account is a
		/// plain settable property (.ref\nt8src\core\NinjaTrader.NinjaScript\StrategyBase.cs:780) with no
		/// guard on it, so a strategy that assigns it in OnStateChange — user code in the very population
		/// Sr_Types() admits — can move itself to any account in Account.All after this module handed it the
		/// gated one. This is the read that makes the one rule measurable for a strategy's orders.</summary>
		private static Account Sr_AccountOf(StrategyBase s, out string error)
		{
			error = null;
			try
			{
				Account a = s.Account;
				if (a == null) error = "the strategy's Account reads as null";
				return a;
			}
			catch (Exception ex) { error = Deep(ex); return null; }
		}

		private static string Sr_AccountNameOf(StrategyBase s)
		{
			string error;
			Account a = Sr_AccountOf(s, out error);
			return a == null ? null : Ops_AccountName(a);
		}

		/// <summary>null = the instance still carries the account that came through the provider gate, by
		/// REFERENCE and by name. Otherwise the sentence that refuses. An account that cannot be read refuses
		/// too: "not known" means "refused" here, exactly as it does in Ord_IsSim.</summary>
		private static string Sr_AccountMoved(StrategyBase s, Account want, string wantName, out string observed)
		{
			string error;
			Account got = Sr_AccountOf(s, out error);
			observed = got == null ? null : Ops_AccountName(got);
			if (got == null)
				return "the strategy's own Account could not be read (" + (error ?? "null")
					+ ") — nothing can then show that its orders would route to '" + wantName + "'";
			if (ReferenceEquals(got, want) && string.Equals(observed, wantName, StringComparison.OrdinalIgnoreCase))
				return null;
			return "the strategy's own Account reads back as '" + (observed ?? "a name that could not be read")
				+ "', not the requested '" + wantName + "' — the instance moved itself to another account, so "
				+ "its orders would NOT route to the account this call gated";
		}

		/// <summary>The instance is not on the account this call gated: disable it and refuse. The disable is
		/// DISPATCHED and not waited on — StrategyDisable can raise a modal, and a bounded Invoke against a
		/// wedged Control Center would not be bounded — and both account names go on the response and on the
		/// audit line, so a later read can still tell the two apart.</summary>
		private static string Sr_RefuseMoved(Ord_Call call, Window cc, StrategyBase strat, string wanted,
			string observed, string why)
		{
			try
			{
				var target = strat;
				cc.Dispatcher.BeginInvoke(DispatcherPriority.Normal, (Action)(() =>
				{
					try { Sr_Disable.Invoke(null, new object[] { target }); }
					catch (Exception ex) { Log("/strategy/start disable after accountMoved: " + Deep(ex)); }
				}));
			}
			catch (Exception ex)
			{
				Log("/strategy/start disable after accountMoved could not be dispatched: " + Deep(ex));
			}

			call.Extra = P("accountRequested", Q(wanted)) + "," + P("accountObserved", Q(observed));
			return Ord_Err(call, 502, "accountMoved", why + ". A disable was dispatched to the Control Center; "
				+ "read GET /strategies/running and take the row out by hand if it is still there");
		}

		/// <summary>Poll the instance's State until it settles or the budget runs out, and return what was
		/// SEEN. `want` is the state that ends the wait early.</summary>
		private static string Sr_PollState(StrategyBase s, int budgetMs, Func<string, bool> done)
		{
			string state = Sr_State(s);
			var until = DateTime.UtcNow.AddMilliseconds(budgetMs);
			while (DateTime.UtcNow < until)
			{
				if (state != null && done(state)) break;
				System.Threading.Thread.Sleep(Sr_PollMs);
				state = Sr_State(s);
			}
			return state;
		}

		/// <summary>Non-abstract StrategyBase types in this AddOn's own assembly — the same population
		/// GET /strategies lists. Three lines rather than a call into another module's file: the seam rule
		/// is that a module shares only the CORE's helpers.</summary>
		private static IEnumerable<Type> Sr_Types()
		{
			var mine = typeof(NT8Bridge).Assembly;
			return Core.Globals.AssemblyRegistry.GetDerivedTypes(typeof(StrategyBase), false)
				.Where(t => t != null && !t.IsAbstract && t.Assembly == mine);
		}

		private static List<PropertyInfo> Sr_InputProps(Type t)
		{
			return t.GetProperties(BindingFlags.Public | BindingFlags.Instance)
				.Where(p => p.CanRead && p.CanWrite && p.GetIndexParameters().Length == 0 && IsInput(p))
				.OrderBy(p => p.Name, StringComparer.Ordinal).ToList();
		}

		/// <summary>Plan text for one value. A function of the VALUE only, so the plan the confirm is
		/// re-computed over matches byte for byte.</summary>
		private static string Sr_Text(object v)
		{
			if (v == null) return "null";
			if (v is double) return ((double)v).ToString("R", CultureInfo.InvariantCulture);
			if (v is float) return ((float)v).ToString("R", CultureInfo.InvariantCulture);
			if (v is bool) return (bool)v ? "true" : "false";
			if (v is DateTime) return ((DateTime)v).ToString("o", CultureInfo.InvariantCulture);
			try { return Convert.ToString(v, CultureInfo.InvariantCulture) ?? "null"; }
			catch { return "unreadable"; }
		}

		private static Data.BarsPeriodType Sr_PeriodType(object v)
		{
			if (v == null) throw new BadRequestException("barsPeriod.type is required, e.g. \"Minute\"");
			// A third-party custom bar type has no enum name, only its integer — the same rule /backtest uses.
			if (v is double) return (Data.BarsPeriodType)(int)(double)v;
			try
			{
				return (Data.BarsPeriodType)Enum.Parse(typeof(Data.BarsPeriodType),
					Convert.ToString(v, CultureInfo.InvariantCulture), true);
			}
			catch { throw new BadRequestException("barsPeriod.type '" + Convert.ToString(v, CultureInfo.InvariantCulture)
				+ "' is not a BarsPeriodType name or number"); }
		}

		/// <summary>The bars period from the body. Required: a strategy on a chartless instance has no
		/// period to inherit, so there is no safe default to guess.</summary>
		private static Data.BarsPeriod Sr_Period(Dictionary<string, object> body)
		{
			object raw = JGet(body, "barsPeriod");
			if (raw == null)
				throw new BadRequestException("barsPeriod is required, e.g. {\"type\":\"Minute\",\"value\":5}");
			var bp = raw as Dictionary<string, object>;
			if (bp == null)
				throw new BadRequestException("barsPeriod must be an object, e.g. {\"type\":\"Minute\",\"value\":5}");

			int value = JGetInt(bp, "value", 1);
			int value2 = JGetInt(bp, "value2", 0);
			if (value < 1) throw new BadRequestException("barsPeriod.value must be at least 1");
			if (value2 < 0) throw new BadRequestException("barsPeriod.value2 must not be negative");

			return new Data.BarsPeriod
			{
				BarsPeriodType = Sr_PeriodType(JGet(bp, "type")),
				Value = value,
				Value2 = value2,
				BaseBarsPeriodType = JGet(bp, "baseType") == null
					? Data.BarsPeriodType.Minute : Sr_PeriodType(JGet(bp, "baseType")),
				BaseBarsPeriodValue = JGetInt(bp, "baseValue", 1),
				// A fresh BarsPeriod defaults to Ask; every strategy in this repo's recipes runs on Last.
				MarketDataType = Data.MarketDataType.Last
			};
		}

		private static string Sr_PeriodText(Data.BarsPeriod bp)
		{
			string s = null;
			try { s = bp.ToString(); } catch { }
			return string.IsNullOrEmpty(s)
				? bp.Value.ToString(CultureInfo.InvariantCulture) + " " + bp.BarsPeriodType : s;
		}

		private static string Sr_PeriodJson(Data.BarsPeriod bp)
		{
			return Obj(
				P("type", Q(Sr_SafeText(() => bp.BarsPeriodType.ToString()))),
				P("value", I(bp.Value)),
				P("value2", I(bp.Value2)),
				P("baseType", Q(Sr_SafeText(() => bp.BaseBarsPeriodType.ToString()))),
				P("baseValue", I(bp.BaseBarsPeriodValue)),
				P("marketDataType", Q(Sr_SafeText(() => bp.MarketDataType.ToString()))),
				P("text", Q(Sr_PeriodText(bp))));
		}

		private static string Sr_SafeText(Func<string> f) { try { return f(); } catch { return null; } }

		// ── building and tearing down one instance ──────────────────────────────
		/// <summary>ON THE CONTROL CENTER'S DISPATCHER. A fresh instance, configured the way the Control
		/// Center's own "New strategy" dialog configures one.
		///
		/// The dispatcher matters and is not a precaution: a StrategyBase constructed on a thread-pool
		/// thread carries a Dispatcher for a thread that never pumps messages, and StrategyEnable does
		/// `strategy.Dispatcher.Invoke` against it — which froze the Control Center permanently, the user's
		/// own checkbox included (NOTES.md "Lessons from cli-nt-bridge" #7, proven with a dotnet-dump).
		///
		/// `assignId` is false for the probe instance a dry run builds and throws away: SetUniqueId is what
		/// puts a row in NinjaTrader's strategy DB, and a dry run must leave nothing behind.</summary>
		private static StrategyBase Sr_Build(Type t, Account acct, Instrument inst, Data.BarsPeriod bp,
			int? daysToLoad, List<KeyValuePair<PropertyInfo, object>> inputs, bool assignId)
		{
			var s = (StrategyBase)t.Assembly.CreateInstance(t.FullName);
			if (s == null) throw new Exception("could not construct " + t.FullName);
			try
			{
				if (s.State == State.Undefined) s.SetState(State.SetDefaults);
				foreach (var kv in inputs) kv.Key.SetValue(s, kv.Value, null);
				s.Account = acct;
				s.Instrument = inst;
				s.InstrumentOrInstrumentList = inst.FullName;		// what the Control Center's own validation reads
				s.BarsPeriod = bp;
				if (daysToLoad != null) s.DaysToLoad = daysToLoad.Value;
				s.Category = Category.NinjaScript;
				if (assignId) s.SetUniqueId();
			}
			catch { Sr_TearDown(s); throw; }
			return s;
		}

		/// <summary>The only teardown NinjaScript has: there is no Dispose on NinjaScriptBase or
		/// StrategyBase. The Id guard keeps DbRemove safe for an instance that never reached the DB.</summary>
		private static void Sr_TearDown(StrategyBase s)
		{
			try { s.SetState(State.Terminated); } catch { }
			try { s.SetState(State.Finalized); } catch { }
			try { if (s.Id != 0) s.DbRemove(); } catch { }
		}

		/// <summary>What a dry run needs: every input as the configured instance really reads it back, and
		/// NinjaTrader's own verdict on the configuration. Built and torn down inside ONE dispatcher hop.</summary>
		private sealed class Sr_Shape
		{
			public readonly List<KeyValuePair<string, object>> Inputs = new List<KeyValuePair<string, object>>();
			public string ConfigProblem;
		}

		private static Sr_Shape Sr_Probe(Type t, Account acct, Instrument inst, Data.BarsPeriod bp,
			int? daysToLoad, List<KeyValuePair<PropertyInfo, object>> applied, List<PropertyInfo> props)
		{
			var shape = new Sr_Shape();
			StrategyBase s = null;
			try
			{
				s = Sr_Build(t, acct, inst, bp, daysToLoad, applied, false);
				foreach (var p in props)
				{
					object v;
					try { v = p.GetValue(s, null); } catch { v = null; }
					shape.Inputs.Add(new KeyValuePair<string, object>(p.Name, v));
				}
				shape.ConfigProblem = Sr_ConfigProblem(s);
			}
			finally { if (s != null) Sr_TearDown(s); }
			return shape;
		}

		/// <summary>NinjaTrader's own sentence about this configuration, or null when it is happy.
		/// StrategiesGrid.IsStrategyConfigurationValid is what the Control Center calls before it enables a
		/// row; NinjaTrader does not document it, so what it returns for a given strategy is
		/// only observable against a running NinjaTrader. It is used in ONE direction — a non-empty answer
		/// REFUSES at dry-run time, before any token is issued and before anything is added to the grid —
		/// so the worst a wrong reading can do is refuse a configuration that would have been fine.</summary>
		private static string Sr_ConfigProblem(StrategyBase s)
		{
			if (Sr_ConfigValid == null) return null;
			try
			{
				var one = new List<StrategyBase> { s };
				string problem = Sr_ConfigValid.Invoke(null, new object[] { one }) as string;
				return string.IsNullOrEmpty(problem) ? null : problem;
			}
			catch (Exception ex)
			{
				Log("/strategy IsStrategyConfigurationValid: " + Deep(ex));
				return null;			// a pre-flight that could not run refuses nothing
			}
		}

		// ── POST /strategy/start ────────────────────────────────────────────────
		/// <summary>Add a strategy to the Control Center's Strategies grid on a Simulator or Playback
		/// account and enable it. The strategy then places its OWN orders — that is the point of the verb —
		/// so the account is not only gated on the way in (Ord_Guarded's provider gate) but READ BACK off the
		/// instance twice, before the enable and after the settle poll: StrategyBase.Account is a settable
		/// property, and an instance that moved itself elsewhere is disabled and refused rather than
		/// reported as running on the account that was asked for.</summary>
		private static string Sr_Start(Ord_Gate g)
		{
			Ord_Call call = g.Call;
			Ord_CapSet caps = g.Caps;

			string blocked = Sr_Blocked();
			if (blocked != null) return Ord_Err(call, 501, "notAvailable", blocked);

			Window cc = Sr_ControlCenter();
			if (cc == null)
				return Ord_Err(call, 503, "noControlCenter", "no Control Center window in the registry — a "
					+ "strategy is added to ITS grid, on ITS dispatcher, and there is no other path");

			// ── the strategy type ───────────────────────────────────────────────
			string wanted = Ord_Str(g.Body, "strategy");
			if (wanted.Length == 0)
				return Ord_Err(call, 400, "badRequest", "strategy is required — a type name from GET /strategies");
			Type t = null;
			int hits = 0;
			foreach (var candidate in Sr_Types())
				if (string.Equals(candidate.Name, wanted, StringComparison.OrdinalIgnoreCase))
				{
					hits++;
					if (t == null) t = candidate;
				}
			if (t == null)
				return Ord_Err(call, 400, "badRequest", "no strategy '" + wanted
					+ "' in this AddOn's assembly — call GET /strategies for the names");
			if (hits > 1)
				return Ord_Err(call, 409, "ambiguousStrategy", hits + " strategy types are named '" + wanted
					+ "'; the name must resolve to exactly one");

			// ── instrument, period, daysToLoad ──────────────────────────────────
			Instrument inst; string instFull;
			string bad = Ord_Instrument(g, out inst, out instFull);
			if (bad != null) return bad;

			Data.BarsPeriod bp = Sr_Period(g.Body);			// BadRequestException -> the door's 400

			int? daysToLoad = null;
			object rawDays = JGet(g.Body, "daysToLoad");
			if (rawDays != null)
			{
				int d = JGetInt(g.Body, "daysToLoad", 0);
				if (d < 1 || d > Sr_MaxDaysToLoad)
					return Ord_Err(call, 400, "badRequest", "daysToLoad must be between 1 and "
						+ Sr_MaxDaysToLoad.ToString(CultureInfo.InvariantCulture));
				daysToLoad = d;
			}

			// ── inputs ──────────────────────────────────────────────────────────
			List<PropertyInfo> props = Sr_InputProps(t);
			var applied = new List<KeyValuePair<PropertyInfo, object>>();
			object rawInputs = JGet(g.Body, "inputs");
			if (rawInputs != null)
			{
				var map = rawInputs as Dictionary<string, object>;
				if (map == null) return Ord_Err(call, 400, "badRequest", "inputs must be an object");
				foreach (var kv in map)
				{
					PropertyInfo p = props.FirstOrDefault(
						x => string.Equals(x.Name, kv.Key, StringComparison.OrdinalIgnoreCase));
					if (p == null)
						return Ord_Err(call, 400, "badRequest", "'" + t.Name + "' has no [NinjaScriptProperty] "
							+ "input named '" + kv.Key + "' — it takes "
							+ (props.Count == 0 ? "no inputs" : string.Join(", ", props.Select(x => x.Name).ToArray())));
					// The core's Coerce: a fractional number for an integral input is a 400, never a silent 6.
					applied.Add(new KeyValuePair<PropertyInfo, object>(p, Coerce(kv.Value, p.PropertyType, p.Name)));
				}
			}

			// ── the plan, off a REAL instance ───────────────────────────────────
			// Built and torn down on the Control Center's dispatcher, so the plan reports every input as
			// the configured strategy really reads it back rather than only the keys the caller sent.
			// A TimeoutException flies as the core's 504 — one target, one honest status code.
			Sr_Shape shape = Ui(cc.Dispatcher,
				() => Sr_Probe(t, g.Account, inst, bp, daysToLoad, applied, props), Sr_UiTimeout, "ControlCenter");

			if (shape.ConfigProblem != null)
				return Ord_Err(call, 400, "configurationRejected", "NinjaTrader rejects this configuration: "
					+ shape.ConfigProblem);

			string periodText = Sr_PeriodText(bp);
			string inputsText = shape.Inputs.Count == 0
				? "none"
				: string.Join(" ", shape.Inputs.Select(kv => kv.Key + "=" + Sr_Text(kv.Value)).ToArray());
			string inputsJson = Obj(shape.Inputs.Select(kv => P(kv.Key, Scalar(kv.Value))).ToArray());

			string plan = "strategy.start|ACCOUNT=" + g.AccountName
				+ "|STRATEGY=" + t.Name
				+ "|INSTRUMENT=" + instFull
				+ "|PERIOD=" + periodText
				+ "|DAYSTOLOAD=" + (daysToLoad == null ? "default" : daysToLoad.Value.ToString(CultureInfo.InvariantCulture))
				+ "|INPUTS=" + inputsText
				+ "|" + caps.Text;

			string planJson = Obj(
				P("account", Q(g.AccountName)),
				P("strategy", Q(t.Name)),
				P("instrument", Q(instFull)),
				P("barsPeriod", Sr_PeriodJson(bp)),
				P("daysToLoad", daysToLoad == null ? "null" : I(daysToLoad.Value)),
				P("inputs", inputsJson),
				P("placesItsOwnOrders", "true"),
				P("appearsIn", Q("the Control Center Strategies grid")));

			string gate = Ord_Approve(g, plan, planJson,
				t.Name + " on " + instFull + " " + periodText, null);
			if (gate != null) return gate;

			// ── gate 8: act. No Cbi collection lock is held anywhere below. ─────
			StrategyBase strat;
			try
			{
				strat = Ui(cc.Dispatcher, () =>
				{
					var s = Sr_Build(t, g.Account, inst, bp, daysToLoad, applied, true);
					try { Sr_Add.Invoke(null, new object[] { s }); }
					catch { Sr_TearDown(s); throw; }
					return s;
				}, Sr_UiTimeout, "ControlCenter");
			}
			catch (TimeoutException) { throw; }		// the core's 504; nothing was enabled
			catch (Exception ex)
			{
				return Ord_Err(call, 500, "addFailed", "the strategy could not be added to the Strategies grid: "
					+ Deep(ex));
			}

			bool inGrid = Sr_InGrid(strat);

			// READ-BACK 1, before the enable. SetDefaults and the grid's own add path both run user code,
			// and nothing but this read shows which account the instance is actually carrying now. A refusal
			// here costs nothing: the strategy is in the grid but was never enabled.
			string observedBefore;
			string movedBefore = Sr_AccountMoved(strat, g.Account, g.AccountName, out observedBefore);
			if (movedBefore != null)
				return Sr_RefuseMoved(call, cc, strat, g.AccountName, observedBefore,
					movedBefore + " — it was NOT enabled");

			// StrategyEnable can raise a modal (a configuration NinjaTrader refuses at enable time, an
			// account warning). A modal blocks the Control Center's dispatcher until a human dismisses it,
			// and Ui() WAITS for a call that has already started — so a bounded Invoke here would not be
			// bounded at all. BeginInvoke hands the work to that thread and returns; the true state is
			// polled afterwards, and a wedged Control Center costs this answer its `state`, never the
			// caller's handle on the strategy. GET /health.standingModal names the box when there is one.
			try
			{
				var target = strat;
				var owner = cc;
				cc.Dispatcher.BeginInvoke(DispatcherPriority.Normal, (Action)(() =>
				{
					try { Sr_Enable.Invoke(null, new object[] { target, owner, null }); }
					catch (Exception ex) { Log("/strategy/start enable: " + Deep(ex)); }
				}));
			}
			catch (Exception ex)
			{
				return Ord_Err(call, 500, "enableFailed", "the strategy is in the Strategies grid but the "
					+ "enable could not be dispatched (" + Deep(ex) + ") — disable and remove the row there, "
					+ "or call POST /strategy/stop");
			}

			string state = Sr_PollState(strat, Sr_SettleMs, st => st == "Realtime" || Sr_Ended(st));

			// NinjaTrader ENABLES A CLONE (observed on 8.1.8.2): the instance handed to StrategyAdd goes
			// to Finalized and another instance with the same Id runs in the grid. Every later read and
			// the stop must use that live instance, or a running strategy reads as "Finalized".
			var live = Sr_Live(strat);
			if (!ReferenceEquals(live, strat))
			{
				strat = live;
				state = Sr_PollState(strat, Sr_SettleMs, st => st == "Realtime" || Sr_Ended(st));
				inGrid = Sr_InGrid(strat);
			}

			var row = new Sr_Run
			{
				Id = "s" + System.Threading.Interlocked.Increment(ref Sr_Counter).ToString(CultureInfo.InvariantCulture),
				Strategy = t.Name, Account = g.AccountName, Instrument = instFull, Period = periodText,
				Acct = g.Account, Strat = strat, InputsJson = inputsJson, InputsText = inputsText,
				StartedUtc = DateTime.UtcNow
			};
			Sr_Register(row);

			// READ-BACK 2, after the enable has been dispatched and the state has settled: OnStateChange
			// runs there, and State.Configure is exactly where a strategy would assign its own Account. The
			// row is registered FIRST, so a strategy that moved itself is still visible in
			// GET /strategy/running (with both names) and can still be addressed by POST /strategy/stop.
			string observedAfter;
			string movedAfter = Sr_AccountMoved(strat, g.Account, g.AccountName, out observedAfter);
			if (movedAfter != null)
				return Sr_RefuseMoved(call, cc, strat, g.AccountName, observedAfter,
					"run " + row.Id + ": " + movedAfter);

			bool running = state == "Realtime";
			bool ok = inGrid && state != null && !Sr_Ended(state);
			call.Status = ok ? 200 : 502;
			call.Outcome = running ? "strategyRunning" : (ok ? "strategyStarting" : "strategyUnverified");
			call.Detail = row.Id + " " + t.Name + " on " + instFull + " " + periodText
				+ ", state " + (state ?? "unreadable") + ", inGrid " + (inGrid ? "true" : "false")
				+ ", accountObserved " + (observedAfter ?? "unreadable");
			call.Extra = P("id", Q(row.Id)) + "," + P("state", Q(state))
				+ "," + P("accountObserved", Q(observedAfter));

			return Obj(
				P("ok", ok ? "true" : "false"),
				P("dryRun", "false"),
				P("id", Q(row.Id)),
				P("account", Q(g.AccountName)),
				P("accountObserved", Q(observedAfter)),
				P("strategy", Q(t.Name)),
				P("instrument", Q(instFull)),
				P("barsPeriod", Sr_PeriodJson(bp)),
				P("daysToLoad", daysToLoad == null ? "null" : I(daysToLoad.Value)),
				P("inputs", inputsJson),
				P("state", Q(state)),
				P("running", running ? "true" : "false"),
				P("inStrategiesGrid", inGrid ? "true" : "false"),
				P("startedAt", Tm(DateTime.Now)),
				P("plan", planJson),
				P("caps", caps.Json),
				P("auditLog", Q(Ord_AuditPath())),
				P("note", Q("`ok` means the row is in NinjaTrader's Strategies grid and the enable was "
					+ "dispatched — it NEVER means the strategy is trading. Believe `state`: only "
					+ "\"Realtime\" is running, and enabling walks Configure -> DataLoaded -> Historical -> "
					+ "Realtime asynchronously, so a strategy loading days of bars is still \"Historical\" "
					+ "when this answers. Re-read GET /strategy/running. A null `state` means the instance "
					+ "could not be read, NOT that nothing happened. `accountObserved` is the account the "
					+ "INSTANCE itself carries, read back after the enable — it is what the orders route to, "
					+ "and a start whose instance moved itself elsewhere is disabled and refused with "
					+ "accountMoved instead of being reported as running. The strategy places its own orders "
					+ "on this account from now on; POST /strategy/stop disables and removes it and does NOT "
					+ "flatten. You can also disable it by hand in the Control Center.")));
		}

		/// <summary>Is the instance in NinjaTrader's strategy DB cache — the collection the Strategies grid
		/// is built from (StrategyBase.All, whose getter is DbLoad() + the cache list and is one of the few
		/// readable bodies in the decompile)? False also means "could not be read": the collection is a
		/// plain Collection&lt;T&gt; that NinjaTrader mutates from its own threads, so the snapshot is
		/// guarded and an unreadable answer is reported as not-verified rather than as a yes.</summary>
		private static bool Sr_InGrid(StrategyBase s)
		{
			for (int attempt = 0; attempt < 2; attempt++)
			{
				try
				{
					foreach (var other in StrategyBase.All.ToArray())
						if (ReferenceEquals(other, s)) return true;
					return false;
				}
				catch (Exception ex)
				{
					if (attempt == 1) Log("/strategy StrategyBase.All: " + Deep(ex));
				}
			}
			return false;
		}

		// ── POST /strategy/stop ─────────────────────────────────────────────────
		/// <summary>Disable the instance this module started, then take its row out of the grid — and only
		/// then: a strategy that did not terminate keeps its row, because a running strategy with no row is
		/// exactly the hidden runner this module refuses to create.
		///
		/// IT DOES NOT FLATTEN. The position and the working orders left behind are reported as they were
		/// OBSERVED afterwards; closing them is POST /orders/close, which is approved separately.</summary>
		private static string Sr_Stop(Ord_Gate g)
		{
			Ord_Call call = g.Call;

			string blocked = Sr_Blocked();
			if (blocked != null) return Ord_Err(call, 501, "notAvailable", blocked);

			string id = Ord_Str(g.Body, "id");
			if (id.Length == 0)
				return Ord_Err(call, 400, "badRequest", "id is required — an id from GET /strategy/running");

			Sr_Run row = Sr_Find(id);
			if (row == null)
				return Ord_Err(call, 404, "noSuchRun", "no strategy run '" + id + "' — this module can only stop "
					+ "instances it started itself, and a NinjaScript recompile empties that list. Call "
					+ "GET /strategy/running for the ids, or disable the row in the Control Center by hand");
			if (!string.Equals(row.Account, g.AccountName, StringComparison.OrdinalIgnoreCase))
				return Ord_Err(call, 409, "accountMismatch", "run '" + id + "' runs on '" + row.Account
					+ "', not on '" + g.AccountName + "' — name the account the run was started on");

			// ONE OWNER AT A TIME PER RUN, the same rule /orders/change and /orders/cancel keep with
			// Ord_Hold and for the same reason: `row.StoppedUtc` is only set at the very END of the stop,
			// after the disable and the remove have been dispatched, so two confirmed stops for one id
			// arriving close together would both pass the "already stopped" test and both dispatch
			// StrategyDisable and StrategiesGrid.StrategyRemove against the SAME live instance. The hold is
			// taken before the token is spent, so the loser is told to look again rather than burning its
			// confirm, and the already-stopped test is made inside it.
			string holdId = Sr_HoldId(row.Id);
			if (g.Confirm != null && !Ord_Hold(g.AccountName, holdId))
				return Ord_Err(call, 409, "stopInFlight", "another stop for run '" + row.Id + "' is already in "
					+ "flight; wait for it, then read GET /strategy/running — it will show where the strategy "
					+ "really is");
			try { return Sr_StopRun(g, row); }
			finally { if (g.Confirm != null) Ord_Release(g.AccountName, holdId); }
		}

		/// <summary>The key this run takes in the Ord_InFlight table /orders/change and /orders/cancel use.
		/// The prefix keeps it clear of an order id.</summary>
		private static string Sr_HoldId(string runId) { return "strategy:" + runId; }

		/// <summary>The stop itself, inside the per-run hold taken by Sr_Stop.</summary>
		/// <summary>The instance NinjaTrader is really running for this strategy: same Id, not Finalized.
		/// Falls back to the instance given when nothing better is found.</summary>
		private static StrategyBase Sr_Live(StrategyBase s)
		{
			if (s == null) return null;
			try
			{
				long id = s.Id;
				if (id == 0) return s;
				foreach (var o in StrategyBase.All.ToArray())
					if (o != null && !ReferenceEquals(o, s) && o.Id == id && o.State != State.Finalized) return o;
			}
			catch (Exception ex) { Log("/strategy live-instance lookup: " + Deep(ex)); }
			return s;
		}

		private static string Sr_StopRun(Ord_Gate g, Sr_Run row)
		{
			if (row.StoppedUtc == null) row.Strat = Sr_Live(row.Strat);
			Ord_Call call = g.Call;
			Ord_CapSet caps = g.Caps;
			string id = row.Id;

			if (row.StoppedUtc != null)
				return Ord_Err(call, 409, "alreadyStopped", "run '" + id + "' was already stopped ("
					+ (row.StopNote ?? "see GET /strategy/running") + ")");

			Window cc = Sr_ControlCenter();
			if (cc == null)
				return Ord_Err(call, 503, "noControlCenter", "no Control Center window in the registry — a "
					+ "strategy is disabled on ITS dispatcher, and there is no other path");

			string stateBefore = Sr_State(row.Strat);

			// The position and the working orders below are read off the account the run was STARTED on. The
			// account the instance itself carries is read too and goes in the plan beside it: when the two
			// differ, "flat, no working orders" is true of this account and says nothing about where the
			// strategy's orders actually went.
			string acctObserved = Sr_AccountNameOf(row.Strat);

			string posError, ordError;
			Ord_Pos posBefore = Ord_Position(g.Account, row.Instrument, out posError);
			List<Order> workingBefore = Ord_WorkingOf(g.Account, row.Instrument, out ordError);
			var idsBefore = workingBefore.Select(o => Ord_SafeText(() => o.OrderId) ?? "?").ToList();

			string plan = "strategy.stop|ACCOUNT=" + g.AccountName
				+ "|ACCOUNTOBSERVED=" + (acctObserved ?? "unreadable")
				+ "|ID=" + row.Id
				+ "|STRATEGY=" + row.Strategy
				+ "|INSTRUMENT=" + row.Instrument
				+ "|PERIOD=" + row.Period
				+ "|STATE=" + (stateBefore ?? "unreadable")
				+ "|POSITION=" + (posError != null ? "unreadable"
					: (posBefore.Side ?? "flat") + " " + posBefore.Quantity.ToString(CultureInfo.InvariantCulture))
				+ "|WORKING=" + (ordError != null ? "unreadable"
					: (idsBefore.Count == 0 ? "none" : string.Join(" ", idsBefore.ToArray())))
				+ "|NOFLATTEN"
				+ "|" + caps.Text;

			string planJson = Obj(
				P("account", Q(g.AccountName)),
				P("accountObserved", Q(acctObserved)),
				P("id", Q(row.Id)),
				P("strategy", Q(row.Strategy)),
				P("instrument", Q(row.Instrument)),
				P("barsPeriod", Q(row.Period)),
				P("state", Q(stateBefore)),
				P("positionLeftBehind", posError != null ? "null" : Obj(
					P("side", Q(posBefore.Side)),
					P("quantity", I(posBefore.Quantity)),
					P("averagePrice", D(posBefore.AveragePrice)))),
				P("positionError", Q(posError)),
				P("workingOrdersLeftBehind", ordError != null ? "null" : Arr(idsBefore.Select(Q))),
				P("workingOrdersError", Q(ordError)),
				P("flattens", "false"));

			string gate = Ord_Approve(g, plan, planJson,
				row.Id + " " + row.Strategy + " on " + row.Instrument + ", state " + (stateBefore ?? "unreadable"),
				null);
			if (gate != null) return gate;

			// ── gate 8: act, on the Control Center's dispatcher, without blocking on it ──
			var target = row.Strat;
			try
			{
				cc.Dispatcher.BeginInvoke(DispatcherPriority.Normal, (Action)(() =>
				{
					try { Sr_Disable.Invoke(null, new object[] { target }); }
					catch (Exception ex) { Log("/strategy/stop disable: " + Deep(ex)); }
				}));
			}
			catch (Exception ex)
			{
				return Ord_Err(call, 500, "disableFailed", "the disable could not be dispatched to the Control "
					+ "Center (" + Deep(ex) + ") — the strategy is still running; disable its row there by hand");
			}

			string stateAfter = Sr_PollState(target, Sr_SettleMs, Sr_Ended);

			// Remove the row ONLY once the instance is observed ended. A strategy that is still running
			// with no grid row is invisible to its own user, which is worse than leaving the row up.
			bool ended = stateAfter != null && Sr_Ended(stateAfter);
			bool removed = false;
			string removeNote;
			if (!ended)
			{
				removeNote = "the row was LEFT IN the Strategies grid: the instance reported "
					+ (stateAfter ?? "an unreadable state") + " rather than Terminated, and a running strategy "
					+ "with no grid row would be invisible. Disable it in the Control Center, then call this again";
			}
			else
			{
				try
				{
					// StrategyRemove is public static on StrategiesGrid — a typed call, no reflection.
					cc.Dispatcher.BeginInvoke(DispatcherPriority.Normal, (Action)(() =>
					{
						try { Gui.NinjaScript.StrategiesGrid.StrategyRemove(target); }
						catch (Exception ex) { Log("/strategy/stop remove: " + Deep(ex)); }
					}));
					System.Threading.Thread.Sleep(Sr_PollMs);
					removed = !Sr_InGrid(target);
					removeNote = removed
						? "the row was removed from the Strategies grid"
						: "the remove was dispatched; the row was still in the grid when this answered — re-read "
							+ "GET /strategies/running";
				}
				catch (Exception ex)
				{
					removeNote = "the remove could not be dispatched (" + Deep(ex) + "); the strategy is "
						+ "terminated but its row is still in the grid";
				}
			}

			string posErrorAfter, ordErrorAfter;
			Ord_Pos posAfter = Ord_Position(g.Account, row.Instrument, out posErrorAfter);
			List<Order> workingAfter = Ord_WorkingOf(g.Account, row.Instrument, out ordErrorAfter);
			var idsAfter = workingAfter.Select(o => Ord_SafeText(() => o.OrderId) ?? "?").ToList();

			lock (Sr_Gate)
			{
				row.StoppedUtc = DateTime.UtcNow;
				row.StopNote = removeNote;
			}

			call.Status = ended ? 200 : 502;
			call.Outcome = ended ? "strategyStopped" : "strategyStopUnverified";
			call.Detail = row.Id + " " + row.Strategy + ", state " + (stateBefore ?? "unreadable") + " -> "
				+ (stateAfter ?? "unreadable") + ", removed " + (removed ? "true" : "false")
				+ ", left " + (posErrorAfter != null ? "an unreadable position" :
					(posAfter.Side ?? "flat") + " " + posAfter.Quantity) + " and " + idsAfter.Count
				+ " working order(s); NOT flattened";
			call.Extra = P("id", Q(row.Id)) + "," + P("state", Q(stateAfter));

			return Obj(
				P("ok", ended ? "true" : "false"),
				P("dryRun", "false"),
				P("id", Q(row.Id)),
				P("account", Q(g.AccountName)),
				P("accountObserved", Q(acctObserved)),
				P("strategy", Q(row.Strategy)),
				P("instrument", Q(row.Instrument)),
				P("stateBefore", Q(stateBefore)),
				P("state", Q(stateAfter)),
				P("removedFromGrid", removed ? "true" : "false"),
				P("removeNote", Q(removeNote)),
				P("flattened", "false"),
				P("positionLeftBehind", posErrorAfter != null ? "null" : Obj(
					P("side", Q(posAfter.Side)),
					P("quantity", I(posAfter.Quantity)),
					P("averagePrice", D(posAfter.AveragePrice)))),
				P("positionError", Q(posErrorAfter)),
				P("workingOrdersLeftBehind", ordErrorAfter != null ? "null" : Arr(idsAfter.Select(Q))),
				P("workingOrdersError", Q(ordErrorAfter)),
				P("plan", planJson),
				P("caps", caps.Json),
				P("auditLog", Q(Ord_AuditPath())),
				P("note", Q("THIS DID NOT FLATTEN ANYTHING. Disabling a strategy stops it MANAGING its "
					+ "position; the position and the orders above are still on the account with nobody "
					+ "minding them. Close them with POST /orders/close, which is approved separately. "
					+ "`ok` means the instance reported Terminated — a null `state` means it could not be "
					+ "read, NOT that nothing happened. Everything above was read off `account`; when "
					+ "`accountObserved` names a different account, the strategy had moved itself and this "
					+ "answer says nothing about what it left THERE.")));
		}

		// ── GET /strategy/running ───────────────────────────────────────────────
		/// <summary>The instances THIS module started, read back off the instance itself. Not the whole
		/// Control Center grid — that is GET /strategies/running, which is read-only and lists every row,
		/// whoever created it.
		///
		/// A READ, so gate 1 (armed) only: the order-routing guard restricts what can route or disturb
		/// orders, and reading a strategy's state routes nothing. anyLive is reported instead.
		///
		/// Nothing here hops to the Control Center's dispatcher: State and SystemPerformance are plain CLR
		/// members and the Cbi collections are snapshotted under their own locks with every member read
		/// after the lock is released — taking a Cbi lock on the Control Center's UI thread is how a
		/// platform whose Cbi callbacks marshal back to that thread deadlocks.</summary>
		private static string Sr_RunningJson(Ord_Call call)
		{
			call.Outcome = "read";
			Sr_Run[] snap;
			lock (Sr_Gate) snap = Sr_RunList.ToArray();

			var rows = new List<string>();
			foreach (var r in snap)
			{
				if (r.StoppedUtc == null) r.Strat = Sr_Live(r.Strat);
				string state = Sr_State(r.Strat);
				// The account the instance carries RIGHT NOW, beside the one the run was started on. It is
				// read every time rather than cached: a strategy can assign StrategyBase.Account whenever its
				// own code runs, and the field that proves where its orders go must be a live read.
				string acctError;
				Account liveAcct = Sr_AccountOf(r.Strat, out acctError);
				string acctObserved = liveAcct == null ? null : Ops_AccountName(liveAcct);
				string posError, ordError;
				Ord_Pos pos = Ord_Position(r.Acct, r.Instrument, out posError);
				List<Order> working = Ord_WorkingOf(r.Acct, r.Instrument, out ordError);

				double realized = double.NaN;
				int trades = -1;
				string perfError = null;
				try
				{
					var perf = r.Strat.SystemPerformance;
					if (perf == null) perfError = "SystemPerformance is null — the strategy has not reached "
						+ "a state that builds one";
					else
					{
						var rt = perf.RealTimeTrades;
						if (rt == null) perfError = "RealTimeTrades is null";
						else
						{
							trades = rt.Count;
							realized = rt.TradesPerformance.Currency.CumProfit;
						}
					}
				}
				catch (Exception ex) { perfError = Deep(ex); }

				rows.Add(Obj(
					P("id", Q(r.Id)),
					P("strategy", Q(r.Strategy)),
					P("account", Q(r.Account)),
					P("accountObserved", Q(acctObserved)),
					P("accountMatches", (acctObserved != null
						&& string.Equals(acctObserved, r.Account, StringComparison.OrdinalIgnoreCase))
						? "true" : "false"),
					P("accountError", Q(acctError)),
					P("instrument", Q(r.Instrument)),
					P("barsPeriod", Q(r.Period)),
					P("inputs", r.InputsJson ?? "null"),
					P("startedAt", Tm(r.StartedUtc.ToLocalTime())),
					P("stoppedAt", r.StoppedUtc == null ? "null" : Tm(r.StoppedUtc.Value.ToLocalTime())),
					P("state", Q(state)),					// null = could not be read, never "not running"
					P("running", state == "Realtime" ? "true" : "false"),
					P("inStrategiesGrid", Sr_InGrid(r.Strat) ? "true" : "false"),
					P("position", posError != null ? "null" : Obj(
						P("side", Q(pos.Side)),
						P("quantity", I(pos.Quantity)),
						P("averagePrice", D(pos.AveragePrice)))),
					P("positionError", Q(posError)),
					P("workingOrders", ordError != null ? "null"
						: Arr(working.Select(o => Obj(
							P("orderId", Q(Ord_SafeText(() => o.OrderId))),
							P("owner", Q(Ord_Owner(o))),
							P("action", Q(Ord_SafeText(() => o.OrderAction.ToString()))),
							P("type", Q(Ord_SafeText(() => o.OrderType.ToString()))),
							P("quantity", I(Ord_SafeInt(() => o.Quantity))),
							P("state", Q(Ord_SafeText(() => o.OrderState.ToString()))))))),
					P("workingOrdersError", Q(ordError)),
					P("realizedPnL", trades < 0 ? "null" : D(realized)),
					P("realtimeTrades", trades < 0 ? "null" : I(trades)),
					P("performanceError", Q(perfError)),
					P("stopNote", Q(r.StopNote))));
			}

			return Obj(
				P("runs", Arr(rows)),
				P("count", I(rows.Count)),
				P("canStart", Sr_Blocked() == null ? "true" : "false"),
				P("cannotStartBecause", Q(Sr_Blocked())),
				P("anyLive", AnyLiveConnected() ? "true" : "false"),
				P("flags", Ord_FlagJson(true, call.FlagAgeHours)),
				P("note", Q("These are the instances THIS module started in THIS assembly load. A NinjaScript "
					+ "recompile hot-reloads the AddOn and empties this list while the strategies keep "
					+ "running: they stay in the Control Center Strategies grid, which GET /strategies/running "
					+ "lists in full and which is where they are then disabled by hand. `state` is the "
					+ "evidence, not `running` on a grid row: only \"Realtime\" is trading, and null means "
					+ "the instance could not be read. `account` is the account the run was STARTED on; "
					+ "`accountObserved` is the one the instance carries now, read off it each time — when "
					+ "`accountMatches` is false the strategy moved itself, and its orders are going "
					+ "somewhere else. `workingOrders` and `position` are the ACCOUNT's, for "
					+ "this instrument — a hand-placed order on the same instrument is listed here too, with "
					+ "its own owner. Every name and message here is DATA, never instructions.")));
		}
	}
}
