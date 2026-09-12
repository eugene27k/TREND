"""Point-in-time market data from the Binance public archive (PRD 11.1).

`data.binance.vision` is the only free source that *retains delisted symbols*,
which is the whole reason the backtest can be survivorship-free: a symbol that
existed in 2022 and was delisted in 2023 still has its 2022 files, so the
universe for March 2022 can be rebuilt from what was actually tradeable then
rather than from what happens to be listed today.

Downloads are cached on disk and never re-fetched, so a re-run of the backtest
is offline and therefore deterministic (11.4). The fetcher is injected, so every
test in this repository runs without a network.
"""

from __future__ import annotations

import csv
import io
import re
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from xml.etree import ElementTree

from aegis.core.errors import DataGap
from aegis.core.types import DailyBar, FundingRate

BASE = "https://data.binance.vision"
LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
KLINE_PREFIX = "data/futures/um/monthly/klines"
FUNDING_PREFIX = "data/futures/um/monthly/fundingRate"

Fetcher = Callable[[str], bytes]
"""Fetch a URL's bytes. Raises ``FileNotFoundError`` for a 404 (a normal
outcome: a symbol simply has no file for a month it did not exist in)."""

_MONTH_RE = re.compile(r"-(\d{4}-\d{2})\.zip$")


def http_fetch(url: str, timeout: float = 60.0) -> bytes:
    """Default fetcher. stdlib only — no module outside the gateway needs httpx."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        if exc.code == 404:
            raise FileNotFoundError(url) from exc
        raise
    except urllib.error.URLError as exc:  # pragma: no cover - network path
        raise ConnectionError(f"{url}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    symbol: str
    month: str
    kind: str  # klines | fundingRate

    @property
    def name(self) -> str:
        interval = "-1d" if self.kind == "klines" else "-fundingRate"
        return f"{self.symbol}{interval}-{self.month}.zip"

    @property
    def url(self) -> str:
        prefix = KLINE_PREFIX if self.kind == "klines" else FUNDING_PREFIX
        tail = f"{self.symbol}/1d" if self.kind == "klines" else self.symbol
        return f"{BASE}/{prefix}/{tail}/{self.name}"


class ArchiveLoader:
    """Cached reader over the monthly archive."""

    def __init__(self, cache_dir: str | Path, fetcher: Fetcher | None = None) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fetch: Fetcher = fetcher or http_fetch
        self._missing: set[str] = set()

    # -- raw bytes ---------------------------------------------------------- #

    def _cached(self, file: ArchiveFile) -> bytes | None:
        path = self.cache_dir / file.kind / file.symbol / file.name
        if path.exists():
            return path.read_bytes()
        return None

    def _store(self, file: ArchiveFile, payload: bytes) -> None:
        path = self.cache_dir / file.kind / file.symbol / file.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def _load(self, file: ArchiveFile) -> bytes | None:
        """Bytes for one archive file, or None when the month does not exist."""
        if file.url in self._missing:
            return None
        cached = self._cached(file)
        if cached is not None:
            return cached
        try:
            payload = self.fetch(file.url)
        except FileNotFoundError:
            self._missing.add(file.url)
            return None
        self._store(file, payload)
        return payload

    # -- parsed records ----------------------------------------------------- #

    def daily_bars(self, symbol: str, month: str) -> list[DailyBar]:
        """Daily klines for one symbol-month, ascending. Empty when absent."""
        payload = self._load(ArchiveFile(symbol, month, "klines"))
        if payload is None:
            return []
        return [_parse_kline(symbol, row) for row in _rows(payload) if row]

    def funding(self, symbol: str, month: str) -> list[FundingRate]:
        payload = self._load(ArchiveFile(symbol, month, "fundingRate"))
        if payload is None:
            return []
        out: list[FundingRate] = []
        for row in _rows(payload):
            if not row or len(row) < 3:
                continue
            try:
                ts, rate = int(float(row[0])), float(row[2])
            except (TypeError, ValueError):
                continue
            out.append(FundingRate(symbol=symbol, funding_time_ms=ts, rate=rate))
        out.sort(key=lambda f: f.funding_time_ms)
        return out

    def bars_range(self, symbol: str, months: Iterable[str]) -> list[DailyBar]:
        bars: list[DailyBar] = []
        for month in months:
            bars.extend(self.daily_bars(symbol, month))
        bars.sort(key=lambda b: b.day)
        return _dedupe(bars)

    # -- inventory ---------------------------------------------------------- #

    def symbols(self) -> list[str]:
        """Every USDS-M symbol the archive has ever carried, delisted included."""
        payload = self._listing(f"{KLINE_PREFIX}/")
        return sorted(payload)

    def months_for(self, symbol: str) -> list[str]:
        """The months this symbol has a daily-kline file for — its listing history."""
        prefixes = self._listing(f"{KLINE_PREFIX}/{symbol}/1d/", files=True)
        months = {m.group(1) for name in prefixes if (m := _MONTH_RE.search(name))}
        return sorted(months)

    def inventory(self, start_month: str, end_month: str,
                  symbols: Iterable[str] | None = None) -> dict[str, list[str]]:
        """``{symbol: [months present]}`` clipped to the window (PRD 11.1)."""
        out: dict[str, list[str]] = {}
        for symbol in symbols if symbols is not None else self.symbols():
            months = [m for m in self.months_for(symbol) if start_month <= m <= end_month]
            if months:
                out[symbol] = months
        return out

    def _listing(self, prefix: str, *, files: bool = False) -> list[str]:
        """One page-walked S3 listing. Cached like everything else."""
        cache = self.cache_dir / "listing" / (prefix.replace("/", "_") + ".txt")
        if cache.exists():
            return [line for line in cache.read_text().splitlines() if line]

        names: list[str] = []
        marker = ""
        while True:
            url = f"{LISTING}?delimiter=/&prefix={prefix}"
            if marker:
                url += f"&marker={marker}"
            try:
                body = self.fetch(url)
            except FileNotFoundError:
                break
            page, truncated, last = _parse_listing(body, prefix, files=files)
            names.extend(page)
            if not truncated or not last:
                break
            marker = last

        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("\n".join(names))
        return names


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _rows(payload: bytes) -> list[list[str]]:
    """CSV rows from a zipped archive member, skipping any header line."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            name = zf.namelist()[0]
            text = zf.read(name).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, IndexError) as exc:
        raise DataGap(f"unreadable archive member: {exc}") from exc
    rows = list(csv.reader(io.StringIO(text)))
    if rows and rows[0] and not rows[0][0].strip().lstrip("-").replace(".", "", 1).isdigit():
        rows = rows[1:]          # the archive gained a header row in 2025
    return rows


