"""US-T05 — volatility and covariance estimators (PRD Section 5.4, Appendix C.2)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from aegis.core.config import CovConfig, VolConfig
from aegis.core.errors import ConfigError
from aegis.core.types import RiskModel
from aegis.riskmodel.estimators import (
    build_risk_model,
    decay,
    ewma_cov,
    ewma_vol,
    ewma_vol_series,
    nearest_psd,
)
from tests.fixtures.appendix_c import (
    C2_FLAT_RETURNS,
    C2_FLAT_SIGMA_FLOORED,
    C2_FLAT_SIGMA_RAW,
    C2_HALF_LIFE,
    C2_LAMBDA,
    C2_RETURNS,
    C2_SIGMA,
    C2_VARIANCE,
    TOL,
)

SEED = 20260912


def _returns(n: int, *, seed: int, sd: float = 0.02) -> list[float]:
    return list(np.random.default_rng(seed).normal(0.0, sd, n))


def _correlated(base: list[float], rho: float, *, seed: int, sd: float = 0.02) -> list[float]:
    noise = np.random.default_rng(seed).normal(0.0, sd, len(base))
    return list(rho * np.asarray(base) + math.sqrt(max(1.0 - rho * rho, 0.0)) * noise)


# --------------------------------------------------------------------------- #
# AC1 — Appendix C.2 reproduced
# --------------------------------------------------------------------------- #


def test_us_t05_ac1_lambda_is_two_to_the_minus_one_over_half_life() -> None:
    assert decay(C2_HALF_LIFE) == pytest.approx(C2_LAMBDA, abs=TOL)


def test_us_t05_ac1_c2_variance_reproduced_to_1e_12_relative() -> None:
    lam = decay(C2_HALF_LIFE)
    v = C2_RETURNS[0] ** 2
    for r in C2_RETURNS[1:]:
        v = lam * v + (1.0 - lam) * r * r
    assert v == pytest.approx(C2_VARIANCE, rel=1e-12)
    # ...and the public estimator is the square root of exactly that variance.
    sigma = ewma_vol(C2_RETURNS, C2_HALF_LIFE)
    assert (sigma / math.sqrt(365)) ** 2 == pytest.approx(C2_VARIANCE, rel=1e-12)


def test_us_t05_ac1_c2_annualised_sigma_passes_through_unclamped() -> None:
    assert ewma_vol(C2_RETURNS, C2_HALF_LIFE) == pytest.approx(C2_SIGMA, abs=TOL)


def test_us_t05_ac1_flat_series_raw_sigma_then_floored() -> None:
    raw = ewma_vol(C2_FLAT_RETURNS, C2_HALF_LIFE, floor=0.0)
    assert raw == pytest.approx(C2_FLAT_SIGMA_RAW, abs=TOL)
    assert ewma_vol(C2_FLAT_RETURNS, C2_HALF_LIFE) == C2_FLAT_SIGMA_FLOORED


def test_us_t05_ac1_cap_binds_at_three_hundred_percent() -> None:
    violent = [0.5, -0.6, 0.55, -0.7, 0.65]
    assert ewma_vol(violent, C2_HALF_LIFE, cap=1e9) > 3.00
    assert ewma_vol(violent, C2_HALF_LIFE) == 3.00


def test_us_t05_ac1_vol_series_last_element_equals_point_estimator() -> None:
    series = ewma_vol_series(C2_RETURNS, C2_HALF_LIFE)
    assert len(series) == len(C2_RETURNS)
    assert series[-1] == pytest.approx(C2_SIGMA, abs=TOL)
    assert series[0] == pytest.approx(min(max(abs(C2_RETURNS[0]) * math.sqrt(365), 0.30), 3.00), abs=TOL)
    # Every prefix of the series matches the point estimator on that prefix —
    # this is what makes the backtester look-ahead free.
    for t in range(len(C2_RETURNS)):
        assert series[t] == pytest.approx(ewma_vol(C2_RETURNS[: t + 1], C2_HALF_LIFE), abs=1e-15)


def test_us_t05_ac1_vol_series_is_clamped_like_the_point_estimator() -> None:
    series = ewma_vol_series(C2_FLAT_RETURNS, C2_HALF_LIFE)
    assert set(series) == {C2_FLAT_SIGMA_FLOORED}
    assert ewma_vol_series([], C2_HALF_LIFE) == []


# --------------------------------------------------------------------------- #
# Degenerate inputs
# --------------------------------------------------------------------------- #


def test_us_t05_empty_returns_fall_back_to_the_floor() -> None:
    assert ewma_vol([]) == 0.30
    assert ewma_vol([], floor=0.45) == 0.45


def test_us_t05_single_return_uses_the_seed_variance() -> None:
    assert ewma_vol([0.04], floor=0.0) == pytest.approx(0.04 * math.sqrt(365), abs=1e-12)
    # Short samples are still estimated: min_obs is a reliability marker, not a gate.
    assert ewma_vol([0.04], min_obs=500, floor=0.0) == pytest.approx(0.04 * math.sqrt(365), abs=1e-12)


def test_us_t05_all_zero_returns_give_zero_variance_and_are_floored() -> None:
    assert ewma_vol([0.0] * 50, floor=0.0) == 0.0
    assert ewma_vol([0.0] * 50) == 0.30


def test_us_t05_rejects_bad_parameters() -> None:
    with pytest.raises(ConfigError):
        decay(0.0)
    with pytest.raises(ConfigError):
        ewma_vol(C2_RETURNS, -1.0)
    with pytest.raises(ConfigError):
        ewma_vol(C2_RETURNS, floor=-0.1)
    with pytest.raises(ConfigError):
        ewma_vol(C2_RETURNS, floor=1.0, cap=0.5)
    with pytest.raises(ConfigError):
        ewma_vol(C2_RETURNS, annualisation_days=0)
    with pytest.raises(ConfigError):
        ewma_vol(C2_RETURNS, min_obs=-1)
    with pytest.raises(ConfigError):
        ewma_vol([0.01, float("nan")])
    with pytest.raises(ConfigError):
        ewma_vol_series([0.01, math.inf])
    with pytest.raises(ConfigError):
        ewma_vol_series(C2_RETURNS, min_obs=-1)
    with pytest.raises(ConfigError):
        ewma_vol_series(C2_RETURNS, annualisation_days=-5)
    with pytest.raises(ConfigError):
        ewma_cov({"AAAUSDT": [0.01]}, min_obs=-1)
    with pytest.raises(ConfigError):
        ewma_cov({"AAAUSDT": [0.01]}, annualisation_days=0)


def test_us_t05_annualisation_days_scales_by_its_square_root() -> None:
    a = ewma_vol(C2_RETURNS, floor=0.0, annualisation_days=365)
    b = ewma_vol(C2_RETURNS, floor=0.0, annualisation_days=252)
    assert b == pytest.approx(a * math.sqrt(252 / 365), rel=1e-12)


# --------------------------------------------------------------------------- #
# AC2 — covariance, annualisation, and the short-history fallback
# --------------------------------------------------------------------------- #


def test_us_t05_ac2_cov_matrix_is_annualised_and_matches_the_stored_vols() -> None:
    base = _returns(200, seed=SEED)
    returns = {
        "AAAUSDT": base,
        "BBBUSDT": _correlated(base, 0.7, seed=SEED + 1),
        "CCCUSDT": _correlated(base, -0.4, seed=SEED + 2),
    }
    rm = ewma_cov(returns)
    sigma = rm.cov_matrix()
    for i, s in enumerate(rm.symbols):
        # Diagonal of Sigma is the annualised variance of the stored (clamped) vol.
        assert sigma[i][i] == pytest.approx(rm.vols[s] ** 2, rel=1e-12)
        assert rm.vols[s] == pytest.approx(ewma_vol(returns[s]), rel=1e-12)
    # Annualisation is a pure scale factor on a daily covariance.
    daily = ewma_cov(returns, annualisation_days=1)
    assert daily.vols["AAAUSDT"] == pytest.approx(
        min(max(rm.vols["AAAUSDT"] / math.sqrt(365), 0.30), 3.0), rel=1e-12
    )
    assert daily.corr == rm.corr  # correlations are scale-free


def test_us_t05_ac2_short_history_symbol_gets_the_universe_average_correlation() -> None:
    base = _returns(200, seed=SEED)
    long_returns = {
        "AAAUSDT": base,
        "BBBUSDT": _correlated(base, 0.7, seed=SEED + 1),
        "CCCUSDT": _correlated(base, 0.2, seed=SEED + 2),
    }
    short_returns = _correlated(base, 0.9, seed=SEED + 3)[-40:]  # only 40 days of history
    rm = ewma_cov({**long_returns, "DDDUSDT": short_returns}, min_obs=60)

    assert rm.symbols == ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT")
    assert rm.n_obs["DDDUSDT"] == 40

    # The universe average is the mean of the three pairs that DO have >= 60 obs.
    measured = ewma_cov(long_returns, min_obs=60)
    expected_avg = (measured.corr[0][1] + measured.corr[0][2] + measured.corr[1][2]) / 3.0
    assert rm.avg_corr == pytest.approx(expected_avg, abs=1e-12)

    d = rm.symbols.index("DDDUSDT")
    for i, s in enumerate(rm.symbols):
        if s == "DDDUSDT":
            continue
        # Exact value, both triangles — no measured correlation leaks in.
        assert rm.corr[d][i] == pytest.approx(expected_avg, abs=1e-12)
        assert rm.corr[i][d] == pytest.approx(expected_avg, abs=1e-12)
    assert rm.corr[d][d] == 1.0
    # The long-history block is untouched by the substitution.
    assert rm.corr[0][1] == pytest.approx(measured.corr[0][1], abs=1e-12)


def test_us_t05_ac2_avg_corr_is_zero_when_no_pair_is_long_enough() -> None:
    rm = ewma_cov({"AAAUSDT": _returns(10, seed=SEED), "BBBUSDT": _returns(10, seed=SEED + 1)})
    assert rm.avg_corr == 0.0
    assert rm.corr == ((1.0, 0.0), (0.0, 1.0))


def test_us_t05_ac2_correlation_matrix_is_symmetric_unit_diagonal_and_bounded() -> None:
    base = _returns(150, seed=SEED)
    returns = {
        "AAAUSDT": base,
        "BBBUSDT": _correlated(base, 0.95, seed=SEED + 1),
        "CCCUSDT": _correlated(base, -0.8, seed=SEED + 2),
        "DDDUSDT": _returns(150, seed=SEED + 3),
    }
    rm = ewma_cov(returns)
    n = len(rm.symbols)
    for i in range(n):
        assert rm.corr[i][i] == 1.0
        for j in range(n):
            assert rm.corr[i][j] == pytest.approx(rm.corr[j][i], abs=1e-15)
            assert -1.0 <= rm.corr[i][j] <= 1.0


def test_us_t05_ac2_zero_variance_symbol_falls_back_to_the_average_correlation() -> None:
    base = _returns(150, seed=SEED)
    returns = {
        "AAAUSDT": base,
        "BBBUSDT": _correlated(base, 0.6, seed=SEED + 1),
        "CCCUSDT": _correlated(base, 0.3, seed=SEED + 2),
        "FLATUSDT": [0.0] * 150,  # no variance -> correlation is undefined
    }
    rm = ewma_cov(returns)
    f = rm.symbols.index("FLATUSDT")
    assert rm.corr[f][0] == pytest.approx(rm.avg_corr, abs=1e-12)
    assert rm.vols["FLATUSDT"] == 0.30  # floored, so Sigma stays invertible-ish


def test_us_t05_ac2_symbol_absent_from_returns_is_absent_from_the_model() -> None:
    rm = ewma_cov({"AAAUSDT": _returns(80, seed=SEED), "BBBUSDT": _returns(80, seed=SEED + 1)})
    assert "ZZZUSDT" not in rm.symbols
    assert "ZZZUSDT" not in rm.vols
    assert "ZZZUSDT" not in rm.n_obs
    # A symbol present with an empty series is kept, floored, and marked 0 obs.
    rm2 = ewma_cov({"AAAUSDT": _returns(80, seed=SEED), "ZZZUSDT": []})
    assert rm2.vols["ZZZUSDT"] == 0.30
    assert rm2.n_obs["ZZZUSDT"] == 0
    assert rm2.corr[0][1] == 0.0  # no qualifying pair -> avg_corr 0.0


def test_us_t05_ac2_empty_universe_returns_an_empty_model() -> None:
    rm = ewma_cov({})
    assert rm.symbols == ()
    assert rm.corr == ()
    assert rm.avg_corr == 0.0
    assert rm.cov_matrix() == []


# --------------------------------------------------------------------------- #
# nearest_psd
# --------------------------------------------------------------------------- #


def test_us_t05_nearest_psd_repairs_a_deliberately_non_psd_matrix() -> None:
    bad = [[1.0, 0.9, -0.9], [0.9, 1.0, 0.9], [-0.9, 0.9, 1.0]]
    assert np.linalg.eigvalsh(np.array(bad)).min() < 0.0
    fixed = nearest_psd(bad)
    arr = np.array(fixed)
    assert np.linalg.eigvalsh(arr).min() >= -1e-12
    assert np.allclose(arr, arr.T, atol=1e-15)
    assert all(fixed[i][i] == 1.0 for i in range(3))
    # ...and the repaired matrix can no longer produce a negative variance.
    rng = np.random.default_rng(SEED)
    for _ in range(200):
        w = rng.normal(size=3)
        assert w @ arr @ w >= -1e-12


def test_us_t05_nearest_psd_leaves_a_valid_matrix_essentially_unchanged() -> None:
    good = [[1.0, 0.8, 0.7], [0.8, 1.0, 0.6], [0.7, 0.6, 1.0]]
    fixed = nearest_psd(good)
    assert np.allclose(np.array(fixed), np.array(good), atol=1e-9)


def test_us_t05_nearest_psd_rejects_malformed_input() -> None:
    assert nearest_psd([]) == ()
    with pytest.raises(ConfigError):
        nearest_psd([[1.0, 0.5]])
    with pytest.raises(ConfigError):
        nearest_psd([[1.0, float("nan")], [float("nan"), 1.0]])


def test_us_t05_avg_corr_substitution_is_repaired_into_a_psd_matrix() -> None:
    # Two strongly anti-correlated long-history symbols plus two short-history
    # ones inheriting a positive average: the raw substitution is not PSD.
    base = _returns(200, seed=SEED)
    returns = {
        "AAAUSDT": base,
        "BBBUSDT": _correlated(base, 0.99, seed=SEED + 1),
        "CCCUSDT": _correlated(base, 0.99, seed=SEED + 2),
        "DDDUSDT": _correlated(base, -0.99, seed=SEED + 3)[-20:],
        "EEEUSDT": _correlated(base, -0.99, seed=SEED + 4)[-20:],
    }
    rm = ewma_cov(returns, min_obs=60)
    sigma = np.array(rm.cov_matrix())
    assert np.linalg.eigvalsh(sigma).min() >= -1e-9
    rng = np.random.default_rng(SEED)
    for _ in range(500):
        w = rng.normal(size=len(rm.symbols))
        assert math.sqrt(max(w @ sigma @ w, 0.0)) >= 0.0


# --------------------------------------------------------------------------- #
# AC3 — build_risk_model
# --------------------------------------------------------------------------- #


def test_us_t05_ac3_build_risk_model_carries_everything_a_snapshot_needs() -> None:
    base = _returns(200, seed=SEED)
    returns = {
        "CCCUSDT": _correlated(base, 0.2, seed=SEED + 2),
        "AAAUSDT": base,
        "BBBUSDT": _correlated(base, 0.7, seed=SEED + 1),
    }
    rm = build_risk_model(returns, VolConfig(), CovConfig())

    assert isinstance(rm, RiskModel)
    assert rm.symbols == ("AAAUSDT", "BBBUSDT", "CCCUSDT")  # deterministic, sorted
    assert set(rm.vols) == set(rm.symbols)
    assert set(rm.n_obs) == set(rm.symbols)
    assert all(rm.n_obs[s] == 200 for s in rm.symbols)
    assert len(rm.corr) == 3 and all(len(row) == 3 for row in rm.corr)
    assert -1.0 <= rm.avg_corr <= 1.0
    sigma = rm.cov_matrix()
    assert sigma[0][1] == pytest.approx(rm.vols["AAAUSDT"] * rm.vols["BBBUSDT"] * rm.corr[0][1], rel=1e-12)


def test_us_t05_ac3_build_risk_model_honours_the_vol_config() -> None:
    returns = {"AAAUSDT": C2_RETURNS, "BBBUSDT": C2_RETURNS}
    default = build_risk_model(returns, VolConfig(), CovConfig())
    assert default.vols["AAAUSDT"] == pytest.approx(C2_SIGMA, abs=TOL)

    clamped = build_risk_model(returns, VolConfig(floor=0.40, cap=0.45), CovConfig())
    assert clamped.vols["AAAUSDT"] == 0.40

    slow = build_risk_model(returns, VolConfig(half_life_days=40.0, floor=0.0), CovConfig())
    assert slow.vols["AAAUSDT"] == pytest.approx(ewma_vol(C2_RETURNS, 40.0, floor=0.0), rel=1e-12)


def test_us_t05_ac3_build_risk_model_symbol_order_is_input_order_independent() -> None:
    base = _returns(120, seed=SEED)
    a = {"BBBUSDT": _correlated(base, 0.5, seed=SEED + 1), "AAAUSDT": base}
    b = {"AAAUSDT": base, "BBBUSDT": _correlated(base, 0.5, seed=SEED + 1)}
    assert build_risk_model(a, VolConfig(), CovConfig()) == build_risk_model(b, VolConfig(), CovConfig())


def test_us_t05_ac3_build_risk_model_rejects_bad_config() -> None:
    with pytest.raises(ConfigError):
        build_risk_model({"AAAUSDT": C2_RETURNS}, VolConfig(), CovConfig(half_life_days=0.0))
    with pytest.raises(ConfigError):
        build_risk_model({"AAAUSDT": C2_RETURNS}, VolConfig(), CovConfig(annualisation_days=0))
    with pytest.raises(ConfigError):
        build_risk_model({"AAAUSDT": [0.01, float("inf")]}, VolConfig(), CovConfig())


def test_us_t05_ac3_build_risk_model_short_history_matches_ewma_cov_fallback() -> None:
    base = _returns(200, seed=SEED)
    returns = {
        "AAAUSDT": base,
        "BBBUSDT": _correlated(base, 0.7, seed=SEED + 1),
        "CCCUSDT": _correlated(base, 0.2, seed=SEED + 2),
        "DDDUSDT": _correlated(base, 0.9, seed=SEED + 3)[-40:],
    }
    rm = build_risk_model(returns, VolConfig(), CovConfig())
    direct = ewma_cov(returns, CovConfig().half_life_days, min_obs=CovConfig().min_obs)
    assert rm.corr == direct.corr
    assert rm.avg_corr == pytest.approx(direct.avg_corr, abs=1e-15)
