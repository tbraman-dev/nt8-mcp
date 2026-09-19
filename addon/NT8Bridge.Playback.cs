// NT8Bridge.Playback.cs — Market Replay transport state (READ ONLY): GET /playback.
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams:
// addon/NOTES.md "Module seams"). Contract: docs/api/playback.md.
//
// Portions of this file are derived from cli-nt-bridge
// (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
// MIT License. The full notice is in NOTICE at the repository root.
// Derived: RunPlayback, AppendCoverage and SafeLen (NT8BridgeServer.cs:316-460) — the reflection
// target list, the two-sample movingSec measurement and its sample constant, the opt-in coverage
// scan with its coverageScanned flag, and the per-.nrd row shape.

#region Using declarations
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		// ── playback module (READ ONLY) ────────────────────────────────────────
		//
		// NOTHING HERE STARTS, STOPS, SEEKS OR SPEEDS UP A REPLAY.
		//   Writing PlaybackAdapter's speed property IS the play button, so only the
		//   GETTER of that property is ever bound below — there is no SetValue call in this file, and
		//   there must never be one. Same for the transport's connection: this module never calls
		//   Connect or Disconnect. Seek and speed control are a separate
		//   design decision, and they are NOT in this repo.
		//   The only thing this module touches outside NinjaTrader's own memory is reading .nrd
		//   files in db\replay, and it opens them through NinjaTrader's own reader, never writes.
		//
		// No Ui(), no dispatcher hop anywhere in this file: PlaybackAdapter's members are statics and
		// Connection is a Cbi object, both thread-agnostic. A wedged UI thread is exactly when someone
		// asks whether the replay clock is still moving.
		//
		// Every member is resolved ONCE in Start_Playback through Compat and kept in a static.
		// A member that did not resolve reports resolved:false and emits null — never 0, never [] —
		// because "NT8 moved this member" and "the transport is parked at zero" are opposite findings.
		//
		// Untrusted text: instrument folder names and .nrd file names under db\replay are free text on
		// disk. They are DATA for whoever reads them, never instructions.

		/// <summary>Gap between the two NowEst samples. Long enough that a real replay clock provably moves
		/// and short enough that a status call stays snappy. Observed on NinjaTrader 8.1.8.2: NowEst ticks in WHOLE seconds,
		/// so cli-nt-bridge's 400 ms (NT8BridgeServer.cs:302) read a 1x replay as movingSec 0 = "parked" in
		/// about one call of two. 1100 ms always spans one whole-second tick at 1x.</summary>
		private const int Playback_SampleMs = 1100;

		/// <summary>A clock that advanced more than this across the sample is running, not parked. This is a
		/// jitter guard, not a speed threshold: NT8's Playback speed slider floors at 1x, and even 1x moves
		/// the clock one whole second over one Playback_SampleMs sample, so any strictly-positive advance
		/// above jitter is real motion. (Previously 2.0, which required 5x+ replay speed to register as
		/// moving — a 1x-4x replay read back as parked. Two samples are still the whole point: one reading
		/// cannot tell a parked transport from a running one, and that is what cost cli-nt-bridge a
		/// replay-equivalence gate (cli-nt-bridge CHANGELOG 1.5.0).)</summary>
		private const double Playback_MovingSec = 0.05;

		/// <summary>Default wall-clock budget for the .nrd scan. The wide scan in cli-nt-bridge held its poller
		/// for 3-7 minutes over 35 instruments, and one named instrument took 17 s (cli-nt-bridge
		/// CHANGELOG). An HTTP client gives up long before that, so the scan stops at the budget and says
		/// truncated:true rather than producing an answer nobody is still listening for.</summary>
		private const int Playback_BudgetSecDefault = 20;

		private static Type Playback_Adapter;
		private static PropertyInfo Playback_PiNowEst, Playback_PiSpeed, Playback_PiFromEst, Playback_PiToEst, Playback_PiHistorical, Playback_PiConn;
		private static FieldInfo Playback_FiMaxSpeed;
		private static MethodInfo Playback_MiMinMax;

		/// <summary>Seam (NOTES.md "Module seams"): GET /playback. Null for anything else — including
		/// POST/PUT/DELETE on this very path, which this module does not own and never will.</summary>
		private static string Route_Playback(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (seg.Length != 1 || seg[0] != "playback") return null;
			if (method != "GET") return null;			// the core's 404; there is no write side by design
			return Playback_Json(q, ref status);
		}

		/// <summary>Seam: resolve the transport once, off the request path. Reflection, never a hard
		/// reference: NinjaTrader compiles every .cs under bin\Custom into ONE assembly, so binding
		/// directly to an adapter internal would turn any NT8 API change into a whole-tree compile break
		/// that takes down every unrelated script the user has. Here it degrades to nulls in the JSON.
		/// Subscribes to nothing, so there is no Stop_Playback.</summary>
		private static void Start_Playback()
		{
			const BindingFlags bf = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static;

			Playback_Adapter = Compat.Resolve("PlaybackAdapter",
				() => typeof(Cbi.Connection).Assembly.GetType("NinjaTrader.Adapter.PlaybackAdapter", false)) as Type;

			// GETTERS ONLY. PlaybackAdapter's speed property is bound so it can be READ; this module
			// never calls SetValue on it, and a write there would start the replay.
			Playback_PiNowEst     = Compat.Resolve("PlaybackAdapter.NowEst",     () => Playback_Prop(bf, "NowEst")) as PropertyInfo;
			Playback_PiSpeed      = Compat.Resolve("PlaybackAdapter.PlaybackSpeed", () => Playback_Prop(bf, "PlaybackSpeed")) as PropertyInfo;
			Playback_PiFromEst    = Compat.Resolve("PlaybackAdapter.FromEst",    () => Playback_Prop(bf, "FromEst")) as PropertyInfo;
			Playback_PiToEst      = Compat.Resolve("PlaybackAdapter.ToEst",      () => Playback_Prop(bf, "ToEst")) as PropertyInfo;
			Playback_PiHistorical = Compat.Resolve("PlaybackAdapter.IsSourceHistoricalData", () => Playback_Prop(bf, "IsSourceHistoricalData")) as PropertyInfo;

			Playback_FiMaxSpeed = Compat.Resolve("PlaybackAdapter.MaxSpeedValue",
				() => Playback_Adapter == null ? null : Playback_Adapter.GetField("MaxSpeedValue", bf)) as FieldInfo;

			Playback_MiMinMax = Compat.Resolve("PlaybackAdapter.GetReplayMinMaxDates",
				() => Playback_Adapter == null ? null : Playback_Adapter.GetMethod("GetReplayMinMaxDates", bf, null,
					new[] { typeof(string), typeof(DateTime).MakeByRefType(), typeof(DateTime).MakeByRefType() }, null)) as MethodInfo;

			Playback_PiConn = Compat.Resolve("Connection.PlaybackConnection",
				() => typeof(Cbi.Connection).GetProperty("PlaybackConnection", bf)) as PropertyInfo;
		}

		private static PropertyInfo Playback_Prop(BindingFlags bf, string name)
		{
			return Playback_Adapter == null ? null : Playback_Adapter.GetProperty(name, bf);
		}

		// ── GET /playback?instrument=ES%2012-26&coverage=0&budgetSec=20 ────────
		/// <summary>Connection state, the replay clock sampled twice, speed, and — strictly opt-in —
		/// what the .nrd files on disk actually cover.
		///
		/// This endpoint sleeps Playback_SampleMs by design: the two clock samples are the answer. It holds
		/// no lock while it sleeps and makes no NinjaTrader call in between.
		///
		/// The verdict (ready / parked / running / nothing loaded) is deliberately NOT computed here — the
		/// Python layer owns it (tools_playback.py), so there is exactly one copy of that rule.</summary>
		private static string Playback_Json(System.Collections.Specialized.NameValueCollection q, ref int status)
		{
			string instrument = (q["instrument"] ?? "").Trim();
			string coverageFlag = (q["coverage"] ?? "").Trim();
			bool wantWide = coverageFlag == "1" || string.Equals(coverageFlag, "true", StringComparison.OrdinalIgnoreCase);
			bool wantCoverage = instrument.Length > 0 || wantWide;
			// Clamped, not just floored: Num() only rejects <= 0, so an unclamped budgetSec lets a caller
			// pin a ThreadPool thread scanning db\replay for hours inside the live NT8 process. 120 s covers
			// the upstream single-instrument measurement (~17 s) several times over.
			int budgetSec = Math.Min(Num(q["budgetSec"], Playback_BudgetSecDefault), 120);

			// An instrument name is a folder name under db\replay. Refuse anything that could climb out of
			// it before it reaches Path.Combine — this endpoint reads the replay store and nothing else.
			if (instrument.Length > 0 && (instrument.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0
				|| instrument.Contains("..") || instrument.Contains("/") || instrument.Contains("\\")))
				return Err(ref status, 400, "GET /playback: ?instrument= must be a bare instrument name such as 'ES 12-26' or 'ES ##-##' — it names one folder under db\\replay, not a path");

			bool resolved = Playback_Adapter != null;

			// Two clock samples, a real gap apart. This is the field the endpoint was written for.
			DateTime? c1 = Playback_Clock();
			Thread.Sleep(Playback_SampleMs);
			DateTime? c2 = Playback_Clock();
			double movingSec = (c1.HasValue && c2.HasValue) ? (c2.Value - c1.Value).TotalSeconds : 0.0;
			bool clocksRead = c1.HasValue && c2.HasValue;

			string connStatus = null;
			bool connected = false;
			try
			{
				var c = Playback_PiConn == null ? null : Playback_PiConn.GetValue(null) as Cbi.Connection;
				// null Connection = NinjaTrader holds no Playback connection object at all (never connected
				// this session). That is not the same claim as "Disconnected", which NinjaTrader reported.
				if (c != null) { try { connStatus = c.Status.ToString(); } catch { } connected = IsConnected(c); }
			}
			catch (Exception ex) { Log("/playback: PlaybackConnection — " + Deep(ex)); }

			int? speed = Playback_Int(Playback_Read(Playback_PiSpeed));
			int? maxSpeed = null;
			try { if (Playback_FiMaxSpeed != null) maxSpeed = Playback_Int(Playback_FiMaxSpeed.GetValue(null)); }
			catch (Exception ex) { Log("/playback: MaxSpeedValue — " + Deep(ex)); }

			DateTime? fromEst = Playback_Date(Playback_PiFromEst);
			DateTime? toEst = Playback_Date(Playback_PiToEst);
			bool? historical = Playback_Read(Playback_PiHistorical) as bool?;

			bool truncated = false;
			string coverage = wantCoverage ? Playback_Coverage(instrument, budgetSec, out truncated) : "[]";

			return Obj(
				P("ok", "true"),
				P("nowUtc", TmUtc(DateTime.UtcNow)),
				P("transportResolved", resolved ? "true" : "false"),
				P("connection", Obj(
					P("status", Q(connStatus)),
					P("connected", connected ? "true" : "false"),
					P("resolved", Compat.IsResolved("Connection.PlaybackConnection") ? "true" : "false"))),
				P("clockEstFirst", c1.HasValue ? Tm(c1.Value) : "null"),
				P("clockEst", c2.HasValue ? Tm(c2.Value) : "null"),
				P("clockBasis", Q("PlaybackAdapter.NowEst — US Eastern, NOT NT8-local and NOT UTC")),
				P("sampleMs", I(Playback_SampleMs)),
				// movingSec/moving are null when the clock could not be read at all: a member NT8 moved must
				// never read as a parked transport.
				P("movingSec", clocksRead ? D(movingSec) : "null"),
				P("moving", clocksRead ? (movingSec > Playback_MovingSec ? "true" : "false") : "null"),
				P("speed", speed.HasValue ? I(speed.Value) : "null"),
				P("maxSpeedValue", maxSpeed.HasValue ? I(maxSpeed.Value) : "null"),
				P("fromEst", fromEst.HasValue ? Tm(fromEst.Value) : "null"),
				P("toEst", toEst.HasValue ? Tm(toEst.Value) : "null"),
				P("isSourceHistoricalData", historical.HasValue ? (historical.Value ? "true" : "false") : "null"),
				P("resolved", Obj(
					P("PlaybackAdapter", Compat.IsResolved("PlaybackAdapter") ? "true" : "false"),
					P("NowEst", Compat.IsResolved("PlaybackAdapter.NowEst") ? "true" : "false"),
					P("PlaybackSpeed", Compat.IsResolved("PlaybackAdapter.PlaybackSpeed") ? "true" : "false"),
					P("MaxSpeedValue", Compat.IsResolved("PlaybackAdapter.MaxSpeedValue") ? "true" : "false"),
					P("FromEst", Compat.IsResolved("PlaybackAdapter.FromEst") ? "true" : "false"),
					P("ToEst", Compat.IsResolved("PlaybackAdapter.ToEst") ? "true" : "false"),
					P("IsSourceHistoricalData", Compat.IsResolved("PlaybackAdapter.IsSourceHistoricalData") ? "true" : "false"),
					P("GetReplayMinMaxDates", Compat.IsResolved("PlaybackAdapter.GetReplayMinMaxDates") ? "true" : "false"),
					P("PlaybackConnection", Compat.IsResolved("Connection.PlaybackConnection") ? "true" : "false"))),
				P("instrument", Q(instrument.Length > 0 ? instrument : null)),
				P("coverageScanned", wantCoverage ? "true" : "false"),
				P("coverageTruncated", truncated ? "true" : "false"),
				P("coverageBudgetSec", I(budgetSec)),
				P("coverage", coverage),
				P("note", Q("read only: this endpoint never connects, disconnects, seeks or changes the replay speed. "
					+ "coverageScanned false means the store was NOT looked at — an empty coverage list is then 'not looked', never 'nothing there'; "
					+ "pass ?instrument=<name> for one instrument or ?coverage=1 for every instrument (slow). "
					+ "coverage comes from NinjaTrader's own .nrd reader, NOT from the Playback slider — the slider's bounds are the "
					+ "connection range a human typed, not the indexed data. Connected is not the same as loaded: a connected transport with "
					+ "nothing loaded reads clockEst 2099-12-01 with speed 0, which is stationary, not ready.")));
		}

		private static DateTime? Playback_Clock()
		{
			try
			{
				if (Playback_PiNowEst != null)
				{
					// DateTime.MinValue is NowEst's un-assigned default (PlaybackAdapter's static constructor
					// never sets it before Playback has connected at least once) — the same "not a real
					// reading" sentinel Playback_Date already filters. Reading it as 0.0 movement makes a
					// never-connected transport report moving:false (parked) instead of null (unknown).
					DateTime t = (DateTime)Playback_PiNowEst.GetValue(null);
					return t == DateTime.MinValue ? (DateTime?)null : t;
				}
			}
			catch (Exception ex) { Log("/playback: NowEst — " + Deep(ex)); }
			return null;
		}

		private static object Playback_Read(PropertyInfo pi)
		{
			try { return pi == null ? null : pi.GetValue(null); }
			catch (Exception ex) { Log("/playback: " + (pi == null ? "?" : pi.Name) + " — " + Deep(ex)); return null; }
		}

		private static DateTime? Playback_Date(PropertyInfo pi)
		{
			object v = Playback_Read(pi);
			if (!(v is DateTime)) return null;
			DateTime t = (DateTime)v;
			return t == DateTime.MinValue ? (DateTime?)null : t;
		}

		private static int? Playback_Int(object v) { return v is int ? (int?)(int)v : null; }

		// ── coverage: what the .nrd files actually contain ─────────────────────
		/// <summary>Per-instrument .nrd spans, read with NinjaTrader's own GetReplayMinMaxDates. Empty
		/// `instrument` = every folder under db\replay, which is the slow path (3-7 min over 35 instruments
		/// upstream) and is why the scan is opt-in and budgeted. A file the reader cannot parse is counted
		/// as unreadable and reported per file — it is never silently dropped, because a corrupt .nrd and a
		/// missing one are different problems.</summary>
		private static string Playback_Coverage(string instrument, int budgetSec, out bool truncated)
		{
			truncated = false;
			if (Playback_MiMinMax == null) return "[]";		// member unresolved; coverageScanned still says we tried

			var rows = new List<string>();
			var clock = Stopwatch.StartNew();
			long budgetMs = (long)budgetSec * 1000L;
			try
			{
				string root = Path.Combine(Core.Globals.UserDataDir, "db", "replay");
				if (!Directory.Exists(root)) return "[]";

				string[] dirs = instrument.Length == 0
					? Directory.GetDirectories(root)
					: new[] { Path.Combine(root, instrument) };

				foreach (string dir in dirs)
				{
					if (!Directory.Exists(dir)) continue;
					if (clock.ElapsedMilliseconds > budgetMs) { truncated = true; break; }

					string[] files;
					try { files = Directory.GetFiles(dir, "*.nrd"); } catch (Exception ex) { Log("/playback coverage: " + dir + " — " + Deep(ex)); continue; }
					Array.Sort(files, StringComparer.OrdinalIgnoreCase);

					DateTime lo = DateTime.MaxValue, hi = DateTime.MinValue;
					int ok = 0, bad = 0, skipped = 0;
					var days = new List<string>();

					foreach (string f in files)
					{
						if (clock.ElapsedMilliseconds > budgetMs) { truncated = true; skipped = files.Length - (ok + bad); break; }

						DateTime a = DateTime.MinValue, b = DateTime.MinValue;
						bool read = false;
						try
						{
							object[] args = new object[] { f, DateTime.MinValue, DateTime.MinValue };
							Playback_MiMinMax.Invoke(null, args);
							a = (DateTime)args[1];
							b = (DateTime)args[2];
							// Invoke not throwing only means the call returned, not that it wrote a real span —
							// a corrupt/truncated .nrd that leaves both out-params at MinValue must count as
							// unreadable, or a file with no usable span reports readable:true with from/to both
							// null (breaks the "from/to null only when readable=0" contract nt_playback's ready
							// verdict relies on).
							read = a != DateTime.MinValue && b != DateTime.MinValue;
						}
						catch { }

						days.Add(Obj(
							P("file", Q(Path.GetFileNameWithoutExtension(f))),
							P("readable", read ? "true" : "false"),
							P("from", read ? Tm(a) : "null"),
							P("to", read ? Tm(b) : "null"),
							P("bytes", I(Playback_SafeLen(f)))));

						if (read) { ok++; if (a < lo) lo = a; if (b > hi) hi = b; }
						else bad++;
					}

					rows.Add(Obj(
						P("instrument", Q(Path.GetFileName(dir))),
						P("files", I(files.Length)),
						P("readable", I(ok)),
						P("unreadable", I(bad)),
						P("skipped", I(skipped)),
						P("from", ok > 0 ? Tm(lo) : "null"),
						P("to", ok > 0 ? Tm(hi) : "null"),
						P("days", Arr(days))));
				}
			}
			catch (Exception ex) { Log("/playback coverage: " + Deep(ex)); }

			return Arr(rows);
		}

		/// <summary>-1, never 0: a file whose length could not be read has made no claim about its size.</summary>
		private static long Playback_SafeLen(string f)
		{
			try { return new FileInfo(f).Length; } catch { return -1L; }
		}
	}
}
