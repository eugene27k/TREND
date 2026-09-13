"""Acceptance criteria whose proof spans several modules.

Each test here is named for the criterion it proves, so tools/self_report.py can
find it. They are integration tests because these particular criteria are about
the whole engine rather than any one module.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from aegis.core.clock import to_ms
from aegis.core.errors import ExchangeUnreachable
from aegis.core.types import Mode, Strategy
from aegis.ops.reports import Reporter
from aegis.storage.db import open_db
from aegis.storage.repositories import Repositories
from aegis.strategy_trend.runner import TrendRunner

BARS_AT = "2026-09-08T00:02:00Z"
REBALANCE_AT = "2026-09-08T00:05:00Z"
TODAY = date(2026, 9, 8)


@pytest.fixture
def traded(world):
    runner = TrendRunner(world)
    runner.start(world.clock.now_ms())
    world.clock.set(to_ms(BARS_AT))
    runner.tick(world.clock.now_ms())
    world.clock.set(to_ms(REBALANCE_AT))
    runner.tick(world.clock.now_ms())
    return world, runner


# --------------------------------------------------------------------------- #
# US-T01 — shared-layer integration
# --------------------------------------------------------------------------- #


def test_us_t01_ac1_the_engine_starts_on_the_shared_layer_in_every_mode(tmp_path):
    """`python -m engine --strategy trend --mode paper|demo|live`, with its own SQLite file."""
    from engine.cli import build_parser, resolve_config

    for mode in ("paper", "demo", "live"):
        parsed = build_parser().parse_args(
            ["--strategy", "trend", "--mode", mode, "--db", str(tmp_path / f"{mode}.db")]
        )
        cfg = resolve_config(parsed)
        assert cfg.strategy is Strategy.TREND
        assert cfg.mode is Mode(mode)
        assert cfg.storage.db_path.endswith(f"{mode}.db"), "each mode gets its own file"

    # The schema the shared Engine expects is created by the shared migrations.
    db = open_db(tmp_path / "paper.db")
    assert {"ledger", "snapshots", "metrics", "approvals", "engine_state"} <= set(db.tables())
    db.close()


def test_us_t01_ac3_the_shared_services_are_strategy_agnostic(world):
    """Reconciliation, ledger, snapshots, metrics, phase gates, controls, heartbeat and
    Telegram are the shared modules: each must run against CARRY as readily as TREND,
    with no code change — only configuration.

    String-matching for "TREND" would prove nothing (a docstring mentions it), so this
    builds a CARRY context over the same schema and drives each service through it.
    """
    from aegis.accounting.ledger import LedgerService
    from aegis.accounting.reconcile import Reconciler
    from aegis.accounting.snapshots import SnapshotService
    from aegis.analytics.engine import MetricsEngine
    from aegis.core.context import Context
    from aegis.core.types import Phase
    from aegis.ops.alerts import AlertBus
    from aegis.ops.controls import Controls
    from aegis.ops.heartbeat import Heartbeat
    from aegis.ops.phases import PhaseGates

    carry_cfg = world.cfg.model_copy(update={"strategy": Strategy.CARRY})
    carry_repos = Repositories(world.repos.db, Strategy.CARRY)
    carry = Context(
        cfg=carry_cfg,
        clock=world.clock,
        gateway=world.gateway,
        repos=carry_repos,
        alerts=AlertBus(carry_repos.alerts, world.clock, Strategy.CARRY),
    )
    now = world.clock.now_ms()

    assert LedgerService(carry).sync(now) >= 0
    assert SnapshotService(carry).take(now).equity >= 0
    assert Reconciler(carry).run_all(now) is not None
    assert isinstance(MetricsEngine(carry).compute_all(now), list)
    assert PhaseGates(carry).evaluate(Phase.P0_BACKTEST, now).passed in (True, False)
    Controls(carry).pause("operator", "strategy-agnostic check")
    assert Controls(carry).state()["paused"] is True
    Heartbeat(carry).beat(now, True, "check")

    # And every row it just wrote is CARRY's, invisible to TREND (US-T01 AC 2).
    assert carry_repos.snapshots.latest() is not None
    assert bool(carry_repos.state.load()["paused"]) is True
    trend_state = world.repos.state.load()
    assert trend_state is None or not bool(trend_state["paused"])


# --------------------------------------------------------------------------- #
# US-T07 AC 2 / US-T08 AC 4 — recorded, not just computed
# --------------------------------------------------------------------------- #


def test_us_t07_ac2_the_funding_haircut_is_recorded_per_symbol(world):
    """A haircut the dashboard cannot see is a haircut nobody can audit."""
    from aegis.core.types import SymbolTarget, Targets

    targets = Targets(
        (
            SymbolTarget("AAAUSDT", 0.8, 0.5, 1000.0, 500.0, funding_ann=0.35, funding_haircut=0.5),
            SymbolTarget("BBBUSDT", 0.8, 0.5, 1000.0, 1000.0, funding_ann=0.05, funding_haircut=1.0),
        ),
        0.1,
        0.8,
        0.2,
        2.0,
        1.0,
        10_000.0,
    )
    world.repos.targets.save("rb-1", targets)
    rows = {r["symbol"]: r for r in world.repos.targets.for_rebalance("rb-1")}
    assert rows["AAAUSDT"]["funding_haircut"] == 0.5
    assert rows["AAAUSDT"]["funding_ann"] == pytest.approx(0.35)
    assert rows["BBBUSDT"]["funding_haircut"] == 1.0


def test_us_t08_ac4_governor_history_is_stored_for_the_chart(world):
    world.repos.governor.record(to_ms(TODAY), 0.13, 1.0, 0.5, "dd=0.1300", applied=True)
    world.repos.governor.record(to_ms(TODAY) + 86_400_000, 0.07, 0.5, 1.0, "dd=0.0700")
    history = world.repos.governor.history()
    assert [(r["g_before"], r["g_after"]) for r in history] == [(1.0, 0.5), (0.5, 1.0)]
    assert history[0]["applied"] == 1, "a downward cut is applied immediately"
    assert world.repos.governor.current_g() == 1.0


# --------------------------------------------------------------------------- #
# US-T09 AC 1 — targets against reconciled positions
# --------------------------------------------------------------------------- #


def test_us_t09_ac1_deltas_are_computed_against_exchange_positions_not_local_state(world):
    """A missed fill must not become a phantom position."""
    import json

    runner = TrendRunner(world)
    runner.start(world.clock.now_ms())
    world.clock.set(to_ms(BARS_AT))
    runner.tick(world.clock.now_ms())

    # Local state claims a big position the venue does not have.
    symbol = world.repos.universe.symbols(world.repos.universe.latest_month())[0]
    from aegis.core.types import Position

    world.repos.positions.replace_all([Position(symbol, 999.0, 100.0, 100.0)], world.clock.now_ms())

    world.clock.set(to_ms(REBALANCE_AT))
    runner.tick(world.clock.now_ms())

    plan = json.loads(world.repos.rebalances.recent(1)[0]["order_plan_json"])
    entry = next((p for p in plan if p["symbol"] == symbol), None)
    assert entry is not None
    assert entry["current_qty"] == 0.0, "the plan must trust the venue, not the local table"


# --------------------------------------------------------------------------- #
# US-T13 AC 2 — safe mode
# --------------------------------------------------------------------------- #


def test_us_t13_ac2_an_unreachable_exchange_enters_safe_mode_with_reductions_only(world):
    runner = TrendRunner(world)
    runner.start(world.clock.now_ms())
    world.gateway.inject_error("account", ExchangeUnreachable("venue down"))

    world.clock.set(to_ms(BARS_AT))
    report = runner.tick(world.clock.now_ms())

    assert "safe_mode" in report.actions
    assert runner.machine.state.safe_mode
    assert not runner.machine.state.may_increase_risk()
    assert runner.machine.state.may_reduce_risk(), "reductions are never disabled"
    assert any(a["code"] == "SAFE_MODE" for a in world.repos.alerts.recent())


def test_us_t13_ac2_safe_mode_clears_when_the_exchange_returns(world):
    runner = TrendRunner(world)
    runner.start(world.clock.now_ms())
    world.gateway.inject_error("account", ExchangeUnreachable("venue down"))
    world.clock.set(to_ms(BARS_AT))
    runner.tick(world.clock.now_ms())
    assert runner.machine.state.safe_mode

    world.gateway.clear_errors()
    world.clock.set(to_ms("2026-09-08T00:04:00Z"))
    runner.tick(world.clock.now_ms())
    assert not runner.machine.state.safe_mode


# --------------------------------------------------------------------------- #
# US-T16 AC 3 and AC 5
# --------------------------------------------------------------------------- #


def test_us_t16_ac3_a_run_stores_metrics_robustness_bootstrap_and_a_manifest(world, tmp_path):
    from aegis.backtest_trend.runner import BacktestRunner
    from tests.backtest_trend.conftest import make_market

    bars, funding, _ = make_market(seed=11, days=620, n_symbols=6)
    cfg = world.cfg.model_copy(
        update={"backtest": world.cfg.backtest.model_copy(update={"bootstrap_resamples": 300})}
    )
    runner = BacktestRunner(cfg, bars, funding, repo=world.repos.backtest)
    start = min(b.day for b in next(iter(bars.values()))) + timedelta(days=430)
    end = max(b.day for b in next(iter(bars.values())))
    outcome = runner.run(start, end, with_walkforward=False)

    stored = world.repos.backtest.get_run(outcome.result.run_id)
    assert stored is not None
    assert stored["manifest_json"] and stored["metrics_json"]
    assert world.repos.backtest.robustness(outcome.result.run_id)
    assert world.repos.backtest.bootstrap(outcome.result.run_id, "3m")
    assert outcome.p0_evidence["max_symbol_contribution"] is not None


def test_us_t16_ac5_the_reference_is_re_run_daily_and_compared(traded):
    """Without this the tracking-error kill rule could never fire."""
    from aegis.backtest_trend.tracking import TrackingService
    from aegis.core.types import EquityPoint

    world, _runner = traded
    equity, index, peak = 10_000.0, 1.0, 1.0
    for i in range(40):
        day = TODAY - timedelta(days=39 - i)
        previous = equity
        equity += 5.0
        if i:
            index *= equity / previous
        peak = max(peak, index)
        world.repos.equity.upsert(
            day, EquityPoint(to_ms(day), equity, 0.0, 1.0, index), peak_index=peak, drawdown=0.0
        )

    result = TrackingService(world).update(TODAY, world.clock.now_ms())
    assert result is not None
    stored = world.repos.tracking.latest()
    assert stored is not None and stored["day"] == TODAY.isoformat()
    for field in ("corr_30d", "cum_diff_frac", "cost_ratio", "turnover_ratio"):
        assert field in stored
    assert world.cfg.tracking.min_corr == 0.7
    assert world.cfg.tracking.max_cum_diff == 0.03
    assert world.cfg.tracking.max_cost_ratio == 2.0
    assert world.cfg.tracking.max_turnover_ratio == 1.5


# --------------------------------------------------------------------------- #
# US-T17 AC 2 — weekly and monthly tables
# --------------------------------------------------------------------------- #


def test_us_t17_ac2_weekly_and_monthly_reports_are_rendered_and_stored(traded):
    world, _runner = traded
    reporter = Reporter(world)
    now = world.clock.now_ms()

    weekly = reporter.weekly("2026-W37", now)
    monthly = reporter.monthly("2026-09", now)
    assert weekly.startswith("TREND")
    assert monthly.startswith("TREND")
    assert world.repos.reports.get("weekly", "2026-W37") is not None
    assert world.repos.reports.get("monthly", "2026-09") is not None


def test_us_t17_ac4_the_governor_alert_carries_the_appendix_d_body(world):
    """AC 4 is about the *format*: the alert the operator receives is Appendix D's."""
    runner = TrendRunner(world, reporter=Reporter(world))
    runner.start(world.clock.now_ms())
    world.clock.set(to_ms(BARS_AT))
    runner.tick(world.clock.now_ms())
    world.clock.set(to_ms(REBALANCE_AT))
    runner.tick(world.clock.now_ms())
    positions = len(world.gateway.positions())
    assert positions, "the cut needs a book to reduce"

    # Next morning, equity 12.3 % below the peak: g must step 1.0 -> 0.5 (5.7).
    world.gateway.set_account(wallet_balance=0.877 * 10_000.0)
    world.clock.set(to_ms("2026-09-09T00:02:00Z"))
    runner.tick(world.clock.now_ms())

    alert = next(a for a in world.repos.alerts.recent() if a["code"] == "GOVERNOR")
    lines = alert["message"].split("\n")
    dd = float(world.repos.governor.history()[0]["dd"])
    assert lines[0] == "TREND · WARN · GOVERNOR 1.0 → 0.5"
    assert lines[2] == "Restore to 1.0 when DD < 8 %."

    # Every number in the body against an independent source: the drawdown the
    # governor acted on, the fixture's starting equity as the peak, the book it
    # was about to reduce, and a gross that halves because g does.
    shape = re.fullmatch(
        r"Drawdown (?P<dd>[\d.]+) % from peak (?P<peak>[\d ]+\.\d\d)\. Immediate cut:"
        r" gross (?P<before>[\d.]+)× → (?P<after>[\d.]+)×"
        r" \((?P<orders>\d+) reduce-only orders, taker allowed\)\.",
        lines[1],
    )
    assert shape is not None, lines[1]
    assert float(shape["dd"]) == pytest.approx(dd * 100.0, abs=0.05)
    assert 12.0 <= float(shape["dd"]) < 20.0, "the 1.0 -> 0.5 rung is DD >= 12 %"
    assert shape["peak"] == "10 000.00"
    assert float(shape["after"]) == pytest.approx(float(shape["before"]) / 2.0, abs=0.01)
    assert int(shape["orders"]) == positions


