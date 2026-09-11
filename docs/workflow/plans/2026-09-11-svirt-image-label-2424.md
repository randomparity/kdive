# Static `svirt_image_t` label for kdive images — implementation plan (#2424)

**Goal.** Make provisioning succeed on an SELinux-enforcing RedHat-family host with sVirt
confinement intact, by labeling kdive's image directories `svirt_image_t` instead of
`virt_image_t`, and by correcting the operator text that still prescribes the old label or reports
the bug as open.

**Architecture.** One surface only: host preparation under `examples/local-libvirt/`. A single
sourced shell helper applies the label to one directory and migrates an existing rule on the same
pattern; three call sites use it. No Python behavior changes, and the rendered domain XML is
untouched — a first design cycle proposed a per-disk `<seclabel>` and the review retired it
(ADR-0639, rejected alternatives).

**Tech stack.** Bash (installer scripts), pytest (driving the helper through a stub PATH).

**Spec:** `docs/workflow/specs/2026-09-11-svirt-image-label-2424-design.md`
**Decision:** `docs/adr/0639-static-svirt-image-label-for-session-mode-domains.md`

Expected implementation size: 150–200 changed lines (M) — derived from the file map below: one new
~30-line shell helper, one new ~85-line test module, three small script edits, and six prose or
help-text corrections.

## Global Constraints

- Python 3.14, managed with `uv`. Ruff line length **100**, lint set `E,F,I,UP,B,SIM`. `ty` runs
  with strict defaults over **src and tests**.
- Shell is **Bash**; `just lint-shell` runs
  `shfmt -f scripts deploy/compose deploy/remote-libvirt-guest-helpers deploy/ansible/tests examples | xargs shellcheck`,
  so a new file under `examples/` is linted by both automatically. Both example scripts run under
  `set -euo pipefail`.
- A `# shellcheck source=` directive in this repository is always paired with `disable=SC1091` —
  see `examples/local-libvirt/build-image.sh:17`. Match that, or `just lint-shell` fails.
- Guardrails: `just lint`, `just type`, `just test`; full gate
  `just ci > <file> 2>&1 < /dev/null`. Never pipe a gate through `tail`/`head`; never append
  `; echo $?`.
- Before `git commit`: `just format` for Python-only changes; for shell/Markdown, stage, run
  `prek run`, then `git add -- <exactly the paths you staged>`.
- Doc-style: use **Milestone**, never "Sprint"; avoid "critical", "robust", "comprehensive",
  "elegant" in ADRs, specs, commit messages and comments.
- ADRs live under `docs/adr/`, named `NNNN-kebab-title.md`, monotonic numbers never reused. This
  change owns **0639** and touches no other ADR file. There is no ADR index (ADR-0504).
- `semanage fcontext -l` prints each rule's pattern **verbatim** — `/var/lib/kdive/rootfs(/.*)?` —
  space-padded into columns. The backslashes in the existing greps at `install-host.sh:277` and
  `build-image.sh:52` are BRE escaping of `.` and `*`, not characters in the output. This plan
  removes both greps, so no code depends on the format either way.
- `svirt_image_t` is listed in `/etc/selinux/targeted/contexts/customizable_types` and
  `virt_image_t` is not. A plain `restorecon` therefore relabels *into* `svirt_image_t` but
  silently skips relabeling *out* of it; only a rollback needs `-F`.
- Verified host baselines for the live proof: Fedora Linux 44 Server, `libvirt-daemon-12.0.0-3.fc44`,
  `qemu-kvm-core-10.2.2-1.fc44`, `selinux-policy-44.3-1.fc44`; Rocky Linux 10.2,
  `libvirt-daemon-11.10.0-12.4.el10_2`, `selinux-policy-42.1.18-4.el10`.

## File map

