// NT8Bridge.Liveness.cs — which strategy Ids are alive, and which strategy owns an order. Reads only.
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md "Module seams").
//
// Shared by the core reads (NT8Bridge.Account.cs order rows, NT8Bridge.Workspace.cs grid rows) and by the
// Sim desk verbs (NT8BridgeDesk.cs). It lives apart from the desk so that a build which drops the desk
// module still compiles: every module file is droppable, the core never depends on one.
//
// "Alive" is judged by the strategy Id, over every instance in StrategyBase.All: NinjaTrader runs a
// CLONE that shares the Id (see NT8BridgeStrategyRun.cs), so the instance a grid row or an order points
// at can read Finalized while its clone trades. An Id is alive when ANY instance with it is in
// Configure, Active, DataLoaded, Historical, Transition or Realtime. Everything else is dead.

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		/// <summary>The State of one instance as text, read off the request thread. `State` is a plain CLR
		/// property, so this needs no dispatcher hop. Null when the read throws.</summary>
		private static string Sr_State(StrategyBase s)
		{
			try { return s.State.ToString(); } catch { return null; }
		}

		private static string Ord_SafeText(Func<string> f) { try { return f(); } catch { return null; } }

		/// <summary>The order name NT8BridgeOrders.cs puts on every leg it sends. It is how an order's owner
		/// reads back as "module" in /orders/status and in a change or cancel plan.</summary>
		private const string Ord_OrderName = "NT8Bridge";

		// ── liveness by strategy Id ─────────────────────────────────────────────
		private static bool Desk_Active(string st)
		{
			return st == "Configure" || st == "Active" || st == "DataLoaded" || st == "Historical"
				|| st == "Transition" || st == "Realtime";
		}

		private static int Desk_Rank(string st)
		{
			if (st == "Realtime") return 4;
			if (Desk_Active(st)) return 3;
			if (st == "Terminated") return 2;
			if (st == "Finalized") return 1;
			return 0;
		}

		private static StrategyBase[] Desk_All()
		{
			try { return StrategyBase.All.ToArray(); }
			catch (Exception ex) { Log("/desk StrategyBase.All: " + Deep(ex)); return new StrategyBase[0]; }
		}

		/// <summary>Id -> the most alive State seen over every instance with that Id.</summary>
		private static Dictionary<long, string> Desk_IdStates(StrategyBase[] all)
		{
			var map = new Dictionary<long, string>();
			foreach (var s in all)
			{
				if (s == null) continue;
				long id; string st;
				try { id = s.Id; } catch { continue; }
				if (id == 0) continue;
				st = Sr_State(s);
				string had;
				if (!map.TryGetValue(id, out had) || Desk_Rank(st) > Desk_Rank(had)) map[id] = st;
			}
			return map;
		}

		/// <summary>Who owns an order, with the owning strategy's Id and whether that Id is alive.</summary>
		private sealed class Desk_OwnerInfo
		{
			public string Kind = "manual";		// module | atm | strategy | automated | manual
			public string Label = "manual";
			public long Id;
			public string Name, State;
			public bool? Alive;				// null when there is no strategy owner
		}

		/// <summary>Order.GetOwnerStrategy() first. When it answers null for an automated order (seen for the
		/// orders of a disabled, terminated instance) every instance in StrategyBase.All is searched for the
		/// order in its own Orders collection. `all`/`states` may be passed in to avoid one scan per order.</summary>
		private static Desk_OwnerInfo Desk_Owner(Order o, StrategyBase[] all = null, Dictionary<long, string> states = null)
		{
			var r = new Desk_OwnerInfo();
			try { if (string.Equals(o.Name, Ord_OrderName, StringComparison.Ordinal)) { r.Kind = r.Label = "module"; return r; } }
			catch { }
			StrategyBase strat = null;
			try { strat = o.GetOwnerStrategy(); } catch { }
			if (strat is AtmStrategy) { r.Kind = r.Label = "atm"; return r; }
			try { if (strat == null && o.GetOwnerServerStrategy() != null) { r.Kind = r.Label = "atm"; return r; } }
			catch { }
			bool automated = false;
			try { automated = o.OrderEntry == OrderEntry.Automated; } catch { }
			if (strat == null && automated)
			{
				if (all == null) all = Desk_All();
				strat = Desk_FindOwner(o, all);
			}
			if (strat == null)
			{
				if (automated) { r.Kind = "automated"; r.Label = "strategy (owner instance not found)"; }
				return r;
			}
			r.Kind = "strategy";
			try { r.Id = strat.Id; } catch { }
			try { r.Name = strat.Name; } catch { }
			if (string.IsNullOrEmpty(r.Name)) { try { r.Name = strat.GetType().Name; } catch { } }
			if (states == null) states = Desk_IdStates(all ?? Desk_All());
			string st;
			r.State = r.Id != 0 && states.TryGetValue(r.Id, out st) ? st : Sr_State(strat);
			r.Alive = Desk_Active(r.State);
			r.Label = "strategy " + (string.IsNullOrEmpty(r.Name) ? "(name unreadable)" : r.Name)
				+ " #" + r.Id.ToString(CultureInfo.InvariantCulture) + (r.Alive == true ? " alive" : " dead");
			return r;
		}

		private static StrategyBase Desk_FindOwner(Order o, StrategyBase[] all)
		{
			string oid = Ord_SafeText(() => o.OrderId);
			foreach (var s in all)
			{
				if (s == null) continue;
				try
				{
					var ords = s.Orders;
					if (ords == null) continue;
					foreach (var x in ords.ToArray())
						if (ReferenceEquals(x, o) || (oid != null && x != null
							&& string.Equals(Ord_SafeText(() => x.OrderId), oid, StringComparison.Ordinal))) return s;
				}
				catch { }
			}
			return null;
		}

		/// <summary>The owner fields every order row carries beside `owner`.</summary>
		private static string Desk_OwnerPairs(Desk_OwnerInfo w)
		{
			return P("ownerStrategyId", w.Id == 0 ? "null" : Q(w.Id.ToString(CultureInfo.InvariantCulture)))
				+ "," + P("ownerState", Q(w.State))
				+ "," + P("ownerAlive", w.Alive == null ? "null" : (w.Alive.Value ? "true" : "false"));
		}
	}
}