def test_us_t17_ac1_the_daily_report_covers_the_closed_day_and_is_sent(world):
    """00:10 covers the day that closed — and a stored body is not a sent one."""
    sent: list[str] = []
    runner = TrendRunner(
        world,
        reporter=Reporter(world),
        report_sender=lambda body: sent.append(body) is None,
    )
    runner.start(world.clock.now_ms())
    world.clock.set(to_ms(BARS_AT))
    runner.tick(world.clock.now_ms())
    world.clock.set(to_ms(REBALANCE_AT))
    runner.tick(world.clock.now_ms())

    world.clock.set(to_ms("2026-09-09T00:10:00Z"))
    report = runner.tick(world.clock.now_ms())

    assert "daily_report:sent" in report.actions
    yesterday = TODAY.isoformat()  # not 2026-09-09, which is ten minutes old
    row = world.repos.reports.get("daily", yesterday)
    assert row is not None, "the closed day is the one reported"
    assert row["delivered"] == 1
    assert sent and sent[0].startswith(f"TREND · daily · {yesterday}")
    # The headline is the day's P&L by component (AC 1) — it must not read n/a
    # merely because the 01:10 metrics job has not run yet.
    assert sent[0].split("\n")[2].startswith("Net P&L day ")
    assert "Net P&L day n/a" not in sent[0]


