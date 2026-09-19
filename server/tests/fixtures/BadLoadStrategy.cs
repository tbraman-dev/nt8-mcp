// TEST FIXTURE — input to nt_check only. NEVER install this file into
// Documents\NinjaTrader 8\bin\Custom: NT8 compiles and loads the whole tree regardless, and this
// name does not need to be rejected to prove the point below.
//
// Observed on NinjaTrader 8.1.8.2: this is NOT
// rejected at load. An unknown BacktestCommissionTemplate is accepted SILENTLY and NinjaTrader
// charges the DEFAULT commission template instead — there is no exception, and the read-back
// cannot catch it either. The AddOn now answers 400 for an unknown template name at
// POST /backtest, closing the gap at the boundary this repo controls, before NinjaTrader ever
// sees the bad value.
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
    // THE GAP CASE. Compiles clean offline (nt_check sees no error). NT8's own load/instantiate
    // step does NOT reject this (see the note above): the
    // commission template name does not exist, nothing outside NinjaTrader can know which
    // templates exist, and NT8 silently substitutes its default template instead of throwing.
    public class BadLoadStrategy : Strategy
    {
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "BadLoadStrategy";
                // Unknown value: NT8 accepts it silently and charges the default template
                // instead. /backtest now rejects this name itself.
                BacktestCommissionTemplate = "DoesNotExist";
            }
        }
    }
}
