"""Operations page (US-T18 AC 6, US-T19 AC 2-3).

Is the process alive (heartbeat uptime over the windows the phase gates use),
does it agree with the exchange (reconciliation), is it backed up, what has it
been shouting about (alerts), what have humans done to it (control log), and
what does it cost to run (infra, with RSS/CPU when the host recorded them as
metrics — they are optional, so the fields are nullable rather than invented).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from aegis.api.deps import MetricIndex, StrategyDeps, engine_state, get_registry, get_sleeve
from aegis.storage.db import json_loads

router = APIRouter(prefix="/api/{strategy}", tags=["operations"])

_DAY_MS = 86_400_000


class HeartbeatPanel(BaseModel):
    enabled: bool
    interval_s: int
    last_ts: int | None
    last_ok: bool | None
    beats_24h: int
    uptime_24h_pct: float | None
    uptime_7d_pct: float | None
    uptime_30d_pct: float | None


class ReconciliationPanel(BaseModel):
    last_ts: int | None
    last_kind: str | None
    last_ok: bool | None
    last_detail: str
    open_breaks: int
    breaks: list[dict]


class BackupPanel(BaseModel):
    enabled: bool
    replica_path: str
    last_ok_ts: int | None
    lag_s: float | None
    restore_check_days: int


class AlertRow(BaseModel):
    id: int
    ts: int
    severity: str
    code: str
    message: str
    acked: bool
    delivered: bool
    context: dict


class ControlRow(BaseModel):
    id: int
    ts: int
    action: str
    operator: str
    reason: str
    payload: dict


class InfraPanel(BaseModel):
    host: str
    monthly_cost_eur: float
    max_rss_mb: int
    rss_mb: float | None
    cpu_pct: float | None
    net_of_infra: float | None


class ReportRow(BaseModel):
    kind: str
    period_key: str
    ts: int
    delivered: bool


class OperationsResponse(BaseModel):
    strategy: str
    as_of_ts: int
    mode: str
    state: dict
    heartbeat: HeartbeatPanel
    reconciliation: ReconciliationPanel
    backup: BackupPanel
    infra: InfraPanel
    alerts: list[AlertRow]
    unacked_critical: int
    control_log: list[ControlRow]
    reports: list[ReportRow]


def _uptime(repos: Any, now_ms: int, days: int, interval_s: float) -> float | None:
    """None when no beat was recorded — "no evidence" is not "0 % uptime"."""
    start = now_ms - days * _DAY_MS
    if repos.heartbeats.count(start, now_ms + 1) == 0:
        return None
    return repos.heartbeats.uptime_pct(start, now_ms + 1, interval_s)


@router.get("/operations", response_model=OperationsResponse)
def operations(
    request: Request,
    alert_limit: int = Query(50, ge=1, le=500),
    control_limit: int = Query(50, ge=1, le=500),
    sleeve: StrategyDeps = Depends(get_sleeve),
) -> OperationsResponse:
    repos = sleeve.repos
    cfg = sleeve.cfg
    now_ms = get_registry(request).now_ms()
    metrics = MetricIndex(repos)

    beats = repos.heartbeats
    last_beat = beats.last()
    heartbeat = HeartbeatPanel(
        enabled=cfg.heartbeat.enabled,
        interval_s=cfg.heartbeat.interval_s,
        last_ts=int(last_beat["ts"]) if last_beat else None,
        last_ok=bool(last_beat["ok"]) if last_beat else None,
        beats_24h=beats.count(now_ms - _DAY_MS, now_ms + 1),
        uptime_24h_pct=_uptime(repos, now_ms, 1, cfg.heartbeat.interval_s),
        uptime_7d_pct=_uptime(repos, now_ms, 7, cfg.heartbeat.interval_s),
        uptime_30d_pct=_uptime(repos, now_ms, 30, cfg.heartbeat.interval_s),
    )

    last_recon = repos.reconciliations.latest()
    open_breaks = repos.reconciliations.open_breaks()
    reconciliation = ReconciliationPanel(
        last_ts=int(last_recon["ts"]) if last_recon else None,
        last_kind=str(last_recon["kind"]) if last_recon else None,
        last_ok=bool(last_recon["ok"]) if last_recon else None,
        last_detail=str(last_recon["detail"]) if last_recon else "",
        open_breaks=len(open_breaks),
        breaks=[
            {
                "id": b["id"],
                "ts": b["ts"],
                "kind": b["kind"],
                "detail": b["detail"],
                "breaks": json_loads(b["breaks_json"], []),
            }
            for b in open_breaks
        ],
    )

    backup_alert = repos.alerts.last_of_code("BACKUP_OK")
    last_backup = int(backup_alert["ts"]) if backup_alert else None
    backup = BackupPanel(
        enabled=cfg.backup.enabled,
        replica_path=cfg.backup.litestream_replica_path,
        last_ok_ts=last_backup,
        lag_s=(now_ms - last_backup) / 1000.0 if last_backup is not None else None,
        restore_check_days=cfg.backup.restore_check_days,
    )

    infra = InfraPanel(
        host=cfg.infra.host,
        monthly_cost_eur=cfg.infra.monthly_cost_eur,
        max_rss_mb=cfg.infra.max_rss_mb,
        rss_mb=metrics.value("rss_mb", "7d"),
        cpu_pct=metrics.value("cpu_pct", "7d"),
        net_of_infra=metrics.value("net_of_infra", "since_inception"),
    )

    return OperationsResponse(
        strategy=str(sleeve.strategy),
        as_of_ts=now_ms,
        mode=str(cfg.mode),
        state=engine_state(repos, cfg),
        heartbeat=heartbeat,
        reconciliation=reconciliation,
        backup=backup,
        infra=infra,
        alerts=[
            AlertRow(
                id=int(a["id"]),
                ts=int(a["ts"]),
                severity=a["severity"],
                code=a["code"],
                message=a["message"],
                acked=a["acked_ts"] is not None,
                delivered=bool(a["delivered"]),
                context=json_loads(a["context_json"], {}) or {},
            )
            for a in repos.alerts.recent(alert_limit)
        ],
        unacked_critical=len(repos.alerts.unacked_critical()),
        control_log=[
            ControlRow(
                id=int(c["id"]),
                ts=int(c["ts"]),
                action=c["action"],
                operator=c["operator"],
                reason=c["reason"],
                payload=json_loads(c["payload_json"], {}) or {},
            )
            for c in repos.state.controls(control_limit)
        ],
        reports=[
            ReportRow(
                kind=r["kind"], period_key=r["period_key"], ts=int(r["ts"]), delivered=bool(r["delivered"])
            )
            for r in repos.reports.recent(10)
        ],
    )


__all__ = ["router"]
