"""Repositories — the only place SQL lives.

Two rules hold everywhere in this file and are what make US-T01 AC 2 true:

1. Every repository is constructed with a ``Strategy`` and binds it into every
   statement it issues. There is no query here that can see another sleeve's
   rows, so CARRY and TREND can share one schema (and, in tests, one file)
   without ever reading each other's data.
2. Writes are idempotent — ``INSERT OR REPLACE`` on a natural key, or
   ``INSERT OR IGNORE`` where the exchange's own id is the key. A restart that
   re-processes the last hour must not double-count anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from aegis.core.types import (
    AccountState,
    Alert,
    DailyBar,
    EquityPoint,
    Fill,
    FundingRate,
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
    Targets,
    TimeInForce,
    UniverseEntry,
    UniverseResult,
)
from aegis.storage.db import Database, json_dumps, json_loads


def _day(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


class _Repo:
    """Base: holds the connection and the strategy every statement is bound to."""

    __slots__ = ("db", "strategy")

    def __init__(self, db: Database, strategy: Strategy) -> None:
        self.db = db
        self.strategy = strategy

    @property
    def s(self) -> str:
        return str(self.strategy)


# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #


class SymbolMetaRepo(_Repo):
    def upsert_many(self, infos: Iterable[SymbolInfo], ts_ms: int) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO symbol_meta (strategy, symbol, base_asset, quote_asset, status,"
            " contract_type, tick_size, step_size, min_qty, min_notional, price_precision,"
            " quantity_precision, onboard_date_ms, maker_fee, taker_fee, funding_interval_hours, updated_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (self.s, i.symbol, i.base_asset, i.quote_asset, i.status, i.contract_type, i.tick_size,
                 i.step_size, i.min_qty, i.min_notional, i.price_precision, i.quantity_precision,
                 i.onboard_date_ms, i.maker_fee, i.taker_fee, i.funding_interval_hours, ts_ms)
                for i in infos
            ],
        )

    def all(self) -> dict[str, SymbolInfo]:
        rows = self.db.query("SELECT * FROM symbol_meta WHERE strategy = ?", (self.s,))
        return {r["symbol"]: self._to_info(r) for r in rows}

    def get(self, symbol: str) -> SymbolInfo | None:
        row = self.db.query_one(
            "SELECT * FROM symbol_meta WHERE strategy = ? AND symbol = ?", (self.s, symbol)
        )
        return self._to_info(row) if row else None

    @staticmethod
    def _to_info(r: dict[str, Any]) -> SymbolInfo:
        return SymbolInfo(
            symbol=r["symbol"], base_asset=r["base_asset"], quote_asset=r["quote_asset"],
            status=r["status"], contract_type=r["contract_type"], tick_size=r["tick_size"],
            step_size=r["step_size"], min_qty=r["min_qty"], min_notional=r["min_notional"],
            price_precision=r["price_precision"], quantity_precision=r["quantity_precision"],
            onboard_date_ms=r["onboard_date_ms"], maker_fee=r["maker_fee"], taker_fee=r["taker_fee"],
            funding_interval_hours=r["funding_interval_hours"],
        )


# --------------------------------------------------------------------------- #
# Accounting
# --------------------------------------------------------------------------- #


class LedgerRepo(_Repo):
    def add_many(self, entries: Iterable[LedgerEntry]) -> int:
        rows = [
            (self.s, e.ts_ms, str(e.income_type), e.asset, e.amount, e.symbol, e.tran_id,
             e.trade_id, e.info)
            for e in entries
        ]
        if not rows:
            return 0
        before = self.count()
        self.db.executemany(
            "INSERT OR IGNORE INTO ledger (strategy, ts, income_type, asset, amount, symbol,"
            " tran_id, trade_id, info) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return self.count() - before

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM ledger WHERE strategy = ?", (self.s,)) or 0)

    def between(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM ledger WHERE strategy = ? AND ts >= ? AND ts < ? ORDER BY ts, id",
            (self.s, start_ms, end_ms),
        )

    def sum_by_type(self, start_ms: int, end_ms: int) -> dict[str, float]:
        rows = self.db.query(
            "SELECT income_type, SUM(amount) AS total FROM ledger"
            " WHERE strategy = ? AND ts >= ? AND ts < ? GROUP BY income_type",
            (self.s, start_ms, end_ms),
        )
        return {r["income_type"]: float(r["total"] or 0.0) for r in rows}

    def sum_by_symbol(self, income_type: str, start_ms: int, end_ms: int) -> dict[str, float]:
        rows = self.db.query(
            "SELECT symbol, SUM(amount) AS total FROM ledger"
            " WHERE strategy = ? AND income_type = ? AND ts >= ? AND ts < ? GROUP BY symbol",
            (self.s, income_type, start_ms, end_ms),
        )
        return {(r["symbol"] or ""): float(r["total"] or 0.0) for r in rows}

    def last_ts(self) -> int | None:
        v = self.db.scalar("SELECT MAX(ts) FROM ledger WHERE strategy = ?", (self.s,))
        return int(v) if v is not None else None

    def sync_cursor(self) -> int | None:
        v = self.db.scalar("SELECT last_ts FROM income_sync_state WHERE strategy = ?", (self.s,))
        return int(v) if v is not None else None

    def set_sync_cursor(self, ts_ms: int, now_ms: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO income_sync_state (strategy, last_ts, updated_ts) VALUES (?,?,?)",
            (self.s, ts_ms, now_ms),
        )


class SnapshotRepo(_Repo):
    def add(self, account: AccountState, positions: Sequence[Position], *, gross: float = 0.0,
            net: float = 0.0) -> None:
        self.db.execute(
            "INSERT INTO snapshots (strategy, ts, wallet_balance, margin_balance, unrealized_pnl,"
            " available_balance, maint_margin, initial_margin, gross_notional, net_notional,"
            " margin_ratio, positions_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.s, account.ts_ms, account.wallet_balance, account.margin_balance,
             account.unrealized_pnl, account.available_balance, account.maint_margin,
             account.initial_margin, gross, net, account.margin_ratio,
             json_dumps([{"symbol": p.symbol, "qty": p.qty, "entry": p.entry_price,
                          "mark": p.mark_price, "upnl": p.unrealized_pnl,
                          "adl": p.adl_quantile} for p in positions])),
        )

    def latest(self) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM snapshots WHERE strategy = ? ORDER BY ts DESC LIMIT 1", (self.s,)
        )

    def between(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM snapshots WHERE strategy = ? AND ts >= ? AND ts < ? ORDER BY ts",
            (self.s, start_ms, end_ms),
        )

    def last_before(self, ts_ms: int) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM snapshots WHERE strategy = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (self.s, ts_ms),
        )

    def first(self) -> dict[str, Any] | None:
        """Oldest snapshot — the anchor the balance reconciliation sums forward from."""
        return self.db.query_one(
            "SELECT * FROM snapshots WHERE strategy = ? ORDER BY ts LIMIT 1", (self.s,)
        )


class EquityCurveRepo(_Repo):
    def upsert(self, day: date | str, point: EquityPoint, *, peak_index: float, drawdown: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO equity_curve (strategy, day, ts, equity, net_transfer,"
            " twr_factor, twr_index, peak_index, drawdown) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.s, _day(day), point.ts_ms, point.equity, point.net_transfer, point.twr_factor,
             point.twr_index, peak_index, drawdown),
        )

    def all(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM equity_curve WHERE strategy = ? ORDER BY day", (self.s,))

    def latest(self) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM equity_curve WHERE strategy = ? ORDER BY day DESC LIMIT 1", (self.s,)
        )

    def points(self) -> list[EquityPoint]:
        return [
            EquityPoint(ts_ms=r["ts"], equity=r["equity"], net_transfer=r["net_transfer"],
                        twr_factor=r["twr_factor"], twr_index=r["twr_index"])
            for r in self.all()
        ]


# --------------------------------------------------------------------------- #
# Trading
# --------------------------------------------------------------------------- #


class OrderRepo(_Repo):
    def upsert(self, order: Order) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO orders (strategy, order_id, client_order_id, symbol, side,"
            " order_type, qty, price, time_in_force, reduce_only, status, filled_qty, avg_price,"
            " created_ts, updated_ts, rebalance_id, slice_id, intent)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.s, order.order_id, order.client_order_id, order.symbol, str(order.side),
             str(order.order_type), order.qty, order.price, str(order.time_in_force),
             int(order.reduce_only), str(order.status), order.filled_qty, order.avg_price,
             order.created_ts_ms, order.updated_ts_ms, order.rebalance_id, order.slice_id,
             order.intent),
        )

    def get(self, order_id: str) -> Order | None:
        row = self.db.query_one(
            "SELECT * FROM orders WHERE strategy = ? AND order_id = ?", (self.s, order_id)
        )
        return self._to_order(row) if row else None

    def for_rebalance(self, rebalance_id: str) -> list[Order]:
        rows = self.db.query(
            "SELECT * FROM orders WHERE strategy = ? AND rebalance_id = ? ORDER BY created_ts",
            (self.s, rebalance_id),
        )
        return [self._to_order(r) for r in rows]

    def open_orders(self) -> list[Order]:
        rows = self.db.query(
            "SELECT * FROM orders WHERE strategy = ? AND status IN ('NEW','PARTIALLY_FILLED')"
            " ORDER BY created_ts",
            (self.s,),
        )
        return [self._to_order(r) for r in rows]

    @staticmethod
    def _to_order(r: dict[str, Any]) -> Order:
        return Order(
            order_id=r["order_id"], client_order_id=r["client_order_id"], symbol=r["symbol"],
            side=Side(r["side"]), order_type=OrderType(r["order_type"]), qty=r["qty"],
            price=r["price"], time_in_force=TimeInForce(r["time_in_force"]),
            reduce_only=bool(r["reduce_only"]), status=OrderStatus(r["status"]),
            filled_qty=r["filled_qty"], avg_price=r["avg_price"], created_ts_ms=r["created_ts"],
            updated_ts_ms=r["updated_ts"], strategy=Strategy(r["strategy"]),
            rebalance_id=r["rebalance_id"], slice_id=r["slice_id"], intent=r["intent"],
        )


class FillRepo(_Repo):
    def add_many(self, fills: Iterable[Fill]) -> int:
        rows = [
            (self.s, f.trade_id, f.order_id, f.symbol, str(f.side), f.qty, f.price, f.fee,
             f.fee_asset, int(f.is_maker), f.realized_pnl, f.ts_ms, f.rebalance_id, f.slice_id,
             f.decision_mid, f.slippage_bps)
            for f in fills
        ]
        if not rows:
            return 0
        before = self.count()
        self.db.executemany(
            "INSERT OR IGNORE INTO fills (strategy, trade_id, order_id, symbol, side, qty, price,"
            " fee, fee_asset, is_maker, realized_pnl, ts, rebalance_id, slice_id, decision_mid,"
            " slippage_bps) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return self.count() - before

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM fills WHERE strategy = ?", (self.s,)) or 0)

    def between(self, start_ms: int, end_ms: int) -> list[Fill]:
        rows = self.db.query(
            "SELECT * FROM fills WHERE strategy = ? AND ts >= ? AND ts < ? ORDER BY ts",
            (self.s, start_ms, end_ms),
        )
        return [self._to_fill(r) for r in rows]

    def for_rebalance(self, rebalance_id: str) -> list[Fill]:
        rows = self.db.query(
            "SELECT * FROM fills WHERE strategy = ? AND rebalance_id = ? ORDER BY ts",
            (self.s, rebalance_id),
        )
        return [self._to_fill(r) for r in rows]

    def last_ts(self, symbol: str | None = None) -> int | None:
        if symbol:
            v = self.db.scalar(
                "SELECT MAX(ts) FROM fills WHERE strategy = ? AND symbol = ?", (self.s, symbol)
            )
        else:
            v = self.db.scalar("SELECT MAX(ts) FROM fills WHERE strategy = ?", (self.s,))
        return int(v) if v is not None else None

    @staticmethod
    def _to_fill(r: dict[str, Any]) -> Fill:
        return Fill(
            trade_id=r["trade_id"], order_id=r["order_id"], symbol=r["symbol"], side=Side(r["side"]),
            qty=r["qty"], price=r["price"], fee=r["fee"], fee_asset=r["fee_asset"],
            is_maker=bool(r["is_maker"]), ts_ms=r["ts"], realized_pnl=r["realized_pnl"],
            strategy=Strategy(r["strategy"]), rebalance_id=r["rebalance_id"],
            slice_id=r["slice_id"], decision_mid=r["decision_mid"], slippage_bps=r["slippage_bps"],
        )


class PositionRepo(_Repo):
    def replace_all(self, positions: Iterable[Position], ts_ms: int) -> None:
        with self.db.transaction():
            self.db.execute("DELETE FROM positions WHERE strategy = ?", (self.s,))
            self.db.executemany(
                "INSERT INTO positions (strategy, symbol, qty, entry_price, mark_price,"
                " unrealized_pnl, leverage, liquidation_price, adl_quantile, ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(self.s, p.symbol, p.qty, p.entry_price, p.mark_price, p.unrealized_pnl,
                  p.leverage, p.liquidation_price, p.adl_quantile, ts_ms)
                 for p in positions if p.qty != 0.0],
            )

    def all(self) -> dict[str, Position]:
        rows = self.db.query("SELECT * FROM positions WHERE strategy = ?", (self.s,))
        return {
            r["symbol"]: Position(
                symbol=r["symbol"], qty=r["qty"], entry_price=r["entry_price"],
                mark_price=r["mark_price"], unrealized_pnl=r["unrealized_pnl"],
                leverage=r["leverage"], liquidation_price=r["liquidation_price"],
                adl_quantile=r["adl_quantile"], ts_ms=r["ts"],
            )
            for r in rows
        }


# --------------------------------------------------------------------------- #
# TREND strategy artefacts
# --------------------------------------------------------------------------- #


class UniverseRepo(_Repo):
    def save(self, result: UniverseResult, now_ms: int) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO universe_history (strategy, month, symbol, rank,"
            " median_quote_volume_30d, history_days, included, reason, created_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [(self.s, result.month, e.symbol, e.rank, e.median_quote_volume_30d, e.history_days,
              int(e.included), e.reason, now_ms) for e in result.entries],
        )

    def month(self, month: str) -> UniverseResult | None:
        rows = self.db.query(
            "SELECT * FROM universe_history WHERE strategy = ? AND month = ? ORDER BY rank, symbol",
            (self.s, month),
        )
        if not rows:
            return None
        return UniverseResult(
            month=month,
            entries=tuple(
                UniverseEntry(symbol=r["symbol"], rank=r["rank"],
                              median_quote_volume_30d=r["median_quote_volume_30d"],
                              history_days=r["history_days"], included=bool(r["included"]),
                              reason=r["reason"])
                for r in rows
            ),
        )

    def symbols(self, month: str) -> list[str]:
        rows = self.db.query(
            "SELECT symbol FROM universe_history WHERE strategy = ? AND month = ? AND included = 1"
            " ORDER BY rank, symbol",
            (self.s, month),
        )
        return [r["symbol"] for r in rows]

    def latest_month(self) -> str | None:
        return self.db.scalar(
            "SELECT MAX(month) FROM universe_history WHERE strategy = ?", (self.s,)
        )

    def months(self) -> list[str]:
        return [
            r["month"]
            for r in self.db.query(
                "SELECT DISTINCT month FROM universe_history WHERE strategy = ? ORDER BY month",
                (self.s,),
            )
        ]


class BarRepo(_Repo):
    def upsert_many(self, bars: Iterable[DailyBar]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO daily_bars (strategy, symbol, day, open, high, low, close,"
            " volume, quote_volume, open_time, close_time, source, filled)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(self.s, b.symbol, _day(b.day), b.open, b.high, b.low, b.close, b.volume,
              b.quote_volume, b.open_time_ms, b.close_time_ms, b.source, int(b.filled))
             for b in bars],
        )

    def series(self, symbol: str, *, start: date | str | None = None,
               end: date | str | None = None, limit: int | None = None) -> list[DailyBar]:
        sql = "SELECT * FROM daily_bars WHERE strategy = ? AND symbol = ?"
        params: list[Any] = [self.s, symbol]
        if start is not None:
            sql += " AND day >= ?"
            params.append(_day(start))
        if end is not None:
            sql += " AND day <= ?"
            params.append(_day(end))
        sql += " ORDER BY day"
        rows = self.db.query(sql, params)
        if limit is not None and len(rows) > limit:
            rows = rows[-limit:]
        return [self._to_bar(r) for r in rows]

    def closes(self, symbol: str, *, end: date | str | None = None,
               limit: int | None = None) -> list[float]:
        return [b.close for b in self.series(symbol, end=end, limit=limit)]

    def realised_series(self, symbol: str, *, start: date | str | None = None,
                        end: date | str | None = None, limit: int | None = None) -> list[DailyBar]:
        """Bars that actually traded — forward-filled rows excluded (US-T03 AC 2).

        Separate from ``series`` rather than a flag on it so that P&L, fee and
        liquidity queries cannot pick up a synthetic bar by forgetting an
        argument. ``limit`` counts realised bars, so a gap does not shorten the
        window the caller asked for.
        """
        sql = "SELECT * FROM daily_bars WHERE strategy = ? AND symbol = ? AND filled = 0"
        params: list[Any] = [self.s, symbol]
        if start is not None:
            sql += " AND day >= ?"
            params.append(_day(start))
        if end is not None:
            sql += " AND day <= ?"
            params.append(_day(end))
        sql += " ORDER BY day"
        rows = self.db.query(sql, params)
        if limit is not None and len(rows) > limit:
            rows = rows[-limit:]
        return [self._to_bar(r) for r in rows]

    def latest_day(self, symbol: str) -> str | None:
        return self.db.scalar(
            "SELECT MAX(day) FROM daily_bars WHERE strategy = ? AND symbol = ?", (self.s, symbol)
        )

    def has_day(self, symbol: str, day: date | str) -> bool:
        return bool(self.db.scalar(
            "SELECT 1 FROM daily_bars WHERE strategy = ? AND symbol = ? AND day = ?",
            (self.s, symbol, _day(day)),
        ))

    def count(self, symbol: str) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM daily_bars WHERE strategy = ? AND symbol = ?", (self.s, symbol)
        ) or 0)

    @staticmethod
    def _to_bar(r: dict[str, Any]) -> DailyBar:
        return DailyBar(
            symbol=r["symbol"], day=date.fromisoformat(r["day"]), open=r["open"], high=r["high"],
            low=r["low"], close=r["close"], volume=r["volume"], quote_volume=r["quote_volume"],
            open_time_ms=r["open_time"], close_time_ms=r["close_time"], source=r["source"],
            filled=bool(r["filled"]),
        )


class SignalRepo(_Repo):
    def save_many(self, day: date | str, results: Iterable[SignalResult], now_ms: int) -> None:
        rows = []
        for r in results:
            x = list(r.x) + [None] * 3
            y = list(r.y) + [None] * 3
            z = list(r.z) + [None] * 3
            u = list(r.u) + [None] * 3
            rows.append((self.s, _day(day), r.symbol, *_nan(x[:3]), *_nan(y[:3]), *_nan(z[:3]),
                         *_nan(u[:3]), r.signal, int(r.warm), r.bar_ts_ms, now_ms))
        self.db.executemany(
            "INSERT OR REPLACE INTO signal_snapshots (strategy, day, symbol, x1,x2,x3, y1,y2,y3,"
            " z1,z2,z3, u1,u2,u3, signal, warm, bar_ts, created_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    def day(self, day: date | str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM signal_snapshots WHERE strategy = ? AND day = ? ORDER BY symbol",
            (self.s, _day(day)),
        )

    def history(self, symbol: str, limit: int = 90) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM signal_snapshots WHERE strategy = ? AND symbol = ? ORDER BY day DESC"
            " LIMIT ?",
            (self.s, symbol, limit),
        )
        return list(reversed(rows))

    def between(self, start: date | str, end: date | str) -> list[dict[str, Any]]:
        """Every snapshot in a closed day range — the signal-statistics window query."""
        return self.db.query(
            "SELECT * FROM signal_snapshots WHERE strategy = ? AND day >= ? AND day <= ?"
            " ORDER BY day, symbol",
            (self.s, _day(start), _day(end)),
        )

    def latest_day(self) -> str | None:
        return self.db.scalar("SELECT MAX(day) FROM signal_snapshots WHERE strategy = ?", (self.s,))


def _nan(values: list[Any]) -> list[Any]:
    """SQLite stores NaN as NULL — make that explicit rather than accidental."""
    out = []
    for v in values:
        if v is None:
            out.append(None)
        else:
            fv = float(v)
            out.append(None if fv != fv else fv)
    return out


class RiskModelRepo(_Repo):
    def save(self, day: date | str, model: RiskModel, now_ms: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO risk_model_snapshots (strategy, day, vols_json, corr_json,"
            " symbols_json, avg_corr, created_ts) VALUES (?,?,?,?,?,?,?)",
            (self.s, _day(day), json_dumps(model.vols), json_dumps([list(r) for r in model.corr]),
             json_dumps(list(model.symbols)), model.avg_corr, now_ms),
        )

    def get(self, day: date | str) -> RiskModel | None:
        row = self.db.query_one(
            "SELECT * FROM risk_model_snapshots WHERE strategy = ? AND day = ?", (self.s, _day(day))
        )
        if not row:
            return None
        symbols = tuple(json_loads(row["symbols_json"], []))
        return RiskModel(
            symbols=symbols, vols=json_loads(row["vols_json"], {}),
            corr=tuple(tuple(r) for r in json_loads(row["corr_json"], [])),
            avg_corr=row["avg_corr"],
        )

    def latest(self) -> RiskModel | None:
        day = self.db.scalar("SELECT MAX(day) FROM risk_model_snapshots WHERE strategy = ?", (self.s,))
        return self.get(day) if day else None


class RebalanceRepo(_Repo):
    def create(self, rebalance_id: str, day: date | str, started_ts: int, *, kind: str = "scheduled",
               equity: float = 0.0, governor_g: float = 1.0, order_plan: Any = (),
               decision_mids: dict[str, float] | None = None, planned_notional: float = 0.0) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO rebalances (strategy, rebalance_id, day, started_ts, status,"
            " kind, order_plan_json, equity, governor_g, decision_mids_json, planned_notional, cursor)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
            (self.s, rebalance_id, _day(day), started_ts, "planned", kind,
             json_dumps(_plan_rows(order_plan)), equity, governor_g,
             json_dumps(decision_mids or {}), planned_notional),
        )

    def set_status(self, rebalance_id: str, status: str, *, ended_ts: int | None = None) -> None:
        self.db.execute(
            "UPDATE rebalances SET status = ?, ended_ts = COALESCE(?, ended_ts)"
            " WHERE strategy = ? AND rebalance_id = ?",
            (status, ended_ts, self.s, rebalance_id),
        )

    def set_cursor(self, rebalance_id: str, cursor: int) -> None:
        self.db.execute(
            "UPDATE rebalances SET cursor = ? WHERE strategy = ? AND rebalance_id = ?",
            (cursor, self.s, rebalance_id),
        )

    def finish(self, rebalance_id: str, *, ended_ts: int, status: str, completion_pct: float,
               traded_notional: float, fees: float, avg_slippage_bps: float, maker_ratio: float,
               residuals: Any) -> None:
        self.db.execute(
            "UPDATE rebalances SET ended_ts = ?, status = ?, completion_pct = ?,"
            " traded_notional = ?, fees = ?, avg_slippage_bps = ?, maker_ratio = ?,"
            " residuals_json = ? WHERE strategy = ? AND rebalance_id = ?",
            (ended_ts, status, completion_pct, traded_notional, fees, avg_slippage_bps,
             maker_ratio, json_dumps(residuals), self.s, rebalance_id),
        )

    def get(self, rebalance_id: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM rebalances WHERE strategy = ? AND rebalance_id = ?", (self.s, rebalance_id)
        )

    def unfinished(self) -> dict[str, Any] | None:
        """The rebalance to resume after a restart (US-T09 AC 4)."""
        return self.db.query_one(
            "SELECT * FROM rebalances WHERE strategy = ? AND status IN ('planned','running')"
            " ORDER BY started_ts DESC LIMIT 1",
            (self.s,),
        )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM rebalances WHERE strategy = ? ORDER BY started_ts DESC LIMIT ?",
            (self.s, limit),
        )

    def for_day(self, day: date | str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM rebalances WHERE strategy = ? AND day = ? ORDER BY started_ts",
            (self.s, _day(day)),
        )

    def between(self, start: date | str, end: date | str, *, kind: str | None = None) -> list[dict[str, Any]]:
        """Rebalances in a closed day range — the analytics window query."""
        sql = "SELECT * FROM rebalances WHERE strategy = ? AND day >= ? AND day <= ?"
        params: list[Any] = [self.s, _day(start), _day(end)]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY day, started_ts"
        return self.db.query(sql, params)

    def consecutive_failures(self, before_day: date | str, threshold_pct: float, days: int) -> int:
        """How many of the last ``days`` scheduled rebalances fell below ``threshold_pct``."""
        rows = self.db.query(
            "SELECT day, completion_pct FROM rebalances WHERE strategy = ? AND kind = 'scheduled'"
            " AND day <= ? ORDER BY day DESC LIMIT ?",
            (self.s, _day(before_day), days),
        )
        streak = 0
        for r in rows:
            if (r["completion_pct"] or 0.0) < threshold_pct:
                streak += 1
            else:
                break
        return streak


def _plan_rows(plan: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in plan or ():
        if isinstance(p, dict):
            out.append(p)
        else:
            out.append({
                "symbol": p.symbol, "side": str(p.side), "delta_notional": p.delta_notional,
                "delta_qty": p.delta_qty, "current_qty": p.current_qty, "target_qty": p.target_qty,
                "reduce_only": p.reduce_only, "risk_reducing": p.risk_reducing,
                "sequence": p.sequence, "n_slices": p.n_slices, "clip_qty": p.clip_qty,
            })
    return out


class TargetRepo(_Repo):
    def save(self, rebalance_id: str, targets: Targets) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO targets (strategy, rebalance_id, symbol, signal, vol, raw,"
            " sigma_p, conv, sigma_eff, s, g, caps_json, funding_ann, funding_haircut,"
            " target_notional, target_qty, current_qty, delta_notional, traded)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(self.s, rebalance_id, t.symbol, t.signal, t.vol, t.raw, targets.sigma_p, targets.conv,
              targets.sigma_eff, targets.s, targets.g, json_dumps(list(t.caps_applied)),
              t.funding_ann, t.funding_haircut, t.target_notional, t.target_qty, t.current_qty,
              t.delta_notional, int(t.traded))
             for t in targets.targets],
        )

    def mark_traded(self, rebalance_id: str, symbol: str, traded: bool = True) -> None:
        self.db.execute(
            "UPDATE targets SET traded = ? WHERE strategy = ? AND rebalance_id = ? AND symbol = ?",
            (int(traded), self.s, rebalance_id, symbol),
        )

    def for_rebalance(self, rebalance_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM targets WHERE strategy = ? AND rebalance_id = ? ORDER BY symbol",
            (self.s, rebalance_id),
        )

    def latest(self) -> list[dict[str, Any]]:
        rid = self.db.scalar(
            "SELECT rebalance_id FROM rebalances WHERE strategy = ? ORDER BY started_ts DESC LIMIT 1",
            (self.s,),
        )
        return self.for_rebalance(rid) if rid else []

    def latest_by_symbol(self) -> dict[str, float]:
        """Last target notional per symbol — the drift monitor's reference (US-T11 AC 1)."""
        return {r["symbol"]: r["target_notional"] for r in self.latest()}


