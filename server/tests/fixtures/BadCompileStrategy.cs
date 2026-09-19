// TEST FIXTURE — input to nt_check only. NEVER install this file into
// Documents\NinjaTrader 8\bin\Custom: it does not compile, and NT8 compiles the whole tree,
// so this one file would break every script the user has.
//
// Portions of this file are derived from cli-nt-bridge
// (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
// MIT License. The full notice is in NOTICE at the repository root.
#region Using declarations
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    // Fails offline in nt_check with CS0103. The case the offline compiler is meant to catch.
    public class BadCompileStrategy : Strategy
    {
        protected override void OnBarUpdate()
        {
            int x = foo;       // CS0103: name 'foo' does not exist in the current context
            int y = bar;       // CS0103: name 'bar' does not exist in the current context
        }
    }
}
