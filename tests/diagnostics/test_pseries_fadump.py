"""Local-libvirt pseries-fadump diagnostic contribution + probe (ADR-0349, #1151).

The probe finds ``qemu-system-ppc64`` on PATH and checks its version against the fadump floor
(reusing ``detect_pseries_fadump``), so it needs no DB/libvirt handle; ``which`` and the version
runner are injected here.
"""

from __future__ import annotations

import asyncio
import threading

from kdive.diagnostics.checks import CheckStatus, run_check
from kdive.diagnostics.contributions.multiarch_gdb import (
    diagnostic_contribution as local_diagnostics,
)
from kdive.diagnostics.contributions.pseries_fadump import default_pseries_fadump_probe
from kdive.diagnostics.provider_checks import PseriesFadumpCheck, PseriesFadumpOutcome
from kdive.providers.assembly.diagnostics import diagnostic_provider_contributions


def _which(present: dict[str, str]):
    def _find(name: str) -> str | None:
        return present.get(name)

    return _find


def _version(text: str):
    def _run(_argv: list[str]) -> str:
        return text

    return _run


def _outcome(probe) -> PseriesFadumpOutcome:
    return asyncio.run(probe())


def test_supported_when_qemu_meets_floor() -> None:
    probe = default_pseries_fadump_probe(
        which=_which({"qemu-system-ppc64": "/usr/bin/qemu-system-ppc64"}),
        run_version=_version("QEMU emulator version 10.2.2 (qemu-10.2.2-1.fc44)"),
    )
    assert _outcome(probe) is PseriesFadumpOutcome.SUPPORTED


def test_unsupported_when_qemu_below_floor() -> None:
    probe = default_pseries_fadump_probe(
        which=_which({"qemu-system-ppc64": "/usr/bin/qemu-system-ppc64"}),
        run_version=_version("QEMU emulator version 9.2.1"),
    )
    assert _outcome(probe) is PseriesFadumpOutcome.UNSUPPORTED


def test_not_applicable_when_no_ppc64_emulator() -> None:
    calls: list[list[str]] = []

    def _run(argv: list[str]) -> str:  # pragma: no cover - must not be called
        calls.append(argv)
        raise AssertionError("no version probe when qemu-system-ppc64 is absent")

    # host_arch is pinned so this stays hermetic: on a real EL ppc64le host the off-PATH
    # emulator exists and the probe would (correctly) resolve it instead.
    probe = default_pseries_fadump_probe(which=_which({}), run_version=_run, host_arch="x86_64")
    assert _outcome(probe) is PseriesFadumpOutcome.NOT_APPLICABLE
    assert calls == []


def test_ppc64le_host_resolves_the_off_path_emulator() -> None:
    """ADR-0636: EL ships no qemu-system-ppc64 binary on PATH — the emulator is at libexec.

    Without this fallback the fadump check reports not_applicable on exactly the EL ppc64le host
    fadump exists for, while LocalLibvirtDiscovery reads libvirt's capabilities XML and sees the
    arch — the divergence the probe's own docstring promises cannot happen.
    """
    seen: list[list[str]] = []

    def _run(argv: list[str]) -> str:
        seen.append(argv)
        return "QEMU emulator version 10.2.2"

    probe = default_pseries_fadump_probe(
        which=_which({}),
        run_version=_run,
        host_arch="ppc64le",
        libexec_emulator="/usr/libexec/qemu-kvm",
        is_executable=lambda path: path == "/usr/libexec/qemu-kvm",
    )
    assert _outcome(probe) is PseriesFadumpOutcome.SUPPORTED
    # The resolved libexec path is what gets exec'd, not the arch-named binary EL does not ship.
    assert seen and "/usr/libexec/qemu-kvm" in seen[0]


