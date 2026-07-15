"""Local-libvirt pseries-fadump diagnostic contribution + probe (ADR-0349, #1151).

The probe finds ``qemu-system-ppc64`` on PATH and checks its version against the fadump floor
(reusing ``detect_pseries_fadump``), so it needs no DB/libvirt handle; ``which`` and the version
runner are injected here.
"""

from __future__ import annotations

import asyncio

from kdive.diagnostics.checks import CheckStatus
from kdive.diagnostics.multiarch_gdb import diagnostic_contribution as local_diagnostics
from kdive.diagnostics.provider_checks import PseriesFadumpCheck, PseriesFadumpOutcome
from kdive.diagnostics.pseries_fadump import default_pseries_fadump_probe
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

    probe = default_pseries_fadump_probe(which=_which({}), run_version=_run)
    assert _outcome(probe) is PseriesFadumpOutcome.NOT_APPLICABLE
    assert calls == []


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
