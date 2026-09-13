"""Liveness beat.

The heartbeat answers a question no internal metric can: *is the process alive
at all?* It is therefore recorded locally **and** pushed to an external watcher
(Healthchecks), and the local record is written first — a beat the engine took
but could not deliver is still evidence of uptime, and the P1 gate (Section 7,
heartbeat uptime >= 99.5 %) is computed from the local rows, not from the
watcher's view.

The ping is injected rather than imported so the tests that assert the recording
behaviour never touch the network (PRD Section 14).
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from aegis.core.context import Context

Pinger = Callable[[str, bool, str], bool]
"""``(url, ok, detail) -> delivered``. Never raises; failure is a False."""

PING_TIMEOUT_S = 5.0


def urllib_pinger(url: str, ok: bool, detail: str) -> bool:
    """Default channel: a Healthchecks GET, ``/fail`` when the beat is unhealthy."""
    target = url if ok else url.rstrip("/") + "/fail"
    try:
        request = urllib.request.Request(target, data=detail.encode("utf-8") if detail else None)
        with urllib.request.urlopen(request, timeout=PING_TIMEOUT_S) as response:
            return 200 <= int(response.status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


class Heartbeat:
    """Records a beat locally, then pings the external watcher when configured."""

    def __init__(self, ctx: Context, pinger: Pinger | None = None) -> None:
        self.ctx = ctx
        self.pinger = pinger or urllib_pinger
        self.last_ping_ok: bool | None = None

    def beat(self, now_ms: int, ok: bool, detail: str = "") -> None:
        """Persist the beat unconditionally; ping only when enabled and configured."""
        self.ctx.repos.heartbeats.add(now_ms, ok, detail)
        cfg = self.ctx.cfg.heartbeat
        if not cfg.enabled or not cfg.url:
            self.last_ping_ok = None
            return
        try:
            self.last_ping_ok = bool(self.pinger(cfg.url, ok, detail))
        except Exception:  # a broken watcher must never stop the engine
            self.last_ping_ok = False

    def uptime_pct(self, start_ms: int, end_ms: int) -> float:
        """Healthy beats over the beats the configured interval expected (Section 7 P1)."""
        return self.ctx.repos.heartbeats.uptime_pct(start_ms, end_ms, self.ctx.cfg.heartbeat.interval_s)

    def prune(self, before_ms: int) -> None:
        """Heartbeats are the highest-volume table on the free host; keep it bounded."""
        self.ctx.repos.heartbeats.prune(before_ms)


__all__ = ["PING_TIMEOUT_S", "Heartbeat", "Pinger", "urllib_pinger"]
