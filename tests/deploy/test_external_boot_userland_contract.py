"""The external-boot guest userland contract is declared and enforced at both of its points.

External-boot identity proof spawns ``/usr/bin/uname`` and ``/usr/bin/cat`` in the guest by
absolute path (ADR-0590). Two roles hold images to that, and they cover different gaps:

* ``guest_base_image`` verifies each qcow2 it builds, immediately before staging it. It carries
  the staging copy's guard, so it fires only when a build ran.
* ``remote_libvirt_facts`` verifies each staged volume before declaring it an ``[[image]]``.
  That is the half covering a volume staged before ADR-0590, or built anywhere else.

Neither the scratch build nor a real libguestfs appliance can run in CI, so what these lock is
the *declaration* and the shape of each check. The render harness
(``deploy/ansible/tests/run-remote-libvirt-facts-render.sh``) exercises the stage-time role's
classification against a guestfish double; a real build host is what turns the build-time check
into evidence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_ANSIBLE = Path(__file__).resolve().parents[2] / "deploy" / "ansible"
_ROLE = _ANSIBLE / "roles" / "guest_base_image" / "tasks"
BUILD_SCRATCH = _ROLE / "build_scratch.yml"
BUILD_ONE = _ROLE / "build_one.yml"
FACTS_TASKS = _ANSIBLE / "roles" / "remote_libvirt_facts" / "tasks" / "main.yml"
ALL_VARS = _ANSIBLE / "inventory" / "group_vars" / "all.yml"

REQUIRED_PROGRAMS = ("/usr/bin/uname", "/usr/bin/cat")
VERIFY_TASK = "external-boot guest userland"
STAGE_TASK = "Stage the finished image into the (root-owned) pool for {{ image.name }}"
PROBE_TASK = "Verify the external-boot guest userland in each unverified staged volume"
INSPECT_FAIL_TASK = "Fail loudly when a staged volume could not be inspected at all"


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


def _run_command(task: dict[str, Any]) -> str:
    """The shell fragment a ``--run-command`` task hands its instrument."""
    argv = _argv(task)
    return argv[argv.index("--run-command") + 1]


def test_build_verification_inspects_the_qcow2_it_is_about_to_stage() -> None:
    """The instrument is virt-customize and the disk it opens is the built image.

    Asserting the ``test -x`` text alone leaves the task free to invoke ``true``, or to point
    ``-a`` at some other disk, while still reading as a check.
    """
    argv = _argv(_named(_tasks(BUILD_ONE), VERIFY_TASK))
    assert argv[0] == "virt-customize", "the check must run an instrument that opens the image"
    assert argv[argv.index("-a") + 1] == "{{ guest_base_image_qcow2 }}", (
        "the disk inspected must be the qcow2 this task is about to stage"
    )


@pytest.mark.parametrize(
    ("present", "mode", "expected_ok"),
    [
        (("uname", "cat"), 0o755, True),
        ((), 0o755, False),
        (("uname",), 0o755, False),
        (("uname", "cat"), 0o644, False),
    ],
    ids=["both-executable", "both-absent", "cat-absent", "cat-not-executable"],
)
def test_build_verification_command_accepts_and_refuses(
    tmp_path: Path,
    present: tuple[str, ...],
    mode: int,
    expected_ok: bool,
) -> None:
    """The check's own shell fragment passes a conformant tree and fails every other shape.

    The string-shape tests above cannot tell an enforcing check from an inert one: appending
    ``|| true``, or swapping the instrument for ``true``, leaves both ``test -x`` substrings in
    place. So run the fragment the task actually ships. ``/usr/bin`` is rewritten to a tmp_path
    so no image or libguestfs is needed — what is under test is the condition, not the appliance.
    """
    bin_dir = tmp_path / "usr" / "bin"
    bin_dir.mkdir(parents=True)
    for program in present:
        target = bin_dir / program
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(mode)

    fragment = _run_command(_named(_tasks(BUILD_ONE), VERIFY_TASK))
    assert "/usr/bin/" in fragment, "the fragment must name the paths it checks absolutely"
    rehomed = fragment.replace("/usr/bin/", f"{bin_dir}/")

    completed = subprocess.run(["sh", "-c", rehomed], capture_output=True, text=True, check=False)
    assert (completed.returncode == 0) is expected_ok, (
        f"rc={completed.returncode} for present={present} mode={mode:o}; stderr={completed.stderr}"
    )
    if not expected_ok:
        assert "ADR-0590" in completed.stderr, "a refusal must name the record that explains it"


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


# --- The stage-time half: remote_libvirt_facts (ADR-0590 Decision 4). ---


def _declared_programs() -> list[str]:
    """The contract's program list, declared once and read by both roles."""
    return yaml.safe_load(ALL_VARS.read_text(encoding="utf-8"))[
        "kdive_external_boot_userland_programs"
    ]