class SliceRepo(_Repo):
    def create(self, slice_id: str, rebalance_id: str, symbol: str, seq: int, side: Side, qty: float,
               reduce_only: bool, placed_ts: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO slices (strategy, slice_id, rebalance_id, symbol, seq, side,"
            " qty, reduce_only, placed_ts, repegs, outcome, fill_qty, avg_price, taker)"
            " VALUES (?,?,?,?,?,?,?,?,?,0,'pending',0,0,0)",
            (self.s, slice_id, rebalance_id, symbol, seq, str(side), qty, int(reduce_only), placed_ts),
        )

    def bump_repegs(self, slice_id: str) -> None:
        self.db.execute(
            "UPDATE slices SET repegs = repegs + 1 WHERE strategy = ? AND slice_id = ?",
            (self.s, slice_id),
        )

    def finish(self, slice_id: str, outcome: str, fill_qty: float, avg_price: float, taker: bool,
               ended_ts: int) -> None:
        self.db.execute(
            "UPDATE slices SET outcome = ?, fill_qty = ?, avg_price = ?, taker = ?, ended_ts = ?"
            " WHERE strategy = ? AND slice_id = ?",
            (outcome, fill_qty, avg_price, int(taker), ended_ts, self.s, slice_id),
        )

    def for_rebalance(self, rebalance_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM slices WHERE strategy = ? AND rebalance_id = ? ORDER BY symbol, seq",
            (self.s, rebalance_id),
        )


class GovernorRepo(_Repo):
    def record(self, ts_ms: int, dd: float, g_before: float, g_after: float, trigger: str,
               applied: bool = False) -> None:
        self.db.execute(
            "INSERT INTO governor_state (strategy, ts, dd, g_before, g_after, trigger, applied)"
            " VALUES (?,?,?,?,?,?,?)",
            (self.s, ts_ms, dd, g_before, g_after, trigger, int(applied)),
        )

    def current_g(self) -> float:
        v = self.db.scalar(
            "SELECT g_after FROM governor_state WHERE strategy = ? ORDER BY ts DESC, id DESC LIMIT 1",
            (self.s,),
        )
        return float(v) if v is not None else 1.0

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM governor_state WHERE strategy = ? ORDER BY ts DESC LIMIT ?", (self.s, limit)
        )
        return list(reversed(rows))

    def between(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM governor_state WHERE strategy = ? AND ts >= ? AND ts < ? ORDER BY ts, id",
            (self.s, start_ms, end_ms),
        )

    def last_before(self, ts_ms: int) -> dict[str, Any] | None:
        """The state in force at ``ts_ms`` — the time-in-state walk starts here."""
        return self.db.query_one(
            "SELECT * FROM governor_state WHERE strategy = ? AND ts <= ? ORDER BY ts DESC, id DESC"
            " LIMIT 1",
            (self.s, ts_ms),
        )


