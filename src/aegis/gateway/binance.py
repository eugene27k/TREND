"""The real Binance USDS-M futures venue.

This is the only module in the repository allowed to import ``httpx``. The
official connector is deliberately not used: we need exact control over three
things it hides — the retry/backoff policy that decides when a failure becomes
``ExchangeUnreachable`` (and therefore safe mode), the request-weight budget we
must stay under to avoid a ban, and the demo/live base-URL switch that keeps a
testnet key from ever reaching mainnet.

Every REST failure is translated into one of ``aegis.core.errors`` at this
boundary, so no caller ever sees an HTTP status code or an ``httpx`` exception.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import re
from datetime import date
from typing import Any
from urllib.parse import urlencode

import httpx

from aegis.core.clock import DAY_MS, Clock, day_of, day_start_ms
from aegis.core.config import AppConfig
from aegis.core.errors import (
    ClockDrift,
    ConfigError,
    ExchangeUnreachable,
    GatewayError,
    InsufficientMargin,
    OrderRejected,
    PermissionChanged,
    RateLimited,
)
from aegis.core.precision import format_price, format_qty
from aegis.core.types import (
    AccountState,
    BookTicker,
    DailyBar,
    Fill,
    FundingRate,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    SymbolInfo,
    TimeInForce,
)

#: Binance caps a single ``/fapi/v1/klines`` page at 1500 rows.
KLINE_PAGE_LIMIT = 1500

#: Defensive bound on pagination loops — a venue that never advances its cursor
#: must fail loudly rather than spin forever inside a rebalance window.
MAX_PAGES = 250

#: Above this fraction of the per-minute weight budget we wait for the window to
#: roll rather than risk the 418 ban that follows repeated 429s.
WEIGHT_THROTTLE_FRAC = 0.80

WEIGHT_HEADER = "X-MBX-USED-WEIGHT-1M"

SIGNED_PATHS: frozenset[str] = frozenset(
    {
        "/fapi/v2/account",
        "/fapi/v2/balance",
        "/fapi/v2/positionRisk",
        "/fapi/v1/order",
        "/fapi/v1/openOrders",
        "/fapi/v1/allOpenOrders",
        "/fapi/v1/userTrades",
        "/fapi/v1/income",
        "/fapi/v1/commissionRate",
        "/fapi/v1/leverage",
        "/fapi/v1/marginType",
        "/sapi/v1/account/apiRestrictions",
    }
)

#: Error bodies returned by these paths describe a refused *order*, not a broken
#: request, so they carry the venue's code up to the executor.
ORDER_PATHS: frozenset[str] = frozenset({"/fapi/v1/order", "/fapi/v1/batchOrders"})

#: "No need to change margin type" — idempotent success, not a failure.
_MARGIN_TYPE_UNCHANGED = -4046

_COID_ALLOWED = re.compile(r"[^A-Za-z0-9_-]")
_COID_MAX_LEN = 36


class BinanceGateway:
    """``ExchangeGateway`` over the Binance USDS-M REST API.

    ``read_only=True`` makes every order-placing method raise ``ConfigError``.
    That is how ``paper`` mode borrows live market data without any chance of a
    stray order: the refusal is structural, not a flag someone can forget.
    """

    def __init__(
        self,
        cfg: AppConfig,
        clock: Clock,
        *,
        transport: httpx.BaseTransport | None = None,
        read_only: bool = False,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.read_only = read_only
        self._base_url = cfg.rest_base()
        headers = {"Accept": "application/json"}
        if cfg.account.api_key:
            headers["X-MBX-APIKEY"] = cfg.account.api_key
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=cfg.exchange.request_timeout_s,
            transport=transport,
            headers=headers,
        )
        self._used_weight = 0
        self._symbols: dict[str, SymbolInfo] = {}
        self._commissions: dict[str, tuple[float, float]] = {}
        self._coid_seq = 0

    # -- transport ---------------------------------------------------------- #

    @property
    def base_url(self) -> str:
        return self._base_url

    def used_weight(self) -> int:
        """Last ``X-MBX-USED-WEIGHT-1M`` value the venue reported."""
        return self._used_weight

    def _throttle(self) -> None:
        limit = self.cfg.exchange.rate_limit_weight_per_min
        if limit <= 0 or self._used_weight < limit * WEIGHT_THROTTLE_FRAC:
            return
        # The counter resets on the wall-clock minute boundary, so waiting out
        # the remainder of the current minute is exactly enough.
        remaining_ms = 60_000 - (self.clock.now_ms() % 60_000)
        self.clock.sleep(remaining_ms / 1000.0)
        self._used_weight = 0

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params)
        signature = hmac.new(
            self.cfg.account.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        sent = {k: v for k, v in (params or {}).items() if v is not None}
        if path in SIGNED_PATHS:
            sent["timestamp"] = self.clock.now_ms()
            sent["recvWindow"] = self.cfg.exchange.recv_window_ms
            query = self._sign(sent)
        else:
            query = urlencode(sent)
        url = f"{path}?{query}" if query else path

        attempts = max(1, self.cfg.exchange.max_retries + 1)
        last: Exception | None = None
        for attempt in range(attempts):
            self._throttle()
            try:
                response = self._client.request(method, url)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                self._backoff(attempt, attempts)
                continue
            self._record_weight(response)
            if response.status_code >= 500:
                last = GatewayError(f"{method} {path} -> HTTP {response.status_code}")
                self._backoff(attempt, attempts)
                continue
            if response.status_code >= 400:
                raise self._map_error(path, response)
            return response.json()
        raise ExchangeUnreachable(f"{method} {path} failed after {attempts} attempts: {last}")

    def _backoff(self, attempt: int, attempts: int) -> None:
        if attempt < attempts - 1:
            self.clock.sleep(self.cfg.exchange.retry_backoff_s * (2**attempt))

    def _record_weight(self, response: httpx.Response) -> None:
        raw = response.headers.get(WEIGHT_HEADER)
        if raw is None:
            return
        # A venue that sends a non-numeric counter leaves the last good reading in
        # place: a parse failure must not silently reset the budget to zero.
        with contextlib.suppress(ValueError):
            self._used_weight = int(raw)

    def _map_error(self, path: str, response: httpx.Response) -> Exception:
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = int(body.get("code", 0)) if isinstance(body, dict) else 0
        msg = str(body.get("msg", response.text)) if isinstance(body, dict) else response.text

        if status in (429, 418):
            retry_after = response.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after is not None else self.cfg.exchange.retry_backoff_s
            except ValueError:
                wait = self.cfg.exchange.retry_backoff_s
            return RateLimited(f"{path}: {msg or status}", retry_after_s=wait)
        if code == -1021:
            # The venue rejected our timestamp; it does not tell us by how much,
            # so the recv window is the tightest bound we can report.
            return ClockDrift(self.cfg.exchange.recv_window_ms)
        if status in (401, 403) or code == -2015:
            return PermissionChanged(f"{path}: {msg or status}")
        if code == -2019:
            return InsufficientMargin(f"{path}: {msg}")
        if path in ORDER_PATHS:
            return OrderRejected(f"{path}: {msg}", code=code)
        return GatewayError(f"{path}: HTTP {status} {msg}")

    # -- instrument & market data ------------------------------------------- #

    def exchange_info(self, *, refresh: bool = False) -> dict[str, SymbolInfo]:
        """All symbols, **including non-TRADING ones** (the delisting watch needs them)."""
        if self._symbols and not refresh:
            return dict(self._symbols)
        payload = self._request("GET", "/fapi/v1/exchangeInfo")
        intervals = self._funding_intervals()
        default_hours = self.cfg.funding.default_interval_hours
        out: dict[str, SymbolInfo] = {}
        for raw in payload.get("symbols", []):
            symbol = raw["symbol"]
            filters = {f.get("filterType"): f for f in raw.get("filters", [])}
            price_f = filters.get("PRICE_FILTER", {})
            lot_f = filters.get("LOT_SIZE", {})
            notional_f = filters.get("MIN_NOTIONAL", {})
            out[symbol] = SymbolInfo(
                symbol=symbol,
                base_asset=raw.get("baseAsset", ""),
                quote_asset=raw.get("quoteAsset", ""),
                status=raw.get("status", ""),
                contract_type=raw.get("contractType", ""),
                tick_size=float(price_f.get("tickSize", 0.0)),
                step_size=float(lot_f.get("stepSize", 0.0)),
                min_qty=float(lot_f.get("minQty", 0.0)),
                # A symbol without a MIN_NOTIONAL filter has no floor, not a zero one.
                min_notional=float(notional_f.get("notional", notional_f.get("minNotional", 0.0))),
                price_precision=int(raw.get("pricePrecision", 8)),
                quantity_precision=int(raw.get("quantityPrecision", 8)),
                onboard_date_ms=int(raw.get("onboardDate", 0)),
                funding_interval_hours=intervals.get(symbol, default_hours),
            )
        self._symbols = out
        return dict(out)

    def _funding_intervals(self) -> dict[str, float]:
        """``fundingInfo`` only lists symbols that deviate from the 8h default."""
        try:
            rows = self._request("GET", "/fapi/v1/fundingInfo")
        except GatewayError:
            return {}
        return {
            r["symbol"]: float(r["fundingIntervalHours"])
            for r in rows
            if isinstance(r, dict) and "symbol" in r and "fundingIntervalHours" in r
        }

    def symbol_info(self, symbol: str) -> SymbolInfo:
        info = self._symbols.get(symbol)
        if info is None:
            info = self.exchange_info().get(symbol)
        if info is None:
            raise GatewayError(f"unknown symbol {symbol!r}")
        return info

    def daily_bars(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        limit: int = 1500,
    ) -> list[DailyBar]:
        """Closed daily klines, ascending, paginated to satisfy any window.

        The still-open bar is excluded: a bar whose ``close_time`` has not passed
        on the venue's own clock is partial, and a signal computed from a partial
        bar is a signal computed from a price that has not happened yet.
        """
        if limit <= 0:
            return []
        start_ms = day_start_ms(start) if start is not None else None
        end_ms = day_start_ms(end) + DAY_MS - 1 if end is not None else None
        now_ms = self.server_time_ms()

        rows: list[list[Any]] = []
        if start_ms is None:
            rows = self._klines_backward(symbol, end_ms, limit)
        else:
            rows = self._klines_forward(symbol, start_ms, end_ms, limit)

        bars: dict[int, DailyBar] = {}
        for k in rows:
            close_time = int(k[6])
            if close_time >= now_ms:
                continue  # the current, still-open bar
            open_time = int(k[0])
            bars[open_time] = DailyBar(
                symbol=symbol,
                day=day_of(open_time),
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                volume=float(k[5]),
                quote_volume=float(k[7]),
                open_time_ms=open_time,
                close_time_ms=close_time,
            )
        ordered = [bars[t] for t in sorted(bars)]
        return ordered[-limit:] if start_ms is None else ordered[:limit]

    def _klines_forward(self, symbol: str, start_ms: int, end_ms: int | None, limit: int) -> list[list[Any]]:
        out: list[list[Any]] = []
        cursor = start_ms
        for _ in range(MAX_PAGES):
            batch = self._request(
                "GET",
                "/fapi/v1/klines",
                {
                    "symbol": symbol,
                    "interval": "1d",
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": KLINE_PAGE_LIMIT,
                },
            )
            if not batch:
                break
            out.extend(batch)
            if len(batch) < KLINE_PAGE_LIMIT or len(out) >= limit + 1:
                break
            cursor = int(batch[-1][0]) + 1
        return out

    def _klines_backward(self, symbol: str, end_ms: int | None, limit: int) -> list[list[Any]]:
        out: list[list[Any]] = []
        cursor = end_ms
        for _ in range(MAX_PAGES):
            # One extra row absorbs the open bar we are about to drop.
            want = min(KLINE_PAGE_LIMIT, limit + 1 - len(out))
            batch = self._request(
                "GET",
                "/fapi/v1/klines",
                {"symbol": symbol, "interval": "1d", "endTime": cursor, "limit": want},
            )
            if not batch:
                break
            out = list(batch) + out
            if len(batch) < want or len(out) >= limit + 1:
                break
            cursor = int(batch[0][0]) - 1
        return out

    def book_ticker(self, symbol: str) -> BookTicker:
        raw = self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        if isinstance(raw, list):
            raw = raw[0]
        return BookTicker(
            symbol=raw["symbol"],
            bid_price=float(raw["bidPrice"]),
            bid_qty=float(raw.get("bidQty", 0.0)),
            ask_price=float(raw["askPrice"]),
            ask_qty=float(raw.get("askQty", 0.0)),
            ts_ms=int(raw.get("time", self.clock.now_ms())),
        )

    def mark_price(self, symbol: str) -> float:
        raw = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        if isinstance(raw, list):
            raw = raw[0]
        return float(raw["markPrice"])

    def predicted_funding(self, symbol: str) -> FundingRate:
        raw = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        if isinstance(raw, list):
            raw = raw[0]
        return FundingRate(
            symbol=raw["symbol"],
            funding_time_ms=int(raw.get("nextFundingTime", 0)),
            rate=float(raw.get("lastFundingRate", 0.0)),
            interval_hours=self._interval_hours(symbol),
        )

    def _interval_hours(self, symbol: str) -> float:
        info = self._symbols.get(symbol)
        return info.funding_interval_hours if info else self.cfg.funding.default_interval_hours

    def funding_history(
        self, symbol: str, start_ms: int | None = None, end_ms: int | None = None
    ) -> list[FundingRate]:
        interval = self._interval_hours(symbol)
        out: list[FundingRate] = []
        cursor = start_ms
        seen: set[int] = set()
        for _ in range(MAX_PAGES):
            batch = self._request(
                "GET",
                "/fapi/v1/fundingRate",
                {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000},
            )
            if not batch:
                break
            for row in batch:
                ts = int(row["fundingTime"])
                if ts in seen:
                    continue
                seen.add(ts)
                out.append(
                    FundingRate(
                        symbol=row["symbol"],
                        funding_time_ms=ts,
                        rate=float(row["fundingRate"]),
                        interval_hours=interval,
                    )
                )
            if len(batch) < 1000:
                break
            cursor = int(batch[-1]["fundingTime"]) + 1
        out.sort(key=lambda f: f.funding_time_ms)
        return out

    def server_time_ms(self) -> int:
        return int(self._request("GET", "/fapi/v1/time")["serverTime"])

    # -- account ------------------------------------------------------------ #

    def account(self) -> AccountState:
        raw = self._request("GET", "/fapi/v2/account")
        return AccountState(
            ts_ms=int(raw.get("updateTime", 0)) or self.clock.now_ms(),
            wallet_balance=float(raw["totalWalletBalance"]),
            margin_balance=float(raw["totalMarginBalance"]),
            unrealized_pnl=float(raw["totalUnrealizedProfit"]),
            available_balance=float(raw["availableBalance"]),
            maint_margin=float(raw["totalMaintMargin"]),
            initial_margin=float(raw["totalInitialMargin"]),
        )

    def positions(self) -> dict[str, Position]:
        rows = self._request("GET", "/fapi/v2/positionRisk")
        out: dict[str, Position] = {}
        for row in rows:
            qty = float(row["positionAmt"])
            if qty == 0.0:
                continue
            out[row["symbol"]] = Position(
                symbol=row["symbol"],
                qty=qty,
                entry_price=float(row.get("entryPrice", 0.0)),
                mark_price=float(row.get("markPrice", 0.0)),
                unrealized_pnl=float(row.get("unRealizedProfit", 0.0)),
                leverage=float(row.get("leverage", 0.0) or 0.0),
                liquidation_price=float(row.get("liquidationPrice", 0.0)),
                adl_quantile=int(row.get("adlQuantile", 0)),
                ts_ms=int(row.get("updateTime", 0)),
            )
        return out

    def income(self, start_ms: int, end_ms: int | None = None, limit: int = 1000) -> list[dict]:
        page = min(max(limit, 1), 1000)
        out: list[dict] = []
        seen: set[str] = set()
        cursor = start_ms
        for _ in range(MAX_PAGES):
            batch = self._request(
                "GET",
                "/fapi/v1/income",
                {"startTime": cursor, "endTime": end_ms, "limit": page},
            )
            if not batch:
                break
            for row in batch:
                key = f"{row.get('tranId', '')}:{row.get('time', '')}:{row.get('incomeType', '')}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
            if len(batch) < page:
                break
            cursor = int(batch[-1]["time"]) + 1
        out.sort(key=lambda r: int(r.get("time", 0)))
        return out

    def commission_rate(self, symbol: str) -> tuple[float, float]:
        """``(maker, taker)`` read from the venue — never hard-coded (Locked Decision 15)."""
        cached = self._commissions.get(symbol)
        if cached is not None:
            return cached
        raw = self._request("GET", "/fapi/v1/commissionRate", {"symbol": symbol})
        rates = (float(raw["makerCommissionRate"]), float(raw["takerCommissionRate"]))
        self._commissions[symbol] = rates
        return rates

    def bnb_balance(self) -> float:
        for row in self._request("GET", "/fapi/v2/balance"):
            if row.get("asset") == "BNB":
                return float(row.get("balance", 0.0))
        return 0.0

    def key_permissions(self) -> dict[str, bool]:
        """Best-effort key audit that **fails closed** on withdrawal.

        ``/sapi/v1/account/apiRestrictions`` lives on spot and is frequently
        unreachable from a futures-only, IP-allowlisted key. When we cannot
        positively observe that withdrawals are *disabled* we report
        ``withdraw=True``, so the startup check refuses the key rather than
        trusting an endpoint that simply did not answer.
        """
        perms = {"futures": False, "withdraw": True, "ip_restricted": False}
        try:
            self.account()
        except GatewayError:
            pass
        else:
            perms["futures"] = True
        try:
            raw = self._request("GET", "/sapi/v1/account/apiRestrictions")
        except (GatewayError, ClockDrift):
            return perms
        perms["withdraw"] = bool(raw.get("enableWithdrawals", True))
        perms["ip_restricted"] = bool(raw.get("ipRestrict", False))
        return perms

    def sub_account_name(self) -> str | None:
        """The configured name only when the venue positively confirms it.

        Binance exposes no sub-account name on any futures endpoint reachable
        from a sub-account key, so this normally returns ``None`` and the caller
        decides what an unconfirmable identity means. It never echoes the
        configured name back as if it had been checked.
        """
        want = self.cfg.account.sub_account_name
        try:
            raw = self._request("GET", "/sapi/v1/account/apiRestrictions")
        except (GatewayError, ClockDrift):
            return None
        if not isinstance(raw, dict):
            return None
        for field in ("subAccountName", "accountName", "email"):
            if raw.get(field) == want:
                return want
        return None

    # -- trading ------------------------------------------------------------ #

    def _assert_writable(self) -> None:
        if self.read_only:
            raise ConfigError("this BinanceGateway is read-only; it cannot send orders")

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._assert_writable()
        self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        self._assert_writable()
        try:
            self._request(
                "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type.upper()}
            )
        except GatewayError as exc:
            if f'code": {_MARGIN_TYPE_UNCHANGED}' in str(exc) or "No need to change" in str(exc):
                return
            raise

    def next_client_order_id(self, request: OrderRequest) -> str:
        """Deterministic, venue-legal id: strategy, intent and a monotonic counter.

        Binance accepts only ``[A-Za-z0-9_-]`` up to 36 characters, and rejects a
        duplicate id, so the counter (not a timestamp) guarantees uniqueness
        within a process without depending on the clock.
        """
        self._coid_seq += 1
        intent = _COID_ALLOWED.sub("-", request.intent or "ord")[:18]
        candidate = f"{request.strategy.value}-{intent}-{self._coid_seq:06d}"
        return _COID_ALLOWED.sub("-", candidate)[:_COID_MAX_LEN]

    def place_order(self, request: OrderRequest) -> Order:
        self._assert_writable()
        info = self.symbol_info(request.symbol)
        client_order_id = request.client_order_id or self.next_client_order_id(request)
        params: dict[str, Any] = {
            "symbol": request.symbol,
            "side": request.side.value,
            "type": request.order_type.value,
            "quantity": format_qty(request.qty, info),
            "newClientOrderId": client_order_id,
        }
        if request.reduce_only:
            params["reduceOnly"] = "true"
        if request.order_type is OrderType.LIMIT:
            if request.price is None:
                raise OrderRejected(f"{request.symbol}: LIMIT order without a price", code=0)
            params["price"] = format_price(request.price, info, request.side)
            params["timeInForce"] = request.time_in_force.value
        try:
            raw = self._request("POST", "/fapi/v1/order", params)
        except OrderRejected as exc:
            raise OrderRejected(str(exc), code=exc.code, client_order_id=client_order_id) from exc
        return self._parse_order(raw, request)

    def cancel_order(self, symbol: str, order_id: str) -> Order:
        self._assert_writable()
        raw = self._request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        return self._parse_order(raw)

    def cancel_all(self, symbol: str) -> None:
        self._assert_writable()
        self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    def get_order(self, symbol: str, order_id: str) -> Order:
        raw = self._request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        return self._parse_order(raw)

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        rows = self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol})
        return [self._parse_order(r) for r in rows]

    def user_trades(self, symbol: str, start_ms: int | None = None, limit: int = 1000) -> list[Fill]:
        page = min(max(limit, 1), 1000)
        rows = self._request(
            "GET",
            "/fapi/v1/userTrades",
            {"symbol": symbol, "startTime": start_ms, "limit": page},
        )
        fills = [
            Fill(
                trade_id=str(r["id"]),
                order_id=str(r["orderId"]),
                symbol=r["symbol"],
                side=Side(r["side"]),
                qty=float(r["qty"]),
                price=float(r["price"]),
                fee=float(r.get("commission", 0.0)),
                fee_asset=r.get("commissionAsset", ""),
                is_maker=bool(r.get("maker", False)),
                ts_ms=int(r["time"]),
                realized_pnl=float(r.get("realizedPnl", 0.0)),
            )
            for r in rows
        ]
        fills.sort(key=lambda f: (f.ts_ms, f.trade_id))
        return fills

    def _parse_order(self, raw: dict[str, Any], request: OrderRequest | None = None) -> Order:
        price = raw.get("price")
        price_f = float(price) if price not in (None, "") else None
        if price_f == 0.0:
            price_f = None
        tif = raw.get("timeInForce") or (request.time_in_force.value if request else TimeInForce.GTC.value)
        created = int(raw.get("time", 0)) or int(raw.get("updateTime", 0))
        return Order(
            order_id=str(raw.get("orderId", "")),
            client_order_id=str(raw.get("clientOrderId", "")),
            symbol=raw["symbol"],
            side=Side(raw["side"]),
            order_type=OrderType(raw.get("type", OrderType.LIMIT.value)),
            qty=float(raw.get("origQty", 0.0)),
            price=price_f,
            time_in_force=TimeInForce(tif),
            reduce_only=bool(raw.get("reduceOnly", False)),
            status=OrderStatus(raw.get("status", OrderStatus.NEW.value)),
            filled_qty=float(raw.get("executedQty", 0.0)),
            avg_price=float(raw.get("avgPrice", 0.0) or 0.0),
            created_ts_ms=created,
            updated_ts_ms=int(raw.get("updateTime", 0)),
            strategy=request.strategy if request else self.cfg.strategy,
            rebalance_id=request.rebalance_id if request else None,
            slice_id=request.slice_id if request else None,
            intent=request.intent if request else "",
        )

    # -- lifecycle ---------------------------------------------------------- #

    def poll(self) -> None:
        """Nothing to advance — the live venue keeps its own state."""

    def close(self) -> None:
        self._client.close()


__all__ = [
    "KLINE_PAGE_LIMIT",
    "ORDER_PATHS",
    "SIGNED_PATHS",
    "WEIGHT_HEADER",
    "WEIGHT_THROTTLE_FRAC",
    "BinanceGateway",
]
