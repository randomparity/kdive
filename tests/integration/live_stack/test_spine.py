"""Non-gated unit tests for the shared spine phase-naming contract (ADR-0042 §4, ADR-0045 §2)."""

from __future__ import annotations

import asyncio

import pytest

from tests.integration.live_stack.spine import SpinePhaseError, phase


def test_phase_names_the_failing_phase() -> None:
    """A raised exception inside a phase becomes a SpinePhaseError naming that phase."""

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            async with phase("provision"):
                raise ValueError("libvirt exploded")
        assert excinfo.value.phase == "provision"
        assert isinstance(excinfo.value.__cause__, ValueError)

    asyncio.run(_run())


def test_phase_passes_through_spine_phase_error() -> None:
    """An inner SpinePhaseError is preserved (not re-wrapped under the outer phase name)."""

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            async with phase("outer"):
                raise SpinePhaseError("boot", "job failed", error_category="infrastructure_failure")
        assert excinfo.value.phase == "boot"

    asyncio.run(_run())
