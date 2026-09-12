"""Alert bus.

Every notable event goes through here: it is persisted first and delivered
second, so an alert survives a Telegram outage and the operator can always
reconstruct what the bot saw from the database alone. Repeat suppression lives
here too, because a CRITICAL that repeats every 60 s is indistinguishable from
no alert at all.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aegis.core.clock import Clock
from aegis.core.types import Alert, Severity, Strategy
from aegis.storage.repositories import AlertRepo

Sink = Callable[[Alert], bool]
"""A delivery channel. Returns True when the alert was delivered."""


class AlertBus:
    """Persist-then-deliver, with per-code repeat suppression."""

    def __init__(self, repo: AlertRepo, clock: Clock, strategy: Strategy,
                 sinks: list[Sink] | None = None, *, repeat_minutes: float = 15.0) -> None:
        self.repo = repo
        self.clock = clock
        self.strategy = strategy
        self.sinks: list[Sink] = sinks or []
        self.repeat_ms = int(repeat_minutes * 60_000)

    def add_sink(self, sink: Sink) -> None:
        self.sinks.append(sink)

    def emit(self, severity: Severity, code: str, message: str,
             context: dict[str, Any] | None = None, *, suppress_repeat: bool = True) -> int | None:
        """Record an alert and try to deliver it. Returns the row id, or None if suppressed.

        Suppression is by ``code`` only: the same condition re-detected on the
        next supervisor tick is the same alert, and re-sending it every minute
        would train the operator to ignore the channel. A CRITICAL that has not
        been acknowledged is re-sent on the repeat interval (US-T17 AC 3).
        """
        now = self.clock.now_ms()
        if suppress_repeat:
            last = self.repo.last_of_code(code)
            if last is not None and now - int(last["ts"]) < self.repeat_ms:
                return None

        alert = Alert(strategy=self.strategy, ts_ms=now, severity=severity, code=code,
                      message=message, context=context or {})
        alert_id = self.repo.add(alert)
        self._deliver(alert, alert_id)
        return alert_id

    def info(self, code: str, message: str, context: dict[str, Any] | None = None) -> int | None:
        return self.emit(Severity.INFO, code, message, context)

    def warn(self, code: str, message: str, context: dict[str, Any] | None = None) -> int | None:
        return self.emit(Severity.WARN, code, message, context)

    def critical(self, code: str, message: str, context: dict[str, Any] | None = None) -> int | None:
        return self.emit(Severity.CRITICAL, code, message, context)

    def _deliver(self, alert: Alert, alert_id: int) -> None:
        delivered = False
        for sink in self.sinks:
            try:
                delivered = bool(sink(alert)) or delivered
            except Exception:  # a broken channel must never stop the engine
                continue
        if delivered:
            self.repo.mark_delivered(alert_id)

    def flush_undelivered(self) -> int:
        """Retry anything a sink failed to take earlier. Returns how many got through."""
        sent = 0
        for row in self.repo.undelivered():
            alert = Alert(strategy=self.strategy, ts_ms=row["ts"], severity=Severity(row["severity"]),
                          code=row["code"], message=row["message"])
            before = self.repo.undelivered()
            self._deliver(alert, int(row["id"]))
            if len(self.repo.undelivered()) < len(before):
                sent += 1
        return sent


class NullAlertBus(AlertBus):
    """An alert bus with no sinks — used in tests that do not assert on delivery."""

    def __init__(self, repo: AlertRepo, clock: Clock, strategy: Strategy) -> None:
        super().__init__(repo, clock, strategy, sinks=[])


__all__ = ["AlertBus", "NullAlertBus", "Sink"]
