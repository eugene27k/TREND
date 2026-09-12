"""Heartbeat: local record first, external ping second (US-T19, P1 uptime gate)."""

from __future__ import annotations

from aegis.core.clock import DAY_MS, FakeClock
from aegis.core.config import load_config
from aegis.core.context import Context
from aegis.core.types import Strategy
from aegis.ops.alerts import AlertBus
from aegis.ops.heartbeat import Heartbeat


class _Recorder:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[str, bool, str]] = []

    def __call__(self, url: str, ok: bool, detail: str) -> bool:
        self.calls.append((url, ok, detail))
        return self.result


def _ctx(clock: FakeClock, gateway, repos, **heartbeat) -> Context:
    cfg = load_config("config/trend.yaml", {"heartbeat": heartbeat}, use_env=False)
    return Context(
        cfg=cfg,
        clock=clock,
        gateway=gateway,
        repos=repos,
        alerts=AlertBus(repos.alerts, clock, Strategy.TREND),
    )


def test_a_disabled_heartbeat_still_records_the_beat(clock, gateway, repos) -> None:
    ctx = _ctx(clock, gateway, repos, enabled=False, url="https://hc.example/abc")
    pinger = _Recorder()
    heartbeat = Heartbeat(ctx, pinger)

    heartbeat.beat(clock.now_ms(), True, "tick")

    assert pinger.calls == []
    assert repos.heartbeats.count(clock.now_ms() - 1, clock.now_ms() + 1) == 1
    assert heartbeat.uptime_pct(clock.now_ms() - 1, clock.now_ms() + 1) == 100.0


def test_an_enabled_heartbeat_without_a_url_records_but_does_not_ping(clock, gateway, repos) -> None:
    ctx = _ctx(clock, gateway, repos, enabled=True, url="")
    pinger = _Recorder()
    Heartbeat(ctx, pinger).beat(clock.now_ms(), True)

    assert pinger.calls == []
    assert repos.heartbeats.count(0, clock.now_ms() + 1) == 1


def test_an_enabled_heartbeat_pings_the_configured_url(clock, gateway, repos) -> None:
    ctx = _ctx(clock, gateway, repos, enabled=True, url="https://hc.example/abc")
    pinger = _Recorder()
    heartbeat = Heartbeat(ctx, pinger)

    heartbeat.beat(clock.now_ms(), False, "bars missing")

    assert pinger.calls == [("https://hc.example/abc", False, "bars missing")]
    assert heartbeat.last_ping_ok is True


def test_a_failing_ping_never_stops_the_engine(clock, gateway, repos) -> None:
    ctx = _ctx(clock, gateway, repos, enabled=True, url="https://hc.example/abc")

    def explode(url: str, ok: bool, detail: str) -> bool:
        raise RuntimeError("network is down")

    heartbeat = Heartbeat(ctx, explode)
    heartbeat.beat(clock.now_ms(), True)

    assert heartbeat.last_ping_ok is False
    assert repos.heartbeats.count(0, clock.now_ms() + 1) == 1


def test_uptime_pct_counts_failed_beats_against_the_p1_gate(clock, gateway, repos) -> None:
    ctx = _ctx(clock, gateway, repos, enabled=False)
    heartbeat = Heartbeat(ctx, _Recorder())
    start = clock.now_ms()
    for i in range(10):
        clock.advance(minutes=5)
        heartbeat.beat(clock.now_ms(), i != 3)

    assert heartbeat.uptime_pct(start, clock.now_ms() + 1) == 90.0


def test_prune_bounds_the_table_on_the_free_host(clock, gateway, repos) -> None:
    ctx = _ctx(clock, gateway, repos, enabled=False)
    heartbeat = Heartbeat(ctx, _Recorder())
    old = clock.now_ms()
    heartbeat.beat(old, True)
    clock.advance(days=3)
    heartbeat.beat(clock.now_ms(), True)

    heartbeat.prune(clock.now_ms() - DAY_MS)

    assert repos.heartbeats.count(0, clock.now_ms() + 1) == 1


def test_the_default_pinger_posts_to_the_url_and_reports_the_status(monkeypatch) -> None:
    import aegis.ops.heartbeat as hb

    seen: list[str] = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

    def fake_urlopen(request, timeout=0):
        seen.append(request.full_url)
        return _Response()

    monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

    assert hb.urllib_pinger("https://hc.example/abc", True, "") is True
    assert hb.urllib_pinger("https://hc.example/abc", False, "down") is True
    assert seen == ["https://hc.example/abc", "https://hc.example/abc/fail"]


def test_the_default_pinger_returns_false_when_the_watcher_is_unreachable(monkeypatch) -> None:
    import aegis.ops.heartbeat as hb

    def explode(request, timeout=0):
        raise OSError("no route to host")

    monkeypatch.setattr(hb.urllib.request, "urlopen", explode)

    assert hb.urllib_pinger("https://hc.example/abc", True, "") is False
