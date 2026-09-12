"""US-T01 / shared gateway — the real Binance USDS-M client.

Every test here is offline: an ``httpx.MockTransport`` is injected, so no socket
is ever opened and the assertions are about the exact bytes we would have sent.
"""

from __future__ import annotations

import hashlib
import hmac
import types
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest

from aegis.core.clock import DAY_MS, FakeClock, to_ms
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
from aegis.core.types import (
    Mode,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Strategy,
    TimeInForce,
)
from aegis.gateway import binance as binance_mod
from aegis.gateway.base import ExchangeGateway
from aegis.gateway.binance import WEIGHT_HEADER, BinanceGateway
from aegis.gateway.factory import build_gateway

API_KEY = "test-key"
API_SECRET = "test-secret"
DAY0 = to_ms(date(2024, 1, 1))
SERVER_NOW = DAY0 + 6 * DAY_MS + 3_600_000  # 2024-01-07 01:00 UTC


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class Recorder:
    """Collects every request the gateway makes, so tests can assert on them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def paths(self) -> list[str]:
        return [urlparse(str(r.url)).path for r in self.requests]

    def params(self, index: int = -1) -> dict[str, str]:
        return dict(parse_qsl(urlparse(str(self.requests[index].url)).query))

    def for_path(self, path: str) -> list[dict[str, str]]:
        return [
            dict(parse_qsl(urlparse(str(r.url)).query))
            for r in self.requests
            if urlparse(str(r.url)).path == path
        ]


def make_gateway(
    routes: dict[str, Any],
    *,
    mode: Mode = Mode.LIVE,
    clock: FakeClock | None = None,
    read_only: bool = False,
    overrides: dict[str, Any] | None = None,
) -> tuple[BinanceGateway, Recorder, FakeClock]:
    """A gateway wired to a MockTransport that serves ``routes`` by path."""
    rec = Recorder()
    base: dict[str, Any] = {
        "mode": mode,
        "account": {"api_key": API_KEY, "api_secret": API_SECRET},
    }
    if overrides:
        base.update(overrides)
    cfg = AppConfig.model_validate(base)
    the_clock = clock or FakeClock(0)

    def handler(request: httpx.Request) -> httpx.Response:
        rec.requests.append(request)
        path = urlparse(str(request.url)).path
        route = routes.get(path)
        if route is None:
            return httpx.Response(404, json={"code": -1121, "msg": f"no route {path}"})
        if callable(route):
            return route(request)
        return httpx.Response(200, json=route)

    gw = BinanceGateway(cfg, the_clock, transport=httpx.MockTransport(handler), read_only=read_only)
    return gw, rec, the_clock


def kline(day_index: int, *, close: float = 100.0) -> list[Any]:
    open_ms = DAY0 + day_index * DAY_MS
    return [
        open_ms,
        f"{close - 1:.2f}",
        f"{close + 2:.2f}",
        f"{close - 3:.2f}",
        f"{close:.2f}",
        "1000",
        open_ms + DAY_MS - 1,
        f"{close * 1000:.2f}",
        50,
        "500",
        "50000",
        "0",
    ]


ALL_KLINES = [kline(i, close=100.0 + i) for i in range(7)]  # days 0..6; day 6 is still open


def kline_route(rows: list[list[Any]] | None = None):
    """A klines endpoint that honours startTime/endTime/limit like Binance does."""
    data = rows if rows is not None else ALL_KLINES

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(parse_qsl(urlparse(str(request.url)).query))
        rows_ = list(data)
        if "startTime" in q:
            rows_ = [r for r in rows_ if r[0] >= int(q["startTime"])]
        if "endTime" in q:
            rows_ = [r for r in rows_ if r[0] <= int(q["endTime"])]
        limit = int(q.get("limit", 1500))
        rows_ = rows_[:limit] if "startTime" in q else rows_[-limit:]
        return httpx.Response(200, json=rows_)

    return handler


TIME_ROUTE = {"serverTime": SERVER_NOW}


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #


def test_signed_request_carries_hmac_sha256_of_the_query_string() -> None:
    gw, rec, _ = make_gateway(
        {"/fapi/v1/commissionRate": {"makerCommissionRate": "0.0002", "takerCommissionRate": "0.0005"}}
    )
    gw.commission_rate("BTCUSDT")

    sent = str(rec.requests[0].url)
    query = urlparse(sent).query
    payload, _, signature = query.rpartition("&signature=")
    expected = hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    params = dict(parse_qsl(payload))
    assert params["symbol"] == "BTCUSDT"
    assert params["recvWindow"] == "5000"
    assert params["timestamp"] == "0"
    assert rec.requests[0].headers["X-MBX-APIKEY"] == API_KEY


def test_public_request_is_not_signed() -> None:
    gw, rec, _ = make_gateway({"/fapi/v1/time": TIME_ROUTE})
    assert gw.server_time_ms() == SERVER_NOW
    assert "signature" not in str(rec.requests[0].url)
    assert "recvWindow" not in str(rec.requests[0].url)


def test_every_retry_of_a_signed_request_is_re_stamped_and_re_signed() -> None:
    """A retry that re-sends the first attempt's timestamp is outside recvWindow.

    Backoff is 1 s then 2 s, so by the third attempt the original stamp is 3 s
    old; with a 5 s recvWindow the venue answers -1021 and this gateway would
    report a clock drift that never happened. Each attempt must carry the clock
    reading of the moment it is sent, signed over that same payload.
    """
    seen: list[tuple[str, str, str]] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        query = urlparse(str(request.url)).query
        payload, _, signature = query.rpartition("&signature=")
        seen.append((dict(parse_qsl(payload))["timestamp"], payload, signature))
        if len(seen) < 3:
            return httpx.Response(503, text="boom")
        return httpx.Response(200, json={"makerCommissionRate": "0.0002", "takerCommissionRate": "0.0005"})

    gw, _, clock = make_gateway({"/fapi/v1/commissionRate": flaky})
    gw.commission_rate("BTCUSDT")

    stamps = [int(t) for t, _, _ in seen]
    assert stamps == [0, 1000, 3000]  # the clock at each attempt, not the first
    assert clock.now_ms() == 3000
    for _, payload, signature in seen:
        expected = hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        assert signature == expected


def test_a_signed_request_is_stamped_after_the_weight_throttle_waits() -> None:
    """The stamp must post-date the throttle sleep, not precede it by a minute."""
    stamps: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stamps.append(int(dict(parse_qsl(urlparse(str(request.url)).query))["timestamp"]))
        return httpx.Response(
            200,
            json={"makerCommissionRate": "0.0002", "takerCommissionRate": "0.0005"},
            headers={WEIGHT_HEADER: "2000"},
        )

    clock = FakeClock(0)
    gw, _, _ = make_gateway({"/fapi/v1/commissionRate": handler}, clock=clock)
    gw.commission_rate("BTCUSDT")
    gw.commission_rate("ETHUSDT")  # the second call waits out the minute first
    assert clock.slept == [60.0]
    assert stamps == [0, 60_000]


def test_a_retried_order_keeps_its_client_order_id() -> None:
    """Re-stamping must not re-identify the order: the venue's duplicate-id
    rejection is what stops a timed-out POST from becoming two positions."""
    ids: list[str] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        q = dict(parse_qsl(urlparse(str(request.url)).query))
        ids.append(q["newClientOrderId"])
        if len(ids) < 2:
            return httpx.Response(502, text="bad gateway")
        return _order_ack(request)

    gw, _, _ = make_gateway(
        {
            "/fapi/v1/exchangeInfo": EXCHANGE_INFO,
            "/fapi/v1/fundingInfo": FUNDING_INFO,
            "/fapi/v1/order": flaky,
        }
    )
    gw.place_order(_order_request())
    assert ids == ["TREND-rebalance-000001", "TREND-rebalance-000001"]


def test_demo_mode_uses_the_testnet_base_url() -> None:
    gw, rec, _ = make_gateway({"/fapi/v1/time": TIME_ROUTE}, mode=Mode.DEMO)
    gw.server_time_ms()
    assert gw.base_url == "https://testnet.binancefuture.com"
    assert str(rec.requests[0].url).startswith("https://testnet.binancefuture.com")


def test_live_mode_uses_the_mainnet_base_url() -> None:
    gw, _, _ = make_gateway({}, mode=Mode.LIVE)
    assert gw.base_url == "https://fapi.binance.com"


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


def _order_request() -> OrderRequest:
    return OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        qty=0.5,
        price=30_000.0,
        intent="rebalance",
    )


EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "pricePrecision": 2,
            "quantityPrecision": 3,
            "onboardDate": 1569398400000,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        },
        {
            "symbol": "OLDUSDT",
            "baseAsset": "OLD",
            "quoteAsset": "USDT",
            "status": "SETTLING",
            "contractType": "PERPETUAL",
            "pricePrecision": 4,
            "quantityPrecision": 0,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
            ],
        },
    ]
}

FUNDING_INFO = [{"symbol": "OLDUSDT", "fundingIntervalHours": 4}]


def _order_error_gateway(code: int, *, status: int = 400):
    routes = {
        "/fapi/v1/exchangeInfo": EXCHANGE_INFO,
        "/fapi/v1/fundingInfo": FUNDING_INFO,
        "/fapi/v1/order": lambda r: httpx.Response(status, json={"code": code, "msg": "nope"}),
    }
    return make_gateway(routes)


def test_binance_code_minus_2022_on_an_order_raises_order_rejected() -> None:
    gw, _, _ = _order_error_gateway(-2022)
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_order_request())
    assert excinfo.value.code == -2022
    assert excinfo.value.is_reduce_only_violation
    assert excinfo.value.client_order_id == "TREND-rebalance-000001"


def test_binance_code_minus_5022_is_flagged_a_post_only_violation() -> None:
    gw, _, _ = _order_error_gateway(-5022)
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_order_request())
    assert excinfo.value.is_post_only_violation


def test_binance_code_minus_2019_raises_insufficient_margin() -> None:
    gw, _, _ = _order_error_gateway(-2019)
    with pytest.raises(InsufficientMargin):
        gw.place_order(_order_request())


def test_binance_code_minus_1021_raises_clock_drift() -> None:
    gw, _, _ = _order_error_gateway(-1021)
    with pytest.raises(ClockDrift) as excinfo:
        gw.place_order(_order_request())
    assert excinfo.value.drift_ms == 5000


def test_binance_code_minus_2015_raises_permission_changed() -> None:
    gw, _, _ = _order_error_gateway(-2015)
    with pytest.raises(PermissionChanged):
        gw.place_order(_order_request())


@pytest.mark.parametrize("status", [401, 403])
def test_http_401_and_403_raise_permission_changed(status: int) -> None:
    gw, _, _ = make_gateway(
        {"/fapi/v2/account": lambda r: httpx.Response(status, json={"code": -1, "msg": "denied"})}
    )
    with pytest.raises(PermissionChanged):
        gw.account()


@pytest.mark.parametrize("status", [429, 418])
def test_http_429_and_418_raise_rate_limited_with_retry_after(status: int) -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v1/time": lambda r: httpx.Response(
                status, json={"code": -1003, "msg": "too many"}, headers={"Retry-After": "17"}
            )
        }
    )
    with pytest.raises(RateLimited) as excinfo:
        gw.server_time_ms()
    assert excinfo.value.retry_after_s == 17.0


def test_rate_limited_without_retry_after_falls_back_to_the_configured_backoff() -> None:
    gw, _, _ = make_gateway(
        {"/fapi/v1/time": lambda r: httpx.Response(429, json={"code": -1003, "msg": "slow down"})}
    )
    with pytest.raises(RateLimited) as excinfo:
        gw.server_time_ms()
    assert excinfo.value.retry_after_s == 1.0


def test_rate_limited_with_unparseable_retry_after_falls_back_to_the_backoff() -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v1/time": lambda r: httpx.Response(
                429, json={"code": -1003, "msg": "x"}, headers={"Retry-After": "soon"}
            )
        }
    )
    with pytest.raises(RateLimited) as excinfo:
        gw.server_time_ms()
    assert excinfo.value.retry_after_s == 1.0


def test_an_unmapped_4xx_on_a_non_order_path_raises_gateway_error() -> None:
    gw, _, _ = make_gateway(
        {"/fapi/v1/time": lambda r: httpx.Response(400, json={"code": -1100, "msg": "bad param"})}
    )
    with pytest.raises(GatewayError) as excinfo:
        gw.server_time_ms()
    assert "bad param" in str(excinfo.value)


def test_a_4xx_with_a_non_json_body_still_raises_gateway_error() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/time": lambda r: httpx.Response(400, text="<html>nope")})
    with pytest.raises(GatewayError):
        gw.server_time_ms()


# --------------------------------------------------------------------------- #
# Retries
# --------------------------------------------------------------------------- #


def test_a_5xx_is_retried_and_the_retry_succeeds() -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="bad gateway")
        return httpx.Response(200, json=TIME_ROUTE)

    gw, _, clock = make_gateway({"/fapi/v1/time": flaky})
    assert gw.server_time_ms() == SERVER_NOW
    assert calls["n"] == 3
    assert clock.slept == [1.0, 2.0]  # exponential backoff


def test_retries_exhausted_raise_exchange_unreachable() -> None:
    gw, rec, clock = make_gateway(
        {"/fapi/v1/time": lambda r: httpx.Response(500, text="boom")},
        overrides={"exchange": {"max_retries": 2}},
    )
    with pytest.raises(ExchangeUnreachable):
        gw.server_time_ms()
    assert len(rec.requests) == 3  # initial attempt + 2 retries
    assert clock.slept == [1.0, 2.0]  # no sleep after the final attempt


def test_a_timeout_is_retried_then_raises_exchange_unreachable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    gw, rec, _ = make_gateway({"/fapi/v1/time": timeout}, overrides={"exchange": {"max_retries": 1}})
    with pytest.raises(ExchangeUnreachable):
        gw.server_time_ms()
    assert len(rec.requests) == 2


def test_a_connection_error_is_retried_then_raises_exchange_unreachable() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    gw, rec, _ = make_gateway({"/fapi/v1/time": refused}, overrides={"exchange": {"max_retries": 0}})
    with pytest.raises(ExchangeUnreachable):
        gw.server_time_ms()
    assert len(rec.requests) == 1


# --------------------------------------------------------------------------- #
# Weight tracking
# --------------------------------------------------------------------------- #


def test_used_weight_is_read_from_the_response_header() -> None:
    gw, _, _ = make_gateway(
        {"/fapi/v1/time": lambda r: httpx.Response(200, json=TIME_ROUTE, headers={WEIGHT_HEADER: "123"})}
    )
    assert gw.used_weight() == 0
    gw.server_time_ms()
    assert gw.used_weight() == 123


def test_a_missing_or_garbage_weight_header_leaves_the_counter_untouched() -> None:
    responses = [
        httpx.Response(200, json=TIME_ROUTE, headers={WEIGHT_HEADER: "50"}),
        httpx.Response(200, json=TIME_ROUTE),
        httpx.Response(200, json=TIME_ROUTE, headers={WEIGHT_HEADER: "not-a-number"}),
    ]
    gw, _, _ = make_gateway({"/fapi/v1/time": lambda r: responses.pop(0)})
    gw.server_time_ms()
    gw.server_time_ms()
    gw.server_time_ms()
    assert gw.used_weight() == 50


def test_above_80_percent_of_the_weight_budget_the_gateway_sleeps_on_the_clock() -> None:
    clock = FakeClock(0)
    gw, rec, _ = make_gateway(
        {"/fapi/v1/time": lambda r: httpx.Response(200, json=TIME_ROUTE, headers={WEIGHT_HEADER: "2000"})},
        clock=clock,
        overrides={"exchange": {"rate_limit_weight_per_min": 2400}},
    )
    gw.server_time_ms()
    assert clock.slept == []  # first call: budget unknown, no wait
    gw.server_time_ms()
    assert clock.slept == [60.0]  # waited out the remainder of the minute
    assert len(rec.requests) == 2


def test_below_80_percent_of_the_weight_budget_the_gateway_does_not_sleep() -> None:
    clock = FakeClock(0)
    gw, _, _ = make_gateway(
        {"/fapi/v1/time": lambda r: httpx.Response(200, json=TIME_ROUTE, headers={WEIGHT_HEADER: "1000"})},
        clock=clock,
    )
    gw.server_time_ms()
    gw.server_time_ms()
    assert clock.slept == []


def test_a_zero_weight_budget_disables_the_throttle() -> None:
    clock = FakeClock(0)
    gw, _, _ = make_gateway(
        {"/fapi/v1/time": lambda r: httpx.Response(200, json=TIME_ROUTE, headers={WEIGHT_HEADER: "9999"})},
        clock=clock,
        overrides={"exchange": {"rate_limit_weight_per_min": 0}},
    )
    gw.server_time_ms()
    gw.server_time_ms()
    assert clock.slept == []


# --------------------------------------------------------------------------- #
# exchangeInfo
# --------------------------------------------------------------------------- #


def test_exchange_info_parses_filters_precision_and_onboard_date() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/exchangeInfo": EXCHANGE_INFO, "/fapi/v1/fundingInfo": FUNDING_INFO})
    info = gw.exchange_info()["BTCUSDT"]
    assert (info.tick_size, info.step_size, info.min_qty) == (0.10, 0.001, 0.001)
    assert info.min_notional == 5.0
    assert (info.price_precision, info.quantity_precision) == (2, 3)
    assert info.onboard_date_ms == 1569398400000
    assert info.funding_interval_hours == 8.0  # default when fundingInfo omits it
    assert info.is_tradeable_perp


def test_exchange_info_returns_non_trading_symbols_for_the_delisting_watch() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/exchangeInfo": EXCHANGE_INFO, "/fapi/v1/fundingInfo": FUNDING_INFO})
    info = gw.exchange_info()
    assert set(info) == {"BTCUSDT", "OLDUSDT"}
    assert info["OLDUSDT"].status == "SETTLING"
    assert not info["OLDUSDT"].is_tradeable_perp


def test_exchange_info_symbol_without_min_notional_filter_gets_zero() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/exchangeInfo": EXCHANGE_INFO, "/fapi/v1/fundingInfo": FUNDING_INFO})
    assert gw.exchange_info()["OLDUSDT"].min_notional == 0.0


def test_exchange_info_merges_the_funding_interval_from_funding_info() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/exchangeInfo": EXCHANGE_INFO, "/fapi/v1/fundingInfo": FUNDING_INFO})
    assert gw.exchange_info()["OLDUSDT"].funding_interval_hours == 4.0


def test_exchange_info_survives_an_unreachable_funding_info_endpoint() -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v1/exchangeInfo": EXCHANGE_INFO,
            "/fapi/v1/fundingInfo": lambda r: httpx.Response(400, json={"code": -1, "msg": "nope"}),
        }
    )
    assert gw.exchange_info()["OLDUSDT"].funding_interval_hours == 8.0


def test_exchange_info_is_cached_until_refresh_is_requested() -> None:
    gw, rec, _ = make_gateway({"/fapi/v1/exchangeInfo": EXCHANGE_INFO, "/fapi/v1/fundingInfo": FUNDING_INFO})
    gw.exchange_info()
    gw.exchange_info()
    assert rec.paths().count("/fapi/v1/exchangeInfo") == 1
    gw.exchange_info(refresh=True)
    assert rec.paths().count("/fapi/v1/exchangeInfo") == 2


def test_symbol_info_for_an_unknown_symbol_raises_gateway_error() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/exchangeInfo": EXCHANGE_INFO, "/fapi/v1/fundingInfo": FUNDING_INFO})
    with pytest.raises(GatewayError):
        gw.symbol_info("NOPEUSDT")


# --------------------------------------------------------------------------- #
# Klines
# --------------------------------------------------------------------------- #


def test_daily_bars_excludes_the_still_open_current_bar() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/klines": kline_route(), "/fapi/v1/time": TIME_ROUTE})
    bars = gw.daily_bars("BTCUSDT", start=date(2024, 1, 1))
    # Server now is 2024-01-07 01:00; day 6 closes at 2024-01-07 23:59:59.999.
    assert [b.day for b in bars] == [date(2024, 1, d) for d in range(1, 7)]
    assert all(b.close_time_ms < SERVER_NOW for b in bars)


def test_daily_bars_maps_quote_volume_open_and_close_times() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/klines": kline_route(), "/fapi/v1/time": TIME_ROUTE})
    bar = gw.daily_bars("BTCUSDT", start=date(2024, 1, 1))[0]
    assert bar.symbol == "BTCUSDT"
    assert bar.open_time_ms == DAY0
    assert bar.close_time_ms == DAY0 + DAY_MS - 1
    assert bar.quote_volume == 100_000.0
    assert (bar.open, bar.high, bar.low, bar.close) == (99.0, 102.0, 97.0, 100.0)
    assert bar.source == "rest"


def test_daily_bars_paginates_forward_from_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binance_mod, "KLINE_PAGE_LIMIT", 2)
    gw, rec, _ = make_gateway({"/fapi/v1/klines": kline_route(), "/fapi/v1/time": TIME_ROUTE})
    bars = gw.daily_bars("BTCUSDT", start=date(2024, 1, 1))
    assert len(bars) == 6
    pages = rec.for_path("/fapi/v1/klines")
    assert len(pages) > 1
    assert all(p["limit"] == "2" for p in pages)
    assert int(pages[1]["startTime"]) > int(pages[0]["startTime"])


def test_daily_bars_paginates_backward_when_only_a_limit_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(binance_mod, "KLINE_PAGE_LIMIT", 2)
    gw, rec, _ = make_gateway({"/fapi/v1/klines": kline_route(), "/fapi/v1/time": TIME_ROUTE})
    bars = gw.daily_bars("BTCUSDT", limit=4)
    assert [b.day for b in bars] == [date(2024, 1, d) for d in range(3, 7)]
    pages = rec.for_path("/fapi/v1/klines")
    assert len(pages) > 2
    assert "endTime" not in pages[0]  # newest page first, then walk backwards
    assert int(pages[2]["endTime"]) < int(pages[1]["endTime"])


def test_daily_bars_honours_the_end_date() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/klines": kline_route(), "/fapi/v1/time": TIME_ROUTE})
    bars = gw.daily_bars("BTCUSDT", start=date(2024, 1, 1), end=date(2024, 1, 3))
    assert [b.day for b in bars] == [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]


def test_daily_bars_truncates_a_forward_window_to_the_limit() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/klines": kline_route(), "/fapi/v1/time": TIME_ROUTE})
    bars = gw.daily_bars("BTCUSDT", start=date(2024, 1, 1), limit=2)
    assert [b.day for b in bars] == [date(2024, 1, 1), date(2024, 1, 2)]


def test_daily_bars_deduplicates_overlapping_pages() -> None:
    repeated = ALL_KLINES[:3] + ALL_KLINES[:3]
    gw, _, _ = make_gateway({"/fapi/v1/klines": kline_route(repeated), "/fapi/v1/time": TIME_ROUTE})
    bars = gw.daily_bars("BTCUSDT", start=date(2024, 1, 1))
    assert len(bars) == 3


def test_daily_bars_with_a_non_positive_limit_returns_nothing() -> None:
    gw, rec, _ = make_gateway({"/fapi/v1/klines": kline_route(), "/fapi/v1/time": TIME_ROUTE})
    assert gw.daily_bars("BTCUSDT", limit=0) == []
    assert rec.requests == []


def test_daily_bars_on_an_empty_symbol_returns_nothing() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/klines": [], "/fapi/v1/time": TIME_ROUTE})
    assert gw.daily_bars("NEWUSDT", start=date(2024, 1, 1)) == []
    assert gw.daily_bars("NEWUSDT", limit=5) == []


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #


def test_book_ticker_and_mark_price_are_parsed() -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v1/ticker/bookTicker": {
                "symbol": "BTCUSDT",
                "bidPrice": "30000.1",
                "bidQty": "2",
                "askPrice": "30000.3",
                "askQty": "3",
                "time": 1700,
            },
            "/fapi/v1/premiumIndex": {
                "symbol": "BTCUSDT",
                "markPrice": "30000.2",
                "lastFundingRate": "0.0001",
                "nextFundingTime": 1800,
            },
        }
    )
    bt = gw.book_ticker("BTCUSDT")
    assert (bt.bid_price, bt.ask_price, bt.ts_ms) == (30000.1, 30000.3, 1700)
    assert gw.mark_price("BTCUSDT") == 30000.2


def test_book_ticker_accepts_a_list_response() -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v1/ticker/bookTicker": [{"symbol": "BTCUSDT", "bidPrice": "1", "askPrice": "2"}],
            "/fapi/v1/premiumIndex": [{"symbol": "BTCUSDT", "markPrice": "3"}],
        }
    )
    assert gw.book_ticker("BTCUSDT").mid == 1.5
    assert gw.mark_price("BTCUSDT") == 3.0


def test_predicted_funding_uses_the_symbols_own_interval() -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v1/exchangeInfo": EXCHANGE_INFO,
            "/fapi/v1/fundingInfo": FUNDING_INFO,
            "/fapi/v1/premiumIndex": {
                "symbol": "OLDUSDT",
                "markPrice": "1",
                "lastFundingRate": "0.0004",
                "nextFundingTime": 1800,
            },
        }
    )
    gw.exchange_info()
    fr = gw.predicted_funding("OLDUSDT")
    assert fr.interval_hours == 4.0
    assert fr.rate == 0.0004
    assert fr.funding_time_ms == 1800
    assert fr.annualised() == pytest.approx(0.0004 * 8760 / 4)


def test_predicted_funding_resolves_the_interval_without_a_prior_exchange_info_call() -> None:
    """Section 5.6 annualises with the symbol's *own* interval.

    A cold instrument cache must not fall back to 8 h for a 4 h symbol: that
    halves ``f_i = lastFundingRate x 8760 / I_h`` and the +/-30 % haircut never
    fires on a symbol that is paying 60 % a year.
    """
    gw, _, _ = make_gateway(
        {
            "/fapi/v1/exchangeInfo": EXCHANGE_INFO,
            "/fapi/v1/fundingInfo": FUNDING_INFO,
            "/fapi/v1/premiumIndex": {
                "symbol": "OLDUSDT",
                "markPrice": "1",
                "lastFundingRate": "0.0004",
                "nextFundingTime": 1800,
            },
        }
    )
    fr = gw.predicted_funding("OLDUSDT")  # no exchange_info() call first
    assert fr.interval_hours == 4.0
    assert fr.annualised() == pytest.approx(0.0004 * 8760 / 4)


def test_predicted_funding_defaults_to_eight_hours_without_exchange_info() -> None:
    gw, _, _ = make_gateway(
        {"/fapi/v1/premiumIndex": [{"symbol": "BTCUSDT", "markPrice": "1", "lastFundingRate": "0"}]}
    )
    assert gw.predicted_funding("BTCUSDT").interval_hours == 8.0


def test_funding_history_paginates_and_returns_ascending_settlements() -> None:
    rows = [
        {"symbol": "BTCUSDT", "fundingTime": DAY0 + i * 28_800_000, "fundingRate": "0.0001"}
        for i in range(1500)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(parse_qsl(urlparse(str(request.url)).query))
        sel = rows
        if "startTime" in q:
            sel = [r for r in sel if r["fundingTime"] >= int(q["startTime"])]
        return httpx.Response(200, json=sel[: int(q.get("limit", 1000))])

    gw, rec, _ = make_gateway({"/fapi/v1/fundingRate": handler})
    out = gw.funding_history("BTCUSDT", start_ms=DAY0)
    assert len(out) == 1500
    assert [f.funding_time_ms for f in out] == sorted(f.funding_time_ms for f in out)
    assert len(rec.for_path("/fapi/v1/fundingRate")) == 2


def test_funding_history_on_an_empty_window_returns_nothing() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/fundingRate": []})
    assert gw.funding_history("BTCUSDT") == []


# --------------------------------------------------------------------------- #
# Account
# --------------------------------------------------------------------------- #

ACCOUNT_ROUTE = {
    "updateTime": 1234,
    "totalWalletBalance": "10000",
    "totalMarginBalance": "10500",
    "totalUnrealizedProfit": "500",
    "availableBalance": "8000",
    "totalMaintMargin": "300",
    "totalInitialMargin": "2000",
}


def test_account_is_parsed_into_an_account_state() -> None:
    gw, _, _ = make_gateway({"/fapi/v2/account": ACCOUNT_ROUTE})
    acct = gw.account()
    assert acct.equity == 10500.0
    assert acct.margin_ratio == pytest.approx(300 / 10500)
    assert acct.ts_ms == 1234


def test_positions_drops_flat_symbols() -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v2/positionRisk": [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "-0.5",
                    "entryPrice": "30000",
                    "markPrice": "29000",
                    "unRealizedProfit": "500",
                    "leverage": "5",
                    "liquidationPrice": "60000",
                    "adlQuantile": 2,
                    "updateTime": 99,
                },
                {"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "0", "markPrice": "2000"},
            ]
        }
    )
    pos = gw.positions()
    assert set(pos) == {"BTCUSDT"}
    assert pos["BTCUSDT"].notional == pytest.approx(-14_500.0)
    assert pos["BTCUSDT"].adl_quantile == 2


def test_positions_reads_the_adl_quantile_from_its_own_endpoint() -> None:
    """``/fapi/v2/positionRisk`` does not report it, and US-T12 AC 4 needs it:
    a short at quantile >= 4 is reduced by 25 %, which cannot happen if every
    position arrives at quantile 0."""
    gw, rec, _ = make_gateway(
        {
            "/fapi/v2/positionRisk": [
                {"symbol": "BTCUSDT", "positionAmt": "-0.5", "entryPrice": "30000", "markPrice": "29000"}
            ],
            "/fapi/v1/adlQuantile": [
                {"symbol": "BTCUSDT", "adlQuantile": {"LONG": 0, "SHORT": 4, "BOTH": 4}},
                {"symbol": "ETHUSDT", "adlQuantile": {"BOTH": 1}},
            ],
        }
    )
    assert gw.positions()["BTCUSDT"].adl_quantile == 4
    assert "/fapi/v1/adlQuantile" in rec.paths()


def test_positions_does_not_ask_for_adl_quantiles_when_the_book_is_flat() -> None:
    gw, rec, _ = make_gateway(
        {"/fapi/v2/positionRisk": [{"symbol": "BTCUSDT", "positionAmt": "0", "markPrice": "1"}]}
    )
    assert gw.positions() == {}
    assert "/fapi/v1/adlQuantile" not in rec.paths()


def test_positions_survive_an_unreachable_adl_endpoint() -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v2/positionRisk": [
                {"symbol": "BTCUSDT", "positionAmt": "-0.5", "entryPrice": "30000", "markPrice": "29000"}
            ]
        }  # the adlQuantile route 404s
    )
    assert gw.positions()["BTCUSDT"].adl_quantile == 0


def test_income_paginates_and_returns_ascending_rows() -> None:
    rows = [
        {"tranId": i, "time": DAY0 + i * 1000, "incomeType": "FUNDING_FEE", "income": "0.1"}
        for i in range(1500)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(parse_qsl(urlparse(str(request.url)).query))
        sel = [r for r in rows if r["time"] >= int(q["startTime"])]
        return httpx.Response(200, json=sel[: int(q["limit"])])

    gw, rec, _ = make_gateway({"/fapi/v1/income": handler})
    out = gw.income(DAY0)
    assert len(out) == 1500
    assert [r["time"] for r in out] == sorted(r["time"] for r in out)
    assert len(rec.for_path("/fapi/v1/income")) == 2


def test_income_deduplicates_repeated_rows() -> None:
    row = {"tranId": 1, "time": DAY0, "incomeType": "COMMISSION", "income": "-0.1"}
    gw, _, _ = make_gateway({"/fapi/v1/income": [row, row]})
    assert len(gw.income(DAY0)) == 1


def test_income_keeps_rows_that_share_a_millisecond_across_a_page_boundary() -> None:
    """Every funding settlement of a day carries the same ``time``.

    A cursor of ``last_time + 1`` steps over the rows of that group that did not
    fit the page; they are then missing from the ledger for ever and the
    identity of US-T14 AC 3 cannot close.
    """
    settle = DAY0 + 3 * DAY_MS
    rows = [
        {"tranId": i, "time": settle, "incomeType": "FUNDING_FEE", "symbol": f"S{i}", "income": "-1.0"}
        for i in range(5)
    ]
    rows.append({"tranId": 99, "time": settle + 1000, "incomeType": "COMMISSION", "income": "-0.1"})

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(parse_qsl(urlparse(str(request.url)).query))
        sel = [r for r in rows if r["time"] >= int(q["startTime"])]
        return httpx.Response(200, json=sel[: int(q["limit"])])

    gw, _, _ = make_gateway({"/fapi/v1/income": handler})
    out = gw.income(settle, limit=2)  # a page that splits the 5-row settlement
    assert [r["tranId"] for r in out] == [0, 1, 2, 3, 4, 99]


def test_income_terminates_when_a_full_page_repeats_itself() -> None:
    """A page that adds nothing new must step past its millisecond, not spin."""
    settle = DAY0
    rows = [
        {"tranId": i, "time": settle, "incomeType": "FUNDING_FEE", "symbol": f"S{i}", "income": "-1.0"}
        for i in range(2)
    ]
    gw, rec, _ = make_gateway({"/fapi/v1/income": rows})
    out = gw.income(settle, limit=2)
    assert [r["tranId"] for r in out] == [0, 1]
    assert len(rec.for_path("/fapi/v1/income")) < 5


def test_income_keeps_two_rows_that_differ_only_by_symbol() -> None:
    same = {"tranId": 7, "time": DAY0, "incomeType": "FUNDING_FEE", "income": "-1.0"}
    gw, _, _ = make_gateway(
        {"/fapi/v1/income": [{**same, "symbol": "BTCUSDT"}, {**same, "symbol": "ETHUSDT"}]}
    )
    assert len(gw.income(DAY0)) == 2


def test_commission_rate_is_read_from_the_exchange_and_cached() -> None:
    gw, rec, _ = make_gateway(
        {
            "/fapi/v1/commissionRate": {
                "symbol": "BTCUSDT",
                "makerCommissionRate": "0.00016",
                "takerCommissionRate": "0.0004",
            }
        }
    )
    assert gw.commission_rate("BTCUSDT") == (0.00016, 0.0004)
    assert gw.commission_rate("BTCUSDT") == (0.00016, 0.0004)
    assert len(rec.for_path("/fapi/v1/commissionRate")) == 1


def test_bnb_balance_reads_the_balance_endpoint() -> None:
    gw, _, _ = make_gateway(
        {"/fapi/v2/balance": [{"asset": "USDT", "balance": "100"}, {"asset": "BNB", "balance": "0.4"}]}
    )
    assert gw.bnb_balance() == 0.4


def test_bnb_balance_is_zero_when_the_asset_is_absent() -> None:
    gw, _, _ = make_gateway({"/fapi/v2/balance": [{"asset": "USDT", "balance": "100"}]})
    assert gw.bnb_balance() == 0.0


# --------------------------------------------------------------------------- #
# Key permissions — fail closed
# --------------------------------------------------------------------------- #


def test_key_permissions_reports_withdraw_true_when_restrictions_are_unreachable() -> None:
    gw, _, _ = make_gateway({"/fapi/v2/account": ACCOUNT_ROUTE})  # sapi route 404s
    perms = gw.key_permissions()
    assert perms["futures"] is True
    assert perms["withdraw"] is True  # fail closed: absence is never proof
    assert perms["ip_restricted"] is False


def test_key_permissions_reports_withdraw_false_only_when_positively_confirmed() -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v2/account": ACCOUNT_ROUTE,
            "/sapi/v1/account/apiRestrictions": {"enableWithdrawals": False, "ipRestrict": True},
        }
    )
    assert gw.key_permissions() == {"futures": True, "withdraw": False, "ip_restricted": True}


def test_key_permissions_reports_no_futures_when_the_account_endpoint_refuses() -> None:
    gw, _, _ = make_gateway(
        {"/fapi/v2/account": lambda r: httpx.Response(403, json={"code": -2015, "msg": "denied"})}
    )
    assert gw.key_permissions()["futures"] is False


def test_sub_account_name_is_none_when_the_venue_cannot_confirm_it() -> None:
    gw, _, _ = make_gateway({"/fapi/v2/account": ACCOUNT_ROUTE})
    assert gw.sub_account_name() is None


def test_sub_account_name_is_none_when_the_probe_reports_a_different_name() -> None:
    gw, _, _ = make_gateway({"/sapi/v1/account/apiRestrictions": {"subAccountName": "carry-01"}})
    assert gw.sub_account_name() is None


def test_sub_account_name_is_returned_only_on_a_positive_match() -> None:
    gw, _, _ = make_gateway({"/sapi/v1/account/apiRestrictions": {"subAccountName": "trend-01"}})
    assert gw.sub_account_name() == "trend-01"


def test_sub_account_name_is_none_for_a_non_mapping_probe_response() -> None:
    gw, _, _ = make_gateway({"/sapi/v1/account/apiRestrictions": ["trend-01"]})
    assert gw.sub_account_name() is None


# --------------------------------------------------------------------------- #
# Trading
# --------------------------------------------------------------------------- #


def _order_ack(request: httpx.Request) -> httpx.Response:
    q = dict(parse_qsl(urlparse(str(request.url)).query))
    return httpx.Response(
        200,
        json={
            "orderId": 777,
            "clientOrderId": q["newClientOrderId"],
            "symbol": q["symbol"],
            "side": q["side"],
            "type": q["type"],
            "origQty": q["quantity"],
            "price": q.get("price", "0"),
            "timeInForce": q.get("timeInForce", "GTC"),
            "reduceOnly": q.get("reduceOnly") == "true",
            "status": "NEW",
            "executedQty": "0",
            "avgPrice": "0",
            "updateTime": 4242,
        },
    )


def _trading_gateway(**kwargs: Any) -> tuple[BinanceGateway, Recorder, FakeClock]:
    return make_gateway(
        {
            "/fapi/v1/exchangeInfo": EXCHANGE_INFO,
            "/fapi/v1/fundingInfo": FUNDING_INFO,
            "/fapi/v1/order": _order_ack,
            "/fapi/v1/allOpenOrders": {"code": 200, "msg": "success"},
            "/fapi/v1/openOrders": [],
            "/fapi/v1/leverage": {"leverage": 5, "symbol": "BTCUSDT"},
            "/fapi/v1/marginType": {"code": 200, "msg": "success"},
        },
        **kwargs,
    )


def test_place_order_formats_qty_and_price_to_the_symbols_precision() -> None:
    gw, rec, _ = _trading_gateway()
    order = gw.place_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            qty=0.0123456,
            price=30_000.17,
            time_in_force=TimeInForce.GTX,
            intent="rebalance",
        )
    )
    params = rec.for_path("/fapi/v1/order")[0]
    assert params["quantity"] == "0.012"  # floored onto the 0.001 lot grid
    assert params["price"] == "30000.10"  # a buy rounds down onto the 0.10 tick
    assert params["timeInForce"] == "GTX"
    assert "reduceOnly" not in params
    assert order.order_id == "777"
    assert order.status is OrderStatus.NEW
    assert order.price == 30_000.10
    assert order.strategy is Strategy.TREND
    assert order.intent == "rebalance"


def test_place_order_rounds_a_sell_price_up_to_stay_passive() -> None:
    gw, rec, _ = _trading_gateway()
    gw.place_order(
        OrderRequest(symbol="BTCUSDT", side=Side.SELL, qty=1.0, price=30_000.11, intent="rebalance")
    )
    assert rec.for_path("/fapi/v1/order")[0]["price"] == "30000.20"


def test_place_order_rounds_an_ioc_price_towards_the_book_so_it_crosses() -> None:
    """5.9 step 3 escalates to IOC *at the current best price*.

    Rounded the passive way, a buy at an off-grid ask lands below it and the IOC
    expires unfilled; the escalation would appear to happen and do nothing.
    """
    gw, rec, _ = _trading_gateway()
    gw.place_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            qty=1.0,
            price=30_000.17,
            time_in_force=TimeInForce.IOC,
            intent="rebalance",
        )
    )
    assert rec.for_path("/fapi/v1/order")[0]["price"] == "30000.20"  # up, onto the ask


def test_place_order_rounds_an_ioc_sell_down_towards_the_bid() -> None:
    gw, rec, _ = _trading_gateway()
    gw.place_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=Side.SELL,
            qty=1.0,
            price=30_000.17,
            time_in_force=TimeInForce.IOC,
            intent="rebalance",
        )
    )
    assert rec.for_path("/fapi/v1/order")[0]["price"] == "30000.10"


def test_place_order_marks_reduce_only_orders() -> None:
    gw, rec, _ = _trading_gateway()
    order = gw.place_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=Side.SELL,
            qty=0.5,
            price=30_000.0,
            reduce_only=True,
            intent="kill_flatten",
        )
    )
    assert rec.for_path("/fapi/v1/order")[0]["reduceOnly"] == "true"
    assert order.reduce_only is True


def test_place_order_market_sends_no_price_or_time_in_force() -> None:
    gw, rec, _ = _trading_gateway()
    gw.place_order(OrderRequest(symbol="BTCUSDT", side=Side.BUY, qty=1.0, order_type=OrderType.MARKET))
    params = rec.for_path("/fapi/v1/order")[0]
    assert "price" not in params
    assert "timeInForce" not in params


def test_place_order_limit_without_a_price_is_rejected_before_any_request() -> None:
    gw, rec, _ = _trading_gateway()
    with pytest.raises(OrderRejected):
        gw.place_order(OrderRequest(symbol="BTCUSDT", side=Side.BUY, qty=1.0, price=None))
    assert rec.for_path("/fapi/v1/order") == []


def test_place_order_honours_an_explicit_client_order_id() -> None:
    gw, rec, _ = _trading_gateway()
    gw.place_order(
        OrderRequest(symbol="BTCUSDT", side=Side.BUY, qty=1.0, price=1.0, client_order_id="my-own-id_1")
    )
    assert rec.for_path("/fapi/v1/order")[0]["newClientOrderId"] == "my-own-id_1"


def test_generated_client_order_ids_are_deterministic_monotonic_and_venue_legal() -> None:
    gw, _, _ = _trading_gateway()
    req = OrderRequest(symbol="BTCUSDT", side=Side.BUY, qty=1.0, price=1.0, intent="governor cut/2")
    ids = [gw.next_client_order_id(req) for _ in range(3)]
    assert ids == [
        "TREND-governor-cut-2-000001",
        "TREND-governor-cut-2-000002",
        "TREND-governor-cut-2-000003",
    ]
    for coid in ids:
        assert len(coid) <= 36
        assert all(c.isalnum() or c in "-_" for c in coid)


def test_generated_client_order_id_is_truncated_to_36_characters() -> None:
    gw, _, _ = _trading_gateway()
    req = OrderRequest(
        symbol="BTCUSDT", side=Side.BUY, qty=1.0, price=1.0, intent="a-very-long-intent-name-indeed"
    )
    coid = gw.next_client_order_id(req)
    assert len(coid) <= 36


def test_a_read_only_gateway_refuses_every_write() -> None:
    gw, rec, _ = _trading_gateway(read_only=True)
    req = OrderRequest(symbol="BTCUSDT", side=Side.BUY, qty=1.0, price=1.0)
    for call in (
        lambda: gw.place_order(req),
        lambda: gw.cancel_order("BTCUSDT", "1"),
        lambda: gw.cancel_all("BTCUSDT"),
        lambda: gw.set_leverage("BTCUSDT", 5),
        lambda: gw.set_margin_type("BTCUSDT", "CROSSED"),
    ):
        with pytest.raises(ConfigError):
            call()
    assert rec.requests == []


def test_cancel_get_and_open_orders_round_trip() -> None:
    ack = {
        "orderId": 55,
        "clientOrderId": "abc",
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "LIMIT",
        "origQty": "1",
        "price": "30000",
        "timeInForce": "GTX",
        "reduceOnly": True,
        "status": "CANCELED",
        "executedQty": "0.25",
        "avgPrice": "30001",
        "time": 10,
        "updateTime": 20,
    }
    gw, _, _ = make_gateway({"/fapi/v1/order": ack, "/fapi/v1/openOrders": [ack]})
    cancelled = gw.cancel_order("BTCUSDT", "55")
    assert cancelled.status is OrderStatus.CANCELED
    assert cancelled.remaining_qty == pytest.approx(0.75)
    assert gw.get_order("BTCUSDT", "55").order_id == "55"
    assert [o.order_id for o in gw.open_orders()] == ["55"]
    assert gw.open_orders("BTCUSDT")[0].time_in_force is TimeInForce.GTX


def test_cancel_all_and_set_leverage_send_the_expected_parameters() -> None:
    gw, rec, _ = _trading_gateway()
    gw.cancel_all("BTCUSDT")
    gw.set_leverage("BTCUSDT", 5)
    assert rec.for_path("/fapi/v1/allOpenOrders")[0]["symbol"] == "BTCUSDT"
    assert rec.for_path("/fapi/v1/leverage")[0]["leverage"] == "5"


def test_set_margin_type_treats_no_change_needed_as_success() -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v1/marginType": lambda r: httpx.Response(
                400, json={"code": -4046, "msg": "No need to change margin type."}
            )
        }
    )
    gw.set_margin_type("BTCUSDT", "crossed")  # must not raise


def test_set_margin_type_accepts_minus_4046_whatever_the_message_says() -> None:
    """Idempotence is decided by the venue's code, not by an English sentence."""
    gw, _, _ = make_gateway(
        {
            "/fapi/v1/marginType": lambda r: httpx.Response(
                400, json={"code": -4046, "msg": "\u65e0\u9700\u66f4\u6539"}
            )
        }
    )
    gw.set_margin_type("BTCUSDT", "CROSSED")  # must not raise


