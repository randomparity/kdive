"""The external-boot guest userland contract is declared and verified in the image build.

External-boot identity proof spawns ``/usr/bin/uname`` and ``/usr/bin/cat`` in the guest by
absolute path (ADR-0590). Neither the scratch build nor the verification task can run in CI —
there is no scratch-capable host — so this locks the *declaration*: the installroots name the
package that supplies those paths, and the role checks for them on each qcow2 it builds before
staging it. A real build host is what turns that check into evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROLE = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "ansible"
    / "roles"
    / "guest_base_image"
    / "tasks"
)
BUILD_SCRATCH = _ROLE / "build_scratch.yml"
BUILD_ONE = _ROLE / "build_one.yml"

REQUIRED_PROGRAMS = ("/usr/bin/uname", "/usr/bin/cat")
VERIFY_TASK = "external-boot guest userland"
STAGE_TASK = "Stage the finished image into the (root-owned) pool for {{ image.name }}"


def _tasks(path: Path) -> list[dict[str, Any]]:
    """Every task in an Ansible task file, parsed as YAML rather than matched as text."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _argv(task: dict[str, Any]) -> list[str]:
    """The argv list of a command task, as strings."""
    return [str(arg) for arg in task["ansible.builtin.command"]["argv"]]


def _named(tasks: list[dict[str, Any]], fragment: str) -> dict[str, Any]:
    """The single task whose name contains ``fragment``."""
    matches = [t for t in tasks if fragment in t["name"]]
    assert len(matches) == 1, f"expected one task naming {fragment!r}, got {len(matches)}"
    return matches[0]


def test_redhat_installroot_names_coreutils() -> None:
    """The dnf installroot names coreutils, so /usr/bin/uname is not a transitive accident."""
    argv = _argv(_named(_tasks(BUILD_SCRATCH), "RedHat-family rootfs"))
    assert "coreutils" in argv
    assert "busybox" in argv, "ADR-0590 keeps busybox; it adds coreutils beside it"


def test_debian_installroot_names_coreutils() -> None:
    """The debootstrap --include names coreutils for the same reason."""
    argv = _argv(_named(_tasks(BUILD_SCRATCH), "Debian-family rootfs"))
    include = next(arg for arg in argv if arg.startswith("--include="))
    packages = include.removeprefix("--include=").split(",")
    assert "coreutils" in packages
    assert "busybox" in packages, "ADR-0590 keeps busybox; it adds coreutils beside it"


def test_build_verifies_both_identity_proof_programs() -> None:
    """The role checks each built qcow2 for both absolute paths the identity proof spawns.

    Asserted against the ``test -x`` condition rather than the whole argv: the task's failure
    message names both paths too, so a substring match over the argv passes even when the
    condition checks only one.
    """
    verify = _named(_tasks(BUILD_ONE), VERIFY_TASK)
    command = " ".join(_argv(verify))
    for program in REQUIRED_PROGRAMS:
        assert f"test -x {program}" in command, f"{program} is named but not tested"
    assert verify["changed_when"] is False, "the task asserts; it does not customize"


def test_verification_precedes_staging() -> None:
    """A non-conformant image must fail before it is copied into the pool.

    Ordering is the whole enforcement: past the copy the volume is staged, and staged is what
    ``remote_libvirt_facts`` turns into an ``[[image]]`` block a System can select.
    """
    names = [t["name"] for t in _tasks(BUILD_ONE) if "name" in t]
    verify = next(i for i, n in enumerate(names) if VERIFY_TASK in n)
    assert verify < names.index(STAGE_TASK)


def test_verification_is_guarded_like_the_staging_copy() -> None:
    """No build path is exempt, and the guard matches the copy that consumes the same qcow2."""
    tasks = _tasks(BUILD_ONE)
    assert _named(tasks, VERIFY_TASK)["when"] == _named(tasks, STAGE_TASK)["when"]