class SymbolPnlRepo(_Repo):
    def upsert_many(self, day: date | str, rows: Iterable[dict[str, Any]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO symbol_pnl_daily (strategy, day, symbol, side, avg_notional,"
            " price_pnl, funding, fees, slippage, net_pnl, traded_notional, signal)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(self.s, _day(day), r["symbol"], r.get("side", "flat"), r.get("avg_notional", 0.0),
              r.get("price_pnl", 0.0), r.get("funding", 0.0), r.get("fees", 0.0),
              r.get("slippage", 0.0), r.get("net_pnl", 0.0), r.get("traded_notional", 0.0),
              r.get("signal", 0.0)) for r in rows],
        )

    def between(self, start: date | str, end: date | str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM symbol_pnl_daily WHERE strategy = ? AND day >= ? AND day <= ?"
            " ORDER BY day, symbol",
            (self.s, _day(start), _day(end)),
        )

    def by_symbol(self, start: date | str, end: date | str) -> dict[str, float]:
        rows = self.db.query(
            "SELECT symbol, SUM(net_pnl) AS total FROM symbol_pnl_daily"
            " WHERE strategy = ? AND day >= ? AND day <= ? GROUP BY symbol",
            (self.s, _day(start), _day(end)),
        )
        return {r["symbol"]: float(r["total"] or 0.0) for r in rows}

    def by_side(self, start: date | str, end: date | str) -> dict[str, float]:
        rows = self.db.query(
            "SELECT side, SUM(net_pnl) AS total FROM symbol_pnl_daily"
            " WHERE strategy = ? AND day >= ? AND day <= ? GROUP BY side",
            (self.s, _day(start), _day(end)),
        )
        return {r["side"]: float(r["total"] or 0.0) for r in rows}

    def by_month(self, start: date | str, end: date | str, *, symbol: str | None = None) -> dict[str, float]:
        """Net P&L per ``YYYY-MM`` (US-T14 AC 2), optionally for one symbol."""
        sql = (
            "SELECT substr(day, 1, 7) AS month, SUM(net_pnl) AS total FROM symbol_pnl_daily"
            " WHERE strategy = ? AND day >= ? AND day <= ?"
        )
        params: list[Any] = [self.s, _day(start), _day(end)]
        if symbol is not None:
            sql += " AND symbol = ?"
            params.append(symbol)
        rows = self.db.query(sql + " GROUP BY month ORDER BY month", params)
        return {r["month"]: float(r["total"] or 0.0) for r in rows}

    def components(self, start: date | str, end: date | str) -> dict[str, float]:
        """Period totals of each P&L component — the roll-up the reports print."""
        row = self.db.query_one(
            "SELECT SUM(price_pnl) AS price_pnl, SUM(funding) AS funding, SUM(fees) AS fees,"
            " SUM(slippage) AS slippage, SUM(net_pnl) AS net_pnl, SUM(traded_notional) AS traded_notional"
            " FROM symbol_pnl_daily WHERE strategy = ? AND day >= ? AND day <= ?",
            (self.s, _day(start), _day(end)),
        )
        keys = ("price_pnl", "funding", "fees", "slippage", "net_pnl", "traded_notional")
        return {k: float((row or {}).get(k) or 0.0) for k in keys}