def test_a_report_the_channel_refused_stays_undelivered(world):
    runner = TrendRunner(world, reporter=Reporter(world), report_sender=lambda body: False)
    runner.start(world.clock.now_ms())
    world.clock.set(to_ms("2026-09-09T00:10:00Z"))
    runner.tick(world.clock.now_ms())

    row = world.repos.reports.get("daily", TODAY.isoformat())
    assert row is not None and row["delivered"] == 0


# --------------------------------------------------------------------------- #
# US-T19 — deployment
# --------------------------------------------------------------------------- #


def _compose() -> dict:
    return yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))


def test_us_t19_ac1_compose_runs_both_sleeves_with_their_own_db_check_and_replica():
    compose = _compose()
    services = compose["services"]
    assert {"trend", "carry", "dashboard"} <= set(services)

    for name, db in (("trend", "trend.db"), ("carry", "carry.db")):
        service = services[name]
        assert service["restart"] == "always"
        env = service["environment"]
        assert env["AEGIS_STORAGE__DB_PATH"].endswith(db)
        assert "AEGIS_BACKUP__LITESTREAM_REPLICA_PATH" in env, "its own replica path"
        assert "AEGIS_HEARTBEAT__URL" in env, "its own Healthchecks check"
        assert f"--strategy {name}" in " ".join(service["command"])

    trend_env = services["trend"]["environment"]
    carry_env = services["carry"]["environment"]
    assert trend_env["AEGIS_STORAGE__DB_PATH"] != carry_env["AEGIS_STORAGE__DB_PATH"]
    assert services["trend"]["volumes"][0] != services["carry"]["volumes"][0]


