// NT8Bridge.Data.cs — the data store: GET /data/coverage, GET /data/probe, and
// POST /data/download + GET|DELETE /data/download/{id}. A download moves no money, so none of
// these need an arming file; they refuse only on real exposure (Data_Exposure) or with no real
// data provider connected (AnyNonSimConnected).
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md "Module seams").
//
// Portions of this file are derived from cli-nt-bridge
// (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
// MIT License. The full notice is in NOTICE at the repository root.
// Derived: the NCD/NRD store-scan rules (NT8BridgeServerPlayback.cs:1544-1619), the
// date-loop rules (nt8bridge/histget.py:53,63,66,69,82-86) and the MarketReplay
// requester reflection chain (NT8BridgeServer.cs:1768-1798).

#region Using declarations
using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using NinjaTrader.Cbi;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		// ── module constants ────────────────────────────────────────────────────
		private const int Data_MaxDays = 10;												// wider needs {"big":true}
		private const int Data_MaxDaysBig = 400;		// a season; {"big":true} RAISES the cap, it never removes it
		private static readonly TimeSpan Data_DateTimeout = TimeSpan.FromSeconds(900);		// a heavy MNQ replay day runs 300-460 s
		private static readonly string[] Data_AllKinds = { "tick", "minute", "day", "replay" };
		private static readonly string[] Data_AllTypes = { "Last", "Bid", "Ask" };

		// ── /data/probe bounds: a few small requests, never an unbounded search ──
		private const int Data_ProbeCapRequests = 14;					// log2(Data_ProbeCapDays) + slack
		private static readonly TimeSpan Data_ProbeCapWall = TimeSpan.FromSeconds(90);
		private static readonly TimeSpan Data_ProbeRequestTimeout = TimeSpan.FromSeconds(20);
		private const int Data_ProbeCapDays = 3650;		// "at least N days back (search cap reached)" past this

		// ── seams (NOTES.md "Module seams") ─────────────────────────────────────
		/// <summary>GET /data/coverage, GET /data/probe, GET /data/probe/{id}, POST /data/download,
		/// GET|DELETE /data/download/{id}. Null for everything else — including any other /data path, so the
		/// core emits the 404.</summary>
		private static string Route_Data(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (seg.Length < 1 || seg[0] != "data") return null;
			if (seg.Length == 2 && seg[1] == "coverage" && method == "GET")		return Data_Coverage(q, ref status);
			if (seg.Length == 2 && seg[1] == "probe" && method == "GET")		return Data_ProbeStart(q, ref status);
			if (seg.Length == 3 && seg[1] == "probe" && method == "GET")		return Data_ProbeStatus(seg[2], ref status);
			if (seg.Length == 2 && seg[1] == "download" && method == "POST")	return Data_DownloadStart(body, ref status);
			if (seg.Length == 3 && seg[1] == "download" && method == "GET")		return Data_DownloadStatus(seg[2], ref status);
			if (seg.Length == 3 && seg[1] == "download" && method == "DELETE")	return Data_DownloadCancel(seg[2], ref status);
			return null;
		}

		/// <summary>Every reflective target resolves ONCE, here, through Compat. Then the download worker.</summary>
		private static void Start_Data()
		{
			// db\<kind>\<Instrument.FullName>\ is the layout every store uses (verified live on 8.1.8.2). NT8's own
			// resolver is used when it agrees with that; a disagreement falls back to the convention and says so.
			Compat.Resolve("BarsBytes.GetDataDir", () => typeof(Data.BarsBytes).GetMethod(
				"GetDataDir", BindingFlags.Public | BindingFlags.Static, null,
				new[] { typeof(Instrument), typeof(Data.BarsPeriodType) }, null));

			// Connection.HistoricalDataClient is internal (Connection.cs:249) so reflection is mandatory;
			// RequestMarketReplay itself is public on that internal type (HdsClient.cs:178).
			var prop = (PropertyInfo)Compat.Resolve("Connection.HistoricalDataClient", () => typeof(Connection).GetProperty(
				"HistoricalDataClient", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance));
			Compat.Resolve("HdsClient.RequestMarketReplay", () => prop == null ? null : prop.PropertyType.GetMethod(
				"RequestMarketReplay", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance));

			// The current/future ET day is never downloaded (cli-nt-bridge histget.py:53,63): replay data is partial until
			// the session closes, and NT8's session dates are ET whatever the machine's own zone is.
			Compat.Resolve("TimeZoneInfo.EasternStandardTime", () => TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time"));

			// Its OWN worker, never the backtest one. Guarded because a rebind retry can run the
			// start hooks a second time, and a second worker would double every download.
			if (Data_thread == null || !Data_thread.IsAlive)
			{
				Data_thread = new Thread(Data_Worker) { IsBackground = true, Name = "NT8Bridge-dl" };
				Data_thread.Start();
			}
		}

		/// <summary>Called by the core's Stop() on NT8's UI thread: bounded, never waits on a dispatcher. Idempotent.</summary>
		private static void Stop_Data()
		{
			Data_CancelAll("shutdown");
			Data_signal.Set();
			// Only drop the reference when the thread really ended: a timed-out Join means a worker is
			// still alive on the queue, and Start_Data's `Data_thread == null || !IsAlive` guard must
			// still see it — otherwise a stranded worker plus a fresh one both dequeue, doubling every download.
			try { if (Data_thread != null && Data_thread.Join(3000)) Data_thread = null; }
			catch { Data_thread = null; }
		}

		// ── /data/coverage ──────────────────────────────────────────────
		/// <summary>One day (or, for the day store, one year) of one store: how many files, how big, and how many of
		/// each market data type.</summary>
		private sealed class Data_Day
		{
			public int Files, Last, Bid, Ask;
			public long Bytes;
		}

		/// <summary>db\&lt;kind&gt;\&lt;name&gt;. NT8's own BarsBytes.GetDataDir is preferred when it resolves AND
		/// points at a folder whose last segment is this instrument; anything else falls back to the convention and
		/// reports `dirSource:"convention"` so a caller is never told a guess is authoritative.</summary>
		private static string Data_StoreDir(string kind, string name, Instrument inst, out string source)
		{
			source = "convention";
			string conv = Path.Combine(Core.Globals.UserDataDir, "db", kind, name);
			if (inst != null && kind != "replay")
			{
				var mi = Compat.Get<MethodInfo>("BarsBytes.GetDataDir");
				if (mi != null)
				{
					try
					{
						var pt = kind == "tick" ? Data.BarsPeriodType.Tick : kind == "minute" ? Data.BarsPeriodType.Minute : Data.BarsPeriodType.Day;
						string got = mi.Invoke(null, new object[] { inst, pt }) as string;
						if (!string.IsNullOrEmpty(got) && Directory.Exists(got)
							&& string.Equals(new DirectoryInfo(got).Name, name, StringComparison.OrdinalIgnoreCase))
						{
							source = "BarsBytes.GetDataDir";
							return got;
						}
					}
					catch (Exception ex) { Log("Data coverage: GetDataDir(" + name + "," + kind + "): " + Deep(ex)); }
				}
			}
			return conv;
		}

		/// <summary>"hour" (tick: YYYYMMDDHHmm.&lt;Type&gt;.ncd), "day" (minute: YYYYMMDD.&lt;Type&gt;.ncd; replay:
		/// YYYYMMDD.nrd) or "year" (day: YYYY.&lt;Type&gt;.ncd — verified live, the day store has NO per-day file).</summary>
		private static string Data_Granularity(string kind)
		{
			return kind == "tick" ? "hour" : kind == "day" ? "year" : "day";
		}

		/// <summary>Name-based pre-flight scan of one store folder. File I/O only — never a dispatcher, never a
		/// NinjaTrader call. Deliberately answers "is there anything for that day", not "is the content that day":
		/// the content is decided when the series loads and a run with missing data still fails loudly there.
		/// Returns null when the folder does not exist — the caller then reports scanned:false, never an empty days map.</summary>
		private static SortedDictionary<string, Data_Day> Data_ScanStore(string dir, string kind)
		{
			if (!Directory.Exists(dir)) return null;
			var days = new SortedDictionary<string, Data_Day>(StringComparer.Ordinal);
			bool replay = kind == "replay";
			int keyLen = kind == "day" ? 4 : 8;
			foreach (var f in Directory.EnumerateFiles(dir, replay ? "*.nrd" : "*.ncd"))
			{
				string stem = Path.GetFileNameWithoutExtension(f) ?? "";		// "202609100100.Last" or "20260911"
				string datePart = stem, type = null;
				int dot = stem.IndexOf('.');
				if (dot >= 0) { datePart = stem.Substring(0, dot); type = stem.Substring(dot + 1); }
				if (datePart.Length < keyLen || !datePart.Take(keyLen).All(char.IsDigit)) continue;
				string key = datePart.Substring(0, keyLen);
				Data_Day d;
				if (!days.TryGetValue(key, out d)) { d = new Data_Day(); days[key] = d; }
				d.Files++;
				try { d.Bytes += new FileInfo(f).Length; } catch { }
				if (string.Equals(type, "Last", StringComparison.OrdinalIgnoreCase)) d.Last++;
				else if (string.Equals(type, "Bid", StringComparison.OrdinalIgnoreCase)) d.Bid++;
				else if (string.Equals(type, "Ask", StringComparison.OrdinalIgnoreCase)) d.Ask++;
			}
			return days;
		}

		private static string Data_DayJson(string kind, Data_Day d)
		{
			if (kind == "replay") return Obj(P("files", I(d.Files)), P("bytes", I(d.Bytes)));
			return Obj(P("files", I(d.Files)), P("bytes", I(d.Bytes)), P("last", I(d.Last)), P("bid", I(d.Bid)), P("ask", I(d.Ask)));
		}

		private static string Data_StoreJson(string kind, string dir, string source, SortedDictionary<string, Data_Day> days, bool withDays)
		{
			if (days == null)
				return Obj(P("scanned", "false"), P("dir", Q(dir)), P("dirSource", Q(source)),
					P("granularity", Q(Data_Granularity(kind))), P("files", "null"), P("bytes", "null"),
					P("firstDay", "null"), P("lastDay", "null"), P("days", "null"));
			int files = days.Values.Sum(v => v.Files);
			long bytes = days.Values.Sum(v => v.Bytes);
			var pairs = new List<string>
			{
				P("scanned", "true"), P("dir", Q(dir)), P("dirSource", Q(source)),
				P("granularity", Q(Data_Granularity(kind))), P("files", I(files)), P("bytes", I(bytes)),
				P("firstDay", Q(days.Count == 0 ? null : days.Keys.First())),
				P("lastDay", Q(days.Count == 0 ? null : days.Keys.Last()))
			};
			pairs.Add(P("days", withDays ? Obj(days.Select(kv => P(kv.Key, Data_DayJson(kind, kv.Value))).ToArray()) : "null"));
			return Obj(pairs.ToArray());
		}

		/// <summary>The continuous contract name for a front month: "ES 12-26" -> "ES ##-##". cli-nt-bridge's trap
		/// (CHANGELOG.md:141-149): a front month can hold every NCD file and still have no .nrd at all, because the
		/// recordings live under the continuous name. Always scan both before telling anyone a store is empty.</summary>
		private static string Data_ContinuousName(string name)
		{
			var m = System.Text.RegularExpressions.Regex.Match(name ?? "", @"^(\S+) \d{2}-\d{2}$");
			return m.Success ? m.Groups[1].Value + " ##-##" : null;
		}

		private static bool Data_TryDay(string s, out DateTime d)
		{
			return DateTime.TryParseExact(s, "yyyyMMdd", CultureInfo.InvariantCulture, DateTimeStyles.None, out d);
		}

		/// <summary>"NQ 12-26" -> "NQ" (the chain a merge policy walks in a backtest). Bare symbols (no dated
		/// contract) return the name itself, which still narrows the scan to an exact folder match below.</summary>
		private static string Data_RootSymbol(string name)
		{
			var m = System.Text.RegularExpressions.Regex.Match(name ?? "", @"^(\S+) \d{2}-\d{2}$");
			return m.Success ? m.Groups[1].Value : name;
		}

		/// <summary>What a BACKTEST can use without a provider: NT8's own bars cache
		/// (db\cache\&lt;TradingHours&gt;.&lt;TimeZone&gt;\&lt;MINUTE|TICK&gt;\&lt;contract&gt;\&lt;seriesKey&gt;.&lt;from&gt;.&lt;to&gt;.&lt;Type&gt;.ncd —
		/// see addon/NOTES.md "cache"), across every contract of the instrument's chain (root symbol match, same
		/// idea as Data_ContinuousName). Name-based only, bounded (Data_CacheScanCap directories/files), never a
		/// dispatcher or NinjaTrader call — same contract as Data_ScanStore. day/replay have no cache folder, so
		/// only "minute"/"tick" are scanned; other kinds return an empty map.</summary>
		private const int Data_CacheScanCap = 4000;
		private static string Data_CacheJson(string name, string[] kinds)
		{
			string root = Data_RootSymbol(name);
			string cacheRoot = Path.Combine(Core.Globals.UserDataDir, "db", "cache");
			// seriesKey -> (contract, firstDay, lastDay); firstDay/lastDay are the "from"/"to" stems NT8 itself
			// wrote into the filename, not re-derived from file content.
			var series = new SortedDictionary<string, Tuple<string, string, string>>(StringComparer.Ordinal);
			int scanned = 0;
			bool capped = false;
			try
			{
				if (Directory.Exists(cacheRoot))
				{
					foreach (var thDir in Directory.EnumerateDirectories(cacheRoot))
					{
						foreach (var k in kinds)
						{
							string kindFolder = k == "minute" ? "MINUTE" : k == "tick" ? "TICK" : null;
							if (kindFolder == null) continue;
							string kDir = Path.Combine(thDir, kindFolder);
							if (!Directory.Exists(kDir)) continue;
							foreach (var contractDir in Directory.EnumerateDirectories(kDir))
							{
								string contract = new DirectoryInfo(contractDir).Name;
								if (!contract.StartsWith(root + " ", StringComparison.OrdinalIgnoreCase) && !string.Equals(contract, root, StringComparison.OrdinalIgnoreCase))
									continue;
								foreach (var f in Directory.EnumerateFiles(contractDir, "*.ncd"))
								{
									if (++scanned > Data_CacheScanCap) { capped = true; break; }
									string stem = Path.GetFileNameWithoutExtension(f) ?? "";	// "<seriesKey>.<from8>.<to8>.<Type>"
									var parts = stem.Split('.');
									if (parts.Length < 4) continue;
									string type = parts[parts.Length - 1], to = parts[parts.Length - 2], from = parts[parts.Length - 3];
									string seriesKey = contract + "/" + string.Join(".", parts.Take(parts.Length - 3)) + "." + type;
									Tuple<string, string, string> cur;
									if (!series.TryGetValue(seriesKey, out cur))
										series[seriesKey] = Tuple.Create(contract, from, to);
									else
									{
										string lo = string.CompareOrdinal(from, cur.Item2) < 0 ? from : cur.Item2;
										string hi = string.CompareOrdinal(to, cur.Item3) > 0 ? to : cur.Item3;
										series[seriesKey] = Tuple.Create(contract, lo, hi);
									}
								}
								if (capped) break;
							}
							if (capped) break;
						}
						if (capped) break;
					}
				}
			}
			catch (Exception ex) { Log("Data coverage cache scan: " + Deep(ex)); }

			return Obj(
				P("note", Q("what a backtest of that series can use without a provider (NT8's own bars cache, name-based scan)")),
				P("scanned", Directory.Exists(cacheRoot) ? "true" : "false"),
				P("capReached", capped ? "true" : "false"),
				P("series", Obj(series.Select(kv => P(kv.Key, Obj(
					P("contract", Q(kv.Value.Item1)), P("firstDay", Q(kv.Value.Item2)), P("lastDay", Q(kv.Value.Item3))))).ToArray())));
		}

		private static string Data_Coverage(System.Collections.Specialized.NameValueCollection q, ref int status)
		{
			string name = (q["instrument"] ?? "").Trim();
			if (name.Length == 0) return Err(ref status, 400, "instrument is required, e.g. /data/coverage?instrument=ES%2012-26");

			string kind = (q["kind"] ?? "").Trim().ToLowerInvariant();
			if (kind.Length > 0 && !Data_AllKinds.Contains(kind))
				return Err(ref status, 400, "kind must be one of tick|minute|day|replay");
			string[] kinds = kind.Length > 0 ? new[] { kind } : Data_AllKinds;

			DateTime from = DateTime.MinValue, to = DateTime.MaxValue;
			string fromS = (q["from"] ?? "").Trim(), toS = (q["to"] ?? "").Trim();
			if (fromS.Length > 0 && !Data_TryDay(fromS, out from)) return Err(ref status, 400, "from must be YYYYMMDD");
			if (toS.Length > 0 && !Data_TryDay(toS, out to)) return Err(ref status, 400, "to must be YYYYMMDD");
			if (fromS.Length > 0 && toS.Length > 0)
			{
				string rp = RangeProblem(from, to);		// never let an inverted or placeholder range through
				if (rp != null) return Err(ref status, 400, rp);
			}

			Instrument inst = null;
			try { inst = Instrument.GetInstrument(name); } catch (Exception ex) { Log("Data coverage: GetInstrument(" + name + "): " + Deep(ex)); }

			// the requested name, in full
			var stores = new List<string>();
			SortedDictionary<string, Data_Day> analysed = null;
			string analysedKind = null;
			foreach (var k in kinds)
			{
				string src; string dir = Data_StoreDir(k, name, inst, out src);
				var days = Data_ScanStore(dir, k);
				stores.Add(P(k, Data_StoreJson(k, dir, src, days, true)));
				if (analysed == null && days != null && days.Count > 0 && Data_Granularity(k) != "year") { analysed = days; analysedKind = k; }
			}

			// the continuous name, as a summary only (no days map): enough to see where the files really are
			var resolved = new List<string> { name };
			var also = new List<string>();
			string cont = Data_ContinuousName(name);
			if (cont != null)
			{
				resolved.Add(cont);
				var per = new List<string>();
				foreach (var k in kinds)
				{
					string src; string dir = Data_StoreDir(k, cont, null, out src);
					per.Add(P(k, Data_StoreJson(k, dir, src, Data_ScanStore(dir, k), false)));
				}
				also.Add(P(cont, Obj(per.ToArray())));
			}

			// analysis over the chosen store, clipped to [from..to]
			string missing = "null", lacking = "null", thin = "null";
			if (analysed != null)
			{
				// An unparseable boundary key (junk/partial file whose first 8 chars are digits but not a
				// real date, e.g. "00000000" or "20260230") means there is no real analysis window — treat
				// it the same as "nothing to analyse" rather than defaulting lo to DateTime.MinValue and
				// looping ~739,000 days.
				DateTime lo, hi;
				if (Data_TryDay(analysed.Keys.First(), out lo) && Data_TryDay(analysed.Keys.Last(), out hi))
				{
					if (from > lo) lo = from;
					if (to < hi) hi = to;

					var miss = new List<string>();
					for (var d = lo.Date; d <= hi.Date; d = d.AddDays(1))
					{
						if (d.DayOfWeek == DayOfWeek.Saturday || d.DayOfWeek == DayOfWeek.Sunday) continue;	// Sat has no session; a Sunday evening open is filed under Monday
						string key = d.ToString("yyyyMMdd", CultureInfo.InvariantCulture);
						if (!analysed.ContainsKey(key)) miss.Add(key);
					}
					missing = Arr(miss.Select(Q));

					var inWindow = analysed.Where(kv =>
					{
						DateTime d;
						return Data_TryDay(kv.Key, out d) && d.Date >= from.Date && (to == DateTime.MaxValue || d.Date <= to.Date);
					}).ToList();

					if (analysedKind != "replay")
						lacking = Arr(inWindow.Where(kv => kv.Value.Bid == 0 || kv.Value.Ask == 0).Select(kv => Q(kv.Key)));

					// "thin" = a day holding less than a fifth of the median day's bytes. A blunt heuristic on purpose:
					// it is a pre-flight hint ("look at that day"), never a completeness verdict. Needs >= 5 days.
					if (inWindow.Count >= 5)
					{
						var sorted = inWindow.Select(kv => kv.Value.Bytes).OrderBy(x => x).ToList();
						long median = sorted[sorted.Count / 2];
						thin = Arr(inWindow.Where(kv => median > 0 && kv.Value.Bytes * 5 < median).Select(kv => Q(kv.Key)));
					}
				}
			}

			return Obj(
				P("instrument", Q(name)),
				P("instrumentResolved", inst == null ? "null" : Q(inst.FullName)),
				P("resolved", Arr(resolved.Select(Q))),
				P("kind", Q(kind.Length > 0 ? kind : null)),
				P("analysisStore", Q(analysedKind)),
				P("from", Q(fromS.Length > 0 ? fromS : null)),
				P("to", Q(toS.Length > 0 ? toS : null)),
				P("stores", Obj(stores.ToArray())),
				P("alsoScanned", Obj(also.ToArray())),
				P("cache", Data_CacheJson(name, kinds)),
				P("missingWeekdays", missing),
				P("daysLackingBidAsk", lacking),
				P("thinDays", thin),
				P("note", Q("pre-flight only: presence of files, not completeness")));
		}

		// ── /data/download: its own worker and job map ─────────────
		private static readonly object Data_gate = new object();
		private static readonly Dictionary<string, Data_Job> Data_jobs = new Dictionary<string, Data_Job>(StringComparer.OrdinalIgnoreCase);
		private static readonly Queue<Data_Job> Data_queue = new Queue<Data_Job>();
		private static readonly AutoResetEvent Data_signal = new AutoResetEvent(false);
		private static Thread Data_thread;
		private static int Data_counter;

		private sealed class Data_Job
		{
			public string			Id;
			public volatile string	State = "queued";		// queued|running|done|error|cancelled|refused
			public volatile bool	Cancel;
			public volatile string	Current;				// the date being worked on, or null

			public string			InstrumentName = "";
			public Instrument		Instrument;
			public DateTime			From, To;
			public string[]			Kinds = new string[0], Types = new string[0];
			public bool				Overwrite, Big;
			public int				Days;

			public DateTime			QueuedAt, StartedAt, FinishedAt;
			public string			Error;
			// written by the worker, read by HTTP threads: every touch is under Data_gate
			public readonly List<string> Downloaded = new List<string>();
			public readonly List<string> Skipped = new List<string>();
			public readonly List<string> SkippedCurrent = new List<string>();
			public readonly List<string> Failed = new List<string>();		// finished JSON objects
		}

		/// <summary>Validate on the HTTP thread, then queue and answer 202. No arming file: a download moves no
		/// money. Guard order: the request first, then the live predicates (Data_Exposure, AnyNonSimConnected).</summary>
		private static string Data_DownloadStart(string body, ref int status)
		{
			Dictionary<string, object> req;
			try { req = ParseJson(body) as Dictionary<string, object>; }
			catch (Exception ex) { return Err(ref status, 400, "bad JSON body: " + ex.Message); }
			if (req == null) return Err(ref status, 400, "body must be a JSON object");

			try
			{
				string name = JGetStr(req, "instrument", null);
				if (string.IsNullOrEmpty(name)) return Err(ref status, 400, "instrument is required");
				Instrument inst = null;
				try { inst = Instrument.GetInstrument(name); } catch (Exception ex) { return Err(ref status, 400, "instrument '" + name + "': " + Deep(ex)); }
				if (inst == null) return Err(ref status, 400, "unknown instrument '" + name + "'");

				DateTime from, to;
				if (!Data_TryDay(JGetStr(req, "from", ""), out from)) return Err(ref status, 400, "from must be YYYYMMDD");
				if (!Data_TryDay(JGetStr(req, "to", ""), out to)) return Err(ref status, 400, "to must be YYYYMMDD");
				string rp = RangeProblem(from, to);
				if (rp != null) return Err(ref status, 400, rp);

				bool big = JGetBool(req, "big", false);
				int days = (int)(to.Date - from.Date).TotalDays + 1;
				if (days > Data_MaxDays && !big)
					return Err(ref status, 400, "range is " + days + " days; more than " + Data_MaxDays
						+ " needs {\"big\":true} (a heavy replay day is ~500 MB and 300-460 s)");
				if (days > Data_MaxDaysBig)
					return Err(ref status, 400, "range is " + days + " days; more than " + Data_MaxDaysBig
						+ " is refused even with {\"big\":true} — split the request");

				string[] kinds = Data_List(req, "kinds", new[] { "replay" }, true);
				foreach (var k in kinds) if (!Data_AllKinds.Contains(k)) return Err(ref status, 400, "kinds must be a subset of tick|minute|day|replay");
				if (kinds.Length == 0) return Err(ref status, 400, "kinds must not be empty");
				string[] types = Data_List(req, "types", Data_AllTypes, false);
				foreach (var t in types) if (!Data_AllTypes.Any(x => string.Equals(x, t, StringComparison.OrdinalIgnoreCase)))
					return Err(ref status, 400, "types must be a subset of Last|Bid|Ask");
				if (types.Length == 0) return Err(ref status, 400, "types must not be empty");

				// Design decision: a download routes no order, so the live-connection guard does NOT refuse it —
				// on a machine whose only real data provider is a broker demo connection (it can route orders, so
				// it counts as live), that guard would make the endpoint unreachable. The real risk is a throttled feed while
				// something is at stake, so the refusal is EXPOSURE: an open position or a working order on any account.
				// AnyNonSimConnected() stays the PRECONDITION: a historical download needs a real data provider.
				string exposure = Data_Exposure();
				if (exposure != null)
				{
					status = 409;
					return Obj(P("error", Q("data download refused: " + exposure + " — flatten and cancel first")), P("exposure", "true"));
				}
				if (!AnyNonSimConnected())
				{
					status = 409;
					return Obj(P("error", Q("data download needs a real data provider connected (see /health.connections)")),
						P("anyLive", AnyLiveConnected() ? "true" : "false"), P("anyNonSim", "false"));
				}

				var job = new Data_Job
				{
					InstrumentName = inst.FullName, Instrument = inst, From = from.Date, To = to.Date,
					Kinds = kinds, Types = types, Overwrite = JGetBool(req, "overwrite", false), Big = big,
					Days = days, QueuedAt = DateTime.Now
				};
				job.Id = "d" + Interlocked.Increment(ref Data_counter);
				lock (Data_gate) { Data_jobs[job.Id] = job; Data_queue.Enqueue(job); }
				Data_signal.Set();
				Log("data download " + job.Id + " queued " + job.InstrumentName + " " + job.From.ToString("yyyyMMdd", CultureInfo.InvariantCulture)
					+ ".." + job.To.ToString("yyyyMMdd", CultureInfo.InvariantCulture) + " kinds=" + string.Join(",", kinds) + " types=" + string.Join(",", types));
				status = 202;
				return Obj(P("id", Q(job.Id)), P("state", Q("queued")), P("days", I(days)),
					P("anyLive", AnyLiveConnected() ? "true" : "false"), P("anyNonSim", "true"));
			}
			catch (BadRequestException ex) { return Err(ref status, 400, ex.Message); }
		}

		/// <summary>A JSON array of strings, or the default when the key is absent. A key present with the wrong type
		/// is a 400, never a silent fall back to the default (the same rule as JGetStr/JGetBool/JGetInt).
		/// `lower` folds case, which `kinds` needs and `types` must not have (Enum.Parse is given the raw value).</summary>
		private static string[] Data_List(Dictionary<string, object> req, string key, string[] dflt, bool lower)
		{
			object v = JGet(req, key);
			if (v == null) return dflt;
			var list = v as List<object>;
			if (list == null) throw new BadRequestException(key + " must be an array of strings");
			var outp = new List<string>();
			foreach (var item in list)
			{
				var s = item as string;
				if (s == null) throw new BadRequestException(key + " must be an array of strings");
				s = s.Trim();
				outp.Add(lower ? s.ToLowerInvariant() : s);
			}
			return outp.ToArray();
		}

		private static Data_Job Data_Find(string id) { lock (Data_gate) { Data_Job j; return Data_jobs.TryGetValue(id, out j) ? j : null; } }

		private static string Data_DownloadStatus(string id, ref int status)
		{
			var job = Data_Find(id);
			return job == null ? Err(ref status, 404, "no data download '" + id + "'") : Data_JobJson(job);
		}

		private static string Data_DownloadCancel(string id, ref int status)
		{
			var job = Data_Find(id);
			if (job == null) return Err(ref status, 404, "no data download '" + id + "'");
			job.Cancel = true;
			lock (Data_gate)
				if (job.State == "queued" || job.State == "running")
				{
					job.State = "cancelled"; job.Error = "cancelled by DELETE"; job.FinishedAt = DateTime.Now;
				}
			Log("data download " + id + " cancelled by DELETE");
			return Obj(P("ok", "true"), P("state", Q(job.State)));
		}

		private static long Data_FreeDiskBytes()
		{
			try { return new DriveInfo(Path.GetPathRoot(Core.Globals.UserDataDir)).AvailableFreeSpace; }
			catch { return -1; }
		}

		private static string Data_JobJson(Data_Job job)
		{
			string[] dl, sk, sc, fa;
			lock (Data_gate)
			{
				dl = job.Downloaded.ToArray(); sk = job.Skipped.ToArray();
				sc = job.SkippedCurrent.ToArray(); fa = job.Failed.ToArray();
			}
			long free = Data_FreeDiskBytes();
			return Obj(
				P("id", Q(job.Id)), P("state", Q(job.State)),
				P("instrument", Q(job.InstrumentName)),
				P("from", Q(job.From.ToString("yyyyMMdd", CultureInfo.InvariantCulture))),
				P("to", Q(job.To.ToString("yyyyMMdd", CultureInfo.InvariantCulture))),
				P("kinds", Arr(job.Kinds.Select(Q))), P("types", Arr(job.Types.Select(Q))),
				P("overwrite", job.Overwrite ? "true" : "false"), P("big", job.Big ? "true" : "false"),
				P("days", I(job.Days)), P("current", Q(job.Current)),
				P("queuedAt", Tm(job.QueuedAt)), P("startedAt", Tm(job.StartedAt)), P("finishedAt", Tm(job.FinishedAt)),
				P("seconds", D(job.FinishedAt == DateTime.MinValue || job.StartedAt == DateTime.MinValue
					? double.NaN : (job.FinishedAt - job.StartedAt).TotalSeconds)),
				P("error", Q(job.Error)),
				P("downloaded", Arr(dl.Select(Q))), P("skipped", Arr(sk.Select(Q))),
				P("skippedCurrent", Arr(sc.Select(Q))), P("failed", Arr(fa)),
				P("anyLive", AnyLiveConnected() ? "true" : "false"),
				P("anyNonSim", AnyNonSimConnected() ? "true" : "false"),
				P("freeDiskBytes", free < 0 ? "null" : I(free)));
		}

		private static void Data_CancelAll(string why)
		{
			Data_Job[] all;
			lock (Data_gate) { all = Data_jobs.Values.ToArray(); Data_queue.Clear(); }
			foreach (var j in all)
			{
				j.Cancel = true;
				lock (Data_gate)
					if (j.State == "queued" || j.State == "running")
					{
						j.State = "cancelled"; j.Error = why; j.FinishedAt = DateTime.Now;
					}
			}
		}

		private static void Data_Add(List<string> list, string item) { lock (Data_gate) list.Add(item); }

		/// <summary>Today in America/New_York, matching NT8's ET session dates whatever the machine's own zone is
		/// (cli-nt-bridge histget.py:53). Falls back to the local date when the zone cannot be resolved.</summary>
		private static DateTime Data_TodayEt()
		{
			var tz = Compat.Get<TimeZoneInfo>("TimeZoneInfo.EasternStandardTime");
			if (tz == null) return DateTime.Today;
			try { return TimeZoneInfo.ConvertTimeFromUtc(DateTime.UtcNow, tz).Date; } catch { return DateTime.Today; }
		}

		// ── the download worker (its own thread, never the backtest one) ────
		private static void Data_Worker()
		{
			while (running)
			{
				Data_Job job = null;
				lock (Data_gate) if (Data_queue.Count > 0) job = Data_queue.Dequeue();
				if (job == null) { Data_signal.WaitOne(500); continue; }
				if (job.Cancel) continue;

				job.StartedAt = DateTime.Now;
				lock (Data_gate) if (job.State == "queued") job.State = "running";
				try { Data_RunJob(job); }
				catch (Exception ex)
				{
					job.Error = Deep(ex);
					lock (Data_gate) if (job.State == "running") job.State = "error";
					Log("data download " + job.Id + ": " + ex);
				}
				finally
				{
					job.Current = null;
					job.FinishedAt = DateTime.Now;
					lock (Data_gate) if (job.State == "running") job.State = "done";
					Log("data download " + job.Id + " " + job.State + " in "
						+ (job.FinishedAt - job.StartedAt).TotalSeconds.ToString("F1", CultureInfo.InvariantCulture) + "s");
				}
			}
		}

		/// <summary>The download's refusal: null = nothing at stake; otherwise what is.
		/// Every account but the Backtest one counts, paper included: the code cannot tell a paper account from a
		/// funded one. Snapshot under each lock, judge outside it. If it cannot be read the answer is "exposed".</summary>
		private static string Data_Exposure()
		{
			try
			{
				Account[] accounts;
				lock (Account.All) accounts = Account.All.ToArray();
				foreach (var a in accounts)
				{
					if (a == null || a.Name == Account.BackTestAccountName) continue;
					Position[] pos; Order[] ords;
					lock (a.Positions) pos = a.Positions.ToArray();
					lock (a.Orders) ords = a.Orders.ToArray();
					if (pos.Any(p => p != null && p.MarketPosition != MarketPosition.Flat)) return "account '" + a.Name + "' has an open position";
					if (ords.Any(o => o != null && WorkingStates.Contains(o.OrderState))) return "account '" + a.Name + "' has a working order";
				}
				return null;
			}
			catch (Exception ex) { Log("Data_Exposure: " + Deep(ex) + " — answering exposed"); return "positions and orders could not be read"; }
		}

		private static void Data_RunJob(Data_Job job)
		{
			DateTime today = Data_TodayEt();
			for (var d = job.From.Date; d <= job.To.Date; d = d.AddDays(1))
			{
				if (job.Cancel || !running) return;

				// Every guard is re-checked per date, not once at POST time: a position can open in the
				// minutes a multi-day job runs.
				string exposure = Data_Exposure();
				if (exposure != null)
				{
					job.Error = exposure + " — stopped";
					lock (Data_gate) if (job.State == "running") job.State = "refused";
					Log("data download " + job.Id + ": refused mid-run, " + exposure);
					return;
				}
				if (!AnyNonSimConnected())
				{
					job.Error = "no real data provider connected — stopped";
					lock (Data_gate) if (job.State == "running") job.State = "error";
					return;
				}

				string ds = d.ToString("yyyyMMdd", CultureInfo.InvariantCulture);
				job.Current = ds;
				if (d >= today) { Data_Add(job.SkippedCurrent, ds); continue; }	// partial until the session closes (cli-nt-bridge histget.py:63)
				if (d.DayOfWeek == DayOfWeek.Saturday) continue;					// no session (cli-nt-bridge histget.py:66)

				foreach (var kind in job.Kinds)
				{
					if (job.Cancel || !running) return;
					try
					{
						if (kind == "replay") Data_ReplayDay(job, d, ds);
						else Data_HistDay(job, kind, d, ds);
					}
					catch (Exception ex)
					{
						Data_Add(job.Failed, Obj(P("date", Q(ds)), P("kind", Q(kind)), P("error", Q(Deep(ex)))));
					}
				}
			}
		}

		// ── replay engine: NT8's own RequestMarketReplay, one date at a time ────
		/// <summary>The HDS client that exposes RequestMarketReplay: Connection.ClientConnection's internal
		/// HistoricalDataClient first, then its public Adapter, then the same two over every Connection
		/// (cli-nt-bridge ResolveMarketReplayRequester, NT8BridgeServer.cs:1768-1798). The MethodInfo for the HdsClient
		/// property type is resolved once in Start_Data; an Adapter's type varies per provider, so that one is
		/// looked up here. Returns false when the API is not on this build — which is reported loudly,
		/// never swallowed into "nothing to do".</summary>
		private static bool Data_Requester(out object client, out MethodInfo method)
		{
			client = null; method = null;
			const BindingFlags F = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
			var hdsProp = Compat.Get<PropertyInfo>("Connection.HistoricalDataClient");
			var hdsMethod = Compat.Get<MethodInfo>("HdsClient.RequestMarketReplay");

			var candidates = new List<Connection>();
			try { if (Connection.ClientConnection != null) candidates.Add(Connection.ClientConnection); } catch { }
			try { foreach (var c in ConnSnapshot()) if (c != null && !candidates.Contains(c)) candidates.Add(c); } catch { }

			foreach (var c in candidates)
			{
				if (hdsProp != null && hdsMethod != null)
				{
					object hds = null;
					try { hds = hdsProp.GetValue(c, null); } catch { }
					if (hds != null) { client = hds; method = hdsMethod; return true; }
				}
				object adapter = null;
				try { adapter = c.Adapter; } catch { }
				if (adapter == null) continue;
				MethodInfo m = null;
				try { m = adapter.GetType().GetMethod("RequestMarketReplay", F); } catch { }
				if (m != null) { client = adapter; method = m; return true; }
			}
			return false;
		}

		private static void Data_ReplayDay(Data_Job job, DateTime day, string ds)
		{
			string path = Path.Combine(Core.Globals.UserDataDir, "db", "replay", job.InstrumentName, ds + ".nrd");
			if (File.Exists(path) && !job.Overwrite) { Data_Add(job.Skipped, ds + "/replay"); return; }

			object client; MethodInfo method;
			if (!Data_Requester(out client, out method))
				throw new Exception("MarketReplay download API not found on this NT8 build "
					+ "(Connection.HistoricalDataClient.RequestMarketReplay); a data provider must also be logged in");

			ErrorCode ec = ErrorCode.NoError;
			string emsg = null;
			using (var done = new ManualResetEventSlim(false))
			{
				Action<ErrorCode, string, object> cb = (code, msg, state) => { ec = code; emsg = msg; try { done.Set(); } catch { } };
				method.Invoke(client, new object[] { job.Instrument, day, cb, null, null });		// (Instrument, dateEst[ET], callback, IProgress, state)
				if (!done.Wait(Data_DateTimeout))
					throw new TimeoutException("replay download did not answer in "
						+ Data_DateTimeout.TotalSeconds.ToString("F0", CultureInfo.InvariantCulture) + "s");
			}
			if (ec != ErrorCode.NoError)
				throw new Exception("replay download " + ec + (string.IsNullOrEmpty(emsg) ? "" : ": " + emsg));
			if (!File.Exists(path))
				throw new Exception("replay download reported success but no " + ds + ".nrd was written");
			Data_Add(job.Downloaded, ds + "/replay");
		}

		// ── historical engine: the Historical Data window's own Download button ─
		private static Data.BarsPeriodType Data_PeriodType(string kind)
		{
			return kind == "tick" ? Data.BarsPeriodType.Tick : kind == "minute" ? Data.BarsPeriodType.Minute : Data.BarsPeriodType.Day;
		}

		/// <summary>Is there already a file for this (kind, day, type)? tick files are hourly
		/// (YYYYMMDDHHmm.&lt;Type&gt;.ncd), minute files daily, day files per YEAR — so for `day` the presence test is
		/// necessarily "is there a file for that year", which is why `overwrite` is the honest switch there.</summary>
		private static bool Data_HasDay(string kind, string name, DateTime day, string type)
		{
			try
			{
				string dir = Path.Combine(Core.Globals.UserDataDir, "db", kind, name);
				if (!Directory.Exists(dir)) return false;
				string prefix = kind == "day" ? day.ToString("yyyy", CultureInfo.InvariantCulture) : day.ToString("yyyyMMdd", CultureInfo.InvariantCulture);
				foreach (var f in Directory.EnumerateFiles(dir, prefix + "*." + type + ".ncd")) return true;
			}
			catch { }
			return false;
		}

		/// <summary>The connection candidates for a tick/minute/day download, in the order a chart would try
		/// them: connected real (non-Simulator/Playback) providers first, then Simulator/Playback connections,
		/// ClientConnection (NT8's own hosted data service) LAST — the decompile spike (addon/NOTES.md /
		/// docs/api/data.md) found no public API that asks NinjaTrader which connection serves an instrument's
		/// historical data, so this is a best-effort order, not an authoritative one. `Data_HistDay` reports,
		/// per day, exactly which candidate served it — never the order itself.</summary>
		private static List<Connection> Data_HistCandidates()
		{
			var real = new List<Connection>();
			var simPlayback = new List<Connection>();
			foreach (var c in ConnSnapshot())
			{
				if (c == null || !IsConnected(c) || ReferenceEquals(c, Connection.ClientConnection)) continue;
				(IsNonSim(c) ? real : simPlayback).Add(c);
			}
			var ordered = new List<Connection>(real);
			ordered.AddRange(simPlayback);
			try { if (Connection.ClientConnection != null && IsConnected(Connection.ClientConnection)) ordered.Add(Connection.ClientConnection); }
			catch { }
			return ordered;
		}

		/// <summary>Provider enum text + a generic ordinal label for a report, e.g. "connection 1 (Provider31,
		/// Simulation)". The provider text and label are what may ever land in a repo file; the connection's own
		/// NAME (here, at the end) is a runtime value on the user's own machine — never write it into source or docs.</summary>
		private static string Data_ConnLabel(Connection c, int ordinal)
		{
			string provider = "unknown", name = null;
			try { if (c != null && c.Options != null) { provider = c.Options.Provider.ToString(); name = c.Options.Name; } }
			catch { }
			return "connection " + ordinal + " (" + provider + (name == null ? "" : ", " + name) + ")";
		}

		private static void Data_HistDay(Data_Job job, string kind, DateTime day, string ds)
		{
			var wanted = job.Types.Where(t => job.Overwrite || !Data_HasDay(kind, job.InstrumentName, day, t)).ToArray();
			if (wanted.Length == 0) { Data_Add(job.Skipped, ds + "/" + kind); return; }

			var th = job.Instrument.MasterInstrument.TradingHours;
			Func<Collection<Data.Bars>> freshColl = () =>
			{
				var c = new Collection<Data.Bars>();
				foreach (var t in wanted)
				{
					var bp = new Data.BarsPeriod
					{
						BarsPeriodType	= Data_PeriodType(kind),
						Value			= 1,
						MarketDataType	= (Data.MarketDataType)Enum.Parse(typeof(Data.MarketDataType), t, true)
					};
					c.Add(new Data.Bars(job.Instrument, bp, day.Date, day.Date.AddDays(1).AddSeconds(-1), th));
				}
				return c;
			};

			var attempts = new List<string>();

			// (a) DownloadFromProvider on each connected candidate — a fresh Bars collection per try, never the
			// same instance handed to two connections. ClientConnection tried last: it has no entitlement when
			// the real data comes from a broker adapter.
			var candidates = Data_HistCandidates();
			for (int i = 0; i < candidates.Count; i++)
			{
				string label = Data_ConnLabel(candidates[i], i + 1);
				bool ok = false;
				try
				{
					using (var done = new ManualResetEventSlim(false))
					{
						// showErrors:false matters — true plausibly raises a modal on the UI thread from this
						// background job. This is exactly what the Historical Data window's Download button
						// calls (HistoricalData.cs:442,539), just with the connection this module picked instead
						// of a hardcoded ClientConnection.
						Data.BarsSeries.DownloadFromProvider(freshColl(), job.Overwrite, false, null, candidates[i], false, false,
							result => { ok = result; try { done.Set(); } catch { } });
						if (!done.Wait(Data_DateTimeout))
						{
							attempts.Add(label + ": did not answer in "
								+ Data_DateTimeout.TotalSeconds.ToString("F0", CultureInfo.InvariantCulture) + "s");
							continue;
						}
					}
				}
				catch (Exception ex) { attempts.Add(label + ": " + Deep(ex)); continue; }

				if (!ok) { attempts.Add(label + ": download reported failure for " + string.Join(",", wanted)); continue; }

				// Trust the store, not the callback: a "true" with nothing on disk is a silent gap later.
				var written = wanted.Where(t => Data_HasDay(kind, job.InstrumentName, day, t)).ToArray();
				if (written.Length == 0) { attempts.Add(label + ": reported success but wrote no .ncd for " + string.Join(",", wanted)); continue; }
				Data_Add(job.Downloaded, ds + "/" + kind + " (" + string.Join(",", written) + ") via " + label);
				return;
			}

			// (b) No candidate connection answered DownloadFromProvider. Fall back to the path a backtest
			// already proves works: ask for the bars the way a chart does (Bars.GetBars, LookupPolicies.Provider
			// | Repository) with NO connection argument — there is none on this API; NinjaTrader resolves it
			// internally. We still trust only a coverage re-read afterward, never the callback alone.
			string fbError;
			if (Data_HistFallback(job, kind, day, wanted, out fbError))
			{
				var written = wanted.Where(t => Data_HasDay(kind, job.InstrumentName, day, t)).ToArray();
				Data_Add(job.Downloaded, ds + "/" + kind + " (" + string.Join(",", written) + ") via fallback GetBars (connection chosen by NinjaTrader)");
				return;
			}
			if (fbError != null) attempts.Add("fallback GetBars: " + fbError);

			throw new Exception(kind + " download failed for " + string.Join(",", wanted) + " — "
				+ (attempts.Count == 0 ? "no connected data connection available" : string.Join(" | ", attempts))
				+ " — a backtest fetches missing bars on demand from the connected provider");
		}

		/// <summary>(b): Bars.GetBars per wanted type, no explicit connection (the public API has none — see
		/// Data_HistCandidates). Returns true only once a coverage re-read confirms the day landed on disk;
		/// "no error, nothing on disk" is reported as a failure, not a success (NinjaTrader may have served the
		/// bars from memory without persisting them — this cannot be told apart from the callback alone).</summary>
		private static bool Data_HistFallback(Data_Job job, string kind, DateTime day, string[] wanted, out string error)
		{
			error = null;
			var th = job.Instrument.MasterInstrument.TradingHours;
			foreach (var t in wanted)
			{
				var bp = new Data.BarsPeriod
				{
					BarsPeriodType	= Data_PeriodType(kind),
					Value			= 1,
					MarketDataType	= (Data.MarketDataType)Enum.Parse(typeof(Data.MarketDataType), t, true)
				};
				ErrorCode ec = ErrorCode.NoError;
				string emsg = null;
				using (var done = new ManualResetEventSlim(false))
				{
					Data.Bars.GetBars(job.Instrument, bp, day.Date, day.Date.AddDays(1).AddSeconds(-1), th,
						false, false, false, false,
						LookupPolicies.Provider | LookupPolicies.Repository, MergePolicy.DoNotMerge,
						false, null, false, null,
						(b, code, msg, state) => { ec = code; emsg = msg; try { done.Set(); } catch { } });
					if (!done.Wait(Data_DateTimeout))
					{
						error = kind + "/" + t + ": did not answer in "
							+ Data_DateTimeout.TotalSeconds.ToString("F0", CultureInfo.InvariantCulture) + "s";
						return false;
					}
				}
				if (ec != ErrorCode.NoError)
				{
					error = kind + "/" + t + ": " + ec + (string.IsNullOrEmpty(emsg) ? "" : " " + emsg);
					return false;
				}
			}
			var written = wanted.Where(t => Data_HasDay(kind, job.InstrumentName, day, t)).ToArray();
			if (written.Length == 0)
			{
				error = "no error, but wrote no .ncd for " + string.Join(",", wanted)
					+ " — NinjaTrader may have served it from memory without persisting to disk";
				return false;
			}
			return true;
		}

		// ── /data/probe: how far back the connected provider serves, by a bounded binary search ────
		// The search itself (below, Data_ProbeRun) runs on its own one-shot background thread, never
		// inline on the HttpListener thread pool that also serves every other endpoint — a slow or half-dead
		// provider could otherwise pin a pool worker for up to Data_ProbeCapWall + Data_ProbeRequestTimeout
		// (~110s). GET /data/probe answers 202 {id,state:"queued"} at once, same shape as /data/download;
		// poll GET /data/probe/{id}.
		private static readonly object Data_probeGate = new object();
		private static readonly Dictionary<string, Data_ProbeJob> Data_probeJobs = new Dictionary<string, Data_ProbeJob>(StringComparer.OrdinalIgnoreCase);
		private static int Data_probeCounter;

		private sealed class Data_ProbeJob
		{
			public string			Id;
			public volatile string	State = "queued";		// queued|running|done|error
			public string			ResultJson;				// the finished response body, set once State != queued|running
			public DateTime		QueuedAt = DateTime.Now, StartedAt, FinishedAt;
		}

		private static Data_ProbeJob Data_ProbeFind(string id) { lock (Data_probeGate) { Data_ProbeJob j; return Data_probeJobs.TryGetValue(id, out j) ? j : null; } }

		/// <summary>Round back to the nearest weekday (probing a Saturday/Sunday only wastes the request cap —
		/// there is no session on either day for any instrument this module covers).</summary>
		private static DateTime Data_PrevWeekday(DateTime d)
		{
			d = d.Date;
			while (d.DayOfWeek == DayOfWeek.Saturday || d.DayOfWeek == DayOfWeek.Sunday) d = d.AddDays(-1);
			return d;
		}

		/// <summary>One small Bars.GetBars for a single day; null means "no answer within the per-request
		/// timeout", which the caller must treat as inconclusive, never as "no data".</summary>
		private static bool? Data_ProbeDay(Instrument inst, Data.BarsPeriodType bpType, Data.TradingHours th, DateTime day)
		{
			var bp = new Data.BarsPeriod { BarsPeriodType = bpType, Value = 1, MarketDataType = Data.MarketDataType.Last };
			bool has = false, answered = false;
			using (var done = new ManualResetEventSlim(false))
			{
				Data.Bars.GetBars(inst, bp, day.Date, day.Date.AddDays(1).AddSeconds(-1), th,
					false, false, false, false,
					LookupPolicies.Provider | LookupPolicies.Repository, MergePolicy.DoNotMerge,
					false, null, false, null,
					(b, code, msg, state) =>
					{
						answered = true;
						try
						{
							// Count > 0 is not enough: NinjaTrader can answer with bars from OUTSIDE the asked day.
							// Only a bar stamped inside the day (session end may fall on the next date) counts.
							if (code == ErrorCode.NoError && b != null)
								for (int i = 0; i < b.Count && !has; i++)
								{
									DateTime t = b.GetTime(i);
									has = t >= day.Date && t < day.Date.AddDays(2);
								}
						}
						catch { has = false; }
						try { done.Set(); } catch { }
					});
				done.Wait(Data_ProbeRequestTimeout);
			}
			return answered ? (bool?)has : null;
		}

		/// <summary>GET /data/probe: validates on the request thread and answers 202 {id,state:"queued"} at
		/// once — the actual bounded binary search (never more than Data_ProbeCapRequests requests or
		/// Data_ProbeCapWall wall time; a slow provider can still add up to Data_ProbeRequestTimeout per
		/// request in flight) runs on its own one-shot background thread, never inline on the
		/// HttpListener thread pool that also serves every other endpoint. Poll GET /data/probe/{id}, same
		/// shape as /data/download. No arming file — a probe moves no money and writes at most a handful of
		/// days into the local store as a side effect of Bars.GetBars, same as Data_HistFallback. Still
		/// refuses on real exposure and without a real data provider connected, same as /data/download.</summary>
		private static string Data_ProbeStart(System.Collections.Specialized.NameValueCollection q, ref int status)
		{
			string name = (q["instrument"] ?? "").Trim();
			if (name.Length == 0) return Err(ref status, 400, "instrument is required, e.g. /data/probe?instrument=ES%2012-26&kind=minute");
			string kind = (q["kind"] ?? "").Trim().ToLowerInvariant();
			if (kind != "minute" && kind != "tick" && kind != "day")
				return Err(ref status, 400, "kind must be one of minute|tick|day");

			Instrument inst = null;
			try { inst = Instrument.GetInstrument(name); } catch (Exception ex) { return Err(ref status, 400, "instrument '" + name + "': " + Deep(ex)); }
			if (inst == null) return Err(ref status, 400, "unknown instrument '" + name + "'");

			string exposure = Data_Exposure();
			if (exposure != null)
			{
				status = 409;
				return Obj(P("error", Q("data probe refused: " + exposure + " — flatten and cancel first")), P("exposure", "true"));
			}
			if (!AnyNonSimConnected())
			{
				status = 409;
				return Obj(P("error", Q("data probe needs a real data provider connected (see /health.connections)")),
					P("anyLive", AnyLiveConnected() ? "true" : "false"), P("anyNonSim", "false"));
			}

			var job = new Data_ProbeJob { Id = "p" + Interlocked.Increment(ref Data_probeCounter) };
			lock (Data_probeGate) Data_probeJobs[job.Id] = job;
			var t = new Thread(() => Data_ProbeRun(job, inst, name, kind)) { IsBackground = true, Name = "NT8Bridge-probe-" + job.Id };
			t.Start();
			status = 202;
			return Obj(P("id", Q(job.Id)), P("state", Q("queued")));
		}

		private static string Data_ProbeStatus(string id, ref int status)
		{
			var job = Data_ProbeFind(id);
			if (job == null) return Err(ref status, 404, "no data probe '" + id + "'");
			if (job.State == "queued" || job.State == "running") return Obj(P("id", Q(job.Id)), P("state", Q(job.State)));
			return job.ResultJson;
		}

		/// <summary>How far back the connected provider serves one instrument at one resolution, found with a
		/// bounded binary search. Runs entirely on its own background thread (Data_ProbeStart above);
		/// never throws out to it, an exception here becomes job.State == "error" instead of an unhandled
		/// exception on a bare thread (which would take the whole NT8 process down).
		/// ponytail: the "boundary is monotonic" assumption (once found, every later day also has data) is a
		/// heuristic, not a guarantee — a provider with a genuine mid-history gap would report a boundary later
		/// than the true earliest day. Upgrade to a full weekday scan if that is ever seen in practice.</summary>
		private static void Data_ProbeRun(Data_ProbeJob job, Instrument inst, string name, string kind)
		{
			job.StartedAt = DateTime.Now;
			job.State = "running";
			try
			{
				var th = inst.MasterInstrument.TradingHours;
				var bpType = Data_PeriodType(kind);
				DateTime today = Data_TodayEt();
				DateTime hi = Data_PrevWeekday(today.AddDays(-1));		// most recent testable (non-current, non-future) day
				int requests = 0;
				var sw = System.Diagnostics.Stopwatch.StartNew();

				Func<DateTime, bool?> probe = d =>
				{
					if (requests >= Data_ProbeCapRequests || sw.Elapsed >= Data_ProbeCapWall) return null;
					requests++;
					return Data_ProbeDay(inst, bpType, th, d);
				};

				Func<int, bool, string, string> result = (used, capReached, note) => Obj(
					P("id", Q(job.Id)), P("state", Q("done")),
					P("instrument", Q(name)), P("instrumentResolved", Q(inst.FullName)), P("kind", Q(kind)),
					P("requestsUsed", I(used)), P("capRequests", I(Data_ProbeCapRequests)), P("capReached", capReached ? "true" : "false"),
					P("earliestDate", "null"), P("depthDays", "null"), P("note", Q(note)));

				bool? atHi = probe(hi);
				if (atHi == null)
				{
					job.ResultJson = result(requests, false, "no answer from the connected provider for "
						+ hi.ToString("yyyyMMdd", CultureInfo.InvariantCulture) + " within "
						+ Data_ProbeRequestTimeout.TotalSeconds.ToString("F0", CultureInfo.InvariantCulture) + "s — cannot probe depth");
				}
				else if (atHi == false)
				{
					job.ResultJson = Obj(P("id", Q(job.Id)), P("state", Q("done")),
						P("instrument", Q(name)), P("instrumentResolved", Q(inst.FullName)), P("kind", Q(kind)),
						P("requestsUsed", I(requests)), P("capRequests", I(Data_ProbeCapRequests)), P("capReached", "false"),
						P("earliestDate", "null"), P("depthDays", I(0)),
						P("note", Q("no " + kind + " data from the connected provider even for " + hi.ToString("yyyyMMdd", CultureInfo.InvariantCulture))));
				}
				else
				{
					DateTime lo = Data_PrevWeekday(hi.AddDays(-Data_ProbeCapDays));
					bool? atLo = probe(lo);
					if (atLo == true)
					{
						job.ResultJson = result(requests, true, "at least " + Data_ProbeCapDays + " days back (search cap reached) — the connected provider may serve more");
					}
					else
					{
						DateTime good = hi, bad = lo;		// good: known to have data; bad: no data OR no answer, treated as the floor
						while (requests < Data_ProbeCapRequests && sw.Elapsed < Data_ProbeCapWall)
						{
							int spanDays = (int)(good - bad).TotalDays;
							if (spanDays <= 1) break;
							DateTime mid = Data_PrevWeekday(bad.AddDays(spanDays / 2));
							if (mid <= bad || mid >= good) break;
							bool? has = probe(mid);
							if (has == null) break;			// no answer — stop, report what is known so far
							if (has == true) good = mid; else bad = mid;
						}
						bool capHit = requests >= Data_ProbeCapRequests || sw.Elapsed >= Data_ProbeCapWall;
						int depthDays = (int)(today.Date - good.Date).TotalDays;
						job.ResultJson = Obj(P("id", Q(job.Id)), P("state", Q("done")),
							P("instrument", Q(name)), P("instrumentResolved", Q(inst.FullName)), P("kind", Q(kind)),
							P("requestsUsed", I(requests)), P("capRequests", I(Data_ProbeCapRequests)), P("capReached", "false"),
							P("earliestDate", Q(good.ToString("yyyyMMdd", CultureInfo.InvariantCulture))), P("depthDays", I(depthDays)),
							P("note", Q(capHit
								? "search stopped at its request/time cap before narrowing further — depth is approximate, no earlier than this date"
								: "binary search over Bars.GetBars; NinjaTrader resolves the connection itself")));
					}
				}
				job.State = "done";
			}
			catch (Exception ex)
			{
				job.ResultJson = Obj(P("id", Q(job.Id)), P("state", Q("error")), P("error", Q(Deep(ex))));
				job.State = "error";
			}
			finally { job.FinishedAt = DateTime.Now; }
		}
	}
}
