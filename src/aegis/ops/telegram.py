"""Telegram delivery sink.

This is a ``Sink`` for ``aegis.ops.alerts.AlertBus`` and nothing more: it takes
an ``Alert`` and returns whether it was delivered. It never raises, because the
bus treats a False as "leave the row undelivered and retry on the next flush" —
whereas an exception escaping a sink would make a chat outage look like an
engine fault. Persist-then-deliver is what lets the operator reconstruct the day
from the database even if Telegram was down for all of it.

The poster is injected so that tests exercise the formatting and the
failure semantics without a network (PRD Section 14).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from aegis.core.config import AppConfig, TelegramConfig
from aegis.core.types import Alert

Poster = Callable[[str, str], bool]
"""``(text, chat_id) -> delivered``. Never raises."""

API_BASE = "https://api.telegram.org"
POST_TIMEOUT_S = 10.0
MAX_MESSAGE_CHARS = 4096


def make_poster(token: str, *, base: str = API_BASE) -> Poster:
    """A urllib-based ``sendMessage`` poster. Only the gateway may use httpx."""

    def post(text: str, chat_id: str) -> bool:
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
        url = f"{base}/bot{token}/sendMessage"
        try:
            with urllib.request.urlopen(url, data=payload, timeout=POST_TIMEOUT_S) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
                return bool(body.get("ok"))
        except (urllib.error.URLError, OSError, ValueError):
            return False

    return post


def format_alert(alert: Alert, prefix: str) -> str:
    """``"TREND - SEVERITY - CODE\\nmessage"`` (Appendix D header, then the body)."""
    head = f"{prefix} · {alert.severity} · {alert.code}"
    return f"{head}\n{alert.message}" if alert.message else head


class TelegramSink:
    """Callable alert sink. Disabled or failed delivery returns False, never raises."""

    def __init__(self, cfg: AppConfig | TelegramConfig, poster: Poster | None = None) -> None:
        self.cfg: TelegramConfig = cfg.telegram if isinstance(cfg, AppConfig) else cfg
        self.poster = poster or (make_poster(self.cfg.bot_token) if self.cfg.bot_token else None)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.chat_id and self.poster is not None)

    def __call__(self, alert: Alert) -> bool:
        if not self.enabled:
            return False
        return self.send(format_alert(alert, self.cfg.prefix))

    def send(self, body: str) -> bool:
        """Deliver a raw body (a report, say). False on any failure."""
        if not self.enabled:
            return False
        assert self.poster is not None
        try:
            return bool(self.poster(body[:MAX_MESSAGE_CHARS], self.cfg.chat_id))
        except Exception:  # a broken channel must never stop the engine
            return False


__all__ = [
    "API_BASE",
    "MAX_MESSAGE_CHARS",
    "POST_TIMEOUT_S",
    "Poster",
    "TelegramSink",
    "format_alert",
    "make_poster",
]
