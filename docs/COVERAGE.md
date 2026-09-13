# PRD coverage — an audit, not a claim

**Audited commit:** `97c3311` (2026-09-13 00:25:02 UTC), plus ~26 uncommitted working-tree
files — those files were committed as `a7c76f5` while the second pass below was running, so
**the content audited here is `a7c76f5`**, not a dirty `97c3311`.
**Audited by:** a separate read-only pass over the PRD, the source and the tests, then a second
adversarial pass over that audit — no code was changed by either.

**Verification runs used as evidence (all offline, on this machine):**

| Check | Command | Result |
|---|---|---|
| Suite | `.venv/bin/python -m pytest tests -q` | **1357 tests, 0 failures, 0 errors, 0 skipped, 186 s** (junit timestamp 2026-09-13T00:40:52Z) |
| Criterion map | `grep -rhoP "def test_us_t[0-9]+_ac[0-9]+" tests` | **82 / 82** acceptance criteria have at least one test named for them |
| Lint | `.venv/bin/python -m ruff check .` | clean |
| Types | `.venv/bin/python -m mypy` | **31 errors in 8 files** |
| Coverage, PRD §14 modules (`signals` `riskmodel` `portfolio` `rebalance` `universe` `backtest_trend`) | `pytest tests --cov=aegis.signals … ` | **96 %**, lowest single file 92 % — above the 85 % floor |
| Archive reachability | `curl https://data.binance.vision/…` | **blocked** — `CONNECT tunnel failed, 403` |

> **Caveat on all of the above.** Throughout this audit another build session was
> committing to this repository — HEAD moved from `4a9cbbe` to `97c3311` (nine commits) while
> the audit ran, and two intermediate full-suite runs failed on tests that were mid-edit at the
> time (`tests/api/test_deps.py::test_read_only_database_refuses_to_migrate`,
> `tests/integration/test_acceptance.py::test_us_t17_ac4_…`); both pass at the audited commit.
> An earlier coverage measurement was corrupted by source files changing during the run. Every
> number in this document was re-taken at or after `97c3311`, but an operator should re-run the
> table above on a quiet tree before relying on it.

> **Second pass, same commit.** This document was afterwards re-checked line by line against the
> code and the tests at `97c3311` **plus the working-tree files the first pass audited**
> (committed as `a7c76f5` partway through, with no change to the content measured — the only
> dirty file afterwards is this document): the suite is green (1357 collected, exit 0), `ruff` is clean, `mypy` still reports 31
> errors in 8 files, `npx tsc -b --noEmit` in `ui/` is clean, and `data.binance.vision` is still
> refused by the proxy (`CONNECT tunnel failed, response 403`). The §14 coverage floor was re-measured too: **96 %** over the six packages, lowest file 92 % (`backtest_trend/archive.py`, `simulator.py`, `tracking.py`). The 82 rows below were confirmed
> against the PRD's own per-story counts as `tools/self_report.py` hard-codes them
> (4/4/4/5/3/4/2/4/4/6/4/5/4/4/3/6/4/8/4 = 82) — every criterion appears exactly once. Every
> `(N)` in the Proof column was re-derived from the suite. What changed:
>
> * **US-T12 AC 2 and AC 4** — the first draft said the 5-minute cadences were unasserted and
>   that the engine cut on every 60 s tick. Both were made false by `c51ab48`, which landed
>   mid-audit: `runner.py::_risk_path` rate-limits the red ladder and the ADL check, and three
>   runner-level tests prove it. Corrected.
> * **"What is NOT done" item 6** — the first draft said Locked Decision 1 was "not enforced
>   anywhere" and that `set_leverage`/`set_margin_type` had no caller. `ad88430`, also mid-audit,
>   gave them one. Two of the five keys are now applied and tested; three remain operator
>   prerequisites. Corrected.
> * **US-T15 AC 2** — 35 documented metric names, not 36 (35 + 11 extra = 46).
> * **US-T12 AC 1** (7 named tests, not 6), **US-T16 AC 1** (19, not 12), **US-T16 AC 5** (9, not
>   8), **US-T18 AC 8** (3, not 2).
> * **US-T16 AC 1 and AC 3, US-T19 AC 2** — gaps the first pass missed, now stated in the rows
>   and as **D6** and **D7**.
> * **D2** — seven of the nine `executor.py` mypy errors are `yield from` artefacts; the other
>   two are something else.
>
> No status in the scoreboard changed: 63 / 18 / 0 / 1 still holds.

---

## Summary — what an operator should do with this

The strategy arithmetic is in good shape and is genuinely proved. Every canonical vector in
PRD Appendix C (C.1 signal, C.2 volatility, C.3 sizing, C.4 metrics, C.5 governor, C.6
hysteresis) is reproduced by a test to the PRD's own tolerance; the three exposure caps are
applied in the specified order inside `size_targets` with a machine-checked post-condition; the
kill rules, the drawdown governor, the funding overlay, the rebalance planner and the execution
slicer all have dense, adversarial tests. 63 of the 82 acceptance criteria are implemented and
proved, 18 are implemented with a materially weaker proof than the criterion asks for, and 1 is
not implemented.

**But the bot has never seen a real number.** The build sandbox blocks
`data.binance.vision`, so the point-in-time backtest over 2021→now has never been run, and
**the P0 gate has therefore not been evaluated at all** — every backtest test in this
repository runs on a synthetic market that was generated with trends in it. The paper soak,
the P1 8-week / 40-rebalance clock and the P2 12-week clock are calendar time and cannot be
compressed. The `carry` sleeve referenced throughout the PRD does not exist in this
repository, so "runs beside CARRY on one free host" is provisioned but not demonstrated. The
React dashboard has no automated test beyond a TypeScript compile.

**Recommended action:** do not treat this as an evaluated strategy. Take it to a host with
network access, run `--fetch-archive` and the 2021→now backtest, and read the P0 verdict that
`ops/phases.py` produces. If P0 passes, run the real 7-day paper soak, then the 8-week P1.
Nothing in this repository substitutes for those three steps, and nothing in this repository
claims to.

**Scoreboard**

| Status | Criteria |
|---|---|
| IMPLEMENTED + TESTED | 63 / 82 |
| PARTIAL | 18 / 82 |
| IMPLEMENTED, UNTESTED | 0 / 82 |
| NOT IMPLEMENTED | 1 / 82 |

Note that `tools/self_report.py` reports **PASS 82 / 82**. That tool measures only "is there a
passing test whose name contains this criterion id", which is a weaker question than "does that
test assert what the PRD asks for". Where this document disagrees with `SELF-REPORT.md`, this
document opened the test.

---

## EPIC A — Foundation and data

> **How to read the Proof column.** Unless a row says otherwise, a `(N)` is the number of test *functions named for that criterion* — what `grep -c "def test_us_tNN_acM" tests/` returns — not the number of tests in the file, and not the collected count, which is higher because parametrised functions expand. A few rows instead cite a whole file's function count and say so (`test_startup.py` (16 tests), `test_paper.py` (50 tests), `test_archive.py` (19)). Every count in this document was re-derived on the second pass.

