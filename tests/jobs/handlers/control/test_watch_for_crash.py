"""Tests for the watch_for_crash worker job handler (ADR-0367, #984).

The pure core ``watch_console_for_crash`` is driven with injected seams
(``read_console``/``sleep``/``clock``/``probe_exited``/``redact``/``now``) so every branch —
fired, not-fired, exited-no-signature, truncation, redaction, bounds — is deterministic without a
VM. The handler gate tests drive ``watch_for_crash_handler`` against a migrated Postgres with a
fake console log file and a monkeypatched domain-exit probe.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.capacity.state import AllocationState, JobState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.control import watch_for_crash
from kdive.jobs.handlers.control.watch_for_crash import (
    CONTEXT_LINES,
    MATCHED_MAX_BYTES,
    WatchVerdict,
    watch_console_for_crash,
    watch_for_crash_handler,
)
from kdive.providers.core.resource_registration import register_discovered_resource
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.systems_support import provider_resolver
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_NOW = "2026-07-16T00:00:00+00:00"


def _reader(states: list[bytes]) -> Callable[[], Awaitable[bytes]]:
    """A read_console seam returning each state in turn, then repeating the last."""
    idx = {"i": 0}

    async def read() -> bytes:
        i = min(idx["i"], len(states) - 1)
        idx["i"] += 1
        return states[i]

    return read


def _clock(times: list[float]) -> Callable[[], float]:
    """A monotonic-clock seam returning each value in turn, then repeating the last."""
    idx = {"i": 0}

    def now() -> float:
        i = min(idx["i"], len(times) - 1)
        idx["i"] += 1
        return times[i]

    return now


async def _noop_sleep(_seconds: float) -> None:
    return None


async def _run_core(
    states: list[bytes],
    times: list[float],
    *,
    mark: int = 0,
    deadline_s: float = 10.0,
    redact: Callable[[str], str] = lambda s: s,
    context_lines: int = CONTEXT_LINES,
    max_bytes: int = MATCHED_MAX_BYTES,
) -> WatchVerdict:
    return await watch_console_for_crash(
        _reader(states),
        _noop_sleep,
        _clock(times),
        redact,
        lambda: _NOW,
        mark=mark,
        deadline_s=deadline_s,
        poll_interval=0.5,
        context_lines=context_lines,
        max_bytes=max_bytes,
    )


# --- pure core ------------------------------------------------------------------------


def test_core_fires_on_first_signature_past_mark() -> None:
    states = [b"[1] booting\n[2] Kernel panic - not syncing: die\n[3] more\n"]
    verdict = asyncio.run(_run_core(states, [0.0, 0.5]))
    assert verdict.outcome == "fired"
    assert verdict.fired is True
    assert verdict.signature == "Kernel panic"
    assert verdict.matched is not None and "Kernel panic" in verdict.matched
    assert verdict.elapsed_s == 0.5
    assert verdict.observed_at == _NOW


def test_core_reports_earliest_of_two_signatures() -> None:
    states = [b"[1] boot\n[2] Oops: 0000\n[3] Kernel panic - not syncing\n"]
    verdict = asyncio.run(_run_core(states, [0.0, 0.1]))
    assert verdict.signature == "Oops:"


def test_core_ignores_pre_mark_panic() -> None:
    body = b"[1] Kernel panic - not syncing: old\n[2] booting after\n"
    verdict = asyncio.run(_run_core([body], [0.0, 11.0], mark=len(body), deadline_s=10.0))
    assert verdict.outcome == "not_fired"
    assert verdict.fired is False


def test_core_not_fired_at_deadline() -> None:
    verdict = asyncio.run(_run_core([b"[1] still booting\n"], [0.0, 11.0]))
    assert verdict.outcome == "not_fired"
    assert verdict.fired is False
    assert verdict.signature is None and verdict.matched is None


def test_core_truncation_resets_mark_and_still_matches() -> None:
    # First read is long; second read is shorter than mark (power-cycle truncation) and carries a
    # fresh panic — mark resets to 0 and the panic is found.
    long = b"x" * 500 + b"\n"
    short = b"[1] fresh boot\n[2] general protection fault: 0000\n"
    verdict = asyncio.run(_run_core([long, short], [0.0, 0.5, 1.0], mark=400, deadline_s=10.0))
    assert verdict.outcome == "fired"
    assert verdict.signature == "general protection fault"


def test_core_non_halting_signature_fires() -> None:
    states = [b"[1] rcu: INFO: rcu_sched self-detected stall on CPU\n"]
    verdict = asyncio.run(_run_core(states, [0.0, 0.5]))
    assert verdict.outcome == "fired"
    assert verdict.signature == "detected stall"


def test_core_redacts_matched_slice() -> None:
    states = [b"[1] leak SECRET=abc\n[2] Kernel panic - not syncing\n"]
    verdict = asyncio.run(
        _run_core(states, [0.0, 0.5], redact=lambda s: s.replace("SECRET=abc", "[REDACTED]"))
    )
    assert verdict.matched is not None
    assert "SECRET=abc" not in verdict.matched
    assert "[REDACTED]" in verdict.matched


def test_core_bounds_context_lines() -> None:
    lines = [f"[{i}] line {i}" for i in range(20)]
    lines[10] = "[10] Kernel panic - not syncing"
    body = ("\n".join(lines) + "\n").encode()
    verdict = asyncio.run(_run_core([body], [0.0, 0.5], context_lines=1))
    assert verdict.matched is not None
    assert verdict.matched.count("\n") <= 2  # matched line +/- 1 context line


def test_core_caps_matched_bytes() -> None:
    huge = "z" * 10_000
    body = f"[1] {huge}\n[2] Kernel panic - not syncing: {huge}\n".encode()
    verdict = asyncio.run(_run_core([body], [0.0, 0.5], context_lines=3, max_bytes=256))
    assert verdict.matched is not None
    assert len(verdict.matched.encode("utf-8")) <= 256


def test_core_redacts_secret_straddling_the_byte_cap() -> None:
    # A secret that straddles max_bytes must be masked *before* the cut, else its surviving
    # prefix would leak (redact-then-cap, not cap-then-redact).
    line = "Kernel panic " + "A" * 4090 + "SECRETVALUE" + "B" * 100
    verdict = asyncio.run(
        _run_core(
            [line.encode()],
            [0.0, 0.5],
            redact=lambda s: s.replace("SECRETVALUE", "[REDACTED]"),
            context_lines=3,
            max_bytes=4096,
        )
    )
    assert verdict.matched is not None
    assert "SECRET" not in verdict.matched
    assert len(verdict.matched.encode("utf-8")) <= 4096


def test_verdict_to_json_shapes() -> None:
    fired = WatchVerdict("fired", True, "Kernel panic", "slice", 1.5, _NOW)
    doc = json.loads(fired.to_json())
    assert doc == {
        "outcome": "fired",
        "fired": True,
        "elapsed_s": 1.5,
        "observed_at": _NOW,
        "signature": "Kernel panic",
        "matched": "slice",
    }
    not_fired = WatchVerdict("not_fired", False, None, None, 10.0, _NOW)
    assert json.loads(not_fired.to_json()) == {
        "outcome": "not_fired",
        "fired": False,
        "elapsed_s": 10.0,
        "observed_at": _NOW,
    }


# --- handler gates + end-to-end -------------------------------------------------------


async def _seed_system(pool: AsyncConnectionPool, state: SystemState) -> UUID:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system", connect=lambda: FakeLibvirtConn(), concurrent_allocation_cap=2
    )
    async with pool.connection() as conn:
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class="local"
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=res.id,
                state=AllocationState.GRANTED,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=alloc.id,
                state=state,
                provisioning_profile={},
                domain_name="kdive-x",
            ),
        )
    return system.id


def _job(system_id: UUID, deadline_s: float) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=JobKind.WATCH_FOR_CRASH,
        payload={"system_id": str(system_id), "deadline_s": deadline_s},
        state=JobState.RUNNING,
        max_attempts=3,
        authorizing={"principal": "user-1", "agent_session": None, "project": "proj"},
        dedup_key=f"{system_id}:watch_for_crash:x",
    )


def _pool(url: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(url, min_size=1, max_size=2, open=False)


async def _run_handler(
    pool: AsyncConnectionPool, job: Job, *, secret_registry: SecretRegistry | None = None
) -> str | None:
    resolver = provider_resolver()
    async with pool.connection() as conn:
        return await watch_for_crash_handler(
            conn,
            job,
            resolver=resolver,
            secret_registry=secret_registry or SecretRegistry(),
        )


def test_handler_fired_returns_verdict_with_slice(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_for_crash, "POLL_INTERVAL_S", 0.0)
    log = tmp_path / "console.log"
    monkeypatch.setattr(watch_for_crash, "console_log_path", lambda _sid: log)
    # The panic appears only *after* the watch snapshots its start offset: the first read (mark)
    # sees a benign boot; the next read has grown with the panic line.
    reads = {"n": 0}
    booting = b"[1] booting\n"
    panicked = booting + b"[2] Kernel panic - not syncing: die\n"

    def _fake_read(_path: Path) -> bytes:
        reads["n"] += 1
        return booting if reads["n"] == 1 else panicked

    monkeypatch.setattr(watch_for_crash, "read_console_log", _fake_read)

    async def _go() -> str | None:
        async with _pool(migrated_url) as pool:
            await pool.open()
            system_id = await _seed_system(pool, SystemState.READY)
            return await _run_handler(pool, _job(system_id, 5.0))

    result_ref = asyncio.run(_go())
    assert result_ref is not None
    doc = json.loads(result_ref)
    assert doc["outcome"] == "fired"
    assert doc["signature"] == "Kernel panic"


def test_handler_not_ready_raises_configuration_error(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "console.log"
    log.write_bytes(b"boot\n")
    monkeypatch.setattr(watch_for_crash, "console_log_path", lambda _sid: log)

    async def _go() -> None:
        async with _pool(migrated_url) as pool:
            await pool.open()
            system_id = await _seed_system(pool, SystemState.PROVISIONING)
            await _run_handler(pool, _job(system_id, 5.0))

    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(_go())
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["reason"] == "system_not_ready"


def test_handler_not_fired_at_deadline(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch_for_crash, "POLL_INTERVAL_S", 0.0)
    log = tmp_path / "console.log"
    log.write_bytes(b"[1] still booting\n")
    monkeypatch.setattr(watch_for_crash, "console_log_path", lambda _sid: log)

    async def _go() -> str | None:
        async with _pool(migrated_url) as pool:
            await pool.open()
            system_id = await _seed_system(pool, SystemState.READY)
            return await _run_handler(pool, _job(system_id, 0.001))

    result_ref = asyncio.run(_go())
    assert result_ref is not None
    doc = json.loads(result_ref)
    assert doc["outcome"] == "not_fired"
    assert doc["fired"] is False
