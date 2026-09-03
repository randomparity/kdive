"""Unit tests for the provisioned-family console-inode handoff (tests.live_vm.console_actor).

Runs in ordinary CI, like the other tests/live_vm guards: the live tier is where the handoff is
*used*, but every branch of the decision is host-free. ``_claim_console_inode`` takes the euid as a
parameter precisely so the foreign-owner branch is provable without root and without a second
account — passing an euid that is not the file's owner is exactly the runner's situation, where the
worker account creates the log and the operator account starts the domain.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest

from tests.live_vm import console_actor
from tests.live_vm.console_actor import _claim_console_inode, claim_console_inode

_SYS = UUID("61510773-cdf7-489f-9a8f-254e6ec98227")
_FOREIGN_UID = os.geteuid() + 1


def _console_log(tmp_path: Path) -> Path:
    log = tmp_path / f"{_SYS}.log"
    log.write_bytes(b"[    0.00] a prior actor's boot\n")
    return log


def test_discards_a_foreign_owned_regular_log(tmp_path: Path) -> None:
    """The runner's case: a readable, peer-owned 0664 log the seam would (correctly) reject."""
    log = _console_log(tmp_path)

    assert _claim_console_inode(log, _FOREIGN_UID) is True
    assert not log.exists()


def test_keeps_a_log_this_process_already_owns(tmp_path: Path) -> None:
    """A single-actor System has nothing to hand over; its current boot window must survive."""
    log = _console_log(tmp_path)

    assert _claim_console_inode(log, os.geteuid()) is False
    assert log.read_bytes() == b"[    0.00] a prior actor's boot\n"


def test_absent_log_is_nothing_to_claim(tmp_path: Path) -> None:
    """The seam creates the log when absent, so an absent path is already the wanted state."""
    assert _claim_console_inode(tmp_path / f"{_SYS}.log", _FOREIGN_UID) is False


def test_keeps_a_symlink_for_the_seam_to_reject(tmp_path: Path) -> None:
    """ADR-0576 fails a symlinked console path by name; the handoff must not launder that away."""
    target = tmp_path / "elsewhere.log"
    target.write_bytes(b"")
    log = tmp_path / f"{_SYS}.log"
    log.symlink_to(target)

    assert _claim_console_inode(log, _FOREIGN_UID) is False
    assert log.is_symlink()


def test_keeps_a_hard_linked_log_for_the_seam_to_reject(tmp_path: Path) -> None:
    """Discarding one name of a multi-linked log would turn a loud failure into a silent start."""
    log = _console_log(tmp_path)
    (tmp_path / "second-name.log").hardlink_to(log)

    assert _claim_console_inode(log, _FOREIGN_UID) is False
    assert log.exists()


def test_claim_targets_the_systems_own_console_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The public entry point resolves through console_log_path — the seam's own path function."""
    seen: list[UUID] = []
    log = _console_log(tmp_path)

    def _fake_path(system_id: UUID) -> Path:
        seen.append(system_id)
        return log

    monkeypatch.setattr(console_actor, "console_log_path", _fake_path)
    monkeypatch.setattr(console_actor.os, "geteuid", lambda: _FOREIGN_UID)

    assert claim_console_inode(_SYS) is True
    assert seen == [_SYS]
    assert not log.exists()