| File | Action | Answerable for |
|---|---|---|
| `examples/local-libvirt/selinux-label.sh` | create | the one labeling+migration function, sourceable for tests |
| `examples/local-libvirt/install-host.sh` | modify | sources the helper; labels rootfs, rootfs/local, and install staging |
| `examples/local-libvirt/build-image.sh` | modify | sources the helper; `label_for_qemu` calls it |
| `tests/scripts/test_selinux_label.py` | create | drives the helper with stubbed `getenforce`/`semanage`/`restorecon` |
| `examples/local-libvirt/README.md` | modify | one table cell |
| `docs/operating/providers/local-libvirt.md` | modify | the SELinux bullet; delete the resolved `## Known limitation` section |
| `src/kdive/config/core_settings.py` | modify | `KDIVE_INSTALL_STAGING` help text |
| `docs/guide/reference/config.md` | regenerate | the generated row for that setting |
| `src/kdive/providers/local_libvirt/lifecycle/install.py` | modify | the staging-root remediation string |
| `deploy/ansible/roles/live_vm_host/tasks/main.yml` | modify | one comment clause |
| the ADR-0639 record | modify | Status Proposed → Accepted |

---

## Task 1 — the labeling helper and its three call sites

**Where it fits.** This is the whole functional fix.

**Creates:** `examples/local-libvirt/selinux-label.sh`, `tests/scripts/test_selinux_label.py`
**Modifies:** `examples/local-libvirt/install-host.sh`, `examples/local-libvirt/build-image.sh`

**Interfaces.** This task defines, and nothing earlier provides:

```bash
# examples/local-libvirt/selinux-label.sh
kdive_label_svirt_image <directory>   # returns 0 always; no-ops off SELinux-enforcing hosts
```

Task 2 consumes nothing from this task.

### Verification

| Contract | Mode | Detail |
|---|---|---|
| A pattern with no rule gets one `svirt_image_t` rule added, then `restorecon` | `focused-test` | `tests/scripts/test_selinux_label.py::test_adds_rule_when_absent`; red before the helper exists — `bash -c 'source <missing>; kdive_label_svirt_image …'` exits 127, so `subprocess.run(..., check=True)` raises `CalledProcessError`; green via `uv run python -m pytest tests/scripts/test_selinux_label.py -q` |
| A pattern carrying a stale rule is **modified**, never added twice | `focused-test` | `…::test_migrates_stale_rule` — asserts a `-m -t svirt_image_t` call and no second rule; red against the shipped `-a`-only logic, whose presence check short-circuits and records no write at all |
| The helper no-ops when SELinux is not enforcing | `focused-test` | `…::test_noop_when_not_enforcing` — asserts no `semanage`/`restorecon` invocation was recorded |
| A missing `semanage` reports and returns 0 | `focused-test` | `…::test_reports_missing_semanage` — asserts nothing was written and the exit status is 0 |
| The three installer call sites | `task-test-not-applicable` | `tests/scripts/test_install_host_gates.py` stops the installer at its `sudo` preflight, far above these lines, and driving the rest needs a real enforcing host with root — the live proof. The branching this task adds lives in the helper, which the four tests above cover. |

### Steps

1. Create `examples/local-libvirt/selinux-label.sh`:

```bash
#!/usr/bin/env bash
# Label a kdive image directory so a confined QEMU domain can use it (ADR-0639, #2424).
#
# svirt_t may read virt_image_t but may not write or map it, and the unprivileged session
# libvirt daemon never performs the dynamic relabel that closes that gap on a privileged
# daemon. So the static label has to be one the confined domain can use: svirt_image_t:s0,
# which every domain reaches by MCS dominance whatever categories libvirt draws for it. A
# privileged daemon relabels from svirt_image_t exactly as it did from virt_image_t, so this
# label is correct under both.
#
# Sourced by install-host.sh and build-image.sh; sourcing it runs nothing.

kdive_label_svirt_image() {
  local directory="$1" pattern="${1}(/.*)?"

  command -v getenforce >/dev/null 2>&1 || return 0
  [[ "$(getenforce)" == "Enforcing" ]] || return 0

  if ! command -v semanage >/dev/null 2>&1; then
    echo "SELinux is enforcing but semanage is missing; install policycoreutils-python-utils" >&2
    echo "and label ${directory} svirt_image_t before provisioning." >&2
    return 0
  fi

  # -m modifies an existing rule, -a adds a missing one, and each fails when the other case
  # applies. Trying -m first reaches the same state on a fresh host and on one installed before
  # ADR-0639 (whose rule is still virt_image_t), without parsing `semanage fcontext -l` output.
  sudo semanage fcontext -m -t svirt_image_t "${pattern}" 2>/dev/null ||
    sudo semanage fcontext -a -t svirt_image_t "${pattern}"
  sudo restorecon -R "${directory}"
}
```

