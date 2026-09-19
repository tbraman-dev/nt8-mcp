// NT8Bridge.Workspace.cs — the desktop module: GET /workspace, GET /strategies/running, POST /screenshot.
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md "Module seams").
//
// Portions of this file are derived from cli-nt-bridge
// (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
// MIT License. The full notice is in NOTICE at the repository root.
// (Derived: the GDI PrintWindow capture and its finally block, the EnumWindows-by-pid title match,
//  the Control Center StrategiesGrid reach and its virtualization handling.)
//
// READ ONLY except for one PNG written to a path the caller chose. Nothing here enables, disables,
// starts or stops a strategy, and nothing here restores, fronts, moves or resizes a window.

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		/// <summary>Seam (NOTES.md "Module seams"): GET /workspace, GET /strategies/running, POST /screenshot.
		/// Null for everything else — GET /strategies (the type listing) stays with Route_Backtest, and
		/// POST /chart/{id}/screenshot stays with Route_Charts.</summary>
		private static string Route_Workspace(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (method == "GET" && seg.Length == 1 && seg[0] == "workspace") return Ws_Workspace();
			if (method == "GET" && seg.Length == 2 && seg[0] == "strategies" && seg[1] == "running")
				return Ws_Running(Ws_Flag(q["materialize"]));
			if (method == "POST" && seg.Length == 1 && seg[0] == "screenshot") return Ws_Shot(body, ref status);
			return null;
		}

		private static bool Ws_Flag(string v)
		{
			return !string.IsNullOrEmpty(v) && (v == "1" || string.Equals(v, "true", StringComparison.OrdinalIgnoreCase));
		}

		// The one reflective member this module needs, resolved once. StrategiesGrid, StrategiesGridEntry and
		// StrategiesGridEntryChild are all public types in NinjaTrader.Gui.dll, so every row field is typed and
		// cannot silently degrade; only `source` (the grid's private backing collection) needs reflection.
		private static FieldInfo Ws_gridSource;

		/// <summary>Seam: runs before the first request is served.</summary>
		private static void Start_Workspace()
		{
			Ws_gridSource = Compat.Resolve("StrategiesGrid.source",
				() => typeof(Gui.NinjaScript.StrategiesGrid).GetField("source", BindingFlags.Instance | BindingFlags.NonPublic)) as FieldInfo;
		}

		// No Stop_Workspace: this module subscribes to nothing and owns no thread.

		// ── GET /workspace ─────────────────────────────────────────────────────
		/// <summary>The loaded workspace plus every window the AddOn can see (owned dialogs included), with
		/// type-specific details for charts. ONE Ui() hop per window, never one global Invoke: a wedged chart
		/// costs that row a `details:null` + `note`, not the whole answer.
		/// Never poll this — walking every chart on its own dispatcher is exactly the load that froze a chart
		/// thread before (MEMORY.md nt8-chart-thread-freeze).</summary>
		private static string Ws_Workspace()
		{
			string name = null;
			// Globals.ActiveWorkspace is a public static string and is typed here. Their CurrentWorkspaceName()
			// probed CurrentWorkspace / Workspace / WorkspaceName — none of the three exists on 8.1.8.2, so it
			// always answered null and their reflection-degrades-to-null discipline hid the miss.
			try { name = Core.Globals.ActiveWorkspace; } catch (Exception ex) { Log("workspace name: " + Deep(ex)); }
			if (string.IsNullOrEmpty(name)) name = null;

			HashSet<Window> owned;
			var all = KnownWindows(out owned);
			var rows = new List<string>();
			foreach (var w in all)
			{
				string title = null, kind, note = null, details = "null";
				IntPtr hwnd = IntPtr.Zero;
				try
				{
					var win = w;
					var got = Ui(win.Dispatcher, () => new object[] { TitleOf(win), new System.Windows.Interop.WindowInteropHelper(win).Handle });
					title = (string)got[0];
					hwnd = (IntPtr)got[1];
				}
				catch (Exception ex) { note = "window did not answer: " + ex.Message; }
				kind = Kind(w, title);

				var chart = w as Gui.Chart.Chart;
				if (chart != null && note == null)
				{
					// An unreadable chart and an empty chart are different claims: details stays null and the
					// reason goes in `note`. A wedged dispatcher raises a real exception; catch it per row.
					try { details = Ws_ChartDetails(chart); }
					catch (Exception ex) { note = "chart did not answer: " + Deep(ex); }
				}
				else if (chart == null && note == null)
					note = "recognised, not decoded";

				var pairs = new List<string>
				{
					P("id", Q(IdOf(w))),
					P("kind", Q(kind)),
					P("type", Q(w.GetType().Name)),		// MarketAnalyzer / StrategyAnalyzer land in kind "Other"; the type name is the evidence
					P("title", Q(title)),
					P("owned", owned.Contains(w) ? "true" : "false")
				};
				pairs.AddRange(Geometry(hwnd));			// core helper; same fields as /windows
				pairs.Add(P("details", details));
				pairs.Add(P("note", Q(note)));
				rows.Add(Obj(pairs.ToArray()));
			}
			return Obj(P("name", Q(name)), P("windows", Arr(rows)));
		}

		private static string Ws_ChartDetails(Gui.Chart.Chart chart)
		{
			return OnChart(chart, cc =>
			{
				var bars = Primary(cc);
				return Obj(
					P("instrument", Q(InstrumentName(bars))),
					P("period", Q(Period(bars))),
					P("barsCount", I(bars.Bars != null ? bars.Bars.Count : 0)),
					P("indicators", Ws_Scripts(cc.Indicators)),
					P("strategies", Ws_Scripts(cc.Strategies)));
			});
		}

		/// <summary>[{name, state}] for a chart's indicator or strategy collection; null when the collection
		/// itself is missing. `state` is NT's own State enum verbatim — a silently disabled strategy is the
		/// whole reason this endpoint exists, and collapsing State into a boolean is a claim we would have to
		/// defend. On the chart's dispatcher (called inside OnChart).</summary>
		private static string Ws_Scripts(System.Collections.IEnumerable list)
		{
			if (list == null) return "null";
			var items = new List<string>();
			foreach (var o in list)
			{
				if (o == null) continue;
				string state = null;
				try { var ns = o as NinjaScriptBase; if (ns != null) state = ns.State.ToString(); } catch { }
				items.Add(Obj(P("name", Q(NameOf(o))), P("state", Q(state))));
			}
			return Arr(items);
		}

		// ── GET /strategies/running ────────────────────────────────────────────
		// READ ONLY, and deliberately so. Nothing in this module writes to the grid: setting
		// StrategiesGridEntry.IsEnabled reads back True and starts nothing, the grid's routed commands start
		// nothing, and the only thing that does start a strategy is the checkbox's own Checked event — i.e. a
		// synthetic click on a live trading platform, so nothing here reaches for it.
		// `enabled` is grid state, NOT proof a strategy is running. Believe `state`.
		private static readonly TimeSpan Ws_GridTimeout = TimeSpan.FromSeconds(10);

		private static string Ws_Running(bool materialize)
		{
			var notes = new List<string>();
			Window cc = null;
			// NT8's real windows are not in Application.Current.Windows. ControlCenter.Instance is
			// protected internal static (ControlCenter.cs:1449) — but our own registry already holds the
			// window (Reseed + OnWindowCreated), so no reflection is needed to find it.
			lock (gate)
				foreach (var w in windows)
					if (w is Gui.ControlCenter) { cc = w; break; }

			string rows = null;
			if (cc == null) notes.Add("no Control Center window in the registry");
			else if (Ws_gridSource == null) notes.Add("StrategiesGrid.source did not resolve — see GET /compat");
			else
			{
				var ccWin = cc;
				// The Control Center owns a UI thread of its own, different from Globals.MainThreadDispatcher:
				// a wrong-thread WPF read throws and reflection re-wraps it as a convincing null. Bounded, because
				// the materialize path calls UpdateLayout. A timeout flies as the core's 504 — one target, one
				// honest status code.
				rows = Ui(ccWin.Dispatcher, () => Ws_ReadGrid(ccWin, materialize, notes), Ws_GridTimeout, "ControlCenter");
			}
			// gridResolved:false means "we could not read the grid", never "there are no strategies": null, not [].
			return Obj(
				P("gridResolved", rows != null ? "true" : "false"),
				P("strategies", rows ?? "null"),
				P("notes", Arr(notes.Select(Q))));
		}

		/// <summary>ON THE CONTROL CENTER'S OWN DISPATCHER. Returns the JSON array of rows, or null when the grid
		/// could not be reached. Reads cached strings and bools off the grid entries only — no Cbi collection is
		/// locked from this thread, which is how you deadlock a platform whose Cbi callbacks marshal back to it
		/// (that is also why `accountPosition` is the grid's own string, not the live Position object).</summary>
		private static string Ws_ReadGrid(Window cc, bool materialize, List<string> notes)
		{
			var grid = Ws_Find<Gui.NinjaScript.StrategiesGrid>(cc);
			if (grid == null)
			{
				// Live 8.1.8.2: the grid leaves the VISUAL tree the moment another tab is selected, even right after
				// a visit (a WPF TabControl hosts only the selected tab's content). A tab's content object can outlive
				// that, so look in the LOGICAL tree before anybody touches the user's tabs.
				grid = Ws_FindLogical<Gui.NinjaScript.StrategiesGrid>(cc, 0);
				if (grid != null) notes.Add("read through the logical tree: the Strategies tab is not the selected tab");
			}
			if (grid == null && materialize) grid = Ws_Materialize(cc, notes);
			if (grid == null)
			{
				// The Strategies tab is virtualized while inactive: the grid is simply not in the visual tree.
				// Cycling the user's tabs is a visible mutation of a live Control Center, so it is opt-in.
				notes.Add(materialize
					? "StrategiesGrid not found after activating every Control Center tab"
					: "Strategies tab not realized; retry with ?materialize=1");
				return null;
			}

			var src = Ws_gridSource.GetValue(grid) as System.Collections.IEnumerable;
			if (src == null) { notes.Add("StrategiesGrid.source is null"); return null; }

			var items = new List<string>();
			foreach (var e in src)
			{
				var entry = e as Gui.NinjaScript.StrategiesGridEntry;
				if (entry == null) continue;
				items.Add(Ws_Row(entry, null));
				// The per-instrument child rows their SnapshotGrid dropped entirely.
				var kids = entry.Children;
				if (kids == null) continue;
				string parent = Ws_RowName(entry);
				foreach (var k in kids)
					if (k != null && !ReferenceEquals(k, entry)) items.Add(Ws_Row(k, parent));
			}
			return Arr(items);
		}

		/// <summary>Cycle the Control Center's tabs until the grid is in the tree, then put the user's tab back
		/// in a finally. On the Control Center's dispatcher only.</summary>
		private static Gui.NinjaScript.StrategiesGrid Ws_Materialize(Window cc, List<string> notes)
		{
			var nt = cc as Gui.Tools.NTWindow;
			System.Windows.Controls.TabControl mtc = null;
			try { if (nt != null) mtc = nt.MainTabControl; } catch (Exception ex) { notes.Add("ControlCenter.MainTabControl: " + ex.Message); }
			if (mtc == null) { notes.Add("ControlCenter.MainTabControl did not resolve"); return null; }

			Gui.NinjaScript.StrategiesGrid grid = null;
			int saved = -1;
			try { saved = mtc.SelectedIndex; } catch { }
			try
			{
				int count = 0;
				try { count = mtc.Items.Count; } catch { }
				for (int i = 0; i < count && grid == null; i++)
				{
					try { mtc.SelectedIndex = i; mtc.UpdateLayout(); } catch { continue; }
					grid = Ws_Find<Gui.NinjaScript.StrategiesGrid>(cc);
				}
			}
			finally
			{
				try { if (saved >= 0) { mtc.SelectedIndex = saved; mtc.UpdateLayout(); } }
				catch (Exception ex) { notes.Add("restoring the Control Center tab: " + ex.Message); }
			}
			if (grid != null) notes.Add("the Strategies tab was materialized and the original tab restored");
			return grid;
		}

		/// <summary>First descendant of that type in the visual tree. Its own thread only.</summary>
		private static T Ws_Find<T>(DependencyObject root) where T : DependencyObject
		{
			if (root == null) return null;
			try
			{
				int n = VisualTreeHelper.GetChildrenCount(root);
				for (int i = 0; i < n; i++)
				{
					var child = VisualTreeHelper.GetChild(root, i);
					if (child == null) continue;
					var hit = child as T;
					if (hit != null) return hit;
					var deeper = Ws_Find<T>(child);
					if (deeper != null) return deeper;
				}
			}
			catch { }
			return null;
		}

		/// <summary>First descendant of that type in the LOGICAL tree (unselected tab content lives only there).
		/// Its own thread only. Depth-capped: a logical tree is a tree, the cap is for a vendor's odd one.</summary>
		private static T Ws_FindLogical<T>(object node, int depth) where T : class
		{
			var hit = node as T;
			if (hit != null) return hit;
			var d = node as DependencyObject;
			if (d == null || depth > 40) return null;
			try
			{
				foreach (var child in LogicalTreeHelper.GetChildren(d))
				{
					var deeper = Ws_FindLogical<T>(child, depth + 1);
					if (deeper != null) return deeper;
				}
			}
			catch { }
			return null;
		}

		/// <summary>The name a row is ADDRESSED by: the grid's own Name, else our NameOf(Strategy). A vendor
		/// script that hides its on-chart label does it by blanking Name, so the fallback has to be the SAME
		/// one every other endpoint uses or rows list fine and then fail to match.</summary>
		private static string Ws_RowName(Gui.NinjaScript.StrategiesGridEntryChild r)
		{
			try { if (!string.IsNullOrEmpty(r.Name)) return r.Name; } catch { }
			try { return NameOf(r.Strategy); } catch { return ""; }
		}

		private static string Ws_Row(Gui.NinjaScript.StrategiesGridEntryChild r, string parent)
		{
			try
			{
				var strat = r.Strategy;
				string state = null, type = null;
				if (strat != null)
				{
					try { state = strat.State.ToString(); } catch { }
					try { type = strat.GetType().Name; } catch { }
				}
				return Obj(
					P("name", Q(Ws_RowName(r))),
					P("parent", Q(parent)),					// null on a master row; the master's name on a per-instrument child
					P("type", Q(type)),
					P("enabled", r.IsEnabled ? "true" : "false"),	// grid state, NOT "is running" — believe `state`
					P("state", Q(state)),
					P("account", Q(r.AccountName)),
					P("instrument", Q(r.InstrumentName)),
					P("connected", r.IsConnected ? "true" : "false"),
					P("connection", Q(r.Connection)),
					P("dataSeries", Q(r.DataSeries)),
					P("position", Q(r.Position2String)),
					P("accountPosition", Q(r.AccountPosition2String)),
					P("averagePrice", D(r.AveragePrice)),
					P("realized", Q(r.Realized)),			// the grid's own formatted strings, not recomputed numbers
					P("unrealized", Q(r.Unrealized)),
					P("trades", I(r.NumberOfTrades)),
					P("parameters", Q(r.Parameters)),
					P("workspace", Q(r.Workspace)));
			}
			catch (Exception ex) { return Obj(P("error", Q(Deep(ex)))); }
		}

		// ── POST /screenshot ───────────────────────────────────────────────────
		// GDI, not WPF rendering: RenderTargetBitmap needs each window's own dispatcher, misses child HWNDs and
		// misses DWM composition. PrintWindow with PW_RENDERFULLCONTENT captures what is genuinely on screen and
		// Win32 is thread-agnostic. It also TOUCHES NO WINDOW STATE — unlike scripts/shot.ps1, which did
		// SW_RESTORE + front, i.e. rearranged a live trading desktop in order to photograph it. A minimized
		// window is therefore an error here, never a restore.
		//
		// SetProcessDPIAware() (cli-nt-bridge calls it) is deliberately NOT called: it mutates the whole NinjaTrader
		// process's DPI awareness, it is a no-op for a WPF app that already declared awareness in its manifest,
		// and "maybe nothing happens" is not a risk worth taking inside a live trading platform.
		private const uint Ws_PW_RENDERFULLCONTENT = 0x00000002;
		private const int Ws_SRCCOPY = 0x00CC0020;
		private const int Ws_BlankBytes = 8192;		// a uniformly black PNG compresses to almost nothing: a HINT, never a verdict

		private delegate bool Ws_EnumWindowsProc(IntPtr hwnd, IntPtr lParam);

		// Module-prefixed with an explicit EntryPoint: the core already declares GetWindowRect / IsIconic /
		// MonitorFromWindow / GetMonitorInfo and a second declaration of any of them is CS0111.
		[DllImport("user32.dll", EntryPoint = "EnumWindows")] private static extern bool Ws_EnumWindows(Ws_EnumWindowsProc cb, IntPtr lParam);
		[DllImport("user32.dll", EntryPoint = "GetWindowTextW", CharSet = CharSet.Unicode)] private static extern int Ws_GetWindowText(IntPtr hwnd, StringBuilder sb, int max);
		[DllImport("user32.dll", EntryPoint = "GetWindowThreadProcessId")] private static extern uint Ws_GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
		[DllImport("user32.dll", EntryPoint = "IsWindow")] private static extern bool Ws_IsWindow(IntPtr hwnd);
		[DllImport("user32.dll", EntryPoint = "IsWindowVisible")] private static extern bool Ws_IsWindowVisible(IntPtr hwnd);
		[DllImport("user32.dll", EntryPoint = "GetDC")] private static extern IntPtr Ws_GetDC(IntPtr hwnd);
		[DllImport("user32.dll", EntryPoint = "GetWindowDC")] private static extern IntPtr Ws_GetWindowDC(IntPtr hwnd);
		[DllImport("user32.dll", EntryPoint = "ReleaseDC")] private static extern int Ws_ReleaseDC(IntPtr hwnd, IntPtr dc);
		[DllImport("user32.dll", EntryPoint = "PrintWindow")] private static extern bool Ws_PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);
		// SendMessageTimeoutW's return is the raw LRESULT (pointer-sized): declared IntPtr, not bool, so a 64-bit
		// truthy value never gets truncated by the marshaller. Nonzero = the target pumped it before the timeout.
		[DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW")]
		private static extern IntPtr Ws_SendMessageTimeout(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam, uint flags, uint timeout, out IntPtr result);
		[DllImport("user32.dll", EntryPoint = "GetSystemMetrics")] private static extern int Ws_GetSystemMetrics(int index);
		[DllImport("user32.dll", EntryPoint = "GetWindow")] private static extern IntPtr Ws_GetWindow(IntPtr hwnd, uint cmd);
		[DllImport("user32.dll", EntryPoint = "GetClassNameW", CharSet = CharSet.Unicode)] private static extern int Ws_GetClassName(IntPtr hwnd, StringBuilder sb, int max);
		[DllImport("gdi32.dll", EntryPoint = "CreateCompatibleDC")] private static extern IntPtr Ws_CreateCompatibleDC(IntPtr dc);
		[DllImport("gdi32.dll", EntryPoint = "CreateCompatibleBitmap")] private static extern IntPtr Ws_CreateCompatibleBitmap(IntPtr dc, int w, int h);
		[DllImport("gdi32.dll", EntryPoint = "SelectObject")] private static extern IntPtr Ws_SelectObject(IntPtr dc, IntPtr obj);
		[DllImport("gdi32.dll", EntryPoint = "DeleteObject")] private static extern bool Ws_DeleteObject(IntPtr obj);
		[DllImport("gdi32.dll", EntryPoint = "DeleteDC")] private static extern bool Ws_DeleteDC(IntPtr dc);
		[DllImport("gdi32.dll", EntryPoint = "BitBlt")] private static extern bool Ws_BitBlt(IntPtr dst, int x, int y, int w, int h, IntPtr src, int sx, int sy, int rop);

		/// <summary>POST /screenshot {"window"|"chart"|"hwnd", "path"}. Target precedence: hwnd, then chart,
		/// then window (title substring), then — nothing given — the whole virtual screen.</summary>
		private static string Ws_Shot(string body, ref int status)
		{
			Dictionary<string, object> m;
			try { m = ParseJson(body) as Dictionary<string, object>; }
			catch (Exception ex) { return Err(ref status, 400, "bad JSON body: " + ex.Message); }

			string title, chartId, path;
			long hwndNum;
			try
			{
				title	= JGetStr(m, "window", null) ?? JGetStr(m, "title", null);	// `title` is the cli-nt-bridge spelling
				chartId	= JGetStr(m, "chart", null);
				path	= JGetStr(m, "path", null) ?? JGetStr(m, "out", null);		// `out` likewise
				hwndNum	= Ws_Hwnd(m);
			}
			catch (BadRequestException ex) { return Err(ref status, 400, ex.Message); }

			IntPtr target = IntPtr.Zero;
			string matched = null;
			if (hwndNum != 0)
			{
				target = new IntPtr(hwndNum);
				if (!Ws_IsWindow(target)) return Err(ref status, 404, "hwnd " + hwndNum + " is not a window");
				matched = Ws_TitleOf(target);
			}
			else if (!string.IsNullOrEmpty(chartId))
			{
				Gui.Chart.Chart chart;
				try { chart = FindChart(chartId); }
				catch (BadRequestException ex) { return Err(ref status, 404, ex.Message); }
				var win = chart;
				// WindowInteropHelper.Handle throws off-thread: one hop to the window's own dispatcher for it.
				var got = Ui(win.Dispatcher, () => new object[] { TitleOf(win), new System.Windows.Interop.WindowInteropHelper(win).Handle });
				matched = (string)got[0];
				target = (IntPtr)got[1];
				if (target == IntPtr.Zero) return Err(ref status, 409, "chart '" + chartId + "' has no window handle yet");
			}
			else if (!string.IsNullOrEmpty(title))
			{
				var found = Ws_FindByTitle(title);
				if (found.Count == 0) return Err(ref status, 404, "no visible window of this process matching '" + title + "'");
				target = found[0].Key;
				matched = found[0].Value;
			}

			int x = 0, y = 0, w, h;
			bool fullScreen = target == IntPtr.Zero;
			if (fullScreen)
			{
				// SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN / SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN: every monitor,
				// including negative-origin ones.
				x = Ws_GetSystemMetrics(76); y = Ws_GetSystemMetrics(77);
				w = Ws_GetSystemMetrics(78); h = Ws_GetSystemMetrics(79);
				matched = "(virtual screen)";
				if (w <= 0 || h <= 0) return Err(ref status, 500, "the virtual screen reports no area (" + w + "x" + h + ")");
			}
			else
			{
				WinRect r;
				if (!GetWindowRect(target, out r)) return Err(ref status, 500, "GetWindowRect failed");
				x = r.Left; y = r.Top; w = r.Right - r.Left; h = r.Bottom - r.Top;
				// A minimized window has a real HWND and nonsense geometry (parked at -32000,-32000). Say so
				// rather than return a sliver that looks like a failed render — and never restore it.
				if (IsIconic(target))
					return Err(ref status, 409, "window '" + matched + "' is minimized — this endpoint never restores or fronts a window, so restore it yourself first");
				if (w <= 0 || h <= 0)
					return Err(ref status, 409, "window '" + matched + "' has no area (" + w + "x" + h + ")");
			}

			byte[] png;
			bool printed, composited;
			try { png = Ws_Capture(target, fullScreen, x, y, w, h, out printed, out composited); }
			catch (Exception ex) { return Err(ref status, 500, Deep(ex)); }

			if (string.IsNullOrEmpty(path))
				path = Path.Combine(Path.Combine(Path.GetTempPath(), "nt8bridge"),
					"shot-" + DateTime.Now.ToString("yyyyMMdd-HHmmss-fff", CultureInfo.InvariantCulture) + ".png");
			try
			{
				string dir = Path.GetDirectoryName(path);
				if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir)) Directory.CreateDirectory(dir);
				File.WriteAllBytes(path, png);
			}
			catch (Exception ex) { return Err(ref status, 400, "could not write '" + path + "': " + ex.Message); }

			return Obj(
				P("ok", "true"),
				P("path", Q(path)),
				P("window", Q(matched)),
				P("hwnd", target == IntPtr.Zero ? "null" : I(target.ToInt64())),
				P("width", I(w)), P("height", I(h)),
				P("bytes", I(png.Length)),
				P("method", Q(printed ? "PrintWindow" : "BitBlt")),
				// true = the target is a chart and its canvas was printed from the chart's own render form and merged in
				P("composited", composited ? "true" : "false"),
				// A capture from a session with no desktop is a valid PNG of pure black, which looks like an
				// answer. This is a HINT that the image is worth doubting, never a verdict: go and look at it.
				P("looksBlank", png.Length < Ws_BlankBytes ? "true" : "false"));
		}

		/// <summary>`hwnd` as a JSON number or a decimal string (their CLI sends strings). 0 = not given.</summary>
		private static long Ws_Hwnd(Dictionary<string, object> m)
		{
			object v = JGet(m, "hwnd");
			if (v == null) return 0;
			if (v is double) return (long)(double)v;
			var s = v as string;
			if (s != null && s.Length == 0) return 0;
			long parsed;
			if (s != null && long.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed)) return parsed;
			throw new BadRequestException("hwnd must be a number");
		}

		private static string Ws_TitleOf(IntPtr hwnd)
		{
			try { var sb = new StringBuilder(512); Ws_GetWindowText(hwnd, sb, sb.Capacity); return sb.ToString(); }
			catch { return ""; }
		}

		/// <summary>Visible top-level windows of THIS process whose caption contains `title`, case-insensitively.
		/// EnumWindows, not Globals.AllWindows: Win32 is thread-agnostic, so no dispatcher can wedge this, and a
		/// caller should not have to know a window's exact caption (NT8 retitles windows as their content changes).</summary>
		private static List<KeyValuePair<IntPtr, string>> Ws_FindByTitle(string title)
		{
			var found = new List<KeyValuePair<IntPtr, string>>();
			uint self;
			try { self = (uint)System.Diagnostics.Process.GetCurrentProcess().Id; } catch { return found; }
			Ws_EnumWindowsProc cb = (h, lp) =>
			{
				try
				{
					uint pid;
					Ws_GetWindowThreadProcessId(h, out pid);
					if (pid != self || !Ws_IsWindow(h) || !Ws_IsWindowVisible(h)) return true;
					string t = Ws_TitleOf(h);
					if (t.Length > 0 && t.IndexOf(title, StringComparison.OrdinalIgnoreCase) >= 0)
						found.Add(new KeyValuePair<IntPtr, string>(h, t));
				}
				catch { }
				return true;
			};
			Ws_EnumWindows(cb, IntPtr.Zero);
			GC.KeepAlive(cb);		// the delegate must outlive the unmanaged call
			return found;
		}

		/// <summary>WM_NULL round trip with SMTO_ABORTIFHUNG: true only if the target's own message loop is
		/// pumping right now. PrintWindow sends WM_PRINT with SendMessage (no timeout of its own) straight to the
		/// target's UI thread, so a wedged thread (MEMORY.md nt8-chart-thread-freeze) parks the calling HttpListener
		/// pool thread inside PrintWindow forever. Probing first keeps that hang out of the request path — a hung
		/// target falls straight through to the BitBlt fallback below, which never touches the target's thread.</summary>
		private static bool Ws_IsResponsive(IntPtr hwnd)
		{
			IntPtr result;
			IntPtr sent = Ws_SendMessageTimeout(hwnd, 0 /*WM_NULL*/, IntPtr.Zero, IntPtr.Zero,
				0x0002 /*SMTO_ABORTIFHUNG*/ | 0x0001 /*SMTO_BLOCK*/, 1000, out result);
			return sent != IntPtr.Zero;
		}

		/// <summary>The GDI capture. Every handle acquired in here is released in the finally — skipping any one
		/// of them leaks a GDI handle per call, which degrades NinjaTrader itself.</summary>
		private static byte[] Ws_Capture(IntPtr target, bool fullScreen, int x, int y, int w, int h, out bool printed, out bool composited)
		{
			byte[] px = Ws_Grab(target, fullScreen, x, y, w, h, true, out printed);
			composited = printed && Ws_MergeRenderSurface(target, x, y, w, h, px);
			// BitmapSource + PngBitmapEncoder: no System.Drawing reference needed. Bgr32: PrintWindow's alpha is noise.
			var enc = new PngBitmapEncoder();
			enc.Frames.Add(BitmapFrame.Create(BitmapSource.Create(w, h, 96, 96, PixelFormats.Bgr32, null, px, w * 4)));
			using (var ms = new MemoryStream()) { enc.Save(ms); return ms.ToArray(); }
		}

		/// <summary>NinjaTrader draws a chart's canvas in a SEPARATE top-level window: a WinForms Direct2DForm that
		/// OWNS the WPF chart window and shows through a see-through region of it. Live on 8.1.8.2, PrintWindow of
		/// the WPF window alone gave the frame and toolbar around a uniform (4,4,4) hole — a chart screenshot with
		/// no chart in it. So when the target's owner is such a form, print it as well and put its pixels wherever
		/// the WPF layer is the see-through key. Same rules as the target: PrintWindow only, nothing fronted.</summary>
		private static bool Ws_MergeRenderSurface(IntPtr target, int x, int y, int w, int h, byte[] px)
		{
			IntPtr owner = Ws_GetWindow(target, 4 /*GW_OWNER*/);
			if (owner == IntPtr.Zero || !Ws_IsWindowVisible(owner) || IsIconic(owner)) return false;
			var cls = new StringBuilder(256);
			Ws_GetClassName(owner, cls, cls.Capacity);
			if (!cls.ToString().StartsWith("WindowsForms10.", StringComparison.Ordinal)) return false;
			WinRect o;
			if (!GetWindowRect(owner, out o)) return false;
			int ow = o.Right - o.Left, oh = o.Bottom - o.Top;
			if (ow <= 0 || oh <= 0) return false;
			bool ownerPrinted;
			byte[] under = Ws_Grab(owner, false, o.Left, o.Top, ow, oh, false, out ownerPrinted);
			if (under == null) return false;

			// ponytail: a keyed merge, not an alpha blend — PrintWindow flattens the layered WPF window, so its
			// alpha reads 255 everywhere and the hole is only recognisable by colour. Ceiling: a WPF overlay pixel
			// darker than the key (<= 8 on every channel) lets the chart show through it. Upgrade path: none
			// known short of rendering the WPF layer ourselves.
			int dx = o.Left - x, dy = o.Top - y;
			for (int oy = 0; oy < oh; oy++)
			{
				int ty = oy + dy;
				if (ty < 0 || ty >= h) continue;
				for (int ox = 0; ox < ow; ox++)
				{
					int tx = ox + dx;
					if (tx < 0 || tx >= w) continue;
					int t = (ty * w + tx) * 4;
					if (px[t] > 8 || px[t + 1] > 8 || px[t + 2] > 8) continue;
					int s = (oy * ow + ox) * 4;
					px[t] = under[s]; px[t + 1] = under[s + 1]; px[t + 2] = under[s + 2];
				}
			}
			return true;
		}

		/// <summary>One window (or the screen) as top-down BGRX bytes. `allowBlit` false = PrintWindow or nothing
		/// (null): a screen blit of an occluded render surface would paste the occluder into the chart.</summary>
		private static byte[] Ws_Grab(IntPtr target, bool fullScreen, int x, int y, int w, int h, bool allowBlit, out bool printed)
		{
			printed = false;
			IntPtr srcDc = IntPtr.Zero, screenDc = IntPtr.Zero, memDc = IntPtr.Zero, bmp = IntPtr.Zero, old = IntPtr.Zero;
			try
			{
				srcDc = fullScreen ? Ws_GetDC(IntPtr.Zero) : Ws_GetWindowDC(target);
				if (srcDc == IntPtr.Zero) throw new Exception("could not get a device context");
				memDc = Ws_CreateCompatibleDC(srcDc);
				bmp = Ws_CreateCompatibleBitmap(srcDc, w, h);
				if (memDc == IntPtr.Zero || bmp == IntPtr.Zero) throw new Exception("could not create the capture bitmap");
				old = Ws_SelectObject(memDc, bmp);

				bool ok = false;
				if (!fullScreen && Ws_IsResponsive(target)) ok = printed = Ws_PrintWindow(target, memDc, Ws_PW_RENDERFULLCONTENT);
				if (!ok && !allowBlit) return null;
				if (!ok)
				{
					// Fallback: blit from the screen. An occluded window comes back occluded — that is the truth,
					// not a defect. cli-nt-bridge acquired this DC inline and released it through the window branch, so
					// every fallback leaked one screen DC; here it has its own local and its own release.
					screenDc = fullScreen ? srcDc : Ws_GetDC(IntPtr.Zero);
					if (screenDc != IntPtr.Zero) ok = Ws_BitBlt(memDc, 0, 0, w, h, screenDc, x, y, Ws_SRCCOPY);
				}
				if (!ok) throw new Exception("both PrintWindow and BitBlt failed");

				// The pixels are copied out while the HBITMAP is still alive; the finally deletes it.
				var src = new FormatConvertedBitmap(System.Windows.Interop.Imaging.CreateBitmapSourceFromHBitmap(
					bmp, IntPtr.Zero, Int32Rect.Empty, BitmapSizeOptions.FromEmptyOptions()), PixelFormats.Bgr32, null, 0);
				var px = new byte[w * h * 4];
				src.CopyPixels(px, w * 4, 0);
				return px;
			}
			finally
			{
				try { if (old != IntPtr.Zero) Ws_SelectObject(memDc, old); } catch { }
				try { if (bmp != IntPtr.Zero) Ws_DeleteObject(bmp); } catch { }
				try { if (memDc != IntPtr.Zero) Ws_DeleteDC(memDc); } catch { }
				try { if (screenDc != IntPtr.Zero && screenDc != srcDc) Ws_ReleaseDC(IntPtr.Zero, screenDc); } catch { }
				try { if (srcDc != IntPtr.Zero) Ws_ReleaseDC(fullScreen ? IntPtr.Zero : target, srcDc); } catch { }
			}
		}
	}
}
