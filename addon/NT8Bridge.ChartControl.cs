// NT8Bridge.ChartControl.cs — chart control: add/remove an indicator, change the primary series,
// scroll to a time. The write -> compile -> put on chart -> look -> fix loop.
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md "Module seams").
//
// This module never touches an account: no arming file, no provider check, no audit log. The one
// guard that matters here is the chart itself — refuse while a modal dialog is standing over it
// (StandingModal(), core), and refuse a series change while the chart carries an ENABLED strategy
// (a running strategy mid-series-swap is how a chart ends up in a state nothing here can fix).
//
// Three of the four moves this file makes go through ChartControl members that are `internal`, not
// `public` — NinjaTrader.Custom carries no [InternalsVisibleTo] from NinjaTrader.Gui, so they are
// only reachable by reflection. Each is resolved ONCE, in Start_ChartControl, the same
// Compat.Resolve pattern Start_Charts uses for the reload menu item. When a resolve comes back
// null the endpoint that needs it refuses cleanly, before it changes anything, naming the member
// that did not resolve — see GET /compat. The decompile in .ref/nt8src carries real signatures for
// every member this file calls (ChartControl.Indicators/BarsPeriod/BarsArray/BarsPropertiesCollection,
// ChartBars.Properties/FromIndex/ToIndex/GetBarIdxByTime, BarsProperties.Instrument/BarsPeriod,
// StrategyRenderBase.IsEnabled), but NinjaTrader does not document the *method bodies* behind
// RefreshIndicators/RefreshBars/RemoveIndicator — what they do with the arguments below was
// inferred from their signatures and from the public NinjaScript state machine (SetState /
// State.SetDefaults etc., documented NinjaScript API), then confirmed by the observed behavior
// below.
//
// Observed on NinjaTrader 8.1.8.2: indicator add only succeeds when the new indicator instance is
// given the chart's primary ChartBars, SetInput(bars) is called on it, and its Owner is set — in
// that order, all three before RefreshIndicators runs; skipping any one throws a
// NullReferenceException out of RefreshIndicators, and the add handler below catches that, removes
// the half-added indicator, and refreshes again so the chart is unchanged. Indicator remove works;
// removing an indicator this module did not add is refused unless "force":true is sent. /series and
// /scroll are not yet exercised on a live platform — see docs/api/chartcontrol.md for the by-hand
// steps to check all of this.

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Reflection;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		/// <summary>Seam (NOTES.md "Module seams"): the four /chart/{id}/... write paths this module owns.
		/// Null for everything else — GET /chart/{id}/... and POST /chart/{id}/{reload,screenshot} stay
		/// Route_Charts', "indicators" (list) is not "indicator" (singular, this module's prefix).</summary>
		private static string Route_ChartControl(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (method != "POST" || seg.Length < 3 || seg[0] != "chart") return null;
			bool mine = (seg.Length == 4 && seg[2] == "indicator" && (seg[3] == "add" || seg[3] == "remove"))
					 || (seg.Length == 3 && (seg[2] == "series" || seg[2] == "scroll"));
			if (!mine) return null;

			Gui.Chart.Chart chart;
			try { chart = FindChart(seg[1]); }
			catch (BadRequestException ex) { status = 404; return Obj(P("error", Q(ex.Message))); }

			// Never touch a chart while a modal dialog is up — checked before the body is even parsed.
			string modal = StandingModal();
			if (modal != null)
			{
				status = 409;
				return Obj(P("error", Q("refused: a modal dialog is open (" + modal + ")")), P("standingModal", Q(modal)));
			}

			try
			{
				if (seg.Length == 4) return seg[3] == "add" ? ChartCtl_IndicatorAdd(chart, body) : ChartCtl_IndicatorRemove(chart, body);
				return seg[2] == "series" ? ChartCtl_Series(chart, body) : ChartCtl_Scroll(chart, body);
			}
			catch (TimeoutException) { throw; }		// -> the core's 504; Handle() maps it, never a 400/500 here
			catch (BadRequestException ex) { return Err(ref status, 400, ex.Message); }
		}

		// ── reflection table, resolved once (NOTES.md "Compat.Resolve") ─────────
		private static MethodInfo CC_RefreshIndicators;	// internal void RefreshIndicators(bool redistributeIndicators = true, bool callInitializeBars = true)
		private static MethodInfo CC_RemoveIndicator;		// private int RemoveIndicator(IndicatorRenderBase ib) — preferred when it resolves, over Indicators.Remove
		private static MethodInfo CC_RefreshBars;			// internal void RefreshBars(IEnumerable<BarsProperties> added, IEnumerable<BarsProperties> changed, ChartObjectCollection<BarsProperties> removed, ChartObjectCollection<BarsProperties> final, bool force, bool allowRecalculateRatios, bool redistributeIndicators = true, bool callInitializeBars = true)

		/// <summary>Seam: runs before the first request is served. No event subscription anywhere in this
		/// module, so there is no Stop_ChartControl.</summary>
		private static void Start_ChartControl()
		{
			CC_RefreshIndicators = Compat.Resolve("ChartControl.RefreshIndicators",
				() => typeof(ChartControl).GetMethod("RefreshIndicators", BindingFlags.Instance | BindingFlags.NonPublic)) as MethodInfo;
			CC_RemoveIndicator = Compat.Resolve("ChartControl.RemoveIndicator",
				() => typeof(ChartControl).GetMethod("RemoveIndicator", BindingFlags.Instance | BindingFlags.NonPublic)) as MethodInfo;
			CC_RefreshBars = Compat.Resolve("ChartControl.RefreshBars",
				() => typeof(ChartControl).GetMethod("RefreshBars", BindingFlags.Instance | BindingFlags.NonPublic)) as MethodInfo;
		}

		/// <summary>Indicator instances this module added, by reference identity — never by Name (a user's
		/// own indicator can share a display name) and never by a bound holding every indicator forever
		/// (an indicator removed by the user directly falls out of this table the moment it is garbage
		/// collected, since a ConditionalWeakTable holds no strong reference to its key).</summary>
		private static readonly System.Runtime.CompilerServices.ConditionalWeakTable<Gui.NinjaScript.IndicatorRenderBase, object> ChartCtl_Added
			= new System.Runtime.CompilerServices.ConditionalWeakTable<Gui.NinjaScript.IndicatorRenderBase, object>();

		/// <summary>An indicator TYPE by short or full name, restricted to this AddOn's own assembly — the
		/// same restriction Backtest.cs's StrategyTypes() applies, and for the same reason: every indicator
		/// NinjaTrader ships or a user compiles lands in NinjaTrader.Custom (this assembly), so this also
		/// covers built-in indicators, not only custom ones.</summary>
		private static Type ChartCtl_FindIndicatorType(string name)
		{
			var mine = typeof(NT8Bridge).Assembly;
			return Core.Globals.AssemblyRegistry.GetDerivedTypes(typeof(Gui.NinjaScript.IndicatorRenderBase), false)
				.FirstOrDefault(t => t != null && !t.IsAbstract && t.Assembly == mine && (t.Name == name || t.FullName == name));
		}

		// ── POST /chart/{id}/indicator/add {indicator, inputs{}, panel?} ────────
		private static string ChartCtl_IndicatorAdd(Gui.Chart.Chart chart, string body)
		{
			var req = ParseJson(body) as Dictionary<string, object>;
			string name = JGetStr(req, "indicator", null);
			if (string.IsNullOrEmpty(name)) throw new BadRequestException("indicator is required");
			int panelReq = JGetInt(req, "panel", -1);
			var inputs = JGetMap(req, "inputs") ?? new Dictionary<string, object>();

			var t = ChartCtl_FindIndicatorType(name);
			if (t == null) throw new BadRequestException("unknown indicator '" + name + "' — see GET /chart/{id}/indicators for what is already on a chart, or /compile for what this AddOn's assembly holds");

			Gui.NinjaScript.IndicatorRenderBase ind;
			try { ind = t.Assembly.CreateInstance(t.FullName) as Gui.NinjaScript.IndicatorRenderBase; }
			catch (Exception ex) { throw new BadRequestException("could not construct " + t.Name + ": " + Deep(ex)); }
			if (ind == null) throw new BadRequestException("'" + name + "' (" + t.FullName + ") is not a chart indicator");

			ind.SetState(State.SetDefaults);
			foreach (var kv in inputs)
			{
				PropertyInfo pi = null;
				try { pi = t.GetProperty(kv.Key, BindingFlags.Public | BindingFlags.Instance | BindingFlags.IgnoreCase); } catch { }
				if (pi == null || !IsInput(pi) || !pi.CanWrite) throw new BadRequestException("unknown input '" + kv.Key + "' for " + t.Name);
				try { pi.SetValue(ind, Coerce(kv.Value, pi.PropertyType, pi.Name), null); }
				catch (Exception ex) { throw new BadRequestException(ex.Message); }
			}
			if (panelReq >= 0) ind.Panel = panelReq;

			return OnChart(chart, cc =>
			{
				if (CC_RefreshIndicators == null)
					throw new BadRequestException("cannot add an indicator: ChartControl.RefreshIndicators did not resolve at startup (see /compat) — nothing changed");
				// An indicator with no bars to read makes RefreshIndicators throw (observed on 8.1.8.2):
				// give it the chart's primary series first, the way the Indicators dialog does.
				var cb = cc.PrimaryBars ?? (cc.BarsArray != null && cc.BarsArray.Count > 0 ? cc.BarsArray[0] : null);
				if (cb == null || cb.Bars == null)
					throw new BadRequestException("cannot add an indicator: this chart has no loaded bars series — nothing changed");
				ind.ChartBars = cb;
				ind.SetInput(cb.Bars);
				ind.Owner = chart;
				cc.Indicators.Add(ind);
				try { CC_RefreshIndicators.Invoke(cc, new object[] { true, true }); }
				catch
				{
					// Never leave a half-added indicator on the user's chart.
					try { cc.Indicators.Remove(ind); CC_RefreshIndicators.Invoke(cc, new object[] { true, false }); } catch { }
					throw;
				}
				ChartCtl_Added.Remove(ind);		// .NET Framework's ConditionalWeakTable has no AddOrUpdate; Add() throws on a duplicate key
				ChartCtl_Added.Add(ind, null);
				return ChartCtl_ReportBody(cc);
			});
		}

		// ── POST /chart/{id}/indicator/remove {indicator | index, force?} ───────
		private static string ChartCtl_IndicatorRemove(Gui.Chart.Chart chart, string body)
		{
			var req = ParseJson(body) as Dictionary<string, object>;
			string name = JGetStr(req, "indicator", null);
			int index = JGetInt(req, "index", -1);
			bool force = JGetBool(req, "force", false);
			if (string.IsNullOrEmpty(name) && index < 0) throw new BadRequestException("indicator or index is required");

			return OnChart(chart, cc =>
			{
				Gui.NinjaScript.IndicatorRenderBase target = null;
				if (!string.IsNullOrEmpty(name))
				{
					if (cc.Indicators != null)
						foreach (var ind in cc.Indicators) if (ind != null && NameOf(ind) == name) { target = ind; break; }
					if (target == null) throw new BadRequestException("no indicator '" + name + "' on this chart");
				}
				else
				{
					if (cc.Indicators == null || index >= cc.Indicators.Count) throw new BadRequestException("index " + index + " is out of range (" + (cc.Indicators == null ? 0 : cc.Indicators.Count) + " indicators)");
					target = cc.Indicators[index];
				}

				object ignore;
				bool owned = ChartCtl_Added.TryGetValue(target, out ignore);
				if (!owned && !force) throw new BadRequestException("'" + NameOf(target) + "' was not added by this module — pass \"force\":true to remove it anyway");

				if (CC_RemoveIndicator == null && CC_RefreshIndicators == null)
					throw new BadRequestException("cannot remove an indicator: neither ChartControl.RemoveIndicator nor RefreshIndicators resolved at startup (see /compat) — nothing changed");
				if (CC_RemoveIndicator != null) CC_RemoveIndicator.Invoke(cc, new object[] { target });
				else
				{
					cc.Indicators.Remove(target);
					CC_RefreshIndicators.Invoke(cc, new object[] { true, true });
				}
				ChartCtl_Added.Remove(target);
				return ChartCtl_ReportBody(cc);
			});
		}

		// ── POST /chart/{id}/series {instrument?, barsPeriod?{type,value,value2,baseType,baseValue}} | {restore:true} ──
		/// <summary>What the chart showed before this module first changed it, keyed by chart. The
		/// ORIGINAL BarsPeriod object is kept, not a copy: a third-party bar type can carry settings no
		/// request body can express, and only the same object puts all of them back. In memory only: a
		/// NinjaScript reload forgets it.</summary>
		private sealed class ChartCtl_Orig { public string Instrument; public BarsPeriod Period; }
		private static readonly System.Runtime.CompilerServices.ConditionalWeakTable<ChartControl, ChartCtl_Orig> ChartCtl_Originals =
			new System.Runtime.CompilerServices.ConditionalWeakTable<ChartControl, ChartCtl_Orig>();

		private static string ChartCtl_Series(Gui.Chart.Chart chart, string body)
		{
			var req = ParseJson(body) as Dictionary<string, object>;
			bool restore = false;
			try { object r = JGet(req, "restore"); restore = r is bool && (bool)r; } catch { }
			string instrName = JGetStr(req, "instrument", null);
			bool wantPeriod = JGet(req, "barsPeriod") != null;
			if (!restore && string.IsNullOrEmpty(instrName) && !wantPeriod)
				throw new BadRequestException("instrument and/or barsPeriod is required, or {\"restore\":true}");

			Instrument inst = null;
			if (!restore && !string.IsNullOrEmpty(instrName))
			{
				try { inst = Instrument.GetInstrument(instrName); } catch (Exception ex) { throw new BadRequestException("instrument '" + instrName + "': " + ex.Message); }
				if (inst == null) throw new BadRequestException("unknown instrument '" + instrName + "'");
			}
			BarsPeriod newPeriod = (!restore && wantPeriod) ? Sr_Period(req) : null;		// the one full parser (custom bar types by number)

			OnChart(chart, cc =>
			{
				if (cc.Strategies != null && cc.Strategies.Any(s => s != null && s.IsEnabled))
					throw new BadRequestException("refused: this chart has an enabled strategy attached — stop it first");
				if (CC_RefreshBars == null)
					throw new BadRequestException("cannot change the series: ChartControl.RefreshBars did not resolve at startup (see /compat) — nothing changed");

				var cb = Primary(cc);
				ChartCtl_Orig orig;
				bool have = ChartCtl_Originals.TryGetValue(cc, out orig);
				string wasInstrument = cb.Properties.Instrument;
				BarsPeriod wasPeriod = cb.Properties.BarsPeriod;
				if (restore)
				{
					if (!have) throw new BadRequestException("nothing to restore: this module has not changed this chart's series since the AddOn was loaded");
					cb.Properties.Instrument = orig.Instrument;
					cb.Properties.BarsPeriod = orig.Period;
					ChartCtl_Originals.Remove(cc);
				}
				else
				{
					if (!have)
						ChartCtl_Originals.Add(cc, new ChartCtl_Orig { Instrument = cb.Properties.Instrument, Period = cb.Properties.BarsPeriod });
					if (inst != null) cb.Properties.Instrument = inst.FullName;
					if (newPeriod != null) cb.Properties.BarsPeriod = newPeriod;
				}

				// RefreshBars enumerates `added` and `removed`: they must be EMPTY, never null (observed on 8.1.8.2).
				try
				{
					object removed = Activator.CreateInstance(CC_RefreshBars.GetParameters()[2].ParameterType);
					CC_RefreshBars.Invoke(cc, new object[] { new BarsProperties[0], new[] { cb.Properties }, removed, cc.BarsPropertiesCollection, true, true, true, true });
				}
				catch
				{
					// Leave nothing half-set on the user's chart.
					cb.Properties.Instrument = wasInstrument;
					cb.Properties.BarsPeriod = wasPeriod;
					if (restore && orig != null) { try { ChartCtl_Originals.Add(cc, orig); } catch { } }
					else if (!have) ChartCtl_Originals.Remove(cc);
					throw;
				}
				return "";
			});

			// The chart reloads its bars AFTER RefreshBars returns (observed on 8.1.8.2): a report taken at
			// once still shows the old series. Re-read, off the dispatcher between tries, until the loaded
			// bars carry the period the chart was asked for, or about 8 s have passed.
			string report = null;
			for (int i = 0; i < 8; i++)
			{
				Thread.Sleep(1000);
				bool settled = false;
				report = OnChart(chart, cc =>
				{
					var cb = Primary(cc);
					try { settled = cb.Bars != null && cb.Bars.BarsPeriod != null && cb.Properties.BarsPeriod != null
						&& cb.Bars.BarsPeriod.ToString() == cb.Properties.BarsPeriod.ToString(); } catch { }
					return ChartCtl_ReportBody(cc);
				});
				if (settled) break;
			}
			return report;
		}

		// ── POST /chart/{id}/scroll {time} ───────────────────────────────────────
		private static string ChartCtl_Scroll(Gui.Chart.Chart chart, string body)
		{
			var req = ParseJson(body) as Dictionary<string, object>;
			string timeStr = JGetStr(req, "time", null);
			DateTime time;
			if (string.IsNullOrEmpty(timeStr) || !DateTime.TryParse(timeStr, CultureInfo.InvariantCulture, DateTimeStyles.None, out time))
				throw new BadRequestException("time: missing or bad date/time");

			return OnChart(chart, cc =>
			{
				var cb = Primary(cc);
				var bars = cb.Bars;
				if (bars == null || bars.Count == 0) throw new BadRequestException("chart has no bars yet");

				// Writing ChartBars.FromIndex / ToIndex does not move the view (observed on 8.1.8.2): the
				// chart's own scroll entry point does. It is not public, so it is looked up by name.
				var mi = typeof(ChartControl).GetMethod("ScrollToTime", BindingFlags.Instance | BindingFlags.NonPublic,
					null, new[] { typeof(DateTime), typeof(bool) }, null);
				if (mi == null)
					throw new BadRequestException("cannot scroll: ChartControl.ScrollToTime(DateTime, bool) was not found on this NinjaTrader build — nothing changed");
				mi.Invoke(cc, new object[] { time, false });
				cc.InvalidateVisual();

				return ChartCtl_ReportBody(cc);
			});
		}

		// ── the read-back every write reports (NOTES.md "shared by modules" style) ──
		/// <summary>Called already on cc's own dispatcher thread — no OnChart hop of its own. Reuses
		/// Charts.cs' IndicatorJson/PanelOf/DrawingsByOwner/NameOf/Primary/InstrumentName/Period/TimeAt so
		/// this module's read-back can never drift from what GET /chart/{id}/indicators reports.</summary>
		private static string ChartCtl_ReportBody(ChartControl cc)
		{
			var cb = Primary(cc);
			var drawCounts = DrawingsByOwner(cc);
			var items = new List<string>();
			if (cc.Indicators != null)
				foreach (var ind in cc.Indicators)
				{
					if (ind == null) continue;
					string name = NameOf(ind);
					int drawn;
					drawCounts.TryGetValue(name, out drawn);
					if (ind.State != State.Realtime && ind.State != State.Historical) { items.Add(Obj(P("name", Q(name)), P("error", Q("indicator state is " + ind.State)))); continue; }
					if (ind.Bars == null) { items.Add(Obj(P("name", Q(name)), P("error", Q("indicator has no bars")))); continue; }
					try { items.Add(IndicatorJson(ind, name, PanelOf(cc, ind), 1, drawn)); }
					catch (Exception ex) { items.Add(Obj(P("name", Q(name)), P("error", Q(ex.Message)))); }
				}
			return Obj(
				P("instrument", Q(InstrumentName(cb))),
				P("period", Q(Period(cb))),
				P("firstVisibleTime", TimeAt(cb.Bars, cb.FromIndex)),
				P("lastVisibleTime", TimeAt(cb.Bars, cb.ToIndex)),
				P("indicators", Arr(items)));
		}
	}
}