class TradeRepo(_Repo):
    def upsert(self, trade: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO trades (strategy, trade_key, symbol, side, open_ts, close_ts,"
            " days, pnl, mae, max_notional, entry_signal, exit_signal)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.s, trade["trade_key"], trade["symbol"], trade["side"], trade["open_ts"],
             trade.get("close_ts"), trade.get("days", 0.0), trade.get("pnl", 0.0),
             trade.get("mae", 0.0), trade.get("max_notional", 0.0), trade.get("entry_signal", 0.0),
             trade.get("exit_signal", 0.0)),
        )

    def open_trade(self, symbol: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM trades WHERE strategy = ? AND symbol = ? AND close_ts IS NULL"
            " ORDER BY open_ts DESC LIMIT 1",
            (self.s, symbol),
        )

    def closed(self) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM trades WHERE strategy = ? AND close_ts IS NOT NULL ORDER BY open_ts",
            (self.s,),
        )

    def closed_between(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """Trades that *finished* inside the window — a trade belongs to the period it closed in."""
        return self.db.query(
            "SELECT * FROM trades WHERE strategy = ? AND close_ts IS NOT NULL AND close_ts >= ?"
            " AND close_ts < ? ORDER BY close_ts",
            (self.s, start_ms, end_ms),
        )


class IlliquidRepo(_Repo):
    def flag(self, symbol: str, flagged_ts: int, until_ts: int, reason: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO illiquid_flags (strategy, symbol, flagged_ts, until_ts, reason)"
            " VALUES (?,?,?,?,?)",
            (self.s, symbol, flagged_ts, until_ts, reason),
        )

    def active(self, now_ms: int) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM illiquid_flags WHERE strategy = ? AND cleared_ts IS NULL AND until_ts > ?"
            " ORDER BY symbol",
            (self.s, now_ms),
        )

    def active_symbols(self, now_ms: int) -> set[str]:
        return {r["symbol"] for r in self.active(now_ms)}

    def clear_all(self, now_ms: int) -> None:
        """Called at the monthly universe refresh — flags last until then (5.9 step 6)."""
        self.db.execute(
            "UPDATE illiquid_flags SET cleared_ts = ? WHERE strategy = ? AND cleared_ts IS NULL",
            (now_ms, self.s),
        )

    def record_failure(self, symbol: str, day: date | str, reason: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO rebalance_failures (strategy, symbol, day, reason)"
            " VALUES (?,?,?,?)",
            (self.s, symbol, _day(day), reason),
        )

    def consecutive_failures(self, symbol: str, days: Sequence[str]) -> int:
        """Count the trailing run of ``days`` (newest first) on which ``symbol`` failed."""
        rows = {
            r["day"]
            for r in self.db.query(
                "SELECT day FROM rebalance_failures WHERE strategy = ? AND symbol = ?",
                (self.s, symbol),
            )
        }
        streak = 0
        for d in days:
            if d in rows:
                streak += 1
            else:
                break
        return streak


