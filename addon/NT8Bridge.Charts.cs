// NT8Bridge.Charts.cs — chart endpoints: /charts, /chart/{id}[/bars|/indicators|/drawings|/reload|/screenshot],
// and the Output window scrape, GET /output/window.
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md "Module seams").

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Reflection;
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
	public partial class NT8Bridge
	{
		/// <summary>Seam (NOTES.md "Module seams"): GET /charts, GET /output/window and the six /chart/{id} routes. Null for everything
		/// else, so another module can own further /chart/{id}/... paths; the unknown-chart 404 is only given on
		/// a path that is ours.</summary>
		private static string Route_Charts(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (method == "GET" && seg.Length == 1 && seg[0] == "charts") return Charts();
			// The Output WINDOW scrape (what GET /output was up to 1.1). GET /output itself belongs to the events module.
			if (method == "GET" && seg.Length == 2 && seg[0] == "output" && seg[1] == "window") return OutputWindow(Num(q["n"], 200));
			if (seg.Length < 2 || seg.Length > 3 || seg[0] != "chart") return null;
			string sub = seg.Length == 3 ? seg[2] : "";
			bool mine = method == "GET"	? (sub == "" || sub == "bars" || sub == "indicators" || sub == "drawings")
										: (method == "POST" && (sub == "reload" || sub == "screenshot"));
			if (!mine) return null;

			Gui.Chart.Chart chart;
			try { chart = FindChart(seg[1]); }
			catch (BadRequestException ex) { status = 404; return Obj(P("error", Q(ex.Message))); }
			if (sub == "")				return ChartState(chart);
			if (sub == "bars")			return Bars(chart, Num(q["n"], 50));
			if (sub == "indicators")	return Indicators(chart, q["name"], Num(q["n"], 1));
			if (sub == "drawings")		return Drawings(chart);
			if (sub == "reload")		return Reload(chart);
			return Screenshot(chart, seg[1], body);
		}

		private static FieldInfo Charts_mnuReload;		// resolved once, in Start_Charts

		/// <summary>Seam: runs before the first request is served.</summary>
		private static void Start_Charts()
		{
			Charts_mnuReload = Compat.Resolve("ChartControl.mnuReloadNinjaScript",
				() => typeof(ChartControl).GetField("mnuReloadNinjaScript", BindingFlags.Instance | BindingFlags.NonPublic)) as FieldInfo;
		}

		// ── output window (GET /output/window) ─────────────────────────────────
		/// <summary>Reads the NinjaScript Output WINDOW's text off its visual tree. Only lines printed while the window
		/// is open and realized; the OutputHub ring (core) is the source that needs no window.</summary>
		private static string OutputWindow(int n)
		{
			Window[] all;
			lock (gate) all = windows.ToArray();
			foreach (var w in all)
			{
				if (!(w is Gui.NinjaScript.NinjaScriptOutput)) continue;
				var win = w;
				var blocks = Ui(win.Dispatcher, () => { var list = new List<string>(); WalkText(win, list); return list; });
				var lines = new List<string>();
				for (int t = 0; t < blocks.Count; t++)
					foreach (var line in blocks[t].Replace("\r\n", "\n").Split('\n'))
						if (line.Length > 0) lines.Add("[" + (t + 1) + "] " + line);
				if (lines.Count == 0) return Obj(P("lines", "[]"), P("note", Q("Output window is open but its text could not be read")));
				return Obj(P("lines", Arr(Tail(lines, n).Select(Q))));
			}
			return Obj(P("lines", "[]"), P("note", Q("Output window not open")));
		}

		/// <summary>The Output window's two tabs are plain WPF text controls; take every multi-line block in the
		/// tree, in tree order, and treat each as a tab.</summary>
		private static void WalkText(DependencyObject o, List<string> outp)
		{
			int count = VisualTreeHelper.GetChildrenCount(o);
			for (int i = 0; i < count; i++)
			{
				var child = VisualTreeHelper.GetChild(o, i);
				var tb = child as System.Windows.Controls.TextBox;
				if (tb != null && !string.IsNullOrEmpty(tb.Text)) outp.Add(tb.Text);
				else
				{
					var tblock = child as System.Windows.Controls.TextBlock;
					if (tblock != null && !string.IsNullOrEmpty(tblock.Text) && tblock.Text.IndexOf('\n') >= 0) outp.Add(tblock.Text);
				}
				WalkText(child, outp);
			}
		}

		private static string Charts()
		{
			Window[] all;
			lock (gate) all = windows.Where(w => chartIds.ContainsKey(w)).ToArray();
			var items = new List<string>();
			foreach (var w in all)
			{
				var chart	= (Gui.Chart.Chart)w;
				string id	= IdOf(w) ?? "?";
				try
				{
					items.Add(OnChart(chart, cc =>
					{
						var bars = Primary(cc);
						return Obj(
							P("id", Q(id)),
							P("title", Q(Caption(chart))),
							P("instrument", Q(InstrumentName(bars))),
							P("period", Q(Period(bars))),
							P("barsCount", (bars.Bars != null ? bars.Bars.Count : 0).ToString(CultureInfo.InvariantCulture)),
							P("indicators", Arr(SafeNames(cc.Indicators))),
							P("strategies", Arr(SafeNames(cc.Strategies))));
					}));
				}
				catch (Exception ex) { items.Add(Obj(P("id", Q(id)), P("error", Q(ex.Message)))); }
			}
			return Arr(items);
		}

		private static IEnumerable<string> SafeNames(System.Collections.IEnumerable list)
		{
			var names = new List<string>();
			if (list == null) return names;
			foreach (var o in list) names.Add(Q(NameOf(o)));
			return names;
		}

		/// <summary>The one place a script's name is decided: NinjaScriptBase.Name when it has one, else the
		/// runtime type name. Some vendors clear Name on their instances, which used to give "" everywhere.</summary>
		private static string NameOf(object o)
		{
			if (o == null) return "";
			try
			{
				var ns = o as NinjaScriptBase;
				if (ns != null && !string.IsNullOrEmpty(ns.Name)) return ns.Name;
			}
			catch { }
			try { return o.GetType().Name; } catch { return "?"; }
		}

		/// <summary>The panel a chart object is really drawn on: the ChartPanel whose ChartObjects hold it
		/// (IndicatorRenderBase is an IChartObject). NinjaScriptBase.Panel is the configured value and is -1 on
		/// instances NT8 never set it on, so it is only the last resort.</summary>
		private static int PanelOf(ChartControl cc, object o)
		{
			if (o == null) return -1;
			try
			{
				if (cc.ChartPanels != null)
					foreach (var panel in cc.ChartPanels)
					{
						if (panel == null || panel.ChartObjects == null) continue;
						foreach (var co in panel.ChartObjects)
							if (ReferenceEquals(co, o)) return panel.PanelIndex;
					}
			}
			catch { }
			try
			{
				var ind = o as Gui.NinjaScript.IndicatorRenderBase;		// IndicatorRenderBase.ChartPanel is its own panel back-reference
				if (ind != null && ind.ChartPanel != null) return ind.ChartPanel.PanelIndex;
			}
			catch { }
			try { var ns = o as NinjaScriptBase; if (ns != null) return ns.Panel; } catch { }
			return -1;
		}

		private static string Caption(Gui.Chart.Chart chart)
		{
			try { return !string.IsNullOrEmpty(chart.Caption) ? chart.Caption : (chart.Title ?? ""); }
			catch { return ""; }
		}

		private static string InstrumentName(ChartBars bars)
		{
			try
			{
				if (bars != null && bars.Bars != null && bars.Bars.Instrument != null) return bars.Bars.Instrument.FullName;
			}
			catch { }
			return "";
		}

		private static string Period(ChartBars bars)
		{
			try
			{
				var bp = bars != null && bars.Bars != null ? bars.Bars.BarsPeriod : null;
				if (bp == null) return "";
				string s = bp.ToString();
				return string.IsNullOrEmpty(s) ? bp.Value + " " + bp.BarsPeriodType : s;
			}
			catch { return ""; }
		}

		private static string ChartState(Gui.Chart.Chart chart)
		{
			string id = IdOf(chart) ?? "?";
			return OnChart(chart, cc =>
			{
				var cb		= Primary(cc);
				var bars	= cb.Bars;
				int count	= bars != null ? bars.Count : 0;

				var panels = new List<string>();
				if (cc.ChartPanels != null)
					foreach (var panel in cc.ChartPanels)
					{
						if (panel == null) continue;
						var names = new List<string>();
						if (cc.Indicators != null)
							foreach (var ind in cc.Indicators)
								try { if (ind != null && PanelOf(cc, ind) == panel.PanelIndex) names.Add(Q(NameOf(ind))); } catch { }
						panels.Add(Obj(P("index", panel.PanelIndex.ToString(CultureInfo.InvariantCulture)), P("indicators", Arr(names))));
					}

				var strats = new List<string>();
				if (cc.Strategies != null)
					foreach (var s in cc.Strategies)
					{
						try
						{
							var pos	= s.Position;
							double qty = pos == null || pos.MarketPosition == MarketPosition.Flat ? 0
								: (pos.MarketPosition == MarketPosition.Short ? -pos.Quantity : pos.Quantity);
							strats.Add(Obj(
								P("name", Q(NameOf(s))),
								P("state", Q(s.State.ToString())),
								P("position", Obj(P("qty", D(qty)), P("avg", D(pos == null ? 0 : pos.AveragePrice))))));
						}
						catch (Exception ex) { strats.Add(Obj(P("error", Q(ex.Message)))); }
					}

				double tick = 0;
				try { if (bars != null && bars.Instrument != null) tick = bars.Instrument.MasterInstrument.TickSize; } catch { }

				return Obj(
					P("id", Q(id)),
					P("title", Q(Caption(chart))),
					P("instrument", Q(InstrumentName(cb))),
					P("period", Q(Period(cb))),
					P("tickSize", D(tick)),
					P("barsCount", count.ToString(CultureInfo.InvariantCulture)),
					P("firstTime", count > 0 ? Tm(bars.GetTime(0)) : "null"),
					P("lastTime", count > 0 ? Tm(bars.GetTime(count - 1)) : "null"),
					P("visibleFrom", TimeAt(bars, cb.FromIndex)),
					P("visibleTo", TimeAt(bars, cb.ToIndex)),
					P("lastPrice", count > 0 ? D(bars.GetClose(count - 1)) : "null"),
					P("panels", Arr(panels)),
					P("strategies", Arr(strats)));
			});
		}

		private static string TimeAt(Data.Bars bars, int index)
		{
			try
			{
				if (bars == null || bars.Count == 0) return "null";
				return Tm(bars.GetTime(Math.Max(0, Math.Min(index, bars.Count - 1))));
			}
			catch { return "null"; }
		}

		private static string Bars(Gui.Chart.Chart chart, int n)
		{
			if (n < 1) n = 1;
			return OnChart(chart, cc =>
			{
				var bars = Primary(cc).Bars;
				int count = bars != null ? bars.Count : 0;
				var items = new List<string>();
				for (int i = Math.Max(0, count - n); i < count; i++)
					items.Add(Obj(
						P("time", Tm(bars.GetTime(i))),
						P("o", D(bars.GetOpen(i))), P("h", D(bars.GetHigh(i))),
						P("l", D(bars.GetLow(i))), P("c", D(bars.GetClose(i))),
						P("v", D(bars.GetVolume(i)))));
				return Arr(items);
			});
		}

		private static string Indicators(Gui.Chart.Chart chart, string nameFilter, int n)
		{
			if (n < 1) n = 1;
			return OnChart(chart, cc =>
			{
				var drawCounts = DrawingsByOwner(cc);
				var items = new List<string>();
				if (cc.Indicators != null)
					foreach (var ind in cc.Indicators)
					{
						if (ind == null) continue;
						string name = NameOf(ind);
						if (!string.IsNullOrEmpty(nameFilter) && name.IndexOf(nameFilter, StringComparison.OrdinalIgnoreCase) < 0) continue;

						int drawn;
						drawCounts.TryGetValue(name, out drawn);

						// An indicator that is not live has no Values and no Bars: say so instead of throwing.
						if (ind.State != State.Realtime && ind.State != State.Historical)
						{
							items.Add(Obj(P("name", Q(name)), P("error", Q("indicator state is " + ind.State))));
							continue;
						}
						if (ind.Bars == null)
						{
							items.Add(Obj(P("name", Q(name)), P("error", Q("indicator has no bars"))));
							continue;
						}

						try { items.Add(IndicatorJson(ind, name, PanelOf(cc, ind), n, drawn)); }
						catch (Exception ex) { items.Add(Obj(P("name", Q(name)), P("error", Q(ex.Message)))); }
					}
				return Arr(items);
			});
		}

		private static string IndicatorJson(Gui.NinjaScript.IndicatorRenderBase ind, string name, int panel, int n, int drawn)
		{
			// Inputs: the properties the author marked [NinjaScriptProperty]. Reflection is the only way in —
			// the set is per-indicator by definition.
			var inputs = new List<string>();
			foreach (var p in ind.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance))
			{
				if (!p.CanRead || p.GetIndexParameters().Length > 0) continue;
				if (!IsInput(p)) continue;
				object v;
				try { v = p.GetValue(ind, null); } catch { continue; }
				inputs.Add(P(p.Name, Scalar(v)));
			}

			var bars	= ind.Bars;
			int count	= bars.Count;
			var plots	= new List<string>();
			if (ind.Plots != null)
				for (int i = 0; i < ind.Plots.Length; i++)
				{
					string plotName = ind.Plots[i] != null ? ind.Plots[i].Name : "plot" + i;
					var values = new List<string>();
					if (ind.Values != null && i < ind.Values.Length && ind.Values[i] != null)
					{
						var series = ind.Values[i];
						for (int b = Math.Max(0, count - n); b < count; b++)
						{
							string v = "null";
							try { if (series.IsValidDataPointAt(b)) v = D(series.GetValueAt(b)); } catch { }
							values.Add(Obj(P("time", Tm(bars.GetTime(b))), P("value", v)));
						}
					}
					plots.Add(Obj(P("name", Q(plotName)), P("values", Arr(values))));
				}

			string display;
			try { display = ind.DisplayName ?? name; } catch { display = name; }

			return Obj(
				P("name", Q(name)),
				P("displayName", Q(display)),
				P("panel", panel.ToString(CultureInfo.InvariantCulture)),
				P("inputs", Obj(inputs.ToArray())),
				P("plots", Arr(plots)),
				P("drawings", drawn.ToString(CultureInfo.InvariantCulture)));
		}

		// ── drawings ───────────────────────────────────────────────────────────
		private static IEnumerable<DrawingTool> DrawingTools(ChartControl cc)
		{
			var found = new List<DrawingTool>();
			if (cc.ChartPanels == null) return found;
			foreach (var panel in cc.ChartPanels)
			{
				if (panel == null || panel.ChartObjects == null) continue;
				foreach (var o in panel.ChartObjects)
				{
					var dt = o as DrawingTool;
					if (dt != null) found.Add(dt);
				}
			}
			return found;
		}

		/// <summary>NinjaScript-drawn objects per owning indicator name, for the "drawings" count.</summary>
		private static Dictionary<string, int> DrawingsByOwner(ChartControl cc)
		{
			var map = new Dictionary<string, int>();
			foreach (var dt in DrawingTools(cc))
			{
				string owner = Owner(dt);
				if (owner == "user") continue;
				int c;
				map.TryGetValue(owner, out c);
				map[owner] = c + 1;
			}
			return map;
		}

		private static string Owner(DrawingTool dt)
		{
			try
			{
				if (dt.IsUserDrawn) return "user";
				var ns = dt.DrawnBy;			// IsUserDrawn is exactly DrawnBy == null
				if (ns != null) return NameOf(ns);		// same fallback as everywhere else, so the drawings count keys match
			}
			catch { }
			return "";
		}

		private static string Drawings(Gui.Chart.Chart chart)
		{
			return OnChart(chart, cc =>
			{
				var items = new List<string>();
				foreach (var dt in DrawingTools(cc))
				{
					try
					{
						var anchors = new List<string>();
						if (dt.Anchors != null)
							foreach (var a in dt.Anchors)
								if (a != null) anchors.Add(Obj(P("time", Tm(a.Time)), P("price", D(a.Price))));

						string text = null;
						var txt = dt as NinjaTrader.NinjaScript.DrawingTools.Text;
						if (txt != null) text = txt.DisplayText;
						else
						{
							var fixedTxt = dt as TextFixed;
							if (fixedTxt != null) text = fixedTxt.DisplayText;
						}

						items.Add(Obj(
							P("tag", Q(dt.Tag)),
							P("type", Q(dt.GetType().Name)),
							P("owner", Q(Owner(dt))),
							P("anchors", Arr(anchors)),
							P("text", Q(text))));
					}
					catch (Exception ex) { items.Add(Obj(P("error", Q(ex.Message)))); }
				}
				return Arr(items);
			});
		}

		// ── reload / screenshot ────────────────────────────────────────────────
		private static string Reload(Gui.Chart.Chart chart)
		{
			return OnChart(chart, cc =>
			{
				// NT8 has no public "reload" method. The right-click entry is ChartControl's private
				// mnuReloadNinjaScript MenuItem, so raise its Click — that is literally the menu path.
				// Fallback: the public hot-key handler behind Ctrl+Shift+R.
				var f = Charts_mnuReload;		// see /compat: "ChartControl.mnuReloadNinjaScript"
				var item = f == null ? null : f.GetValue(cc) as System.Windows.Controls.MenuItem;
				if (item != null) item.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.MenuItem.ClickEvent));
				else cc.OnReloadNinjaScriptHotKey(cc, null);
				return Obj(P("ok", "true"));
			});
		}

		private static string Screenshot(Gui.Chart.Chart chart, string id, string body)
		{
			string path = null;
			try { path = JGetStr(ParseJson(body) as Dictionary<string, object>, "path", null); } catch { }
			if (string.IsNullOrEmpty(path))
				path = Path.Combine(Path.Combine(Path.GetTempPath(), "nt8bridge"), (id == "first" ? (IdOf(chart) ?? "chart") : id) + ".png");

			var cc = Control(chart);
			var png = Ui(cc.Dispatcher, () =>
			{
				BitmapSource bmp = null;
				// NTWindow.GetScreenshot is the public NT8 path and gives the whole chart; RenderTargetBitmap of
				// the ChartControl is the fallback when it returns nothing.
				try { bmp = chart.GetScreenshot(ShareScreenshotType.Chart, null); } catch { }
				if (bmp == null)
				{
					int w = (int)Math.Round(cc.ActualWidth), h = (int)Math.Round(cc.ActualHeight);
					if (w <= 0 || h <= 0) throw new Exception("chart has no size");
					var rtb = new RenderTargetBitmap(w, h, 96, 96, PixelFormats.Pbgra32);
					rtb.Render(cc);
					bmp = rtb;
				}
				if (bmp.CanFreeze && !bmp.IsFrozen) bmp.Freeze();
				var encoder = new PngBitmapEncoder();
				encoder.Frames.Add(BitmapFrame.Create(bmp));
				using (var ms = new MemoryStream()) { encoder.Save(ms); return new object[] { ms.ToArray(), bmp.PixelWidth, bmp.PixelHeight }; }
			});

			string dir = Path.GetDirectoryName(path);
			if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir)) Directory.CreateDirectory(dir);
			File.WriteAllBytes(path, (byte[])png[0]);
			return Obj(P("ok", "true"), P("path", Q(path)),
				P("width", ((int)png[1]).ToString(CultureInfo.InvariantCulture)),
				P("height", ((int)png[2]).ToString(CultureInfo.InvariantCulture)));
		}
	}
}