def test_a_failure_whose_message_mentions_no_change_still_propagates() -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v1/marginType": lambda r: httpx.Response(
                400, json={"code": -4048, "msg": "No need to change: margin type is locked"}
            )
        }
    )
    with pytest.raises(GatewayError):
        gw.set_margin_type("BTCUSDT", "CROSSED")


def test_set_margin_type_propagates_a_real_failure() -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v1/marginType": lambda r: httpx.Response(
                400, json={"code": -4047, "msg": "Margin type cannot be changed with open position"}
            )
        }
    )
    with pytest.raises(GatewayError):
        gw.set_margin_type("BTCUSDT", "CROSSED")


def _order_ack_with(**overrides: Any) -> dict[str, Any]:
    ack = {
        "orderId": 5,
        "clientOrderId": "c",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "origQty": "1",
        "price": "30000",
        "timeInForce": "GTX",
        "status": "NEW",
        "executedQty": "0",
        "updateTime": 1,
    }
    ack.update(overrides)
    return ack


def test_an_expired_in_match_status_is_read_as_expired() -> None:
    """Binance adds statuses without notice; a new one must not be a ValueError."""
    gw, _, _ = make_gateway({"/fapi/v1/order": _order_ack_with(status="EXPIRED_IN_MATCH")})
    assert gw.get_order("BTCUSDT", "5").status is OrderStatus.EXPIRED


