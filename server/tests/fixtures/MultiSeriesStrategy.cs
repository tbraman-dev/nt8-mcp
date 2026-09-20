// TEST FIXTURE — input to nt_check only. NEVER install this file into
// Documents\NinjaTrader 8\bin\Custom: NT8 compiles the whole tree, and a fixture in it
// (see BadCompileStrategy.cs) breaks every script the user has.
//
// A minimal multi-series strategy (one AddDataSeries call, on top of the primary series),
// stock names only. Its point is BarsArray.Length > 1 after Configure — the shape
// addon/NT8Bridge.Backtest.cs's Backtest_VerifyRan checks for the fillResolution "High"
// refusal (see NOTES.md "Backtests"): 'High' Order Fill Resolution is only available for
// single-series strategies.
// Places no orders: nt_check only ever compiles this file, so it must carry no live-trading
// risk if it is ever copied into bin\Custom on its own and enabled on an account.
#region Using declarations
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class MultiSeriesStrategy : Strategy
    {
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "MultiSeriesStrategy";
                Calculate = Calculate.OnBarClose;
            }
            else if (State == State.Configure)
            {
                // The second series is what makes this multi-series: BarsArray.Length becomes 2.
                AddDataSeries(BarsPeriodType.Day, 1);
            }
        }

        protected override void OnBarUpdate()
        {
            if (BarsInProgress != 0) return;
            if (CurrentBar < 20) return;
            if (Close[0] > Close[1]) Print("up");
        }
    }
}
