# Runbook

## Daily rhythm (all times UTC)

| Time | What happens |
|---|---|
| 00:00 | Daily bar closes; funding settles |
| 00:02 | Bars fetched, retried until 00:04 |
| 00:05 | Signals, risk model, targets, plan persisted, rebalance starts |
| 00:10 | Daily report (Telegram) |
| 01:00 | Rebalance window closes; working orders cancelled; residuals logged |
| 01:05 | Rebalance summary (Telegram) |
| 01:10 | Metrics job (staggered clear of CARRY's 00:05 job) |
| every 60 s | Risk supervisor |
| every 60 min | Drift monitor, symbol status and delisting watch |
| 1st of month, 00:05 | Universe refresh; illiquid flags cleared |

## First deployment

1. Create the `trend-01` sub-account. API key: futures **only**, IP-allowlisted,
   **no withdrawal permission**. The startup check fails closed — a key whose
   withdrawal permission cannot be confirmed absent is refused.
2. One-way position mode, cross margin, multi-assets mode OFF, BNB fee discount ON.
3. `cp .env.example .env` and fill it in. `docker compose up -d trend dashboard`.
4. Confirm in the dashboard Operations page: heartbeat green, reconciliation OK,
   backup lag small, universe populated, bars backfilled ≥ 400 days per symbol.

## Moving between phases

Never by feel. `GET /api/trend/backtest` and the Controls page show each gate
criterion with its required and actual value. A gate whose inputs are missing
reports **failed**, not passed. Record the approval (operator + reason) before
changing `phase.current` and the capital.

## When something goes wrong

**Telegram says CRITICAL and the engine halted.** It flattened first and asked
questions second — that is the design. Read the alert context, then the
Positions and Rebalances pages. `HALTED_RISK` clears only with a written reason,
which is stored. Do not clear it before you can write that reason.

**A reconciliation break.** Risk-increasing orders are already blocked. Compare
the Positions page against the exchange. The usual causes are a missed fill (the
next sync books it and the break resolves itself) and a liquidation or ADL (the
break is real and needs acknowledgement).

**Rebalance completion is low for several days.** Look at maker ratio and
slippage first. Persistent failure on one symbol flags it illiquid until the next
refresh. Three consecutive days below 50 % completion blocks trading and needs an
acknowledgement — investigate liquidity or the clip size before clearing it.

**Three losing months.** That is inside the expected distribution. The question
is not "is it down" but "is it behaving like the reference": check the tracking
error panel — daily P&L correlation ≥ 0.7, cumulative difference within ±3 % of
equity, cost ratio ≤ 2, turnover ratio ≤ 1.5. Inside those bounds, a drawdown is
the strategy working as specified. Outside them, something has changed.

**The host died.** The container restores from the Litestream replica on first
start. Verify with the restore drill below before you ever need it.

## Restore drill (run monthly)

```bash
docker compose exec trend litestream restore -o /tmp/verify.db "$AEGIS_BACKUP__LITESTREAM_REPLICA_PATH"
docker compose exec trend python -m engine --strategy trend --verify-restore /tmp/verify.db
```

The verify step compares schema and row counts against the live database and
reports the replication lag. Do the same for `carry.db` — US-T19 AC 4 requires
both.

## Changing a parameter

Every tunable is in `config/trend.yaml` and the engine never writes to it. A
change is a commit, a restart, and a note in the approvals log. It appears in the
parameter snapshot stored with every backtest manifest and every rebalance, so
"what was it running when this happened" is always answerable.

Never tune from live results. Walk-forward analysis exists to show that the fixed
defaults are *robust*, not to pick better ones; nothing in that table may reach
the live configuration (Invariant 7).
