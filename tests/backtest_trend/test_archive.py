"""PRD 11.1 — the public-archive loader. Offline: the fetcher is injected."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date

import pytest

from aegis.backtest_trend.archive import ArchiveFile, ArchiveLoader
from aegis.core.errors import DataGap

KLINES = [
    # open_time, open, high, low, close, volume, close_time, quote_volume, ...
    [1640995200000, "100.0", "110.0", "95.0", "105.0", "1000", 1641081599999, "105000", 10, "5", "5", "0"],
    [1641081600000, "105.0", "115.0", "100.0", "112.0", "1200", 1641167999999, "134400", 12, "6", "6", "0"],
]
FUNDING = [
    [1640995200000, "USDT", "0.0001"],
    [1641024000000, "USDT", "-0.00005"],
]


def _zip(rows, name="data.csv", header: list[str] | None = None) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    if header:
        writer.writerow(header)
    writer.writerows(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(name, buf.getvalue())
    return out.getvalue()


def _listing(prefixes: list[str], *, keys: list[str] | None = None, truncated: bool = False) -> bytes:
    ns = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'
    body = [f"<ListBucketResult {ns}><IsTruncated>{str(truncated).lower()}</IsTruncated>"]
    for p in prefixes:
        body.append(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>")
    for k in keys or []:
        body.append(f"<Contents><Key>{k}</Key></Contents>")
    body.append("</ListBucketResult>")
    return "".join(body).encode()


@pytest.fixture
def loader(tmp_path):
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        if "BTCUSDT-1d-2022-01.zip" in url:
            return _zip(KLINES)
        if "BTCUSDT-fundingRate-2022-01.zip" in url:
            return _zip(FUNDING)
        if "prefix=data/futures/um/monthly/klines/BTCUSDT/1d/" in url:
            return _listing(
                [],
                keys=[
                    "data/futures/um/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2022-01.zip",
                    "data/futures/um/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2022-02.zip",
                    "data/futures/um/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2022-01.zip.CHECKSUM",
                ],
            )
        if "prefix=data/futures/um/monthly/klines/" in url:
            return _listing(
                ["data/futures/um/monthly/klines/BTCUSDT/", "data/futures/um/monthly/klines/DEADUSDT/"]
            )
        raise FileNotFoundError(url)

    ld = ArchiveLoader(tmp_path, fetcher=fetch)
    ld.calls = calls  # type: ignore[attr-defined]
    return ld


def test_url_layout_matches_the_binance_archive():
    assert ArchiveFile("BTCUSDT", "2022-03", "klines").url == (
        "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2022-03.zip"
    )
    assert ArchiveFile("BTCUSDT", "2022-03", "fundingRate").url == (
        "https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/"
        "BTCUSDT-fundingRate-2022-03.zip"
    )


def test_daily_bars_are_parsed_with_quote_volume_from_field_7(loader):
    bars = loader.daily_bars("BTCUSDT", "2022-01")
    assert [b.day for b in bars] == [date(2022, 1, 1), date(2022, 1, 2)]
    assert bars[0].close == 105.0
    assert bars[0].quote_volume == 105_000.0
    assert bars[0].source == "archive"


def test_a_header_row_is_skipped(tmp_path):
    header = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume",
        "count",
        "taker_buy_volume",
        "taker_buy_quote_volume",
        "ignore",
    ]
    ld = ArchiveLoader(tmp_path, fetcher=lambda url: _zip(KLINES, header=header))
    assert len(ld.daily_bars("BTCUSDT", "2022-01")) == 2


def test_funding_rows_are_parsed_and_sorted(loader):
    rates = loader.funding("BTCUSDT", "2022-01")
    assert [r.rate for r in rates] == [0.0001, -0.00005]
    assert rates[0].funding_time_ms < rates[1].funding_time_ms


def test_a_missing_month_is_empty_not_an_error(loader):
    assert loader.daily_bars("BTCUSDT", "2019-01") == []
    assert loader.funding("BTCUSDT", "2019-01") == []


def test_a_missing_month_is_not_refetched(loader):
    loader.daily_bars("BTCUSDT", "2019-01")
    before = len(loader.calls)
    loader.daily_bars("BTCUSDT", "2019-01")
    assert len(loader.calls) == before, "a known-absent month must not be re-requested"


def test_downloads_are_cached_on_disk(loader, tmp_path):
    loader.daily_bars("BTCUSDT", "2022-01")
    fetches = len(loader.calls)
    loader.daily_bars("BTCUSDT", "2022-01")
    assert len(loader.calls) == fetches, "a cached month must not be re-fetched"

    # A brand-new loader over the same cache also stays offline.
    def explode(url: str) -> bytes:
        raise AssertionError(f"should not fetch {url}")

    assert len(ArchiveLoader(tmp_path, fetcher=explode).daily_bars("BTCUSDT", "2022-01")) == 2


def test_symbol_inventory_includes_delisted_symbols(loader):
    """The whole point of the archive (PRD 11.1)."""
    assert loader.symbols() == ["BTCUSDT", "DEADUSDT"]


def test_months_for_reads_the_listing_and_ignores_checksum_files(loader):
    assert loader.months_for("BTCUSDT") == ["2022-01", "2022-02"]


def test_inventory_clips_to_the_requested_window(loader):
    assert loader.inventory("2022-02", "2022-12", symbols=["BTCUSDT"]) == {"BTCUSDT": ["2022-02"]}
    assert loader.inventory("2023-01", "2023-12", symbols=["BTCUSDT"]) == {}


def test_bars_range_concatenates_and_dedupes(loader, tmp_path):
    bars = loader.bars_range("BTCUSDT", ["2022-01", "2022-01"])
    assert [b.day for b in bars] == [date(2022, 1, 1), date(2022, 1, 2)]


def test_a_corrupt_archive_member_raises_datagap(tmp_path):
    ld = ArchiveLoader(tmp_path, fetcher=lambda url: b"not a zip")
    with pytest.raises(DataGap):
        ld.daily_bars("BTCUSDT", "2022-01")


# --------------------------------------------------------------------------- #
# PRD 11.1 — "verified against REST klines for the last 90 days"
# --------------------------------------------------------------------------- #


def _bar(symbol: str, day: date, close: float = 100.0, quote_volume: float = 1_000.0) -> "DailyBar":
    from aegis.core.types import DailyBar

    return DailyBar(symbol, day, close - 1, close + 1, close - 2, close, 10.0, quote_volume, 0, 0)


def test_matching_archive_and_rest_bars_verify_clean():
    from aegis.backtest_trend.archive import verify_against_rest

    days = [date(2026, 9, 1) + __import__("datetime").timedelta(days=i) for i in range(30)]
    archive = [_bar("BTCUSDT", d, 100.0 + i) for i, d in enumerate(days)]
    result = verify_against_rest(archive, list(archive), symbol="BTCUSDT")
    assert result.ok
    assert result.compared == 30
    assert "30 days match" in result.summary()


def test_a_rescaled_quote_volume_is_caught():
    """The failure that would silently change which symbols the universe picks."""
    from aegis.backtest_trend.archive import verify_against_rest

    day = date(2026, 9, 1)
    archive = [_bar("BTCUSDT", day, quote_volume=1_000.0)]
    rest = [_bar("BTCUSDT", day, quote_volume=1_000_000.0)]
    result = verify_against_rest(archive, rest, symbol="BTCUSDT")
    assert not result.ok
    assert [m.field for m in result.mismatches] == ["quote_volume"]
    assert "quote_volume" in result.summary()


def test_a_shifted_day_boundary_shows_up_as_a_close_mismatch():
    from aegis.backtest_trend.archive import verify_against_rest

    days = [date(2026, 9, 1), date(2026, 9, 2)]
    archive = [_bar("BTCUSDT", days[0], 100.0), _bar("BTCUSDT", days[1], 110.0)]
    rest = [_bar("BTCUSDT", days[0], 110.0), _bar("BTCUSDT", days[1], 120.0)]
    result = verify_against_rest(archive, rest, symbol="BTCUSDT")
    assert not result.ok
    assert {m.day for m in result.mismatches} == set(days)


def test_a_day_missing_from_the_archive_is_a_defect():
    from aegis.backtest_trend.archive import verify_against_rest

    days = [date(2026, 9, 1), date(2026, 9, 2)]
    result = verify_against_rest([_bar("BTCUSDT", days[0])],
                                 [_bar("BTCUSDT", d) for d in days], symbol="BTCUSDT")
    assert not result.ok
    assert result.missing_in_archive == (days[1],)
    assert "absent from the archive" in result.summary()


def test_a_day_missing_from_rest_is_not_a_defect():
    """REST's window is shorter than the archive's; it is the archive being checked."""
    from aegis.backtest_trend.archive import verify_against_rest

    days = [date(2026, 9, 1), date(2026, 9, 2)]
    result = verify_against_rest([_bar("BTCUSDT", d) for d in days],
                                 [_bar("BTCUSDT", days[1])], symbol="BTCUSDT")
    assert result.ok
    assert result.missing_in_rest == (days[0],)


def test_tiny_float_differences_are_tolerated():
    from aegis.backtest_trend.archive import verify_against_rest

    day = date(2026, 9, 1)
    archive = [_bar("BTCUSDT", day, 100.0)]
    rest = [_bar("BTCUSDT", day, 100.0 * (1 + 1e-9))]
    assert verify_against_rest(archive, rest, symbol="BTCUSDT").ok


def test_no_overlap_reports_nothing_compared():
    from aegis.backtest_trend.archive import verify_against_rest

    result = verify_against_rest([], [], symbol="BTCUSDT")
    assert result.compared == 0
    assert "no overlapping days" in result.summary()
