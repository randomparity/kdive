# External-boot guest userland contract — implementation plan

**Goal.** Settle #2160 with the answer that `bare-kdive-remote-base` is admissible, by making the
guest userland external-boot identity proof depends on a declared, verified property of the image
instead of an undeclared transitive accident. The decision is
[ADR-0590](../../adr/0590-external-boot-requires-a-posix-userland-in-every-catalog-image.md); the
requirements are
[the spec](../specs/2026-09-02-external-boot-guest-userland-contract-design.md). Neither is
restated here.

**Tech stack.** Ansible (ansible-core 2.21.1, ansible-lint 26.4.0 `profile: production`), Python
3.14 with PyYAML 6.0.3, pytest.

Expected implementation size: 125–155 changed lines (S) — summed from the file map and this plan's
own fenced blocks: 3 lines in `build_scratch.yml`, 20 in `build_one.yml`, 5 in `all.yml`, ~20 in
the README, 89 in the test module.

## Global Constraints

- The required paths are exactly `/usr/bin/uname` and `/usr/bin/cat`; `coreutils` supplies both on
  RedHat and Debian families alike. `busybox` stays in the scratch installroot.
- ansible-lint runs `profile: production`: capitalised `name`, fully-qualified module names, and an
  explicit `changed_when` on every `ansible.builtin.command`. yamllint caps lines at 160 characters.
- The verification task's `when` must be byte-identical to the staging copy's, because the local
  qcow2 it inspects exists only when a build ran.
- The ADR is `docs/adr/0590-external-boot-requires-a-posix-userland-in-every-catalog-image.md`,
  status `Accepted (2026-09-02)`. `scripts/guards/check_adr_status.py` fails a `Proposed` ADR cited
  from `tests/`, and the test module cites it, so it must be `Accepted` in this same PR.
- Guardrails: `just lint`, `just type`, `just lint-ansible`, `just test-ansible`; `just ci` as the
  pre-push gate, run as `just ci > <file> 2>&1 < /dev/null; echo $?` — never piped through
  `tail`/`head`.
- Out of bounds (sibling-owned): `src/kdive/providers/remote_libvirt/lifecycle/` and everything
  under it, and `tests/providers/remote_libvirt/fakes.py`.

## File map

| Path | Change | Answerable for |
|---|---|---|
| `deploy/ansible/roles/guest_base_image/tasks/build_scratch.yml` | modify | declaring `coreutils` on both bootstrap paths |
| `deploy/ansible/roles/guest_base_image/tasks/build_one.yml` | modify | verifying the contract on the built qcow2 before staging |
| `deploy/ansible/inventory/group_vars/all.yml` | modify | recording on the bare catalog entry why the contract binds it |
| `deploy/ansible/README.md` | modify | operator-facing statement of the contract and the re-verify path |
| `tests/deploy/test_guest_base_image_external_boot_userland.py` | create | structurally locking both edits |

## Task 1 — Declare and verify the contract in the build

One task: no reviewer could accept the `coreutils` declaration while rejecting the check that proves
it, or the reverse. The two are not interchangeable — the declaration pins the scratch installroot,
the only rootfs this repository composes; the check is the sole control on the other three entries.

**Interfaces.** Consumes role facts already defined earlier in `build_one.yml`, confirmed present at
these lines: `guest_base_image_force` (key at line 22, folded value on 23), `guest_base_image_qcow2`
(line 24), and `guest_base_image_staged` (registered at line 30). Defines no new variable. The test
relies on the new task's `name` containing `external-boot guest userland`, its argv containing both
required paths, and its position preceding the task named
`Stage the finished image into the (root-owned) pool for {{ image.name }}`.

### Step 1 — Write the test first

Create `tests/deploy/test_guest_base_image_external_boot_userland.py`:

```python
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
    """The role checks each built qcow2 for both absolute paths the identity proof spawns."""
    verify = _named(_tasks(BUILD_ONE), VERIFY_TASK)
    command = " ".join(_argv(verify))
    for program in REQUIRED_PROGRAMS:
        assert program in command, f"{program} is not checked"
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
```

### Step 2 — Confirm it fails

```sh
uv run python -m pytest tests/deploy/test_guest_base_image_external_boot_userland.py -q
```

Expect **4 failed, 1 error**: two `AssertionError` from the missing `coreutils` entries, two from
`_named` on the verification task not existing, and a `StopIteration` **error** (not a failure) from
`test_verification_precedes_staging`, whose generator expression finds no matching task.

### Step 3 — Name `coreutils` on the RedHat bootstrap path

In `build_scratch.yml`, task `Bootstrap a minimal RedHat-family rootfs for {{ image.name }}`, insert
into the argv list immediately after the `- busybox` entry (line 28):

```yaml
      # /usr/bin/uname and /usr/bin/cat for the external-boot identity proof (ADR-0590).
      - coreutils
```

### Step 4 — Name `coreutils` on the Debian bootstrap path

In the same file, task `Bootstrap a minimal Debian-family rootfs for {{ image.name }}`, replace the
`--include` folded scalar (lines 47-48) with:

```yaml
      - >-
        --include=systemd-sysv,busybox,coreutils,qemu-guest-agent,kexec-tools,makedumpfile,curl,tar,linux-image-generic,grub-efi-amd64
```

### Step 5 — Verify the contract on the built image

In `build_one.yml`, insert immediately before the task named
`Stage the finished image into the (root-owned) pool for {{ image.name }}` (line 220):

```yaml
# --- Verify the guest userland the external-boot identity proof reads (ADR-0590). ---
# Guarded like the staging copy below: the local qcow2 exists only when a build ran, so an image
# already staged is not re-verified until force_image_rebuild. `test -x` is a shell builtin, so the
# check does not depend on the binaries it tests. `changed_when: false` describes the task's
# contract — it asserts rather than customizes — not the qcow2's bytes, which virt-customize
# rewrites on every invocation (random seed, SELinux relabel).
- name: Verify the external-boot guest userland for {{ image.name }}
  ansible.builtin.command: # noqa: command-instead-of-module
    # No Ansible module wraps virt-customize; the command module is the only option.
    argv:
      - virt-customize
      - -a
      - "{{ guest_base_image_qcow2 }}"
      - --run-command
      - >-
        test -x /usr/bin/uname && test -x /usr/bin/cat || { echo "kdive: image lacks
        /usr/bin/uname and/or /usr/bin/cat, which the external-boot identity proof spawns by
        absolute path (ADR-0590); install coreutils in this image" >&2; exit 1; }
  when: (not guest_base_image_staged.stat.exists) or guest_base_image_force | bool
  changed_when: false
```

### Step 6 — Record the contract on the catalog entry

In `deploy/ansible/inventory/group_vars/all.yml`, on the `bare-kdive-remote-base` entry, insert
above its `host_distros` line:

```yaml
    # External-boot admission (ADR-0590, #2160). The installroot names `coreutils`, so
    # /usr/bin/uname and /usr/bin/cat — which the external-boot identity proof spawns by absolute
    # path — are a declared property of this image rather than a transitive systemd dependency.
    # guest_base_image verifies both paths in each qcow2 it builds, for every entry here, before
    # staging it.
```

### Step 7 — Record it for operators

In `deploy/ansible/README.md`, add a subsection immediately after
`### Build-host admission (host_distros)`, in the surrounding prose style, covering: identity proof
spawns `/usr/bin/uname` and `/usr/bin/cat` by absolute path through the guest agent; every catalog
image owes both, and `coreutils` supplies them; the scratch installroot names it explicitly, while
the other three take their userland from a base image this repository does not compose and rest on
the check alone; `guest_base_image` verifies both paths in each qcow2 it builds, before staging, so
a non-conformant image never reaches the pool or an `[[image]]` block; the check runs only on a
build, so any already-staged volume stays unverified until `force_image_rebuild=true`. Link
`docs/adr/0590-external-boot-requires-a-posix-userland-in-every-catalog-image.md`.

Then append one sentence to the scratch bullet under `## Caveats`, so it does not read as if
userland contents were part of what is unvalidated: the userland is declared and the build verifies
the external-boot paths before staging, but neither has run on a real host.

### Step 8 — Confirm it passes, and that each test bites

Run the module: expect `5 passed`. Then, for each of the five, make one controlled fault, re-run,
confirm that test and only that test fails, and revert: delete `coreutils` from the RedHat argv;
delete it from the Debian `--include`; delete `/usr/bin/cat` from the verification argv; move the
verification task after the staging copy; change its `when` to `true`.

### Step 9 — Confirm the gates

```sh
just lint; echo $?
just type; echo $?
just lint-ansible < /dev/null; echo $?
just test-ansible < /dev/null; echo $?
```

Expect `0` from each. `run-guest-base-image-admission.sh` inside `test-ansible` must still report
all six of its cases ok — this change does not touch the admission block, so any movement there is
a regression.

### Step 10 — Acceptance criteria

- `build_scratch.yml` names `coreutils` on both bootstrap paths and still names `busybox` on both.
- `build_one.yml` carries exactly one task naming both required paths, before the staging copy,
  with a `when` byte-identical to that copy's and `changed_when: false`.
- The diff touches `all.yml` and `deploy/ansible/README.md`, and both name ADR-0590 — the
  discoverability pointers ADR-0590 substitutes for a forward amendment note in ADR-0188 §4.
- Five tests pass, each failing when its own subject is broken (Step 8).
- `just lint`, `just type`, `just lint-ansible`, `just test-ansible`, `just adr-status-check` all
  exit 0, and `git diff --name-only main...HEAD` names no sibling-owned path.

### Step 11 — Commit

Run `just format` (Python changed), then stage explicit paths and commit as
`fix(provisioning): declare and verify the external-boot guest userland`.

## Review deferrals

None. Every finding from the design review was `accepted-fixed`; the run's dispositions are recorded
in the quest's `WORK:REVIEW` annotation and the PR body.