def test_libexec_fallback_does_not_apply_on_a_foreign_arch_host() -> None:
    """/usr/libexec/qemu-kvm is this host's OWN emulator — on x86_64 it is never a ppc64 one."""
    probe = default_pseries_fadump_probe(
        which=_which({}),
        run_version=_version("QEMU emulator version 10.2.2"),
        host_arch="x86_64",
        libexec_emulator="/usr/libexec/qemu-kvm",
        is_executable=lambda _path: True,
    )
    assert _outcome(probe) is PseriesFadumpOutcome.NOT_APPLICABLE


def test_path_emulator_wins_over_the_libexec_fallback() -> None:
    """Fedora ppc64le ships qemu-system-ppc64 on PATH; the fallback must not shadow it."""
    seen: list[list[str]] = []

    def _run(argv: list[str]) -> str:
        seen.append(argv)
        return "QEMU emulator version 10.2.2"

    probe = default_pseries_fadump_probe(
        which=_which({"qemu-system-ppc64": "/usr/bin/qemu-system-ppc64"}),
        run_version=_run,
        host_arch="ppc64le",
        is_executable=lambda _path: True,
    )
    assert _outcome(probe) is PseriesFadumpOutcome.SUPPORTED
    assert seen and "/usr/bin/qemu-system-ppc64" in seen[0]


def test_blocking_version_probe_is_bounded_by_run_check_timeout() -> None:
    started = threading.Event()
    release = threading.Event()
    watchdog_released = threading.Event()

    def _blocking_version(_argv: list[str]) -> str:
        started.set()
        if not release.wait(timeout=1.0):
            watchdog_released.set()
        return "QEMU emulator version 10.2.0"

    probe = default_pseries_fadump_probe(
        which=_which({"qemu-system-ppc64": "/usr/bin/qemu-system-ppc64"}),
        run_version=_blocking_version,
    )
    check = PseriesFadumpCheck(provider="local-libvirt", probe=probe)

    async def _exercise_timeout() -> CheckStatus:
        task = asyncio.create_task(run_check(check, timeout=0.01))
        try:
            while not started.is_set():
                if task.done():
                    await task
                    raise AssertionError("fadump version runner did not start")
                await asyncio.sleep(0)
            result = await task
            assert not watchdog_released.is_set()
            assert not release.is_set()
            assert "did not respond within" in result.detail
            return result.status
        finally:
            release.set()

    assert asyncio.run(_exercise_timeout()) is CheckStatus.ERROR


def test_check_maps_outcomes_to_statuses() -> None:
    async def _run_check(outcome: PseriesFadumpOutcome) -> CheckStatus:
        async def _probe() -> PseriesFadumpOutcome:
            return outcome

        result = await PseriesFadumpCheck(provider="local-libvirt", probe=_probe).run()
        return result.status

    assert asyncio.run(_run_check(PseriesFadumpOutcome.SUPPORTED)) is CheckStatus.PASS
    assert asyncio.run(_run_check(PseriesFadumpOutcome.NOT_APPLICABLE)) is CheckStatus.PASS
    # A qemu present but below the floor is an actionable fail with a fix.
    assert asyncio.run(_run_check(PseriesFadumpOutcome.UNSUPPORTED)) is CheckStatus.FAIL


def test_fadump_check_is_in_the_single_local_contribution() -> None:
    # One local-libvirt contribution carries every local worker check (one dispatcher per
    # contribution), so the fadump check rides alongside multiarch-gdb — not a second contribution.
    contribution = local_diagnostics()
    assert contribution.provider == "local-libvirt"
    assert any(isinstance(c, PseriesFadumpCheck) for c in contribution.worker_checks())
    assert "pseries_fadump" in {d.id for d in contribution.unavailable_worker_checks()}


def test_registered_in_assembly_without_duplicate_local_contribution() -> None:
    contributions = diagnostic_provider_contributions()
    # Exactly one local-libvirt contribution (no duplicate provider dispatcher).
    assert [c.provider for c in contributions].count("local-libvirt") == 1
    ids = {d.id for c in contributions for d in c.unavailable_worker_checks()}
    assert "pseries_fadump" in ids
