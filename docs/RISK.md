# Risk — the consolidated rule table

Everything here is pre-registered. That is the point: in a drawdown there is no
discretionary decision to make, because every decision was made in advance, in
writing, and encoded in `strategy_trend/risk_supervisor.py` and
`strategy_trend/kill_rules.py`.

The supervisor runs every 60 s. The drift monitor runs every 60 min. The status
watch runs every 60 min and at every rebalance.

| Rule | Trigger | Action | Clears |
|---|---|---|---|
| Caps | gross > 2.5 E, net > 1.5 E or single > 0.25 E after fills or price moves | reduce the excess within 15 min | automatically |
| Margin amber | margin ratio ≥ 20 % | block risk-increasing orders | automatically |
| Margin red | margin ratio ≥ 35 % | reduce every position by 25 % per 5 min until amber | automatically |
| Survivable downtime | projected liquidation under a 40 % shock within 12 h | block any rebalance that would breach it; reduce net | automatically |
| Governor | drawdown ≥ 12 % / ≥ 20 % | `g` → 0.5 / 0.25, with an immediate proportional cut | dd < 15 % / < 8 % |
| Hard halt | drawdown ≥ 25 % (or ≥ 1.5 × backtest max DD, whichever is lower) | flatten everything, `HALTED_RISK` | operator, with a reason string |
| Daily loss | day net P&L < −6 % of equity | block risk-increasing orders until the next rebalance | automatically |
| ADL quantile | a short position reaches quantile ≥ 4 | reduce that position by 25 % | automatically |
| Liquidation / ADL event | a position changed with no order of ours | reconcile, `CRITICAL` | operator acknowledgement |
| Delisting / status | symbol status leaves `TRADING` | close the position immediately (60 s taker escalation) | automatically |
| Illiquid symbol | 3 consecutive days failing to reach tolerance | exclude until the next universe refresh, `WARN` | next refresh |
| Rebalance failure | 3 consecutive days below 50 % completion | `CRITICAL`, block until acknowledged | operator acknowledgement |
| Reconciliation | any break | block risk-increasing; `CRITICAL` after 60 min | automatically on resolve |
| Tracking error | bounds breached for 14 days | block risk-increasing orders | operator, with a reason |
| Backtest 5th percentile | rolling 3-month live P&L below the bootstrap p05 | block risk-increasing, `WARN`, post-mortem | operator, with a reason |
| Safe mode | exchange unreachable, stream down, clock drift, permission change | risk-reducing orders only; alert every 15 min | automatically |

## What "blocked" means

A block is a named entry in `engine_state.blocks_json`. While any block is
present the engine may send **only risk-reducing orders**. It never declines to
reduce: `Controls.may_reduce_risk()` returns `True` unconditionally, by
construction, because the one thing worse than a bad position is a bad position
you have disabled yourself from closing.

`Pause` is an operator block on rebalances only. Risk actions continue.

## Why 25 % is the hard halt

A 20–30 % maximum drawdown is *inside* the expected distribution for this
strategy. A 25 % halt will therefore trigger in a bad-but-normal year and stop a
strategy that was not broken. That is deliberate: for a solo operator, the cost
of stopping a working strategy is recoverable and the cost of not stopping a
broken one is not. Restarting is an explicit operator action with a written
reason, which is exactly the moment at which the evidence should be re-examined.

## Sizing is where risk is actually controlled

The rules above are the backstop. The primary control is that exposure is bounded
*before an order exists*: `size_targets` applies the single, net and gross caps in
that order and a property test asserts all three hold on every output. The
supervisor re-checks after fills because prices move between rebalances — not
because the sizing function is trusted to be optional.