def test_us_t18_the_dashboard_can_append_the_control_row_it_must_write():
    """The one write the API makes (control_log) needs a writable mount.

    Every page is read through a mode=ro connection, so the single-writer rule
    does not depend on the mount; the operator's Start/Pause/Stop/Flatten row
    does (docs/SERVICES.md, PRD Section 7, US-T13 AC 3).
    """
    dashboard = _compose()["services"]["dashboard"]
    for volume in dashboard["volumes"]:
        assert not str(volume).endswith(":ro"), volume


def test_us_t19_ac2_the_combined_footprint_is_bounded_below_the_free_shape():
    """RSS < 1 GB and CPU < 20 % of one core outside the rebalance window."""
    services = _compose()["services"]
    total_mb = sum(int(str(services[s]["mem_limit"]).rstrip("m")) for s in ("trend", "carry", "dashboard"))
    assert total_mb < 1024, f"{total_mb} MB of declared limits exceeds the 1 GB budget"
    assert all(services[s]["cpus"] <= 1.0 for s in ("trend", "carry", "dashboard"))


def test_us_t19_ac3_the_cost_inventory_is_zero_and_split_by_config(world):
    assert world.cfg.infra.monthly_cost_eur == 0.0
    compose = _compose()
    for service in ("trend", "carry"):
        env = compose["services"][service]["environment"]
        assert "AEGIS_INFRA__MONTHLY_COST_EUR" not in env, (
            "the cost split is a config value, not a container default"
        )


