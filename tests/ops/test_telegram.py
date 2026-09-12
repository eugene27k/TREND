"""Telegram sink: formatting, and the "False, never raise" contract (US-T17 AC 3/4)."""

from __future__ import annotations

from aegis.core.config import load_config
from aegis.core.types import Alert, Severity, Strategy
from aegis.ops.alerts import AlertBus
from aegis.ops.telegram import MAX_MESSAGE_CHARS, TelegramSink, format_alert


class _Poster:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.sent: list[tuple[str, str]] = []

    def __call__(self, text: str, chat_id: str) -> bool:
        self.sent.append((text, chat_id))
        return self.result


def _cfg(**telegram):
    base = {"enabled": True, "chat_id": "-100", "prefix": "TREND"}
    base.update(telegram)
    return load_config("config/trend.yaml", {"telegram": base}, use_env=False)


def _alert(severity: Severity = Severity.WARN, code: str = "GOVERNOR", message: str = "cut to 0.5") -> Alert:
    return Alert(strategy=Strategy.TREND, ts_ms=0, severity=severity, code=code, message=message)


def test_the_body_is_prefix_severity_code_then_the_message() -> None:
    assert format_alert(_alert(), "TREND") == "TREND · WARN · GOVERNOR\ncut to 0.5"


def test_the_prefix_comes_from_config() -> None:
    poster = _Poster()
    TelegramSink(_cfg(prefix="TREND-DEMO"), poster)(_alert())

    assert poster.sent[0][0].startswith("TREND-DEMO · WARN · GOVERNOR")
    assert poster.sent[0][1] == "-100"


def test_a_disabled_sink_returns_false_and_never_posts() -> None:
    poster = _Poster()
    sink = TelegramSink(_cfg(enabled=False), poster)

    assert sink(_alert()) is False
    assert poster.sent == []


def test_a_failed_post_returns_false_so_the_bus_keeps_the_row_undelivered(clock, repos) -> None:
    poster = _Poster(result=False)
    bus = AlertBus(repos.alerts, clock, Strategy.TREND, sinks=[TelegramSink(_cfg(), poster)])

    bus.critical("HARD_HALT_DRAWDOWN", "flattening")

    assert len(poster.sent) == 1
    assert len(repos.alerts.undelivered()) == 1


def test_a_raising_poster_is_swallowed_and_reported_as_undelivered(clock, repos) -> None:
    def explode(text: str, chat_id: str) -> bool:
        raise RuntimeError("telegram is down")

    sink = TelegramSink(_cfg(), explode)
    assert sink(_alert()) is False


def test_a_successful_post_marks_the_alert_delivered(clock, repos) -> None:
    poster = _Poster()
    bus = AlertBus(repos.alerts, clock, Strategy.TREND, sinks=[TelegramSink(_cfg(), poster)])

    bus.warn("UNIVERSE_REFRESH", "16 symbols")

    assert repos.alerts.undelivered() == []
    assert poster.sent[0][0] == "TREND · WARN · UNIVERSE_REFRESH\n16 symbols"


def test_a_long_body_is_truncated_to_the_telegram_limit() -> None:
    poster = _Poster()
    TelegramSink(_cfg(), poster).send("x" * (MAX_MESSAGE_CHARS + 500))

    assert len(poster.sent[0][0]) == MAX_MESSAGE_CHARS


def test_a_sink_without_a_chat_id_is_disabled() -> None:
    poster = _Poster()
    assert TelegramSink(_cfg(chat_id=""), poster)(_alert()) is False
    assert poster.sent == []