| Criterion | Status | Code | Proof | Note |
|---|---|---|---|---|
| **T01.1** engine starts `--strategy trend --mode paper\|demo\|live` on the shared layer, own `trend.db` | IMPLEMENTED + TESTED | `src/engine/cli.py`, `src/engine/__main__.py`, `src/aegis/storage/migrations/001_shared.sql` + `002_trend.sql` | `tests/integration/test_acceptance.py::test_us_t01_ac1_the_engine_starts_on_the_shared_layer_in_every_mode`; `tests/integration/test_cli.py::test_us_t01_ac1_the_documented_invocation_parses` | `--strategy carry` parses but the engine refuses it — CARRY is not in this repository |
| **T01.2** every shared row carries `strategy='TREND'`; no shared query returns cross-strategy data | IMPLEMENTED + TESTED | `src/aegis/storage/repositories.py` (`_Repo` binds `strategy`) | `tests/storage/test_isolation.py` (4 tests) — reflective sweep over every repository's zero-argument readers, >40 reads, asserting the foreign symbol never appears; plus `tests/api/test_empty_and_unknown.py::test_us_t01_ac2_a_run_id_from_another_sleeve_is_not_served` | The sweep can only call readers with no required arguments; `for_rebalance(id)`-style readers are not swept |
| **T01.3** shared reconciliation / ledger / snapshots / metrics / phase gates / controls / heartbeat / backups / Telegram work for TREND with configuration only | **PARTIAL** | `src/aegis/accounting/*`, `src/aegis/analytics/engine.py`, `src/aegis/ops/*` | `tests/integration/test_acceptance.py::test_us_t01_ac3_the_shared_services_are_strategy_agnostic` builds a CARRY-tagged `Context` over the same schema and drives ledger, snapshots, reconcile, metrics, phase gates, controls and heartbeat through it | The test's "second strategy" is a synthetic context, not the CARRY sleeve, which does not exist here. Backups and Telegram are not in this test (covered separately by T19.1/T19.4 and `tests/ops/test_telegram.py`) |
| **T01.4** `LIVE_CONFIRM`, key-permission checks, sub-account name assertion | IMPLEMENTED + TESTED | `src/engine/startup.py` | `tests/strategy_trend/test_startup.py` (16 tests), incl. `test_us_t01_ac4_the_key_must_belong_to_the_configured_sub_account` | Withdrawal permission that cannot be *confirmed absent* is treated as present — correct direction |
| **T02.1** `select_universe` pure; stable-peg exclusion, 400-day gate, forced BTC/ETH, tie-break by name | IMPLEMENTED + TESTED | `src/aegis/universe/select.py` | `tests/universe/test_select.py` + `test_universe_purity.py` (26 tests) | |
| **T02.2** `universe_history` per month with ranking value and inclusion flag; dashboard entrants/leavers with reasons | IMPLEMENTED + TESTED | `src/aegis/universe/service.py`, `src/aegis/api/routes/universe.py` | `tests/universe/test_service.py` (10, incl. `test_us_t02_ac2_entrants_and_leavers_are_alerted_with_reasons`); `tests/api/test_pages_seeded.py::test_us_t18_ac6_universe_page_shows_entrants_leavers_and_illiquid` | |
| **T02.3** symbols leaving the universe are flattened at the next rebalance | IMPLEMENTED + TESTED | `src/aegis/universe/service.py`, `src/aegis/rebalance/planner.py` | `tests/universe/test_service.py` `us_t02_ac3` (4); `tests/rebalance/test_planner.py::test_us_t09_ac2_symbol_out_of_universe_always_trades_to_zero` | |
| **T02.4** backtest universe is point-in-time; a 2025 listing never appears in a 2022 universe | IMPLEMENTED + TESTED | `src/aegis/backtest_trend/universe_builder.py`, `archive.py` | `tests/universe/test_select.py::test_us_t02_ac4_symbol_listed_in_2025_never_appears_in_a_2022_universe`; `tests/backtest_trend/test_universe_builder.py::test_us_t02_ac4_a_symbol_listed_later_never_appears_earlier` | Exercised against a stubbed archive fetcher only — see Known limitations |
| **T03.1** ≥400-bar backfill; closed bar at 00:02 with retry to 00:04; missing bar defers + `WARN` | IMPLEMENTED + TESTED | `src/aegis/bars/service.py` | `tests/bars/test_service.py` `us_t03_ac1` (10), incl. `…ensure_day_stops_at_the_deadline_and_alerts_bar_missing` | |
| **T03.2** `daily_bars` with `source`/`filled`; gaps forward-filled for signals only, never for P&L | IMPLEMENTED + TESTED | `src/aegis/bars/service.py`, `002_trend.sql` | `tests/bars/test_service.py` `us_t03_ac2` (6), incl. `…gap_is_forward_filled_flagged_and_excluded_from_realised_bars` | |
| **T03.3** realised funding and `fundingIntervalHours` backfilled and maintained | IMPLEMENTED + TESTED | `src/aegis/bars/service.py::sync_funding` | `tests/bars/test_service.py::test_us_t03_ac3_sync_funding_is_idempotent`, `…resumes_from_the_stored_cursor` | Thin (2 tests). "Exactly as CARRY US-03 AC 3–4" cannot be checked — the CARRY PRD and sleeve are not in this repository |
| **T03.4** predicted funding read at rebalance start and stored with the targets | IMPLEMENTED + TESTED | `src/aegis/bars/service.py::predicted_funding`; `targets.funding_ann` | `tests/bars/test_service.py::test_us_t03_ac4_predicted_funding_annualises_with_the_symbol_interval`, `…prefers_the_stored_interval`; storage proved by `test_us_t07_ac2_the_funding_haircut_is_recorded_per_symbol` | |

## EPIC B — Signals and portfolio construction