def test_us_t19_ac1_the_metrics_jobs_are_staggered_for_the_single_core():
    """Section 13: CARRY's metrics at 00:05, TREND's pushed to 01:10."""
    services = _compose()["services"]
    assert services["trend"]["environment"]["AEGIS_METRICS__JOB_TIME_UTC"] == "01:10"
    assert services["carry"]["environment"]["AEGIS_METRICS__JOB_TIME_UTC"] == "00:05"


def test_us_t19_ac4_the_restore_drill_covers_both_databases(tmp_path):
    """CARRY US-18 AC 2 extended: verify-restore works per sleeve."""
    from engine.cli import build_parser, cmd_verify_restore, resolve_config

    for name in ("trend", "carry"):
        live, copy = tmp_path / f"{name}.db", tmp_path / f"{name}-restored.db"
        open_db(live).close()
        Repositories(open_db(copy), Strategy.TREND)
        parsed = build_parser().parse_args(
            ["--strategy", "trend", "--config", "config/trend.yaml", "--db", str(live)]
        )
        assert cmd_verify_restore(resolve_config(parsed), str(copy)) == 0


# --------------------------------------------------------------------------- #
# Locked Decision 1 — the account is actually configured
# --------------------------------------------------------------------------- #


def test_locked_decision_1_leverage_and_margin_are_applied_to_every_symbol(world):
    """Leaving the venue's defaults changes the maintenance-margin schedule the
    risk supervisor's survivability estimate is computed against."""
    demo = world.cfg.model_copy(update={"mode": Mode.DEMO})
    ctx = type(world)(
        cfg=demo, clock=world.clock, gateway=world.gateway, repos=world.repos, alerts=world.alerts
    )
    runner = TrendRunner(ctx)
    runner.start(ctx.clock.now_ms())
    ctx.clock.set(to_ms(BARS_AT))
    runner.tick(ctx.clock.now_ms())

    universe = ctx.repos.universe.symbols(ctx.repos.universe.latest_month())
    assert universe
    applied = runner.apply_account_settings(ctx.clock.now_ms())
    assert set(applied) >= set(universe)
    for symbol in universe:
        assert world.gateway.leverage[symbol] == demo.account.leverage == 5
        assert world.gateway.margin_type[symbol] == demo.account.margin_type == "CROSSED"