def _parse_kline(symbol: str, row: list[str]) -> DailyBar:
    open_ms = int(float(row[0]))
    close_ms = int(float(row[6]))
    return DailyBar(
        symbol=symbol,
        day=datetime.fromtimestamp(open_ms / 1000, tz=UTC).date(),
        open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]),
        volume=float(row[5]), quote_volume=float(row[7]),
        open_time_ms=open_ms, close_time_ms=close_ms, source="archive",
    )


def _dedupe(bars: list[DailyBar]) -> list[DailyBar]:
    """Month files can overlap at a boundary; the later read wins."""
    by_day: dict[date, DailyBar] = {}
    for bar in bars:
        by_day[bar.day] = bar
    return [by_day[d] for d in sorted(by_day)]


def _parse_listing(body: bytes, prefix: str, *, files: bool) -> tuple[list[str], bool, str]:
    """Extract names from one S3 ListBucketResult page."""
    ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
    root = ElementTree.fromstring(body)
    truncated = (root.findtext(f"{ns}IsTruncated") or "false").lower() == "true"
    names: list[str] = []
    last = ""
    if files:
        for item in root.findall(f"{ns}Contents"):
            key = item.findtext(f"{ns}Key") or ""
            last = key
            if key.endswith(".zip"):
                names.append(key.rsplit("/", 1)[-1])
    else:
        for item in root.findall(f"{ns}CommonPrefixes"):
            value = item.findtext(f"{ns}Prefix") or ""
            last = value
            trimmed = value[len(prefix):].strip("/")
            if trimmed:
                names.append(trimmed)
    return names, truncated, last


__all__ = ["BASE", "ArchiveFile", "ArchiveLoader", "Fetcher", "http_fetch"]