@pytest.mark.parametrize(
    "ack",
    [
        {"status": "NEW_ADL"},
        {"type": "TRAILING_STOP_MARKET"},
        {"timeInForce": "GTD"},
    ],
)
def test_an_unknown_venue_enum_raises_a_gateway_error_not_a_value_error(ack: dict[str, Any]) -> None:
    gw, _, _ = make_gateway({"/fapi/v1/order": _order_ack_with(**ack)})
    with pytest.raises(GatewayError):
        gw.get_order("BTCUSDT", "5")


def test_user_trades_are_parsed_ascending_with_fees_and_maker_flag() -> None:
    gw, _, _ = make_gateway(
        {
            "/fapi/v1/userTrades": [
                {
                    "id": 2,
                    "orderId": 9,
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "qty": "0.5",
                    "price": "31000",
                    "commission": "0.31",
                    "commissionAsset": "USDT",
                    "maker": False,
                    "time": 200,
                    "realizedPnl": "12.5",
                },
                {
                    "id": 1,
                    "orderId": 8,
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "qty": "0.5",
                    "price": "30000",
                    "commission": "0.06",
                    "commissionAsset": "BNB",
                    "maker": True,
                    "time": 100,
                },
            ]
        }
    )
    fills = gw.user_trades("BTCUSDT", start_ms=0)
    assert [f.trade_id for f in fills] == ["1", "2"]
    assert fills[0].is_maker is True
    assert fills[0].fee_asset == "BNB"
    assert fills[1].realized_pnl == 12.5
    assert fills[1].notional == pytest.approx(15_500.0)