| Criterion | Status | Code | Proof | Note |
|---|---|---|---|---|
| **T04.1** `compute_signal` returns `x,y,z,u` per pair and the clipped signal; Appendix C.1 to 1e-6 | IMPLEMENTED + TESTED | `src/aegis/signals/engine.py` | `tests/signals/test_signal_engine.py::test_us_t04_ac1_appendix_c1_intermediates_reproduced`, `…_signal_reproduced`; vectors in `tests/fixtures/appendix_c.py` | Fixture values match the PRD table exactly, including the (4,12) pair's "largest z, smallest u" property |
| **T04.2** EMA seeding, `ddof=1`, windows and the 0.89 normaliser are configuration-driven | IMPLEMENTED + TESTED | `src/aegis/core/config.py::SignalConfig` | `tests/signals/test_signal_engine.py` `us_t04_ac2` (12) — one test per default, each asserting the output moves | This is the anti-hard-coding guard the PRD asks for, and it is real |
| **T04.3** property tests: drift → ±1, flat → \|signal\|<0.05, spike shrinks the fastest \|u\| | IMPLEMENTED + TESTED | `src/aegis/signals/engine.py::_response` | `tests/signals/test_signal_properties.py` + `test_signal_engine.py` (12) | |
| **T04.4** `signal_snapshots` written daily per symbol with all intermediates and the bar timestamp | IMPLEMENTED + TESTED | `signal_snapshot_row()`; `signal_snapshots` table; `SignalRepo.save_many` | `tests/signals/test_signal_engine.py` `us_t04_ac4` (3), incl. `…snapshot_row_keys_are_real_signal_snapshots_columns` | The *daily write* is exercised through the runner in `tests/integration/test_paper_soak.py`, not by a test named for this AC |
| **T04.5** the signal function has no access to positions, P&L or the clock | IMPLEMENTED + TESTED | `src/aegis/signals/engine.py` | `tests/signals/test_signal_purity.py` (4) — import-graph check, no `datetime.now`, signature check, frozen results | Strong: it checks the import graph, not just the signature |
| **T05.1** `ewma_vol` half-life 10 × √365, floor 30 %, cap 300 %; Appendix C.2 | IMPLEMENTED + TESTED | `src/aegis/riskmodel/estimators.py` | `tests/riskmodel/test_estimators.py` `us_t05_ac1` (7), incl. C.2 variance to 1e-12 relative and both clamps | |
| **T05.2** `ewma_cov` half-life 20 annualised; missing pairs use the average pairwise correlation | IMPLEMENTED + TESTED | `estimators.py`; fallback also in `portfolio/sizing.py::_covariance` | `tests/riskmodel/test_estimators.py` `us_t05_ac2` (11), incl. a symbol with 40 days of history | |
| **T05.3** estimates stored daily in `risk_model_snapshots` (vols, corr JSON, avg corr) | IMPLEMENTED + TESTED | `002_trend.sql`; `RiskModelRepo.save` | `tests/riskmodel/test_estimators.py` `us_t05_ac3` (5) | |
| **T06.1** `size_targets` reproduces Appendix C.3 to 1e-6 including the `s_max` clip | IMPLEMENTED + TESTED | `src/aegis/portfolio/sizing.py` | `tests/portfolio/test_sizing.py` `us_t06_ac1` (7) — intermediates, raw, targets, gross/net, and the *unclipped* ratio 18.174779 vs the clip at 3.0 | |
| **T06.2** cap tests: single only, net only, gross only, all three; exact post-cap notionals and order | IMPLEMENTED + TESTED | `sizing.py::_apply_single_cap/_apply_net_cap/_apply_gross_cap`, `CAP_ORDER` | `tests/portfolio/test_sizing.py` `us_t06_ac2` (13), plus a Hypothesis invariant test `test_us_t06_invariant_8_all_three_caps_hold_on_every_returned_book` | The net cap is re-applied after the funding haircut and the zeroing floor, and the code asserts the post-condition on its own output |
| **T06.3** targets below `min_notional` or 0.1 % of E become zero | IMPLEMENTED + TESTED | `sizing.py` (floor step) | `tests/portfolio/test_sizing.py` `us_t06_ac3` (4) | |
| **T06.4** `targets` rows persisted per rebalance with every intermediate | IMPLEMENTED + TESTED | `002_trend.sql::targets`; `TargetsRepo.save` | `tests/portfolio/test_sizing.py` `us_t06_ac4` (3) for the values; `tests/integration/test_acceptance.py::test_us_t07_ac2_…` and `tests/api/test_pages_seeded.py` (`caps_applied` round-trip) for the row | |
| **T07.1** funding overlay as a pure function; +35 % halves, +25 % does not, −35 % short halves, 4 h annualisation | IMPLEMENTED + TESTED | `src/aegis/portfolio/funding_overlay.py` | `tests/portfolio/test_funding_overlay.py` (9), incl. `…four_hour_interval_annualises_by_2190` and the exact-threshold boundary | |
| **T07.2** haircut recorded per symbol in `targets` and shown in the dashboard | IMPLEMENTED + TESTED | `targets.funding_haircut`; `src/aegis/api/routes/signals.py` | `tests/integration/test_acceptance.py::test_us_t07_ac2_the_funding_haircut_is_recorded_per_symbol`; `tests/api/test_pages_seeded.py::test_us_t18_ac2_signals_row_carries_u_k_target_and_funding_haircut` | |
| **T08.1** `governor(dd, g)` implements the 5.7 table; the C.5 walk | IMPLEMENTED + TESTED | `src/aegis/portfolio/governor.py` | `tests/portfolio/test_governor.py::test_us_t08_ac1_appendix_c5_sequence_reproduces_exactly` + a per-step parametrised walk + inclusive/exclusive boundary tests | |
| **T08.2** downward transitions cut immediately via the risk-cut executor (60 s escalation); upward apply at the next rebalance | **PARTIAL** | `src/aegis/strategy_trend/runner.py::_update_governor` → `_risk_cut` → `RebalanceExecutor.reduce_by` (uses `exec.risk_escalate_s`) | The two tests *named* for this AC (`test_us_t08_ac2_is_downward_flags_the_immediate_cuts`, `…every_c5_downward_step_is_flagged`) only exercise the pure `is_downward` predicate. The end-to-end downward cut is asserted **incidentally** by `tests/integration/test_acceptance.py::test_us_t17_ac4_the_governor_alert_carries_the_appendix_d_body`, which drives a 12.3 % drawdown through the runner and checks the reduce-only order count and the halving of gross | Nothing asserts the *upward* half ("applies at the next rebalance"), and nothing asserts the 60 s escalation on the governor path specifically |
| **T08.3** peak equity from the time-weighted equity path; transfers neither create nor erase drawdowns | IMPLEMENTED + TESTED | `src/aegis/accounting/snapshots.py`, `governor.py::drawdown` | `tests/portfolio/test_governor.py` + `tests/accounting/test_snapshots.py` `us_t08_ac3` (14), incl. mid-period deposit and withdrawal | |
| **T08.4** `governor_state` history stored and charted | IMPLEMENTED + TESTED | `002_trend.sql::governor_state`; `api/routes/positions.py` | `tests/integration/test_acceptance.py::test_us_t08_ac4_governor_history_is_stored_for_the_chart`; `tests/api/test_pages_seeded.py::test_us_t18_ac3_…` | |

## EPIC C — Rebalance and execution

