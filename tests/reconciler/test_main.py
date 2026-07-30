"""CLI wiring for the `python -m kdive reconciler` subcommand (issue #12)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import cast

import pytest

from kdive.__main__ import build_parser
from kdive.observability.facade import Telemetry
from kdive.processes.runtime import (
    POOL_CLOSE_TIMEOUT_SECONDS,
    POOL_OPEN_TIMEOUT_SECONDS,
)
from kdive.reconciler.loop import ReconcileConfig
from kdive.security.secrets.secret_registry import SecretRegistry


def _warm_open() -> list[str]:
    """The warm-up the runtime must perform: open, then take one connection (ADR-0449)."""
    return ["open", f"acquire(timeout={POOL_OPEN_TIMEOUT_SECONDS})"]


def _close() -> str:
    """Teardown, at the bounded timeout that keeps a stuck worker off the fail-fast path."""
    return f"close(timeout={POOL_CLOSE_TIMEOUT_SECONDS})"


def test_reconciler_subcommand_parses() -> None:
    args = build_parser().parse_args(["reconciler"])
    assert args.command == "reconciler"
    # No flag → None; the INFO default is supplied by the config registry, not argparse.
    assert args.log_level is None


def test_reconciler_subcommand_with_log_level() -> None:
    args = build_parser().parse_args(["--log-level", "DEBUG", "reconciler"])
    assert args.command == "reconciler"
    assert args.log_level == "DEBUG"


def _fake_telemetry() -> Telemetry:
    """A Telemetry the reconciler runner reaches for (providers + scrape reader)."""
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from tests.support.otel import tracer_provider

    reader = InMemoryMetricReader()
    return Telemetry(
        logger_provider=LoggerProvider(),
        tracer_provider=tracer_provider(),
        meter_provider=MeterProvider(metric_readers=[reader]),
        scrape_reader=reader,
    )


def test_run_reconciler_builds_and_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_run_reconciler` opens a pool, constructs a Reconciler, runs, closes."""
    from kdive import __main__
    from kdive.providers.assembly import composition
    from kdive.reconciler import loop

    events: list[str] = []
    discovery_release = asyncio.Event()

    class _FakePool:
        # Mirrors the `AsyncConnectionPool` surface the runtime uses. Record the warm-up,
        # not just the open: the runtime must establish one connection at start (ADR-0449),
        # and a fake that discards that lets a revert to the cold default pass.
        async def open(self, wait: bool = False, timeout: float = 30.0) -> None:
            events.append("open" if not wait else f"open(wait=True, timeout={timeout})")

        @contextlib.asynccontextmanager
        async def connection(self, timeout: float | None = None) -> AsyncIterator[object]:
            events.append(f"acquire(timeout={timeout})")
            yield object()

        async def close(self, timeout: float = 5.0) -> None:
            events.append(f"close(timeout={timeout})")

    monkeypatch.setattr("kdive.processes.reconciler.create_pool", lambda **kw: _FakePool())
    monkeypatch.setattr(
        "kdive.processes.reconciler.install_stop", lambda: __import__("asyncio").Event()
    )
    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", lambda: object())

    async def _no_serve(*a: object, **k: object) -> None:
        return None

    monkeypatch.setattr("kdive.health.aux_listener.serve_aux", _no_serve)
    monkeypatch.setattr(
        "kdive.health.processes.server.build_postgres_ping", lambda pool: lambda: None
    )

    class _FakeResolver:
        async def register_all_discovery(self, pool: object) -> None:
            events.append("discover-start")
            await discovery_release.wait()
            events.append("discover-end")

    expected_reaper = object()
    expected_resetter = object()
    expected_dump_volume_reaper = object()
    expected_registry = SecretRegistry()

    class _FakeProviderComposition:
        def __init__(self, *, secret_registry: SecretRegistry | None = None) -> None:
            assert secret_registry is expected_registry

        def build_provider_resolver(self) -> _FakeResolver:
            return _FakeResolver()

        def build_reconciler_reaper(self) -> object:
            return expected_reaper

        def build_reconciler_transport_resetter(self) -> object:
            return expected_resetter

        def build_reconciler_dump_volume_reaper(self) -> object:
            return expected_dump_volume_reaper

        async def build_reconciler_console_hosting(
            self,
            *,
            enable_remote_libvirt: bool | None = None,
            console_telemetry: object | None = None,
        ) -> None:
            del enable_remote_libvirt, console_telemetry
            return None

    monkeypatch.setattr(composition, "ProviderComposition", _FakeProviderComposition)

    constructed: dict[str, object] = {}

    def _fake_init(self: object, pool: object, reaper: object, **kw: object) -> None:
        constructed["reaper"] = reaper
        config = cast(ReconcileConfig, kw["config"])
        constructed["resetter"] = config.resetter
        constructed["dump_volume_reaper"] = config.dump_volume_reaper

    async def _fake_run(self: object, stop: object) -> None:
        events.append("run")
        discovery_release.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(loop.Reconciler, "__init__", _fake_init)
    monkeypatch.setattr(loop.Reconciler, "run", _fake_run)

    asyncio.run(__main__._run_reconciler(expected_registry, _fake_telemetry()))

    assert events[:2] == _warm_open()
    assert events[-1] == _close()
    assert "discover-end" in events
    assert events.index("run") < events.index("discover-end")
    assert constructed["reaper"] is expected_reaper
    assert constructed["resetter"] is expected_resetter
    assert constructed["dump_volume_reaper"] is expected_dump_volume_reaper