# --------------------------------------------------------------------------- #
# Lifecycle & protocol
# --------------------------------------------------------------------------- #


def test_the_gateway_satisfies_the_exchange_gateway_protocol() -> None:
    gw, _, _ = make_gateway({})
    assert isinstance(gw, ExchangeGateway)
    gw.poll()  # no-op live
    gw.close()


def test_close_closes_the_http_client() -> None:
    gw, _, _ = make_gateway({})
    gw.close()
    assert gw._client.is_closed


def test_a_gateway_without_an_api_key_sends_no_api_key_header() -> None:
    cfg = AppConfig.model_validate({"mode": Mode.LIVE})
    rec = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.requests.append(request)
        return httpx.Response(200, json=TIME_ROUTE)

    gw = BinanceGateway(cfg, FakeClock(0), transport=httpx.MockTransport(handler))
    gw.server_time_ms()
    assert "X-MBX-APIKEY" not in rec.requests[0].headers


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


class _StubPaper:
    def __init__(self, cfg: AppConfig, clock: FakeClock, inner: Any) -> None:
        self.cfg = cfg
        self.clock = clock
        self.inner = inner


@pytest.fixture
def stub_paper_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("aegis.gateway.paper")
    module.PaperGateway = _StubPaper  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "aegis.gateway.paper", module)
    return module


