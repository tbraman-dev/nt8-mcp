// NT8Bridge.Data.cs — the data store: GET /data/coverage and the opt-in,
// flag-gated POST /data/download + GET|DELETE /data/download/{id}.
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
		/// <summary>The arming file, beside the AddOn in bin\Custom\AddOns. Stat-checked on EVERY request and
		/// IGNORED once its mtime is older than 24 h: that clause is the whole point — a flag forgotten after one
		/// debugging session must not arm the module forever.</summary>
		private const string Data_FlagName = "data.download.enabled";
		private static readonly TimeSpan Data_FlagMaxAge = TimeSpan.FromHours(24);
		private const int Data_MaxDays = 10;												// wider needs {"big":true}
		private const int Data_MaxDaysBig = 400;		// a season; {"big":true} RAISES the cap, it never removes it
		private static readonly TimeSpan Data_DateTimeout = TimeSpan.FromSeconds(900);		// a heavy MNQ replay day runs 300-460 s
		private static readonly string[] Data_AllKinds = { "tick", "minute", "day", "replay" };
		private static readonly string[] Data_AllTypes = { "Last", "Bid", "Ask" };

		// ── seams (NOTES.md "Module seams") ─────────────────────────────────────
		/// <summary>GET /data/coverage, POST /data/download, GET|DELETE /data/download/{id}. Null for everything else —
		/// including any other /data path, so the core emits the 404.</summary>
		private static string Route_Data(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (seg.Length < 1 || seg[0] != "data") return null;
			if (seg.Length == 2 && seg[1] == "coverage" && method == "GET")		return Data_Coverage(q, ref status);
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

			Data_Flag();		// publishes Data.downloadFlag into /compat at start, before any request

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

		// ── the arming flag ─────────────────────────────────────────────────────
		private static string Data_FlagPath()
		{
			return Path.Combine(Core.Globals.UserDataDir, "bin", "Custom", "AddOns", Data_FlagName);
		}

		/// <summary>Stat the flag NOW — never cached, on every request. Returns true only when the file exists AND its
		/// last write is inside 24 h. Also republishes the answer (and the flag's age) into GET /compat under
		/// `Data.downloadFlag`; /compat is core-owned, so that row is as of the last check, not as of the /compat call.</summary>
		private static bool Data_Flag()
		{
			bool armed = false;
			double ageH = -1;
			string detail;
			try
			{
				string p = Data_FlagPath();
				if (!File.Exists(p)) detail = "absent (" + Data_FlagName + ")";
				else
				{
					ageH = (DateTime.UtcNow - File.GetLastWriteTimeUtc(p)).TotalHours;
					// both sides: a future-dated flag would otherwise arm the gate for ever (same rule as Ops_Flag)
					armed = ageH >= -5.0 / 3600 && ageH <= Data_FlagMaxAge.TotalHours;	// 5 s of clock skew
					detail = (armed ? "armed, age " : ageH < 0 ? "IGNORED (mtime in the FUTURE), age " : "STALE (ignored), age ")
						+ ageH.ToString("F2", CultureInfo.InvariantCulture) + " h of "
						+ Data_FlagMaxAge.TotalHours.ToString("F0", CultureInfo.InvariantCulture) + " h";
				}
			}
			catch (Exception ex) { detail = "stat failed: " + Deep(ex); armed = false; }
			Compat.Set("Data.downloadFlag", armed, detail, null);
			Data_flagAgeHours = ageH;
			return armed;
		}

		private static double Data_flagAgeHours = -1;		// as of the last Data_Flag() call; -1 = absent / unreadable

		private static string Data_FlagJson(bool armed)
		{
			return Obj(P("name", Q(Data_FlagName)), P("armed", armed ? "true" : "false"),
				P("ageHours", Data_flagAgeHours < 0 ? "null" : D(Data_flagAgeHours)),
				P("maxAgeHours", D(Data_FlagMaxAge.TotalHours)));
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

		/// <summary>Validate on the HTTP thread, then queue and answer 202. Guard order is deliberate: the arming flag
		/// first (an unarmed module tells you nothing about the machine), then the request, then the live predicates.</summary>
		private static string Data_DownloadStart(string body, ref int status)
		{
			bool armed = Data_Flag();
			if (!armed) return Err(ref status, 403, "data download not enabled");

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
					P("anyLive", AnyLiveConnected() ? "true" : "false"), P("anyNonSim", "true"), P("flag", Data_FlagJson(true)));
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
			bool armed = Data_Flag();		// stat-checked here too: a reader must see the flag go stale
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
				P("flag", Data_FlagJson(armed)),
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

				// Every guard is re-checked per date, not once at POST time: a position can open, and the
				// flag can go stale, in the minutes a multi-day job runs.
				if (!Data_Flag())
				{
					job.Error = "arming flag " + Data_FlagName + " is absent or stale — stopped";
					lock (Data_gate) if (job.State == "running") job.State = "error";
					return;
				}
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

		private static void Data_HistDay(Data_Job job, string kind, DateTime day, string ds)
		{
			var wanted = job.Types.Where(t => job.Overwrite || !Data_HasDay(kind, job.InstrumentName, day, t)).ToArray();
			if (wanted.Length == 0) { Data_Add(job.Skipped, ds + "/" + kind); return; }

			var th = job.Instrument.MasterInstrument.TradingHours;
			var coll = new Collection<Data.Bars>();
			foreach (var t in wanted)
			{
				var bp = new Data.BarsPeriod
				{
					BarsPeriodType	= Data_PeriodType(kind),
					Value			= 1,
					MarketDataType	= (Data.MarketDataType)Enum.Parse(typeof(Data.MarketDataType), t, true)
				};
				coll.Add(new Data.Bars(job.Instrument, bp, day.Date, day.Date.AddDays(1).AddSeconds(-1), th));
			}

			bool ok = false;
			using (var done = new ManualResetEventSlim(false))
			{
				// showErrors:false matters — true plausibly raises a modal on the UI thread from this background job.
				// This is exactly what the Historical Data window's Download button calls (HistoricalData.cs:442,539).
				Data.BarsSeries.DownloadFromProvider(coll, job.Overwrite, false, null, Connection.ClientConnection, false, false,
					result => { ok = result; try { done.Set(); } catch { } });
				if (!done.Wait(Data_DateTimeout))
					throw new TimeoutException(kind + " download did not answer in "
						+ Data_DateTimeout.TotalSeconds.ToString("F0", CultureInfo.InvariantCulture) + "s");
			}
			if (!ok) throw new Exception(kind + " download reported failure for " + string.Join(",", wanted));

			// Trust the store, not the callback: a "true" with nothing on disk is a silent gap later.
			var written = wanted.Where(t => Data_HasDay(kind, job.InstrumentName, day, t)).ToArray();
			if (written.Length == 0) throw new Exception(kind + " download reported success but wrote no .ncd for " + string.Join(",", wanted));
			Data_Add(job.Downloaded, ds + "/" + kind + " (" + string.Join(",", written) + ")");
		}
	}
}
