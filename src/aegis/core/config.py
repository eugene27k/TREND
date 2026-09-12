"""Typed configuration (Appendix A) loaded from YAML with env-var overrides.

Every tunable in the PRD lives here with its Appendix A default, so that
Invariant 7 ("no live parameter re-optimisation") is enforceable: the engine
never writes to a config object, and an operator change is a file change that
shows up in the parameter snapshot stored with every backtest and rebalance.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aegis.core.types import Mode, Strategy

_ENV_PREFIX = "AEGIS_"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# Shared sections (CARRY Locked Decisions 3-11, 13, 15-17)
# --------------------------------------------------------------------------- #


class AccountConfig(_Base):
    sub_account_name: str = "trend-01"
    api_key: str = ""
    api_secret: str = ""
    leverage: int = 5
    margin_type: Literal["CROSSED", "ISOLATED"] = "CROSSED"
    position_mode_one_way: bool = True
    multi_assets_mode: bool = False
    bnb_fee_discount: bool = True
    assert_sub_account: bool = True

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def _from_env(cls, v: Any) -> Any:
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            return os.environ.get(v[2:-1], "")
        return v


class ExchangeConfig(_Base):
    rest_base_live: str = "https://fapi.binance.com"
    rest_base_demo: str = "https://testnet.binancefuture.com"
    ws_base_live: str = "wss://fstream.binance.com"
    ws_base_demo: str = "wss://stream.binancefuture.com"
    recv_window_ms: int = 5000
    request_timeout_s: float = 10.0
    max_retries: int = 5
    retry_backoff_s: float = 1.0
    rate_limit_weight_per_min: int = 2400
    public_data_base: str = "https://data.binance.vision"


class UniverseConfig(_Base):
    size: int = 16
    min_history_days: int = 400
    volume_window_days: int = 30
    force_include: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    exclude_bases: tuple[str, ...] = ("USDC", "FDUSD", "DAI", "TUSD", "USDP", "EURI", "EUR")
    quote_asset: str = "USDT"
    refresh_day_utc: int = 1
    refresh_time_utc: str = "00:05"
    status_watch_minutes: int = 60


class SignalConfig(_Base):
    pairs: tuple[tuple[int, int], ...] = ((8, 24), (16, 48), (32, 96))
    price_std_window: int = 63
    y_std_window: int = 250
    response_norm: float = 0.89
    clip: float = 1.0
    ddof: int = 1

    @field_validator("pairs", mode="before")
    @classmethod
    def _tuples(cls, v: Any) -> Any:
        if isinstance(v, list | tuple):
            return tuple(tuple(int(x) for x in p) for p in v)
        return v

    @model_validator(mode="after")
    def _check(self) -> SignalConfig:
        for short, long in self.pairs:
            if short >= long:
                raise ValueError(f"signal pair {(short, long)}: short span must be < long span")
        if self.response_norm <= 0:
            raise ValueError("signal.response_norm must be > 0")
        return self

    @property
    def warmup_days(self) -> int:
        """Bars needed before the first fully-warm signal."""
        longest = max(long for _, long in self.pairs)
        return self.price_std_window + self.y_std_window + longest


class VolConfig(_Base):
    half_life_days: float = 10.0
    floor: float = 0.30
    cap: float = 3.00
    min_obs: int = 20
    annualisation_days: int = 365


class CovConfig(_Base):
    half_life_days: float = 20.0
    min_obs: int = 60
    annualisation_days: int = 365


class SizingConfig(_Base):
    sigma_target_asset: float = 0.25
    sigma_target_portfolio: float = 0.20
    conviction_full: float = 0.5
    s_max: float = 3.0
    min_target_frac: float = 0.001
    universe_size_divisor: int | None = None  # defaults to universe.size (fixed N = 16)


class CapsConfig(_Base):
    gross: float = 2.5
    net: float = 1.5
    single: float = 0.25


class FundingConfig(_Base):
    haircut_threshold: float = 0.30
    haircut: float = 0.5
    default_interval_hours: float = 8.0


class GovernorConfig(_Base):
    down: dict[float, float] = Field(default_factory=lambda: {0.12: 0.5, 0.20: 0.25})
    up: dict[float, float] = Field(default_factory=lambda: {0.15: 0.5, 0.08: 1.0})

    @field_validator("down", "up", mode="before")
    @classmethod
    def _floats(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {float(k): float(x) for k, x in v.items()}
        return v


class RebalanceConfig(_Base):
    time_utc: str = "00:05"
    end_utc: str = "01:00"
    bar_fetch_time_utc: str = "00:02"
    bar_deadline_utc: str = "00:04"
    hysteresis_frac: float = 0.10
    hysteresis_equity_frac: float = 0.0025
    illiquid_days: int = 3
    failure_days: int = 3
    failure_completion_pct: float = 50.0


class ExecConfig(_Base):
    repeg_s: int = 30
    escalate_s: int = 300
    risk_escalate_s: int = 60
    clip_frac_of_minute: float = 0.005
    max_slices: int = 30
    max_twap_min: int = 30
    slippage_bps: dict[str, float] = Field(
        default_factory=lambda: {"BTCUSDT": 2.0, "ETHUSDT": 2.0, "default": 6.0}
    )
    maker_fee_fallback: float = 0.0002
    taker_fee_fallback: float = 0.0005

    def slippage_for(self, symbol: str) -> float:
        return float(self.slippage_bps.get(symbol, self.slippage_bps.get("default", 6.0)))


class RiskConfig(_Base):
    supervisor_interval_s: int = 60
    drift_interval_min: int = 60
    drift_frac: float = 0.20
    margin_amber: float = 0.20
    margin_red: float = 0.35
    red_reduce_frac: float = 0.25
    red_reduce_interval_s: int = 300
    cap_breach_fix_minutes: int = 15
    max_expected_downtime_h: float = 12.0
    shock_price: float = 0.40
    hard_halt_dd: float = 0.25
    hard_halt_backtest_multiple: float = 1.5
    daily_loss_block: float = 0.06
    adl_reduce_quantile: int = 4
    adl_check_interval_s: int = 300
    adl_reduce_frac: float = 0.25
    bnb_min_days: float = 7.0
    clock_drift_ms: int = 1000
    reconciliation_critical_minutes: int = 60


class TrackingConfig(_Base):
    min_corr: float = 0.7
    min_days: int = 30
    max_cum_diff: float = 0.03
    max_cost_ratio: float = 2.0
    max_turnover_ratio: float = 1.5
    breach_days_block: int = 14


class BacktestConfig(_Base):
    start: str = "2021-01-01"
    inventory_start: str = "2020-10-01"
    cost_model: Literal["conservative", "mixed"] = "conservative"
    maker_share_mixed: float = 0.70
    bootstrap_resamples: int = 10_000
    bootstrap_block_days: int = 91
    bootstrap_seed: int = 20260907
    archive_cache_dir: str = "data/archive"
    verify_rest_days: int = 90
    walkforward_train_months: int = 12
    walkforward_test_months: int = 6
    walkforward_step_months: int = 6
    walkforward_top_half_frac: float = 0.70


class MetricsConfig(_Base):
    job_time_utc: str = "01:10"
    periods: tuple[str, ...] = ("7d", "30d", "90d", "mtd", "ytd", "since_inception")
    min_active_days: int = 20
    beta_window_days: int = 60
    vol_window_days: int = 30


class BenchConfig(_Base):
    rf_annual: float = 0.04
    btc_symbol: str = "BTCUSDT"


class HeartbeatConfig(_Base):
    enabled: bool = False
    url: str = ""
    interval_s: int = 300
    grace_s: int = 900


class BackupConfig(_Base):
    enabled: bool = False
    litestream_replica_path: str = "backups/trend"
    restore_check_days: int = 7


class TelegramConfig(_Base):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    prefix: str = "TREND"
    daily_time_utc: str = "00:10"
    rebalance_summary_time_utc: str = "01:05"
    weekly_dow: int = 0
    monthly_day: int = 1
    repeat_critical_minutes: int = 15

    @field_validator("bot_token", "chat_id", mode="before")
    @classmethod
    def _from_env(cls, v: Any) -> Any:
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            return os.environ.get(v[2:-1], "")
        return v


class InfraConfig(_Base):
    monthly_cost_eur: float = 0.0
    host: str = "oracle-free"
    max_rss_mb: int = 1024


class StorageConfig(_Base):
    db_path: str = "data/trend.db"
    busy_timeout_ms: int = 10_000
    wal: bool = True


class PhaseConfig(_Base):
    current: str = "P0_BACKTEST"
    live_confirm: str = ""  # must equal LIVE_CONFIRM_TOKEN to start in live
    capital_usdt: float = 0.0


class AppConfig(_Base):
    """Root config object. One YAML file per strategy/deployment."""

    strategy: Strategy = Strategy.TREND
    mode: Mode = Mode.PAPER
    account: AccountConfig = Field(default_factory=AccountConfig)
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    signal: SignalConfig = Field(default_factory=SignalConfig)
    vol: VolConfig = Field(default_factory=VolConfig)
    cov: CovConfig = Field(default_factory=CovConfig)
    sizing: SizingConfig = Field(default_factory=SizingConfig)
    caps: CapsConfig = Field(default_factory=CapsConfig)
    funding: FundingConfig = Field(default_factory=FundingConfig)
    governor: GovernorConfig = Field(default_factory=GovernorConfig)
    rebalance: RebalanceConfig = Field(default_factory=RebalanceConfig)
    exec: ExecConfig = Field(default_factory=ExecConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    bench: BenchConfig = Field(default_factory=BenchConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    infra: InfraConfig = Field(default_factory=InfraConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    phase: PhaseConfig = Field(default_factory=PhaseConfig)

    @property
    def sizing_divisor(self) -> int:
        """Fixed N in Section 5.5 — the universe size, not the count of live signals."""
        return self.sizing.universe_size_divisor or self.universe.size

    def rest_base(self) -> str:
        return self.exchange.rest_base_demo if self.mode is Mode.DEMO else self.exchange.rest_base_live

    def ws_base(self) -> str:
        return self.exchange.ws_base_demo if self.mode is Mode.DEMO else self.exchange.ws_base_live

    def parameter_snapshot(self) -> dict[str, Any]:
        """Deterministic dict for backtest manifests and ``targets`` provenance."""
        data = self.model_dump(mode="json")
        for secret in ("api_key", "api_secret"):
            data.get("account", {}).pop(secret, None)
        data.get("telegram", {}).pop("bot_token", None)
        return data


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _coerce(text: str) -> Any:
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """``AEGIS_RISK__HARD_HALT_DD=0.2`` -> ``{"risk": {"hard_halt_dd": 0.2}}``."""
    env = environ if environ is not None else dict(os.environ)
    out: dict[str, Any] = {}
    for key, value in env.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        path = key[len(_ENV_PREFIX) :].lower().split("__")
        cursor = out
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = _coerce(value)
    return out


def load_config(
    path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    use_env: bool = True,
    environ: dict[str, str] | None = None,
) -> AppConfig:
    """Load YAML -> env overrides -> explicit overrides -> validated ``AppConfig``."""
    raw: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"config file not found: {p}")
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config file {p} must contain a YAML mapping")
        raw = loaded
    if use_env:
        raw = _deep_merge(raw, env_overrides(environ))
    if overrides:
        raw = _deep_merge(raw, overrides)
    return AppConfig.model_validate(raw)


__all__ = [
    "AccountConfig",
    "AppConfig",
    "BacktestConfig",
    "BackupConfig",
    "BenchConfig",
    "CapsConfig",
    "CovConfig",
    "ExchangeConfig",
    "ExecConfig",
    "FundingConfig",
    "GovernorConfig",
    "HeartbeatConfig",
    "InfraConfig",
    "MetricsConfig",
    "PhaseConfig",
    "RebalanceConfig",
    "RiskConfig",
    "SignalConfig",
    "SizingConfig",
    "StorageConfig",
    "TelegramConfig",
    "TrackingConfig",
    "UniverseConfig",
    "VolConfig",
    "env_overrides",
    "load_config",
]
