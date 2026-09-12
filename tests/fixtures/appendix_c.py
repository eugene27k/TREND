"""PRD Appendix C canonical test vectors.

Every number below was recomputed from the PRD's own formulas before any
implementation existed, and all of them reproduce the PRD exactly. They are
therefore the specification, not a record of what the code happens to do:
a test that fails here means the implementation is wrong.

Semantics pinned by the recomputation:
  * EMA          pandas ``ewm(span=n, adjust=False)`` (alpha = 2/(n+1), seeded at obs 0)
  * rolling std  ``rolling(w).std(ddof=1)`` — sample std, NaN until the window fills
  * EWMA vol     lambda = 2**(-1/half_life); v_t = lambda*v_{t-1} + (1-lambda)*r_t**2, v_0 = r_0**2
  * covariance   Sigma = diag(sigma) . Corr . diag(sigma), annualised
  * annualise    x sqrt(365) / x 365 (crypto year, Locked Decision 4)
"""

from __future__ import annotations

import math

TOL = 1e-6

# --------------------------------------------------------------------------- #
# C.1 — Signal
# --------------------------------------------------------------------------- #

#: ``P_t = 100 * exp(0.002 t + 0.01 sin(t/3))`` for t = 0..59.
C1_PRICES: list[float] = [100.0 * math.exp(0.002 * t + 0.01 * math.sin(t / 3)) for t in range(60)]
C1_LAST_PRICE = 113.347906  # P_59, to 6 dp

#: Test-only signal parameters (deliberately shorter than the production defaults).
C1_PARAMS = {
    "pairs": ((2, 6), (4, 12), (8, 24)),
    "price_std_window": 10,
    "y_std_window": 20,
    "response_norm": 0.89,
    "clip": 1.0,
}

#: (pair, x, y, z, u) at t = 59.
C1_EXPECTED: list[tuple[tuple[int, int], float, float, float, float]] = [
    ((2, 6), 0.952997, 0.702506, 1.750603, 0.914243),
    ((4, 12), 1.310251, 0.965858, 3.134116, 0.302163),
    ((8, 24), 1.856538, 1.368556, 0.676271, 0.677759),
]
C1_SIGNAL = 0.631388

# --------------------------------------------------------------------------- #
# C.2 — Volatility estimator
# --------------------------------------------------------------------------- #

C2_RETURNS = [0.02, -0.01, 0.015, -0.03, 0.01, 0.005, -0.02, 0.025, -0.005, 0.01]
C2_HALF_LIFE = 10.0
C2_LAMBDA = 0.933033
C2_VARIANCE = 3.39696524e-4
C2_SIGMA = 0.352121  # annualised, above the 0.30 floor so it passes through

C2_FLAT_RETURNS = [0.001] * 10
C2_FLAT_SIGMA_RAW = 0.019105
C2_FLAT_SIGMA_FLOORED = 0.30

# --------------------------------------------------------------------------- #
# C.3 — Sizing
# --------------------------------------------------------------------------- #

C3_EQUITY = 10_000.0
C3_N = 16
C3_SIGMA_TARGET_ASSET = 0.25
C3_SIGMA_TARGET_PORTFOLIO = 0.20
C3_G = 1.0
C3_SYMBOLS = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
C3_SIGNALS = {"AAAUSDT": 0.8, "BBBUSDT": -0.5, "CCCUSDT": 0.3}
C3_VOLS = {"AAAUSDT": 0.60, "BBBUSDT": 0.90, "CCCUSDT": 0.45}
C3_CORR = ((1.0, 0.8, 0.7), (0.8, 1.0, 0.6), (0.7, 0.6, 1.0))

C3_RAW = {"AAAUSDT": 208.333333, "BBBUSDT": -86.805556, "CCCUSDT": 104.166667}
C3_SIGMA_P = 0.011004
C3_CONV = 0.533333        # mean(|signal|) over the three symbols with a signal
C3_SIGMA_EFF = 0.20
C3_S = 3.0                # s_max clip binds (unclipped 18.174779)
C3_S_UNCLIPPED = 18.174779
C3_TARGETS = {"AAAUSDT": 625.000000, "BBBUSDT": -260.416667, "CCCUSDT": 312.500000}
C3_GROSS = 1197.916667
C3_NET = 677.083333

# --------------------------------------------------------------------------- #
# C.4 — Metrics
# --------------------------------------------------------------------------- #

C4_TRADED_NOTIONAL = 12_000.0
C4_AVG_EQUITY = 10_000.0
C4_DAYS = 30
C4_TURNOVER_PERIOD = 1.2
C4_TURNOVER_ANNUALISED = 14.6      # 1.2 * 365 / 30
C4_BETA_SEED = 7                    # numpy.random.default_rng(7)
C4_BETA_DAYS = 60
C4_BETA_BTC_SD = 0.03
C4_BETA_EPS_SD = 0.005
C4_BETA = 0.3098
C4_BETA_TOL = 0.02
C4_CORR = 0.891
C4_CORR_TOL = 0.02

# --------------------------------------------------------------------------- #
# C.5 — Governor
# --------------------------------------------------------------------------- #

C5_DD_SEQUENCE = [0.00, 0.11, 0.12, 0.13, 0.19, 0.20, 0.25, 0.16, 0.14, 0.09, 0.07]
C5_EXPECTED_G = [1.0, 1.0, 0.5, 0.5, 0.5, 0.25, 0.25, 0.25, 0.5, 0.5, 1.0]
C5_START_G = 1.0

# --------------------------------------------------------------------------- #
# C.6 — Hysteresis
# --------------------------------------------------------------------------- #

C6_EQUITY = 10_000.0
#: (target, current, in_universe, should_trade, why)
C6_CASES: list[tuple[float, float, bool, bool, str]] = [
    (1000.0, 950.0, True, False, "|delta| 50 < max(0.10*1000=100, 0.0025*E=25)"),
    (1000.0, 850.0, True, True, "|delta| 150 > 100"),
    (20.0, 0.0, True, False, "target 20 is below 0.25% of E = 25"),
    (0.0, 20.0, False, True, "symbol left the universe -> always trade to zero"),
]

__all__ = [n for n in dir() if n.startswith(("C1_", "C2_", "C3_", "C4_", "C5_", "C6_", "TOL"))]
