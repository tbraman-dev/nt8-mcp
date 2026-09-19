// TEST FIXTURE — input to nt_check only. NEVER install this file into
// Documents\NinjaTrader 8\bin\Custom: NT8 compiles the whole tree, and a fixture in it
// (see BadCompileStrategy.cs) breaks every script the user has.
//
// Portions of this file are derived from cli-nt-bridge
// (https://github.com/eman007/cli-nt-bridge), Copyright (c) 2026 eman007,
// MIT License. The full notice is in NOTICE at the repository root.
#region Using declarations
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    // Compiles clean offline AND loads clean in NT8. The control case. Places no orders:
    // nt_check only ever compiles this file, so it must carry no live-trading risk if it
    // is ever copied into bin\Custom on its own and enabled on an account.
    public class GoodStrategy : Strategy
    {
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "GoodStrategy";
                Calculate = Calculate.OnBarClose;
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < 20) return;
            if (Close[0] > Close[1]) Print("up");
        }
    }
}
