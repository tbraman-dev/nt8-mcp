// NT8Bridge.Compile.cs — NinjaScript compile endpoint: POST /compile and POST /compile?reload=1.
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md "Module seams").
//
// Portions of this file are derived from cli-nt-bridge
// (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
// MIT License. The full notice is in NOTICE at the repository root.

#region Using declarations
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		// ── compile vs reload ──────────────────────────────────────────────────
		//  checkCompileOnly is the ONLY argument that differs, and it is the difference between
		//  "does this tree build" and "is this code now RUNNING".
		//
		//    POST /compile           checkCompileOnly = true  — validates and emits nothing. Disturbs
		//                            nothing: no indicator reload, no strategy restart, no chart flicker.
		//    POST /compile?reload=1  checkCompileOnly = false — a real build; NinjaTrader swaps the
		//                            NinjaScript assembly exactly as the Editor's F5 does.
		//
		//  reload is DISRUPTIVE by nature: it restarts indicators, can interrupt a running strategy, and
		//  orphans bars-type instances (they keep executing the pre-reload assembly until NinjaTrader is
		//  restarted — which matters here for any custom bar type in use). It is therefore gated
		//  on AnyLiveConnected() and needs {"force":true} to push past it.
		//  The diagnostics are IDENTICAL either way; the only observable difference is the swap.

		/// <summary>One compile at a time. A compile holds this 20-30 s, so it is taken with TryEnter and a
		/// second request is refused at once rather than queued behind it (the core's Route must not block).</summary>
		private static readonly object Compile_gate = new object();

		/// <summary>NinjaTrader.Code.Compiler.Compile(bool,bool,IEnumerable&lt;string&gt;,IEnumerable&lt;string&gt;),
		/// resolved ONCE in Start_Compile through Compat. The class is [EditorBrowsable(Never)] public static,
		/// so reflection is the only route.</summary>
		private static MethodInfo Compile_compile;

		// Roslyn's Diagnostic.GetMessage has no zero-arg overload — it is GetMessage(IFormatProvider = null).
		// Probing it walks every method on the type, so the result is cached. Written only under Compile_gate.
		private static Type Compile_diagType;
		private static MethodInfo Compile_getMessage;

		/// <summary>`warnings` lists at most this many; `warningCount` is always the full number. Errors are never capped.</summary>
		private const int Compile_WarnCap = 200;

		/// <summary>Seam (NOTES.md "Module seams"): POST /compile[?reload=1]. Null for everything else — a GET
		/// /compile falls through to the core's 404.</summary>
		private static string Route_Compile(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (method == "POST" && seg.Length == 1 && seg[0] == "compile") return Compile_Run(q, body, ref status);
			return null;
		}

		/// <summary>Seam: resolve the compiler entry point once, before the first request is served.</summary>
		private static void Start_Compile()
		{
			Compile_compile = Compat.Resolve("Compiler.Compile", () =>
			{
				Type t = Type.GetType("NinjaTrader.Code.Compiler, NinjaTrader.Core");
				if (t == null) return null;
				return t.GetMethod("Compile", BindingFlags.Public | BindingFlags.Static, null,
					new[] { typeof(bool), typeof(bool), typeof(IEnumerable<string>), typeof(IEnumerable<string>) }, null);
			}) as MethodInfo;
		}
		// No Stop_Compile: this module subscribes to no event and starts no thread.

		private static string Compile_Run(System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			bool reload = Compile_Flag(q["reload"]);
			bool force;
			try
			{
				var req = ParseJson(body) as Dictionary<string, object>;
				force = JGetBool(req, "force", false);
			}
			catch (BadRequestException ex) { return Err(ref status, 400, ex.Message); }
			catch (Exception ex) { return Err(ref status, 400, "body is not JSON: " + ex.Message); }

			// Order-routing risk: a check-only compile emits nothing and is never refused.
			if (reload)
			{
				string refusal = RefuseIfLive(ref status, "compile?reload=1", force);
				if (refusal != null) return refusal;
			}

			if (Compile_compile == null)
				return Err(ref status, 503, "NinjaTrader.Code.Compiler.Compile(bool,bool,IEnumerable<string>,IEnumerable<string>) did not resolve — see GET /compat");

			if (!Monitor.TryEnter(Compile_gate))
				return Err(ref status, 409, "a compile is already running — one at a time");
			try
			{
				var sw = System.Diagnostics.Stopwatch.StartNew();
				// checkCompileOnly = !reload, debugBuild = false, filesToIgnore = [], filesInTmp = []
				object emit = Compile_compile.Invoke(null, new object[] { !reload, false, new List<string>(), new List<string>() });
				sw.Stop();

				// Loud, not plausible: no result object means we do not know whether it built, and a cheerful
				// ok:true here is exactly the lie this endpoint exists to stop telling.
				if (emit == null) return Err(ref status, 500, "NinjaTrader's compiler returned no result object");

				bool success;
				List<string> errors, warnings;
				int warningCount, warningsSuppressed;
				if (!Compile_Read(emit, out success, out errors, out warnings, out warningCount, out warningsSuppressed))
					return Err(ref status, 500, "NinjaTrader's compiler result could not be read — see GET /compat");
				bool ok = success && errors.Count == 0;

				// TRUTHFULNESS. This is the one field cli-nt-bridge got wrong twice.
				// It is a CLAIM, and it is the strongest claim the compiling process can make: only a non-check
				// build that emitted successfully swapped the assembly. It cannot be an observation here, because
				// the code writing this response IS the pre-swap assembly — its own Location never changes, and
				// bin\Custom\NinjaTrader.Custom.dll's mtime and ModuleVersionId both lie after a hot reload
				// (the running code is Documents\NinjaTrader 8\tmp\<guid>.dll) — AND a pre-swap assembly that
				// never restarted still reports a NEW assemblyBuiltUtc once the reload's emit overwrites that
				// same dll file, so assemblyBuiltUtc alone is not proof of anything. So the response also
				// carries assemblyBuiltUtc and startedAt OF THE ASSEMBLY THAT ANSWERED: a client that re-reads
				// /health after the swap and sees startedAt change (set once, in TryBind(), only when a
				// process binds :7891) has an out-of-band OBSERVATION of the reload. That comparison — not
				// this field — is the interlock.
				string json = Obj(
					P("ok", ok ? "true" : "false"),
					P("assemblyReloaded", (reload && ok) ? "true" : "false"),
					P("reloadRequested", reload ? "true" : "false"),
					P("checkCompileOnly", reload ? "false" : "true"),
					P("seconds", D(sw.Elapsed.TotalSeconds)),
					P("errors", Arr(errors)),
					P("warnings", Arr(warnings)),
					P("warningCount", I(warningCount)),
					P("warningsSuppressed", I(warningsSuppressed)),
					P("assemblyBuiltUtc", TmUtc(AssemblyBuiltUtc())),
					P("startedAt", Tm(startedAt)),
					P("compiledAtUtc", TmUtc(DateTime.UtcNow)));

				Compile_WriteLast(json);
				Log("compile: reload=" + reload + " ok=" + ok + " errors=" + errors.Count + " warnings=" + warningCount + " (+" + warningsSuppressed + " CS1701/CS1702 suppressed)"
					+ " in " + sw.Elapsed.TotalSeconds.ToString("0.0", CultureInfo.InvariantCulture) + "s");
				return json;
			}
			finally { Monitor.Exit(Compile_gate); }
		}

		private static bool Compile_Flag(string v)
		{
			return v == "1" || string.Equals(v, "true", StringComparison.OrdinalIgnoreCase);
		}

		/// <summary>Read EmitResult by reflection: NinjaTrader.Custom references no Microsoft.CodeAnalysis, so
		/// the result is only ever an object here. Errors AND warnings — cli-nt-bridge's loop dropped every warning
		/// (addon/NT8BridgeServer.cs:2561), which this closes.
		/// Returns false when `Success` or `Diagnostics` cannot be read off the result's shape — an unresolved
		/// member must be reported loudly, never degraded into a cheerful ok:true with empty errors.</summary>
		private static bool Compile_Read(object emit, out bool success, out List<string> errors, out List<string> warnings,
			out int warningCount, out int warningsSuppressed)
		{
			errors = new List<string>();
			warnings = new List<string>();
			success = false;
			warningCount = 0;
			warningsSuppressed = 0;

			Type t = emit.GetType();
			PropertyInfo succ = t.GetProperty("Success");
			PropertyInfo diagProp = t.GetProperty("Diagnostics");
			if (succ == null || diagProp == null)
			{
				Compat.Set("Compiler.EmitResult.shape", false,
					t.FullName + " is missing " + (succ == null ? "Success" : "Diagnostics"), null);
				return false;
			}
			Compat.Set("Compiler.EmitResult.shape", true, t.FullName, null);

			success = (bool)succ.GetValue(emit, null);

			// ImmutableArray<Diagnostic> is a struct; boxed here, it still enumerates as IEnumerable.
			IEnumerable diags = diagProp.GetValue(emit, null) as IEnumerable;
			if (diags == null) return true;	// a null/empty diagnostics collection is a clean build, not unreadable

			foreach (object d in diags)
			{
				if (d == null) continue;
				Type dt = d.GetType();
				string sev = Compile_Str(Compile_Prop(dt, d, "Severity"));
				List<string> bucket = sev == "Error" ? errors : (sev == "Warning" ? warnings : null);
				if (bucket == null) continue;		// Hidden / Info

				// Observed on NinjaTrader 8.1.8.2: a CLEAN tree gives 113 796 warnings, 113 779 of them CS1701
				// ("Assuming assembly reference mscorlib 2.0.0.0 ... matches 4.0.0.0") — one per reference per
				// file, no file/line, a 38 MB response. NinjaTrader's own editor never shows them. They are
				// counted, not listed; the real warnings are listed up to Compile_WarnCap and counted in full.
				if (bucket == warnings)
				{
					string id = Compile_Str(Compile_Prop(dt, d, "Id"));
					if (id == "CS1701" || id == "CS1702") { warningsSuppressed++; continue; }
					warningCount++;
					if (warnings.Count >= Compile_WarnCap) continue;
				}

				string file = "";
				int line = 0, column = 0;
				object loc = Compile_Prop(dt, d, "Location");
				if (loc != null)
				{
					MethodInfo span = loc.GetType().GetMethod("GetLineSpan", Type.EmptyTypes);
					object ls = span != null ? span.Invoke(loc, null) : null;
					if (ls != null)
					{
						file = Compile_Str(Compile_Prop(ls.GetType(), ls, "Path"));
						object start = Compile_Prop(ls.GetType(), ls, "StartLinePosition");
						if (start != null)
						{
							// Roslyn line AND character are both 0-based; editors are 1-based.
							object ln = Compile_Prop(start.GetType(), start, "Line");
							object ch = Compile_Prop(start.GetType(), start, "Character");
							if (ln is int) line = (int)ln + 1;
							if (ch is int) column = (int)ch + 1;
						}
					}
				}

				bucket.Add(Obj(
					P("file", Q(file)),
					P("line", I(line)),
					P("column", I(column)),
					P("code", Q(Compile_Str(Compile_Prop(dt, d, "Id")))),
					P("message", Q(Compile_Message(dt, d)))));
			}
			return true;
		}

		private static object Compile_Prop(Type t, object o, string name)
		{
			try { PropertyInfo p = t.GetProperty(name); return p == null ? null : p.GetValue(o, null); }
			catch { return null; }
		}

		private static string Compile_Str(object o) { return o == null ? "" : o.ToString(); }

		private static string Compile_Message(Type dt, object d)
		{
			if (dt != Compile_diagType)
			{
				MethodInfo found = null;
				foreach (MethodInfo mi in dt.GetMethods())
				{
					if (mi.Name != "GetMessage") continue;
					int np = mi.GetParameters().Length;
					if (np > 1) continue;
					found = mi;
					if (np == 0) break;
				}
				Compile_getMessage = found;
				Compile_diagType = dt;
			}
			MethodInfo m = Compile_getMessage;
			if (m == null) return "";
			try { return Compile_Str(m.Invoke(d, m.GetParameters().Length == 0 ? null : new object[] { null })); }
			catch (Exception ex) { return Deep(ex); }
		}

		/// <summary>Surviving the swap: reload=1 replaces the assembly this listener lives in, so the socket can
		/// drop before the response is written. The same JSON goes to &lt;UserDataDir&gt;\NT8Bridge\compile_last.json
		/// so the client can read the result it never received. Best effort — never fails the request.</summary>
		private static void Compile_WriteLast(string json)
		{
			try
			{
				string dir = Path.Combine(Core.Globals.UserDataDir, "NT8Bridge");
				Directory.CreateDirectory(dir);
				// No BOM: Encoding.UTF8 writes one, and Python's json.load(encoding="utf-8") refuses a BOM —
				// the fallback file would be unreadable by the one client it exists for.
				File.WriteAllText(Path.Combine(dir, "compile_last.json"), json, new UTF8Encoding(false));
			}
			catch (Exception ex) { Log("compile_last.json: " + ex.Message); }
		}
	}
}
