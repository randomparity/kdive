"""Coverage anchor for the split boot run handler module."""

from __future__ import annotations

from kdive.jobs.handlers import runs, runs_boot


def test_boot_handler_facade_and_leaf_console_patch_surface() -> None:
    assert runs.boot_handler is runs_boot.boot_handler
    assert runs_boot.console_log_path is not None
    assert runs_boot.read_console_log is not None
