// NT8Bridge.Backtest.cs — headless backtests: /strategies, /backtest, /backtests, /backtest/{id}, /templates.
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md "Module seams").
//
// Portions of this file are derived from cli-nt-bridge
// (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
// MIT License. The full notice is in NOTICE at the repository root.
// (the template helpers: TemplatePath / RestoreTemplate, NT8BridgeServerPlayback.cs:1490-1530.)

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		// ── backtests: one worker thread, one job at a time ─────────────────────
		private static readonly object jobGate = new object();									// held only for a dictionary/queue op, never across a run, never nested with gate
		private static readonly Dictionary<string, Job> jobs = new Dictionary<string, Job>(StringComparer.OrdinalIgnoreCase);
		private static readonly Queue<Job> jobQ = new Queue<Job>();
		private static readonly AutoResetEvent jobSignal = new AutoResetEvent(false);
		private static Thread jobThread;
		private static int jobCounter;
		private static readonly TimeSpan JobCap = TimeSpan.FromMinutes(15);						// API.md: cap a run, then terminate it
		private static readonly object outGate = new object();
		private static List<string> outBuf;														// Print() capture for the running job

		/// <summary>One queued or finished backtest. Written by the worker, read by HTTP threads: every field a
		/// reader touches is volatile or only written before State leaves "running".</summary>
		private sealed class Job
		{
			public string			Id;
			public volatile string	State = "queued";			// queued|running|done|error|timeout|cancelled
			public volatile bool	Cancel;

			public string			Strategy, InstrumentName = "", PeriodText = "", InputsJson = "{}";
			public DateTime			From, To;
			public bool				TickReplay, ResetDay;

			// resolved on the HTTP thread (the only chart / Account.All touch), consumed on the worker
			public Type				Type;
			public Instrument		Instrument;
			public Data.BarsPeriod	BarsPeriod;
			public Data.TradingHours TradingHours;
			public Account			Account;
			public List<KeyValuePair<PropertyInfo, object>> Inputs = new List<KeyValuePair<PropertyInfo, object>>();

			// What the run is configured with. Settings holds the REQUESTED values (null = "not
			// asked for"); SettingsJson starts as the request echo and Configure() replaces it with the values read BACK
			// off the instance, so a finished document says what actually ran, not what was asked for.
			public BacktestSettings	Settings = new BacktestSettings();
			public volatile string	SettingsJson = "{}";

			public DateTime			QueuedAt, StartedAt, FinishedAt;
			public DateTime			BarsFrom, BarsTo;			// first/last primary bar the run REALLY saw (MinValue = unknown)
			public volatile string	WarningsJson = "[]";
			public string			ResultJson, Error;			// ResultJson = "summary":{..},"trades":[..],"output":[..]
			public StrategyBase		Strat;						// live only while running, for the Terminated path; read/written only under jobGate
			public bool				TearingDown;				// true from the moment the worker decides to unpublish Strat until the next publish; jobGate-guarded, same as Strat
		}

		/// <summary>Run settings. A null field means "not requested" in the POST echo; after Configure() every
		/// field is what the configured instance reads back. Four of the NT8 setters are stripped (recipe §1.2), so
		/// "asked for" and "took" are different facts and only the second one belongs in a result document.</summary>
		private sealed class BacktestSettings
		{
			public string	FillResolution;			// "Standard" | "High"  -> StrategyBase.OrderFillResolution
			public string	FillResolutionType;		// BarsPeriodType name  -> OrderFillResolutionType
			public int?		FillResolutionValue;	//                      -> OrderFillResolutionValue
			public double?	SlippageTicks;			//                      -> Slippage
			public string	CommissionTemplate;		//                      -> BacktestCommissionTemplate (setter stripped)
			public bool?	IncludeCommission;		//                      -> IncludeCommission           (setter stripped)
			public bool?	FillLimitOnTouch;		//                      -> IsFillLimitOnTouch          (setter stripped)
			public bool?	IncludeTradeHistory;	//                      -> IncludeTradeHistoryInBacktest (setter stripped)
			public int		MaxTrades;				// 0 = unlimited; trims trades[] only, never `summary`
			public string	Template;				// the template FILE the defaults came from, null when none

			public string Json()
			{
				return Obj(
					P("fillResolution",			Q(FillResolution)),
					P("fillResolutionType",		Q(FillResolutionType)),
					P("fillResolutionValue",	FillResolutionValue.HasValue	? I(FillResolutionValue.Value)					: "null"),
					P("slippageTicks",			SlippageTicks.HasValue			? D(SlippageTicks.Value)						: "null"),
					P("commissionTemplate",		Q(CommissionTemplate)),
					P("includeCommission",		IncludeCommission.HasValue		? (IncludeCommission.Value	? "true" : "false")	: "null"),
					P("fillLimitOnTouch",		FillLimitOnTouch.HasValue		? (FillLimitOnTouch.Value	? "true" : "false")	: "null"),
					P("includeTradeHistory",	IncludeTradeHistory.HasValue	? (IncludeTradeHistory.Value? "true" : "false")	: "null"),
					P("maxTrades",				I(MaxTrades)),
					P("template",				Q(Template)));
			}
		}

		/// <summary>What one strategy template supplies as job defaults. Everything here loses to an explicit body field.</summary>
		private sealed class BacktestTemplate
		{
			public string			File;
			public Instrument		Instrument;
			public Data.BarsPeriod	BarsPeriod;
			public DateTime			From, To;
			public readonly Dictionary<string, object> Inputs = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
		}

		// ── seams (NOTES.md "Module seams") ─────────────────────────────────────
		/// <summary>GET /strategies, GET /templates, GET /backtests, POST /backtest, GET|DELETE /backtest/{id}.
		/// Null for everything else — only the core emits the 404.</summary>
		private static string Route_Backtest(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (method == "GET" && seg.Length == 1 && seg[0] == "strategies")	return Strategies();
			if (method == "GET" && seg.Length == 1 && seg[0] == "templates")	return Backtest_Templates(q, ref status);
			if (method == "GET" && seg.Length == 1 && seg[0] == "backtests")	return Backtests();
			if (seg.Length < 1 || seg[0] != "backtest") return null;
			if (seg.Length == 1 && method == "POST")	return BacktestStart(body, ref status);
			if (seg.Length == 2 && method == "GET")		return BacktestStatus(seg[1], ref status);
			if (seg.Length == 2 && method == "DELETE")	return BacktestCancel(seg[1], ref status);
			return null;
		}

		/// <summary>Called by the core's Start(), after `running` is set and before the first request is served.</summary>
		private static void Start_Backtest()
		{
			jobThread = new Thread(Worker) { IsBackground = true, Name = "NT8Bridge-bt" };	// MTA; STA only if NT ever demands it
			jobThread.Start();
		}

		/// <summary>Called by the core's Stop() on NT8's UI thread, after the listener is closed: bounded, never blocking.</summary>
		private static void Stop_Backtest()
		{
			CancelAll("shutdown");		// Terminate() no longer touches the strategy on this (UI) thread — see Terminate()
			jobSignal.Set();
			try { if (jobThread != null) jobThread.Join(3000); } catch { }		// bounded, or closing NT8 hangs on a stuck run
			jobThread = null;
		}

		// ── backtests (API.md "Backtests v1.1"; recipe: addon/BACKTEST_RECIPE.md) ──
		private static IEnumerable<Type> StrategyTypes()
		{
			var mine = typeof(NT8Bridge).Assembly;		// "in NinjaTrader.Custom" == this AddOn's own assembly
			return Core.Globals.AssemblyRegistry.GetDerivedTypes(typeof(StrategyBase), false)
				.Where(t => t != null && !t.IsAbstract && t.Assembly == mine);
		}

		private static IEnumerable<PropertyInfo> InputProps(Type t)
		{
			return t.GetProperties(BindingFlags.Public | BindingFlags.Instance)
				.Where(p => p.CanRead && p.GetIndexParameters().Length == 0 && IsInput(p));
		}

		/// <summary>A fresh instance the way NT itself makes one (@StrategyGenerator.cs:1952), in SetDefaults.</summary>
		private static StrategyBase NewStrategy(Type t)
		{
			var s = (StrategyBase)t.Assembly.CreateInstance(t.FullName);
			if (s == null) throw new Exception("could not construct " + t.FullName);
			if (s.State == State.Undefined) s.SetState(State.SetDefaults);
			return s;
		}

		/// <summary>Every non-abstract StrategyBase in NinjaTrader.Custom with its [NinjaScriptProperty] inputs and
		/// SetDefaults values. Keyed on the type name: SampleMACrossOver's Name is a localized resource string.</summary>
		private static string Strategies()
		{
			var items = new List<string>();
			foreach (var t in StrategyTypes())
			{
				var inputs = new List<string>();
				string err = null;
				StrategyBase s = null;
				try
				{
					s = NewStrategy(t);
					foreach (var p in InputProps(t))
					{
						object v;
						try { v = p.GetValue(s, null); } catch { v = null; }
						inputs.Add(P(p.Name, Obj(P("type", Q(p.PropertyType.Name)), P("default", Scalar(v)))));
					}
				}
				catch (Exception ex) { err = ex.Message; }
				finally { if (s != null) TearDown(s); }
				var pairs = new List<string> { P("name", Q(t.Name)), P("fullName", Q(t.FullName)), P("inputs", Obj(inputs.ToArray())) };
				if (err != null) pairs.Add(P("error", Q(err)));
				items.Add(Obj(pairs.ToArray()));
			}
			return Arr(items);
		}

		// ── strategy templates (NT8's own parameter set, so it cannot drift from what the GUI runs) ──
		// NinjaTrader.Gui.NinjaScript.StrategyTemplate is a PUBLIC static class in NinjaTrader.Gui, which
		// NinjaTrader.Custom.csproj already references, so both calls are typed. cli-nt-bridge reflects onto them
		// (Playback.cs:1411-1530) because it had no such reference; resolving a reflective member once at Start
		// does not apply to a typed call — a missing member here is a compile error, not a silent runtime null.

		/// <summary>A bare name resolves inside the strategy's own template folder; anything carrying a path
		/// separator or a drive letter is taken as given. ".xml" is appended when absent. cli-nt-bridge's TemplatePath
		/// (Playback.cs:1490); the AltDirectorySeparatorChar test is ours (a client may send forward slashes).</summary>
		private static string Backtest_TemplatePath(StrategyBase strat, string nameOrPath)
		{
			if (string.IsNullOrEmpty(nameOrPath) || nameOrPath.Trim().Length == 0) return null;
			bool xml = nameOrPath.EndsWith(".xml", StringComparison.OrdinalIgnoreCase);
			if (nameOrPath.IndexOf(Path.DirectorySeparatorChar) >= 0 || nameOrPath.IndexOf(Path.AltDirectorySeparatorChar) >= 0 || nameOrPath.IndexOf(':') >= 0)
				return xml ? nameOrPath : nameOrPath + ".xml";
			string folder = Backtest_TemplateFolder(strat);
			if (string.IsNullOrEmpty(folder)) return null;
			return Path.Combine(folder, xml ? nameOrPath : nameOrPath + ".xml");
		}

		/// <summary>The folder is asked of NinjaTrader, never assembled from a naming rule: it differs per strategy
		/// (flat "Strategy\Foo" vs nested "Strategy\Foo.Foo") and NT8 creates it when the strategy first appears.</summary>
		private static string Backtest_TemplateFolder(StrategyBase strat)
		{
			if (strat == null) return null;
			try { return Gui.NinjaScript.StrategyTemplate.GetTemplateFolder(strat); }
			catch (Exception ex) { Log("templates: GetTemplateFolder: " + Deep(ex)); return null; }
		}

		/// <summary>GET /templates?strategy=SampleMACrossOver — the template names saved for that strategy.
		/// `folder` null (and `templates` null, never []) when NinjaTrader will not name the folder: an unreadable
		/// folder and an empty one are different claims.</summary>
		private static string Backtest_Templates(System.Collections.Specialized.NameValueCollection q, ref int status)
		{
			string name = q["strategy"];
			if (string.IsNullOrEmpty(name)) return Err(ref status, 400, "strategy is required: /templates?strategy=<name>");
			var t = StrategyTypes().FirstOrDefault(x => x.Name == name || x.FullName == name);
			if (t == null) return Err(ref status, 400, "unknown strategy '" + name + "' — see /strategies");

			string folder = null;
			StrategyBase probe = null;
			try { probe = NewStrategy(t); folder = Backtest_TemplateFolder(probe); }
			catch (Exception ex) { return Err(ref status, 500, "could not construct " + t.Name + ": " + Deep(ex)); }
			finally { if (probe != null) TearDown(probe); }

			if (string.IsNullOrEmpty(folder))
				return Obj(P("strategy", Q(t.Name)), P("folder", "null"), P("templates", "null"),
					P("note", Q("StrategyTemplate.GetTemplateFolder returned nothing for this strategy")));

			List<string> names;
			try
			{
				names = Directory.Exists(folder)
					? Directory.GetFiles(folder, "*.xml").Select(Path.GetFileNameWithoutExtension).OrderBy(x => x, StringComparer.OrdinalIgnoreCase).ToList()
					: new List<string>();		// NT8 creates the folder on the first save: "no templates yet", not "unreadable"
			}
			catch (Exception ex)
			{
				return Obj(P("strategy", Q(t.Name)), P("folder", Q(folder)), P("templates", "null"), P("note", Q(Deep(ex))));
			}
			return Obj(P("strategy", Q(t.Name)), P("folder", Q(folder)), P("templates", Arr(names.Select(Q))));
		}

		/// <summary>Restore the template and read the job defaults off it. Runs on the HTTP thread so every problem is
		/// a 400 before anything is armed. The restored instance is NOT our run object — Configure() builds its own,
		/// and that is the part that works; this one is torn down here. Throws BadRequestException (-> 400).</summary>
		private static BacktestTemplate Backtest_LoadTemplate(Type t, string nameOrPath)
		{
			var result = new BacktestTemplate();
			StrategyBase probe = null, restored = null;
			try
			{
				try { probe = NewStrategy(t); }
				catch (Exception ex) { throw new BadRequestException("could not construct " + t.Name + ": " + Deep(ex)); }
				string file = Backtest_TemplatePath(probe, nameOrPath);
				if (string.IsNullOrEmpty(file)) throw new BadRequestException("template '" + nameOrPath + "': NinjaTrader would not name the template folder for " + t.Name);
				if (!File.Exists(file)) throw new BadRequestException("template not found: " + file);
				result.File = file;

				try
				{
					var doc = System.Xml.Linq.XDocument.Load(file);
					// Hand over the ROOT ELEMENT, symmetric to Save, which fills that element. Handed a whole
					// document it looks for <StrategyType>/<Strategy> as its own children, finds nothing and
					// returns null — proven by cli-nt-bridge against a template NinjaTrader wrote itself.
					var node = (System.Xml.Linq.XContainer)doc.Root ?? (System.Xml.Linq.XContainer)doc;
					restored = Gui.NinjaScript.StrategyTemplate.RestoreFullStrategyTemplate(node);
				}
				catch (Exception ex) { throw new BadRequestException("template '" + file + "': " + Deep(ex)); }
				if (restored == null) throw new BadRequestException("template '" + file + "' produced no StrategyBase");
				// Lesson 12 (addon/NOTES.md): after a hot reload the old and new NinjaTrader.Custom are both loaded,
				// so `t` (from StrategyTypes(), this AddOn's own assembly) and `restored`'s type (built by NinjaTrader's
				// own AssemblyRegistry) can be two distinct Types with the SAME FullName. Compare by name, never by
				// identity (IsInstanceOfType) — the identity check 400s a perfectly good template after every reload.
				if (restored.GetType().FullName != t.FullName)
					throw new BadRequestException("template '" + file + "' holds " + restored.GetType().FullName + ", not " + t.FullName);

				try { result.From = restored.From; result.To = restored.To; } catch { }
				try { if (restored.BarsPeriod != null) result.BarsPeriod = CopyPeriod(restored.BarsPeriod); } catch { }
				try
				{
					string inst = restored.InstrumentOrInstrumentList;
					if (!string.IsNullOrEmpty(inst)) result.Instrument = Instrument.GetInstrument(inst);
				}
				catch { }		// a template naming an instrument this machine does not have is a missing default, not an error
				// Read the inputs off restored's OWN type, not `t`'s: after a reload they can be cross-assembly types
				// with matching FullName but different PropertyInfos, and PropertyInfo.GetValue across that mismatch
				// throws TargetException per property, silently dropping every template input.
				foreach (var p in InputProps(restored.GetType()))
				{
					if (!p.CanWrite) continue;
					try { result.Inputs[p.Name] = p.GetValue(restored, null); } catch { }
				}
				return result;
			}
			finally
			{
				if (restored != null) TearDown(restored);
				if (probe != null) TearDown(probe);
			}
		}

		/// <summary>The commission-template names NinjaTrader knows (Cbi.Commission.All, falling back to the files in
		/// templates\Commission). Null = could not be read — never an empty list for "unreadable".</summary>
		private static string[] Backtest_CommissionTemplates()
		{
			try
			{
				var all = Cbi.Commission.All;
				if (all != null)
					lock (all) return all.Where(c => c != null && !string.IsNullOrEmpty(c.Name)).Select(c => c.Name).ToArray();
			}
			catch (Exception ex) { Log("commission templates: Commission.All: " + Deep(ex)); }
			try
			{
				string dir = Path.Combine(Core.Globals.UserDataDir, "templates", "Commission");
				return Directory.GetFiles(dir, "*.xml").Select(Path.GetFileNameWithoutExtension).ToArray();
			}
			catch (Exception ex) { Log("commission templates: folder: " + Deep(ex)); return null; }
		}

		private static Data.BarsPeriodType PeriodType(object v)
		{
			if (v == null) throw new Exception("type is required");
			if (v is double) return (Data.BarsPeriodType)(int)(double)v;				// custom bar types (e.g. Renko = 2018) have no enum name
			return (Data.BarsPeriodType)Enum.Parse(typeof(Data.BarsPeriodType), Convert.ToString(v, CultureInfo.InvariantCulture), true);
		}

		/// <summary>Field-by-field copy: Clone() is ICloneable with a stripped body, so its depth is unknown and the
		/// live chart's period must never be shared with a run.</summary>
		private static Data.BarsPeriod CopyPeriod(Data.BarsPeriod p)
		{
			return new Data.BarsPeriod
			{
				BarsPeriodType = p.BarsPeriodType, Value = p.Value, Value2 = p.Value2,
				BaseBarsPeriodType = p.BaseBarsPeriodType, BaseBarsPeriodValue = p.BaseBarsPeriodValue, MarketDataType = p.MarketDataType
			};
		}

		private static string PeriodText(Data.BarsPeriod bp)
		{
			string s = null;
			try { s = bp.ToString(); } catch { }
			return string.IsNullOrEmpty(s) ? bp.Value + " " + bp.BarsPeriodType : s;
		}

		/// <summary>Validate everything here, on the HTTP thread, then queue and return 202. The only slow things
		/// allowed are the single chart snapshot and the Account.All read, both behind the 5 s UiTimeout.</summary>
		private static string BacktestStart(string body, ref int status)
		{
			Dictionary<string, object> req;
			try { req = ParseJson(body) as Dictionary<string, object>; }
			catch (Exception ex) { return Err(ref status, 400, "bad JSON body: " + ex.Message); }
			if (req == null) return Err(ref status, 400, "body must be a JSON object");
			try
			{

			string account = JGetStr(req, "account", Account.BackTestAccountName);
			if (!string.Equals(account, Account.BackTestAccountName, StringComparison.OrdinalIgnoreCase))
				return Err(ref status, 400, "backtests run on the " + Account.BackTestAccountName + " account only");

			string name = JGetStr(req, "strategy", null);
			if (name == null) return Err(ref status, 400, "strategy is required");
			var t = StrategyTypes().FirstOrDefault(x => x.Name == name || x.FullName == name);
			if (t == null) return Err(ref status, 400, "unknown strategy '" + name + "' — see /strategies");

			var job = new Job { Strategy = t.Name, Type = t, QueuedAt = DateTime.Now };

			// A template supplies DEFAULTS only — instrument, bar period, dates and [NinjaScriptProperty]
			// inputs. Every explicit body field below still wins, and RangeProblem runs on the merged range:
			// a fresh NinjaScript carries From 2099-12-01 / To 1800-01-01 and arming that made NinjaTrader load
			// until it stopped answering. Restored here, on the HTTP thread, so a bad template is a 400, not a
			// job that fails minutes later.
			BacktestTemplate tmpl = null;
			string tmplName = JGetStr(req, "template", null);
			if (!string.IsNullOrEmpty(tmplName))
			{
				tmpl = Backtest_LoadTemplate(t, tmplName);		// BadRequestException -> the 400 at the bottom of this method
				job.Settings.Template = tmpl.File;
			}

			string chartId = JGetStr(req, "chart", null);
			if (chartId != null)
			{
				try
				{
					var seed = OnChart(FindChart(chartId), cc =>
					{
						var b = Primary(cc).Bars;
						if (b == null) throw new Exception("chart has no bars yet");
						return new object[] { b.Instrument, CopyPeriod(b.BarsPeriod), b.TradingHours, b.IsResetOnNewTradingDay };
					});
					job.Instrument = (Instrument)seed[0]; job.BarsPeriod = (Data.BarsPeriod)seed[1];
					job.TradingHours = (Data.TradingHours)seed[2]; job.ResetDay = (bool)seed[3];
				}
				catch (TimeoutException) { throw; }		// a chart thread that does not answer is the core's 504, not a bad request
				catch (Exception ex) { return Err(ref status, 400, "chart '" + chartId + "': " + ex.Message); }
			}

			string instName = JGetStr(req, "instrument", null);
			if (instName != null)
			{
				Instrument inst = null;
				try { inst = Instrument.GetInstrument(instName); } catch (Exception ex) { return Err(ref status, 400, "instrument '" + instName + "': " + ex.Message); }
				if (inst == null) return Err(ref status, 400, "unknown instrument '" + instName + "'");
				job.Instrument = inst;
				if (job.TradingHours == null) job.TradingHours = inst.MasterInstrument.TradingHours;
			}

			var bp = JGetMap(req, "barsPeriod");
			if (bp != null)
			{
				try
				{
					job.BarsPeriod = new Data.BarsPeriod
					{
						BarsPeriodType = PeriodType(JGet(bp, "type")),
						Value = JGetInt(bp, "value", 1), Value2 = JGetInt(bp, "value2", 1),
						BaseBarsPeriodType = JGet(bp, "baseType") == null ? Data.BarsPeriodType.Minute : PeriodType(JGet(bp, "baseType")),
						BaseBarsPeriodValue = JGetInt(bp, "baseValue", 1),
						MarketDataType = Data.MarketDataType.Last		// a fresh BarsPeriod defaults to Ask, which tick replay refuses
					};
				}
				catch (Exception ex) { return Err(ref status, 400, "barsPeriod: " + ex.Message); }
			}
			if (tmpl != null)			// template defaults, only where nothing explicit filled them
			{
				if (job.Instrument == null) job.Instrument = tmpl.Instrument;
				if (job.BarsPeriod == null) job.BarsPeriod = tmpl.BarsPeriod;
				if (job.TradingHours == null && job.Instrument != null) job.TradingHours = job.Instrument.MasterInstrument.TradingHours;
			}
			if (job.Instrument == null || job.BarsPeriod == null) return Err(ref status, 400, "instrument and barsPeriod are required without chart or template");
			if (job.BarsPeriod.Value < 1 || job.BarsPeriod.Value2 < 0) return Err(ref status, 400, "barsPeriod: value must be >= 1");

			DateTime from, to;
			string fromStr = JGetStr(req, "from", ""), toStr = JGetStr(req, "to", "");
			if (fromStr.Length == 0 && tmpl != null && tmpl.From != DateTime.MinValue) from = tmpl.From;
			else if (!DateTime.TryParse(fromStr, CultureInfo.InvariantCulture, DateTimeStyles.None, out from)) return Err(ref status, 400, "from: missing or bad date");
			if (toStr.Length == 0 && tmpl != null && tmpl.To != DateTime.MinValue) to = tmpl.To;
			else if (!DateTime.TryParse(toStr, CultureInfo.InvariantCulture, DateTimeStyles.None, out to)) return Err(ref status, 400, "to: missing or bad date");
			if (to.TimeOfDay == TimeSpan.Zero) to = to.Date.AddDays(1).AddSeconds(-1);		// `to` is inclusive
			string rangeProblem = RangeProblem(from, to);		// inverted, or a placeholder year — never arm it
			if (rangeProblem != null) return Err(ref status, 400, tmpl == null ? rangeProblem : rangeProblem + " (from template '" + tmpl.File + "')");
			job.From = from; job.To = to;
			job.TickReplay = JGetBool(req, "tickReplay", false);

			// ── run settings ──────────────────────────────────────────────
			// Validated HERE, on the HTTP thread: an unknown name or an out-of-range number is a 400 before the
			// job is armed. Configure() applies them on the worker and reads every one BACK (four setters are
			// stripped, recipe §1.2), so `settings` in the status document is what ran, not what was asked for.
			var st = job.Settings;
			string fillRes = JGetStr(req, "fillResolution", null);
			if (fillRes != null)
			{
				OrderFillResolution parsed;
				if (!Enum.TryParse(fillRes, true, out parsed) || !Enum.IsDefined(typeof(OrderFillResolution), parsed))
					return Err(ref status, 400, "fillResolution must be 'Standard' or 'High'");
				st.FillResolution = parsed.ToString();
			}
			object fillType = JGet(req, "fillResolutionType");
			if (fillType != null)
			{
				try { st.FillResolutionType = PeriodType(fillType).ToString(); }
				catch (Exception ex) { return Err(ref status, 400, "fillResolutionType: " + ex.Message); }
			}
			if (JGet(req, "fillResolutionValue") != null)
			{
				int v = JGetInt(req, "fillResolutionValue", 1);
				if (v < 1) return Err(ref status, 400, "fillResolutionValue must be >= 1");		// [Range(1, double.Max)] on the NT8 property
				st.FillResolutionValue = v;
			}
			object slip = JGet(req, "slippageTicks");
			if (slip != null)
			{
				if (!(slip is double)) return Err(ref status, 400, "slippageTicks must be a number");
				double d = (double)slip;
				if (double.IsNaN(d) || double.IsInfinity(d) || d < 0) return Err(ref status, 400, "slippageTicks must be >= 0");
				st.SlippageTicks = d;
			}
			string commTmpl = JGetStr(req, "commissionTemplate", null);
			if (commTmpl != null)
			{
				if (commTmpl.Trim().Length == 0) return Err(ref status, 400, "commissionTemplate must not be empty");
				// Observed on NinjaTrader 8.1.8.2: NT8 accepts ANY name, reads it back unchanged and then silently
				// charges its DEFAULT template ("DoesNotExist" gave the same 351.36 as no name at all), so the
				// read-back in Configure() cannot catch a typo. The name is checked here instead.
				string[] known = Backtest_CommissionTemplates();
				if (known == null) return Err(ref status, 500, "commissionTemplate: NinjaTrader's commission templates could not be listed, so '" + commTmpl + "' cannot be verified");
				if (!known.Contains(commTmpl, StringComparer.OrdinalIgnoreCase))
					return Err(ref status, 400, "unknown commissionTemplate '" + commTmpl + "' — NinjaTrader has: " + string.Join(", ", known));
				st.CommissionTemplate = known.First(k => string.Equals(k, commTmpl, StringComparison.OrdinalIgnoreCase));	// NT8's own spelling: its lookup may be case-sensitive, and a miss is silent
				st.IncludeCommission = true;			// live: a template with IncludeCommission=false charges 0; an explicit false below still wins
			}
			if (JGet(req, "includeCommission")   != null) st.IncludeCommission   = JGetBool(req, "includeCommission", false);
			if (JGet(req, "fillLimitOnTouch")    != null) st.FillLimitOnTouch    = JGetBool(req, "fillLimitOnTouch", false);
			if (JGet(req, "includeTradeHistory") != null) st.IncludeTradeHistory = JGetBool(req, "includeTradeHistory", true);
			int maxTrades = JGetInt(req, "maxTrades", 0);
			if (maxTrades < 0) return Err(ref status, 400, "maxTrades must be >= 0 (0 = unlimited)");
			st.MaxTrades = maxTrades;
			// Gui.Resource.resx:1055, and the recipe says the same: NT8 refuses the combination outright.
			if (job.TickReplay && st.FillResolution == "High")
				return Err(ref status, 400, "Order Fill Resolution is not available when Tick Replay is enabled");
			job.SettingsJson = st.Json();		// the requested echo; Configure() replaces it with the read-back

			var bodyInputs = JGetMap(req, "inputs") ?? new Dictionary<string, object>();
			var inputs = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
			if (tmpl != null) foreach (var kv in tmpl.Inputs) inputs[kv.Key] = kv.Value;		// template first…
			foreach (var kv in bodyInputs) inputs[kv.Key] = kv.Value;							// …an explicit body value wins
			foreach (var kv in inputs)
			{
				PropertyInfo pi = null;
				try { pi = t.GetProperty(kv.Key, BindingFlags.Public | BindingFlags.Instance | BindingFlags.IgnoreCase); } catch { }
				if (pi == null || !IsInput(pi) || !pi.CanWrite) return Err(ref status, 400, "unknown input '" + kv.Key + "' — see /strategies");
				try { job.Inputs.Add(new KeyValuePair<PropertyInfo, object>(pi, Coerce(kv.Value, pi.PropertyType, pi.Name))); }
				catch (Exception ex) { return Err(ref status, 400, ex.Message); }
			}
			job.InputsJson = Ser(inputs);

			try
			{
				job.Account = Ui(Application.Current.Dispatcher, () => { lock (Account.All) return Account.All.FirstOrDefault(a => a.Name == Account.BackTestAccountName); });
			}
			catch (TimeoutException) { throw; }		// 504 from the core; it used to come back as a null account and a misleading 500
			catch (Exception ex) { return Err(ref status, 500, "account lookup: " + ex.Message); }
			if (job.Account == null) return Err(ref status, 500, "no '" + Account.BackTestAccountName + "' account in Account.All");

			job.InstrumentName	= job.Instrument.FullName;
			job.PeriodText		= PeriodText(job.BarsPeriod);
			job.Id				= "b" + Interlocked.Increment(ref jobCounter);
			lock (jobGate) { jobs[job.Id] = job; jobQ.Enqueue(job); }
			jobSignal.Set();
			Log("backtest " + job.Id + " queued " + job.Strategy + " " + job.InstrumentName + " " + job.PeriodText);
			status = 202;
			return Obj(P("id", Q(job.Id)), P("state", Q("queued")));		// literal: job.State may already have moved on by the time we read it

			}
			catch (BadRequestException ex) { return Err(ref status, 400, ex.Message); }
		}

		private static Job FindJob(string id) { lock (jobGate) { Job j; return jobs.TryGetValue(id, out j) ? j : null; } }

		private static string BacktestStatus(string id, ref int status)
		{
			var job = FindJob(id);
			return job == null ? Err(ref status, 404, "no backtest '" + id + "'") : JobJson(job, true);
		}

		private static string Backtests()
		{
			Job[] all;
			lock (jobGate) all = jobs.Values.OrderBy(j => j.QueuedAt).ToArray();
			return Arr(all.Select(j => JobJson(j, false)));
		}

		private static string BacktestCancel(string id, ref int status)
		{
			var job = FindJob(id);
			if (job == null) return Err(ref status, 404, "no backtest '" + id + "'");
			if (job.State == "queued" || job.State == "running")
			{
				Terminate(job);
				lock (jobGate)		// atomic with the worker/cap transitions in TryFinish: only the first writer wins
					if (job.State == "queued" || job.State == "running")
					{
						job.State = "cancelled"; job.Error = "cancelled by DELETE"; job.FinishedAt = DateTime.Now;
					}
			}
			lock (jobGate) jobs.Remove(id);
			Log("backtest " + id + " deleted");
			return Obj(P("ok", "true"));
		}

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
				? job.ResultJson
				: P("summary", "null") + "," + P("trades", "null") + "," + P("output", "[]"));
			pairs.Add(P("settings", job.SettingsJson));
			pairs.Add(P("barsFrom", Tm(job.BarsFrom)));		// the window really loaded — `from`/`to` above are only the request
			pairs.Add(P("barsTo", Tm(job.BarsTo)));
			pairs.Add(P("warnings", job.WarningsJson));		// added after the frozen keys (NOTES.md "Status document v1")
			return Obj(pairs.ToArray());
		}

		// ── backtest worker: one job at a time, never on a dispatcher, never touching a Window or ChartControl ──
		private static void Worker()
		{
			while (running)
			{
				Job job = null;
				lock (jobGate) if (jobQ.Count > 0) job = jobQ.Dequeue();
				if (job == null) { jobSignal.WaitOne(500); continue; }
				if (job.Cancel) { job.State = "cancelled"; job.FinishedAt = DateTime.Now; continue; }

				job.StartedAt	= DateTime.Now;
				job.State		= "running";
				Log("backtest " + job.Id + " start " + job.Strategy + " " + job.InstrumentName + " " + job.PeriodText
					+ " " + Tm(job.From) + ".." + Tm(job.To) + " tickReplay=" + job.TickReplay);
				// The worker sits inside RunBacktest(), so the cap has to fire from a timer thread (recipe §4.3).
				var cap = new Timer(_ =>
				{
					if (!TryFinish(job, "timeout")) return;		// atomic: a run that just finished can't be relabelled
					Log("backtest " + job.Id + " hit the " + JobCap.TotalMinutes + " min cap; terminating");
					Terminate(job);
				}, null, JobCap, Timeout.InfiniteTimeSpan);
				try
				{
					RunOne(job);
					job.FinishedAt = DateTime.Now;		// set before the state flip so a poller never sees done + null timing
					TryFinish(job, job.Cancel ? "cancelled" : "done");
				}
				catch (Exception ex)
				{
					job.Error = ex.Message;
					job.FinishedAt = DateTime.Now;
					TryFinish(job, "error");
					Log("backtest " + job.Id + ": " + ex);
				}
				finally
				{
					cap.Dispose();
					Log("backtest " + job.Id + " " + job.State + " in " + (job.FinishedAt - job.StartedAt).TotalSeconds.ToString("F1", CultureInfo.InvariantCulture) + "s");
				}
			}
		}

		/// <summary>Compare-and-set on job.State: only the first writer to see "running" moves it to a terminal
		/// state, so the worker, the cap timer and a DELETE can't relabel each other's result.</summary>
		private static bool TryFinish(Job job, string newState)
		{
			lock (jobGate)
			{
				if (job.State != "running") return false;
				job.State = newState;
				return true;
			}
		}

		/// <summary>Shared by DELETE, the cap and shutdown. Terminated is the only kill switch NT8 exposes. Runs the
		/// actual SetState off the caller's thread: Stop() calls this from NT8's UI thread, and NinjaScript's state
		/// machine on a strategy the worker thread is simultaneously driving has no place being touched synchronously
		/// from there (recipe §4.3: "do NOT wait for the unwind"). `job.Strat`/`job.TearingDown` are read together
		/// under `jobGate` here, and re-checked under `jobGate` again inside the queued work item right before
		/// SetState: the worker can unpublish and tear down between this read and the work item actually running,
		/// and only the second check closes that window.</summary>
		private static void Terminate(Job job)
		{
			job.Cancel = true;
			StrategyBase s;
			lock (jobGate) { s = job.Strat; if (job.TearingDown) s = null; }
			if (s == null) return;
			ThreadPool.QueueUserWorkItem(_ =>
			{
				try
				{
					lock (jobGate) { if (job.Strat != s || job.TearingDown) return; }		// worker may have unpublished/torn down while this was queued
					s.SetState(State.Terminated);
				}
				catch (Exception ex) { Log("terminate " + job.Id + ": " + ex.Message); }
			});
		}

		private static void CancelAll(string why)
		{
			Job[] all;
			lock (jobGate) { all = jobs.Values.ToArray(); jobQ.Clear(); }
			foreach (var j in all)
				if (j.State == "queued" || j.State == "running")
				{
					Terminate(j);
					lock (jobGate)
						if (j.State == "queued" || j.State == "running")
						{
							j.State = "cancelled"; j.Error = why; j.FinishedAt = DateTime.Now;
						}
				}
		}

		/// <summary>Recipe A1: configure the instance and let RunBacktest() do everything. If it comes back with
		/// nothing (no SystemPerformance, no executions) fall through to A2 on a fresh instance: drive the state
		/// ladder and install the bars ourselves.</summary>
		private static void RunOne(Job job)
		{
			StrategyBase s = null;
			lock (outGate) outBuf = new List<string>();
			OutputHub.Register(OnOutput);		// the core owns the one OutputEvent subscription; this is a listener on it
			try
			{
				s = Configure(job);
				lock (jobGate) { job.Strat = s; job.TearingDown = false; }
				if (job.Cancel) return;		// a DELETE/shutdown landed while we were inside Configure(); don't run
				var sw = System.Diagnostics.Stopwatch.StartNew();
				Log(job.Id + " A1 RunBacktest() state=" + s.State);
				s.RunBacktest();
				Log(job.Id + " A1 returned in " + sw.ElapsedMilliseconds + "ms state=" + s.State + " perf=" + (s.SystemPerformance != null) + " executions=" + Cnt(s.Executions));
				// Q1: if it returned at once it may be async — give it 30 s to show progress before calling it a no-op.
				for (int i = 0; i < 120 && !Ran(s) && !job.Cancel && sw.ElapsedMilliseconds < 2000 + i * 250; i++) Thread.Sleep(250);
				if (!Ran(s))
				{
					Log(job.Id + " A1 produced nothing (state=" + s.State + "); trying A2");
					// Unpublish AND mark TearingDown together, before tearing down: Terminate() reads both under jobGate
					// and its queued work item re-checks both right before SetState, so nothing else can touch this
					// instance once TearingDown is true, even from a Terminate() call already in flight.
					lock (jobGate) { job.Strat = null; job.TearingDown = true; }
					TearDown(s); s = null;
					if (job.Cancel) return;
					s = Configure(job);
					lock (jobGate) { job.Strat = s; job.TearingDown = false; }
					if (job.Cancel) return;
					RunA2(job, s);
				}
				if (job.Cancel || job.State != "running") return;
				Backtest_Window(job, s);
				job.ResultJson = Snapshot(job, s);
			}
			finally
			{
				OutputHub.Unregister(OnOutput);
				if (s != null)
				{
					lock (jobGate) { job.Strat = null; job.TearingDown = true; }		// same: clear + flag before TearDown so Terminate() can't race the finalize
					TearDown(s);
				}
			}
		}

		/// <summary>A Connected Playback connection caps historical data at the replay clock, so RunBacktest()
		/// silently loads a window that ENDS there. The engine's bodies are stripped in the reference,
		/// so the load cannot be steered; the document says what was loaded and warns instead.</summary>
		private static void Backtest_Window(Job job, StrategyBase s)
		{
			try
			{
				var b = s.BarsArray != null && s.BarsArray.Length > 0 ? s.BarsArray[0] : null;
				if (b != null && b.Count > 0) { job.BarsFrom = b.GetTime(0); job.BarsTo = b.GetTime(b.Count - 1); }
			}
			catch (Exception ex) { Log(job.Id + " bars window: " + Deep(ex)); }

			DateTime? clock = null;
			try { if (ConnSnapshot().Any(c => IsConnected(c) && c.Options != null && c.Options.Provider == Provider.Playback)) clock = Playback_Clock(); }
			catch (Exception ex) { Log(job.Id + " playback check: " + Deep(ex)); }
			// ponytail: NowEst is US Eastern, job.To is NT8-local; equal on this machine. Convert if NT8 ever runs in another zone.
			bool capped = clock.HasValue && job.To > clock.Value;
			bool known = job.BarsFrom != DateTime.MinValue;
			bool outside = known && (job.BarsTo < job.From || job.BarsFrom > job.To);
			if (!capped && !outside) return;
			string w = known
				? "the numbers in this document are for the bars really loaded, " + job.BarsFrom.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture) + ".." + job.BarsTo.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture)
					+ ", NOT for the requested from..to"
				: "the loaded bar window is unknown and may not be the requested from..to";
			if (clock.HasValue) w += ": a Connected Playback connection caps historical data at the replay clock " + clock.Value.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture);
			job.WarningsJson = Arr(new[] { Q(w) });
			Log(job.Id + " WARNING " + w);
		}

		private static bool Ran(StrategyBase s) { return s.SystemPerformance != null || Cnt(s.Executions) > 0; }
		private static int Cnt(System.Collections.ICollection c) { try { return c == null ? 0 : c.Count; } catch { return 0; } }

		/// <summary>Recipe §2.1 steps 4-8 on the worker thread. Stripped setters are read back; the three that matter
		/// throw, so a silently rejected setting is an error, not a wrong result.</summary>
		private static StrategyBase Configure(Job job)
		{
			var s = NewStrategy(job.Type);
			try
			{
				Log(job.Id + " fresh " + job.Type.Name + " state=" + s.State);
				var srb = s as Gui.NinjaScript.StrategyRenderBase;
				if (srb != null) srb.IsInStrategyAnalyzer = true;				// what the Strategy Analyzer sets; keeps the render layer off a null chart

				foreach (var kv in job.Inputs) kv.Key.SetValue(s, kv.Value, null);

				var th = job.TradingHours ?? job.Instrument.MasterInstrument.TradingHours ?? Data.TradingHours.Get("Default 24 x 7");
				s.Instruments				= new[] { job.Instrument };		// the four things NinjaScriptBase.Setup() asserts on
				s.BarsPeriods				= new[] { job.BarsPeriod };
				s.IsTickReplays				= new bool?[] { job.TickReplay };
				s.IsResetOnNewTradingDays	= new bool?[] { job.ResetDay };
				s.TradingHoursArray			= new[] { th };
				s.Instrument				= job.Instrument;				// facades over [BarsInProgress]; harmless twice
				s.BarsPeriod				= job.BarsPeriod;
				s.TradingHours				= th;
				s.TradingHoursInstance		= th;
				s.IsTickReplay				= job.TickReplay;
				s.From						= job.From;
				s.To						= job.To;

				s.Account	= job.Account;										// resolved on the UI thread, Name == BackTestAccountName
				s.Category	= Category.Backtest;
				s.IncludeTradeHistoryInBacktest = job.Settings.IncludeTradeHistory ?? true;	// false => AllTrades is empty
				s.PrintTo	= PrintTo.OutputTab2;								// so captured Print() is separable from the user's tab 1
				Backtest_ApplySettings(s, job.Settings);						// written before SetUniqueId like every other setting
				s.SetUniqueId();

				bool wantHistory = job.Settings.IncludeTradeHistory ?? true;
				if (s.IncludeTradeHistoryInBacktest != wantHistory) throw new Exception("IncludeTradeHistoryInBacktest rejected (asked " + wantHistory + ", State=" + s.State + ")");
				if (job.TickReplay && !s.IsTickReplay && s.IsTickReplays[0] != true) throw new Exception("IsTickReplay rejected (State=" + s.State + ")");
				if (s.Account == null || s.Account.Name != Account.BackTestAccountName) throw new Exception("strategy Account is not " + Account.BackTestAccountName);
				Backtest_VerifySettings(s, job.Settings);						// stripped setters: a silently rejected setting is an error, not a wrong number
				job.SettingsJson = Backtest_ReadSettings(s, job.Settings).Json();	// what the run really carries, requested or not
				Log(job.Id + " configured: account=" + s.Account.Name + " tickReplay=" + s.IsTickReplay + " th=" + (th != null ? th.Name : "null") + " from=" + Tm(s.From) + " to=" + Tm(s.To)
					+ " settings=" + job.SettingsJson);
				return s;
			}
			catch { TearDown(s); throw; }		// don't leak a partially-configured instance (or its SetUniqueId DB row)
		}

		// ── the three halves of a run setting — apply it, prove it took, report what it reads ──────────
		/// <summary>Only fields the request actually asked for are written; everything else keeps NinjaTrader's own
		/// default. Every name and range was validated on the HTTP thread, so nothing here can be a client error.</summary>
		private static void Backtest_ApplySettings(StrategyBase s, BacktestSettings w)
		{
			if (w.FillResolution != null)			s.OrderFillResolution		= (OrderFillResolution)Enum.Parse(typeof(OrderFillResolution), w.FillResolution, true);
			if (w.FillResolutionType != null)		s.OrderFillResolutionType	= (Data.BarsPeriodType)Enum.Parse(typeof(Data.BarsPeriodType), w.FillResolutionType, true);
			if (w.FillResolutionValue.HasValue)		s.OrderFillResolutionValue	= w.FillResolutionValue.Value;
			if (w.SlippageTicks.HasValue)			s.Slippage					= w.SlippageTicks.Value;
			if (w.CommissionTemplate != null)		s.BacktestCommissionTemplate= w.CommissionTemplate;		// setter stripped
			if (w.IncludeCommission.HasValue)		s.IncludeCommission			= w.IncludeCommission.Value;	// setter stripped
			if (w.FillLimitOnTouch.HasValue)		s.IsFillLimitOnTouch		= w.FillLimitOnTouch.Value;		// setter stripped
		}

		/// <summary>Read each requested setting back and throw naming the property when it did not take (recipe §2.1
		/// step 8). Three of these have stripped setters that no-op depending on State, and a silently ignored
		/// commission template or slippage produces a plausible, wrong P&amp;L that nothing else would ever flag.</summary>
		private static void Backtest_VerifySettings(StrategyBase s, BacktestSettings w)
		{
			if (w.FillResolution != null && !string.Equals(s.OrderFillResolution.ToString(), w.FillResolution, StringComparison.OrdinalIgnoreCase))
				throw new Exception("OrderFillResolution rejected (asked " + w.FillResolution + ", reads " + s.OrderFillResolution + ", State=" + s.State + ")");
			if (w.FillResolutionType != null && !string.Equals(s.OrderFillResolutionType.ToString(), w.FillResolutionType, StringComparison.OrdinalIgnoreCase))
				throw new Exception("OrderFillResolutionType rejected (asked " + w.FillResolutionType + ", reads " + s.OrderFillResolutionType + ", State=" + s.State + ")");
			if (w.FillResolutionValue.HasValue && s.OrderFillResolutionValue != w.FillResolutionValue.Value)
				throw new Exception("OrderFillResolutionValue rejected (asked " + w.FillResolutionValue.Value + ", reads " + s.OrderFillResolutionValue + ", State=" + s.State + ")");
			if (w.SlippageTicks.HasValue && Math.Abs(s.Slippage - w.SlippageTicks.Value) > 1e-9)
				throw new Exception("Slippage rejected (asked " + D(w.SlippageTicks.Value) + ", reads " + D(s.Slippage) + ", State=" + s.State + ")");
			if (w.CommissionTemplate != null && !string.Equals(s.BacktestCommissionTemplate ?? "", w.CommissionTemplate, StringComparison.OrdinalIgnoreCase))
				throw new Exception("BacktestCommissionTemplate rejected (asked '" + w.CommissionTemplate + "', reads '" + (s.BacktestCommissionTemplate ?? "") + "', State=" + s.State + ")");
			if (w.IncludeCommission.HasValue && s.IncludeCommission != w.IncludeCommission.Value)
				throw new Exception("IncludeCommission rejected (asked " + w.IncludeCommission.Value + ", State=" + s.State + ")");
			if (w.FillLimitOnTouch.HasValue && s.IsFillLimitOnTouch != w.FillLimitOnTouch.Value)
				throw new Exception("IsFillLimitOnTouch rejected (asked " + w.FillLimitOnTouch.Value + ", State=" + s.State + ")");
		}

		/// <summary>The settings the configured instance really carries — requested or NinjaTrader's own default.
		/// A result document that does not say what it ran with is a silently wrong number later.</summary>
		private static BacktestSettings Backtest_ReadSettings(StrategyBase s, BacktestSettings w)
		{
			var e = new BacktestSettings { MaxTrades = w.MaxTrades, Template = w.Template };
			try { e.FillResolution		= s.OrderFillResolution.ToString(); }		catch { }
			try { e.FillResolutionType	= s.OrderFillResolutionType.ToString(); }	catch { }
			try { e.FillResolutionValue	= s.OrderFillResolutionValue; }				catch { }
			try { e.SlippageTicks		= s.Slippage; }								catch { }
			try { e.CommissionTemplate	= s.BacktestCommissionTemplate; }			catch { }
			try { e.IncludeCommission	= s.IncludeCommission; }					catch { }
			try { e.FillLimitOnTouch	= s.IsFillLimitOnTouch; }					catch { }
			try { e.IncludeTradeHistory	= s.IncludeTradeHistoryInBacktest; }		catch { }
			return e;
		}

		/// <summary>Recipe A2: Configure, load bars with the only public loader that takes isTickReplay, install
		/// them, DataLoaded, Historical, RunBacktest().</summary>
		private static void RunA2(Job job, StrategyBase s)
		{
			s.SetState(State.Configure);
			Log(job.Id + " A2 after Configure state=" + s.State);
			if (s.State == State.Finalized) throw new Exception("A2: the engine finalized the instance at Configure");

			var progress = new NullProgress();
			Data.Bars loaded = null; ErrorCode ec = ErrorCode.NoError; string emsg = null;
			var got = new ManualResetEventSlim(false);
			Data.Bars.GetBars(job.Instrument, job.BarsPeriod, job.From, job.To, s.TradingHoursArray[0],
				false, false, job.TickReplay, job.ResetDay, LookupPolicies.Provider | LookupPolicies.Repository, MergePolicy.DoNotMerge,
				false, progress, false, null, (b, code, msg, st) => { loaded = b; ec = code; emsg = msg; got.Set(); });
			if (!got.Wait(TimeSpan.FromMinutes(10))) throw new TimeoutException("A2: bars load timed out");
			if (ec != ErrorCode.NoError || loaded == null) throw new Exception("A2: bars load failed: " + ec + " " + emsg);
			Log(job.Id + " A2 bars=" + loaded.Count + " tickReplay=" + loaded.IsTickReplay);

			var installed = new ManualResetEventSlim(false);
			s.InitializeBars(new[] { loaded }, progress, _ => installed.Set());
			if (!installed.Wait(TimeSpan.FromMinutes(2))) throw new TimeoutException("A2: InitializeBars timed out");

			s.SetState(State.DataLoaded);
			Log(job.Id + " A2 after DataLoaded state=" + s.State);
			if (s.State == State.Finalized) throw new Exception("A2: the engine finalized the instance at DataLoaded (recipe: go to B)");
			s.SetState(State.Historical);
			var sw = System.Diagnostics.Stopwatch.StartNew();
			s.RunBacktest();
			Log(job.Id + " A2 RunBacktest returned in " + sw.ElapsedMilliseconds + "ms state=" + s.State + " perf=" + (s.SystemPerformance != null) + " executions=" + Cnt(s.Executions));
			if (!Ran(s)) throw new Exception("A2: RunBacktest returned but produced no SystemPerformance and no executions (State=" + s.State + ")");
		}

		private sealed class NullProgress : Core.IProgress
		{
			public bool IsAborted { get { return false; } }
			public string Message { get; set; }
			public event EventHandler Aborted { add { } remove { } }
			public void PerformStep() { }
			public void SetUp(long maxSteps, bool isAbortable) { }
			public void TearDown() { }
		}

		/// <summary>In a finally, on the worker: Terminated, Finalized, drop any strategy-DB row. There is no Dispose.</summary>
		private static void TearDown(StrategyBase s)
		{
			try { if (s.State != State.Terminated && s.State != State.Finalized) s.SetState(State.Terminated); } catch (Exception ex) { Log("teardown Terminated: " + ex.Message); }
			try { if (s.State != State.Finalized) s.SetState(State.Finalized); } catch (Exception ex) { Log("teardown Finalized: " + ex.Message); }
			try { if (s.Id != 0) s.DbRemove(); } catch (Exception ex) { Log("teardown DbRemove: " + ex.Message); }
		}

		/// <summary>OutputHub listener, registered only while a job runs. Tab 2 is what Configure() points the strategy at.</summary>
		private static void OnOutput(OutputHub.Line line)
		{
			if (line.Reset || line.Tab != 2) return;
			lock (outGate) if (outBuf != null && outBuf.Count < 20000) outBuf.Add(line.Text);		// NT8's producer thread: never do I/O here (the hub swallows a throw)
		}

		private static int TCnt(TradeCollection c) { try { return c == null ? 0 : c.TradesCount; } catch { return 0; } }
		private static double Avg(TradeCollection c) { try { return c == null ? double.NaN : c.TradesPerformance.Currency.AverageProfit; } catch { return double.NaN; } }

		/// <summary>Results mapping (recipe §3), built BEFORE teardown: AllTrades may be gone after Finalized.</summary>
		private static string Snapshot(Job job, StrategyBase s)
		{
			var perf = s.SystemPerformance ?? SystemPerformance.Calculate(s.Executions);
			var all = perf.AllTrades;
			var tp = all.TradesPerformance; var cur = tp.Currency;
			int n = all.TradesCount, w = TCnt(all.WinningTrades), l = TCnt(all.LosingTrades);
			string summary = Obj(
				P("trades", I(n)), P("winners", I(w)), P("losers", I(l)), P("winRate", D(n == 0 ? 0 : (double)w / n)),
				P("netProfit", D(tp.NetProfit)), P("grossProfit", D(tp.GrossProfit)), P("grossLoss", D(tp.GrossLoss)), P("profitFactor", D(tp.ProfitFactor)),
				P("commission", D(tp.TotalCommission)), P("maxDrawdown", D(cur.Drawdown)), P("avgTrade", D(cur.AverageProfit)),
				P("avgWinner", D(Avg(all.WinningTrades))), P("avgLoser", D(Avg(all.LosingTrades))),
				P("largestWinner", D(cur.LargestWinner)), P("largestLoser", D(cur.LargestLoser)), P("avgMae", D(cur.AverageMae)), P("avgMfe", D(cur.AverageMfe)),
				P("avgBarsInTrade", D(tp.AverageBarsInTrade)), P("sharpe", D(tp.SharpeRatio)),
				P("maxConsecWinners", I(tp.MaxConsecutiveWinner)), P("maxConsecLosers", I(tp.MaxConsecutiveLoser)));

			var trades = new List<string>();
			int cap = job.Settings.MaxTrades;		// maxTrades: trims trades[] ONLY — `summary` always counts every trade
			foreach (Trade t in all)
			{
				if (cap > 0 && trades.Count >= cap) break;
				try
				{
					var en = t.Entry; var ex = t.Exit;
					int bars = en != null && ex != null ? ex.BarIndex - en.BarIndex : 0;
					trades.Add(Obj(
						P("n", I(t.TradeNumber)),
						P("side", Q(en != null ? en.MarketPosition.ToString() : "")),
						P("qty", I(t.Quantity)),
						P("entryName", Q(en != null ? en.Name : null)), P("exitName", Q(ex != null ? ex.Name : null)),
						P("entryTime", Tm(en != null ? en.Time : DateTime.MinValue)), P("exitTime", Tm(ex != null ? ex.Time : DateTime.MinValue)),
						P("entryPrice", D(en != null ? en.Price : double.NaN)), P("exitPrice", D(ex != null ? ex.Price : double.NaN)),
						P("pnl", D(t.ProfitCurrency)), P("pnlPoints", D(t.ProfitPoints)),
						P("mae", D(t.MaePoints)), P("mfe", D(t.MfePoints)),
						P("bars", en != null && ex != null && (en.BarIndex != 0 || ex.BarIndex != 0) ? I(bars) : "null")));	// BarIndex all-zero => unknown, not 0
				}
				catch (Exception e) { trades.Add(Obj(P("error", Q(e.Message)))); }
			}
			List<string> outp;
			lock (outGate) { outp = outBuf ?? new List<string>(); outBuf = null; }
			return P("summary", summary) + "," + P("trades", Arr(trades)) + "," + P("output", Arr(outp.Select(Q)));
		}
	}
}
