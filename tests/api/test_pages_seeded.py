"""US-T18: every page answers with the documented shape from stored data."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.conftest import GET_ROUTES, REBALANCE_ID


def test_us_t18_ac7_strategies_lists_only_deployed_sleeves(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/strategies").json()
    assert [s["strategy"] for s in body["strategies"]] == ["TREND"]
    assert body["strategies"][0]["mode"] == "paper"


def test_us_t18_every_page_returns_200_on_a_seeded_database(seeded_client: TestClient) -> None:
    for path in GET_ROUTES:
        assert seeded_client.get(path).status_code == 200, path


def test_us_t18_ac1_overview_carries_curve_components_and_kpi_tiles(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/trend/overview").json()
    kpis = body["kpis"]
    for tile in (
        "equity",
        "since_inception_net",
        "net_30d",
        "realised_vol",
        "max_drawdown",
        "current_drawdown",
        "governor_g",
        "sharpe",
        "sharpe_std_error",
        "gross_notional",
        "net_notional",
        "phase",
        "risk_status",
    ):
        assert tile in kpis, tile
    assert kpis["equity"] > 0
    assert kpis["sharpe"] == 1.1 and kpis["sharpe_std_error"] == 0.4  # read from metrics, not recomputed
    assert kpis["max_drawdown"] == 0.08
    assert kpis["vol_target"] == 0.20

    assert len(body["curve"]) == 40
    point = body["curve"][-1]
    assert point["cash_alternative"] is not None  # the three chart lines of AC 1
    assert point["backtest_reference"] is not None
    assert body["reference_run_id"] == "RUN-1"

    components = body["cumulative_components"]
    assert components["net_pnl"] != 0.0
    assert {"price_pnl", "funding", "fees", "slippage"} <= set(components)
    assert {row["side"] for row in body["by_side"]} == {"long", "short"}


def test_us_t18_ac2_signals_row_carries_u_k_target_and_funding_haircut(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/trend/signals").json()
    rows = {r["symbol"]: r for r in body["rows"]}
    btc = rows["BTCUSDT"]
    assert (btc["u1"], btc["u2"], btc["u3"]) == (0.3, 0.2, 0.1)
    assert btc["vol"] == 0.6
    assert btc["target_notional"] == 1000.0
    assert btc["current_notional"] == 0.05 * 61_000.0
    assert btc["delta_notional"] == btc["target_notional"] - btc["current_notional"]
    assert btc["funding_ann"] == 0.35 and btc["haircut_applied"] is True
    assert rows["ETHUSDT"]["haircut_applied"] is False
    assert rows["SOLUSDT"]["illiquid"] is True


def test_us_t18_ac2_signal_history_returns_the_requested_window(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/trend/signals/BTCUSDT/history?days=10").json()
    assert body["symbol"] == "BTCUSDT"
    assert len(body["points"]) == 10
    assert body["points"][0]["day"] < body["points"][-1]["day"]


def test_us_t18_ac3_positions_page_shows_book_caps_margin_and_kill_rules(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/trend/positions").json()
    rows = {r["symbol"]: r for r in body["positions"]}
    assert rows["BTCUSDT"]["notional"] == 0.05 * 61_000.0
    assert rows["BTCUSDT"]["side"] == "long" and rows["ETHUSDT"]["side"] == "short"
    assert rows["BTCUSDT"]["funding_accrued"] == -10.0  # 40 daily funding rows of -0.25
    assert rows["ETHUSDT"]["adl_quantile"] == 4

    caps = {c["cap"]: c for c in body["caps"]}
    assert set(caps) == {"gross", "net", "single"}
    assert caps["gross"]["limit_x"] == 2.5
    assert 0.0 <= caps["gross"]["utilisation"] <= 1.0
    assert caps["single"]["used_usdt"] == max(abs(r["notional"]) for r in body["positions"])

    assert body["margin"]["survivable_move"] > 0
    assert body["margin"]["survives_downtime"] is True
    assert [g["g_after"] for g in body["governor_history"]] == [0.5, 1.0]
    board = {r["rule"]: r for r in body["kill_rules"]}
    assert "hard_halt_drawdown" in board and board["hard_halt_drawdown"]["active"] is False
    assert board["daily_loss"]["detail"] == "day loss 6.2% of equity"


def test_us_t18_ac4_rebalance_list_and_drilldown(seeded_client: TestClient) -> None:
    listing = seeded_client.get("/api/trend/rebalances").json()
    assert [r["rebalance_id"] for r in listing["rows"]] == [REBALANCE_ID]
    row = listing["rows"][0]
    assert row["completion_pct"] == 98.0 and row["duration_s"] == 600.0
    assert row["maker_ratio"] == 1.0 and row["n_residuals"] == 1

    detail = seeded_client.get(f"/api/trend/rebalances/{REBALANCE_ID}").json()
    assert detail["sizing"]["sigma_p"] == 0.22
    assert [t["symbol"] for t in detail["targets"]] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert detail["targets"][0]["caps_applied"] == ["single"]
    assert [s["slice_id"] for s in detail["slices"]] == ["SL-1"]
    assert [f["trade_id"] for f in detail["fills"]] == ["T1"]
    assert detail["residuals"][0]["symbol"] == "SOLUSDT"


def test_us_t18_ac4_unknown_rebalance_is_404(seeded_client: TestClient) -> None:
    assert seeded_client.get("/api/trend/rebalances/nope").status_code == 404


def test_us_t18_ac5_attribution_covers_periods_shares_and_regimes(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/trend/attribution").json()
    assert [p["period"] for p in body["periods"]] == ["7d", "30d", "90d", "mtd", "ytd", "since_inception"]
    since = next(p for p in body["periods"] if p["period"] == "since_inception")
    assert {r["symbol"] for r in since["by_symbol"]} == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert abs(sum(r["contribution_share"] for r in since["by_symbol"]) - 1.0) < 1e-9
    assert {r["side"] for r in since["by_side"]} == {"long", "short"}
    assert since["concentration"] == 0.4
    assert {r["bucket"] for r in body["regime"]} == {"down", "flat", "up"}
    assert body["monthly_net_pnl"]


def test_us_t18_ac6_metrics_page_returns_stored_values_with_n_obs_and_se(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/trend/metrics").json()
    assert body["min_active_days"] == 20
    block = next(b for b in body["periods"] if b["period"] == "30d")
    by_name = {m["name"]: m for m in block["metrics"]}
    assert by_name["sharpe"]["value"] == 1.1
    assert by_name["sharpe"]["std_error"] == 0.4
    assert by_name["sharpe"]["n_obs"] == 25
    assert block["below_min_active_days"] is False

    filtered = seeded_client.get("/api/trend/metrics?period=7d").json()
    assert [b["period"] for b in filtered["periods"]] == ["7d"]


def test_us_t18_ac6_operations_reports_uptime_recon_alerts_and_infra(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/trend/operations").json()
    assert body["heartbeat"]["beats_24h"] == 24
    assert body["heartbeat"]["uptime_24h_pct"] == 100.0
    assert body["reconciliation"]["open_breaks"] == 1
    assert body["reconciliation"]["breaks"][0]["kind"] == "balance"
    assert body["backup"]["lag_s"] == 900.0
    assert body["infra"]["rss_mb"] == 420.0 and body["infra"]["cpu_pct"] == 8.5
    assert {a["code"] for a in body["alerts"]} == {"BACKUP_OK", "DAILY_LOSS"}
    assert body["control_log"][0]["action"] == "start"
    assert body["reports"][0]["kind"] == "daily"


def test_us_t18_ac6_backtest_page_carries_robustness_walkforward_and_bootstrap(
    seeded_client: TestClient,
) -> None:
    body = seeded_client.get("/api/trend/backtest").json()
    assert [r["run_id"] for r in body["runs"]] == ["RUN-1"]
    assert body["run"]["metrics"]["sharpe"] == 0.9
    assert {r["variant"] for r in body["robustness"]} == {"default", "fees_x2"}
    assert [w["param_set"] for w in body["walkforward"]] == ["fast", "default"]  # by rank
    assert body["bootstrap"]["5.0"] == -0.08
    assert body["tracking"]["latest"]["corr_30d"] == 0.85
    assert body["tracking"]["min_corr"] == 0.7


def test_us_t18_ac6_universe_page_shows_entrants_leavers_and_illiquid(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/trend/universe").json()
    assert body["current_month"] == "2026-09"
    assert body["current_symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    september = next(m for m in body["months"] if m["month"] == "2026-09")
    assert september["entrants"] == ["SOLUSDT"]
    assert september["leavers"] == ["XRPUSDT"]
    assert [e["symbol"] for e in september["excluded"]] == ["DOGEUSDT"]
    assert september["excluded"][0]["reason"] == "insufficient history"
    assert [f["symbol"] for f in body["illiquid"]] == ["SOLUSDT"]


def test_us_t18_ac7_combined_overview_shows_each_sleeve_side_by_side(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/overview").json()
    assert [s["strategy"] for s in body["sleeves"]] == ["TREND"]
    sleeve = body["sleeves"][0]
    assert sleeve["equity"] > 0
    assert sleeve["max_drawdown"] == 0.08
    assert sleeve["risk_status"] in ("green", "amber", "red")
    assert body["total_equity"] == sleeve["equity"]


def test_health_is_answerable_without_touching_a_database(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/health").json()
    assert body["ok"] is True
    assert body["strategies"] == ["TREND"]
