"""Unit tests for the provisioned-family console-inode handoff (tests.live_vm.console_actor).

Runs in ordinary CI, like the other tests/live_vm guards: the live tier is where the handoff is
*used*, but every branch of the decision is host-free. ``_claim_console_inode`` takes the euid as a
parameter precisely so the foreign-owner branch is provable without root and without a second
account — passing an euid that is not the file's owner is exactly the runner's situation, where the
worker account creates the log and the operator account starts the domain.

The last test is a source guard rather than a behaviour test. The call site lives in a
``live_vm``-gated body that ordinary CI never executes, so nothing but the self-hosted dispatch
would notice its removal; the guard reads the boot test's AST instead, and fails here.
"""

from __future__ import annotations

import ast
import os
import warnings
from pathlib import Path
from uuid import UUID

import pytest

from tests.live_vm import console_actor
from tests.live_vm.console_actor import _claim_console_inode, claim_console_inode

_SYS = UUID("61510773-cdf7-489f-9a8f-254e6ec98227")
_FOREIGN_UID = os.geteuid() + 1
_INSTALL_TEST = Path(__file__).resolve().parents[1] / "providers/local_libvirt/test_install.py"
_BOOT_TEST = "test_live_vm_real_install_boot"


def _console_log(tmp_path: Path) -> Path:
    log = tmp_path / f"{_SYS}.log"
    log.write_bytes(b"[    0.00] a prior actor's boot\n")
    return log


def test_discards_a_foreign_owned_regular_log(tmp_path: Path) -> None:
    """The runner's case: a readable, peer-owned 0664 log the seam would (correctly) reject."""
    log = _console_log(tmp_path)
    owner = log.stat().st_uid

    assert _claim_console_inode(log, _FOREIGN_UID) == owner
    assert not log.exists()


def test_keeps_a_log_this_process_already_owns(tmp_path: Path) -> None:
    """A single-actor System has nothing to hand over; its current boot window must survive."""
    log = _console_log(tmp_path)

    assert _claim_console_inode(log, os.geteuid()) is None
    assert log.read_bytes() == b"[    0.00] a prior actor's boot\n"


def test_absent_log_is_nothing_to_claim(tmp_path: Path) -> None:
    """The seam creates the log when absent, so an absent path is already the wanted state."""
    assert _claim_console_inode(tmp_path / f"{_SYS}.log", _FOREIGN_UID) is None


def test_keeps_a_symlink_for_the_seam_to_reject(tmp_path: Path) -> None:
    """ADR-0576 fails a symlinked console path by name; the handoff must not launder that away."""
    target = tmp_path / "elsewhere.log"
    target.write_bytes(b"")
    log = tmp_path / f"{_SYS}.log"
    log.symlink_to(target)

    assert _claim_console_inode(log, _FOREIGN_UID) is None
    assert log.is_symlink()


def test_keeps_a_hard_linked_log_for_the_seam_to_reject(tmp_path: Path) -> None:
    """Discarding one name of a multi-linked log would turn a loud failure into a silent start."""
    log = _console_log(tmp_path)
    (tmp_path / "second-name.log").hardlink_to(log)

    assert _claim_console_inode(log, _FOREIGN_UID) is None
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

    with pytest.warns(UserWarning):
        assert claim_console_inode(_SYS) is True
    assert seen == [_SYS]
    assert not log.exists()


def test_a_discard_announces_the_path_and_the_uid_it_took(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live run must show the handoff happened; a silent delete cannot be told from a no-op."""
    log = _console_log(tmp_path)
    owner = log.stat().st_uid
    monkeypatch.setattr(console_actor, "console_log_path", lambda _sid: log)
    monkeypatch.setattr(console_actor.os, "geteuid", lambda: _FOREIGN_UID)

    with pytest.warns(UserWarning) as caught:
        claim_console_inode(_SYS)

    announced = str(caught[0].message)
    assert str(log) in announced
    assert f"uid {owner}" in announced
    assert str(_SYS) in announced


def test_nothing_is_announced_when_nothing_is_claimed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Silence is the honest signal for the no-op path, and keeps the live run's output clean."""
    log = _console_log(tmp_path)
    monkeypatch.setattr(console_actor, "console_log_path", lambda _sid: log)

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning here fails the test
        assert claim_console_inode(_SYS) is False
    assert log.exists()


def _first_statement_calling(body: list[ast.stmt], callee: str) -> int | None:
    """The index of the first statement in ``body`` containing a call to ``callee``."""
    for index, statement in enumerate(body):
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == callee:
                return index
            if isinstance(func, ast.Attribute) and func.attr == callee:
                return index
    return None


def test_the_live_boot_test_claims_the_inode_before_it_boots() -> None:
    """A source guard for a live_vm body ordinary CI never runs (see the module docstring)."""
    tree = ast.parse(_INSTALL_TEST.read_text(encoding="utf-8"), filename=str(_INSTALL_TEST))
    bodies = [
        node.body
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == _BOOT_TEST
    ]
    assert len(bodies) == 1, f"expected exactly one {_BOOT_TEST} in {_INSTALL_TEST}"

    claim = _first_statement_calling(bodies[0], "claim_console_inode")
    boot = _first_statement_calling(bodies[0], "boot")
    assert boot is not None, f"{_BOOT_TEST} no longer starts a domain — this guard is stale"
    assert claim is not None, (
        f"{_BOOT_TEST} starts a worker-provisioned System's domain in-process without claiming "
        "its console inode first; the boot will fail ADR-0576's identity check on the runner"
    )
    assert claim < boot, f"{_BOOT_TEST} must claim the console inode BEFORE the boot, not after"