def _cfg(mode: Mode) -> AppConfig:
    return AppConfig.model_validate({"mode": mode})


def test_build_gateway_backtest_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        build_gateway(_cfg(Mode.BACKTEST), FakeClock(0))


def test_build_gateway_demo_targets_the_testnet_base_url() -> None:
    gw = build_gateway(_cfg(Mode.DEMO), FakeClock(0))
    assert isinstance(gw, BinanceGateway)
    assert gw.base_url == "https://testnet.binancefuture.com"
    assert gw.read_only is False
    gw.close()


def test_build_gateway_live_targets_the_live_base_url() -> None:
    gw = build_gateway(_cfg(Mode.LIVE), FakeClock(0))
    assert isinstance(gw, BinanceGateway)
    assert gw.base_url == "https://fapi.binance.com"
    gw.close()


def test_build_gateway_paper_wraps_a_read_only_binance_client(
    stub_paper_module: types.ModuleType,
) -> None:
    gw = build_gateway(_cfg(Mode.PAPER), FakeClock(0))
    assert isinstance(gw, _StubPaper)
    assert isinstance(gw.inner, BinanceGateway)
    assert gw.inner.read_only is True
    gw.inner.close()


def test_build_gateway_paper_constructs_the_real_paper_gateway() -> None:
    """The stubbed-signature tests above prove nothing about the real class."""
    from aegis.gateway.fake import FakeGateway
    from aegis.gateway.paper import PaperGateway

    clock = FakeClock(0)
    inner = FakeGateway(clock)
    gw = build_gateway(_cfg(Mode.PAPER), clock, inner=inner)
    assert isinstance(gw, PaperGateway)
    assert gw.inner is inner
    assert gw.cfg.mode is Mode.PAPER


def test_build_gateway_paper_uses_an_injected_inner_gateway(
    stub_paper_module: types.ModuleType,
) -> None:
    sentinel, _, _ = make_gateway({})
    gw = build_gateway(_cfg(Mode.PAPER), FakeClock(0), inner=sentinel)
    assert gw.inner is sentinel
    sentinel.close()


def test_funding_history_deduplicates_a_repeated_settlement() -> None:
    row = {"symbol": "BTCUSDT", "fundingTime": DAY0, "fundingRate": "0.0001"}
    gw, _, _ = make_gateway({"/fapi/v1/fundingRate": [row, row]})
    assert len(gw.funding_history("BTCUSDT")) == 1


def test_income_stops_on_an_empty_page() -> None:
    gw, _, _ = make_gateway({"/fapi/v1/income": []})
    assert gw.income(DAY0) == []