2. In `examples/local-libvirt/install-host.sh`, source the helper immediately after `example_dir`
   is resolved (`install-host.sh:16`; the variable is `example_dir`, not `script_dir`):

```bash
# shellcheck source=examples/local-libvirt/selinux-label.sh disable=SC1091
source "${example_dir}/selinux-label.sh"
```

3. In the same file, add the install-staging root beside the existing `local/` creation at step 7
   (`install-host.sh:265-266`), since the installer does not create it today:

```bash
step "/var/lib/kdive/install"
sudo install -d -o "${USER}" -g kdive-live-libvirt -m 2770 /var/lib/kdive/install
```

4. Replace the whole of step 7b (`install-host.sh:268-285`, the
   `if command -v getenforce …` block ending at its `fi`) with:

```bash
# 7b. SELinux labels for every directory a confined domain opens. Provisioning writes each
#     System's overlay under rootfs/ and direct-kernel boot maps the baseline kernel/initrd from
#     there; the install plane points a live domain's <os> at kernel/initrd under install/.
#     svirt_t can do neither against virt_image_t (ADR-0639). build-image.sh owns the nested
#     rootfs/local rule, migrated here too so re-running this script alone is sufficient.
step "SELinux svirt_image_t on the kdive image directories"
kdive_label_svirt_image /var/lib/kdive/rootfs
kdive_label_svirt_image /var/lib/kdive/rootfs/local
kdive_label_svirt_image /var/lib/kdive/install
```

5. In `examples/local-libvirt/build-image.sh`, source the helper after `example_dir` is resolved
   (`build-image.sh:15`, beside the existing `env.sh` source at `:17`):

```bash
# shellcheck source=examples/local-libvirt/selinux-label.sh disable=SC1091
source "${example_dir}/selinux-label.sh"
```

   and reduce `label_for_qemu` (`build-image.sh:44-55`) to:

```bash
# Label the rootfs directory on SELinux-enforcing hosts only (Fedora/EL). A qcow2 published from
# a $HOME workspace can carry data_home_t, which the confined domain cannot read (ADR-0639).
label_for_qemu() {
  kdive_label_svirt_image "${rootfs_dir}"
}
```

6. Create `tests/scripts/test_selinux_label.py`:

