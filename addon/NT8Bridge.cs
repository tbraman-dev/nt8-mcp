// NT8Bridge.cs — local HTTP API for NinjaTrader 8 (contract: nt8-mcp/API.md).
//
// One HttpListener on http://localhost:7891/, started when the Control Center window appears and stopped
// when it closes (or at State.Terminated). Everything that touches a chart hops onto that chart's
// ChartControl.Dispatcher, reads, and returns — no Draw.*, no market data or depth subscriptions, nothing
// slow: a long or drawing callback on the chart thread freezes the chart.
//
// Read only, plus: reload NinjaScript on a chart, a PNG written to disk, and headless backtests (v1.1).
// A backtest runs a strategy on the Backtest account only — simulated fills, never a Sim or live account:
// the account name is checked against Account.BackTestAccountName before anything runs, and no other
// code path submits, changes or cancels an order or changes account state.
//
// Endpoints: /health /windows /log /compat /ntstatus (core)   /charts /chart/{id} /chart/{id}/bars
//            /chart/{id}/indicators /chart/{id}/drawings /chart/{id}/reload /chart/{id}/screenshot /output/window
//            /account /strategies /backtest (POST) /backtests /backtest/{id} (GET, DELETE)
// {id} is "c<N>" assigned in OnWindowCreated or by the Start() reseed, or "first"; a backtest id is "b<N>".
//
// This file is the CORE of one partial class: life cycle (incl. reload survival), listener, Handle, Route, Ui,
// JSON, log, window registry, the shared helpers (Deep, Compat, Ring, OutputHub, RangeProblem, the live-connection
// predicates) and the handlers for /health /windows /log /compat /ntstatus. Everything else lives in a module file
// (NT8Bridge.Charts.cs, NT8Bridge.Account.cs, NT8Bridge.Backtest.cs, ...) and plugs in through three seams
// the core discovers by reflection at Start() — see "module seams" below and addon/NOTES.md "Module seams":
//   private static string Route_<Module>(string method, string[] seg, NameValueCollection q, string body, ref int status)
//   private static void Start_<Module>()      private static void Stop_<Module>()

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
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
	public partial class NT8Bridge : AddOnBase
	{
		public const string Version = "1.3.0";
		private const int Port = 7891;
		private static readonly TimeSpan UiTimeout = TimeSpan.FromSeconds(5);
		private const int RebindTries = 5;													// bounded, off the UI thread
		private static readonly TimeSpan RebindWait = TimeSpan.FromSeconds(2);

		// ── state ──────────────────────────────────────────────────────────────
		private static readonly object gate = new object();
		private static readonly List<Window> windows = new List<Window>();          // every NT8 window we have seen, in creation order
		private static readonly Dictionary<Window, string> chartIds = new Dictionary<Window, string>();
		// The counter lives in the AppDomain, not in a static: NT8 loads every recompiled NinjaTrader.Custom
		// into the SAME AppDomain, so this value survives a hot reload while the statics come up empty. Without
		// it the Reseed() in AfterBind() handed c1, c2, ... out again and a client's pre-reload "c2" resolved to
		// a DIFFERENT chart — 200, well-formed, wrong. Ids now never repeat within an NT8 session, so a stale
		// id 404s (loud) instead. It does restart at c1 when NinjaTrader itself restarts.
		private const string ChartCounterSlot = "NT8Bridge.chartCounter";
		private static int chartCounter = LoadChartCounter();

		private static int LoadChartCounter()
		{
			try { object v = AppDomain.CurrentDomain.GetData(ChartCounterSlot); return v is int ? (int)v : 0; }
			catch { return 0; }
		}

		private static HttpListener listener;
		private static Thread listenThread;
		private static volatile bool running;
		private static DateTime startedAt;
		private static bool binding;				// under gate: a rebind retry loop owns the start
		private static int startEpoch;				// under gate: bumped by Stop(); a retry loop from an older epoch gives up
		private static readonly DateTime loadedUtc = DateTime.UtcNow;		// when this assembly's statics came up: the build-time stand-in for a byte[]-loaded assembly
		private bool live;							// this INSTANCE reached Configure or saw a window (NT8 also makes throwaway instances)

		// ── module seams: discovered by reflection in Discover(), at Start() ────
		/// <summary>A module's router. Returns null for anything it does not fully handle: only the core emits the 404.</summary>
		private delegate string RouteFn(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status);
		// Replaced as a whole, never mutated: HTTP threads read them without a lock.
		private static volatile KeyValuePair<string, RouteFn>[] routes = new KeyValuePair<string, RouteFn>[0];
		private static volatile KeyValuePair<string, Action>[] startHooks = new KeyValuePair<string, Action>[0];
		private static volatile KeyValuePair<string, Action>[] stopHooks = new KeyValuePair<string, Action>[0];

		// ── log: ring buffer + file ───────────────────────────────────────
		private static readonly Ring<string> ring = new Ring<string>(500);

		private static void Log(string msg)
		{
			string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff", CultureInfo.InvariantCulture) + " " + msg;
			ring.Add(line);
			try { File.AppendAllText(Path.Combine(Core.Globals.UserDataDir, "NT8Bridge.log"), line + Environment.NewLine); }
			catch { }
		}

		// ── life cycle ─────────────────────────────────────────────────────────
		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Name		= "NT8 Bridge";
				Description	= "Read-only HTTP API on localhost:" + Port + " for the nt8-mcp MCP server.";
			}
			else if (State == State.Configure)
			{
				// A NinjaScript hot reload does not re-create the Control Center, so OnWindowCreated alone would
				// leave the new assembly without a listener. Start() is idempotent; both callers stay.
				live = true;
				try { Start(); } catch (Exception ex) { Log("Configure: " + Deep(ex)); }
			}
			else if (State == State.Terminated)
			{
				// Only an instance that started something may stop it: a throwaway instance NT8 builds and drops must not
				// close the live instance's listener. Stop() is also where OutputHub's '-=' lives (unconditional in there).
				if (live) Stop();
			}
		}

		/// <summary>Under gate. The one place a window enters the registry and a chart gets its id.</summary>
		private static void Register(Window window)
		{
			if (window == null) return;
			if (!windows.Contains(window)) windows.Add(window);
			if (window is Gui.Chart.Chart && !chartIds.ContainsKey(window))
			{
				chartIds[window] = "c" + (++chartCounter);
				try { AppDomain.CurrentDomain.SetData(ChartCounterSlot, chartCounter); } catch { }
			}
		}

		protected override void OnWindowCreated(Window window)
		{
			try
			{
				live = true;
				lock (gate) Register(window);
				if (window is Gui.ControlCenter) Start();
			}
			catch (Exception ex) { Log("OnWindowCreated: " + ex.Message); }
		}

		protected override void OnWindowDestroyed(Window window)
		{
			try
			{
				lock (gate) { windows.Remove(window); chartIds.Remove(window); }
				if (window is Gui.ControlCenter) Stop();
			}
			catch (Exception ex) { Log("OnWindowDestroyed: " + ex.Message); }
		}

		// ── HTTP server ────────────────────────────────────────────────────────
		/// <summary>Idempotent. Called from State.Configure (every load, incl. a hot reload) and from the Control
		/// Center's OnWindowCreated. Never throws, never blocks: a busy port is retried off this thread.</summary>
		private static void Start()
		{
			int epoch;
			string busy;
			lock (gate)
			{
				if (running || binding) return;
				try { JsonSelfTest(); CoreSelfTest(); Discover(); }
				catch (Exception ex) { Log("start: " + Deep(ex)); }
				epoch	= startEpoch;
				busy	= TryBind();
				if (busy != null) binding = true;
			}
			if (busy == null) { AfterBind(); return; }
			// After a hot reload the OLD assembly's listener may still be closing; its Close() needs a moment to land.
			Log("port " + Port + " busy (" + busy + "); retrying " + RebindTries + "x every " + RebindWait.TotalSeconds.ToString(CultureInfo.InvariantCulture) + "s off the UI thread");
			ThreadPool.QueueUserWorkItem(_ => Rebind(epoch));
		}

		/// <summary>Under gate. null = bound and `running`; else why not. Never throws: a busy port must leave NT8 alone.</summary>
		private static string TryBind()
		{
			HttpListener l = null;
			try
			{
				l = new HttpListener();
				l.Prefixes.Add("http://localhost:" + Port + "/");
				l.Start();				// http.sys queues requests from here on; nothing is served until Listen runs
				listener	= l;
				running		= true;
				startedAt	= DateTime.Now;
				return null;
			}
			catch (Exception ex)
			{
				try { if (l != null) l.Close(); } catch { }
				return ex.Message;
			}
		}

		/// <summary>Thread-pool thread, never a UI thread. Gives up when Stop() bumped the epoch, or after RebindTries.</summary>
		private static void Rebind(int epoch)
		{
			for (int i = 1; i <= RebindTries; i++)
			{
				Thread.Sleep(RebindWait);
				string busy;
				lock (gate)
				{
					if (epoch != startEpoch) return;			// Stop() came in between; it already cleared `binding`
					busy = TryBind();
					if (busy == null || i == RebindTries) binding = false;
				}
				if (busy == null) { Log("bound on retry " + i); AfterBind(); return; }
				if (i == RebindTries)
					Log("could not start on port " + Port + " after " + RebindTries + " retries: " + busy
						+ " — another process, or the pre-reload assembly, still owns it (compare /ntstatus with the files on disk)");
			}
		}

		/// <summary>The listener is bound and `running` is true. Outside the gate: a Start_* hook may hop to a window
		/// thread that is itself waiting on the gate in OnWindowCreated. Modules are up before the first request is
		/// served, and only if the listener is.</summary>
		private static void AfterBind()
		{
			Compat.Resolve("Window._ownedWindows", () => typeof(Window).GetField("_ownedWindows", BindingFlags.Instance | BindingFlags.NonPublic));
			Reseed();
			OutputHub.Subscribe();
			// One line on Output tab 2 per start: tells the user the bridge is up, and it is the hub's own end-to-end
			// check — UiSelfTest() looks for it coming back through OutputEvent, ring and listener fan-out both.
			hubEcho = false;
			OutputHub.Register(HubEcho);
			try { Code.Output.Process(HubEchoText + Version + " listening on http://localhost:" + Port + "/", PrintTo.OutputTab2); } catch { }
			RunHooks(startHooks);
			listenThread = new Thread(Listen) { IsBackground = true, Name = "NT8Bridge" };
			listenThread.Start();
			Log("listening on http://localhost:" + Port + "/  v" + Version);
			ThreadPool.QueueUserWorkItem(_ => UiSelfTest());
			// Only the retry path can lose this race: Stop() ran while the hooks above were still starting things.
			// Undo it — which is why every Stop_* hook has to be idempotent.
			if (!running) { OutputHub.Unsubscribe(); RunHooks(stopHooks); }
		}

		/// <summary>After a hot reload the statics are empty and NT8 re-creates no window, so seed the registry
		/// from Globals.AllWindows (a plain collection: no dispatcher) and give every chart an id. Adds only; removal
		/// stays with OnWindowDestroyed. Owned windows (dialogs) are never stored — KnownWindows() reads them fresh.</summary>
		private static void Reseed()
		{
			try
			{
				var all = TopWindows();
				int charts;
				lock (gate)
				{
					foreach (var w in all) Register(w);
					charts = chartIds.Count;
				}
				Log("reseed: " + all.Length + " windows in Globals.AllWindows, " + charts + " chart(s) registered");
			}
			catch (Exception ex) { Log("reseed: " + Deep(ex)); }
		}

		/// <summary>Fill the three seam tables from this class's own methods, sorted by name. A method with the
		/// right prefix and the wrong signature is logged and skipped; nothing here throws out of Start().</summary>
		private static void Discover()
		{
			var r = new List<KeyValuePair<string, RouteFn>>();
			var up = new List<KeyValuePair<string, Action>>();
			var down = new List<KeyValuePair<string, Action>>();
			try
			{
				// 'ref int' surfaces as System.Int32& — typeof(int) matches nothing.
				Type[] routeSig = { typeof(string), typeof(string[]), typeof(System.Collections.Specialized.NameValueCollection), typeof(string), typeof(int).MakeByRefType() };
				var methods = typeof(NT8Bridge).GetMethods(BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.DeclaredOnly)
					.OrderBy(m => m.Name, StringComparer.Ordinal);
				foreach (var m in methods)
				{
					bool isRoute = m.Name.StartsWith("Route_", StringComparison.Ordinal);
					bool isStart = m.Name.StartsWith("Start_", StringComparison.Ordinal);
					bool isStop	 = m.Name.StartsWith("Stop_", StringComparison.Ordinal);
					if (!isRoute && !isStart && !isStop) continue;
					try
					{
						var ps = m.GetParameters().Select(p => p.ParameterType).ToArray();
						if (isRoute)
						{
							if (m.ReturnType != typeof(string) || !ps.SequenceEqual(routeSig)) { Log("seam: " + m.Name + " skipped — signature is not (string, string[], NameValueCollection, string, ref int) : string"); continue; }
							r.Add(new KeyValuePair<string, RouteFn>(m.Name, (RouteFn)Delegate.CreateDelegate(typeof(RouteFn), m)));
						}
						else
						{
							if (m.ReturnType != typeof(void) || ps.Length != 0) { Log("seam: " + m.Name + " skipped — signature is not () : void"); continue; }
							(isStart ? up : down).Add(new KeyValuePair<string, Action>(m.Name, (Action)Delegate.CreateDelegate(typeof(Action), m)));
						}
					}
					catch (Exception ex) { Log("seam: " + m.Name + " skipped — " + ex.Message); }
				}
			}
			catch (Exception ex) { Log("seam discovery failed: " + ex); }
			routes = r.ToArray(); startHooks = up.ToArray(); stopHooks = down.ToArray();
			foreach (var kv in routes)		Log("seam: route " + kv.Key);
			foreach (var kv in startHooks)	Log("seam: start " + kv.Key);
			foreach (var kv in stopHooks)	Log("seam: stop  " + kv.Key);
		}

		/// <summary>Start_*/Stop_* in name order; one module's failure is logged and never stops the rest.</summary>
		private static void RunHooks(KeyValuePair<string, Action>[] hooks)
		{
			foreach (var kv in hooks)
				try { kv.Value(); }
				catch (Exception ex) { Log(kv.Key + ": " + ex); }
		}

		private static void Stop()
		{
			bool wasUp;
			lock (gate)
			{
				startEpoch++;			// a pending rebind retry gives up
				binding	= false;
				wasUp	= running || listener != null;
				running	= false;
				try { if (listener != null) listener.Close(); } catch { }
				listener = null;
			}
			// Always, even when nothing was up: Output.OutputEvent is a static event in NinjaTrader.Core, which is NOT
			// reloaded — a handler left on it pins this whole dead NinjaTrader.Custom after every recompile.
			OutputHub.Unsubscribe();
			if (!wasUp) return;
			try { if (listenThread != null) listenThread.Join(2000); } catch { }
			listenThread = null;
			RunHooks(stopHooks);		// this is NT8's UI thread: a Stop_<Module> must be bounded (see Stop_Backtest)
			Log("stopped");
		}

		private static void Listen()
		{
			while (running)
			{
				HttpListenerContext ctx;
				try { ctx = listener.GetContext(); }
				catch (Exception) { if (!running) break; continue; }		// Close() during GetContext throws; that is the shutdown path
				try { ThreadPool.QueueUserWorkItem(o => Handle(ctx)); }
				catch (Exception ex) { Log("queue: " + ex.Message); }
			}
		}

		private static void Handle(HttpListenerContext ctx)
		{
			string path = "?";
			int status = 200;
			string json;
			var sw = System.Diagnostics.Stopwatch.StartNew();
			try
			{
				path = ctx.Request.Url.AbsolutePath.TrimEnd('/');
				if (path.Length == 0) path = "/";
				string body = "";
				if (ctx.Request.HttpMethod == "POST" || ctx.Request.HttpMethod == "PUT")
					using (var sr = new StreamReader(ctx.Request.InputStream, ctx.Request.ContentEncoding ?? Encoding.UTF8))
						body = sr.ReadToEnd();
				json = Route(ctx.Request.HttpMethod, path, ctx.Request.QueryString, body, out status);
			}
			catch (TimeoutException ex)
			{
				// Ui() throws when a window thread does not take the call. 504, never a document of zeros — and a
				// standing modal dialog is the usual reason, so say whether there is one.
				status	= 504;
				json	= Obj(P("error", Q(ex.Message)), P("standingModal", Q(StandingModal())));
				Log("504 " + path + ": " + ex.Message);
			}
			catch (Exception ex)
			{
				status	= 500;
				json	= Obj(P("error", Q(ex.InnerException == null ? ex.Message : Deep(ex))));		// reflection wraps the real cause
				Log("500 " + path + ": " + ex);
			}

			Log(ctx.Request.HttpMethod + " " + path + (string.IsNullOrEmpty(ctx.Request.Url.Query) ? "" : ctx.Request.Url.Query)
				+ " -> " + status + " " + sw.ElapsedMilliseconds + "ms");
			try
			{
				byte[] buf = Encoding.UTF8.GetBytes(json);
				ctx.Response.StatusCode		= status;
				ctx.Response.ContentType	= "application/json; charset=utf-8";
				ctx.Response.ContentLength64 = buf.Length;
				ctx.Response.OutputStream.Write(buf, 0, buf.Length);
				ctx.Response.OutputStream.Close();
			}
			catch (Exception ex) { Log("write: " + ex.Message); }
		}

		private static string Route(string method, string path, System.Collections.Specialized.NameValueCollection q, string body, out int status)
		{
			status = 200;
			string[] seg = path.Split(new[] { '/' }, StringSplitOptions.RemoveEmptyEntries);

			// PRECEDENCE: the discovered Route_* table first (name order), then the core's own handlers, then the 404.
			// A module that answers sets its own status; one that returns null must leave no trace, so status is reset.
			foreach (var kv in routes)
			{
				string answer = kv.Value(method, seg, q, body, ref status);
				if (answer != null) return answer;
				status = 200;
			}

			if (method == "GET" && seg.Length == 1)
			{
				if (seg[0] == "health")		return Health();
				if (seg[0] == "windows")	return Windows();
				if (seg[0] == "log")		return Obj(P("lines", Arr(ring.Tail(Num(q["n"], 100)).Select(Q))));
				if (seg[0] == "compat")		return CompatJson();
				if (seg[0] == "ntstatus")	return NtStatus();
			}

			status = 404;
			return Obj(P("error", Q("no route for " + method + " " + path)));
		}

		// ── threading helpers ──────────────────────────────────────────────────
		/// <summary>Run f on that dispatcher's thread and hand the result (or the exception) back. NT8 gives most
		/// windows their own UI thread, so "the dispatcher" is never just the application one.</summary>
		private static T Ui<T>(Dispatcher d, Func<T> f) { return Ui(d, f, UiTimeout, null); }

		/// <summary>THROWS TimeoutException when the thread does not take the call in time — it never hands back
		/// default(T) for the caller to serialize as data. WPF aborts an operation that is still queued when the
		/// timeout fires, so f never runs late; one that has already STARTED is waited for (f must be quick).
		/// `what` names the target in the message; null = looked up from the window registry.
		/// Handle() maps the exception to HTTP 504. Never call this while holding `gate`.</summary>
		private static T Ui<T>(Dispatcher d, Func<T> f, TimeSpan timeout, string what)
		{
			if (d == null) throw new Exception("no dispatcher");
			if (d.CheckAccess()) return f();
			T result = default(T);
			Exception err = null;
			bool done = false;
			try
			{
				d.Invoke(new Action(() => { try { result = f(); } catch (Exception ex) { err = ex; } finally { done = true; } }),
					DispatcherPriority.Send, CancellationToken.None, timeout);
			}
			catch (TimeoutException) { }				// the Action overload throws on abort; the older Delegate overloads just return
			catch (OperationCanceledException) { }		// the dispatcher is shutting down
			if (!done)
				throw new TimeoutException((d.HasShutdownStarted ? "UI thread has shut down" : "UI thread did not answer in "
					+ timeout.TotalSeconds.ToString("0.###", CultureInfo.InvariantCulture) + "s") + " (window: " + (what ?? WindowLabel(d)) + ")");
			if (err != null) throw err;
			return result;
		}

		/// <summary>Type name (+ chart id) of the registered window that lives on this dispatcher. No dispatcher access:
		/// a title is a DependencyProperty and the thread we would ask is the one that is not answering.</summary>
		private static string WindowLabel(Dispatcher d)
		{
			try
			{
				lock (gate)
					foreach (var w in windows)
						if (w.Dispatcher == d) { string id; return w.GetType().Name + (chartIds.TryGetValue(w, out id) ? " " + id : ""); }
			}
			catch { }
			try { return "thread " + d.Thread.ManagedThreadId; } catch { return "?"; }
		}

		private static Gui.Chart.Chart FindChart(string id)
		{
			lock (gate)
			{
				foreach (var w in windows)
				{
					string cid;
					if (!chartIds.TryGetValue(w, out cid)) continue;
					if (cid == id || id == "first") return (Gui.Chart.Chart)w;
				}
			}
			throw new BadRequestException("no chart '" + id + "' — call /charts first");   // 404 on /chart routes, 400 on /backtest
		}

		private static string IdOf(Window w) { lock (gate) { string id; return chartIds.TryGetValue(w, out id) ? id : null; } }

		/// <summary>The chart's ChartControl, resolved on the window thread: the active tab, else the first tab
		/// that has one.</summary>
		private static ChartControl Control(Gui.Chart.Chart chart)
		{
			var cc = Ui(chart.Dispatcher, () =>
			{
				var active = chart.ActiveChartControl;
				if (active != null) return active;
				if (chart.MainTabControl != null)
					foreach (var item in chart.MainTabControl.Items)
					{
						var tab = item as ChartTab;
						if (tab != null && tab.ChartControl != null) return tab.ChartControl;
					}
				return null;
			});
			if (cc == null) throw new Exception("chart has no ChartControl yet");
			return cc;
		}

		/// <summary>Every chart read goes through here: on the ChartControl's own dispatcher, in and out fast.</summary>
		private static T OnChart<T>(Gui.Chart.Chart chart, Func<ChartControl, T> f)
		{
			var cc = Control(chart);
			return Ui(cc.Dispatcher, () => f(cc));
		}

		private static ChartBars Primary(ChartControl cc)
		{
			if (cc.BarsArray == null || cc.BarsArray.Count == 0 || cc.BarsArray[0] == null) throw new Exception("chart has no bars yet");
			return cc.BarsArray[0];
		}

		// ── handlers ───────────────────────────────────────────────────────────
		// ── the live-connection guard predicates ──
		/// <summary>Snapshot under the collection lock; every caller evaluates AFTER releasing it — never do I/O while holding the lock.</summary>
		private static Connection[] ConnSnapshot() { lock (Connection.Connections) return Connection.Connections.ToArray(); }

		private static bool IsConnected(Connection c) { return c != null && c.Status == ConnectionStatus.Connected; }

		/// <summary>DATA / metering: anything that is not the Simulator or Playback. Null Options = true.</summary>
		private static bool IsNonSim(Connection c)
		{
			var o = c.Options;
			return o == null || (o.Provider != Provider.Simulator && o.Provider != Provider.Playback);
		}

		/// <summary>ORDER ROUTING: non-sim AND able to manage orders. Null Options = live. There is no Provider.Backtest
		/// (the Backtest account belongs to the Simulator connection). NEVER ConnectOptions' demo flag, NEVER the Cbi mode
		/// enum: a funded broker demo is both and still routes orders to a real broker. Judge by Provider, never by the
		/// connection NAME — names are free text.</summary>
		private static bool IsLive(Connection c)
		{
			var o = c.Options;
			return o == null || (o.Provider != Provider.Simulator && o.Provider != Provider.Playback && o.CanManageOrders);
		}

		/// <summary>Real money at risk right now. Gates /compile?reload=1, /data/download and every ops endpoint.
		/// If the connections cannot be read the answer is TRUE: not knowing is not a licence.</summary>
		private static bool AnyLiveConnected()
		{
			try { return ConnSnapshot().Any(c => IsConnected(c) && IsLive(c)); }
			catch (Exception ex) { Log("AnyLiveConnected: " + Deep(ex) + " — answering true"); return true; }
		}

		/// <summary>A real data provider is up. Refuses nothing by itself; /data/download needs it TRUE to have anything to do.</summary>
		private static bool AnyNonSimConnected()
		{
			try { return ConnSnapshot().Any(c => IsConnected(c) && IsNonSim(c)); }
			catch (Exception ex) { Log("AnyNonSimConnected: " + Deep(ex) + " — answering true"); return true; }
		}

		/// <summary>The one guard for every live-order-routing-sensitive endpoint. null = go ahead; otherwise the complete 409
		/// body, status already set. `force` exists for /compile?reload=1 only; /data/download and
		/// every ops endpoint pass false.</summary>
		private static string RefuseIfLive(ref int status, string what, bool force)
		{
			if (!AnyLiveConnected()) return null;
			if (force) { Log(what + ": a live order-routing connection is up — forced through"); return null; }
			status = 409;
			return Obj(P("error", Q(what + " refused: a live order-routing connection is up (see /health.connections)")), P("anyLive", "true"));
		}

		private static string Health()
		{
			int charts;
			lock (gate) charts = chartIds.Count;
			var conns = new List<string>();
			bool anyLive = true, anyNonSim = true;		// what the two predicates answer when the read fails
			try
			{
				var snap = ConnSnapshot();
				foreach (var c in snap)
				{
					if (c == null) continue;
					var o = c.Options;
					conns.Add(Obj(
						P("name", Q(o != null ? o.Name : "?")),
						P("status", Q(c.Status.ToString())),
						P("provider", Q(o == null ? "unknown" : o.Provider.ToString())),
						P("canManageOrders", o == null ? "null" : (o.CanManageOrders ? "true" : "false"))));
				}
				anyLive		= snap.Any(c => IsConnected(c) && IsLive(c));
				anyNonSim	= snap.Any(c => IsConnected(c) && IsNonSim(c));
			}
			catch (Exception ex) { conns.Add(Obj(P("error", Q(ex.Message)))); }

			var asm = typeof(NT8Bridge).Assembly;
			string asmName = null, asmLoc = null;
			try { asmName = asm.GetName().Name; } catch { }
			try { asmLoc = asm.Location ?? ""; } catch { }		// "" = loaded from a byte[]: that is information, keep it
			int pid = 0; DateTime procStart = DateTime.MinValue;
			try { var p = System.Diagnostics.Process.GetCurrentProcess(); pid = p.Id; procStart = p.StartTime.ToUniversalTime(); } catch { }

			return Obj(
				P("ok", "true"),
				P("addonVersion", Q(Version)),
				P("nt8Version", Q(Core.Globals.ProductVersion)),
				P("startedAt", Tm(startedAt)),
				P("connections", Arr(conns)),
				P("charts", charts.ToString(CultureInfo.InvariantCulture)),
				P("anyLive", anyLive ? "true" : "false"),
				P("anyNonSim", anyNonSim ? "true" : "false"),
				P("standingModal", Q(StandingModal())),
				P("pid", I(pid)),
				P("processStartUtc", TmUtc(procStart)),
				P("assemblyName", Q(asmName)),
				P("assemblyLocation", Q(asmLoc)),
				P("assemblyBuiltUtc", TmUtc(AssemblyBuiltUtc())),
				P("moduleCount", I(routes.Length)),
				P("reflection", Compat.Flags()));
		}

		/// <summary>When the code that is answering was built: the mtime of the file this assembly EXECUTES from — after a
		/// hot reload that is Documents\NinjaTrader 8\tmp\&lt;guid&gt;.dll. Never bin\Custom\NinjaTrader.Custom.dll, whose
		/// mtime and ModuleVersionId both lie after a reload. No file (byte[] load) = the moment this assembly came up.</summary>
		private static DateTime AssemblyBuiltUtc()
		{
			try
			{
				string loc = typeof(NT8Bridge).Assembly.Location;
				if (!string.IsNullOrEmpty(loc) && File.Exists(loc)) return File.GetLastWriteTimeUtc(loc);
			}
			catch { }
			return loadedUtc;
		}

		/// <summary>Is NinjaTrader running the code that is on disk? Executing-assembly build time against the newest
		/// .cs under bin\Custom. Walks the whole tree, so it is its own endpoint and stays off /health.
		/// Self-referential by nature: if a pre-reload assembly still owns the port, IT answers, truthfully and wrongly —
		/// the out-of-band comparison lives on the Python side.</summary>
		private static string NtStatus()
		{
			string loc = null;
			try { loc = typeof(NT8Bridge).Assembly.Location ?? ""; } catch { }
			DateTime built = AssemblyBuiltUtc();
			string newest = null, scanError = null;
			DateTime newestUtc = DateTime.MinValue;
			int count = 0;
			try
			{
				foreach (var f in Directory.EnumerateFiles(Path.Combine(Core.Globals.UserDataDir, "bin", "Custom"), "*.cs", SearchOption.AllDirectories))
				{
					if (!f.EndsWith(".cs", StringComparison.OrdinalIgnoreCase)) continue;		// "*.cs" also matches .csproj
					count++;
					var m = File.GetLastWriteTimeUtc(f);
					if (m > newestUtc) { newestUtc = m; newest = f; }
				}
			}
			catch (Exception ex) { scanError = ex.Message; }
			bool known = newest != null && scanError == null;
			bool newer = known && newestUtc > built;
			return Obj(
				P("runningAssembly", Q(loc)),
				P("assemblyBuiltUtc", TmUtc(built)),
				P("assemblyBuiltFrom", Q(string.IsNullOrEmpty(loc) ? "loadTime" : "file")),
				P("newestSource", Q(newest)),
				P("newestSourceUtc", TmUtc(newestUtc)),
				P("sourcesScanned", I(count)),
				P("sourcesNewerThanRunningCode", known ? (newer ? "true" : "false") : "null"),
				P("verdict", Q(!known ? "unknown" : newer ? "stale" : "current")),
				P("scanError", Q(scanError)));
		}

		/// <summary>The reflection table, plus what the seam discovery found and the OutputHub counters.</summary>
		private static string CompatJson()
		{
			return Obj(
				P("compat", Compat.Json()),
				P("routes", Arr(routes.Select(kv => Q(kv.Key)))),
				P("startHooks", Arr(startHooks.Select(kv => Q(kv.Key)))),
				P("stopHooks", Arr(stopHooks.Select(kv => Q(kv.Key)))),
				P("outputHub", Obj(
					P("subscribed", OutputHub.Subscribed ? "true" : "false"),
					P("seen", I(OutputHub.Lines.Seen)),
					P("dropped", I(OutputHub.Lines.Dropped)),
					P("listeners", I(OutputHub.ListenerCount)))));
		}

		// ── windows ────────────────────────────────────────────────────────────
		/// <summary>Globals.AllWindows is a plain Collection that window threads mutate: snapshot it, retry if it moved.</summary>
		private static Window[] TopWindows()
		{
			for (int i = 0; ; i++)
				try { return Core.Globals.AllWindows.Where(w => w != null).ToArray(); }
				catch (InvalidOperationException) { if (i >= 3) throw; }
		}

		/// <summary>The registry, then anything in Globals.AllWindows it missed, then the dialogs those windows OWN
		/// (Globals.AllWindows does not list owned/modal windows). Window.OwnedWindows is thread-affine, so the owned
		/// list is read off the private field behind it: no dispatcher hop anywhere in here, which is what lets
		/// StandingModal() answer while a UI thread is wedged. `owned` = the windows found only that way.</summary>
		private static List<Window> KnownWindows(out HashSet<Window> owned)
		{
			owned = new HashSet<Window>();
			List<Window> all;
			lock (gate) all = windows.ToList();
			try { foreach (var w in TopWindows()) if (!all.Contains(w)) all.Add(w); } catch { }
			var f = Compat.Get<FieldInfo>("Window._ownedWindows");
			if (f == null) return all;
			for (int i = 0; i < all.Count && all.Count < 2000; i++)		// grows while we walk it: an owned window can own windows
				try
				{
					var oc = f.GetValue(all[i]) as WindowCollection;
					if (oc == null || oc.Count == 0) continue;
					var arr = new Window[oc.Count];
					oc.CopyTo(arr, 0);
					foreach (var o in arr) if (o != null && !all.Contains(o)) { all.Add(o); owned.Add(o); }
				}
				catch { }		// its own thread changed it mid-copy: skip this owner
			return all;
		}

		private static string TitleOf(Window win)
		{
			var nt = win as Gui.Tools.NTWindow;
			return nt != null && !string.IsNullOrEmpty(nt.Caption) ? nt.Caption : (win.Title ?? "");
		}

		/// <summary>A modal must never be clicked away — if one is standing, that is the finding. The title when
		/// its thread gives it up within 500 ms (a modal pumps its own dispatcher frame, so it normally does), else
		/// the type name; null when there is none. Detection itself needs no dispatcher.</summary>
		private static string StandingModal()
		{
			try
			{
				HashSet<Window> owned;
				foreach (var w in KnownWindows(out owned))
				{
					string ty = w.GetType().Name;
					if (ty.IndexOf("MessageBox", StringComparison.OrdinalIgnoreCase) < 0) continue;
					try
					{
						var win = w;
						string t = Ui(win.Dispatcher, () => TitleOf(win), TimeSpan.FromMilliseconds(500), ty);
						if (!string.IsNullOrEmpty(t)) return t;
					}
					catch { }
					return ty;
				}
			}
			catch { }
			return null;
		}

		[StructLayout(LayoutKind.Sequential)]
		private struct WinRect { public int Left, Top, Right, Bottom; }
		[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Auto)]
		private struct WinMonitorInfo { public int Size; public WinRect Monitor, Work; public int Flags; [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string Device; }
		// user32 geometry is thread-agnostic; Window.Left / ActualWidth / WindowState are not. Shared: modules reuse these.
		[DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hwnd, out WinRect rect);
		[DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hwnd);
		[DllImport("user32.dll")] private static extern IntPtr MonitorFromWindow(IntPtr hwnd, int flags);
		[DllImport("user32.dll", CharSet = CharSet.Auto)] private static extern bool GetMonitorInfo(IntPtr monitor, ref WinMonitorInfo info);

		/// <summary>hwnd, left, top, width, height (screen pixels), isMinimized, screen (device name). All null when
		/// the window has no handle. A minimized window is parked at (-32000,-32000): read isMinimized first.</summary>
		private static string[] Geometry(IntPtr hwnd)
		{
			string left = "null", top = "null", width = "null", height = "null", min = "null", screen = "null";
			try
			{
				WinRect r;
				if (hwnd != IntPtr.Zero && GetWindowRect(hwnd, out r))
				{
					left = I(r.Left); top = I(r.Top); width = I(r.Right - r.Left); height = I(r.Bottom - r.Top);
					min = IsIconic(hwnd) ? "true" : "false";
					var mi = new WinMonitorInfo();
					mi.Size = Marshal.SizeOf(typeof(WinMonitorInfo));
					IntPtr mon = MonitorFromWindow(hwnd, 2);		// MONITOR_DEFAULTTONEAREST
					if (mon != IntPtr.Zero && GetMonitorInfo(mon, ref mi)) screen = Q(mi.Device);
				}
			}
			catch { }
			return new[]
			{
				P("hwnd", hwnd == IntPtr.Zero ? "null" : I(hwnd.ToInt64())),
				P("left", left), P("top", top), P("width", width), P("height", height), P("isMinimized", min), P("screen", screen)
			};
		}

		private static string Windows()
		{
			HashSet<Window> owned;
			var all = KnownWindows(out owned);
			var items = new List<string>();
			foreach (var w in all)
			{
				string title, kind;
				IntPtr hwnd = IntPtr.Zero;
				try
				{
					var win = w;
					var got = Ui(win.Dispatcher, () => new object[] { TitleOf(win), new System.Windows.Interop.WindowInteropHelper(win).Handle });
					title = (string)got[0]; hwnd = (IntPtr)got[1];
					kind = Kind(w, title);
				}
				catch (Exception ex) { title = "<" + ex.Message + ">"; kind = "Other"; }		// documented shape: one dead window thread never fails the list
				var pairs = new List<string> { P("title", Q(title)), P("kind", Q(kind)), P("owned", owned.Contains(w) ? "true" : "false") };
				pairs.AddRange(Geometry(hwnd));
				items.Add(Obj(pairs.ToArray()));
			}
			return Arr(items);
		}

		private static string Kind(Window w, string title)
		{
			if (w is Gui.Chart.Chart)						return "Chart";
			if (w is Gui.ControlCenter)						return "ControlCenter";
			if (w is Gui.SuperDom.SuperDom)					return "SuperDom";
			if (w is Gui.NinjaScript.NinjaScriptOutput)		return "Output";
			// The NinjaScript editor has no NTWindow subclass of its own; go by the type name, then the caption.
			string t = w.GetType().FullName ?? "";
			if (t.IndexOf("Editor", StringComparison.OrdinalIgnoreCase) >= 0) return "NinjaScriptEditor";
			if (title != null && title.StartsWith("NinjaScript Editor", StringComparison.OrdinalIgnoreCase)) return "NinjaScriptEditor";
			return "Other";
		}

		// ── shared by modules (indicator inputs in Charts, strategy inputs in Backtest) ──
		/// <summary>True for a property the author marked [NinjaScriptProperty]. Matched on the attribute's type
		/// name through the metadata (GetCustomAttributesData), not on our own typeof: a vendor assembly can carry
		/// its own copy of the attribute type, and reading metadata never has to load an attribute type that a
		/// protected vendor DLL cannot resolve.</summary>
		private static bool IsInput(PropertyInfo p)
		{
			try
			{
				foreach (var d in p.GetCustomAttributesData())
					if (d.AttributeType != null && d.AttributeType.Name == "NinjaScriptPropertyAttribute") return true;
			}
			catch { }
			return false;
		}

		// ── exception text ─────────────────────────────────────────────────
		/// <summary>The innermost exception, named, with where it was thrown. Reflection wraps every failure in
		/// "Exception has been thrown by the target of an invocation.", which names neither cause nor place.
		/// (Ported from cli-nt-bridge, MIT.)</summary>
		private static string Deep(Exception ex)
		{
			if (ex == null) return "(null)";
			Exception cur = ex;
			for (int guard = 0; cur.InnerException != null && guard < 8; guard++) cur = cur.InnerException;
			string s = cur.GetType().Name + ": " + cur.Message;
			if (!ReferenceEquals(cur, ex)) s += "   [via " + ex.GetType().Name + "]";
			if (!string.IsNullOrEmpty(cur.StackTrace)) s += "   @ " + cur.StackTrace.Split('\n')[0].Trim();
			return s;
		}

		// ── date guard ─────────────────────────────────────────────────────
		/// <summary>null = fine; else why this range must never be armed. A fresh NinjaScript (and a template saved
		/// from one) carries From 2099-12-01 / To 1800-01-01, and arming that made NinjaTrader load until it stopped
		/// answering. Used by POST /backtest; templates and anything else that arms a range must call it too.</summary>
		private static string RangeProblem(DateTime from, DateTime to)
		{
			if (to < from) return "to is before from";
			if (from.Year < 1990 || from.Year > 2090) return "from " + from.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture) + " is outside 1990..2090 — that is a placeholder, not a range";
			if (to.Year < 1990 || to.Year > 2090) return "to " + to.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture) + " is outside 1990..2090 — that is a placeholder, not a range";
			return null;
		}

		// ── ring buffer ────────────────────────────────────────────────────────
		/// <summary>Fixed-capacity FIFO with SEQUENCE NUMBERS. Queue + Dequeue, never List + RemoveRange (that memmoves
		/// the whole buffer per line, under the lock, on the printing thread). The first item ever added has seq 1;
		/// Seen = seq of the newest = Dropped + Count. A reader's cursor is a seq, never a slot index: a slot index
		/// stops moving once the buffer is full. Its own lock, held for queue operations only — never call into
		/// NinjaTrader, a dispatcher or the file system while holding it (nothing in here does).</summary>
		private sealed class Ring<T>
		{
			private readonly object g = new object();
			private readonly Queue<T> q = new Queue<T>();
			private readonly int cap;
			private long seen;

			public Ring(int capacity) { cap = Math.Max(1, capacity); }

			/// <summary>Returns the item's seq.</summary>
			public long Add(T item) { lock (g) { q.Enqueue(item); if (q.Count > cap) q.Dequeue(); return ++seen; } }

			public long Seen	{ get { lock (g) return seen; } }
			public long Dropped	{ get { lock (g) return seen - q.Count; } }
			public int Count	{ get { lock (g) return q.Count; } }

			/// <summary>since &lt; 0: the newest `max` items. since >= 0: the items with seq > since, oldest first, at most
			/// `max` of them (so a cursor reader loses nothing: continue from firstSeq + Count - 1). max &lt;= 0 = no cap.
			/// A cursor older than the buffer gets everything that is left; one from the future gets nothing.
			/// firstSeq = seq of the first item returned (item i has seq firstSeq + i); seenNow/dropped as of this read.</summary>
			public List<T> Read(long since, int max, out long firstSeq, out long seenNow, out long dropped)
			{
				lock (g)
				{
					seenNow = seen; dropped = seen - q.Count;
					long oldest = dropped + 1;
					long from = since < 0 ? (max <= 0 ? oldest : Math.Max(oldest, seen - max + 1)) : Math.Max(oldest, since + 1);
					firstSeq = from;
					var list = new List<T>();
					if (from > seen) return list;
					int skip = (int)(from - oldest), take = max <= 0 ? int.MaxValue : max;
					foreach (var item in q)
					{
						if (skip > 0) { skip--; continue; }
						if (take-- <= 0) break;
						list.Add(item);
					}
					return list;
				}
			}

			public List<T> Tail(int n) { long a, b, c; return Read(-1, n, out a, out b, out c); }
		}

		// ── reflection table ───────────────────────────────────────────────────
		/// <summary>Everything reflective resolves ONCE, in a Start_* hook, through Compat.Resolve, and the endpoint
		/// that needs it reports resolved:false and emits null — never [] — when it is missing. GET /compat prints the
		/// table; /health.reflection is the key -> bool digest. Without it an NT8 upgrade leaves N endpoints each
		/// confidently returning empty.</summary>
		private static class Compat
		{
			private sealed class Entry { public string Key, Detail; public bool Resolved; public object Value; }
			private static readonly object g = new object();
			private static readonly List<Entry> entries = new List<Entry>();

			/// <summary>Runs the resolver NOW, records {key, resolved, detail}, returns what it found or null. Never throws.
			/// Resolving a key again replaces its row (a Start after a Stop).</summary>
			public static object Resolve(string key, Func<object> resolver)
			{
				object v = null;
				string detail;
				try { v = resolver(); detail = v == null ? "not found" : Describe(v); }
				catch (Exception ex) { v = null; detail = Deep(ex); }
				Set(key, v != null, detail, v);
				return v;
			}

			/// <summary>For a fact that is not a single lookup (a self-test, a multi-step probe).</summary>
			public static void Set(string key, bool resolved, string detail, object value)
			{
				lock (g)
				{
					var e = entries.FirstOrDefault(x => x.Key == key);
					if (e == null) { e = new Entry { Key = key }; entries.Add(e); }
					e.Resolved = resolved; e.Detail = detail; e.Value = value;
				}
			}

			public static T Get<T>(string key) where T : class
			{
				lock (g) { var e = entries.FirstOrDefault(x => x.Key == key); return e == null ? null : e.Value as T; }
			}

			public static bool IsResolved(string key) { lock (g) { var e = entries.FirstOrDefault(x => x.Key == key); return e != null && e.Resolved; } }

			private static string Describe(object v)
			{
				string s;
				try { var t = v as Type; s = t != null ? t.FullName : v.GetType().Name + " " + v; } catch { s = "?"; }
				return s.Length > 200 ? s.Substring(0, 200) : s;
			}

			public static string Json()
			{
				lock (g) return Arr(entries.Select(e => Obj(P("key", Q(e.Key)), P("resolved", e.Resolved ? "true" : "false"), P("detail", Q(e.Detail)))));
			}

			public static string Flags()
			{
				lock (g) return Obj(entries.Select(e => P(e.Key, e.Resolved ? "true" : "false")).ToArray());
			}
		}

		// ── OutputHub: the ONE subscription to Output.OutputEvent ──────────────
		/// <summary>NinjaScript Print() output, window open or not: the Output window is just another subscriber of the
		/// same static event. Subscribe() in AfterBind, Unsubscribe() in Stop() (which State.Terminated calls) — the
		/// event lives in NinjaTrader.Core, which a recompile does NOT reload, so a missed '-=' pins the dead assembly.
		/// Nobody else touches OutputEvent: Register() a listener instead.
		/// The handler runs on whatever thread called Print(), chart threads included: one allocation, one lock, one
		/// Enqueue, the listeners, all inside catch {}. NO Ui(), no file I/O, no NinjaTrader call — in the handler or
		/// in any listener. That is what keeps this answering while the UI thread is wedged; do not "refactor" it away.</summary>
		private static class OutputHub
		{
			/// <summary>Tab is 1 or 2. A cleared tab arrives as Reset = true with Text "&lt;&lt;cleared: tab N&gt;&gt;": an event, never silence.
			/// The seq is positional: Ring.Read's firstSeq + index.</summary>
			public sealed class Line { public DateTime Time; public int Tab; public string Text; public bool Reset; }

			public static readonly Ring<Line> Lines = new Ring<Line>(20000);
			private static readonly object g = new object();
			private static bool subscribed;
			private static volatile Action<Line>[] listeners = new Action<Line>[0];		// replaced whole, never mutated: the handler reads it without a lock

			public static bool Subscribed	{ get { lock (g) return subscribed; } }
			public static int ListenerCount	{ get { return listeners.Length; } }

			public static void Subscribe()		{ lock (g) { if (subscribed) return; Code.Output.OutputEvent += OnOutput; subscribed = true; } }
			public static void Unsubscribe()	{ lock (g) { Code.Output.OutputEvent -= OnOutput; subscribed = false; } }

			/// <summary>A listener gets every line after it registers, on the printing thread: be as cheap as the handler.
			/// Pair every Register with an Unregister (a module: in its Stop_* hook).</summary>
			public static void Register(Action<Line> listener)		{ lock (g) listeners = listeners.Concat(new[] { listener }).ToArray(); }
			public static void Unregister(Action<Line> listener)	{ lock (g) listeners = listeners.Where(x => x != listener).ToArray(); }

			private static void OnOutput(object sender, Code.OutputEventArgs e)
			{
				try
				{
					if (e == null) return;
					int tab = (int)e.OutputTab + 1;
					var line = new Line { Time = DateTime.Now, Tab = tab, Reset = e.IsReset, Text = e.IsReset ? "<<cleared: tab " + tab + ">>" : e.Message };
					Lines.Add(line);
					var ls = listeners;
					for (int i = 0; i < ls.Length; i++)
						try { ls[i](line); } catch { }
				}
				catch { }		// never let a print break the printer
			}
		}

		// ── JSON (hand rolled: NinjaTrader.Custom references no JSON library) ───
		private static string Obj(params string[] pairs) { return "{" + string.Join(",", pairs) + "}"; }
		private static string P(string key, string rawValue) { return Q(key) + ":" + rawValue; }
		private static string Arr(IEnumerable<string> items) { return "[" + string.Join(",", items.ToArray()) + "]"; }

		private static string Q(string s)
		{
			if (s == null) return "null";
			var sb = new StringBuilder(s.Length + 2);
			sb.Append('"');
			foreach (char c in s)
			{
				switch (c)
				{
					case '"':	sb.Append("\\\""); break;
					case '\\':	sb.Append("\\\\"); break;
					case '\b':	sb.Append("\\b"); break;
					case '\f':	sb.Append("\\f"); break;
					case '\n':	sb.Append("\\n"); break;
					case '\r':	sb.Append("\\r"); break;
					case '\t':	sb.Append("\\t"); break;
					default:
						if (c < ' ' || c == (char)0x2028 || c == (char)0x2029) sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
						else sb.Append(c);
						break;
				}
			}
			sb.Append('"');
			return sb.ToString();
		}

		private static string D(double v)
		{
			if (double.IsNaN(v) || double.IsInfinity(v)) return "null";
			return v.ToString("R", CultureInfo.InvariantCulture);
		}

		private static string Tm(DateTime t)
		{
			if (t == DateTime.MinValue) return "null";
			return Q(t.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture));
		}

		/// <summary>UTC with a trailing Z, for the fields named *Utc. Tm() is NT8-local and carries no zone.</summary>
		private static string TmUtc(DateTime t)
		{
			if (t == DateTime.MinValue) return "null";
			return Q(t.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture) + "Z");
		}

		private static string Scalar(object v)
		{
			if (v == null) return "null";
			if (v is bool) return (bool)v ? "true" : "false";
			if (v is Enum) return Q(v.ToString());
			if (v is DateTime) return Tm((DateTime)v);
			if (v is double || v is float || v is decimal) return D(Convert.ToDouble(v, CultureInfo.InvariantCulture));
			if (v is int || v is long || v is short || v is byte || v is uint || v is ulong || v is ushort || v is sbyte)
				return Convert.ToInt64(v, CultureInfo.InvariantCulture).ToString(CultureInfo.InvariantCulture);
			try { return Q(Convert.ToString(v, CultureInfo.InvariantCulture)); } catch { return "null"; }
		}

		private static string I(long v) { return v.ToString(CultureInfo.InvariantCulture); }

		// ── JSON parse (POST bodies; everything above is the other direction) ──
		// Minimal recursive descent. object -> Dictionary<string,object> (case-insensitive keys), array -> List<object>,
		// number -> double, string -> string, true/false -> bool, null -> null. Malformed input throws.
		private static object ParseJson(string s)
		{
			if (string.IsNullOrEmpty(s)) return null;
			int i = 0;
			object v = JVal(s, ref i);
			JWs(s, ref i);
			if (i < s.Length) throw new Exception("trailing JSON at " + i);
			return v;
		}

		private static void JWs(string s, ref int i) { while (i < s.Length && char.IsWhiteSpace(s[i])) i++; }

		private static object JVal(string s, ref int i)
		{
			JWs(s, ref i);
			if (i >= s.Length) throw new Exception("JSON ended early");
			switch (s[i])
			{
				case '{': return JObj(s, ref i);
				case '[': return JArr(s, ref i);
				case '"': return JStr(s, ref i);
				case 't': JLit(s, ref i, "true");  return true;
				case 'f': JLit(s, ref i, "false"); return false;
				case 'n': JLit(s, ref i, "null");  return null;
				default:  return JNum(s, ref i);
			}
		}

		private static void JLit(string s, ref int i, string lit)
		{
			if (i + lit.Length > s.Length || string.CompareOrdinal(s, i, lit, 0, lit.Length) != 0) throw new Exception("bad JSON literal at " + i);
			i += lit.Length;
		}

		private static Dictionary<string, object> JObj(string s, ref int i)
		{
			var map = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
			i++;
			JWs(s, ref i);
			if (i < s.Length && s[i] == '}') { i++; return map; }
			while (true)
			{
				JWs(s, ref i);
				if (i >= s.Length || s[i] != '"') throw new Exception("expected JSON key at " + i);
				string key = JStr(s, ref i);
				JWs(s, ref i);
				if (i >= s.Length || s[i] != ':') throw new Exception("expected ':' at " + i);
				i++;
				map[key] = JVal(s, ref i);
				JWs(s, ref i);
				if (i >= s.Length) throw new Exception("JSON ended early");
				if (s[i] == ',') { i++; continue; }
				if (s[i] == '}') { i++; return map; }
				throw new Exception("expected ',' or '}' at " + i);
			}
		}

		private static List<object> JArr(string s, ref int i)
		{
			var list = new List<object>();
			i++;
			JWs(s, ref i);
			if (i < s.Length && s[i] == ']') { i++; return list; }
			while (true)
			{
				list.Add(JVal(s, ref i));
				JWs(s, ref i);
				if (i >= s.Length) throw new Exception("JSON ended early");
				if (s[i] == ',') { i++; continue; }
				if (s[i] == ']') { i++; return list; }
				throw new Exception("expected ',' or ']' at " + i);
			}
		}

		private static string JStr(string s, ref int i)
		{
			var sb = new StringBuilder();
			i++;
			while (i < s.Length)
			{
				char c = s[i++];
				if (c == '"') return sb.ToString();
				if (c != '\\') { sb.Append(c); continue; }
				if (i >= s.Length) break;
				char e = s[i++];
				switch (e)
				{
					case '"':	sb.Append('"');  break;
					case '\\':	sb.Append('\\'); break;
					case '/':	sb.Append('/');  break;
					case 'b':	sb.Append('\b'); break;
					case 'f':	sb.Append('\f'); break;
					case 'n':	sb.Append('\n'); break;
					case 'r':	sb.Append('\r'); break;
					case 't':	sb.Append('\t'); break;
					case 'u':
						if (i + 4 > s.Length) throw new Exception("bad \\u at " + i);
						sb.Append((char)int.Parse(s.Substring(i, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture));
						i += 4;
						break;
					default: throw new Exception("bad escape at " + i);
				}
			}
			throw new Exception("unterminated JSON string");
		}

		private static object JNum(string s, ref int i)
		{
			int start = i;
			if (i < s.Length && (s[i] == '-' || s[i] == '+')) i++;
			while (i < s.Length && (char.IsDigit(s[i]) || s[i] == '.' || s[i] == 'e' || s[i] == 'E' || s[i] == '-' || s[i] == '+')) i++;
			double d;
			if (!double.TryParse(s.Substring(start, i - start), NumberStyles.Float, CultureInfo.InvariantCulture, out d)) throw new Exception("bad JSON number at " + start);
			return d;
		}

		private static object JGet(Dictionary<string, object> m, string key) { object v; return m != null && m.TryGetValue(key, out v) ? v : null; }

		/// <summary>Thrown by the typed JGet* accessors below when a key is present with the wrong JSON type.
		/// BacktestStart catches this and 400s instead of silently running with a defaulted value.</summary>
		private sealed class BadRequestException : Exception { public BadRequestException(string msg) : base(msg) { } }

		// Absent key (or JSON null) -> dflt. Present with the right type -> that value (an empty string counts as
		// present). Present with the wrong type -> throws, so a stringified bool/number is a 400, not a silent default.
		private static string JGetStr(Dictionary<string, object> m, string key, string dflt) { object v = JGet(m, key); if (v == null) return dflt; if (v is string) return (string)v; throw new BadRequestException(key + " must be a string"); }
		private static bool JGetBool(Dictionary<string, object> m, string key, bool dflt) { object v = JGet(m, key); if (v == null) return dflt; if (v is bool) return (bool)v; throw new BadRequestException(key + " must be a boolean"); }
		private static int JGetInt(Dictionary<string, object> m, string key, int dflt) { object v = JGet(m, key); if (v == null) return dflt; if (v is double) return (int)(double)v; throw new BadRequestException(key + " must be a number"); }
		private static Dictionary<string, object> JGetMap(Dictionary<string, object> m, string key) { return JGet(m, key) as Dictionary<string, object>; }

		/// <summary>Parsed JSON back to a JSON string, so the status document can echo `inputs` verbatim.</summary>
		private static string Ser(object v)
		{
			var map = v as Dictionary<string, object>;
			if (map != null) return Obj(map.Select(kv => P(kv.Key, Ser(kv.Value))).ToArray());
			var list = v as List<object>;
			if (list != null) return Arr(list.Select(Ser));
			return Scalar(v);
		}

		/// <summary>Every JSON number arrives as double; a [NinjaScriptProperty] is usually int, long, bool or an
		/// enum. Throws with the property name so BacktestStart can 400 with something readable.</summary>
		private static object Coerce(object v, Type t, string name)
		{
			// Convert.ChangeType(5.5, typeof(int)) is 6 — a silently different run. Refuse instead.
			var ut = Nullable.GetUnderlyingType(t) ?? t;
			if (v is double && (ut.IsEnum || (ut.IsPrimitive && ut != typeof(bool) && ut != typeof(char) && ut != typeof(double) && ut != typeof(float))))
			{
				double d = (double)v;
				if (double.IsNaN(d) || double.IsInfinity(d) || d != Math.Floor(d))
					throw new Exception("input '" + name + "' is " + ut.Name + " and cannot take " + d.ToString("R", CultureInfo.InvariantCulture) + " — send a whole number");
			}
			try
			{
				if (t.IsEnum) return v is string ? Enum.Parse(t, (string)v, true) : Enum.ToObject(t, Convert.ToInt32(v, CultureInfo.InvariantCulture));
				var nn = Nullable.GetUnderlyingType(t);
				if (nn != null) return v == null ? null : Coerce(v, nn, name);
				return Convert.ChangeType(v, t, CultureInfo.InvariantCulture);
			}
			catch { throw new Exception("input '" + name + "' is not a " + t.Name); }
		}

		private static string Err(ref int status, int code, string msg) { status = code; return Obj(P("error", Q(msg))); }

		/// <summary>One assert of the parser against the serializer, at load. A broken parser says so in the log
		/// instead of failing the first POST.</summary>
		private static void JsonSelfTest()
		{
			try
			{
				const string src = "{\"a\":[1,-2.5e3,true,null,\"q\\\"\\u0041\\n\"],\"b\":{\"c\":0.25}}";
				var m = ParseJson(src) as Dictionary<string, object>;
				var a = JGet(m, "a") as List<object>;
				bool ok = m != null && a != null && a.Count == 5
					&& (double)a[0] == 1 && (double)a[1] == -2500 && (bool)a[2] && a[3] == null && (string)a[4] == "q\"A\n"
					&& JGetMap(m, "B") != null
					&& (double)JGet(JGetMap(m, "b"), "c") == 0.25
					&& ParseJson(Ser(m)) is Dictionary<string, object>;
				Log("json self-test " + (ok ? "ok" : "FAILED"));
				Compat.Set("selftest.json", ok, ok ? "ok" : "FAILED", null);
			}
			catch (Exception ex) { Log("json self-test FAILED: " + ex.Message); Compat.Set("selftest.json", false, Deep(ex), null); }
		}

		/// <summary>The runnable check for Ring (the sequence-number cursor is the bug this catches), RangeProblem and Deep.</summary>
		private static void CoreSelfTest()
		{
			string bad = null;
			try
			{
				var r = new Ring<string>(2);
				r.Add("a"); r.Add("b"); r.Add("c");
				long first, seen, dropped;
				var all = r.Read(-1, 0, out first, out seen, out dropped);
				if (!(all.Count == 2 && all[0] == "b" && first == 2 && seen == 3 && dropped == 1 && r.Count == 2)) bad = "ring: cap/seq";
				else if (r.Read(seen, 0, out first, out seen, out dropped).Count != 0) bad = "ring: a cursor at `seen` must read nothing";
				else if (r.Read(0, 0, out first, out seen, out dropped).Count != 2 || first != 2) bad = "ring: a cursor older than the buffer must read what is left";
				else if (r.Read(99, 0, out first, out seen, out dropped).Count != 0) bad = "ring: a cursor from the future must read nothing";
				else if (r.Read(1, 1, out first, out seen, out dropped)[0] != "b" || r.Tail(1)[0] != "c") bad = "ring: max";
				else if (RangeProblem(new DateTime(2026, 9, 10), new DateTime(2026, 9, 17)) != null) bad = "range: a good range was refused";
				else if (RangeProblem(new DateTime(2026, 9, 17), new DateTime(2026, 9, 10)) == null) bad = "range: inverted range passed";
				else if (RangeProblem(new DateTime(2099, 12, 1), new DateTime(2099, 12, 2)) == null || RangeProblem(new DateTime(1800, 1, 1), new DateTime(2026, 1, 1)) == null) bad = "range: placeholder year passed";
				else if (!(Coerce(5.0, typeof(int), "x") is int) || (int)Coerce(5.0, typeof(int), "x") != 5) bad = "coerce: 5.0 -> int";
				else if (!Throws(() => Coerce(5.5, typeof(int), "x")) || !Throws(() => Coerce(double.NaN, typeof(long), "x")) || !Throws(() => Coerce(1e30, typeof(int), "x"))
					|| !Throws(() => Coerce(0.5, typeof(DayOfWeek), "x")) || !Throws(() => Coerce(5.5, typeof(int?), "x"))) bad = "coerce: a fractional / NaN / out-of-range number reached an integral input";
				else if ((double)Coerce(5.5, typeof(double), "x") != 5.5) bad = "coerce: double";
				else if (Deep(new TargetInvocationException(new InvalidOperationException("inner"))).IndexOf("InvalidOperationException: inner", StringComparison.Ordinal) != 0) bad = "deep: did not unwrap";
			}
			catch (Exception ex) { bad = Deep(ex); }
			Log("core self-test " + (bad == null ? "ok" : "FAILED: " + bad));
			Compat.Set("selftest.core", bad == null, bad ?? "ok", null);
		}

		private static bool Throws(Action a) { try { a(); return false; } catch { return true; } }

		private const string HubEchoText = "NT8Bridge v";
		private static volatile bool hubEcho;
		private static void HubEcho(OutputHub.Line line) { if (line.Tab == 2 && line.Text != null && line.Text.StartsWith(HubEchoText, StringComparison.Ordinal)) hubEcho = true; }

		/// <summary>Regression check, run once per Start on a pool thread against a PRIVATE dispatcher thread (never an NT8
		/// one): while that thread is busy, a Ui() with a short timeout must THROW TimeoutException — not return
		/// default(T) — and the aborted call must never run late.</summary>
		private static void UiSelfTest()
		{
			string bad = null;
			Dispatcher d = null;
			try
			{
				var ready = new ManualResetEventSlim(false);
				var t = new Thread(() => { d = Dispatcher.CurrentDispatcher; ready.Set(); Dispatcher.Run(); }) { IsBackground = true, Name = "NT8Bridge-selftest" };
				t.SetApartmentState(ApartmentState.STA);
				t.Start();
				if (!ready.Wait(5000)) throw new Exception("test dispatcher did not start");
				var busy = new ManualResetEventSlim(false);
				d.BeginInvoke(new Action(() => { busy.Set(); Thread.Sleep(700); }));
				busy.Wait(5000);
				bool ranLate = false;
				try { int v = Ui(d, () => { ranLate = true; return 42; }, TimeSpan.FromMilliseconds(200), "selftest"); bad = "returned " + v + " instead of throwing"; }
				catch (TimeoutException) { }
				if (bad == null && Ui(d, () => 42, TimeSpan.FromSeconds(5), "selftest") != 42) bad = "happy path";		// also drains the queue behind the sleeper
				if (bad == null && ranLate) bad = "the timed-out call ran anyway";
			}
			catch (Exception ex) { bad = Deep(ex); }
			finally { try { if (d != null) d.BeginInvokeShutdown(DispatcherPriority.Normal); } catch { } }
			Log("ui self-test " + (bad == null ? "ok" : "FAILED: " + bad));
			Compat.Set("selftest.uiTimeout", bad == null, bad ?? "ok: a busy dispatcher gave TimeoutException, not default(T)", null);
			OutputHub.Unregister(HubEcho);				// AfterBind printed the start line at least 700 ms ago
			string hubBad = OutputHub.Lines.Seen <= 0 ? "the start line never reached the ring" : !hubEcho ? "the start line never reached the listener" : null;
			Log("output hub self-test " + (hubBad == null ? "ok" : "FAILED: " + hubBad));
			Compat.Set("selftest.outputHub", hubBad == null, hubBad ?? "ok: the start line came back through OutputEvent, ring and listener", null);
		}

		// ── misc ───────────────────────────────────────────────────────────────
		private static int Num(string s, int fallback)
		{
			int v;
			return !string.IsNullOrEmpty(s) && int.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out v) && v > 0 ? v : fallback;
		}

		private static List<string> Tail(IEnumerable<string> src, int n)
		{
			var all = src.ToList();
			return all.Skip(Math.Max(0, all.Count - n)).ToList();
		}
	}
}