| Criterion | Status | Code | Proof | Note |
|---|---|---|---|---|
| **T09.1** targets and deltas computed at 00:05 against **reconciled** positions, never local state | IMPLEMENTED + TESTED | `runner.py::_rebalance`, `rebalance/planner.py` | `tests/integration/test_acceptance.py::test_us_t09_ac1_deltas_are_computed_against_exchange_positions_not_local_state` — plants a phantom 999-unit local position and asserts the plan records `current_qty == 0` | A good test: it fails if the engine ever trusts its own table |
| **T09.2** hysteresis rule 5.8, all four Appendix C.6 cases | IMPLEMENTED + TESTED | `src/aegis/portfolio/hysteresis.py` | `tests/portfolio/test_hysteresis.py` + `tests/rebalance/test_planner.py` `us_t09_ac2` (10) | |
| **T09.3** deltas ordered risk-reducing first; the order list persisted before any order is sent | IMPLEMENTED + TESTED | `planner.py`, `executor.py::execute` (writes plan + `cursor`, then flips to `running`) | `tests/rebalance/test_planner.py::test_us_t09_ac3_risk_reducing_first_by_current_notional_descending`; `tests/rebalance/test_executor.py` | |
| **T09.4** a restart during REBALANCING resumes from the persisted list | **PARTIAL** | `executor.py::resume`, `rebalances.cursor` | `tests/integration/test_restart.py` and `tests/rebalance/test_executor.py` `us_t09_ac4`; `tests/integration/test_paper_soak.py::test_the_soak_survives_a_process_kill_during_a_rebalance_window` | The "kill" is a discarded runner object plus a fresh one resuming from the database — not an OS process kill, and not in a real soak. The persistence contract is proved; process-level durability is not |
| **T10.1** per-slice lifecycle (placed → re-pegged n → filled \| escalated \| cancelled) stored | IMPLEMENTED + TESTED | `executor.py::_run_slice`, `slices` table | `tests/rebalance/test_executor.py` `us_t10_ac1` (4), incl. re-peg on best-price move, post-only rejection re-peg, IOC escalation after `escalate_s` | |
| **T10.2** reduce-only on every reducing/closing order; a rejection is raised, never a flip | IMPLEMENTED + TESTED | `executor.py::_slice_rejected` (records and abandons; never retries without the flag) | `tests/rebalance/test_executor.py`, `test_planner.py`, `tests/gateway/test_fake.py`, `test_paper.py` — 11 tests | |
| **T10.3** 25× clip → ≤30 evenly spaced slices ≤30 min; slices adapt to fills | IMPLEMENTED + TESTED | `planner.py::clip_notional/slice_count`, `executor.py::_plan_worker/_remaining_qty` | `tests/rebalance/test_planner.py::test_us_t10_ac3_order_of_25_clips_produces_25_slices`, `…capped_at_max_slices`; `tests/rebalance/test_executor.py::test_us_t10_ac3_twenty_five_slices_spread_evenly_over_the_twap_window`, `…slices_adapt_to_fills_and_never_over_trade`, `…position_already_past_the_target_stops_the_symbol` | Both halves — deriving the count from the clip and honouring it — are covered |
| **T10.4** slippage bps vs decision mid and maker/taker per fill; rebalance summary fields | IMPLEMENTED + TESTED | `executor.py::_record_fills`, `rebalances` columns | `tests/rebalance/test_executor.py::test_us_t10_ac4_fills_carry_the_decision_mid_and_maker_flag`, `…summary_records_notional_fees_slippage_maker_ratio_and_completion` | |
| **T10.5** at 01:00 all working orders cancelled; residuals logged with `window_end` | IMPLEMENTED + TESTED | `executor.py` (`REASON_WINDOW_END`, `ALERT_WINDOW_END`) | `tests/rebalance/test_executor.py::test_us_t10_ac5_window_end_cancels_orders_and_logs_residuals`, `…a_position_we_failed_to_close_is_a_residual_however_small` | |
| **T10.6** paper fills follow the shared paper model with the TREND slippage table | IMPLEMENTED + TESTED | `src/aegis/gateway/paper.py` | `tests/gateway/test_paper.py` (50 tests) — `test_taker_buy_pays_exactly_the_symbol_slippage_above_the_ask`, `…default_slippage_applies_to_symbols_outside_the_table` (2/2/6 bps), plus the two `us_t10_ac6` tests | |
| **T11.1** hourly drift check; >20 % of \|target\| → `WARN`; position change without our own order → `CRITICAL` + reconcile | IMPLEMENTED + TESTED | `src/aegis/rebalance/drift.py`; `runner.py::_risk_path` | `tests/rebalance/test_drift.py` (9) + `tests/accounting/test_reconcile.py` — incl. suppression inside the rebalance window and "explained by our own fill" | |
| **T11.2** 5.10 status watch; a `SETTLING` symbol is closed within 5 minutes in the paper soak | **PARTIAL** | `src/aegis/universe/status_watch.py`; `runner.py` (`risk_cut:delisting`) | `tests/universe/test_status_watch.py` `us_t11_ac2` (10); `tests/integration/test_paper_soak.py::test_the_soak_closes_a_symbol_that_leaves_trading_status` | Closure on the detecting tick is proved. The **5-minute bound is not measured**, and the watch cadence is `universe.status_watch_minutes` (hourly), so worst-case detection latency is ~60 minutes, not 5 |
| **T11.3** a changed `fundingIntervalHours` updates the annualisation before the next overlay and logs `INFO` | IMPLEMENTED + TESTED | `status_watch.py::check_funding_intervals` | `tests/universe/test_status_watch.py` `us_t11_ac3` (5) | |
| **T11.4** `illiquid` symbols excluded from sizing until the next refresh, listed in the dashboard | IMPLEMENTED + TESTED | `universe/service.py::current_symbols`, `illiquid_flags`, `api/routes/universe.py` | `tests/universe/test_service.py::test_us_t11_ac4_current_symbols_excludes_illiquid_flagged`, `…an_expired_flag_lets_the_symbol_back` | `all_symbols()` deliberately still returns flagged symbols so a held position cannot be hidden |

## EPIC D — Risk

| Criterion | Status | Code | Proof | Note |
|---|---|---|---|---|
| **T12.1** every 60 s: gross, net, largest, margin ratio, available; green / amber / red | IMPLEMENTED + TESTED | `src/aegis/strategy_trend/risk_supervisor.py::check`; `scheduler.py::SUPERVISOR` | `tests/strategy_trend/test_risk_supervisor.py` `us_t12_ac1` (7) — one per cap and both margin bands | |
| **T12.2** amber-on-cap → reduce the excess within 15 min; red → 25 % per 5 min | IMPLEMENTED + TESTED | `risk_supervisor.py::reductions`; `runner.py::_risk_path` — a cap breach is trimmed on the tick that sees it (inside 15 min), the red ladder is rate-limited to `risk.red_reduce_interval_s` (300 s) | Amounts: `tests/strategy_trend/test_risk_supervisor.py` `us_t12_ac2` (7) — exact trim fractions for single, gross and net breaches, and 25 % on red. Cadence: `…::test_the_red_margin_ladder_reduces_25_pct_per_five_minutes_not_per_tick` and `…::test_a_cap_breach_is_trimmed_on_the_very_next_tick` drive the real `TrendRunner` over four 60 s ticks and count the cuts | Both halves are asserted. The cadence tests are not named for this AC, so `tools/self_report.py` does not see them; the rate limit lives in the engine state, so a restart cannot reset it into firing immediately |
| **T12.3** survivable-downtime rule; reports the survivable move and blocks breaching rebalances | IMPLEMENTED + TESTED | `risk_supervisor.py::survivable_move/survives_downtime/would_breach_downtime_rule` | `tests/strategy_trend/test_risk_supervisor.py` `us_t12_ac3` (6), incl. the PRD's "book at net 1.5 E" case | Honest test: at the net cap the rule *cannot* fire, and the test says so. Note `risk.max_expected_downtime_h` (12) never enters the arithmetic — the shock is applied instantaneously; the hours are documentation |
| **T12.4** ADL quantile ≥ 4 on a short → reduce 25 % | IMPLEMENTED + TESTED | `risk_supervisor.py::adl_reductions` | `tests/strategy_trend/test_risk_supervisor.py::test_us_t12_ac4_short_at_adl_quantile_4_is_reduced_25_pct`, `…longs_and_low_quantiles_are_left_alone`; the 5-minute cadence by `…::test_adl_is_checked_on_its_own_five_minute_cadence` (runner-level, not named for this AC) | |
| **T12.5** BNB fee balance check | IMPLEMENTED + TESTED | `risk_supervisor.py::check_bnb_balance` | `tests/strategy_trend/test_risk_supervisor.py::test_us_t12_ac5_bnb_balance_below_the_floor_alerts` | One test |
| **T13.1** six pre-registered kill rules with the specified responses | IMPLEMENTED + TESTED | `src/aegis/strategy_trend/kill_rules.py` | `tests/strategy_trend/test_kill_rules.py` `us_t13_ac1` (7) — hard halt at 25 % DD *and* the 1.5× backtest-max-DD tightening, daily loss, bootstrap p05, tracking error, rebalance failure, reconciliation break | |
| **T13.2** safe-mode triggers (exchange unreachable, stream down, clock drift, permission change) with reduce-only | **PARTIAL** | `runner.py::tick` (catches `GatewayError` → `machine.enter_safe_mode`), `machine.py` | `tests/integration/test_acceptance.py::test_us_t13_ac2_an_unreachable_exchange_enters_safe_mode_with_reductions_only`, `…safe_mode_clears_when_the_exchange_returns`; `tests/ops/test_controls.py` | Only the **exchange-unreachable** trigger exists. Clock drift is checked at startup only (`engine/startup.py::check_clock`) and never re-checked while running; a key-permission change is never re-checked at runtime; there is no market-data stream to lose (Appendix B: "stream not required"), which excuses one of the four but not the other two |
| **T13.3** `HALTED_RISK` clears only with an audited operator reason | IMPLEMENTED + TESTED | `ops/controls.py`, `strategy_trend/machine.py`, `api/routes/controls.py` | `tests/ops/test_controls.py::test_us_t13_ac3_clear_halt_is_the_only_exit_from_halted_risk`, `…refuses_an_empty_reason`; `tests/api/test_readonly_and_controls.py`; `tests/strategy_trend/test_machine.py` | |
| **T13.4** chaos suite forces **every** rule and asserts the exact response and alert | **PARTIAL** | — | `tests/strategy_trend/test_kill_rules.py::test_us_t13_ac4_the_chaos_suite_can_force_every_rule_at_once` — forces all six simultaneously and asserts each fires, one halts, all block risk-increasing | Per-rule responses are covered by the seven `us_t13_ac1` tests, so the coverage is real. What does not exist is fault injection across the *safe-mode* triggers or the ops layer — a "chaos suite" in the CARRY sense |