def test_paper_mode_configures_nothing_at_the_venue(world):
    """There is no venue to configure, and pretending otherwise hides a failure."""
    runner = TrendRunner(world)
    runner.start(world.clock.now_ms())
    assert runner.apply_account_settings(world.clock.now_ms()) == []
    assert world.gateway.leverage == {}


def test_a_venue_that_refuses_a_setting_warns_and_carries_on(world):
    from aegis.core.errors import GatewayError

    demo = world.cfg.model_copy(update={"mode": Mode.DEMO})
    ctx = type(world)(
        cfg=demo, clock=world.clock, gateway=world.gateway, repos=world.repos, alerts=world.alerts
    )
    runner = TrendRunner(ctx)
    runner.start(ctx.clock.now_ms())
    ctx.clock.set(to_ms(BARS_AT))
    runner.tick(ctx.clock.now_ms())

    world.gateway.inject_error("set_leverage", GatewayError("no such symbol"))
    assert runner.apply_account_settings(ctx.clock.now_ms()) == []
    assert any(a["code"] == "ACCOUNT_SETTINGS" for a in ctx.repos.alerts.recent())


# --------------------------------------------------------------------------- #
# The engine must actually CALL its services — not merely contain them
# --------------------------------------------------------------------------- #


def test_us_t16_ac5_the_runner_itself_runs_the_daily_comparison(traded):
    """A service the engine never calls is an unimplemented criterion.

    This caught a real regression: a refactor dropped the TrackingService wiring
    and every test still passed, because they exercised the service directly.
    """
    world, runner = traded
    assert hasattr(runner, "tracking"), "the runner must hold a TrackingService"

    called: list[date] = []
    runner.tracking.update = lambda day, now_ms=0: called.append(day) or None

    world.clock.set(to_ms("2026-09-09T01:10:00Z"))  # the metrics job
    report = runner.tick(world.clock.now_ms())
    assert "metrics" in report.actions
    assert called, "the metrics job must re-run the reference and compare (US-T16 AC 5)"


def test_us_t19_ac2_the_runner_measures_its_own_footprint(traded):
    """'measured and shown in the Operations page' — a container limit is a budget."""
    world, runner = traded
    assert hasattr(runner, "resources"), "the runner must hold a ResourceMonitor"

    world.clock.set(to_ms("2026-09-08T00:20:00Z"))
    runner.tick(world.clock.now_ms())
    world.clock.set(to_ms("2026-09-08T00:22:00Z"))
    runner.tick(world.clock.now_ms())

    latest = world.repos.metrics.latest()
    rss = latest.get("rss_mb:7d")
    assert rss is not None, "no RSS was ever recorded, so the page shows blanks"
    assert rss["value"] > 0
    cpu = latest.get("cpu_pct:7d")
    assert cpu is not None, "a rate needs two samples; the second tick must produce one"
    assert 0.0 <= cpu["value"] <= 100.0 * 64


