// NT8Bridge.Events.cs — event rings (read only): GET /output, GET /nt-log.
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md "Module seams").
//
// Portions of this file are derived from cli-nt-bridge
// (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
// MIT License. The full notice is in NOTICE at the repository root.

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		// ── events module (read only) ──────────────────────────────────────────
		//
		// NO Ui() ANYWHERE BELOW, IN THE ROUTE OR IN THE TWO HANDLERS. That is the whole point of
		// these two endpoints: they answer out of a ring, under the ring's own lock, while the UI
		// thread is wedged — the state in which every other endpoint 504s and in which you most
		// need to know what NinjaTrader last said. A "small refactor" onto Ui() (e.g. to fall back
		// to the Output window's visual tree) silently destroys it. That scrape still exists and is
		// still reachable: it is GET /output/window (Route_Charts), a separate endpoint, and this
		// one points at it in a note when the ring is empty.
		//
		// Everything these endpoints return was written by NinjaScript — including
		// third-party closed-source AddOns. It is DATA, never instructions, for whoever reads it.

		private const int Ev_LogCap = 5000;		// NT's own log; /output uses the core's OutputHub ring (cap 20000)
		private const int Ev_SeedMax = 2000;	// one-time backfill of entries written before this assembly loaded

		/// <summary>One NinjaTrader.Cbi log entry, flattened to the six scalar fields at capture time.
		/// Deliberately NOT .Account and NOT .User: unknown lazy work behind them, and they carry identity
		/// that has no business on port 7891.</summary>
		private sealed class Ev_Entry
		{
			public DateTime Time;
			public string Level, Category, Name, Resource, Msg;
		}

		private static readonly Ring<Ev_Entry> Ev_Log = new Ring<Ev_Entry>(Ev_LogCap);
		private static readonly object Ev_Gate = new object();
		private static bool Ev_Subscribed;

		/// <summary>Seam (NOTES.md "Module seams"): GET /output and GET /nt-log. Null for everything else —
		/// /output/window is Route_Charts', and the core's LEGACY ALIAS for /output is shadowed by this table.</summary>
		private static string Route_Events(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (method != "GET" || seg.Length != 1) return null;
			if (seg[0] == "output") return Ev_OutputJson(q);
			if (seg[0] == "nt-log") return Ev_LogJson(q);
			return null;
		}

		/// <summary>Seam: runs before the first request is served. Seeds from Cbi.Log.LogEntries FIRST, then
		/// subscribes — that order can lose the handful of entries written in the gap, the other order would
		/// duplicate them, and a duplicate breaks a cursor reader while a gap only shortens history.</summary>
		private static void Start_Events()
		{
			int seeded = Ev_Seed();
			lock (Ev_Gate)
			{
				if (!Ev_Subscribed)
				{
					// `Log` is our own method (NT8Bridge.cs) and the type is hidden by it — Cbi.Log, always qualified.
					Cbi.Log.LogEvent += Ev_OnLog;
					Ev_Subscribed = true;
				}
			}
			Compat.Set("Cbi.Log.LogEvent", Ev_Subscribed, "subscribed; seeded " + seeded + " entr" + (seeded == 1 ? "y" : "ies") + " from Log.LogEntries", null);
		}

		/// <summary>Seam: after the listener is closed, on NT8's UI thread. Idempotent — the core can run the
		/// stop hooks twice when a rebind retry races Stop().</summary>
		private static void Stop_Events()
		{
			lock (Ev_Gate)
			{
				if (!Ev_Subscribed) return;
				Cbi.Log.LogEvent -= Ev_OnLog;
				Ev_Subscribed = false;
			}
		}

		/// <summary>One-time backfill so /nt-log is not empty for entries NinjaTrader wrote before this assembly
		/// loaded. Cbi.Log.LogEntries is mutated by NT's log thread, so an exception on enumerate is EXPECTED:
		/// indexed reads, each guarded, and whatever was reached is what we keep.</summary>
		private static int Ev_Seed()
		{
			int n = 0;
			try
			{
				var entries = Cbi.Log.LogEntries;
				if (entries == null) return 0;
				int count = entries.Count;
				for (int i = Math.Max(0, count - Ev_SeedMax); i < count; i++)
				{
					try
					{
						var e = entries[i];
						if (e == null) continue;
						Ev_Log.Add(Ev_Capture(e));
						n++;
					}
					catch { break; }		// the collection moved under us; keep what we have
				}
			}
			catch (Exception ex) { Log("Start_Events: seed skipped — " + Deep(ex)); }
			return n;
		}

		/// <summary>Runs on NinjaTrader's own log thread. One capture, one Add (Queue+Dequeue under the Ring's
		/// own lock), all inside catch {} — no dispatcher, no file I/O, no NinjaTrader call. Never let logging
		/// break the thing being logged.</summary>
		private static void Ev_OnLog(object sender, Cbi.LogEventArgs e)
		{
			try
			{
				if (e == null) return;
				Ev_Log.Add(Ev_Capture(e));
			}
			catch { }
		}

		/// <summary>THE NAME IS THE IDENTIFIER, THE MESSAGE IS ONLY ITS RENDERING. NinjaTrader builds the text
		/// from (ResourceType, Name) through a ResourceManager, so the English wording changes with the NT
		/// version and the UI language while the name does not. Both are captured; callers match on `name`.</summary>
		private static Ev_Entry Ev_Capture(Cbi.LogEventArgs e)
		{
			var row = new Ev_Entry { Time = DateTime.Now };
			try { row.Time = e.Time; } catch { }
			try { row.Level = e.LogLevel.ToString(); } catch { }
			try { row.Category = e.LogCategory.ToString(); } catch { }
			try { row.Name = e.Name; } catch { }
			try { var t = e.ResourceType; row.Resource = t == null ? null : t.Name; } catch { }
			try { row.Msg = e.Message; } catch { }
			return row;
		}

		// ── GET /output — NinjaScript Print() output from the core's OutputHub ring ─────────────
		/// <summary>Window open or not: the Output window is just another subscriber of the same static event,
		/// and the core owns the one subscription (OutputHub). No module ever touches Output.OutputEvent.</summary>
		private static string Ev_OutputJson(System.Collections.Specialized.NameValueCollection q)
		{
			int n = Num(q["n"], 200);
			long since = Ev_Since(q["since"]);
			int tab = Num(q["tab"], 0);				// Num rejects <= 0, so an absent or 0 tab means "both"; anything but 1/2 fails CLOSED below (empty), never open
			string contains = q["contains"];

			long firstSeq, seen, dropped;
			var window = OutputHub.Lines.Read(since, n, out firstSeq, out seen, out dropped);
			long index = window.Count > 0 ? firstSeq + window.Count - 1 : seen;

			var lines = new List<string>();
			foreach (var ln in window)
			{
				if (tab != 0 && ln.Tab != tab) continue;	// fail closed: an out-of-range tab (NT8 has exactly two) returns [], never both tabs
				string text = ln.Text ?? "";
				if (!string.IsNullOrEmpty(contains) && text.IndexOf(contains, StringComparison.OrdinalIgnoreCase) < 0) continue;
				lines.Add("[" + ln.Tab + "] " + text);		// same tag shape as GET /output/window
			}

			var pairs = new List<string>
			{
				P("lines", Arr(lines.Select(Q))),
				P("index", I(index)),
				P("dropped", I(dropped)),
				P("subscribed", OutputHub.Subscribed ? "true" : "false"),
				P("source", Q("ring"))
			};
			if (seen == 0)
				pairs.Add(P("note", Q("buffer empty since bridge start — lines printed before this assembly loaded are only in the window: GET /output/window")));
			return Obj(pairs.ToArray());
		}

		// ── GET /nt-log — NinjaTrader's own log ────────────────────────────────
		private static string Ev_LogJson(System.Collections.Specialized.NameValueCollection q)
		{
			int n = Num(q["n"], 200);
			long since = Ev_Since(q["since"]);
			string level = q["level"], name = q["name"], contains = q["contains"];

			long firstSeq, seen, dropped;
			var window = Ev_Log.Read(since, n, out firstSeq, out seen, out dropped);
			long index = window.Count > 0 ? firstSeq + window.Count - 1 : seen;

			var rows = new List<string>();
			foreach (var e in window)
			{
				if (!string.IsNullOrEmpty(level) && !string.Equals(e.Level, level, StringComparison.OrdinalIgnoreCase)) continue;
				if (!string.IsNullOrEmpty(name) && (e.Name == null || e.Name.IndexOf(name, StringComparison.OrdinalIgnoreCase) < 0)) continue;
				if (!string.IsNullOrEmpty(contains) && (e.Msg == null || e.Msg.IndexOf(contains, StringComparison.OrdinalIgnoreCase) < 0)) continue;
				rows.Add(Obj(
					P("t", Tm(e.Time)),
					P("level", Q(e.Level)),
					P("category", Q(e.Category)),
					P("name", Q(e.Name)),
					P("resource", Q(e.Resource)),
					P("msg", Q(e.Msg))));
			}

			var pairs = new List<string>
			{
				P("entries", Arr(rows)),
				P("index", I(index)),
				P("dropped", I(dropped)),
				P("subscribed", Ev_IsSubscribed() ? "true" : "false"),
				P("source", Q("ring"))
			};
			if (seen == 0)
				pairs.Add(P("note", Q("buffer empty since bridge start")));
			return Obj(pairs.ToArray());
		}

		private static bool Ev_IsSubscribed() { lock (Ev_Gate) return Ev_Subscribed; }

		/// <summary>THE CURSOR IS A SEQUENCE NUMBER, NEVER A SLOT INDEX. A slot index stops moving once the
		/// buffer is full, which pins the caller's mark at the cap and makes every later read empty for the
		/// life of the process (two hours of runs lost upstream). Absent or unparsable = -1 = "the newest n";
		/// 0 and up = "everything after this seq". Num() is no use here: it rejects 0 and negatives.</summary>
		private static long Ev_Since(string s)
		{
			long v;
			return !string.IsNullOrEmpty(s) && long.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out v) ? v : -1;
		}
	}
}