## EPIC E — Accounting and analytics

| Criterion | Status | Code | Proof | Note |
|---|---|---|---|---|
| **T14.1** `symbol_pnl_daily` per symbol per day with price / funding / fees / slippage / side / avg notional / signal | IMPLEMENTED + TESTED | `src/aegis/accounting/attribution.py`; `symbol_pnl_daily` | `tests/accounting/test_attribution.py` `us_t14_ac1` (5), incl. side from average signed notional and signed slippage vs decision mid | |
| **T14.2** roll-ups by symbol, side, month; contribution share | IMPLEMENTED + TESTED | `attribution.py` | `tests/accounting/test_attribution.py::test_us_t14_ac2_rollups_by_symbol_side_and_month`, `…contribution_share_identifies_concentration` | Feeds the P0 "no symbol > 50 %" test |
| **T14.3** identity: Σ symbol P&L + non-position ledger = equity change − transfers, to 0.01 USDT/day | IMPLEMENTED + TESTED | `attribution.py` (`ATTRIBUTION_IDENTITY_BREAK` alert) | `tests/accounting/test_attribution.py` `us_t14_ac3` (5) — holds on a synthetic day, survives a deposit, and **breaks loudly** when income is missing or an unbooked fill appears | The negative cases are what make this evidence |
| **T14.4** trade records (open → flat) with holding days, P&L, MAE, entry/exit signal | IMPLEMENTED + TESTED | `attribution.py`; `trades` table | `tests/accounting/test_attribution.py` `us_t14_ac4` (4), incl. idempotent re-run | |
| **T15.1** Section 10 metrics as pure functions with the Appendix C.4 vectors, stored in `metrics` | IMPLEMENTED + TESTED | `src/aegis/analytics/trend_metrics.py`, `analytics/engine.py` | `tests/analytics/test_trend_metrics.py::test_us_t15_ac1_appendix_c4_turnover_vector`, `…beta_and_correlation_vector` (β 0.3098 ± 0.02, ρ 0.891 ± 0.02) | |
| **T15.2** the full TREND metric additions for the standard periods | IMPLEMENTED + TESTED | `analytics/engine.py::METRIC_NAMES` (35 documented + 11 extra = 46), `trend_metrics.py` | `tests/analytics/test_trend_metrics.py` + `test_engine.py` (81 tests) | Every Section 10 row has an implementation, including the regime table, governor time-in-state, execution alpha and MAE quantiles. `corr_carry` is implemented and returns `None` — there is no CARRY return series to correlate against |
| **T15.3** shared metrics produced by the shared engine unchanged | IMPLEMENTED + TESTED | `src/aegis/analytics/metrics.py` | `tests/analytics/test_metrics.py` + `test_engine.py` (45 tests), incl. Sharpe with standard error, `n_obs`, information ratio vs the backtest reference | |
| **T16.1** point-in-time universe builder from the Binance public archive, delisted symbols included | IMPLEMENTED + TESTED **(stub only)** | `src/aegis/backtest_trend/archive.py`, `universe_builder.py` | `tests/backtest_trend/test_archive.py` (19 — URL layout, header rows, missing months, caching, corrupt member, and the REST cross-check) and `test_universe_builder.py::test_us_t16_ac1_a_delisted_symbol_is_in_the_universes_of_the_months_it_existed` | Never executed against `data.binance.vision` — see Known limitations. `archive.py::verify_against_rest` (PRD 11.1) compares the archive field by field against the venue's own klines and refuses to continue on a mismatch, but it is wired into the networked `--fetch-archive` pass only, so it too has never run for real |
| **T16.2** daily simulator reusing `signals`/`riskmodel`/`portfolio`; next-open fills, conservative costs, funding, governor, monthly refresh | IMPLEMENTED + TESTED **(synthetic data)** | `src/aegis/backtest_trend/simulator.py` | `tests/backtest_trend/test_simulator.py` (16), incl. `test_no_look_ahead_a_run_ending_earlier_is_a_prefix` and `test_equity_reconciles_with_cash_and_marked_positions` (PRD §14.2) | The market is `tests/backtest_trend/conftest.py::make_market`, a seeded synthetic series **built with flipping trends in it**. `test_us_t16_ac2_momentum_is_profitable_on_a_trending_market` is therefore a check that the machine captures a trend, not evidence of an edge — the test's own docstring says so |
| **T16.3** outputs: metrics, equity path, attribution, parameter snapshot, checksums, git commit, robustness, bootstrap | IMPLEMENTED + TESTED | `backtest_trend/runner.py`, `bootstrap.py`, `robustness.py`; `backtest_runs`/`robustness_reports`/`bootstrap_distribution` | `tests/integration/test_acceptance.py::test_us_t16_ac3_a_run_stores_metrics_robustness_bootstrap_and_a_manifest`; `tests/backtest_trend/test_runner.py` (8); checksums by `tests/backtest_trend/test_simulator.py::test_manifest_carries_checksums_and_the_parameter_snapshot` and `…test_a_changed_bar_changes_the_manifest_checksum` | One element of the list is **unasserted**: `runner.py::git_commit()` runs on every backtest, but every `git_commit` in the suite is a value a test passed *in* to a fixture — nothing reads back what a real `BacktestRunner` stored, so a `git_commit()` that silently returned `"unknown"` on the deployment host would not fail a test |
| **T16.4** walk-forward is a robustness check only; defaults in the top half on ≥70 % of windows | IMPLEMENTED + TESTED | `backtest_trend/walkforward.py` | `tests/backtest_trend/test_robustness_walkforward.py` (12), incl. the 3×3×3 grid, 12 m/6 m rolling windows, and `test_us_t16_ac4_gate_needs_the_default_in_the_top_half_on_70_pct_of_windows` | The gate *logic* is proved. No walk-forward has ever been run on real data, so the ≥70 % result itself is unknown |
| **T16.5** the reference run is re-executed daily; TREND tracking bounds | IMPLEMENTED + TESTED | `src/aegis/backtest_trend/tracking.py`; `tracking` table; `kill_rules._tracking_error` | `tests/integration/test_acceptance.py::test_us_t16_ac5_the_reference_is_re_run_daily_and_compared`; `tests/integration/test_tracking.py` (9) | Bounds (0.7 / 3 % / 2.0 / 1.5) are asserted against config |
| **T16.6** backtest completes in <15 min for 2021→now (16 symbols) and is deterministic | **PARTIAL** | `simulator.py` | Determinism: `test_us_t16_ac6_the_run_is_deterministic` (identical `run_id`, manifest, metrics, equity) and `…a_different_parameter_changes_the_run_id`. Budget: `test_us_t16_ac6_completes_well_inside_the_15_minute_budget` asserts <120 s and <200 ms/day on a ~470-day, 20-symbol synthetic run | The determinism half is solid. The 15-minute budget is **extrapolated** from a shorter synthetic run on this build machine, not measured over 2021→now on the free VM |

