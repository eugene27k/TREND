"""US-T01 AC 2 — cross-strategy isolation.

"a test inserts rows from both strategies into one test database and asserts no
query in the shared layer returns cross-strategy data."

The test below is reflective on purpose: it walks *every* read method on *every*
repository rather than a hand-picked list, so a repository added later without a
``WHERE strategy = ?`` clause fails here instead of in production.
"""

from __future__ import annotations

import inspect

import pytest

from aegis.core.types import (
    AccountState,
    Alert,
    DailyBar,
    EquityPoint,
    Fill,
    FundingRate,
    IncomeType,
    LedgerEntry,
    MetricValue,
    Order,
    OrderStatus,
    OrderType,
    Position,
    RiskModel,
    Severity,
    Side,
    SignalResult,
    Strategy,
    SymbolInfo,
    SymbolTarget,
    Targets,
    TimeInForce,
    UniverseEntry,
    UniverseResult,
)
from aegis.storage.db import open_db
from aegis.storage.repositories import Repositories, _Repo

DAY = "2026-09-08"
TS = 1_757_289_600_000


def _info(symbol: str) -> SymbolInfo:
    return SymbolInfo(symbol, symbol[:-4], "USDT", "TRADING", "PERPETUAL", 0.1, 0.001, 0.001,
                      100.0, 1, 3)


def _populate(repos: Repositories, symbol: str, amount: float) -> None:
    """Write one row into every shared and TREND table for this strategy."""
    st = repos.strategy
    repos.symbol_meta.upsert_many([_info(symbol)], TS)
    repos.ledger.add_many([
        LedgerEntry(st, TS, IncomeType.FUNDING_FEE, "USDT", amount, symbol, f"tran-{symbol}"),
        LedgerEntry(st, TS, IncomeType.COMMISSION, "USDT", -amount, symbol, f"com-{symbol}"),
    ])
    repos.ledger.set_sync_cursor(TS, TS)
    acct = AccountState(TS, amount, amount, 0.0, amount, 0.0, 0.0)
    pos = Position(symbol, 1.0, 100.0, 100.0)
    repos.snapshots.add(acct, [pos], gross=amount, net=amount)
    repos.equity.upsert(DAY, EquityPoint(TS, amount, 0.0, 1.0, 1.0), peak_index=1.0, drawdown=0.0)
    repos.positions.replace_all([pos], TS)
    repos.orders.upsert(Order(f"o-{symbol}", f"c-{symbol}", symbol, Side.BUY, OrderType.LIMIT, 1.0,
                              100.0, TimeInForce.GTX, False, OrderStatus.NEW, strategy=st,
                              rebalance_id="rb-1", slice_id="sl-1"))
    repos.fills.add_many([Fill(f"t-{symbol}", f"o-{symbol}", symbol, Side.BUY, 1.0, 100.0, 0.02,
                               "USDT", True, TS, strategy=st, rebalance_id="rb-1",
                               slice_id="sl-1")])
    repos.universe.save(UniverseResult("2026-09", (UniverseEntry(symbol, 1, amount, 500, True,
                                                                "top-16 by volume"),)), TS)
    repos.bars.upsert_many([DailyBar(symbol, __import__("datetime").date.fromisoformat(DAY),
                                     100.0, 101.0, 99.0, 100.5, 10.0, amount, TS, TS + 1)])
    repos.signals.save_many(DAY, [SignalResult(symbol, (1.0,) * 3, (1.0,) * 3, (1.0,) * 3,
                                               (0.5,) * 3, 0.5, bar_ts_ms=TS)], TS)
    repos.risk_model.save(DAY, RiskModel((symbol,), {symbol: 0.5}, ((1.0,),), 0.0), TS)
    repos.rebalances.create("rb-1", DAY, TS, equity=amount)
    repos.targets.save("rb-1", Targets(
        (SymbolTarget(symbol, 0.5, 0.5, amount, amount),), 0.1, 0.5, 0.2, 2.0, 1.0, amount))
    repos.slices.create("sl-1", "rb-1", symbol, 0, Side.BUY, 1.0, False, TS)
    repos.governor.record(TS, 0.05, 1.0, 1.0, "check")
    repos.symbol_pnl.upsert_many(DAY, [{"symbol": symbol, "side": "long", "net_pnl": amount}])
    repos.trades.upsert({"trade_key": f"{symbol}:{TS}", "symbol": symbol, "side": "long",
                         "open_ts": TS, "close_ts": TS + 1000, "pnl": amount})
    repos.illiquid.flag(symbol, TS, TS + 86_400_000, "test")
    repos.illiquid.record_failure(symbol, DAY, "test")
    repos.metrics.save_many([MetricValue(st, "sharpe", "30d", amount, TS, 30)])
    repos.alerts.add(Alert(st, TS, Severity.CRITICAL, "TEST", f"alert for {symbol}"))
    repos.reports.save("daily", DAY, f"body for {symbol}", TS)
    repos.state.save(state="IDLE", phase="P1_PAPER", paused=False, stopped=False, safe_mode=False,
                     halt_reason="", governor_g=1.0, blocks=[], context={"symbol": symbol},
                     now_ms=TS)
    repos.state.log_control("start", "op", "test", {"symbol": symbol}, TS)
    repos.approvals.add("P1_PAPER", True, "op", f"approval for {symbol}", {}, amount, TS)
    repos.heartbeats.add(TS, True, symbol)
    repos.reconciliations.add(TS, "positions", True, symbol, [])
    repos.funding.upsert_many([FundingRate(symbol, TS, 0.0001, 8.0)])
    repos.tracking.upsert(DAY, live_pnl=amount, ref_pnl=amount, cum_live=amount, cum_ref=amount)


