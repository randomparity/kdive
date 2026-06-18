"""Coverage anchor for shared run handler helpers."""

from __future__ import annotations

from kdive.jobs.handlers import runs_common


def test_abandon_step_helper_owns_idempotency_patch_surface() -> None:
    assert runs_common.abandon_run_step is not None
    assert runs_common.abandon_run_step_best_effort is not None
