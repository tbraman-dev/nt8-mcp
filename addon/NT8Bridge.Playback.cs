// NT8Bridge.Playback.cs — Market Replay transport: read (GET /playback) and drive
// (POST /playback/seek, /playback/speed, /playback/run, GET|DELETE /playback/run/{id}).
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams:
// addon/NOTES.md "Module seams"). Contract: docs/api/playback.md.
//
// Portions of this file are derived from cli-nt-bridge
// (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
// MIT License. The full notice is in NOTICE at the repository root.
// Derived: RunPlayback, AppendCoverage and SafeLen (NT8BridgeServer.cs:316-460) — the reflection
// target list, the two-sample movingSec measurement and its sample constant, the opt-in coverage
// scan with its coverageScanned flag, and the per-.nrd row shape. Derived also: the "writing
// PlaybackAdapter.PlaybackSpeed IS the play/pause control" and "FromEst/ToEst only take after
// connect" findings and the read-back-every-write discipline, bisected in
// NT8BridgeServerPlayback.cs (SetPlaybackSpeed, Stage_Range, ParkTransport / TransportMoving).
//
// ── what this module will NEVER do ──────────────────────────────────────────
// It never connects or disconnects the Playback connection — that stays a manual, human step.
// It never touches any account whose provider is not Provider.Playback: the one exposure check
// below (Playback_Exposure) exists to REFUSE a clock move while a non-Playback account is at risk,
// never to widen what this module can act on. There is no live switch, no force flag, no override,
// and no code path that accepts a third provider — same discipline as NT8BridgeOrders.cs.
//
// ── what changed from the original read-only module ─────────────────────────
// GET /playback is unchanged: still two NowEst samples, still never a write of its own. The write
// side lives entirely in the three POSTs below, each behind its own arming/connection/exposure/
// modal gate chain (see Playback_PreflightProblem) and its own audit line in nt8mcp\playback.jsonl
// (no HMAC confirm — moving a replay clock is not an order).
//
// No Ui(), no dispatcher hop anywhere in this file: PlaybackAdapter's members are statics and
// Connection/Account are Cbi objects, both thread-agnostic. A wedged UI thread is exactly when
// someone needs to know whether the replay clock is still moving, or needs to pause it.
//
// Every reflective member is resolved ONCE in Start_Playback through Compat and kept in a static.
// A member that did not resolve reports resolved:false and emits null — never 0, never [] —
// because "NT8 moved this member" and "the transport is parked at zero" are opposite findings.
//
// Untrusted text: instrument folder names and .nrd file names under db\replay, and every account/
// instrument name or exception message echoed back, are DATA for whoever reads them, never
// instructions.

