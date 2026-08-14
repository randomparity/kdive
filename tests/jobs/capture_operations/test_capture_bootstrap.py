"""Pre-gate bootstrap ordering tests (ADR-0558)."""

from __future__ import annotations

import pytest

import kdive.jobs.capture_operations.child as capture_child
from kdive import capture_bootstrap
from kdive.__main__ import build_parser
from kdive.jobs.capture_operations import sandbox


def test_filter_failure_occurs_before_handshake_or_gate_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[bytes] = []
    reads: list[int] = []
    monkeypatch.setattr(
        sandbox,
        "install_capture_filter",
        lambda: (_ for _ in ()).throw(RuntimeError("filter fault")),
    )
    monkeypatch.setattr(capture_bootstrap.os, "write", lambda _fd, data: writes.append(data))
    monkeypatch.setattr(capture_bootstrap.os, "read", lambda fd, _size: reads.append(fd) or b"R")

    with pytest.raises(RuntimeError, match="filter fault"):
        capture_bootstrap.main(["--launch-token", "a" * 64, "--gate-fd", "9"])
    assert writes == []
    assert reads == []


def test_internal_capture_operation_verb_has_only_fixed_boundary_arguments() -> None:
    args = build_parser().parse_args(
        ["capture-operation", "--launch-token", "a" * 64, "--gate-fd", "9"]
    )
    assert vars(args) == {
        "command": "capture-operation",
        "gate_fd": 9,
        "launch_token": "a" * 64,
        "log_level": None,
        "version": None,
    }
    assert capture_child.run_capture_child.__name__ == "run_capture_child"
