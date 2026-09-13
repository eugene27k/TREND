"""Analytics — the metric engine.

``metrics`` holds the shared, strategy-agnostic performance functions;
``trend_metrics`` the PRD Section 10 TREND additions. Both are pure. ``engine``
is the only part that touches the database: it loads the rows, calls the pure
functions for every configured period, and stores ``MetricValue`` rows.
"""

from __future__ import annotations

from aegis.analytics.engine import (
    DOCUMENTED_METRIC_NAMES,
    EXTRA_METRIC_NAMES,
    METRIC_NAMES,
    MetricsEngine,
    Window,
)
from aegis.analytics.metrics import MetricInputError

__all__ = [
    "DOCUMENTED_METRIC_NAMES",
    "EXTRA_METRIC_NAMES",
    "METRIC_NAMES",
    "MetricInputError",
    "MetricsEngine",
    "Window",
]
