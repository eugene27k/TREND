"""Shared fixtures: an offline Context wired to a FakeGateway and an in-memory DB."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aegis.core.clock import FakeClock
from aegis.core.config import AppConfig, load_config
from aegis.core.context import Context
from aegis.core.types import Strategy
from aegis.gateway.fake import FakeGateway
from aegis.ops.alerts import AlertBus
from aegis.storage.db import open_db
from aegis.storage.repositories import Repositories

START = "2026-09-08T00:05:00Z"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def cfg() -> AppConfig:
    return load_config("config/trend.yaml", use_env=False)


@pytest.fixture
def repos() -> Iterator[Repositories]:
    db = open_db(":memory:")
    yield Repositories(db, Strategy.TREND)
    db.close()


@pytest.fixture
def gateway(clock: FakeClock) -> FakeGateway:
    return FakeGateway(clock)


@pytest.fixture
def ctx(cfg: AppConfig, clock: FakeClock, gateway: FakeGateway, repos: Repositories) -> Context:
    return Context(
        cfg=cfg,
        clock=clock,
        gateway=gateway,
        repos=repos,
        alerts=AlertBus(repos.alerts, clock, Strategy.TREND),
    )
