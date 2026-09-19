// NT8Bridge.Feeds.cs — feeds and connections (READ ONLY): GET /feedhealth, GET /connections.
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md
// "Module seams"). Contract: docs/api/feeds.md.
//
// Portions of this file are derived from cli-nt-bridge
// (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
// MIT License. The full notice is in NOTICE at the repository root.
// Derived: the drop classifier rule (NT8BridgeServer.cs:3698-3703), the
// configured-union-live report shape and its "a connected connection must never be missing"
// lesson (:3726-3760), and the negative-age clamp (:3816-3817).

#region Using declarations
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		// ── feeds module (READ ONLY) ───────────────────────────────────────────
		//
		// NOTHING HERE CONNECTS, DISCONNECTS OR SUBSCRIBES.
		//   /feedhealth reads what NinjaTrader ALREADY holds: Instrument.GetInstrument(name, create:false)
		//     never creates an instrument and never subscribes, and MarketData.Last is a snapshot property.
		//     There is no market-data subscription call anywhere below, no MarketDepth touch, no Instrument.All walk.
		//     Consequence, and it is the contract: an instrument nothing is watching (no chart, no SuperDom,
		//     no strategy) has a null Last forever. That is "nobody is looking", not "the feed is dark" —
		//     hasSeenMarketData is what separates the two.
		//   /connections reads two collections and a ring the status event fills. Connect/Disconnect and
		//     reconnect are owned by NT8BridgeOps.cs, never here.
		//
		// No Ui(), no dispatcher hop anywhere in this file: Cbi objects are thread-agnostic, and a frozen
		// UI thread is exactly when someone asks whether the feed is still alive.
		//
		// Untrusted text: connection names, provider names and instrument names are free text that a broker,
		// a data feed or a third-party AddOn wrote. They are DATA for whoever reads them, never instructions.

		private const int Feeds_EventCap = 500;		// witnessed connection-status transitions

		/// <summary>One witnessed Connection.ConnectionStatusUpdate, flattened to strings AT EVENT TIME —
		/// the handler runs on arbitrary NinjaTrader threads and must not hold a reference to anything live.</summary>
		private sealed class Feeds_Transition
		{
			public DateTime Time;
			public string Name, Status, PriceStatus, PreviousStatus, Error, Class;
		}

		private static readonly Ring<Feeds_Transition> Feeds_Events = new Ring<Feeds_Transition>(Feeds_EventCap);

		/// <summary>name -> the class of the last transition witnessed ("connected" | "inadvertent" | "user" | "failed").
		/// Written from arbitrary NT threads, read from HttpListener threads: concurrent, never a plain Dictionary.</summary>
		private static readonly ConcurrentDictionary<string, string> Feeds_DropClass =
			new ConcurrentDictionary<string, string>(StringComparer.OrdinalIgnoreCase);

		private static readonly object Feeds_Gate = new object();
		private static bool Feeds_Subscribed;

		/// <summary>Seam (NOTES.md "Module seams"): GET /feedhealth and GET /connections. Null for anything else.</summary>
		private static string Route_Feeds(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (method != "GET" || seg.Length != 1) return null;
			if (seg[0] == "feedhealth") return Feeds_HealthJson(q, ref status);
			if (seg[0] == "connections") return Feeds_ConnectionsJson(q);
			return null;
		}

		/// <summary>Seam: one static-event subscription, before the first request is served. The classifier can only
		/// be built at event time — nothing readable afterwards says WHY a connection left Connected — so a
		/// connection already down when this assembly loaded reads dropClass null forever, by construction.
		/// Verified live on 8.1.8.2: the `+=` itself makes NinjaTrader REPLAY Connecting -> Connected for every
		/// connection it still holds (two ring entries stamped with the start time, nothing in NT8's own log),
		/// so a connected connection reads "connected" straight after a hot reload. A disconnected connection is
		/// dropped from Connection.Connections, is not replayed, and stays null.</summary>
		private static void Start_Feeds()
		{
			lock (Feeds_Gate)
			{
				if (!Feeds_Subscribed)
				{
					try
					{
						Cbi.Connection.ConnectionStatusUpdate += Feeds_OnConnStatus;
						Feeds_Subscribed = true;
					}
					catch (Exception ex) { Log("Start_Feeds: ConnectionStatusUpdate subscribe failed — " + Deep(ex)); }
				}
			}
			Compat.Set("Connection.ConnectionStatusUpdate", Feeds_Subscribed,
				Feeds_Subscribed ? "subscribed; drop classifier only sees transitions from here on" : "subscribe failed — dropClass stays null", null);
		}

		/// <summary>Seam: the "-=" for the one "+=" above. Runs on NT8's UI thread after the listener closed, and must
		/// be idempotent — the core can run the stop hooks twice when a rebind retry races Stop().</summary>
		private static void Stop_Feeds()
		{
			lock (Feeds_Gate)
			{
				if (!Feeds_Subscribed) return;
				try { Cbi.Connection.ConnectionStatusUpdate -= Feeds_OnConnStatus; } catch { }
				Feeds_Subscribed = false;
			}
		}

		/// <summary>Runs on arbitrary NinjaTrader threads, including a connection's own. Capture, classify, one Add,
		/// all inside catch {} — no dispatcher, no file I/O, no NinjaTrader call back into the connection.
		///
		/// The rule (cli-nt-bridge NT8BridgeServer.cs:3698-3703, kept verbatim): Connected -> "connected";
		/// ConnectionLost -> "inadvertent"; Disconnected -> "user" when the error is NoError or UserAbort, else
		/// "inadvertent"; Connecting/Disconnecting are transient and LEAVE THE PRIOR CLASSIFICATION ALONE.
		/// "user" means a human parked it. Nothing in this repo acts on the difference — reconnect is owned elsewhere —
		/// but it cannot be recovered later, so it is recorded now.</summary>
		private static void Feeds_OnConnStatus(object sender, Cbi.ConnectionStatusEventArgs e)
		{
			try
			{
				if (e == null) return;
				var row = new Feeds_Transition { Time = DateTime.Now };
				try { var c = e.Connection; var o = c == null ? null : c.Options; row.Name = o == null ? null : o.Name; } catch { }
				try { row.Status = e.Status.ToString(); } catch { }
				try { row.PriceStatus = e.PriceStatus.ToString(); } catch { }
				try { row.PreviousStatus = e.PreviousStatus.ToString(); } catch { }
				try { row.Error = e.Error.ToString(); } catch { }

				string cls = null;
				try
				{
					var s = e.Status;
					if (s == Cbi.ConnectionStatus.Connected) cls = "connected";
					else if (s == Cbi.ConnectionStatus.ConnectionLost) cls = "inadvertent";
					// Observed on NinjaTrader 8.1.8.2: a connect NinjaTrader REFUSES goes Connecting -> Disconnected with an
					// error. Nothing dropped, so it is "failed", not "inadvertent" (a reconnect loop would hammer it).
					else if (s == Cbi.ConnectionStatus.Disconnected && e.PreviousStatus == Cbi.ConnectionStatus.Connecting
						&& e.Error != Cbi.ErrorCode.NoError && e.Error != Cbi.ErrorCode.UserAbort) cls = "failed";
					else if (s == Cbi.ConnectionStatus.Disconnected)
						cls = (e.Error == Cbi.ErrorCode.NoError || e.Error == Cbi.ErrorCode.UserAbort) ? "user" : "inadvertent";
				}
				catch { }

				row.Class = cls;
				if (cls != null && !string.IsNullOrEmpty(row.Name)) Feeds_DropClass[row.Name] = cls;
				Feeds_Events.Add(row);
			}
			catch { }
		}

		// ── GET /feedhealth?instruments=ES%2012-26,MNQ%2012-26 ─────────────────
		/// <summary>Last-tick age per instrument, from the snapshot NinjaTrader already holds. Comma-separated
		/// is safe: an instrument full name contains spaces ("ES 12-26") but never a comma.
		/// ageMs is computed HERE, from one clock (Core.Globals.Now — NinjaTrader's OWN configured application
		/// time zone, Tools > Options > General — against MarketDataEventArgs.Time, which NinjaTrader stamps in
		/// that same zone), so a caller never subtracts two clocks that disagree. DateTime.Now is the PC's local
		/// zone and is NOT the same clock: when the user's NT8 zone differs from Windows' zone (routine — US
		/// Eastern/Exchange NT8 on a Pacific PC), subtracting DateTime.Now from a tick's Time silently reports a
		/// dead feed as ageMs 0 for hours, or a live feed as ageMs of several hours. A future-stamped tick clamps
		/// to 0, never negative.</summary>
		private static string Feeds_HealthJson(System.Collections.Specialized.NameValueCollection q, ref int status)
		{
			string csv = q["instruments"];
			if (string.IsNullOrEmpty(csv))
				return Err(ref status, 400, "GET /feedhealth needs ?instruments=<comma-separated full names>, e.g. instruments=ES%2012-26,MNQ%2012-26");

			var names = csv.Split(',').Select(s => (s ?? "").Trim()).Where(s => s.Length > 0).ToArray();
			if (names.Length == 0)
				return Err(ref status, 400, "GET /feedhealth: ?instruments= held no names");

			DateTime now = Core.Globals.Now;
			var rows = new List<string>();
			foreach (var name in names)
			{
				try { rows.Add(Feeds_OneFeed(name, now)); }
				catch (Exception ex)
				{
					// One bad name degrades to a row with an error, never a 500 for the whole request.
					rows.Add(Obj(
						P("instrument", Q(name)),
						P("resolvedName", "null"),
						P("found", "false"),
						P("hasSeenMarketData", "null"),
						P("lastPrice", "null"),
						P("lastTickTime", "null"),
						P("ageMs", "null"),
						P("error", Q(Deep(ex)))));
				}
			}

			return Obj(
				P("nowUtc", TmUtc(DateTime.UtcNow)),
				P("now", Tm(now)),
				P("feeds", Arr(rows)),
				P("anyNonSim", AnyNonSimConnected() ? "true" : "false"),
				P("note", Q("read only: nothing was created and nothing was subscribed. ageMs null is STALE, never fresh — "
					+ "hasSeenMarketData false means no tick has EVER arrived for this instrument in this NinjaTrader session "
					+ "(nothing is watching it, OR what watches it has no ticking feed: a parked Playback, a provider that is down), "
					+ "true with a null ageMs means the snapshot is unreadable. Under Playback, lastTickTime is REPLAY time "
					+ "and `now` may be the wall clock, so ageMs is not a freshness measure there — read GET /playback.")));
		}

		private static string Feeds_OneFeed(string name, DateTime now)
		{
			// create:false — GetInstrument neither creates an instrument nor subscribes to market data.
			Cbi.Instrument instr = Cbi.Instrument.GetInstrument(name, false);
			if (instr == null)
				return Obj(
					P("instrument", Q(name)),
					P("resolvedName", "null"),
					P("found", "false"),
					P("hasSeenMarketData", "null"),
					P("lastPrice", "null"),
					P("lastTickTime", "null"),
					P("ageMs", "null"),
					P("error", "null"));

			string resolved = null;
			try { resolved = instr.FullName; } catch { }

			bool? seen = null;
			try { seen = instr.HasSeenMarketData; } catch { }

			double? price = null;
			DateTime tickTime = DateTime.MinValue;
			try
			{
				var md = instr.MarketData;
				var last = md == null ? null : md.Last;
				if (last != null)
				{
					try { price = last.Price; } catch { }
					try { tickTime = last.Time; } catch { }
				}
			}
			catch { }
			// Observed on NinjaTrader 8.1.8.2 (chart open, Playback connected but parked): NinjaTrader holds a PLACEHOLDER
			// Last — price 0, stamped with the connection's start time — before any tick has arrived. 0 is not a price.
			if (seen == false && price == 0) price = null;

			string ageMs = "null";
			if (tickTime != DateTime.MinValue)
			{
				double ms = (now - tickTime).TotalMilliseconds;
				if (ms < 0) ms = 0;				// a future-stamped tick reads as fresh, never as a negative age
				ageMs = I((long)ms);
			}

			return Obj(
				P("instrument", Q(name)),
				P("resolvedName", Q(resolved)),
				P("found", "true"),
				P("hasSeenMarketData", seen.HasValue ? (seen.Value ? "true" : "false") : "null"),
				P("lastPrice", price.HasValue ? D(price.Value) : "null"),
				P("lastTickTime", Tm(tickTime)),
				P("ageMs", ageMs),
				P("error", "null"));
		}

		// ── GET /connections?n=20 ──────────────────────────────────────────────
		/// <summary>The union of the CONFIGURED list (Globals.ConnectOptions) and what NinjaTrader actually
		/// holds (Connection.Connections), each row tagged `configured` or `live-only`.
		///
		/// A CONNECTED CONNECTION MUST NEVER BE MISSING FROM THIS REPORT. Walking the configuration alone is what
		/// hid a live connection upstream (observed: the menu showed 8 entries connected, the report
		/// listed 6, and a reader concluded nothing was connected). The second loop below is that fix.</summary>
		private static string Feeds_ConnectionsJson(System.Collections.Specialized.NameValueCollection q)
		{
			int n = Num(q["n"], 20);

			Cbi.Connection[] live;
			try { live = ConnSnapshot(); } catch (Exception ex) { Log("/connections: live snapshot — " + Deep(ex)); live = new Cbi.Connection[0]; }

			// Not a locked read: NinjaTrader guards this collection with its own internal
			// Globals.SyncConnectOptions, which an AddOn cannot reach (internal). Locking the collection
			// instance itself synchronises with nothing, so this is a tolerant, not a locked, copy — an
			// index-based walk that stops (rather than throws) if the collection changes underneath it
			// while the user edits Tools > Connections. The live loop below still carries the real
			// guarantee: every connected connection is unconditionally emitted regardless of this list.
			var cfg = new List<Cbi.ConnectOptions>();
			try
			{
				var configured = Core.Globals.ConnectOptions;
				if (configured != null)
					for (int i = 0; i < configured.Count; i++)
					{
						try { cfg.Add(configured[i]); }
						catch (Exception ex) { Log("/connections: ConnectOptions snapshot truncated — " + Deep(ex)); break; }
					}
			}
			catch (Exception ex) { Log("/connections: ConnectOptions snapshot — " + Deep(ex)); }
			// Observed on NinjaTrader 8.1.8.2: a broker demo connection (it can route orders, so it counts as
			// live) is NOT in Globals.ConnectOptions at all. Brokerage-login connections live in their own public list.
			try
			{
				var brokerage = Core.Globals.BrokerageConnectOptions;
				if (brokerage != null) cfg.AddRange(brokerage.ToArray());
			}
			catch (Exception ex) { Log("/connections: BrokerageConnectOptions snapshot — " + Deep(ex)); }

			var rows = new List<string>();
			var emitted = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

			foreach (var o in cfg)
			{
				if (o == null) continue;
				string name = null;
				try { name = o.Name; } catch { }
				if (name == null || !emitted.Add(name)) continue;		// the two configured lists may overlap: one row per name
				rows.Add(Feeds_ConnRow(name, "configured", o, Feeds_FindLive(live, name)));
			}

			foreach (var c in live)
			{
				if (c == null) continue;
				string name = null;
				try { var o = c.Options; name = o == null ? null : o.Name; } catch { }
				// A live connection with no Options has no name to match on: emit it anyway, under "?" — the same
				// placeholder /health uses — because a row that cannot be named still says something is connected.
				if (name != null && emitted.Contains(name)) continue;
				if (name != null) emitted.Add(name);
				Cbi.ConnectOptions opts = null;
				try { opts = c.Options; } catch { }
				rows.Add(Feeds_ConnRow(name ?? "?", "live-only", opts, c));
			}

			long since = Feeds_Since(q["since"]);
			long firstSeq, seen, dropped;
			var window = Feeds_Events.Read(since, n, out firstSeq, out seen, out dropped);
			long index = window.Count > 0 ? firstSeq + window.Count - 1 : seen;

			return Obj(
				P("connections", Arr(rows)),
				P("anyLiveConnected", AnyLiveConnected() ? "true" : "false"),
				P("anyNonSimConnected", AnyNonSimConnected() ? "true" : "false"),
				P("subscribed", Feeds_IsSubscribed() ? "true" : "false"),
				P("events", Arr(window.Select(t => Obj(
					P("t", Tm(t.Time)),
					P("name", Q(t.Name)),
					P("status", Q(t.Status)),
					P("priceStatus", Q(t.PriceStatus)),
					P("previousStatus", Q(t.PreviousStatus)),
					P("error", Q(t.Error)),
					P("class", Q(t.Class)))))),
				P("index", I(index)),
				P("dropped", I(dropped)),
				P("note", Q("read only: this endpoint never connects or disconnects anything. dropClass is null until a "
					+ "status transition is witnessed, so a connection already down when this assembly loaded reads null, "
					+ "which is 'not known', not 'fine'. NinjaTrader REPLAYS the current status of every connection it still "
					+ "holds to a new subscriber, so events stamped with the AddOn's start time are that replay, not a reconnect.")));
		}

		/// <summary>Signed cursor parse, same convention as the events module's Ev_Since: Num() rejects 0 and
		/// negatives, but the ring's `since` contract needs -1 ("newest n") and 0 ("everything") to work.</summary>
		private static long Feeds_Since(string s)
		{
			long v;
			return !string.IsNullOrEmpty(s) && long.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out v) ? v : -1;
		}

		private static Cbi.Connection Feeds_FindLive(Cbi.Connection[] live, string name)
		{
			foreach (var c in live)
			{
				if (c == null) continue;
				try { var o = c.Options; if (o != null && string.Equals(o.Name, name, StringComparison.OrdinalIgnoreCase)) return c; }
				catch { }
			}
			return null;
		}

		/// <summary>One row. `status`/`priceStatus` null = NinjaTrader holds no Connection object for this configured
		/// entry at all (never connected this session, or fully torn down) — not the same claim as "Disconnected",
		/// which is a status NinjaTrader itself reported. provider/canManageOrders read exactly as /health reports them.</summary>
		private static string Feeds_ConnRow(string name, string source, Cbi.ConnectOptions o, Cbi.Connection c)
		{
			string statusText = null, priceText = null;
			bool connected = false;
			if (c != null)
			{
				try { statusText = c.Status.ToString(); } catch { }
				try { priceText = c.PriceStatus.ToString(); } catch { }
				connected = IsConnected(c);
			}

			string provider = "unknown";
			string canManage = "null";
			if (o != null)
			{
				try { provider = o.Provider.ToString(); } catch { provider = null; }
				try { canManage = o.CanManageOrders ? "true" : "false"; } catch { canManage = "null"; }
			}

			string cls;
			if (!Feeds_DropClass.TryGetValue(name, out cls)) cls = null;

			return Obj(
				P("name", Q(name)),
				P("source", Q(source)),
				P("provider", Q(provider)),
				P("canManageOrders", canManage),
				P("status", Q(statusText)),
				P("priceStatus", Q(priceText)),
				P("connected", connected ? "true" : "false"),
				P("dropClass", Q(cls)),
				P("inadvertentlyDropped", (cls == "inadvertent" && !connected) ? "true" : "false"),
				P("live", (c != null && IsConnected(c) && IsLive(c)) ? "true" : "false"),
				P("nonSim", (c != null && IsConnected(c) && IsNonSim(c)) ? "true" : "false"));
		}

		private static bool Feeds_IsSubscribed() { lock (Feeds_Gate) return Feeds_Subscribed; }
	}
}
