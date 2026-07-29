"""The auxiliary health/metrics HTTP app (ADR-0090 §5).

Drives the Starlette aux app in-process via ``httpx.ASGITransport`` (no socket): asserts
``/livez`` reflects the heartbeat, ``/readyz`` flips with the probe and returns 503 when
not-ready, and ``/metrics`` renders the process's metrics in Prometheus text exposition.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kdive.health import aux_listener
from kdive.health.aux_listener import build_aux_app
from kdive.health.deployed_version import DeployedVersion
from kdive.health.heartbeat import Heartbeat
from kdive.health.metrics_text import CONTENT_TYPE
from kdive.health.probe import BackendCheck, HealthProbe

# A build no real resolution could produce, so an assertion against it cannot pass by both
# sides degrading to the same None/"0.0.0" on a git-less, _buildinfo-less host.
_SENTINEL = DeployedVersion(
    version="9.9.9", commit="cafebabe", is_release=True, started_at="2026-01-02T03:04:05Z"
)
_SENTINEL_PAYLOAD = _SENTINEL.payload()


@pytest.fixture
def sentinel_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aux_listener, "deployed_version", lambda: _SENTINEL)


def _client(app: object) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)  # ty: ignore[invalid-argument-type]
    return httpx.AsyncClient(transport=transport, base_url="http://aux")


def test_livez_reflects_heartbeat() -> None:
    async def _run() -> None:
        beats = {"now": 100.0}
        hb = Heartbeat(stale_after=10.0, now=lambda: beats["now"])
        app = build_aux_app(heartbeat=hb, probe=HealthProbe(checks=[]), metric_reader=None)
        async with _client(app) as client:
            live = await client.get("/livez")
            assert live.status_code == 200
            assert live.text == "ok"  # body is the affirmative liveness token, not empty
            beats["now"] = 200.0  # last tick now stale
            stale = await client.get("/livez")
            assert stale.status_code == 503
            assert stale.text == "stale"

    asyncio.run(_run())


def test_readyz_ok_when_all_checks_pass() -> None:
    async def _run() -> None:
        async def ok() -> None:
            return None

        probe = HealthProbe(checks=[BackendCheck(name="pg", probe=ok)])
        app = build_aux_app(heartbeat=_fresh_hb(), probe=probe, metric_reader=None)
        async with _client(app) as client:
            resp = await client.get("/readyz")
            assert resp.status_code == 200
            assert resp.json()["checks"] == {"pg": True}

    asyncio.run(_run())


def test_readyz_503_when_a_check_fails() -> None:
    async def _run() -> None:
        async def down() -> None:
            raise RuntimeError("pg down")

        probe = HealthProbe(checks=[BackendCheck(name="pg", probe=down)])
        app = build_aux_app(heartbeat=_fresh_hb(), probe=probe, metric_reader=None)
        async with _client(app) as client:
            resp = await client.get("/readyz")
            assert resp.status_code == 503
            assert resp.json()["ready"] is False

    asyncio.run(_run())


def test_readyz_carries_the_deployed_build(sentinel_build: None) -> None:
    async def _run() -> None:
        async def ok() -> None:
            return None

        probe = HealthProbe(checks=[BackendCheck(name="pg", probe=ok)])
        app = build_aux_app(heartbeat=_fresh_hb(), probe=probe, metric_reader=None)
        async with _client(app) as client:
            body = (await client.get("/readyz")).json()
            # `version` is a sibling of the pre-existing keys, which keep their shape.
            assert body["ready"] is True
            assert body["checks"] == {"pg": True}
            assert set(body["version"]) == {"version", "commit", "is_release", "started_at"}
            # Pinned to a sentinel, not to version_info() itself: version_info() is
            # lru_cached, so comparing the body against another call to it compares an object
            # with itself and passes even if the listener reported None on a git-less host.
            assert body["version"] == _SENTINEL_PAYLOAD

    asyncio.run(_run())


def test_readyz_carries_the_deployed_build_even_when_not_ready(sentinel_build: None) -> None:
    # The load-bearing edge: a stack whose backends are down is exactly when someone needs to
    # know whether they are chasing a defect or a stale build. A 503 must still carry `version`,
    # or the preflight goes blind precisely when it matters most (ADR-0482 §1).
    async def _run() -> None:
        async def down() -> None:
            raise RuntimeError("pg down")

        probe = HealthProbe(checks=[BackendCheck(name="pg", probe=down)])
        app = build_aux_app(heartbeat=_fresh_hb(), probe=probe, metric_reader=None)
        async with _client(app) as client:
            resp = await client.get("/readyz")
            assert resp.status_code == 503
            assert resp.json()["ready"] is False
            assert resp.json()["version"] == _SENTINEL_PAYLOAD

    asyncio.run(_run())


def test_readyz_reports_the_build_instant_resolved_once_at_app_build_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `started_at` is the *process* start, resolved once when the app is built. If it were
    # recomputed per request it would always equal "now" and could never read as stale, which
    # is the whole signal the local skew variant depends on (ADR-0482 §1/§3). Driving a moving
    # clock proves the resolution happens once, without sleeping a real second.
    ticks = iter(["FIRST", "SECOND", "THIRD"])
    monkeypatch.setattr(
        aux_listener,
        "deployed_version",
        lambda: DeployedVersion(
            version="9.9.9", commit="cafebabe", is_release=True, started_at=next(ticks)
        ),
    )

    async def _run() -> None:
        app = build_aux_app(heartbeat=_fresh_hb(), probe=HealthProbe(checks=[]), metric_reader=None)
        async with _client(app) as client:
            first = (await client.get("/readyz")).json()["version"]["started_at"]
            second = (await client.get("/readyz")).json()["version"]["started_at"]
            assert first == second == "FIRST"

    asyncio.run(_run())


def test_metrics_renders_recorded_counter() -> None:
    async def _run() -> None:
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        counter = provider.get_meter("test").create_counter("kdive_test_total")
        counter.add(3, {"outcome": "ok"})
        app = build_aux_app(
            heartbeat=_fresh_hb(), probe=HealthProbe(checks=[]), metric_reader=reader
        )
        async with _client(app) as client:
            resp = await client.get("/metrics")
            assert resp.status_code == 200
            # The Prometheus exposition content-type (with version) is load-bearing for a
            # scraper, not a bare text/plain default.
            assert resp.headers["content-type"] == CONTENT_TYPE
            assert "kdive_test_total" in resp.text
            assert 'outcome="ok"' in resp.text

    asyncio.run(_run())


def test_metrics_404_when_no_reader() -> None:
    async def _run() -> None:
        app = build_aux_app(heartbeat=_fresh_hb(), probe=HealthProbe(checks=[]), metric_reader=None)
        async with _client(app) as client:
            resp = await client.get("/metrics")
            assert resp.status_code == 404
            assert resp.text == "no metric reader configured"

    asyncio.run(_run())


def _fresh_hb() -> Heartbeat:
    return Heartbeat(stale_after=1e9)