def test_us_t19_ac2_the_operations_page_shows_the_measurement(traded, tmp_path):
    from fastapi.testclient import TestClient

    from aegis.api.app import create_app

    world, runner = traded
    world.clock.set(to_ms("2026-09-08T00:20:00Z"))
    runner.tick(world.clock.now_ms())
    world.clock.set(to_ms("2026-09-08T00:22:00Z"))
    runner.tick(world.clock.now_ms())

    db_path = tmp_path / "trend.db"
    source = Path("config/trend.yaml").read_text(encoding="utf-8")
    config = tmp_path / "trend.yaml"
    config.write_text(source.replace("db_path: data/trend.db", f"db_path: {db_path}"), encoding="utf-8")
    # Copy the in-memory database out to a file the API can open read-only.
    import sqlite3

    target = sqlite3.connect(db_path)
    world.repos.db._conn.backup(target)  # test plumbing: copy the in-memory DB to a file
    target.close()

    client = TestClient(create_app({"TREND": config}))
    infra = client.get("/api/TREND/operations").json()["infra"]
    assert infra["rss_mb"] is not None and infra["rss_mb"] > 0
    assert infra["max_rss_mb"] == world.cfg.infra.max_rss_mb


def test_us_t18_the_built_dashboard_is_actually_served(tmp_path):
    """The container runs uvicorn alone, so an unmounted SPA means every page 404s.

    This was real: the Dockerfile never built ui/dist and the app mounted no
    static files, so the dashboard existed only under `vite dev`.
    """
    import importlib
    import os
    import shutil

    from fastapi.testclient import TestClient

    dist = Path("ui/dist")
    if not dist.is_dir():
        pytest.skip("ui/dist not built in this environment")

    shutil.copytree(dist, tmp_path / "ui")
    os.environ["AEGIS_DASHBOARD_DIR"] = str(tmp_path / "ui")
    try:
        import aegis.api.app as appmod

        importlib.reload(appmod)
        db_path = tmp_path / "trend.db"
        open_db(db_path).close()
        source = Path("config/trend.yaml").read_text(encoding="utf-8")
        config = tmp_path / "trend.yaml"
        config.write_text(source.replace("db_path: data/trend.db", f"db_path: {db_path}"), encoding="utf-8")
        client = TestClient(appmod.create_app({"TREND": config}))

        for route in ("/", "/overview", "/positions", "/controls"):
            response = client.get(route)
            assert response.status_code == 200, route
            assert response.headers["content-type"].startswith("text/html"), route

        # The API must still win: a catch-all that shadowed /api would be worse
        # than not serving the dashboard at all.
        assert client.get("/api/health").headers["content-type"].startswith("application/json")
        assert client.get("/api/TREND/operations").status_code == 200
    finally:
        os.environ.pop("AEGIS_DASHBOARD_DIR", None)
        import aegis.api.app as appmod

        importlib.reload(appmod)


def test_us_t19_the_image_builds_the_dashboard_and_copies_it_in():
    dockerfile = Path("ops/docker/Dockerfile").read_text(encoding="utf-8")
    assert "npm run build" in dockerfile, "the image must build the dashboard"
    assert "COPY --from=dashboard /ui/dist ./ui" in dockerfile, "and copy it into the runtime"

    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    dashboard = compose["services"]["dashboard"]
    assert dashboard["environment"]["AEGIS_DASHBOARD_DIR"] == "/app/ui"
    assert "--factory" in dashboard["entrypoint"], (
        "aegis.api.app exposes create_app(), not a module-level app"
    )
    # Dead configuration implies a knob that does not exist.
    assert not [k for k in dashboard["environment"] if k.startswith("AEGIS_API__")]
