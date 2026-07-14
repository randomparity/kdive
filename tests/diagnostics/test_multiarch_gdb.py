"""Local-libvirt multiarch-gdb diagnostic contribution + probe (ADR-0347, #1149).

The probe gates on kdive's *static* cross-arch capability (``SUPPORTED_ARCHES − host``), so it
needs no DB/libvirt handle; ``host_arch``, ``supported``, ``which``, and the batch runner are all
injected here. Acceptance is decided on gdb's *stdout* (batch exit status is unreliable for a
bad ``set architecture``), so the fake runner is keyed on stdout.
"""

from __future__ import annotations

import asyncio

from kdive.diagnostics.multiarch_gdb import (
    default_multiarch_gdb_probe,
    diagnostic_contribution,
)
from kdive.diagnostics.provider_checks import MultiarchGdbCheck, MultiarchGdbOutcome
from kdive.providers.assembly.diagnostics import diagnostic_provider_contributions


def _which(present: dict[str, str]):
    def _find(name: str) -> str | None:
        return present.get(name)

    return _find


def _runner(stdout_by_binary: dict[str, str], *, calls: list[list[str]] | None = None):
    async def _run(argv: list[str]) -> str:
        if calls is not None:
            calls.append(argv)
        return stdout_by_binary.get(argv[0], "")

    return _run


def _raising_runner():
    async def _run(argv: list[str]) -> str:
        raise OSError("gdb not spawnable")

    return _run


def _outcome(probe) -> MultiarchGdbOutcome:
    return asyncio.run(probe())


def test_foreign_arch_targetable_is_supported() -> None:
    calls: list[list[str]] = []
    probe = default_multiarch_gdb_probe(
        host_arch="x86_64",
        supported=frozenset({"x86_64", "ppc64le"}),
        which=_which({"gdb-multiarch": "/usr/bin/gdb-multiarch"}),
        run=_runner(
            {"/usr/bin/gdb-multiarch": 'target architecture is "powerpc:common64".'}, calls=calls
        ),
    )
    assert _outcome(probe) is MultiarchGdbOutcome.SUPPORTED
    # The candidate binary and the ppc gdb-name reached the runner.
    assert calls and calls[0][0] == "/usr/bin/gdb-multiarch"
    assert "powerpc:common64" in " ".join(calls[0])


def test_no_candidate_binary_is_missing() -> None:
    calls: list[list[str]] = []
    probe = default_multiarch_gdb_probe(
        host_arch="x86_64",
        supported=frozenset({"x86_64", "ppc64le"}),
        which=_which({}),
        run=_runner({}, calls=calls),
    )
    assert _outcome(probe) is MultiarchGdbOutcome.MISSING
    assert calls == []  # never spawned a gdb


def test_candidate_cannot_target_arch_is_missing() -> None:
    probe = default_multiarch_gdb_probe(
        host_arch="x86_64",
        supported=frozenset({"x86_64", "ppc64le"}),
        which=_which({"gdb": "/usr/bin/gdb"}),
        # Plain (native-only) gdb echoes back an x86 arch, not powerpc.
        run=_runner({"/usr/bin/gdb": 'target architecture is "i386:x86-64".'}),
    )
    assert _outcome(probe) is MultiarchGdbOutcome.MISSING


def test_spawn_failure_is_undeterminable() -> None:
    probe = default_multiarch_gdb_probe(
        host_arch="x86_64",
        supported=frozenset({"x86_64", "ppc64le"}),
        which=_which({"gdb-multiarch": "/usr/bin/gdb-multiarch"}),
        run=_raising_runner(),
    )
    assert _outcome(probe) is MultiarchGdbOutcome.UNDETERMINABLE


def test_native_only_host_is_supported_without_spawning() -> None:
    calls: list[list[str]] = []
    probe = default_multiarch_gdb_probe(
        host_arch="x86_64",
        supported=frozenset({"x86_64"}),  # only the host arch is supported
        which=_which({"gdb": "/usr/bin/gdb"}),
        run=_runner({}, calls=calls),
    )
    assert _outcome(probe) is MultiarchGdbOutcome.SUPPORTED
    assert calls == []


def test_contribution_shape() -> None:
    contribution = diagnostic_contribution()
    assert contribution.provider == "local-libvirt"
    assert contribution.enabled() is True
    worker_checks = list(contribution.worker_checks())
    assert len(worker_checks) == 1
    assert isinstance(worker_checks[0], MultiarchGdbCheck)
    descriptors = list(contribution.unavailable_worker_checks())
    assert [d.id for d in descriptors] == ["multiarch_gdb"]


def test_registered_in_assembly() -> None:
    providers = {c.provider for c in diagnostic_provider_contributions()}
    assert "local-libvirt" in providers
    assert "remote-libvirt" in providers