@pytest.fixture
def both() -> tuple[Repositories, Repositories]:
    db = open_db(":memory:")
    trend = Repositories(db, Strategy.TREND)
    carry = Repositories(db, Strategy.CARRY)
    _populate(trend, "TRNDUSDT", 111.0)
    _populate(carry, "CRRYUSDT", 222.0)
    return trend, carry


def _read_methods(repo: object) -> list[str]:
    """Public zero-required-argument readers — everything we can call blind."""
    out = []
    for name, fn in inspect.getmembers(repo, predicate=inspect.ismethod):
        if name.startswith("_"):
            continue
        sig = inspect.signature(fn)
        required = [
            p for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        ]
        if not required:
            out.append(name)
    return out


def _contains(value: object, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_contains(k, needle) or _contains(v, needle) for k, v in value.items())
    if isinstance(value, list | tuple | set):
        return any(_contains(v, needle) for v in value)
    if hasattr(value, "__dict__"):
        return _contains(vars(value), needle)
    if hasattr(value, "__slots__"):
        return any(_contains(getattr(value, s, None), needle) for s in value.__slots__)
    return False


def test_us_t01_ac2_no_repository_read_returns_the_other_strategy(both) -> None:
    trend, carry = both
    checked = 0
    for repo_name in Repositories.__slots__:
        if repo_name in ("db", "strategy"):
            continue
        for owner, foreign_symbol in ((trend, "CRRYUSDT"), (carry, "TRNDUSDT")):
            repo = getattr(owner, repo_name)
            for method in _read_methods(repo):
                result = getattr(repo, method)()
                checked += 1
                assert not _contains(result, foreign_symbol), (
                    f"{repo_name}.{method}() leaked {foreign_symbol} to {owner.strategy}"
                )
    assert checked > 40, f"only {checked} reads exercised — the sweep is not covering the layer"


def test_us_t01_ac2_every_repository_binds_its_strategy() -> None:
    db = open_db(":memory:")
    repos = Repositories(db, Strategy.TREND)
    for repo_name in Repositories.__slots__:
        if repo_name in ("db", "strategy"):
            continue
        repo = getattr(repos, repo_name)
        assert repo.strategy is Strategy.TREND, f"{repo_name} does not carry its strategy"


def test_us_t01_ac2_shared_tables_hold_both_strategies_side_by_side(both) -> None:
    trend, _carry = both
    # The point of the isolation guarantee: the rows really are in one file.
    for table in ("ledger", "fills", "orders", "alerts", "metrics", "targets", "daily_bars"):
        distinct = trend.db.query(f"SELECT DISTINCT strategy FROM {table} ORDER BY strategy")
        assert [r["strategy"] for r in distinct] == ["CARRY", "TREND"], table


def test_us_t01_ac2_counts_are_per_strategy(both) -> None:
    trend, carry = both
    assert trend.ledger.count() == 2
    assert carry.ledger.count() == 2
    assert trend.db.scalar("SELECT COUNT(*) FROM ledger") == 4


def test_repositories_expose_every_declared_slot() -> None:
    db = open_db(":memory:")
    repos = Repositories(db, Strategy.TREND)
    for name in Repositories.__slots__:
        assert getattr(repos, name) is not None, name


def test_base_repo_binds_strategy_string() -> None:
    db = open_db(":memory:")
    assert _Repo(db, Strategy.TREND).s == "TREND"