```python
"""Gate tests for examples/local-libvirt/selinux-label.sh (ADR-0639, #2424).

The helper is sourced, not executed: each test drives one function call with stubbed
getenforce/sudo/semanage/restorecon on PATH, so nothing is ever labeled. Every stub appends its
argv to a log file, and that log is the assertion surface.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tests.host_capabilities import requires_bash

HELPER = Path(__file__).resolve().parents[2] / "examples" / "local-libvirt" / "selinux-label.sh"
BASH = shutil.which("bash")

pytestmark = requires_bash(4, 3, "the helper uses [[ ]] and local")

_TARGET = "/var/lib/kdive/rootfs"
_PATTERN = f"{_TARGET}(/.*)?"


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(body)
    path.chmod(0o755)


def _run(tmp_path: Path, *, enforce: str, modify_rc: int = 0, semanage: bool = True) -> list[str]:
    """Source the helper, call it once, and return the recorded stub invocations.

    ``modify_rc`` is the status the ``semanage fcontext -m`` arm returns: 0 stands for a host
    that already has a rule on the pattern, 1 for a fresh host where only ``-a`` can succeed.
    """
    assert BASH is not None, "bash is required to source the helper"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "log"
    _stub(bindir, "getenforce", f"#!/bin/sh\necho {enforce}\n")
    # sudo is transparent: it records nothing and runs its argv, so the stubs below see exactly
    # the argv the helper passed.
    _stub(bindir, "sudo", '#!/bin/sh\nexec "$@"\n')
    _stub(
        bindir,
        "semanage",
        f'#!/bin/sh\necho "semanage $*" >> "{log}"\n'
        f'[ "$2" = "-m" ] && exit {modify_rc}\nexit 0\n',
    )
    _stub(bindir, "restorecon", f'#!/bin/sh\necho "restorecon $*" >> "{log}"\n')
    if not semanage:
        (bindir / "semanage").unlink()
    subprocess.run(
        [BASH, "-c", f'source "{HELPER}"; kdive_label_svirt_image {_TARGET}'],
        env={"PATH": str(bindir)},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return log.read_text().splitlines() if log.exists() else []


def test_adds_rule_when_absent(tmp_path: Path) -> None:
    calls = _run(tmp_path, enforce="Enforcing", modify_rc=1)
    assert f"semanage fcontext -m -t svirt_image_t {_PATTERN}" in calls
    assert f"semanage fcontext -a -t svirt_image_t {_PATTERN}" in calls
    assert f"restorecon -R {_TARGET}" in calls


def test_migrates_stale_rule(tmp_path: Path) -> None:
    calls = _run(tmp_path, enforce="Enforcing", modify_rc=0)
    assert f"semanage fcontext -m -t svirt_image_t {_PATTERN}" in calls
    assert not any(" -a -t " in call for call in calls), "a second rule must not be added"
    assert f"restorecon -R {_TARGET}" in calls


def test_noop_when_not_enforcing(tmp_path: Path) -> None:
    assert _run(tmp_path, enforce="Permissive") == []


def test_reports_missing_semanage(tmp_path: Path) -> None:
    assert _run(tmp_path, enforce="Enforcing", semanage=False) == []
```

7. Run the focused tests red first, before steps 1–6:
   `uv run python -m pytest tests/scripts/test_selinux_label.py -q`
   Expect: collection succeeds, all four fail with `subprocess.CalledProcessError` and exit
   status 127 — the function is undefined after `source` of a non-existent file.
8. After steps 1–6, run the same command. Expect: `4 passed`.
9. `just lint-shell` — expect no output and exit 0.
10. Stage, `prek run`, re-add exactly the staged paths, commit:
    `fix(local-libvirt): label kdive image directories svirt_image_t`

### Acceptance criteria

- `kdive_label_svirt_image` exists in one file and is called three times by `install-host.sh` and
  once by `build-image.sh`; neither script contains a `semanage fcontext` invocation of its own.
- No `virt_image_t` literal remains in `examples/local-libvirt/`.
- The four focused tests pass; `just lint-shell` is green.

---

## Task 2 — correct the operator-facing text, and ratify the ADR

**Where it fits.** After Task 1 the project's own documentation tells operators the feature is
broken and prescribes the label that breaks it. This closes that, and flips ADR-0639 to Accepted
in the PR that implements it, as `docs/adr/README.md` requires.

**Modifies:** `examples/local-libvirt/README.md`,
`docs/operating/providers/local-libvirt.md`, `src/kdive/config/core_settings.py`,
`docs/guide/reference/config.md`, `src/kdive/providers/local_libvirt/lifecycle/install.py`,
`deploy/ansible/roles/live_vm_host/tasks/main.yml`, the ADR-0639 record

**Interfaces.** Consumes nothing and provides nothing; no signature changes.

### Verification

| Contract | Mode | Detail |
|---|---|---|
| The generated config table matches the changed `Setting` help text | `focused-test` | `just config-docs-check` — red immediately after editing `core_settings.py` and before regenerating, green after; CI gates this recipe individually |
| ADR-0639 carries a valid, non-Proposed status while `src/` cites it | `focused-test` | `just adr-status-check` — red while the record says `Proposed` and `install.py` cites ADR-0639, green once flipped to `Accepted` |
| Prose corrections | `task-test-not-applicable` | documentation wording with no executable consumer; `just docs-links` and `just docs-paths` cover link and path integrity |

### Steps

1. `examples/local-libvirt/README.md` — in the `build-image.sh` table row, change "label the
   rootfs directory `virt_image_t` on SELinux hosts" to "label the rootfs directory
   `svirt_image_t` on SELinux hosts (ADR-0639)".