# --------------------------------------------------------------------------- #
# Ops / analytics
# --------------------------------------------------------------------------- #


class MetricRepo(_Repo):
    def save_many(self, values: Iterable[MetricValue]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO metrics (strategy, name, period, as_of_ts, value, n_obs,"
            " std_error, extra_json) VALUES (?,?,?,?,?,?,?,?)",
            [(self.s, m.name, m.period, m.as_of_ts_ms, m.value, m.n_obs, m.std_error,
              json_dumps(m.extra)) for m in values],
        )

    def latest(self, period: str | None = None) -> dict[str, dict[str, Any]]:
        sql = (
            "SELECT m.* FROM metrics m JOIN (SELECT name, period, MAX(as_of_ts) AS ts FROM metrics"
            " WHERE strategy = ? GROUP BY name, period) t"
            " ON m.name = t.name AND m.period = t.period AND m.as_of_ts = t.ts"
            " WHERE m.strategy = ?"
        )
        params: list[Any] = [self.s, self.s]
        if period:
            sql += " AND m.period = ?"
            params.append(period)
        rows = self.db.query(sql, params)
        return {f"{r['name']}:{r['period']}": r for r in rows}

    def series(self, name: str, period: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM metrics WHERE strategy = ? AND name = ? AND period = ? ORDER BY as_of_ts",
            (self.s, name, period),
        )


class AlertRepo(_Repo):
    def add(self, alert: Alert) -> int:
        cur = self.db.execute(
            "INSERT INTO alerts (strategy, ts, severity, code, message, context_json)"
            " VALUES (?,?,?,?,?,?)",
            (self.s, alert.ts_ms, str(alert.severity), alert.code, alert.message,
             json_dumps(alert.context)),
        )
        return int(cur.lastrowid or 0)

    def mark_delivered(self, alert_id: int) -> None:
        self.db.execute(
            "UPDATE alerts SET delivered = 1 WHERE strategy = ? AND id = ?", (self.s, alert_id)
        )

    def undelivered(self) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM alerts WHERE strategy = ? AND delivered = 0 ORDER BY ts, id", (self.s,)
        )

    def recent(self, limit: int = 100, severity: Severity | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alerts WHERE strategy = ?"
        params: list[Any] = [self.s]
        if severity:
            sql += " AND severity = ?"
            params.append(str(severity))
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return self.db.query(sql, params)

    def last_of_code(self, code: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM alerts WHERE strategy = ? AND code = ? ORDER BY ts DESC LIMIT 1",
            (self.s, code),
        )

    def ack(self, alert_id: int, ts_ms: int) -> None:
        self.db.execute(
            "UPDATE alerts SET acked_ts = ? WHERE strategy = ? AND id = ?", (ts_ms, self.s, alert_id)
        )

    def unacked_critical(self) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM alerts WHERE strategy = ? AND severity = 'CRITICAL' AND acked_ts IS NULL"
            " ORDER BY ts",
            (self.s,),
        )


