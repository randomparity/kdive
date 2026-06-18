"""Coverage anchor for the split boot run handler module."""

from __future__ import annotations

from kdive.jobs.handlers import runs, runs_boot


def test_boot_handler_and_patchable_console_seams_are_facade_exported() -> None:
    assert runs.boot_handler is runs_boot.boot_handler
    assert runs.console_log_path is not None
    assert runs.read_console_log is not None