2. `docs/operating/providers/local-libvirt.md:76-78` — replace the **SELinux** bullet body with:
   "Fedora and Enterprise Linux run SELinux enforcing, so `install-host.sh` and `build-image.sh`
   label the kdive image directories `svirt_image_t` for the confined domain (ADR-0639).
   `install-host.sh` installs `policycoreutils-python-utils` for the `semanage` that needs."
3. Same file — delete the whole `## Known limitation — SELinux and per-System overlays` section:
   lines **108-122**, from the `##` heading through the blank line before `## Preflight` at line
   123. It describes a resolved defect and hands the reader `setenforce 0`, which ADR-0639
   rejects. Add no replacement section; the SELinux bullet above now carries the operative
   statement.
4. `src/kdive/config/core_settings.py` — in `INSTALL_STAGING.help`, change "on SELinux hosts with
   the virt_image_t label" to "on SELinux hosts with the svirt_image_t label (ADR-0639)".
5. Regenerate the config table and confirm: `just config-docs` (the generator recipe, `justfile:588`),
   then `just config-docs-check` (`justfile:592`) — expect exit 0. Stage the regenerated
   `docs/guide/reference/config.md` with the source edit; CI gates the check recipe individually.
6. `src/kdive/providers/local_libvirt/lifecycle/install.py:569-571` — change the remedy string's
   tail from "on SELinux hosts give it the virt_image_t label" to "on SELinux hosts give it the
   svirt_image_t label (ADR-0639)". Update the one assertion that reads it,
   `tests/providers/local_libvirt/test_install.py:1443`, from `assert "virt_image_t" in remedy`
   to `assert "svirt_image_t" in remedy`, and run
   `uv run python -m pytest tests/providers/local_libvirt/test_install.py -q -k staging`
   — expect it red between the two edits and green after.
7. `deploy/ansible/roles/live_vm_host/tasks/main.yml:1929` — change the closing clause from
   "sVirt label (the SELinux virt_image_t equivalent RHEL required)." to "sVirt label. The
   SELinux equivalent RHEL requires is a static svirt_image_t label on the image directories
   (ADR-0639)." Touch no other line in that file, and leave
   `inventory/group_vars/live_vm_runners.yml` alone: its comment describes
   `/var/lib/kdive/live-vm` and `/var/lib/kdive/install` on an Ubuntu/AppArmor runner, neither of
   which this change relabels on that host.
8. the ADR-0639 record — change `## Status` from `Proposed` to `Accepted (2026-09-11)` and delete
   the ratification note beneath it.
9. `just lint && just type && just adr-status-check && just docs-links && just docs-paths &&
   just config-docs-check` — expect every one green.
10. `just lint-ansible < /dev/null` — expect exit 0. (ansible-core aborts on non-blocking stdin;
    redirect it.)
11. Stage, `prek run`, re-add exactly the staged paths, commit:
    `docs(local-libvirt): record the svirt_image_t label and retire the SELinux limitation`

### Acceptance criteria

- No operator-facing text prescribes `virt_image_t` for a path Task 1 relabels, and none reports
  #2424 as an open limitation.
- The `qemu:///system` references in `docs/operating/runbooks/` and `src/kdive/testing/live_vm.py`
  are untouched, as the spec's Scope records.
- ADR-0639 is `Accepted`; `just adr-status-check` and `just config-docs-check` are green.

---

## Rollback and cleanup

`git revert`-clean in the repository. On a host already migrated, reverting the repository does
**not** restore the old label. An operator who needs that runs, for each of the three patterns:

```sh
sudo semanage fcontext -m -t virt_image_t '/var/lib/kdive/rootfs(/.*)?'
sudo restorecon -R -F /var/lib/kdive/rootfs
```

`-F` is required in this direction and not the other: `svirt_image_t` is a customizable type, so a
plain `restorecon` will not relabel out of it. This returns the host to the failing state #2424
describes.

## Deferrals carried into this plan

None. Every design-review finding from both passes was accepted and applied, or rejected with
evidence recorded in ADR-0639's rejected alternatives.