def test_the_two_roles_enforce_one_declared_program_list() -> None:
    """Both checks read the same declaration, so neither can drift from the other.

    The contract is stated once in all.yml. The stage-time role consumes that variable
    directly; the build-time check embeds the paths in a shell fragment, which is what could
    silently disagree, so the fragment is held to the declaration here.
    """
    declared = _declared_programs()
    assert declared == list(REQUIRED_PROGRAMS)

    fragment = _run_command(_named(_tasks(BUILD_ONE), VERIFY_TASK))
    for program in declared:
        assert f"test -x {program}" in fragment, f"{program} is declared but not built-checked"

    probe = _named(_tasks(FACTS_TASKS), PROBE_TASK)
    assert "kdive_external_boot_userland_programs" in probe["ansible.builtin.command"]["stdin"], (
        "the stage-time check must read the declared list, not its own copy of the paths"
    )


def test_stage_time_check_opens_the_staged_volume_read_only() -> None:
    """The instrument must not write to what it inspects.

    virt-customize — the build-time instrument — rewrites the image on every invocation (random
    seed, SELinux relabel). Harmless against a build workdir; not against a staged volume other
    Systems clone from. Different volume, different blast radius.
    """
    argv = _argv(_named(_tasks(FACTS_TASKS), PROBE_TASK))
    assert argv[0] == "guestfish"
    assert "--ro" in argv, "a staged volume must be opened read-only"
    assert "virt-customize" not in argv, "the writing instrument must not reach a staged volume"
    assert argv[argv.index("--add") + 1] == "{{ storage_pool_target }}/{{ volume_name }}.qcow2"


def test_stage_time_check_follows_symlinks() -> None:
    """A busybox image supplies the applets as symlinks, and that satisfies the identity proof.

    guest-exec spawns ``/usr/bin/cat`` and the kernel follows the link, so a symlink to
    /usr/sbin/busybox is conformant. guestfish's ``is-file`` lstats by default and reports false
    for exactly that shape, which would omit a working image. The render harness cannot catch
    this — its guestfish double has no filesystem — so the flag is locked here.
    """
    stdin = _named(_tasks(FACTS_TASKS), PROBE_TASK)["ansible.builtin.command"]["stdin"]
    assert "followsymlinks:true" in stdin


def test_uninspectable_volume_is_not_folded_into_the_missing_set() -> None:
    """An absent program and an unreadable volume are different answers, with different remedies.

    Folding the second into the first would let one host with a broken libguestfs emit an empty
    but valid fragment, breaking provisioning everywhere with nothing naming the cause.
    """
    task = _named(_tasks(FACTS_TASKS), INSPECT_FAIL_TASK)
    assert "ansible.builtin.assert" in task, "an uninspectable volume must stop the play"
    fail_msg = task["ansible.builtin.assert"]["fail_msg"]
    assert "could not inspect" in fail_msg
    assert "ADR-0590" in fail_msg
    assert "libguestfs" in fail_msg, "the message must name what to install"


def test_stage_time_check_is_gated_by_a_cache_keyed_on_the_volume() -> None:
    """Steady state must not pay an appliance launch per image per run.

    The key has to move when the image does, or a restaged volume would inherit the old
    verdict — so it carries the volume's size and mtime as well as the contract's identity.
    """
    lookup = _named(_tasks(FACTS_TASKS), "Look up the cached userland verdict")
    key = lookup["ansible.builtin.stat"]["path"]
    assert "item.stat.size" in key
    assert "item.stat.mtime" in key
    assert "remote_libvirt_facts_userland_contract_id" in key

    probe = _named(_tasks(FACTS_TASKS), PROBE_TASK)
    assert "rejectattr('stat.exists')" in probe["loop"], (
        "the probe must run only for volumes with no cached verdict"
    )