class ReportRepo(_Repo):
    def save(self, kind: str, period_key: str, body: str, ts_ms: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO reports (strategy, ts, kind, period_key, body, delivered)"
            " VALUES (?,?,?,?,?,0)",
            (self.s, ts_ms, kind, period_key, body),
        )

    def mark_delivered(self, kind: str, period_key: str) -> None:
        self.db.execute(
            "UPDATE reports SET delivered = 1 WHERE strategy = ? AND kind = ? AND period_key = ?",
            (self.s, kind, period_key),
        )

    def get(self, kind: str, period_key: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM reports WHERE strategy = ? AND kind = ? AND period_key = ?",
            (self.s, kind, period_key),
        )

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM reports WHERE strategy = ? ORDER BY ts DESC LIMIT ?", (self.s, limit)
        )


class StateRepo(_Repo):
    def save(self, *, state: str, phase: str, paused: bool, stopped: bool, safe_mode: bool,
             halt_reason: str, governor_g: float, blocks: Sequence[str], context: dict[str, Any],
             now_ms: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO engine_state (strategy, state, phase, paused, stopped,"
            " safe_mode, halt_reason, governor_g, blocks_json, context_json, updated_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.s, state, phase, int(paused), int(stopped), int(safe_mode), halt_reason,
             governor_g, json_dumps(list(blocks)), json_dumps(context), now_ms),
        )

    def load(self) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM engine_state WHERE strategy = ?", (self.s,))
        if row:
            row["blocks"] = json_loads(row["blocks_json"], [])
            row["context"] = json_loads(row["context_json"], {})
        return row

    def log_control(self, action: str, operator: str, reason: str, payload: dict[str, Any],
                    now_ms: int) -> None:
        self.db.execute(
            "INSERT INTO control_log (strategy, ts, action, operator, reason, payload_json)"
            " VALUES (?,?,?,?,?,?)",
            (self.s, now_ms, action, operator, reason, json_dumps(payload)),
        )

    def controls(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM control_log WHERE strategy = ? ORDER BY ts DESC LIMIT ?", (self.s, limit)
        )


