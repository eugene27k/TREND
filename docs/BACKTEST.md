# Backtest

The backtest is the P0 gate's only evidence, so it is built to be attacked: it
reuses the live strategy code unchanged, it refuses to see the future by
construction rather than by care, and it is reproducible from a manifest.

## What it reuses

`signals/`, `riskmodel/` and `portfolio/` are imported and called directly —
the same functions, with the same `AppConfig` object, that the live engine calls
at 00:05 UTC. A backtest number is therefore evidence about the code that will
trade, not about a sibling of it (US-T16 AC 2).

What the simulator owns is only what is *not* the strategy:

- **the calendar** — which bars exist on day *t*, and nothing after it;
- **execution** — fills at the **next** daily open, charged the taker fee plus
  the slippage table (2 bps BTC/ETH, 6 bps otherwise). That is Locked Decision
  8's deliberately conservative model: live maker fills then show up as
  *execution alpha*, upside against the reference rather than a shortfall;
- **cash and funding** — kept as a literal cash account, so the equity path can
  be recomputed from its own fills and reconciles by construction.

## Why it cannot look ahead

Three structural properties, each with a test:

1. Signals for day *t* are read from a series precomputed over the whole history
   at index *t*. `compute_signal_series` is bar-for-bar identical to calling
   `compute_signal` on every prefix, which its own test asserts to 1e-12.
2. Orders decided from day *t*'s close fill at day *t+1*'s open.
3. The universe for month *m* is built from bars strictly before *m* began, from
   the archive's file inventory — not from today's `exchangeInfo`.

The decisive test is `test_no_look_ahead_a_run_ending_earlier_is_a_prefix`: a run
that stops early must produce exactly the equity path of the longer run's prefix.
If any future information leaked in, the two would diverge.

## Point-in-time data

`data.binance.vision` is the only free source that **retains delisted symbols**,
which is what makes the universe survivorship-free: a symbol that traded in 2022
and was delisted in 2023 still has its 2022 files, so March 2022's universe is
built from what was tradeable then. Historical `exchangeInfo` does not exist, so
status is derived from the file inventory — a symbol with a kline file for the
previous month was trading.

Downloads are cached on disk and never re-fetched, so a re-run is offline and
therefore deterministic.

> **Known environment limitation.** The build sandbox's network policy denies
> `data.binance.vision`, so the archive could not be exercised against the live
> service here and **the real 2021→now backtest has not been run**. The loader
> is tested against a stubbed fetcher covering the real file layout, header rows,
> missing months, caching and corrupt archives. On a host with access, populate
> the cache and run the gate as below; nothing else changes.

## Running it

```bash
# Warm the cache (the only step that needs network), then run offline.
python -m engine --strategy trend --mode backtest --start 2021-01-01 --fetch-archive
python -m engine --strategy trend --mode backtest --start 2021-01-01
```

The `--fetch-archive` pass also **verifies the archive against the venue's own
klines** for the last `backtest.verify_rest_days` (90) days, per PRD 11.1, and
refuses to continue on a mismatch. The archive is a convenience, not an
authority: if it disagrees with the API, the backtest is measuring a market that
did not happen, and the failures that matter — a shifted day boundary, a
rescaled quote volume — silently change which symbols the universe picks. The
offline replay does no network I/O at all, which is what keeps it deterministic.

The run stores, under one `run_id`: the equity path, metrics, per-symbol
contribution, the 13 robustness variants, the walk-forward ranking, the bootstrap
distribution, the manifest with per-symbol data checksums, and the git commit.

## The P0 criteria and where each number comes from

| Criterion | Source |
|---|---|
| net annualised return ≥ 8 % | `metrics.annualised_return` |
| net Sharpe ≥ 0.7 | `metrics.sharpe` |
| max drawdown ≤ 35 % | `metrics.max_drawdown` |
| net P&L positive in ≥ 60 % of calendar years | `p0_evidence.yearly_pnl` |
| no single symbol > 50 % of net P&L | `p0_evidence.max_symbol_contribution` |
| sign of net P&L unchanged in every robustness variant | `robustness_reports` |
| annualised turnover ≤ 30× equity | `metrics.turnover_annualised` |

`ops/phases.py` judges these; the backtester only produces them. Producing
evidence and judging it are separate jobs on purpose, so the gate cannot be
satisfied by the thing it is meant to be judging.

## Robustness (PRD 11.5)

Thirteen variants: two alternative speed sets, ±30 % on each volatility target,
5 %/20 % hysteresis, universe 12 and 20, cost model ×2, governor off, and a
time-of-day variant. The gate is not that a variant is *good* — it is that the
**sign of net P&L does not flip**. If moving the lookbacks one notch turns a
winner into a loser, the result was a property of the parameters, not of the
market.

`governor_off` is reported but is explicitly **not** a gate condition (11.5.6):
it exists to show the governor's contribution.

`rebalance_midday` is an honest approximation. Daily bars cannot express a 12:00
UTC rebalance, so it fills at the same day's close instead of the next open —
the nearest available proxy for "a different time of day", recorded as a
limitation rather than hidden.

## Walk-forward (PRD 11.6)

A 3×3×3 grid (speed set × σ target × hysteresis) over rolling 12-month train /
6-month test windows stepping by 6 months. The defaults must rank in the top half
on ≥ 70 % of test windows.

Nothing fitted, nothing selected: **no value from this table may ever reach the
live configuration** (Invariant 7). The train window carries no information; it
is kept because it defines how much history a live deployment would have had
before the test period, which is what makes that period genuinely out-of-sample.

## Bootstrap (PRD 11.3)

10 000 resamples of the daily returns in 91-day blocks, fixed seed. Blocks rather
than i.i.d. draws, because daily crypto returns cluster: an i.i.d. bootstrap
produces a reassuringly narrow distribution that the live engine then breaches
every other quarter. The 5th percentile of the 3-month P&L is what the
`backtest_p05` kill rule compares live performance against — the difference
between "losing" and "broken".

## A note on realised volatility

With the PRD's own parameters the `s_max = 3.0` clip binds in a diversified book:
16 positions at ~45 % annualised vol and moderate correlation give an ex-ante
portfolio vol around 4 %, so reaching the 20 % target would need a scaling factor
near 4.5. Appendix C.3 shows exactly the same thing — `s = min(0.20/0.011004,
3.0) = 3.0`, noted in the PRD as "(clip binds)".

Realised volatility therefore runs below the 20 % target — around 13 % in
testing, i.e. 0.67×, inside the PRD's own 0.5×–1.5× vol-target-adherence band.
This is the specified behaviour, not a defect: `s_max` is a leverage limiter and
changing it would be a live re-optimisation. It is recorded here because it is
the first thing that looks wrong on the dashboard and is not.
