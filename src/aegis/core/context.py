"""The engine context — one object carrying everything a service needs.

Services take a ``Context`` rather than a constructor full of collaborators.
That keeps every service testable with one fixture, and it makes the
dependency direction obvious: services depend on the context, never on each
other's internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aegis.core.clock import Clock
from aegis.core.config import AppConfig
from aegis.core.types import Strategy

if TYPE_CHECKING:
    from aegis.gateway.base import ExchangeGateway
    from aegis.ops.alerts import AlertBus
    from aegis.storage.repositories import Repositories


@dataclass(frozen=True, slots=True)
class Context:
    cfg: AppConfig
    clock: Clock
    gateway: ExchangeGateway
    repos: Repositories
    alerts: AlertBus

    @property
    def strategy(self) -> Strategy:
        return self.cfg.strategy

    def now_ms(self) -> int:
        return self.clock.now_ms()


__all__ = ["Context"]