class ApprovalRepo(_Repo):
    def add(self, phase: str, granted: bool, operator: str, reason: str, evidence: dict[str, Any],
            capital: float, now_ms: int) -> None:
        self.db.execute(
            "INSERT INTO approvals (strategy, phase, ts, granted, operator, reason, evidence_json,"
            " capital_usdt) VALUES (?,?,?,?,?,?,?,?)",
            (self.s, phase, now_ms, int(granted), operator, reason, json_dumps(evidence), capital),
        )

    def latest(self, phase: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM approvals WHERE strategy = ? AND phase = ? ORDER BY ts DESC LIMIT 1",
            (self.s, phase),
        )
        if row:
            row["evidence"] = json_loads(row["evidence_json"], {})
        return row

    def all(self) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM approvals WHERE strategy = ? ORDER BY ts DESC", (self.s,)
        )


class HeartbeatRepo(_Repo):
    def add(self, ts_ms: int, ok: bool, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO heartbeats (strategy, ts, ok, detail) VALUES (?,?,?,?)",
            (self.s, ts_ms, int(ok), detail),
        )

    def uptime_pct(self, start_ms: int, end_ms: int) -> float:
        rows = self.db.query(
            "SELECT ok FROM heartbeats WHERE strategy = ? AND ts >= ? AND ts < ?",
            (self.s, start_ms, end_ms),
        )
        if not rows:
            return 0.0
        return 100.0 * sum(1 for r in rows if r["ok"]) / len(rows)

    def prune(self, before_ms: int) -> None:
        self.db.execute("DELETE FROM heartbeats WHERE strategy = ? AND ts < ?", (self.s, before_ms))


class ReconciliationRepo(_Repo):
    def add(self, ts_ms: int, kind: str, ok: bool, detail: str, breaks: Sequence[dict[str, Any]]) -> int:
        cur = self.db.execute(
            "INSERT INTO reconciliations (strategy, ts, kind, ok, detail, breaks_json)"
            " VALUES (?,?,?,?,?,?)",
            (self.s, ts_ms, kind, int(ok), detail, json_dumps(list(breaks))),
        )
        return int(cur.lastrowid or 0)

    def resolve(self, recon_id: int, ts_ms: int) -> None:
        self.db.execute(
            "UPDATE reconciliations SET resolved_ts = ? WHERE strategy = ? AND id = ?",
            (ts_ms, self.s, recon_id),
        )

    def open_breaks(self) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM reconciliations WHERE strategy = ? AND ok = 0 AND resolved_ts IS NULL"
            " ORDER BY ts",
            (self.s,),
        )

    def latest(self) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM reconciliations WHERE strategy = ? ORDER BY ts DESC LIMIT 1", (self.s,)
        )


class FundingRepo(_Repo):
    def upsert_many(self, rates: Iterable[FundingRate]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO funding_rates (strategy, symbol, funding_time, rate,"
            " interval_hours) VALUES (?,?,?,?,?)",
            [(self.s, r.symbol, r.funding_time_ms, r.rate, r.interval_hours) for r in rates],
        )

    def history(self, symbol: str, start_ms: int | None = None,
                end_ms: int | None = None) -> list[FundingRate]:
        sql = "SELECT * FROM funding_rates WHERE strategy = ? AND symbol = ?"
        params: list[Any] = [self.s, symbol]
        if start_ms is not None:
            sql += " AND funding_time >= ?"
            params.append(start_ms)
        if end_ms is not None:
            sql += " AND funding_time < ?"
            params.append(end_ms)
        sql += " ORDER BY funding_time"
        return [
            FundingRate(symbol=r["symbol"], funding_time_ms=r["funding_time"], rate=r["rate"],
                        interval_hours=r["interval_hours"])
            for r in self.db.query(sql, params)
        ]

    def last_time(self, symbol: str) -> int | None:
        v = self.db.scalar(
            "SELECT MAX(funding_time) FROM funding_rates WHERE strategy = ? AND symbol = ?",
            (self.s, symbol),
        )
        return int(v) if v is not None else None


class TrackingRepo(_Repo):
    def upsert(self, day: date | str, **fields: Any) -> None:
        cols = ["live_pnl", "ref_pnl", "cum_live", "cum_ref", "corr_30d", "cum_diff_frac",
                "cost_ratio", "turnover_ratio", "in_bounds", "breach_days"]
        values = [fields.get(c) for c in cols]
        values[cols.index("in_bounds")] = int(fields.get("in_bounds", True))
        values[cols.index("breach_days")] = int(fields.get("breach_days", 0))
        self.db.execute(
            f"INSERT OR REPLACE INTO tracking (strategy, day, {', '.join(cols)})"
            f" VALUES (?,?,{','.join('?' * len(cols))})",
            [self.s, _day(day), *values],
        )

    def latest(self) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM tracking WHERE strategy = ? ORDER BY day DESC LIMIT 1", (self.s,)
        )

    def series(self, limit: int = 400) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM tracking WHERE strategy = ? ORDER BY day DESC LIMIT ?", (self.s, limit)
        )
        return list(reversed(rows))


