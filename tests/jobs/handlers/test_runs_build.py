"""Coverage anchor for the split build run handler module."""

from __future__ import annotations

from kdive.jobs.handlers import runs, runs_build


def test_build_handler_is_exported_through_runs_facade() -> None:
    assert runs.build_handler is runs_build.build_handler
    assert runs._run_build is runs_build._run_build
