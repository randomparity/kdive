"""Coverage anchor for the split install run handler module."""

from __future__ import annotations

from kdive.jobs.handlers import runs, runs_install


def test_install_handler_is_exported_through_runs_facade() -> None:
    assert runs.install_handler is runs_install.install_handler