## EPIC F — Operations, dashboard, deployment

| Criterion | Status | Code | Proof | Note |
|---|---|---|---|---|
| **T17.1** daily report at 00:10 with the full field list; second message at 01:05 | IMPLEMENTED + TESTED | `src/aegis/ops/reports.py::Reporter.daily/rebalance_summary/due_reports` | `tests/ops/test_reports.py::test_us_t17_ac1_daily_is_due_at_00_10_and_the_summary_at_01_05`, `…rebalance_summary_matches_appendix_d`; `tests/integration/test_acceptance.py::test_us_t17_ac1_the_daily_report_covers_the_closed_day_and_is_sent` | Also tested: an empty database renders `n/a` and never crashes; a report missed during downtime is still due later |
| **T17.2** weekly/monthly metric, contribution, regime, tracking, turnover and infra tables | IMPLEMENTED + TESTED | `reports.py::weekly/monthly/_period_body` | `tests/integration/test_acceptance.py::test_us_t17_ac2_weekly_and_monthly_reports_are_rendered_and_stored`; `tests/ops/test_reports.py::test_weekly_report_carries_the_metric_contribution_and_tracking_tables` | |
| **T17.3** event alerts for the full event list, with severities and repeat rules | **PARTIAL** | `src/aegis/ops/alerts.py` (persist-then-deliver, 15-minute per-code repeat suppression); alert codes across `executor.py`, `risk_supervisor.py`, `kill_rules.py`, `status_watch.py`, `bars/service.py`, `drift.py` | `tests/ops/test_reports.py::test_us_t17_ac3_governor_alert_matches_appendix_d`; individual codes are asserted in each module's own tests | Every event on the PRD's list has a code emitted somewhere, and repeat suppression is implemented — but **no test enumerates the required event list and its severities**. The single test named for this AC covers one alert |
| **T17.4** message formats per Appendix D; bodies stored in `reports` | IMPLEMENTED + TESTED | `reports.py` (Appendix D's `·`, `−`, `×` glyphs are deliberate) | `tests/ops/test_reports.py::test_us_t17_ac4_daily_body_matches_appendix_d_line_by_line`, `…daily_body_is_stored_in_reports`; `tests/integration/test_acceptance.py::test_us_t17_ac4_the_governor_alert_carries_the_appendix_d_body` (regex over every number in the body) | |
| **T18.1** Overview page: curves, P&L by component and side, KPI tiles | **PARTIAL** | `src/aegis/api/routes/overview.py`; `ui/src/pages/Overview.tsx` | `tests/api/test_pages_seeded.py::test_us_t18_ac1_overview_carries_curve_components_and_kpi_tiles` | The **API contract** is proved. The React page is only type-checked (`npx tsc -b --noEmit`, clean); nothing renders it |
| **T18.2** Signals page: per-symbol signal, three `u_k`, vol, target vs current, funding + haircut; 90-day history | **PARTIAL** | `api/routes/signals.py`; `ui/src/pages/Signals.tsx` | `tests/api/test_pages_seeded.py::test_us_t18_ac2_signals_row_carries_u_k_target_and_funding_haircut`, `…signal_history_returns_the_requested_window` | as above |
| **T18.3** Positions & risk: positions, ADL, caps utilisation, margin, survivable move, governor history, kill-rule board | **PARTIAL** | `api/routes/positions.py`; `kill_rules.status_board`; `ui/src/pages/Positions.tsx` | `tests/api/test_pages_seeded.py::test_us_t18_ac3_positions_page_shows_book_caps_margin_and_kill_rules` | as above |
| **T18.4** Rebalances: history table with drill-down to slices and fills | **PARTIAL** | `api/routes/rebalances.py`; `ui/src/pages/Rebalances.tsx` | `tests/api/test_pages_seeded.py::test_us_t18_ac4_rebalance_list_and_drilldown`, `…unknown_rebalance_is_404` | as above |
| **T18.5** Attribution: per-symbol/per-side tables, regime table, contribution shares | **PARTIAL** | `api/routes/attribution.py`; `ui/src/pages/Attribution.tsx` | `tests/api/test_pages_seeded.py::test_us_t18_ac5_attribution_covers_periods_shares_and_regimes` | as above |
| **T18.6** Metrics / Operations / Backtest / Controls with the TREND additions | **PARTIAL** | `api/routes/{metrics,operations,backtest,controls,universe}.py` | `tests/api/test_pages_seeded.py` `us_t18_ac6` (5) — metrics with `n_obs`+SE, operations uptime/recon/alerts/infra, backtest robustness+walk-forward+bootstrap, universe entrants/leavers/illiquid | as above. The Operations page exposes `rss_mb`/`cpu_pct` fields that nothing ever populates — see T19.2 |
| **T18.7** strategy selector and a combined "All sleeves" overview | **PARTIAL** | `api/routes/overview.py` (`/api/strategies`, combined overview); `ui/src/pages/AllSleeves.tsx` | `tests/api/test_pages_seeded.py::test_us_t18_ac7_strategies_lists_only_deployed_sleeves`, `…combined_overview_shows_each_sleeve_side_by_side`; `tests/api/test_empty_and_unknown.py::test_us_t18_ac7_a_sleeve_that_is_not_deployed_is_absent_not_a_500` | Exercised with a synthetic second database; there is no real CARRY sleeve to show |
| **T18.8** all pages load from stored data in <1 s on the free VM | **PARTIAL** | — | `tests/api/test_performance.py` `us_t18_ac8` (3, the first parametrised over every page) — every page and the rebalance drill-down answer inside the budget on a database seeded with a year of history | Measures the **API response** in-process on the build machine. Not a page load, not a browser, not the free VM |
| **T19.1** compose gains a `trend` service, second Litestream replica path, second Healthchecks check, `restart: always` | **PARTIAL** | `docker-compose.yml`, `ops/docker/Dockerfile` | `tests/integration/test_acceptance.py::test_us_t19_ac1_compose_runs_both_sleeves_with_their_own_db_check_and_replica`, `…the_metrics_jobs_are_staggered_for_the_single_core` (00:05 vs 01:10) | The `carry` service is a **declared placeholder**: it sits behind a compose profile so `docker compose up -d` never starts it, and the engine refuses `--strategy carry`. The file is right for the day CARRY lands; the host does not today run two sleeves |
| **T19.2** combined footprint RSS <1 GB and CPU <20 % of one core, **measured and shown in the Operations page** | **NOT IMPLEMENTED** | `api/routes/operations.py` reads metrics named `rss_mb` and `cpu_pct` | `tests/integration/test_acceptance.py::test_us_t19_ac2_the_combined_footprint_is_bounded_below_the_free_shape` sums the **declared** `mem_limit` in `docker-compose.yml` (448 + 320 + 192 = 960 MB) and asserts each service's `cpus` is `<= 1.0` | Nothing anywhere in `src/` ever writes an `rss_mb` or `cpu_pct` metric (`grep -rn "rss_mb\|cpu_pct" src/` returns only the reader and a config default). The page will always show blanks. Declared container limits are a budget, not a measurement — and the CPU half of the test is weaker still: the declared `cpus` are 0.8 + 0.8 + 0.3 = 1.9 cores and the assertion is `<= 1.0` **per service**, i.e. five whole cores' worth of headroom against a criterion of "< 20 % of one core". That assertion cannot fail |
| **T19.3** cost inventory lists both strategies; the daily report states the total; `infra.monthly_cost_eur` split by config | **PARTIAL** | `core/config.py::infra`, `reports.py::_infra_mtd` | `tests/integration/test_acceptance.py::test_us_t19_ac3_the_cost_inventory_is_zero_and_split_by_config`; the daily report's infra line is asserted in `tests/ops/test_reports.py::test_us_t17_ac4_daily_body_matches_appendix_d_line_by_line` | The config default is 0 and the compose file does not override it. "Lists both strategies" cannot be shown without CARRY |
| **T19.4** restore test extended to both databases | IMPLEMENTED + TESTED | `ops/backup.py`, `engine/cli.py::cmd_verify_restore` | `tests/integration/test_acceptance.py::test_us_t19_ac4_the_restore_drill_covers_both_databases`; `tests/ops/test_backup.py::test_us_t19_ac4_each_database_is_restored_from_its_own_replica`, `…a_host_without_litestream_reports_unavailable_rather_than_failing`; `tests/integration/test_cli.py::test_us_t19_ac4_verify_restore_compares_row_counts` | |

---

## What is NOT done

Plainly, with what each would take.

1. **The real backtest has never been run, so P0 is unevaluated.** This is the single most
   important gap. The sandbox blocks `data.binance.vision` (verified: the proxy returns
   `CONNECT tunnel failed, 403`). Every backtest number in this repository comes from a seeded
   synthetic market. *To close:* on a host with network access, run
   `python -m engine --strategy trend --mode backtest --start 2021-01-01 --fetch-archive`, then
   the offline replay, then read the P0 verdict from `ops/phases.py`. Budget a few hours,
   mostly download time. The result may well be a documented **fail**; that is a legitimate
   outcome and the PRD's M3 exit criterion allows for it.

2. **No host-resource measurement (US-T19 AC 2).** The `rss_mb` / `cpu_pct` metrics the
   Operations page reads are never produced by anything. *To close:* add a periodic sampler
   (`resource.getrusage` or `/proc/self/status` plus a CPU-time delta) writing those two metric
   names on the shared metrics schedule; a handful of lines plus a test.

3. **The `carry` sleeve does not exist.** `docker-compose.yml` provisions it, the schema and
   repositories carry `strategy='CARRY'`, and the API serves a second database — but
   `--strategy carry` is refused by the engine and no CARRY strategy module exists. Everything
   the PRD says about "beside CARRY" (T01.3, T18.7, T19.1, T19.3, the `corr_carry` metric) is
   therefore structurally ready and behaviourally unproven. *To close:* build CARRY, per its
   own PRD.

4. **No test renders a dashboard page.** All eight US-T18 criteria are proved at the API
   boundary; `ui/src/**` is only type-checked. A JSON contract can be right while the page that
   consumes it is blank or wrong. *To close:* add Vitest + Testing Library render tests against
   the same seeded fixtures the API tests use — the `ui/src/__check__/*.json` fixtures that used
   to serve this purpose were deleted during this build.

5. **Runtime safe-mode triggers are incomplete (US-T13 AC 2).** Only "exchange unreachable" is
   implemented. Clock drift is checked once, at startup; a key-permission change is never
   re-checked. *To close:* move `check_clock` and `check_key_permissions` onto the supervisor
   schedule and route their failures into `machine.enter_safe_mode`.

6. **Locked Decision 1 is enforced for two of its five keys.** `config/trend.yaml` declares
   `leverage: 5`, `margin_type: CROSSED`, `position_mode_one_way: true`,
   `multi_assets_mode: false`, `bnb_fee_discount: true`. The first two are applied to every
   universe symbol by `runner.py::apply_account_settings` — at `start()` and again after a
   universe refresh, since entrants have never been configured — and only when
   `mode.sends_real_orders`; a symbol the venue refuses raises `ACCOUNT_SETTINGS` and is skipped
   rather than being fatal. Three tests in `tests/integration/test_acceptance.py`
   (`test_locked_decision_1_leverage_and_margin_are_applied_to_every_symbol`,
   `test_paper_mode_configures_nothing_at_the_venue`,
   `test_a_venue_that_refuses_a_setting_warns_and_carries_on`) prove it. The remaining three —
   one-way position mode, multi-assets off, BNB fee discount — are account-wide, are not exposed
   by the futures API in a form the engine can set safely, and are **neither set nor asserted**:
   `docs/RUNBOOK.md` step 2 makes them a manual operator task, so an operator who forgets to
   switch off multi-assets mode, or leaves hedge mode on, gets no warning from the engine.
   *To close:* read those three back at startup and refuse to start on a mismatch (assert, do
   not silently set — silently setting would be the software changing risk parameters on its
   own). Note this bullet said "not enforced anywhere" in the first draft of this audit; that
   was true of `4a9cbbe` and was made false by `ad88430`, which landed mid-audit.

7. **The upward half of the governor's timing is unasserted (US-T08 AC 2).** Nothing tests that
   a restore from 0.5 to 1.0 waits for the next rebalance rather than acting immediately.
   *To close:* one runner-level test.

8. **No test enumerates the event-alert catalogue (US-T17 AC 3).** *To close:* a table-driven
   test over the PRD's list of events asserting code, severity and repeat rule.

9. **`mypy` does not pass:** 31 errors in 8 files. *To close:* see the defects section.

---

## Known limitations

These are properties of the build environment or of the PRD's own design, not defects.

* **`data.binance.vision` is denied by the build sandbox.** The point-in-time archive loader is
  tested only against a stubbed fetcher (real file layout, header rows, missing months, disk
  caching, corrupt members). The 2021→now backtest has never been run and **P0 has not been
  evaluated against real data**. `docs/BACKTEST.md` states this in the same terms.

* **The paper soak and the phase clocks are calendar time.** PRD §14.1 asks for 7 days and ≥7
  rebalances with a process kill and a forced `SETTLING`. `tests/integration/test_paper_soak.py`
  runs the real `TrendRunner` over seven *simulated* days with both drills, and its own
  docstring says what that cannot prove: wall-clock endurance, a memory leak, a leaked file
  handle. Likewise P1 (8 weeks, ≥40 rebalances), P2 (12 weeks) and P3 cannot start until the bot
  is deployed and time passes. The gate arithmetic in `ops/phases.py` is tested; the clocks are
  not runnable here.

* **Realised volatility will sit below the 20 % target.** With the PRD's own parameters the
  `s_max = 3.0` clip binds on a diversified book, so the portfolio runs at roughly 0.67× the
  target — inside the PRD's own 0.5×–1.5× adherence band, and visible in Appendix C.3 itself
  (`s = min(0.20/0.011004, 3.0)`). Specified behaviour, and the first thing that looks wrong on
  the dashboard. `docs/BACKTEST.md` documents it. Note that the "0.67×" (≈13 % realised) is a
  reading off the same seeded synthetic market as everything else in this repository; that the
  clip binds is arithmetic and certain, but the ratio it lands at on real data is not known.

* **The `rebalance_midday` robustness variant is an approximation.** Daily bars cannot express a
  12:00 UTC rebalance, so the variant fills at the same day's close instead of the next open.
  Documented in `docs/BACKTEST.md`; it is the nearest available proxy for time-of-day robustness.

* **Predicted funding is proxied by the last realised rate** in the backtest (PRD 11.1 allows
  this explicitly, as CARRY does).

* **`risk.max_expected_downtime_h` is inert.** The survivability rule applies the 40 % shock
  instantaneously; the 12-hour horizon appears in config and in alert text but never in the
  arithmetic. Given the caps, the rule can only fire on a book that has already drifted outside
  them — the code says so in its own docstring.

* **The status watch runs hourly.** US-T11 AC 2's "within 5 minutes" is satisfied only in the
  sense that closure happens on the detecting tick; detection itself can lag a status change by
  up to `universe.status_watch_minutes` (60).

* **The cross-strategy isolation sweep only calls zero-argument readers.** A repository method
  that takes an id and forgets its `WHERE strategy = ?` would not be caught by
  `test_us_t01_ac2_no_repository_read_returns_the_other_strategy`.

* **This repository was being modified while it was audited.** See the caveat at the top.

---

## Defects found during the audit

Recorded, not fixed — this was an audit.

**D1 — `rss_mb` / `cpu_pct` are read but never written.** `src/aegis/api/routes/operations.py`
lines 181–182 read `metrics.value("rss_mb", "7d")` and `metrics.value("cpu_pct", "7d")`. Neither
name appears in `analytics/engine.py::METRIC_NAMES` and nothing in `src/` writes them. The
Operations page's footprint fields are permanently `null`, and US-T19 AC 2's "measured" is
unmet. Severity: low for trading safety, but it is the one criterion that is simply absent.

**D2 — `mypy` reports 31 errors in 8 files.** Distribution at the audited commit:
`backtest_trend/simulator.py` 10, `rebalance/executor.py` 9, `analytics/engine.py` 5,
`backtest_trend/walkforward.py` 2, `backtest_trend/tracking.py` 2, and one each in
`storage/repositories.py`, `ops/phases.py`, `gateway/fake.py`, `analytics/trend_metrics.py`.
Most are `Optional` leakage: `simulator.py` threads a `dict[str, float | None]` of marks into
functions annotated `Mapping[str, float]` and then multiplies by it
(`simulator.py:231–232: Unsupported operand types for * ("float" and "None")`), which would be a
real `TypeError` if a mark were ever missing on a day with a position — the tests never produce
that input. `backtest_trend/tracking.py:154–155` types `previous` as `str` and then subscripts it
with `"breach_days"`. Of the nine in `executor.py`, seven (lines 440, 451, 536, 540, 563, 590,
602) are `yield from` generator-return artefacts and two (968–969) are `int()` calls over JSON
plan rows whose `type: ignore` codes do not match the error mypy actually emits
(`call-overload`, not `arg-type`); all nine appear benign. Recommend fixing the `simulator.py`
and `tracking.py` groups before trusting the
backtest on real (gappy) archive data, where missing marks are exactly what will happen.

**D3 — US-T08 AC 2's named tests do not test the criterion.**
`test_us_t08_ac2_is_downward_flags_the_immediate_cuts` and
`test_us_t08_ac2_every_c5_downward_step_is_flagged` exercise the pure `is_downward` predicate
only. The behaviour the AC describes — an immediate proportional reduction through the risk-cut
executor on the way down, deferral to the next rebalance on the way up — lives in
`runner.py::_update_governor` and is asserted only by a test named for a different criterion
(`test_us_t17_ac4_the_governor_alert_carries_the_appendix_d_body`), and only in the downward
direction. `tools/self_report.py` reports this criterion as PASS.

**D4 — the compose `carry` service passes its own test while being inert.**
`test_us_t19_ac1_compose_runs_both_sleeves_with_their_own_db_check_and_replica` asserts
`restart: always`, distinct DB paths, replica paths, heartbeat URLs and `--strategy carry` in the
command — all true — but does not notice `profiles: ["carry"]`, which means the service never
starts, or that the engine refuses that strategy. The test name overstates what it proves. The
compose file itself is honest about this in a comment.

**D5 — a transient full-suite failure that did not reproduce.** During one full run,
`tests/api/test_deps.py::test_read_only_database_refuses_to_migrate` failed; it passes in
isolation, passes with `tests/api`, and passes in every subsequent full run. This coincided with
another session editing the tree, so the most likely explanation is a mid-edit source file rather
than an order dependency — and the suite installs no `pytest-randomly` and sets no random seed
(`pyproject.toml` `addopts = "-q --strict-markers"`), so collection order is fixed and a genuine
order dependency would reproduce on **every** run, not one. It is recorded anyway because an
unexplained failure in this suite is worth a second look. Re-run the suite a few times on a quiet
tree to rule it out.

**D6 — the CPU half of the US-T19 AC 2 test cannot fail.**
`test_us_t19_ac2_the_combined_footprint_is_bounded_below_the_free_shape` checks
`services[s]["cpus"] <= 1.0` for each of the three services. The compose file declares 0.8, 0.8
and 0.3, so the assertion holds with roughly five cores of slack against a criterion of "CPU
< 20 % of one core". Same species as D4: a green assertion that carries no information. It should
be deleted along with the memory sum once a real sampler exists (D1).

**D7 — nothing reads back the backtest's `git_commit`.** `backtest_trend/runner.py::git_commit()`
is called on every run and stored, but every `git_commit` in the test suite is a literal a test
handed *to* a fixture (`"abc"`, `"deadbee"`, `"abc123"`). No test runs a `BacktestRunner` and
asserts the stored commit is this repository's. "Which code produced this backtest" is the
question the manifest exists to answer, and on a deployment host where `git` is absent or the
checkout is shallow, a silent `"unknown"` would pass the suite. *To close:* one assertion in
`tests/backtest_trend/test_runner.py::test_the_run_persists_everything_under_one_run_id`.

---

## How to re-run this audit

```bash
cd /home/user/TREND
.venv/bin/python -m pytest tests -q                       # 1357 tests at 97c3311
.venv/bin/python tools/self_report.py                     # writes SELF-REPORT.md (see caveat above)
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
.venv/bin/python -m pytest tests -q --cov=aegis.signals --cov=aegis.riskmodel \
    --cov=aegis.portfolio --cov=aegis.rebalance --cov=aegis.universe \
    --cov=aegis.backtest_trend --cov-report=term-missing   # PRD §14 coverage floor
( cd ui && npx tsc -b --noEmit )                           # the dashboard's only check
```

Do it on a tree nobody else is writing to, and compare `git rev-parse HEAD` before and after.