class BacktestRepo:
    """Backtest artefacts are keyed by run_id, not by strategy — a run IS a strategy snapshot."""

    __slots__ = ("db", "strategy")

    def __init__(self, db: Database, strategy: Strategy) -> None:
        self.db = db
        self.strategy = strategy

    def save_run(self, run_id: str, *, created_ts: int, start_day: str, end_day: str,
                 variant: str, params: dict[str, Any], manifest: dict[str, Any], git_commit: str,
                 metrics: dict[str, Any], equity: Sequence[Any], duration_s: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO backtest_runs (run_id, strategy, created_ts, start_day, end_day,"
            " variant, params_json, manifest_json, git_commit, metrics_json, equity_json, duration_s)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, str(self.strategy), created_ts, start_day, end_day, variant,
             json_dumps(params), json_dumps(manifest), git_commit, json_dumps(metrics),
             json_dumps(list(equity)), duration_s),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,))

    def latest_run(self, variant: str = "default") -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM backtest_runs WHERE strategy = ? AND variant = ?"
            " ORDER BY created_ts DESC LIMIT 1",
            (str(self.strategy), variant),
        )

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT run_id, strategy, created_ts, start_day, end_day, variant, git_commit,"
            " duration_s FROM backtest_runs WHERE strategy = ? ORDER BY created_ts DESC LIMIT ?",
            (str(self.strategy), limit),
        )

    def save_robustness(self, run_id: str, rows: Iterable[dict[str, Any]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO robustness_reports (run_id, variant, net_pnl, sharpe, max_dd,"
            " sign_ok, detail_json) VALUES (?,?,?,?,?,?,?)",
            [(run_id, r["variant"], r["net_pnl"], r["sharpe"], r["max_dd"], int(r["sign_ok"]),
              json_dumps(r.get("detail", {}))) for r in rows],
        )

    def robustness(self, run_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM robustness_reports WHERE run_id = ? ORDER BY variant", (run_id,)
        )

    def save_walkforward(self, run_id: str, rows: Iterable[dict[str, Any]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO walkforward (run_id, window, param_set, test_sharpe, rank,"
            " n_params, default_in_top_half, is_default) VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, r["window"], r["param_set"], r["test_sharpe"], r["rank"],
              r.get("n_params", 0), int(r.get("default_in_top_half", False)),
              int(r.get("is_default", False))) for r in rows],
        )

    def walkforward(self, run_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM walkforward WHERE run_id = ? ORDER BY window, rank", (run_id,)
        )

    def save_bootstrap(self, run_id: str, horizon: str, percentiles: dict[float, float]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO bootstrap_distribution (run_id, horizon, percentile, value)"
            " VALUES (?,?,?,?)",
            [(run_id, horizon, p, v) for p, v in percentiles.items()],
        )

    def bootstrap(self, run_id: str, horizon: str = "3m") -> dict[float, float]:
        rows = self.db.query(
            "SELECT percentile, value FROM bootstrap_distribution WHERE run_id = ? AND horizon = ?",
            (run_id, horizon),
        )
        return {r["percentile"]: r["value"] for r in rows}


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #


class Repositories:
    """Every repository, bound to one database and one strategy."""

    __slots__ = (
        "alerts", "approvals", "backtest", "bars", "db", "equity", "fills", "funding", "governor",
        "heartbeats", "illiquid", "ledger", "metrics", "orders", "positions", "rebalances",
        "reconciliations", "reports", "risk_model", "signals", "slices", "snapshots", "state",
        "strategy", "symbol_meta", "symbol_pnl", "targets", "tracking", "trades", "universe",
    )

    def __init__(self, db: Database, strategy: Strategy) -> None:
        self.db = db
        self.strategy = strategy
        self.symbol_meta = SymbolMetaRepo(db, strategy)
        self.ledger = LedgerRepo(db, strategy)
        self.snapshots = SnapshotRepo(db, strategy)
        self.equity = EquityCurveRepo(db, strategy)
        self.orders = OrderRepo(db, strategy)
        self.fills = FillRepo(db, strategy)
        self.positions = PositionRepo(db, strategy)
        self.universe = UniverseRepo(db, strategy)
        self.bars = BarRepo(db, strategy)
        self.signals = SignalRepo(db, strategy)
        self.risk_model = RiskModelRepo(db, strategy)
        self.rebalances = RebalanceRepo(db, strategy)
        self.targets = TargetRepo(db, strategy)
        self.slices = SliceRepo(db, strategy)
        self.governor = GovernorRepo(db, strategy)
        self.symbol_pnl = SymbolPnlRepo(db, strategy)
        self.trades = TradeRepo(db, strategy)
        self.illiquid = IlliquidRepo(db, strategy)
        self.metrics = MetricRepo(db, strategy)
        self.alerts = AlertRepo(db, strategy)
        self.reports = ReportRepo(db, strategy)
        self.state = StateRepo(db, strategy)
        self.approvals = ApprovalRepo(db, strategy)
        self.heartbeats = HeartbeatRepo(db, strategy)
        self.reconciliations = ReconciliationRepo(db, strategy)
        self.funding = FundingRepo(db, strategy)
        self.tracking = TrackingRepo(db, strategy)
        self.backtest = BacktestRepo(db, strategy)

    def close(self) -> None:
        self.db.close()


__all__ = [
    "AlertRepo", "ApprovalRepo", "BacktestRepo", "BarRepo", "EquityCurveRepo", "FillRepo",
    "FundingRepo", "GovernorRepo", "HeartbeatRepo", "IlliquidRepo", "LedgerRepo", "MetricRepo",
    "OrderRepo", "PositionRepo", "RebalanceRepo", "ReconciliationRepo", "ReportRepo",
    "Repositories", "RiskModelRepo", "SignalRepo", "SliceRepo", "SnapshotRepo", "StateRepo",
    "SymbolMetaRepo", "SymbolPnlRepo", "TargetRepo", "TrackingRepo", "TradeRepo", "UniverseRepo",
]
