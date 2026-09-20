// NT8Bridge.Api.cs — NinjaScript API lookup by reflection over the already-loaded NinjaTrader
// assemblies: GET /api/search?q=&limit=  GET /api/type?name=&member=
// So a model calling this stops inventing a signature and asks for the real one instead.
// Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md "Module seams").
//
// Pure reflection on types/members already loaded in this AppDomain: no instance is created,
// nothing is invoked, no NinjaTrader call is made. Runs inline on the HttpListener worker thread —
// no Ui(), no dispatcher, nothing that can block a chart.

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Reflection;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		/// <summary>Seam (NOTES.md "Module seams"): GET /api/search and GET /api/type. Null for everything else.</summary>
		private static string Route_Api(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (method != "GET" || seg.Length != 2 || seg[0] != "api") return null;

			if (seg[1] == "search")
			{
				string query = q["q"];
				if (string.IsNullOrWhiteSpace(query)) { status = 400; return Obj(P("error", Q("q is required"))); }
				int limit = Num(q["limit"], 30);
				if (limit > 200) limit = 200;
				return Api_SearchJson(query, limit);
			}

			if (seg[1] == "type")
			{
				string name = q["name"];
				if (string.IsNullOrWhiteSpace(name)) { status = 400; return Obj(P("error", Q("name is required"))); }
				var matches = Api_FindTypes(name);
				if (matches.Count == 0) { status = 404; return Obj(P("error", Q("no type '" + name + "' in the loaded NinjaTrader assemblies"))); }
				if (matches.Count > 1)
					return Obj(P("ambiguous", "true"), P("candidates", Arr(matches.Select(t => Q(t.FullName ?? t.Name)))));
				return Api_TypeJson(matches[0], q["member"]);
			}

			return null;
		}

		// ── which assemblies count as "NinjaTrader" ─────────────────────────────
		private static readonly string[] Api_Assemblies = { "NinjaTrader.Core", "NinjaTrader.Gui", "NinjaTrader.Custom", "NinjaTrader.Vendor" };
		private const int Api_MemberCap = 500;

		private static IEnumerable<Assembly> Api_TargetAssemblies()
		{
			Assembly[] all = AppDomain.CurrentDomain.GetAssemblies();
			foreach (var name in Api_Assemblies)
			{
				Assembly found = null;
				foreach (var a in all) { try { if (a.GetName().Name == name) { found = a; break; } } catch { } }
				if (found != null) yield return found;
			}
		}

		/// <summary>GetTypes() throws ReflectionTypeLoadException when even one type in the assembly fails to
		/// load; Types still carries every type that DID load (with nulls for the ones that did not) — one
		/// broken assembly (of the four) must not blank out search/type lookup in the other three.</summary>
		private static Type[] Api_SafeTypes(Assembly asm)
		{
			try { return asm.GetTypes(); }
			catch (ReflectionTypeLoadException ex) { return ex.Types.Where(t => t != null).ToArray(); }
			catch { return new Type[0]; }
		}

		private static string Api_AssemblyName(Type t) { try { return t.Assembly.GetName().Name; } catch { return "?"; } }

		private static string Api_TypeKind(Type t)
		{
			if (t.IsEnum) return "Enum";
			if (t.IsInterface) return "Interface";
			if (typeof(Delegate).IsAssignableFrom(t)) return "Delegate";
			if (t.IsValueType) return "Struct";
			return "Class";
		}

		// ── /api/search ──────────────────────────────────────────────────────────
		private sealed class Api_Hit { public int Rank; public string Name; public string Json; }

		/// <summary>-1 = no match. 0 = exact (case-insensitive), 1 = prefix, 2 = contains.</summary>
		private static int Api_Rank(string name, string query)
		{
			if (string.IsNullOrEmpty(name)) return -1;
			if (string.Equals(name, query, StringComparison.OrdinalIgnoreCase)) return 0;
			if (name.StartsWith(query, StringComparison.OrdinalIgnoreCase)) return 1;
			if (name.IndexOf(query, StringComparison.OrdinalIgnoreCase) >= 0) return 2;
			return -1;
		}

		private static string Api_MemberKind(MemberInfo m)
		{
			if (m is MethodInfo) return "Method";
			if (m is PropertyInfo) return "Property";
			if (m is FieldInfo) return "Field";
			if (m is EventInfo) return "Event";
			if (m is ConstructorInfo) return "Constructor";
			return "Member";
		}

		/// <summary>Ranked (exact, prefix, contains) search over public type names AND public member names,
		/// across the loaded NinjaTrader assemblies. Bounded to `limit`; `matched` is the true count.</summary>
		private static string Api_SearchJson(string query, int limit)
		{
			var hits = new List<Api_Hit>();
			const BindingFlags memberFlags = BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;

			foreach (var asm in Api_TargetAssemblies())
			{
				foreach (var t in Api_SafeTypes(asm))
				{
					if (t == null || !(t.IsPublic || t.IsNestedPublic)) continue;
					string tname = t.FullName ?? t.Name;

					int trank = Api_Rank(t.Name, query);
					if (trank >= 0)
						hits.Add(new Api_Hit
						{
							Rank = trank,
							Name = tname,
							Json = Obj(
								P("rank", I(trank)),
								P("resultKind", Q("Type")),
								P("typeKind", Q(Api_TypeKind(t))),
								P("name", Q(tname)),
								P("assembly", Q(Api_AssemblyName(t))))
						});

					// Declared-only: an inherited member is found once, on the type that declares it —
					// scanning every subtype too would repeat the same hit hundreds of times.
					foreach (var m in t.GetMembers(memberFlags))
					{
						if (m.MemberType == MemberTypes.NestedType) continue;
						var mi = m as MethodInfo;
						if (mi != null && mi.IsSpecialName) continue;		// property/event accessors: listed under their property/event

						int mrank = Api_Rank(m.Name, query);
						if (mrank < 0) continue;
						hits.Add(new Api_Hit
						{
							Rank = mrank,
							Name = m.Name,
							Json = Obj(
								P("rank", I(mrank)),
								P("resultKind", Q(Api_MemberKind(m))),
								P("name", Q(m.Name)),
								P("declaringType", Q(tname)),
								P("assembly", Q(Api_AssemblyName(t))))
						});
					}
				}
			}

			var ordered = hits.OrderBy(h => h.Rank).ThenBy(h => h.Name, StringComparer.OrdinalIgnoreCase).ToList();
			bool truncated = ordered.Count > limit;
			var page = ordered.Take(limit).Select(h => h.Json);
			return Obj(P("query", Q(query)), P("results", Arr(page)), P("matched", I(ordered.Count)), P("truncated", truncated ? "true" : "false"));
		}

		// ── /api/type ────────────────────────────────────────────────────────────
		/// <summary>Exact FullName match wins outright; otherwise every publicly visible type whose simple
		/// Name matches (case-insensitive) — 0, 1 (use it) or many (report as ambiguous candidates).</summary>
		private static List<Type> Api_FindTypes(string name)
		{
			var exact = new List<Type>();
			var byName = new List<Type>();
			foreach (var asm in Api_TargetAssemblies())
				foreach (var t in Api_SafeTypes(asm))
				{
					if (t == null || !(t.IsPublic || t.IsNestedPublic)) continue;
					string full = t.FullName ?? t.Name;
					if (string.Equals(full, name, StringComparison.OrdinalIgnoreCase)) exact.Add(t);
					else if (string.Equals(t.Name, name, StringComparison.OrdinalIgnoreCase)) byName.Add(t);
				}
			return exact.Count > 0 ? exact : byName;
		}

		private static bool Api_IsPublicOrProtected(MemberInfo m)
		{
			var f = m as FieldInfo; if (f != null) return f.IsPublic || f.IsFamily || f.IsFamilyOrAssembly;
			var p = m as PropertyInfo;
			if (p != null) { var acc = p.GetAccessors(true); return acc.Any(a => a.IsPublic || a.IsFamily || a.IsFamilyOrAssembly); }
			var e = m as EventInfo;
			if (e != null) { var add = e.GetAddMethod(true); return add != null && (add.IsPublic || add.IsFamily || add.IsFamilyOrAssembly); }
			var mb = m as MethodBase; if (mb != null) return mb.IsPublic || mb.IsFamily || mb.IsFamilyOrAssembly;
			return false;
		}

		private static string Api_TypeJson(Type t, string memberFilter)
		{
			var interfaces = t.GetInterfaces().Where(i => i.IsPublic || i.IsNestedPublic).Select(i => Q(Api_TypeName(i)));

			if (t.IsEnum)
			{
				var values = new List<string>();
				foreach (var n in Enum.GetNames(t))
				{
					long num;
					try { num = Convert.ToInt64(Enum.Parse(t, n), CultureInfo.InvariantCulture); } catch { num = 0; }
					values.Add(Obj(P("name", Q(n)), P("value", I(num))));
				}
				return Obj(
					P("name", Q(t.FullName ?? t.Name)),
					P("assembly", Q(Api_AssemblyName(t))),
					P("kind", Q("Enum")),
					P("baseType", "null"),
					P("interfaces", Arr(interfaces)),
					P("values", Arr(values)));
			}

			// FlattenHierarchy: without it, an inherited STATIC member is invisible on the derived type
			// (instance members are inherited either way) — a gap that would silently hide half of a
			// base class's static API from a type that inherits it.
			const BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.FlattenHierarchy;
			var members = new List<MemberInfo>();
			members.AddRange(t.GetFields(flags));
			members.AddRange(t.GetProperties(flags));
			members.AddRange(t.GetEvents(flags));
			members.AddRange(t.GetConstructors(flags));
			members.AddRange(t.GetMethods(flags).Where(m => !m.IsSpecialName));

			IEnumerable<MemberInfo> visible = members.Where(Api_IsPublicOrProtected);
			if (!string.IsNullOrEmpty(memberFilter))
				visible = visible.Where(m => m.Name.IndexOf(memberFilter, StringComparison.OrdinalIgnoreCase) >= 0);
			var sorted = visible.OrderBy(m => m.Name, StringComparer.OrdinalIgnoreCase).ToList();		// overloads of the same name land together

			bool truncated = sorted.Count > Api_MemberCap;
			var items = sorted.Take(Api_MemberCap).Select(Api_MemberJson);

			return Obj(
				P("name", Q(t.FullName ?? t.Name)),
				P("assembly", Q(Api_AssemblyName(t))),
				P("kind", Q(Api_TypeKind(t))),
				P("baseType", t.BaseType == null ? "null" : Q(Api_TypeName(t.BaseType))),
				P("interfaces", Arr(interfaces)),
				P("members", Arr(items)),
				P("memberCount", I(sorted.Count)),
				P("truncated", truncated ? "true" : "false"));
		}

		private static string Api_MemberJson(MemberInfo m)
		{
			var ctor = m as ConstructorInfo;
			if (ctor != null)
				return Obj(P("kind", Q("Constructor")), P("name", Q(ctor.DeclaringType.Name)),
					P("declaringType", Q(Api_TypeName(ctor.DeclaringType))), P("static", ctor.IsStatic ? "true" : "false"),
					P("signature", Q(Api_MethodSignature(ctor, null))));

			var method = m as MethodInfo;
			if (method != null)
				return Obj(P("kind", Q("Method")), P("name", Q(method.Name)),
					P("declaringType", Q(Api_TypeName(method.DeclaringType))), P("static", method.IsStatic ? "true" : "false"),
					P("signature", Q(Api_MethodSignature(method, method.ReturnType))));

			var prop = m as PropertyInfo;
			if (prop != null)
			{
				var get = prop.GetGetMethod(true);
				var set = prop.GetSetMethod(true);
				var mainAcc = get ?? set;
				return Obj(P("kind", Q("Property")), P("name", Q(prop.Name)),
					P("declaringType", Q(Api_TypeName(prop.DeclaringType))),
					P("static", mainAcc != null && mainAcc.IsStatic ? "true" : "false"),
					P("propertyType", Q(Api_TypeName(prop.PropertyType))),
					P("get", get != null ? "true" : "false"), P("set", set != null ? "true" : "false"),
					P("signature", Q(Api_PropertySignature(prop, get, set))));
			}

			var field = m as FieldInfo;
			if (field != null)
				return Obj(P("kind", Q("Field")), P("name", Q(field.Name)),
					P("declaringType", Q(Api_TypeName(field.DeclaringType))), P("static", field.IsStatic ? "true" : "false"),
					P("fieldType", Q(Api_TypeName(field.FieldType))),
					P("signature", Q(Api_FieldSignature(field))));

			var ev = m as EventInfo;
			if (ev != null)
				return Obj(P("kind", Q("Event")), P("name", Q(ev.Name)),
					P("declaringType", Q(Api_TypeName(ev.DeclaringType))),
					P("eventType", Q(Api_TypeName(ev.EventHandlerType))),
					P("signature", Q(Api_EventSignature(ev))));

			return Obj(P("kind", Q("Unknown")), P("name", Q(m.Name)));
		}

		// ── signature text ───────────────────────────────────────────────────────
		private static string Api_Accessibility(MethodBase m)
		{
			if (m.IsPublic) return "public";
			if (m.IsFamilyOrAssembly) return "protected internal";
			if (m.IsFamily) return "protected";
			return "internal";
		}

		private static string Api_MethodModifiers(MethodBase m)
		{
			var parts = new List<string> { Api_Accessibility(m) };
			if (m.IsStatic) parts.Add("static");
			var mi = m as MethodInfo;
			if (mi != null)
			{
				if (mi.IsAbstract) parts.Add("abstract");
				else if (mi.IsVirtual)
				{
					bool isOverride = false;
					try { isOverride = mi.GetBaseDefinition().DeclaringType != mi.DeclaringType; } catch { }
					parts.Add(isOverride ? "override" : "virtual");
				}
			}
			return string.Join(" ", parts);
		}

		private static string Api_ParamText(ParameterInfo p)
		{
			Type t = p.ParameterType;
			string prefix = "";
			if (t.IsByRef) { prefix = p.IsOut ? "out " : "ref "; t = t.GetElementType(); }
			string def = "";
			try { if (p.HasDefaultValue) def = " = " + Api_Literal(p.DefaultValue); } catch { }
			return prefix + Api_TypeName(t) + " " + p.Name + def;
		}

		private static string Api_Literal(object v)
		{
			if (v == null) return "null";
			if (v is bool) return (bool)v ? "true" : "false";
			if (v is string) return "\"" + v + "\"";
			try { return Convert.ToString(v, CultureInfo.InvariantCulture); } catch { return "?"; }
		}

		private static string Api_MethodSignature(MethodBase m, Type returnType)
		{
			string ret = returnType == null ? null : Api_TypeName(returnType);
			string name = m is ConstructorInfo ? m.DeclaringType.Name : m.Name;
			string generics = "";
			var mi = m as MethodInfo;
			if (mi != null && mi.IsGenericMethod)
				generics = "<" + string.Join(", ", mi.GetGenericArguments().Select(Api_TypeName)) + ">";
			string parms = string.Join(", ", m.GetParameters().Select(Api_ParamText));
			return Api_MethodModifiers(m) + " " + (ret == null ? "" : ret + " ") + name + generics + "(" + parms + ")";
		}

		private static string Api_PropertySignature(PropertyInfo p, MethodInfo get, MethodInfo set)
		{
			var main = get ?? set;
			string acc = main == null ? "public" : Api_Accessibility(main);
			bool isStatic = main != null && main.IsStatic;
			string accessors = "{ ";
			if (get != null) accessors += (Api_Accessibility(get) != acc ? Api_Accessibility(get) + " " : "") + "get; ";
			if (set != null) accessors += (Api_Accessibility(set) != acc ? Api_Accessibility(set) + " " : "") + "set; ";
			accessors += "}";
			return acc + (isStatic ? " static" : "") + " " + Api_TypeName(p.PropertyType) + " " + p.Name + " " + accessors;
		}

		private static string Api_FieldSignature(FieldInfo f)
		{
			var parts = new List<string> { f.IsPublic ? "public" : f.IsFamilyOrAssembly ? "protected internal" : f.IsFamily ? "protected" : "internal" };
			if (f.IsStatic) parts.Add("static");
			if (f.IsLiteral) parts.Add("const");
			else if (f.IsInitOnly) parts.Add("readonly");
			parts.Add(Api_TypeName(f.FieldType));
			parts.Add(f.Name);
			return string.Join(" ", parts);
		}

		private static string Api_EventSignature(EventInfo e)
		{
			var add = e.GetAddMethod(true);
			string acc = add == null ? "public" : Api_Accessibility(add);
			return acc + (add != null && add.IsStatic ? " static" : "") + " event " + Api_TypeName(e.EventHandlerType) + " " + e.Name;
		}

		// ── friendly type names (int, not Int32; List<T>, not List`1) ──────────────
		private static readonly Dictionary<Type, string> Api_Aliases = new Dictionary<Type, string>
		{
			{ typeof(void), "void" }, { typeof(object), "object" }, { typeof(string), "string" },
			{ typeof(bool), "bool" }, { typeof(byte), "byte" }, { typeof(sbyte), "sbyte" },
			{ typeof(short), "short" }, { typeof(ushort), "ushort" }, { typeof(int), "int" },
			{ typeof(uint), "uint" }, { typeof(long), "long" }, { typeof(ulong), "ulong" },
			{ typeof(float), "float" }, { typeof(double), "double" }, { typeof(decimal), "decimal" },
			{ typeof(char), "char" },
		};

		private static string Api_TypeName(Type t)
		{
			if (t == null) return "?";
			if (t.IsByRef) return Api_TypeName(t.GetElementType());
			string alias;
			if (Api_Aliases.TryGetValue(t, out alias)) return alias;
			if (t.IsArray) return Api_TypeName(t.GetElementType()) + "[]";
			var nullable = Nullable.GetUnderlyingType(t);
			if (nullable != null) return Api_TypeName(nullable) + "?";
			if (t.IsGenericType)
			{
				string name = t.Name;
				int tick = name.IndexOf('`');
				if (tick >= 0) name = name.Substring(0, tick);
				return name + "<" + string.Join(", ", t.GetGenericArguments().Select(Api_TypeName)) + ">";
			}
			return t.Name;
		}
	}
}
