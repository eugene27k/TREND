"""Robustness variants (PRD 11.5) and the parameter grid for walk-forward (11.6).

The P0 gate does not ask a variant to be *good*; it asks the **sign of net P&L
not to flip**. That is the honest question for a systematic strategy: if moving
the lookbacks one notch turns a winner into a loser, the result was a property
of the parameters rather than of the market.

Nothing produced here may ever reach the live configuration (Invariant 7).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from aegis.core.config import AppConfig

#: PRD 11.5, in order. ``overrides`` are applied to the config with
#: ``model_copy(update=...)`` on the relevant section.
VARIANTS: dict[str, dict[str, Any]] = {
    "default": {},
    "speeds_fast": {"signal": {"pairs": ((4, 12), (8, 24), (16, 48))}},
    "speeds_slow": {"signal": {"pairs": ((16, 48), (32, 96), (64, 192))}},
    "sigma_asset_down": {"sizing": {"sigma_target_asset": 0.175}},
    "sigma_asset_up": {"sizing": {"sigma_target_asset": 0.325}},
    "sigma_portfolio_down": {"sizing": {"sigma_target_portfolio": 0.14}},
    "sigma_portfolio_up": {"sizing": {"sigma_target_portfolio": 0.26}},
    "hysteresis_5": {"rebalance": {"hysteresis_frac": 0.05}},
    "hysteresis_20": {"rebalance": {"hysteresis_frac": 0.20}},
    "universe_12": {"universe": {"size": 12}},
    "universe_20": {"universe": {"size": 20}},
    "cost_x2": {"exec": {"slippage_bps": {"BTCUSDT": 4.0, "ETHUSDT": 4.0, "default": 12.0},
                         "taker_fee_fallback": 0.001}},
    "governor_off": {"governor": {"down": {}, "up": {}}},
    "rebalance_midday": {"_fill_at": "close"},
}

#: Not a gate condition — it exists to *show* the governor's contribution (11.5.6).
NOT_A_GATE = frozenset({"governor_off"})


def apply_overrides(cfg: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    """Return a new config with the variant applied. The original is untouched."""
    update: dict[str, Any] = {}
    for section, values in overrides.items():
        if section.startswith("_"):
            continue
        current = getattr(cfg, section)
        update[section] = current.model_copy(update=dict(values))
    return cfg.model_copy(update=update) if update else cfg


def variant_configs(cfg: AppConfig) -> Iterator[tuple[str, AppConfig, dict[str, Any]]]:
    """``(name, config, simulator_options)`` for every PRD 11.5 variant."""
    for name, overrides in VARIANTS.items():
        options = {k[1:]: v for k, v in overrides.items() if k.startswith("_")}
        yield name, apply_overrides(cfg, overrides), options


@dataclass(frozen=True, slots=True)
class RobustnessRow:
    variant: str
    net_pnl: float
    sharpe: float
    max_dd: float
    sign_ok: bool
    is_gate: bool = True
    detail: dict[str, Any] | None = None

    def as_row(self) -> dict[str, Any]:
        return {"variant": self.variant, "net_pnl": self.net_pnl, "sharpe": self.sharpe,
                "max_dd": self.max_dd, "sign_ok": self.sign_ok, "detail": self.detail or {}}


def evaluate(results: dict[str, Any], baseline: str = "default") -> list[RobustnessRow]:
    """Compare every variant's net P&L sign against the baseline's (11.5 gate)."""
    base = results.get(baseline)
    base_sign = _sign(base.metrics.get("net_pnl", 0.0)) if base is not None else 0
    rows: list[RobustnessRow] = []
    for name, result in results.items():
        pnl = float(result.metrics.get("net_pnl", 0.0))
        rows.append(RobustnessRow(
            variant=name, net_pnl=pnl,
            sharpe=float(result.metrics.get("sharpe", 0.0)),
            max_dd=float(result.metrics.get("max_drawdown", 0.0)),
            sign_ok=(name == baseline) or (_sign(pnl) == base_sign),
            is_gate=name not in NOT_A_GATE,
        ))
    return sorted(rows, key=lambda r: r.variant)


def gate_passes(rows: list[RobustnessRow]) -> bool:
    """P0: 'sign of net P&L unchanged under each robustness variant'."""
    gated = [r for r in rows if r.is_gate]
    return bool(gated) and all(r.sign_ok for r in gated)


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


#: PRD 11.6 grid: speed sets x sigma_target_asset x hysteresis.
GRID_SPEEDS = {
    "fast": ((4, 12), (8, 24), (16, 48)),
    "default": ((8, 24), (16, 48), (32, 96)),
    "slow": ((16, 48), (32, 96), (64, 192)),
}
GRID_SIGMA = (0.20, 0.25, 0.30)
GRID_HYSTERESIS = (0.05, 0.10, 0.20)

#: The live defaults' coordinates in that grid — the set whose rank is reported.
DEFAULT_PARAM_SET = "default|0.25|0.10"


def parameter_grid() -> list[tuple[str, dict[str, Any]]]:
    """``(name, overrides)`` for every point of the walk-forward grid."""
    out: list[tuple[str, dict[str, Any]]] = []
    for speed_name, pairs in GRID_SPEEDS.items():
        for sigma in GRID_SIGMA:
            for hyst in GRID_HYSTERESIS:
                out.append((
                    f"{speed_name}|{sigma:.2f}|{hyst:.2f}",
                    {"signal": {"pairs": pairs},
                     "sizing": {"sigma_target_asset": sigma},
                     "rebalance": {"hysteresis_frac": hyst}},
                ))
    return out


__all__ = [
    "DEFAULT_PARAM_SET", "GRID_HYSTERESIS", "GRID_SIGMA", "GRID_SPEEDS", "NOT_A_GATE",
    "VARIANTS", "RobustnessRow", "apply_overrides", "evaluate", "gate_passes",
    "parameter_grid", "variant_configs",
]
