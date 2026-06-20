from __future__ import annotations

from kdive.domain.build_phase import BuildPhase


def test_build_phase_members_are_the_orchestrator_phases() -> None:
    assert {p.value for p in BuildPhase} == {
        "provision",
        "source_sync",
        "configure",
        "compile",
        "modules",
        "artifact",
    }