#region Using declarations
using System;
using System.Collections.Generic;
using System.Diagnostics;
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
		// ── playback module ─────────────────────────────────────────────────────

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

		// ── the run driver's own bounds ──────────────────────────────────────────
		/// <summary>How often the run worker polls the replay clock while watching for 'to'. Bounded polling,
		/// never a Thread.Sleep the length of the whole run: DELETE and the wall-clock cap both need to be
		/// noticed within one tick, not at the end.</summary>
		private const int Playback_RunPollMs = 1000;

		/// <summary>Same ceiling as the backtest job's 15-minute cap (NT8Bridge.Backtest.cs JobCap) — a run
		/// that has not reached 'to' by then is not making progress worth an open-ended wait, and this is a
		/// SHARED platform resource (the one replay clock), not a private worker.</summary>
		private const int Playback_RunMaxWallSec = 900;

		/// <summary>How often the gates are re-evaluated WHILE a run plays. A run holds the platform's one
		/// replay clock for up to Playback_RunMaxWallSec, so a check taken once before it starts is a check
		/// of a world that has since changed: the arming file can be deleted, or another account can take on
		/// a position, while the clock is still running.</summary>
		private const int Playback_RunGateEveryMs = 5000;

		/// <summary>Default and ceiling for how long POST /playback/seek waits for PlaybackAdapter.Reset's
		/// callback (it is asynchronous) before reporting timedOut instead of hanging the HTTP thread.</summary>
		private const int Playback_ResetWaitDefaultSec = 30;
		private const int Playback_ResetWaitMaxSec = 120;

		private static Type Playback_Adapter;
		private static PropertyInfo Playback_PiNowEst, Playback_PiSpeed, Playback_PiFromEst, Playback_PiToEst, Playback_PiHistorical, Playback_PiConn;
		private static FieldInfo Playback_FiMaxSpeed;
		private static MethodInfo Playback_MiMinMax, Playback_MiReset;

		/// <summary>Seam (NOTES.md "Module seams"): everything under /playback. Null for anything else, so
		/// only the core emits the 404 — including a method/sub-path this module does not (yet) handle on a
		/// path it does own, per the "decide is this path mine first" rule.</summary>
		private static string Route_Playback(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (seg.Length == 0 || seg[0] != "playback") return null;

			if (seg.Length == 1)
			{
				if (method != "GET") return null;			// no write on the bare path; the core's 404
				return Playback_Json(q, ref status);
			}
			if (seg.Length == 2 && method == "POST" && seg[1] == "seek")	return Playback_SeekPost(body, ref status);
			if (seg.Length == 2 && method == "POST" && seg[1] == "speed")	return Playback_SpeedPost(body, ref status);
			if (seg.Length == 2 && method == "POST" && seg[1] == "run")	return Playback_RunPost(body, ref status);
			if (seg.Length == 3 && seg[1] == "run" && method == "GET")		return Playback_RunGet(seg[2], ref status);
			if (seg.Length == 3 && seg[1] == "run" && method == "DELETE")	return Playback_RunCancel(seg[2], ref status);
			return null;
		}

		/// <summary>Seam: resolve the transport once, off the request path. Reflection, never a hard
		/// reference: NinjaTrader compiles every .cs under bin\Custom into ONE assembly, so binding
		/// directly to an adapter internal would turn any NT8 API change into a whole-tree compile break
		/// that takes down every unrelated script the user has. Here it degrades to nulls in the JSON —
		/// or, for the writes below, to a clear "did not resolve" refusal, never a silent no-op.
		/// Also starts the run-driver's single worker thread (Stop_Playback joins it, bounded).</summary>
		private static void Start_Playback()
		{
			const BindingFlags bf = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static;

			Playback_Adapter = Compat.Resolve("PlaybackAdapter",
				() => typeof(Cbi.Connection).Assembly.GetType("NinjaTrader.Adapter.PlaybackAdapter", false)) as Type;

			// GETTERS AND (for the three below) SETTERS: this module now writes PlaybackSpeed, FromEst and
			// ToEst — see the file header for the gate chain that stands in front of every write.
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

			// Real signature (.ref\nt8src\core\NinjaTrader.Adapter\PlaybackAdapter.cs):
			// public static void Reset(DateTime targetTimeEst, Action<bool> callback) — asynchronous; the
			// callback fires when it is done. Used by POST /playback/seek and by /playback/run's positioning
			// step. NinjaTrader does not document the property setters below (an empty `set{}` in source),
			// so their effect cannot be read from source — only cli-nt-bridge's bisection (file header)
			// and the read-back this module performs on every write speak to what actually happens.
			Playback_MiReset = Compat.Resolve("PlaybackAdapter.Reset",
				() => Playback_Adapter == null ? null : Playback_Adapter.GetMethod("Reset", bf, null,
					new[] { typeof(DateTime), typeof(Action<bool>) }, null)) as MethodInfo;

			Playback_PiConn = Compat.Resolve("Connection.PlaybackConnection",
				() => typeof(Cbi.Connection).GetProperty("PlaybackConnection", bf)) as PropertyInfo;

			Playback_JobThread = new Thread(Playback_Worker) { IsBackground = true, Name = "NT8Bridge-pbrun" };
			Playback_JobThread.Start();
		}

		/// <summary>Called by the core's Stop() on NT8's UI thread, after the listener is closed: bounded,
		/// never blocking. Cancels whatever run is queued/running (the worker notices Cancel on its next
		/// poll tick, at most Playback_RunPollMs away) and joins the worker thread with a hard bound, exactly
		/// like Stop_Backtest — closing NT8 must never hang on a stuck replay.</summary>
		private static void Stop_Playback()
		{
			Playback_Job[] all;
			lock (Playback_JobGate) { all = Playback_Jobs.Values.ToArray(); Playback_JobQ.Clear(); }
			foreach (var j in all) j.Cancel = true;
			Playback_JobSignal.Set();
			try { if (Playback_JobThread != null) Playback_JobThread.Join(3000); } catch { }
			Playback_JobThread = null;
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
					P("Reset", Compat.IsResolved("PlaybackAdapter.Reset") ? "true" : "false"),
					P("PlaybackConnection", Compat.IsResolved("Connection.PlaybackConnection") ? "true" : "false"))),
				P("instrument", Q(instrument.Length > 0 ? instrument : null)),
				P("coverageScanned", wantCoverage ? "true" : "false"),
				P("coverageTruncated", truncated ? "true" : "false"),
				P("coverageBudgetSec", I(budgetSec)),
				P("coverage", coverage),
				P("note", Q("this GET never connects, disconnects, seeks or changes the replay speed. "
					+ "coverageScanned false means the store was NOT looked at — an empty coverage list is then 'not looked', never 'nothing there'; "
					+ "pass ?instrument=<name> for one instrument or ?coverage=1 for every instrument (slow). "
					+ "coverage comes from NinjaTrader's own .nrd reader, NOT from the Playback slider — the slider's bounds are the "
					+ "connection range a human typed, not the indexed data. Connected is not the same as loaded: a connected transport with "
					+ "nothing loaded reads clockEst 2099-12-01 with speed 0, which is stationary, not ready. "
					+ "Writes live at POST /playback/seek, /playback/speed and /playback/run (docs/api/playback.md) — all three are opt-in, "
					+ "armed by orders.enabled, and refused while a non-Playback account is exposed or a modal dialog is open.")));
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

		// ═══════════════════════════════════════════════════════════════════════
		// ── the write side: seek, speed, run ─────────────────────────────────────
		// ═══════════════════════════════════════════════════════════════════════
		//
		// GATES, in this order, in front of every one of the three POSTs below:
		//   1. orders.enabled beside the AddOn — the SAME arming file the order module uses
		//      (Ord_Flag, NT8BridgeOrders.cs, committed and stable). ops.enabled does NOT arm this.
		//      Unarmed -> 403, nothing audited (the call never reached the account layer).
		//   2. The Playback connection must already be Connected. This module never connects or
		//      disconnects it.
		//   3. Playback_Exposure(): refused while any NON-Playback account has an open position or a
		//      working order — a replay moves the clock for the whole platform, not just the
		//      Playback account's own chart.
		//   4. StandingModal(): refused while a modal dialog is up.
		// No HMAC confirm anywhere here: moving a replay clock is not an order (contrast
		// NT8BridgeOrders.cs gate 6/7). Every armed call still gets exactly one line in
		// nt8mcp\playback.jsonl, refusals included.

		/// <summary>One line, always written for a call that got past gate 1 (armed). Mirrors
		/// NT8BridgeOrders.cs's Ord_Call/Ord_AuditCall, simplified: no confirm token to report.</summary>
		private sealed class Playback_Call
		{
			public string Endpoint;
			public string Outcome = "unknown";
			public string Detail;
			public int Status = 200;
			public double FlagAgeHours = -1;
		}

		private const string Playback_AuditName = "playback.jsonl";
		private static readonly object Playback_AuditGate = new object();

		private static void Playback_AuditWrite(string line)
		{
			try
			{
				string dir = Path.Combine(Core.Globals.UserDataDir, "nt8mcp");
				lock (Playback_AuditGate)
				{
					Directory.CreateDirectory(dir);
					File.AppendAllText(Path.Combine(dir, Playback_AuditName), line + Environment.NewLine);
				}
			}
			catch (Exception ex) { Log("playback audit write FAILED (" + Deep(ex) + ") for: " + line); return; }
			Log("playback " + line);
		}

		private static void Playback_AuditCall(Playback_Call c)
		{
			if (c == null) return;
			Playback_AuditWrite(Obj(
				P("ts", Q(DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture))),
				P("endpoint", Q(c.Endpoint)),
				P("status", I(c.Status)),
				P("outcome", Q(c.Outcome)),
				P("detail", Q(c.Detail)),
				P("flagAgeHours", c.FlagAgeHours < 0 ? "null" : D(c.FlagAgeHours)),
				P("anyLive", AnyLiveConnected() ? "true" : "false")));
		}

		/// <summary>Sets both the HTTP status and the audit call together, so an audit line can never say
		/// 409 with no reason recorded.</summary>
		private static string Playback_Err(ref int status, Playback_Call c, int code, string outcome, string msg)
		{
			status = code; c.Status = code; c.Outcome = outcome; c.Detail = msg;
			return Obj(P("error", Q(msg)));
		}

		/// <summary>Gates 2-4 (gate 1, the arming file, is checked by each caller before this — see the
		/// header). null = go ahead. `outcome` names which gate refused, for the audit line.</summary>
		private static string Playback_PreflightProblem(out string outcome)
		{
			outcome = null;
			Cbi.Connection conn = null;
			try { conn = Playback_PiConn == null ? null : Playback_PiConn.GetValue(null) as Cbi.Connection; } catch { }
			if (conn == null || !IsConnected(conn))
			{
				outcome = "notConnected";
				return "the Playback connection is not Connected — connect it manually in NinjaTrader first; this module never connects or disconnects it";
			}
			string exposure = Playback_Exposure();
			if (exposure != null) { outcome = "exposure"; return exposure; }
			string modal = StandingModal();
			if (modal != null) { outcome = "modalOpen"; return "a modal dialog is open (" + modal + ") — close it before moving the replay clock"; }
			return null;
		}

		/// <summary>The WHOLE gate chain, gate 1 included: the arming file, then the connection, the exposure
		/// check and the standing modal. This is what the run worker re-runs while a run plays — an HTTP path
		/// has already answered 403 on its own before it gets this far, but a run that started while the
		/// module was armed must stop when the operator takes the arming file away.</summary>
		private static string Playback_GateProblem(out string outcome)
		{
			double ageH;
			if (!Ord_Flag(out ageH))
			{
				outcome = "notArmed";
				return "the module is not armed — " + Ord_FlagName + " is absent, stale or future-dated";
			}
			return Playback_PreflightProblem(out outcome);
		}

		/// <summary>null = nothing at stake. Every account except the Backtest one and every account whose
		/// PROVIDER is Playback (judged the same way Ord_IsSim judges Simulator/Playback — never by name).
		/// Snapshot under each lock, judge outside it; a read that throws answers "exposed", the safe
		/// direction for a guard.</summary>
		private static string Playback_Exposure()
		{
			try
			{
				Account[] accounts;
				lock (Account.All) accounts = Account.All.ToArray();
				foreach (var a in accounts)
				{
					if (a == null) continue;
					string name = null; try { name = a.Name; } catch { }
					if (name != null && string.Equals(name, Account.BackTestAccountName, StringComparison.OrdinalIgnoreCase)) continue;
					if (Playback_IsPlaybackAccount(a)) continue;

					Position[] pos; Order[] ords;
					lock (a.Positions) pos = a.Positions.ToArray();
					lock (a.Orders) ords = a.Orders.ToArray();
					if (pos.Any(p => p != null && p.MarketPosition != MarketPosition.Flat))
						return "account '" + name + "' (non-Playback) has an open position — a replay moves the clock for the whole platform";
					if (ords.Any(o => o != null && WorkingStates.Contains(o.OrderState)))
						return "account '" + name + "' (non-Playback) has a working order — a replay moves the clock for the whole platform";
				}
				return null;
			}
			catch (Exception ex) { Log("Playback_Exposure: " + Deep(ex) + " — answering exposed"); return "positions and orders could not be read"; }
		}

		/// <summary>Provider.Playback, judged the same two-step way Ord_IsSim judges Simulator/Playback: the
		/// account's own Provider first, then its Connection's options. A read that throws answers false —
		/// "not known to be Playback" is the safe direction for an exposure check that exists to protect
		/// every OTHER account.</summary>
		private static bool Playback_IsPlaybackAccount(Account a)
		{
			if (a == null) return false;
			try { if (a.Provider == Provider.Playback) return true; } catch { }
			try
			{
				Cbi.Connection c = a.Connection;
				ConnectOptions o = c == null ? null : c.Options;
				if (o != null) return o.Provider == Provider.Playback;
			}
			catch { }
			return false;
		}

		private static DateTime Playback_ParseOrThrow(string s, string field)
		{
			DateTime d;
			if (!DateTime.TryParse(s, CultureInfo.InvariantCulture, DateTimeStyles.None, out d))
				throw new BadRequestException(field + ": bad date/time '" + s + "' (US Eastern, e.g. \"2026-08-10T09:30:00\" — see GET /playback clockBasis)");
			return d;
		}

		private static int Playback_ClampWaitSec(int s)
		{
			if (s < 1) return Playback_ResetWaitDefaultSec;
			return Math.Min(s, Playback_ResetWaitMaxSec);
		}

		/// <summary>Writes PlaybackAdapter.PlaybackSpeed and reads it back — THIS IS THE PLAY/PAUSE CONTROL
		/// (file header; bisected by cli-nt-bridge, NT8BridgeServerPlayback.cs SetPlaybackSpeed). null =
		/// confirmed; otherwise what did not match, for a caller that must never be told "ok" on a lie.</summary>
		private static string Playback_WriteSpeed(int want, out int? readBack)
		{
			readBack = null;
			if (Playback_PiSpeed == null) return "PlaybackSpeed did not resolve on this NT8 build";
			if (!Playback_PiSpeed.CanWrite) return "PlaybackSpeed is not writable on this build";
			try { Playback_PiSpeed.SetValue(null, want); }
			catch (Exception ex) { return "write threw " + Deep(ex); }
			readBack = Playback_Int(Playback_Read(Playback_PiSpeed));
			if (readBack == want) return null;
			return "wrote " + want.ToString(CultureInfo.InvariantCulture) + " but it reads back "
				+ (readBack.HasValue ? readBack.Value.ToString(CultureInfo.InvariantCulture) : "unreadable");
		}

		/// <summary>PlaybackAdapter.Reset(DateTime, Action&lt;bool&gt;) — asynchronous. `callbackOk` is
		/// NinjaTrader's own success signal; `clockAfter` is read back independently, because a callback with
		/// no matching clock move would be exactly the "assignment did not throw" lie the read-back
		/// discipline exists to catch (NOTES.md lesson 21). Bounded by `waitMs`: a callback that never fires
		/// must not hang the caller (the HTTP thread for /seek, the run worker for /run).</summary>
		private static void Playback_DoReset(DateTime targetEst, int waitMs, out bool callbackOk, out DateTime? clockAfter, out bool timedOut)
		{
			callbackOk = false; timedOut = false; clockAfter = null;
			if (Playback_MiReset == null) { timedOut = true; return; }
			var done = new ManualResetEventSlim(false);
			bool ok = false;
			Action<bool> cb = success => { ok = success; try { done.Set(); } catch { } };
			try { Playback_MiReset.Invoke(null, new object[] { targetEst, cb }); }
			catch (Exception ex) { Log("/playback/seek Reset invoke — " + Deep(ex)); timedOut = true; clockAfter = Playback_Clock(); return; }
			timedOut = !done.Wait(Math.Max(1, waitMs));
			callbackOk = ok;
			clockAfter = Playback_Clock();
		}

		// ── POST /playback/seek {"time","waitSec"} ──────────────────────────────
		private static string Playback_SeekPost(string body, ref int status)
		{
			double ageH;
			if (!Ord_Flag(out ageH))
			{
				status = 403;
				return Obj(P("error", Q("playback module not armed — create orders.enabled beside the AddOn (see docs/api/orders.md); "
					+ "the same file arms /playback/seek, /playback/speed and /playback/run")));
			}

			var call = new Playback_Call { Endpoint = "/playback/seek", FlagAgeHours = ageH };
			try
			{
				string outcome;
				string gateProblem = Playback_PreflightProblem(out outcome);
				if (gateProblem != null) return Playback_Err(ref status, call, 409, outcome, gateProblem);

				Dictionary<string, object> req;
				try { req = ParseJson(body) as Dictionary<string, object>; }
				catch (Exception ex) { return Playback_Err(ref status, call, 400, "badRequest", "bad JSON body: " + ex.Message); }
				if (req == null) return Playback_Err(ref status, call, 400, "badRequest", "body must be a JSON object");

				DateTime target;
				int waitSec;
				try
				{
					string s = JGetStr(req, "time", null);
					if (string.IsNullOrWhiteSpace(s))
						return Playback_Err(ref status, call, 400, "badRequest", "time is required, e.g. \"2026-08-10T09:30:00\" (US Eastern — see GET /playback clockBasis)");
					target = Playback_ParseOrThrow(s, "time");
					waitSec = Playback_ClampWaitSec(JGetInt(req, "waitSec", Playback_ResetWaitDefaultSec));
				}
				catch (BadRequestException ex) { return Playback_Err(ref status, call, 400, "badRequest", ex.Message); }

				string rp = RangeProblem(target, target);
				if (rp != null) return Playback_Err(ref status, call, 400, "badRequest", "time: " + rp);

				if (Playback_MiReset == null)
					return Playback_Err(ref status, call, 500, "resetUnresolved", "PlaybackAdapter.Reset did not resolve on this NT8 build — see GET /compat");

				DateTime? before = Playback_Clock();
				int? beforeSpeed = Playback_Int(Playback_Read(Playback_PiSpeed));

				bool callbackOk; DateTime? after; bool timedOut;
				Playback_DoReset(target, waitSec * 1000, out callbackOk, out after, out timedOut);

				// The clock read-back decides. Observed on 8.1.8.2: Reset's callback reports false even when
				// the replay clock lands exactly on the asked time, so it is reported but never trusted.
				bool landed = after.HasValue && Math.Abs((after.Value - target).TotalSeconds) <= 2;
				status = 200; call.Status = 200;
				call.Outcome = landed ? "ok" : "notConfirmed";
				call.Detail = "seek to " + Tm(target) + " callbackOk=" + callbackOk + " timedOut=" + timedOut;
				return Obj(
					P("ok", landed ? "true" : "false"),
					P("requestedTime", Tm(target)),
					P("before", Obj(
						P("clockEst", before.HasValue ? Tm(before.Value) : "null"),
						P("speed", beforeSpeed.HasValue ? I(beforeSpeed.Value) : "null"))),
					P("clockEst", after.HasValue ? Tm(after.Value) : "null"),
					P("callbackOk", callbackOk ? "true" : "false"),
					P("timedOut", timedOut ? "true" : "false"),
					P("waitedSec", I(waitSec)),
					P("note", Q("ok means the replay clock was READ BACK within 2 s of the asked time. callbackOk is the flag "
						+ "PlaybackAdapter.Reset hands its callback; on 8.1.8.2 it reads false even after a good seek, so it is "
						+ "shown but does not decide ok. A large jump can take longer than waitSec to settle; re-read GET /playback if timedOut.")));
			}
			catch (TimeoutException) { throw; }
			catch (Exception ex) { return Playback_Err(ref status, call, 500, "error", Deep(ex)); }
			finally { Playback_AuditCall(call); }
		}

		// ── POST /playback/speed {"speed"} ──────────────────────────────────────
		private static string Playback_SpeedPost(string body, ref int status)
		{
			double ageH;
			if (!Ord_Flag(out ageH))
			{
				status = 403;
				return Obj(P("error", Q("playback module not armed — create orders.enabled beside the AddOn (see docs/api/orders.md); "
					+ "the same file arms /playback/seek, /playback/speed and /playback/run")));
			}

			var call = new Playback_Call { Endpoint = "/playback/speed", FlagAgeHours = ageH };
			try
			{
				string outcome;
				string gateProblem = Playback_PreflightProblem(out outcome);
				if (gateProblem != null) return Playback_Err(ref status, call, 409, outcome, gateProblem);

				Dictionary<string, object> req;
				try { req = ParseJson(body) as Dictionary<string, object>; }
				catch (Exception ex) { return Playback_Err(ref status, call, 400, "badRequest", "bad JSON body: " + ex.Message); }
				if (req == null) return Playback_Err(ref status, call, 400, "badRequest", "body must be a JSON object");

				double? speedNum;
				try { speedNum = Ord_Number(req, "speed"); }
				catch (BadRequestException ex) { return Playback_Err(ref status, call, 400, "badRequest", ex.Message); }
				if (speedNum == null) return Playback_Err(ref status, call, 400, "badRequest", "speed is required — a whole number, 0 pauses");
				if (speedNum.Value != Math.Floor(speedNum.Value) || speedNum.Value < 0)
					return Playback_Err(ref status, call, 400, "badRequest", "speed must be a whole number >= 0");
				int speed = (int)speedNum.Value;
				int? maxSpeed = Playback_Int(Playback_FiMaxSpeed == null ? null : Playback_FiMaxSpeed.GetValue(null));
				if (maxSpeed.HasValue && speed > maxSpeed.Value)
					return Playback_Err(ref status, call, 400, "badRequest", "speed " + speed + " is above this build's MaxSpeedValue (" + maxSpeed.Value + ")");

				DateTime? before = Playback_Clock();
				int? beforeSpeed = Playback_Int(Playback_Read(Playback_PiSpeed));

				int? readBack;
				string problem = Playback_WriteSpeed(speed, out readBack);

				status = 200; call.Status = 200;
				call.Outcome = problem == null ? "ok" : "notConfirmed";
				call.Detail = "speed " + speed + (problem == null ? " confirmed" : (": " + problem));
				return Obj(
					P("ok", problem == null ? "true" : "false"),
					P("requestedSpeed", I(speed)),
					P("before", Obj(
						P("clockEst", before.HasValue ? Tm(before.Value) : "null"),
						P("speed", beforeSpeed.HasValue ? I(beforeSpeed.Value) : "null"))),
					P("speed", readBack.HasValue ? I(readBack.Value) : "null"),
					P("problem", Q(problem)),
					P("note", Q("writing this property IS the play/pause control (speed 0 pauses, any positive value plays at that "
						+ "multiple) — the value is read back, never trusted from the write call alone.")));
			}
			catch (TimeoutException) { throw; }
			catch (Exception ex) { return Playback_Err(ref status, call, 500, "error", Deep(ex)); }
			finally { Playback_AuditCall(call); }
		}

		// ── the run driver: POST /playback/run, GET|DELETE /playback/run/{id} ───

		/// <summary>One queued or finished playback run. Written by the worker, read by HTTP threads —
		/// State/Cancel are volatile for that reason, same discipline as NT8Bridge.Backtest.cs's Job.</summary>
		private sealed class Playback_Job
		{
			public string Id;
			public volatile string State = "queued";		// queued|running|done|error|cancelled
			public volatile bool Cancel;

			public DateTime? From;		// null = leave FromEst/the current position alone
			public DateTime To;		// required: both the new ToEst and the watch target
			public int Speed;
			public bool StopAtEnd;		// always true — see Playback_RunPost; kept for the echoed request

			public DateTime QueuedAt, StartedAt, FinishedAt;
			public DateTime? BeforeClockEst;
			public int? BeforeSpeed;
			public DateTime? ClockStartEst, ClockEndEst;
			public bool ReachedTo;
			public volatile string ExecutionsJson = "[]";
			public readonly List<string> Warnings = new List<string>();
			public string Error;
		}

		private static readonly object Playback_JobGate = new object();
		private static readonly Dictionary<string, Playback_Job> Playback_Jobs = new Dictionary<string, Playback_Job>(StringComparer.OrdinalIgnoreCase);
		private static readonly Queue<Playback_Job> Playback_JobQ = new Queue<Playback_Job>();
		private static readonly AutoResetEvent Playback_JobSignal = new AutoResetEvent(false);
		private static Thread Playback_JobThread;
		private static int Playback_JobCounter;

		private static string Playback_RunPost(string body, ref int status)
		{
			double ageH;
			if (!Ord_Flag(out ageH))
			{
				status = 403;
				return Obj(P("error", Q("playback module not armed — create orders.enabled beside the AddOn (see docs/api/orders.md); "
					+ "the same file arms /playback/seek, /playback/speed and /playback/run")));
			}

			var call = new Playback_Call { Endpoint = "/playback/run", FlagAgeHours = ageH };
			try
			{
				string outcome;
				string gateProblem = Playback_PreflightProblem(out outcome);
				if (gateProblem != null) return Playback_Err(ref status, call, 409, outcome, gateProblem);

				Dictionary<string, object> req;
				try { req = ParseJson(body) as Dictionary<string, object>; }
				catch (Exception ex) { return Playback_Err(ref status, call, 400, "badRequest", "bad JSON body: " + ex.Message); }
				if (req == null) return Playback_Err(ref status, call, 400, "badRequest", "body must be a JSON object");

				try
				{
					// ponytail: the only supported mode is "watch to 'to', then pause" — a free-running mode
					// (stopAtEnd:false, return immediately, keep playing unattended) would need its own
					// background-clock-watchdog design (who pauses it, on what trigger, if nobody polls?) and
					// this module does not need it. Refused explicitly rather than silently ignored.
					object rawStop = JGet(req, "stopAtEnd");
					if (rawStop != null)
					{
						if (!(rawStop is bool)) return Playback_Err(ref status, call, 400, "badRequest", "stopAtEnd must be a boolean");
						if (!(bool)rawStop)
							return Playback_Err(ref status, call, 400, "badRequest",
								"stopAtEnd:false is not supported — this module always pauses at 'to' or the wall-clock cap; there is no free-running mode");
					}
				}
				catch (BadRequestException ex) { return Playback_Err(ref status, call, 400, "badRequest", ex.Message); }

				DateTime? from; DateTime to;
				try
				{
					string fromS = JGetStr(req, "from", null);
					from = string.IsNullOrWhiteSpace(fromS) ? (DateTime?)null : Playback_ParseOrThrow(fromS, "from");

					string toS = JGetStr(req, "to", null);
					if (string.IsNullOrWhiteSpace(toS))
						return Playback_Err(ref status, call, 400, "badRequest", "to is required, e.g. \"2026-08-10T16:00:00\" (US Eastern) — it is both the new range end and the watch target");
					to = Playback_ParseOrThrow(toS, "to");
				}
				catch (BadRequestException ex) { return Playback_Err(ref status, call, 400, "badRequest", ex.Message); }

				string rp = RangeProblem(from ?? to.AddDays(-1), to);
				if (rp != null) return Playback_Err(ref status, call, 400, "badRequest", rp);

				double? speedNum;
				try { speedNum = Ord_Number(req, "speed"); }
				catch (BadRequestException ex) { return Playback_Err(ref status, call, 400, "badRequest", ex.Message); }
				if (speedNum == null)
					return Playback_Err(ref status, call, 400, "badRequest", "speed is required — a whole number >= 1 (read maxSpeedValue off GET /playback for this build's ceiling)");
				if (speedNum.Value != Math.Floor(speedNum.Value) || speedNum.Value < 1)
					return Playback_Err(ref status, call, 400, "badRequest", "speed must be a whole number >= 1 — use POST /playback/speed with 0 to pause instead");
				int speed = (int)speedNum.Value;
				int? maxSpeed = Playback_Int(Playback_FiMaxSpeed == null ? null : Playback_FiMaxSpeed.GetValue(null));
				if (maxSpeed.HasValue && speed > maxSpeed.Value)
					return Playback_Err(ref status, call, 400, "badRequest", "speed " + speed + " is above this build's MaxSpeedValue (" + maxSpeed.Value + ")");

				// One shared clock: a second run while one is already active would fight the first over the
				// same PlaybackAdapter statics. Refused outright rather than queued — queuing a clock move
				// behind another clock move is not a meaningful "wait your turn".
				//
				// The job is BUILT first and then checked-and-enqueued inside ONE lock, the way
				// Ord_ReserveSubmit reserves the submit caps: the core dispatches every request on its own
				// thread-pool thread, so a check in one lock and an enqueue in a second lets two callers — a
				// client retrying after a slow answer, or two callers — both pass the check in the gap and both
				// get a 202, and the second run's `before` snapshot would then describe a clock the first run
				// had already moved.
				var job = new Playback_Job
				{
					Id = "pr" + Interlocked.Increment(ref Playback_JobCounter).ToString(CultureInfo.InvariantCulture),
					From = from,
					To = to,
					Speed = speed,
					StopAtEnd = true,
					QueuedAt = DateTime.Now,
				};
				lock (Playback_JobGate)
				{
					foreach (var j in Playback_Jobs.Values)
						if (j.State == "queued" || j.State == "running")
							return Playback_Err(ref status, call, 409, "runInProgress",
								"a playback run is already " + j.State + " (id=" + j.Id + ") — this module drives one shared replay clock; "
								+ "GET /playback/run/" + j.Id + " or DELETE it first");
					Playback_Jobs[job.Id] = job;
					Playback_JobQ.Enqueue(job);
				}
				Playback_JobSignal.Set();
				Log("playback run " + job.Id + " queued " + (from.HasValue ? Tm(from.Value) : "(current)") + ".." + Tm(to) + " speed=" + speed);

				status = 202; call.Status = 202;
				call.Outcome = "queued";
				call.Detail = job.Id;
				return Obj(P("id", Q(job.Id)), P("state", Q("queued")));		// literal: the worker may already have moved it on
			}
			catch (TimeoutException) { throw; }
			catch (Exception ex) { return Playback_Err(ref status, call, 500, "error", Deep(ex)); }
			finally { Playback_AuditCall(call); }
		}

		private static Playback_Job Playback_FindJob(string id)
		{
			lock (Playback_JobGate) { Playback_Job j; return Playback_Jobs.TryGetValue(id, out j) ? j : null; }
		}

		private static string Playback_RunGet(string id, ref int status)
		{
			var job = Playback_FindJob(id);
			return job == null ? Err(ref status, 404, "no playback run '" + id + "'") : Playback_JobJson(job);
		}

		/// <summary>Sets Cancel; the worker notices it on its next poll tick (at most Playback_RunPollMs
		/// away) and finishes the job itself — pausing and reporting, same as a normal end. A job still
		/// "queued" (has not reached the worker yet) is finished here directly. Removes the record either
		/// way, same convention as DELETE /backtest/{id}.</summary>
		private static string Playback_RunCancel(string id, ref int status)
		{
			var job = Playback_FindJob(id);
			if (job == null) return Err(ref status, 404, "no playback run '" + id + "'");
			job.Cancel = true;
			Playback_JobSignal.Set();
			lock (Playback_JobGate)
				if (job.State == "queued")
				{
					job.State = "cancelled"; job.Error = "cancelled by DELETE before it started"; job.FinishedAt = DateTime.Now;
				}
			lock (Playback_JobGate) Playback_Jobs.Remove(id);
			Log("playback run " + id + " deleted");
			return Obj(P("ok", "true"));
		}

		private static string Playback_JobJson(Playback_Job job)
		{
			return Obj(
				P("id", Q(job.Id)),
				P("state", Q(job.State)),
				P("request", Obj(
					P("from", job.From.HasValue ? Tm(job.From.Value) : "null"),
					P("to", Tm(job.To)),
					P("speed", I(job.Speed)),
					P("stopAtEnd", job.StopAtEnd ? "true" : "false"))),
				P("queuedAt", Tm(job.QueuedAt)),
				P("startedAt", job.StartedAt == DateTime.MinValue ? "null" : Tm(job.StartedAt)),
				P("finishedAt", job.FinishedAt == DateTime.MinValue ? "null" : Tm(job.FinishedAt)),
				P("before", Obj(
					P("clockEst", job.BeforeClockEst.HasValue ? Tm(job.BeforeClockEst.Value) : "null"),
					P("speed", job.BeforeSpeed.HasValue ? I(job.BeforeSpeed.Value) : "null"))),
				P("clockStartEst", job.ClockStartEst.HasValue ? Tm(job.ClockStartEst.Value) : "null"),
				P("clockEndEst", job.ClockEndEst.HasValue ? Tm(job.ClockEndEst.Value) : "null"),
				P("reachedTo", job.ReachedTo ? "true" : "false"),
				P("wallSeconds", (job.StartedAt == DateTime.MinValue || job.FinishedAt == DateTime.MinValue) ? "null" : D((job.FinishedAt - job.StartedAt).TotalSeconds)),
				P("executions", string.IsNullOrEmpty(job.ExecutionsJson) ? "[]" : job.ExecutionsJson),
				P("warnings", Arr(job.Warnings.Select(Q))),
				P("error", Q(job.Error)));
		}

		// ── the run worker: one job at a time, never on a dispatcher ────────────
		private static void Playback_Worker()
		{
			while (running)
			{
				Playback_Job job = null;
				lock (Playback_JobGate) if (Playback_JobQ.Count > 0) job = Playback_JobQ.Dequeue();
				if (job == null) { Playback_JobSignal.WaitOne(500); continue; }
				if (job.Cancel)
				{
					if (job.State == "queued") { job.State = "cancelled"; job.FinishedAt = DateTime.Now; if (job.Error == null) job.Error = "cancelled before it started"; }
					continue;
				}

				job.StartedAt = DateTime.Now;
				job.State = "running";
				Log("playback run " + job.Id + " start " + (job.From.HasValue ? Tm(job.From.Value) : "(current)") + ".." + Tm(job.To) + " speed=" + job.Speed);
				try
				{
					Playback_DoRun(job);
					job.FinishedAt = DateTime.Now;
					Playback_TryFinishJob(job, job.Cancel ? "cancelled" : "done");
				}
				catch (Exception ex)
				{
					if (job.Error == null) job.Error = ex.Message;
					job.FinishedAt = DateTime.Now;
					Playback_TryFinishJob(job, "error");
					Log("playback run " + job.Id + ": " + Deep(ex));
				}
				Log("playback run " + job.Id + " " + job.State + " in "
					+ (job.FinishedAt - job.StartedAt).TotalSeconds.ToString("F1", CultureInfo.InvariantCulture) + "s");
			}
		}

		/// <summary>Compare-and-set on job.State, same reasoning as NT8Bridge.Backtest.cs's TryFinish: only
		/// the first caller to see "running" moves it to a terminal state.</summary>
		private static bool Playback_TryFinishJob(Playback_Job job, string newState)
		{
			lock (Playback_JobGate)
			{
				if (job.State != "running") return false;
				job.State = newState;
				return true;
			}
		}

		/// <summary>The actual drive, on the worker thread: re-check the gates (the world can have changed
		/// between POST and the worker picking this up), record the before-state, set the range, position
		/// the clock, play, watch, pause, collect executions. Throws to report an error; the caller (Worker)
		/// catches it and still has every field this method set on `job` before throwing.
		///
		/// The gates are re-run every Playback_RunGateEveryMs WHILE the clock is playing, the arming file
		/// included: a run holds the platform's one replay clock for up to 15 minutes, and a check taken once
		/// at the start cannot see an operator deleting orders.enabled or another account taking on a
		/// position ten seconds in. A refusal breaks the watch loop, and the unconditional pause below stops
		/// the clock.</summary>
		private static void Playback_DoRun(Playback_Job job)
		{
			string outcome;
			string problem = Playback_GateProblem(out outcome);
			if (problem != null) throw new Exception("refused at start (" + outcome + "): " + problem);

			job.BeforeClockEst = Playback_Clock();
			job.BeforeSpeed = Playback_Int(Playback_Read(Playback_PiSpeed));

			// range: ToEst always (it is both the new range end and the watch target); FromEst only if asked
			if (job.From.HasValue) Playback_WriteDate(Playback_PiFromEst, "FromEst", job.From.Value, job.Warnings);
			Playback_WriteDate(Playback_PiToEst, "ToEst", job.To, job.Warnings);

			// position: seek to 'from' only if asked — omitting it means "keep playing from wherever the
			// clock already sits", a deliberate resume mode.
			if (job.From.HasValue && !job.Cancel)
			{
				if (Playback_MiReset == null)
					job.Warnings.Add("PlaybackAdapter.Reset did not resolve — could not seek to 'from', clock left wherever it was");
				else
				{
					bool callbackOk; DateTime? after; bool timedOut;
					Playback_DoReset(job.From.Value, Playback_ResetWaitDefaultSec * 1000, out callbackOk, out after, out timedOut);
					bool landed = after.HasValue && Math.Abs((after.Value - job.From.Value).TotalSeconds) <= 2;
					if (!landed)
						job.Warnings.Add("seek to " + Tm(job.From.Value) + " did NOT land: clock reads "
							+ (after.HasValue ? Tm(after.Value) : "null") + " (callbackOk=" + callbackOk + " timedOut=" + timedOut
							+ ") — the run starts from where the clock is, not from 'from'");
				}
			}

			job.ClockStartEst = Playback_Clock();

			if (!job.Cancel)
			{
				int? playReadBack;
				string playProblem = Playback_WriteSpeed(job.Speed, out playReadBack);
				if (playProblem != null) job.Warnings.Add("play at speed " + job.Speed + ": " + playProblem);

				DateTime deadlineWall = DateTime.UtcNow.AddSeconds(Playback_RunMaxWallSec);
				DateTime nextGate = DateTime.UtcNow.AddMilliseconds(Playback_RunGateEveryMs);
				DateTime? last = job.ClockStartEst;
				while (!job.Cancel)
				{
					if (DateTime.UtcNow >= deadlineWall)
					{
						job.Error = "wall-clock cap of " + Playback_RunMaxWallSec + "s reached before the replay clock reached 'to' (last seen "
							+ (last.HasValue ? Tm(last.Value) : "unknown") + ", target " + Tm(job.To) + ")";
						break;
					}
					if (DateTime.UtcNow >= nextGate)
					{
						nextGate = DateTime.UtcNow.AddMilliseconds(Playback_RunGateEveryMs);
						string midOutcome;
						string midProblem = Playback_GateProblem(out midOutcome);
						if (midProblem != null)
						{
							job.Error = "stopped mid-run (" + midOutcome + "): " + midProblem + " — the clock was paused "
								+ "where it stood (last seen " + (last.HasValue ? Tm(last.Value) : "unknown") + ", target "
								+ Tm(job.To) + ")";
							break;
						}
					}
					Thread.Sleep(Playback_RunPollMs);
					DateTime? c = Playback_Clock();
					if (c.HasValue) last = c;
					if (c.HasValue && c.Value >= job.To) { job.ReachedTo = true; break; }
				}
			}

			// restore = pause and report, always — never reconnect, never leave it running.
			int? pauseReadBack;
			string pauseProblem = Playback_WriteSpeed(0, out pauseReadBack);
			if (pauseProblem != null) job.Warnings.Add("pause at end: " + pauseProblem);
			job.ClockEndEst = Playback_Clock();

			try { job.ExecutionsJson = Playback_ExecutionsJson(job.ClockStartEst, job.ClockEndEst, job.Warnings); }
			catch (Exception ex) { job.Warnings.Add("executions: " + Deep(ex)); job.ExecutionsJson = "[]"; }

			if (job.Error != null) throw new Exception(job.Error);
		}

		private static void Playback_WriteDate(PropertyInfo pi, string name, DateTime value, List<string> warnings)
		{
			if (pi == null) { warnings.Add(name + " did not resolve on this NT8 build — range not set"); return; }
			if (!pi.CanWrite) { warnings.Add(name + " is not writable on this build — range not set"); return; }
			try
			{
				pi.SetValue(null, value);
				object back = pi.GetValue(null);
				bool confirmed = back is DateTime && (DateTime)back == value;
				if (!confirmed) warnings.Add(name + ": wrote " + Tm(value) + " but it reads back " + (back is DateTime ? Tm((DateTime)back) : "unreadable"));
			}
			catch (Exception ex) { warnings.Add(name + ": write threw " + Deep(ex)); }
		}

		/// <summary>Executions on every Playback-provider account, for the window this run actually covered.
		/// Reuses NT8Bridge.Account.cs's own Acct_Fetch/Acct_ExecutionOne (Acct_Query, Acct_MaxRows,
		/// Acct_PadDays) — "read them the way the accounts module does" — rather than a second copy of the
		/// trade-DB-plus-memory-union logic.
		///
		/// ponytail: the window is padded +/-3h around the Eastern replay clock because Execution.Time is
		/// stamped in the EXCHANGE's own local time (e.g. CME Central for ES), not Eastern, and this module
		/// has no per-exchange timezone table. A pad this wide can pull in fills from outside the run for an
		/// account that traded right before/after it; each row still carries its own `time`, so a caller
		/// that needs exact bounds can re-filter. Upgrade: resolve each instrument's exchange TZ if a
		/// boundary miss is ever observed live.</summary>
		private static string Playback_ExecutionsJson(DateTime? clockStart, DateTime? clockEnd, List<string> warnings)
		{
			if (!clockStart.HasValue || !clockEnd.HasValue)
			{
				warnings.Add("executions: the replay clock could not be read, so no time window could be formed");
				return "[]";
			}
			DateTime from = clockStart.Value.AddHours(-3);
			DateTime to = clockEnd.Value.AddHours(3);

			var rows = new List<string>();
			try
			{
				Account[] accounts;
				lock (Account.All) accounts = Account.All.ToArray();
				foreach (var a in accounts)
				{
					if (a == null || !Playback_IsPlaybackAccount(a)) continue;
					string name = null; try { name = a.Name; } catch { }
					if (name == null) continue;

					var qq = new Acct_Query { Acct = a, AccountName = name, InstrumentName = "", Instrument = null, From = from, To = to, N = Acct_MaxRows };
					var w = new List<string>();
					string source = "db"; int lookbackDays = 3;
					List<Execution> execs;
					try { execs = Acct_Fetch(qq, Acct_PadDays, w, out source, out lookbackDays); }
					catch (Exception ex) { w.Add("fetch failed: " + Deep(ex)); execs = new List<Execution>(); source = "error"; }

					var execRows = execs.Where(e => { try { return e.Time >= from && e.Time <= to; } catch { return false; } })
						.Select(Acct_ExecutionOne).ToList();

					rows.Add(Obj(
						P("account", Q(name)),
						P("windowFromEst", Tm(from)),
						P("windowToEst", Tm(to)),
						P("source", Q(source)),
						P("count", I(execRows.Count)),
						P("executions", Arr(execRows)),
						P("warnings", Arr(w.Select(Q)))));
				}
			}
			catch (Exception ex) { warnings.Add("executions: account enumeration failed: " + Deep(ex)); }
			return Arr(rows);
		}
	}
}
