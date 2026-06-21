"""Coverage anchor for the split boot run handler module, plus the runs registrar facade."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from kdive.domain.lifecycle import Run
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers import runs, runs_boot
from kdive.jobs.models import HandlerRegistry
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.artifacts.artifact_search import (
    ArtifactSearchInputError,
    search_text,
)
from kdive.security.secrets.secret_registry import SecretRegistry


def test_boot_handler_facade_and_leaf_console_patch_surface() -> None:
    assert runs.boot_handler is runs_boot.boot_handler
    assert runs_boot.console_log_path is not None
    assert runs_boot.read_console_log is not None


class _FakeRun:
    """Stand-in carrying only the field _expected_crash_matches reads."""

    def __init__(self, expected_boot_failure: object) -> None:
        self.expected_boot_failure = expected_boot_failure


_CONSOLE = b"line1\nkernel BUG at mm/slub.c:1\nline3\n"


def test_expected_crash_matches_when_pattern_is_found_in_console() -> None:
    run = cast(Run, _FakeRun({"kind": "console_crash", "pattern": "BUG at"}))
    assert runs_boot._expected_crash_matches(run, _CONSOLE) is True


def test_expected_crash_no_match_when_pattern_absent() -> None:
    run = cast(Run, _FakeRun({"kind": "console_crash", "pattern": "no-such-marker"}))
    assert runs_boot._expected_crash_matches(run, _CONSOLE) is False


def test_expected_crash_false_when_no_expected_failure_declared() -> None:
    run = cast(Run, _FakeRun(None))
    assert runs_boot._expected_crash_matches(run, _CONSOLE) is False


def test_expected_crash_false_for_non_console_crash_kind() -> None:
    run = cast(Run, _FakeRun({"kind": "exit_code", "pattern": "BUG at"}))
    assert runs_boot._expected_crash_matches(run, _CONSOLE) is False


def test_expected_crash_false_when_pattern_is_not_a_string() -> None:
    run = cast(Run, _FakeRun({"kind": "console_crash", "pattern": 123}))
    assert runs_boot._expected_crash_matches(run, _CONSOLE) is False


def test_expected_crash_false_on_invalid_search_pattern() -> None:
    # A trailing '|' yields an empty term, so parse_literal_terms raises
    # ArtifactSearchInputError inside search_text. The handler must catch it and
    # fail closed (no crash match) rather than let it propagate out.
    run = cast(Run, _FakeRun({"kind": "console_crash", "pattern": "BUG at|"}))
    # Guard: the pattern truly drives search_text into the raising path.
    with pytest.raises(ArtifactSearchInputError):
        search_text(_CONSOLE, pattern="BUG at|", max_matches=1)
    assert runs_boot._expected_crash_matches(run, _CONSOLE) is False


def test_expected_crash_fails_closed_when_search_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even if search_text raises for some other reason, the except branch must
    # swallow it and return False — a mutant deleting the try/except or flipping
    # its `return False` to `return True` is killed here.
    def _boom(*_args: object, **_kwargs: object) -> object:
        raise ArtifactSearchInputError("forced")

    monkeypatch.setattr(runs_boot, "search_text", _boom)
    run = cast(Run, _FakeRun({"kind": "console_crash", "pattern": "BUG at"}))
    assert runs_boot._expected_crash_matches(run, _CONSOLE) is False


def _ports() -> runs.RunHandlerPorts:
    return runs.RunHandlerPorts(
        resolver=cast(ProviderResolver, object()),
        secret_registry=cast(SecretRegistry, object()),
        transport_factories=cast(object, "transport-factories"),  # type: ignore[arg-type]
        artifact_store=cast(object, "artifact-store"),  # type: ignore[arg-type]
    )


def test_register_handlers_binds_each_run_kind_to_its_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The facade must bind exactly build/install/boot, each to its own leaf handler — a
    # mis-wired lambda (wrong kind, wrong handler) would let the worker dispatch a job to the
    # wrong run phase.
    calls: dict[str, tuple[object, object, dict[str, object]]] = {}

    async def _fake(label: str, conn: object, job: object, **kwargs: object) -> str:
        calls[label] = (conn, job, kwargs)
        return label

    monkeypatch.setattr(
        runs, "build_handler", lambda conn, job, **kw: _fake("build", conn, job, **kw)
    )
    monkeypatch.setattr(
        runs, "install_handler", lambda conn, job, **kw: _fake("install", conn, job, **kw)
    )
    monkeypatch.setattr(
        runs, "boot_handler", lambda conn, job, **kw: _fake("boot", conn, job, **kw)
    )

    registry = HandlerRegistry()
    ports = _ports()
    runs.register_handlers(registry, ports=ports)

    claimed = {JobKind.BUILD, JobKind.INSTALL, JobKind.BOOT}
    for kind in claimed:
        assert registry.get(kind) is not None
    # Every other JobKind must remain unclaimed by this facade — a mutant that
    # additionally registered some unrelated kind is caught here.
    for kind in JobKind:
        if kind not in claimed:
            assert registry.get(kind) is None, f"facade should not claim {kind}"

    conn, job = object(), object()
    assert asyncio.run(registry.get(JobKind.BUILD)(conn, job)) == "build"  # type: ignore[misc]
    assert asyncio.run(registry.get(JobKind.INSTALL)(conn, job)) == "install"  # type: ignore[misc]
    assert asyncio.run(registry.get(JobKind.BOOT)(conn, job)) == "boot"  # type: ignore[misc]

    # Each lambda threads the shared conn/job plus the ports the leaf handler needs.
    assert calls["build"][0] is conn and calls["build"][1] is job
    assert calls["build"][2] == {
        "resolver": ports.resolver,
        "secret_registry": ports.secret_registry,
        "transport_factories": ports.transport_factories,
        "build_phase_recorder": ports.build_phase_recorder,
    }
    assert calls["install"][0] is conn and calls["install"][1] is job
    assert calls["install"][2] == {"resolver": ports.resolver}
    assert calls["boot"][0] is conn and calls["boot"][1] is job
    assert calls["boot"][2] == {
        "resolver": ports.resolver,
        "secret_registry": ports.secret_registry,
        "artifact_store": ports.artifact_store,
    }
