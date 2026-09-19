// NT8Bridge.Account.cs — account reads (READ ONLY): /account, /executions, /performance.
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md "Module seams").
//
// Nothing in this file submits, modifies or cancels an order, opens a window, or writes
// anything. Execution.DbGet is a SELECT against NinjaTrader's own trade database.
//
// Portions of this file are derived from cli-nt-bridge
// (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
// MIT License. The full notice is in NOTICE at the repository root.

#region Using declarations
using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Linq;
using System.Windows;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		// ── account (read only) ────────────────────────────────────────────────
		// CancelSubmitted belongs here — the order is still live at the broker and used
		// to be invisible in /account. (cli-nt-bridge's whitelist, addon/NT8BridgeServer.cs:3932.)
		private static readonly OrderState[] WorkingStates =
		{
			OrderState.Accepted, OrderState.Working, OrderState.PartFilled, OrderState.TriggerPending,
			OrderState.ChangePending, OrderState.ChangeSubmitted, OrderState.Submitted,
			OrderState.CancelPending, OrderState.CancelSubmitted
		};

		/// <summary>Trades whose exit lands before `from` can still be half-open at `from`, so the DB query
		/// starts this many days early. The pad is filtered back out by Exit.Time and reported as padTrimmed.</summary>
		private const int Acct_PadDays = 2;

		/// <summary>Hard ceiling on rows in one response (cli-nt-bridge: 5000). Over it, `capped` is true.</summary>
		private const int Acct_MaxRows = 5000;

		/// <summary>Hard ceiling on the from/to window in days. `Acct_MaxRows` only trims the RESPONSE
		/// after Execution.DbGet has already materialized the whole range (a year-wide query on a busy
		/// account is hundreds of MB and a multi-minute call inside NinjaTrader's own process) — this
		/// bounds the QUERY. Refused with 400, not silently truncated, so a caller pages with from/to.</summary>
		private const int Acct_MaxRangeDays = 366;

		/// <summary>Seam (NOTES.md "Module seams"): GET /account, /executions, /performance. Null for
		/// everything else — including POST/DELETE on these same paths, so only the core emits the 404.
		/// One handler-wide guard, so a failed read is a structured error, never a dead thread.
		/// TimeoutException is re-thrown on purpose: the core maps it to 504, and a 500 here would hide
		/// "the UI thread never answered" behind "something went wrong".</summary>
		private static string Route_Account(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (method != "GET" || seg.Length != 1) return null;
			if (seg[0] != "account" && seg[0] != "executions" && seg[0] != "performance") return null;
			try
			{
				if (seg[0] == "account") return AccountJson(q["name"]);
				if (seg[0] == "executions") return Acct_ExecutionsJson(q);
				return Acct_PerformanceJson(q);
			}
			catch (TimeoutException) { throw; }									// core -> 504
			catch (BadRequestException ex) { return Err(ref status, 400, ex.Message); }
			catch (Acct_NotFoundException ex) { return Err(ref status, 404, ex.Message); }
			catch (Exception ex)
			{
				Log("/" + seg[0] + ": " + Deep(ex));
				return Err(ref status, 500, Deep(ex));
			}
		}

		private sealed class Acct_NotFoundException : Exception { public Acct_NotFoundException(string msg) : base(msg) { } }

		// ── GET /account ───────────────────────────────────────────────────────

		/// <summary>The name match is OrdinalIgnoreCase, deliberately — cli-nt-bridge's is exact `==`.
		/// NinjaTrader account names are user-typed and case is not meaningful in them, so "sim101" and
		/// "Sim101" are the same account here. An unknown name is a 404, not an empty list: "no such
		/// account" and "that account is flat" are different claims.</summary>
		private static string AccountJson(string name)
		{
			// Snapshot under the lock, build JSON outside it (cli-nt-bridge's lesson): Account.All is a live
			// Cbi collection and the JSON build calls back into NinjaTrader per position.
			Account[] all = Ui(Application.Current.Dispatcher, () =>
			{
				lock (Account.All) return Account.All.ToArray();
			});

			if (!string.IsNullOrEmpty(name))
			{
				Account single = all.FirstOrDefault(a => string.Equals(Acct_Name(a), name, StringComparison.OrdinalIgnoreCase));
				if (single == null) throw new Acct_NotFoundException("no account '" + name + "' — call GET /account for the list");
				return Ui(Application.Current.Dispatcher, () => AccountOne(single));
			}
			return Ui(Application.Current.Dispatcher, () => Arr(all.Select(AccountOne).ToList()));
		}

		private static string Acct_Name(Account a) { try { return a.Name; } catch { return null; } }

		private static string AccountOne(Account a)
		{
			var positions = new List<string>();
			try
			{
				// Same lesson as Acct_Fetch below: Positions/Orders are live Cbi collections mutated
				// by NinjaTrader's connection/adapter threads. Snapshot under the lock, build JSON outside it.
				Position[] pos;
				lock (a.Positions) pos = a.Positions.ToArray();
				foreach (var p in pos)
				{
					// One failed position degrades to an error row; it never zeroes the others
					// and never kills the list.
					try
					{
						positions.Add(Obj(
							P("instrument", Q(Acct_Sv(() => p.Instrument.FullName))),
							P("qty", D(p.MarketPosition == MarketPosition.Short ? -p.Quantity : p.Quantity)),
							P("avg", D(p.AveragePrice)),
							// The price argument defaults to double.MinValue, i.e. "use the current
							// market price" — no MarketData plumbing needed. A failed read is null, never 0.
							P("unrealized", D(Acct_Dv(() => p.GetUnrealizedProfitLoss(PerformanceUnit.Currency)))),
							// Has this instrument ever ticked in this session? `unrealized` computed off a
							// stale or absent price is not an error, but it is not a mark-to-market either.
							P("hasSeenMarketData", Acct_Bv(() => p.Instrument.HasSeenMarketData))));
					}
					catch (Exception ex) { positions.Add(Obj(P("error", Q(Deep(ex))))); }
				}
			}
			catch (Exception ex) { positions.Add(Obj(P("error", Q(Deep(ex))))); }

			var orders = new List<string>();
			try
			{
				Order[] ords;
				lock (a.Orders) ords = a.Orders.ToArray();
				foreach (var o in ords)
				{
					try
					{
						if (Array.IndexOf(WorkingStates, o.OrderState) < 0) continue;
						orders.Add(Obj(
							P("id", Q(o.OrderId)),
							P("instrument", Q(Acct_Sv(() => o.Instrument.FullName))),
							P("action", Q(o.OrderAction.ToString())),
							P("type", Q(o.OrderType.ToString())),
							P("qty", D(o.Quantity)),
							P("price", D(o.OrderType == OrderType.Limit || o.OrderType == OrderType.StopLimit ? o.LimitPrice : o.StopPrice)),
							P("state", Q(o.OrderState.ToString()))));
					}
					catch (Exception ex) { orders.Add(Obj(P("error", Q(Deep(ex))))); }
				}
			}
			catch (Exception ex) { orders.Add(Obj(P("error", Q(Deep(ex))))); }

			return Obj(
				P("name", Q(Acct_Name(a))),
				P("cash", D(Money(a, AccountItem.CashValue))),
				P("realized", D(Money(a, AccountItem.RealizedProfitLoss))),
				P("unrealized", D(Money(a, AccountItem.UnrealizedProfitLoss))),
				P("positions", Arr(positions)),
				P("orders", Arr(orders)));
		}

		private static double Money(Account a, AccountItem item)
		{
			try { return a.Get(item, Currency.UsDollar); } catch { return double.NaN; }
		}

		// SafeStr/SafeNum semantics — a failed read is JSON null, never 0 and never "".
		private static double Acct_Dv(Func<double> f) { try { return f(); } catch { return double.NaN; } }
		private static string Acct_Sv(Func<string> f) { try { return f(); } catch { return null; } }
		private static string Acct_Bv(Func<bool> f) { try { return f() ? "true" : "false"; } catch { return "null"; } }
		private static long Acct_Lv(Func<long> f) { try { return f(); } catch { return 0; } }
		private static DateTime Acct_Tv(Func<DateTime> f) { try { return f(); } catch { return DateTime.MinValue; } }

		// ── the shared query (account + range + instrument) ─────────────

		/// <summary>Everything /executions and /performance agree on, resolved once.</summary>
		private sealed class Acct_Query
		{
			public Account Acct;
			public string AccountName, InstrumentName;
			public Instrument Instrument;
			public DateTime From, To;
			public int N;
		}

		private static Acct_Query Acct_ParseQuery(System.Collections.Specialized.NameValueCollection q, int defaultN)
		{
			var r = new Acct_Query();
			r.AccountName = (q["account"] ?? "").Trim();
			if (r.AccountName.Length == 0)
				throw new BadRequestException("account is required — a name from GET /account (e.g. account=Sim101)");

			// The Account object is the only thing that needs NinjaTrader's UI thread. Resolve it here and
			// release the thread: Execution.DbGet is a synchronous SQLite query and DB or file I/O is never
			// done while holding the UI thread's lock — a wide range would block NT's dispatcher for the whole query.
			string want = r.AccountName;
			r.Acct = Ui<Account>(Application.Current.Dispatcher, () =>
			{
				lock (Account.All)
					foreach (var a in Account.All)
						if (string.Equals(a.Name, want, StringComparison.OrdinalIgnoreCase)) return a;
				return null;
			});
			if (r.Acct == null) throw new Acct_NotFoundException("no account '" + r.AccountName + "' — call GET /account for the list");
			r.AccountName = Acct_Name(r.Acct) ?? r.AccountName;

			r.InstrumentName = (q["instrument"] ?? "").Trim();
			if (r.InstrumentName.Length > 0)
			{
				try { r.Instrument = Instrument.GetInstrument(r.InstrumentName); }
				catch (Exception ex) { throw new BadRequestException("instrument '" + r.InstrumentName + "': " + Deep(ex)); }
				if (r.Instrument == null) throw new BadRequestException("unknown instrument '" + r.InstrumentName + "'");
			}

			DateTime now = DateTime.Now;
			r.From = Acct_Date(q["from"], now.Date, "from");
			r.To = Acct_Date(q["to"], now, "to");
			if (!string.IsNullOrEmpty(q["to"]) && r.To.TimeOfDay == TimeSpan.Zero)
				r.To = r.To.Date.AddDays(1).AddSeconds(-1);						// `to` is inclusive, as /backtest treats it
			string bad = RangeProblem(r.From, r.To);							// never query an inverted or placeholder range
			if (bad != null) throw new BadRequestException(bad);
			if ((r.To - r.From).TotalDays > Acct_MaxRangeDays)
				throw new BadRequestException("range too wide (max " + Acct_MaxRangeDays +
					" days) — page with from/to instead of one wide query");

			r.N = Num(q["n"], defaultN);
			if (r.N > Acct_MaxRows) r.N = Acct_MaxRows;
			return r;
		}

		private static DateTime Acct_Date(string s, DateTime fallback, string what)
		{
			if (string.IsNullOrEmpty(s)) return fallback;
			DateTime d;
			if (!DateTime.TryParse(s, CultureInfo.InvariantCulture, DateTimeStyles.None, out d))
				throw new BadRequestException(what + ": bad date '" + s + "' (use YYYY-MM-DD)");
			return d;
		}

		/// <summary>The union of NinjaTrader's trade DB and the account's in-memory executions, deduped by
		/// ExecutionId, oldest first. `source` is "db" when the DB answered, "memory" when it did not.
		/// Runs on the HTTP thread, never inside Ui().</summary>
		private static List<Execution> Acct_Fetch(Acct_Query qq, int padDays, List<string> warnings, out string source, out int lookbackDays)
		{
			source = "db";
			lookbackDays = 3;
			try { lookbackDays = Account.LookbackDaysExecutions; } catch { }	// report the constant, never hardcode it

			DateTime padFrom = qq.From.AddDays(-padDays);

			// Execution.DbGet THROWS on a bound whose Kind is Local — NinjaTrader's trade DB is in the
			// EXCHANGE time zone, so a Local DateTime trips an internal TimeZoneInfo conversion. With no
			// `to`, `to` defaults to DateTime.Now (Local), DbGet threw, and every intraday pull silently
			// fell back to the ~3-day in-memory window: the failure looked like thin data, not an error.
			// Pass Unspecified wall-clock bounds, exactly as NT's own Trade Performance window does.
			DateTime dbFrom = DateTime.SpecifyKind(padFrom, DateTimeKind.Unspecified);
			DateTime dbTo = DateTime.SpecifyKind(qq.To, DateTimeKind.Unspecified);

			var execs = new List<Execution>();
			var seen = new HashSet<string>(StringComparer.Ordinal);
			try
			{
				// The 4-arg overload pushes the instrument filter into SQLite instead of post-filtering
				// every row of the account's history in C#.
				Collection<Execution> rows = qq.Instrument != null
					? Execution.DbGet(qq.Acct, qq.Instrument, dbFrom, dbTo)
					: Execution.DbGet(qq.Acct, dbFrom, dbTo);
				if (rows != null)
					foreach (Execution e in rows) Acct_Take(e, padFrom, qq.To, qq.Instrument, execs, seen);
			}
			catch (Exception ex)
			{
				Log("/executions DbGet: " + Deep(ex));
				source = "memory";
				warnings.Add("trade DB unavailable (" + Deep(ex) + "); limited to the ~" + lookbackDays + "-day in-memory window");
			}

			// The freshest intraday fills live only in memory. Snapshot under the collection's lock and
			// build everything else outside it (cli-nt-bridge's lesson).
			try
			{
				Execution[] mem;
				lock (qq.Acct.Executions) mem = qq.Acct.Executions.ToArray();
				foreach (Execution e in mem) Acct_Take(e, padFrom, qq.To, qq.Instrument, execs, seen);
			}
			catch (Exception ex)
			{
				Log("/executions memory union: " + Deep(ex));
				warnings.Add("in-memory executions unreadable: " + Deep(ex));
			}

			execs.Sort((a, b) => Acct_Tv(() => a.Time).CompareTo(Acct_Tv(() => b.Time)));
			return execs;
		}

		/// <summary>In-scope and not already held. A read that throws excludes the row rather than the query.</summary>
		private static void Acct_Take(Execution e, DateTime lo, DateTime hi, Instrument instr, List<Execution> into, HashSet<string> seen)
		{
			try
			{
				if (e == null) return;
				DateTime t = e.Time;
				if (t < lo || t > hi) return;
				if (instr != null && !string.Equals(Acct_Sv(() => e.Instrument.FullName), instr.FullName, StringComparison.Ordinal)) return;
				string k = Acct_Sv(() => e.ExecutionId);
				if (!string.IsNullOrEmpty(k) && !seen.Add(k)) return;
				into.Add(e);
			}
			catch { }
		}

		/// <summary>null when every instrument in the (time-sorted) set starts from a flat account, else
		/// "ES 09-26 was 50 before its first fill". Position-before = Execution.Position minus the signed
		/// quantity. Partial fills share one timestamp and come back in any order, so the first TIMESTAMP is
		/// judged, not the first row: one fill there that starts from 0 is enough. A set in which no fill
		/// carries a Position at all (a provider that does not report it) cannot be judged and says so.</summary>
		private static string Acct_NotFlatAtStart(List<Execution> execs)
		{
			if (execs.Count == 0) return null;
			if (execs.All(e => Acct_Lv(() => e.Position) == 0))
				return "cannot be verified — no fill here carries Execution.Position";
			var bad = new List<string>();
			foreach (var g in execs.GroupBy(e => Acct_Sv(() => e.Instrument.FullName) ?? "?"))
			{
				DateTime t0 = Acct_Tv(() => g.First().Time);
				long best = long.MaxValue;
				foreach (var e in g.TakeWhile(x => Acct_Tv(() => x.Time) == t0))
				{
					long signed = e.MarketPosition == MarketPosition.Short ? -e.Quantity : e.Quantity;
					long before = e.Position - signed;
					if (Math.Abs(before) < Math.Abs(best)) best = before;
				}
				if (best != 0 && best != long.MaxValue) bad.Add(g.Key + " was " + best + " before its first fill");
			}
			return bad.Count == 0 ? null : string.Join("; ", bad);
		}

		// ── GET /executions ────────────────────────────────────────────────────

		private static string Acct_ExecutionsJson(System.Collections.Specialized.NameValueCollection q)
		{
			var qq = Acct_ParseQuery(q, 200);
			var warnings = new List<string>();
			string source; int lookbackDays;
			List<Execution> execs = Acct_Fetch(qq, Acct_PadDays, warnings, out source, out lookbackDays);

			// The pad exists to pair round trips; a raw execution list has no pairing, so report only
			// what the caller asked for.
			var inRange = execs.Where(e => Acct_Tv(() => e.Time) >= qq.From).ToList();
			bool capped = inRange.Count > qq.N;
			var rows = inRange.Take(qq.N).Select(Acct_ExecutionOne).ToList();

			return Obj(
				P("account", Q(qq.AccountName)),
				P("instrument", Q(qq.InstrumentName.Length == 0 ? null : qq.InstrumentName)),
				P("from", Tm(qq.From)), P("to", Tm(qq.To)),
				P("source", Q(source)),
				P("lookbackDays", I(lookbackDays)),
				P("total", I(inRange.Count)),
				P("capped", capped ? "true" : "false"),
				P("warnings", Arr(warnings.Select(Q))),
				P("executions", Arr(rows)));
		}

		private static string Acct_ExecutionOne(Execution e)
		{
			try
			{
				MarketPosition mp = e.MarketPosition;
				return Obj(
					P("id", I(Acct_Lv(() => e.Id))),
					P("executionId", Q(Acct_Sv(() => e.ExecutionId))),
					P("account", Q(Acct_Sv(() => e.Account.Name))),
					P("instrument", Q(Acct_Sv(() => e.Instrument.FullName))),
					// An execution carries a MarketPosition, not an OrderAction: Long is the buy side.
					P("side", Q(mp == MarketPosition.Long ? "Buy" : mp == MarketPosition.Short ? "Sell" : "")),
					P("qty", I(Acct_Lv(() => e.Quantity))),
					P("price", D(Acct_Dv(() => e.Price))),
					P("time", Tm(Acct_Tv(() => e.Time))),
					// NinjaTrader never PERSISTS per-fill commission: a row that came out of the DB carries 0
					// here even on an account that is charged. /performance reconstructs it and says so.
					P("commission", D(Acct_Dv(() => e.Commission))),
					P("fee", D(Acct_Dv(() => e.Fee))),
					// The account's position in this instrument AFTER the fill, as the provider reported it.
					P("position", I(Acct_Lv(() => e.Position))),
					P("orderId", Q(Acct_Sv(() => e.OrderId))),
					P("orderName", Q(Acct_Sv(() => e.Name))));
			}
			catch (Exception ex) { return Obj(P("error", Q(Deep(ex)))); }
		}

		// ── GET /performance ───────────────────────────────────────────────────

		private static string Acct_PerformanceJson(System.Collections.Specialized.NameValueCollection q)
		{
			var qq = Acct_ParseQuery(q, Acct_MaxRows);
			var warnings = new List<string>();
			string source; int lookbackDays;
			// Pairing is only right when the account was FLAT at the start of the padded window. A position
			// opened before the pad is invisible, so its closing fills pair as NEW entries and every later
			// trade in that instrument shifts (observed effect: a Friday-to-Tuesday hold over the 2-day
			// pad turned 55 trades / +24583 into 27 trades / +825, silently). Execution.Position — the
			// account's position after the fill, as the provider reported it — gives the position before the
			// first fill, so widen the pad until that is 0, and say so when it never is.
			int padDays = Acct_PadDays;
			List<Execution> execs;
			string notFlat;
			while (true)
			{
				var w = new List<string>();
				execs = Acct_Fetch(qq, padDays, w, out source, out lookbackDays);
				notFlat = Acct_NotFlatAtStart(execs);
				int next = padDays < 7 ? 7 : padDays < 30 ? 30 : padDays < 120 ? 120 : 0;
				if (notFlat == null || next == 0 || source != "db") { warnings.AddRange(w); break; }
				padDays = next;
			}
			if (notFlat != null)
				warnings.Add("not flat at the start of the " + padDays + "-day pad (" + notFlat + "): a position opened before it is "
					+ "invisible here, so its closing fills pair as new entries and later trades in that instrument can be mis-paired");

			// The same engine the native Trade Performance window uses. Pairing needs the padded set.
			SystemPerformance perf = null;
			try { perf = SystemPerformance.Calculate(execs); }
			catch (Exception ex) { Log("/performance Calculate: " + Deep(ex)); warnings.Add("SystemPerformance.Calculate failed: " + Deep(ex)); }

			TradeCollection all = perf == null ? null : perf.AllTrades;
			var kept = new List<Trade>();
			int total = 0;
			if (all != null)
			{
				total = all.Count;
				foreach (Trade t in all)
				{
					DateTime xt = Acct_Tv(() => t.Exit.Time);
					if (xt == DateTime.MinValue) continue;						// still open, or unreadable — not a closed trade
					if (xt >= qq.From && xt <= qq.To) kept.Add(t);
				}
			}
			int padTrimmed = total - kept.Count;

			// The pad must never reach the metrics. Re-pair over the legs of the kept trades only, so
			// `summary` and `trades` come out of ONE SystemPerformance and sum(trades[].pnl) == netProfit
			// by construction. Nothing to do when the pad trimmed nothing.
			TradeCollection metricSet = all;
			if (padTrimmed > 0)
			{
				var legs = new List<Execution>();
				// Reference identity, not ExecutionId: sibling pairs from one partial fill SHARE a leg, and
				// a leg whose id is empty would otherwise be counted once per pair.
				var legSeen = new HashSet<Execution>();
				foreach (Trade t in kept)
				{
					Execution en = null, xx = null;
					try { en = t.Entry; xx = t.Exit; } catch { }
					if (en != null && legSeen.Add(en)) legs.Add(en);
					if (xx != null && legSeen.Add(xx)) legs.Add(xx);
				}
				try
				{
					SystemPerformance trimmed = SystemPerformance.Calculate(legs);
					metricSet = trimmed == null ? null : trimmed.AllTrades;
				}
				catch (Exception ex)
				{
					Log("/performance re-Calculate: " + Deep(ex));
					warnings.Add("metrics fall back to the padded set (" + padTrimmed + " trade(s) outside the range are included): " + Deep(ex));
					metricSet = all;
				}
			}

			var final = new List<Trade>();
			if (metricSet != null) foreach (Trade t in metricSet) final.Add(t);
			bool capped = final.Count > qq.N;
			if (capped) final = final.Take(qq.N).ToList();

			// Commission template: NinjaTrader recomputes per-fill commission at display time from the
			// account's template. Read it once, off the UI thread, and never present the reconstruction
			// as an unlabelled number.
			Commission tmpl = null;
			try { tmpl = qq.Acct.Commission; } catch (Exception ex) { Log("/performance commission template: " + Deep(ex)); }
			Currency denom = Currency.UsDollar;
			try { denom = qq.Acct.Denomination; } catch { }
			double svrComm = Acct_Dv(() => qq.Acct.Get(AccountItem.Commission, denom));
			double svrFee = Acct_Dv(() => qq.Acct.Get(AccountItem.Fee, denom));

			int nStored = 0, nTemplate = 0, nNone = 0;
			double commTotal = 0, tradeCommTotal = 0, tradeFeeTotal = 0;
			var rows = new List<string>();
			foreach (Trade t in final)
			{
				string src;
				double c = Acct_Commission(t, tmpl, out src);
				if (src == "stored") nStored++; else if (src == "template") nTemplate++; else nNone++;
				commTotal += c;
				tradeCommTotal += Acct_Dv(() => t.Commission);
				tradeFeeTotal += Acct_Dv(() => t.Fee);
				rows.Add(Acct_TradeOne(t, c, src));
			}

			string commSource = nStored > 0 && nTemplate > 0 ? "mixed" : nStored > 0 ? "stored" : nTemplate > 0 ? "template" : "none";

			return Obj(
				P("account", Q(qq.AccountName)),
				P("instrument", Q(qq.InstrumentName.Length == 0 ? null : qq.InstrumentName)),
				P("from", Tm(qq.From)), P("to", Tm(qq.To)),
				P("source", Q(source)),
				P("lookbackDays", I(lookbackDays)),
				P("executions", I(execs.Count)),
				P("padTrimmed", I(padTrimmed)),
				P("padDays", I(padDays)),
				P("capped", capped ? "true" : "false"),
				P("warnings", Arr(warnings.Select(Q))),
				P("summary", Acct_Summary(metricSet)),
				P("trades", Arr(rows)),
				P("commissionInfo", Obj(
					P("template", Q(Acct_Sv(() => tmpl == null ? null : tmpl.Name))),
					P("source", Q(commSource)),
					P("total", D(commTotal)),
					P("tradesFromStored", I(nStored)),
					P("tradesFromTemplate", I(nTemplate)),
					P("tradesNoCommission", I(nNone)),
					// Trade.Commission / Trade.Fee are public on 8.1.8.2 and MAY already do the proration
					// cli-nt-bridge's 17-line block did by hand. Reported next to the reconstruction so one live
					// comparison on a real account settles it instead of a code change.
					P("tradeCommissionTotal", D(tradeCommTotal)),
					P("tradeFeeTotal", D(tradeFeeTotal)),
					P("serverCommissionTotal", D(svrComm)),
					P("serverFeeTotal", D(svrFee)))));
		}

		/// <summary>Per-trade commission, resolved once so the total and the per-trade number agree.
		/// (1) the commission the server stamped on the fills, when there is one; (2) else the account's
		/// Commission TEMPLATE, entry side + exit side, the way the native window recomputes it;
		/// (3) else 0 — a funded/prop account with no local template genuinely has no local answer.</summary>
		private static double Acct_Commission(Trade t, Commission tmpl, out string source)
		{
			double stored = Acct_Dv(() => t.Entry.Commission + t.Exit.Commission);
			if (!double.IsNaN(stored) && stored != 0) { source = "stored"; return stored; }
			if (tmpl != null)
			{
				double c = Acct_Dv(() => tmpl.GetWithMinimum(t.Entry.Instrument, t.Entry.Quantity)
										+ tmpl.GetWithMinimum(t.Exit.Instrument, t.Exit.Quantity));
				if (!double.IsNaN(c) && c != 0) { source = "template"; return c; }
			}
			source = "none";
			return 0;
		}

		/// <summary>The status document v1 `trades[]` item (addon/NOTES.md), key for key and in order, so
		/// report.py reads a /performance document and a /backtest/{id} document the same way. Three keys
		/// are ADDED after the frozen ones: commission, commissionSource, fee.</summary>
		private static string Acct_TradeOne(Trade t, double commission, string commissionSource)
		{
			try
			{
				Execution en = t.Entry, ex = t.Exit;
				// A real fill carries BarIndex -1 (only a backtest fill has a bar): -1 - -1 = 0 is not a bar count.
				int bi = en == null ? -1 : Acct_BarIndex(en), bx = ex == null ? -1 : Acct_BarIndex(ex);
				int bars = bx - bi;
				bool barsKnown = bi >= 0 && bx >= 0 && (bi != 0 || bx != 0);
				return Obj(
					P("n", I(Acct_Lv(() => t.TradeNumber))),
					P("side", Q(en == null ? "" : Acct_Sv(() => en.MarketPosition.ToString()))),
					P("qty", I(Acct_Lv(() => t.Quantity))),
					P("entryName", Q(en == null ? null : Acct_Sv(() => en.Name))),
					P("exitName", Q(ex == null ? null : Acct_Sv(() => ex.Name))),
					P("entryTime", Tm(en == null ? DateTime.MinValue : Acct_Tv(() => en.Time))),
					P("exitTime", Tm(ex == null ? DateTime.MinValue : Acct_Tv(() => ex.Time))),
					P("entryPrice", D(en == null ? double.NaN : Acct_Dv(() => en.Price))),
					P("exitPrice", D(ex == null ? double.NaN : Acct_Dv(() => ex.Price))),
					// ProfitCurrency is NET of commission and fee when the provider stamped them on the fills.
					P("pnl", D(Acct_Dv(() => t.ProfitCurrency))),
					P("pnlPoints", D(Acct_Dv(() => t.ProfitPoints))),
					P("mae", D(Acct_Dv(() => t.MaePoints))),
					P("mfe", D(Acct_Dv(() => t.MfePoints))),
					P("bars", barsKnown ? I(bars) : "null"),
					P("commission", D(commission)),
					P("commissionSource", Q(commissionSource)),
					P("fee", D(Acct_Dv(() => t.Entry.Fee + t.Exit.Fee))));
			}
			catch (Exception e) { return Obj(P("error", Q(Deep(e)))); }
		}

		private static int Acct_BarIndex(Execution e) { try { return e.BarIndex; } catch { return -1; } }

		/// <summary>The status document v1 `summary` object (addon/NOTES.md), all 21 keys, in order, from
		/// NinjaTrader's own TradesPerformance — the same source Snapshot() in the backtest module uses.
		/// Every value is read through Acct_Dv, so one metric NinjaTrader cannot produce is a null, not a
		/// 500 for the whole report. null when there is no trade collection at all.</summary>
		private static string Acct_Summary(TradeCollection all)
		{
			if (all == null) return "null";
			int n = (int)Acct_Lv(() => all.TradesCount);
			int w = (int)Acct_Lv(() => all.WinningTrades.TradesCount);
			int l = (int)Acct_Lv(() => all.LosingTrades.TradesCount);
			return Obj(
				P("trades", I(n)), P("winners", I(w)), P("losers", I(l)), P("winRate", D(n == 0 ? 0 : (double)w / n)),
				P("netProfit", D(Acct_Dv(() => all.TradesPerformance.NetProfit))),
				P("grossProfit", D(Acct_Dv(() => all.TradesPerformance.GrossProfit))),
				P("grossLoss", D(Acct_Dv(() => all.TradesPerformance.GrossLoss))),
				P("profitFactor", D(Acct_Dv(() => all.TradesPerformance.ProfitFactor))),
				P("commission", D(Acct_Dv(() => all.TradesPerformance.TotalCommission))),
				P("maxDrawdown", D(Acct_Dv(() => all.TradesPerformance.Currency.Drawdown))),
				P("avgTrade", D(Acct_Dv(() => all.TradesPerformance.Currency.AverageProfit))),
				P("avgWinner", D(Acct_Dv(() => all.WinningTrades.TradesPerformance.Currency.AverageProfit))),
				P("avgLoser", D(Acct_Dv(() => all.LosingTrades.TradesPerformance.Currency.AverageProfit))),
				P("largestWinner", D(Acct_Dv(() => all.TradesPerformance.Currency.LargestWinner))),
				P("largestLoser", D(Acct_Dv(() => all.TradesPerformance.Currency.LargestLoser))),
				P("avgMae", D(Acct_Dv(() => all.TradesPerformance.Currency.AverageMae))),
				P("avgMfe", D(Acct_Dv(() => all.TradesPerformance.Currency.AverageMfe))),
				P("avgBarsInTrade", D(Acct_Dv(() => all.TradesPerformance.AverageBarsInTrade))),
				P("sharpe", D(Acct_Dv(() => all.TradesPerformance.SharpeRatio))),
				P("maxConsecWinners", I(Acct_Lv(() => all.TradesPerformance.MaxConsecutiveWinner))),
				P("maxConsecLosers", I(Acct_Lv(() => all.TradesPerformance.MaxConsecutiveLoser))));
		}
	}
}
