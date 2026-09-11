# Static `svirt_image_t` label for session-mode domains — implementation plan (#2424)

**Goal.** Make provisioning succeed on an SELinux-enforcing RedHat-family host with sVirt
confinement intact, by labeling the kdive rootfs tree `svirt_image_t` and declaring the static
label contract per disk in the domain XML.

**Architecture.** Two independent surfaces. Host preparation (`examples/local-libvirt/`) owns the
filesystem label: one shared shell helper, sourced by the two scripts that label today, that
*replaces* an existing fcontext rule on the kdive rootfs pattern instead of skipping it. Domain
rendering (`src/kdive/providers/local_libvirt/lifecycle/xml.py`) owns the XML contract: one
`<seclabel model='selinux' relabel='no'/>` inside each disk's `<source>`, added in the single
shared `_append_root_disk` that both renderers call.

**Tech stack.** Bash (installer scripts), Python 3.14 (`xml.etree.ElementTree`), pytest.

**Spec:** `docs/workflow/specs/2026-09-11-svirt-image-label-2424-design.md`
**Decision:** `docs/adr/0639-static-svirt-image-label-for-session-mode-domains.md`

Expected implementation size: 190–240 changed lines (M) — derived from the file map below: one
new ~45-line shell helper, one new ~90-line test module, two small script edits, one 8-line XML
change with ~25 lines of tests, and four prose lines.

## Global Constraints

- Python 3.14, managed with `uv`. Ruff line length **100**, lint set `E,F,I,UP,B,SIM`.
  `ty` runs with strict defaults over **src and tests**.
- Shell is **Bash**; `just lint-shell` runs `shfmt -f scripts deploy/compose
  deploy/remote-libvirt-guest-helpers deploy/ansible/tests examples | xargs shellcheck`, so any
  new file under `examples/` is linted by both automatically.
- Guardrails: `just lint`, `just type`, `just test`; full gate
  `just ci > <file> 2>&1 < /dev/null`. Never pipe a gate through `tail`/`head`; never append
  `; echo $?`.
- Before `git commit`, run `just format` for Python-only changes; for shell/Markdown, stage, run
  `prek run`, then `git add -- <exactly the paths you staged>`.
- Doc-style: use **Milestone**, never "Sprint"; avoid "critical", "robust", "comprehensive",
  "elegant" in ADRs, specs, commit messages, and comments.
- ADRs live under `docs/adr/`, named `NNNN-kebab-title.md`, monotonic numbers never reused. This change owns
  **0639** and touches no other ADR file. There is no ADR index (ADR-0504).
- Target SELinux types are the literals `svirt_image_t` (new) and `virt_image_t` (old). The
  fcontext path pattern is exactly `/var/lib/kdive/rootfs(/.*)?`; `semanage fcontext -l` prints it
  with the `.` escaped, as `/var/lib/kdive/rootfs(/\.\*)? `.
- Verified host baselines for the live proof: Fedora Linux 44 Server, `libvirt-daemon-12.0.0-3.fc44`,
  `qemu-kvm-core-10.2.2-1.fc44`, `selinux-policy-44.3-1.fc44`; Rocky Linux 10.2,
  `libvirt-daemon-11.10.0-12.4.el10_2`, `selinux-policy-42.1.18-4.el10`.

## File map

| File | Action | Answerable for |
|---|---|---|
| `examples/local-libvirt/selinux-label.sh` | create | the one labeling+migration function, sourceable for tests |
| `examples/local-libvirt/install-host.sh` | modify | sources the helper; step 7b calls it |
| `examples/local-libvirt/build-image.sh` | modify | sources the helper; `label_for_qemu` calls it |
| `examples/local-libvirt/README.md` | modify | prose naming the label |
| `tests/scripts/test_selinux_label.py` | create | drives the helper with stubbed `getenforce`/`semanage`/`restorecon` |
| `src/kdive/providers/local_libvirt/lifecycle/xml.py` | modify | `_append_root_disk` emits the per-disk seclabel |
| `tests/adversarial/test_provider_xml.py` | modify | System-domain seclabel assertion |
| `tests/providers/local_libvirt/lifecycle/test_xml.py` | modify | customization-domain seclabel assertion |
| `deploy/ansible/roles/live_vm_host/tasks/main.yml` | modify | one comment |
| `deploy/ansible/inventory/group_vars/live_vm_runners.yml` | modify | one comment |

---

## Task 1 — the labeling helper and its two callers

**Where it fits.** This is the functional fix. Without it nothing else in this change makes an
enforcing host provision.

**Creates:** `examples/local-libvirt/selinux-label.sh`, `tests/scripts/test_selinux_label.py`
**Modifies:** `examples/local-libvirt/install-host.sh`, `examples/local-libvirt/build-image.sh`,
`examples/local-libvirt/README.md`

**Interfaces.** This task defines, and nothing earlier provides:

```bash
# examples/local-libvirt/selinux-label.sh
kdive_label_svirt_image <directory>   # returns 0 always; no-ops off SELinux-enforcing hosts
```

Task 2 consumes nothing from this task.

### Verification

| Contract | Mode | Detail |
|---|---|---|
| A host with **no** existing rule gets one `svirt_image_t` rule added | `focused-test` | `tests/scripts/test_selinux_label.py::test_adds_rule_when_absent`; red before the helper exists (`ModuleNotFoundError`-equivalent: the script path does not exist, `subprocess` exits 127); green via `uv run python -m pytest tests/scripts/test_selinux_label.py -q` |
| A host carrying a **`virt_image_t`** rule on the same pattern has it **replaced**, leaving exactly one rule | `focused-test` | `…::test_replaces_stale_rule`; red against the current `-a`-only logic, which records no `semanage` write at all because the presence check short-circuits |
| The helper no-ops when SELinux is not enforcing | `focused-test` | `…::test_noop_when_not_enforcing` — asserts no `semanage`/`restorecon` invocation was recorded |
| The helper reports and returns 0 when `semanage` is absent | `focused-test` | `…::test_reports_missing_semanage` — asserts no rule was written and the exit status is 0 |
| An unreadable fcontext list fails soft rather than writing a duplicate rule | `focused-test` | `…::test_unreadable_list_writes_nothing` — `semanage fcontext -l` exits 1; assert neither `-a` nor `-m` was recorded |
| README prose | `task-test-not-applicable` | documentation wording with no executable consumer; `just docs-links` covers link integrity |

### Steps

1. Create `examples/local-libvirt/selinux-label.sh`:

```bash
#!/usr/bin/env bash
# Label a kdive image directory so a confined QEMU domain can use it (ADR-0639, #2424).
#
# svirt_t may read virt_image_t but may not write or map it, and the unprivileged session
# libvirt daemon never performs the dynamic relabel that closes that gap on a privileged
# daemon. So the static label has to be one the confined domain can use: svirt_image_t:s0,
# which every domain reaches by MCS dominance whatever categories libvirt draws for it.
#
# Sourced by install-host.sh and build-image.sh; sourcing it runs nothing.

kdive_label_svirt_image() {
  local directory="$1" pattern="${1}(/.*)?" rules

  command -v getenforce >/dev/null 2>&1 || return 0
  [[ "$(getenforce)" == "Enforcing" ]] || return 0

  if ! command -v semanage >/dev/null 2>&1; then
    echo "SELinux is enforcing but semanage is missing; install policycoreutils-python-utils" >&2
    echo "and label ${directory} svirt_image_t before provisioning." >&2
    return 0
  fi

  # Read the list into a variable rather than piping it into `grep -q`: the callers run under
  # `set -o pipefail`, and `grep -q` exits at the first match, which can SIGPIPE semanage and
  # make a *successful* match read as a failed pipeline — silently taking the add branch below.
  if ! rules="$(sudo semanage fcontext -l)"; then
    echo "could not read the SELinux file-context list; label ${directory} svirt_image_t" >&2
    echo "manually before provisioning." >&2
    return 0
  fi

  # `-a` fails on a pattern that already has a rule, and a host installed before ADR-0639 has
  # one of type virt_image_t. Modify when a rule exists, add when it does not, so re-running
  # the installer migrates the host instead of leaving the stale type in place. The match is a
  # literal substring (the pattern plus its trailing column separator), so a nested rule such
  # as `<dir>/local(/.*)?` cannot satisfy it.
  if [[ $rules == *"${pattern} "* ]]; then
    sudo semanage fcontext -m -t svirt_image_t "${pattern}"
  else
    sudo semanage fcontext -a -t svirt_image_t "${pattern}"
  fi
  sudo restorecon -R "${directory}"
}
```

2. In `examples/local-libvirt/install-host.sh`, replace the body of step 7b (the
   `if command -v getenforce …` block that runs `semanage fcontext -a -t virt_image_t`) with a
   call to the helper, and source the helper immediately after the script resolves `example_dir`
   (confirmed at `install-host.sh:16`; the variable is `example_dir`, not `script_dir`):

```bash
# shellcheck source=examples/local-libvirt/selinux-label.sh
source "${example_dir}/selinux-label.sh"
```

```bash
# 7b. SELinux label for the whole rootfs tree, not just the base images under local/.
#     Provisioning writes each System's overlay here and direct-kernel boot maps the baseline
#     kernel/initrd from here; svirt_t can do neither against virt_image_t (ADR-0639).
step "SELinux svirt_image_t on /var/lib/kdive/rootfs"
kdive_label_svirt_image /var/lib/kdive/rootfs
```

3. In `examples/local-libvirt/build-image.sh`, source the helper after `example_dir` is resolved
   (`build-image.sh:15`) and reduce `label_for_qemu` to a call. Note this script's `rootfs_dir` is
   `/var/lib/kdive/rootfs/local` (`build-image.sh:28`) — the nested base-image directory, not the
   tree root the installer labels — so it keeps its own rule, as it does today:

```bash
# shellcheck source=examples/local-libvirt/selinux-label.sh
source "${example_dir}/selinux-label.sh"
```

```bash
# Label the rootfs directory on SELinux-enforcing hosts only (Fedora/EL). A qcow2 published from
# a $HOME workspace can carry data_home_t, which the confined domain cannot read (ADR-0639).
label_for_qemu() {
  kdive_label_svirt_image "${rootfs_dir}"
}
```

4. Update `examples/local-libvirt/README.md`: in the `build-image.sh` table row, change
   "label the rootfs directory `virt_image_t` on SELinux hosts" to
   "label the rootfs directory `svirt_image_t` on SELinux hosts (ADR-0639)".

5. Create `tests/scripts/test_selinux_label.py`. It sources the helper in a Bash subshell with a
   stub PATH, calls the function, and asserts on a recorded command log:

```python
"""Gate tests for examples/local-libvirt/selinux-label.sh (ADR-0639, #2424).

The helper is sourced, not executed: every test drives one function call with stubbed
getenforce/semanage/restorecon/sudo on PATH, so nothing is ever labeled. Each stub appends its
argv to a log file, which is the assertion surface.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.host_capabilities import requires_bash

HELPER = Path(__file__).resolve().parents[2] / "examples" / "local-libvirt" / "selinux-label.sh"
BASH = shutil.which("bash")

pytestmark = requires_bash(4, 3, "the helper uses [[ ]] and local")

_TARGET = "/var/lib/kdive/rootfs"
# What `semanage fcontext -l` actually prints for a rule on this pattern: the regex verbatim,
# then space-padded columns. The helper matches it as a literal substring, so this is the exact
# text it has to see — not an escaped form.
_LISTED_RULE = f"{_TARGET}(/.*)?    all files    system_u:object_r:virt_image_t:s0"


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(body)
    path.chmod(0o755)


def _run(tmp_path: Path, *, enforce: str, existing: str, semanage: bool = True) -> list[str]:
    """Source the helper, call it once, and return the recorded stub invocations."""
    assert BASH is not None, "bash is required to source the helper"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "log"
    _stub(bindir, "getenforce", f"#!/bin/sh\necho {enforce}\n")
    # sudo is transparent: it records nothing and runs its argv, so the semanage/restorecon
    # stubs below see the same argv the script passed.
    _stub(bindir, "sudo", '#!/bin/sh\nexec "$@"\n')
    _stub(bindir, "semanage", f'#!/bin/sh\necho "semanage $*" >> "{log}"\n{existing}\n')
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
```

   Then the four cases. `existing` is the shell fragment the `semanage` stub runs after
   logging — it is what makes `semanage fcontext -l` print a matching rule or not:

```python
_NO_RULE = "exit 0"
_STALE_RULE = f'if [ "$1 $2" = "fcontext -l" ]; then echo "{_LISTED_RULE}"; fi\nexit 0'


def test_adds_rule_when_absent(tmp_path: Path) -> None:
    calls = _run(tmp_path, enforce="Enforcing", existing=_NO_RULE)
    assert f"semanage fcontext -a -t svirt_image_t {_TARGET}(/.*)?" in calls
    assert not any("-m -t" in call for call in calls)
    assert f"restorecon -R {_TARGET}" in calls


def test_replaces_stale_rule(tmp_path: Path) -> None:
    calls = _run(tmp_path, enforce="Enforcing", existing=_STALE_RULE)
    assert f"semanage fcontext -m -t svirt_image_t {_TARGET}(/.*)?" in calls
    assert not any("-a -t" in call for call in calls), "a second rule must not be added"
    assert f"restorecon -R {_TARGET}" in calls


def test_noop_when_not_enforcing(tmp_path: Path) -> None:
    assert _run(tmp_path, enforce="Permissive", existing=_NO_RULE) == []


def test_reports_missing_semanage(tmp_path: Path) -> None:
    assert _run(tmp_path, enforce="Enforcing", existing=_NO_RULE, semanage=False) == []


def test_unreadable_list_writes_nothing(tmp_path: Path) -> None:
    calls = _run(tmp_path, enforce="Enforcing", existing='exit 1')
    assert not any("-a -t" in call or "-m -t" in call for call in calls)
    assert not any(call.startswith("restorecon") for call in calls)
```

6. Run the focused tests red first, before step 1 exists:
   `uv run python -m pytest tests/scripts/test_selinux_label.py -q`
   Expect: collection succeeds, all five fail — `subprocess.CalledProcessError`, exit status
   127, because the function is undefined after `source` of a non-existent file.
7. After steps 1–5, run the same command. Expect: `5 passed`.
8. `just lint-shell` — expect no output and exit 0.
9. `just format && just lint && just type` — expect `All checks passed!` from ruff and no
   diagnostics from `ty`.
10. Stage, `prek run`, re-add exactly the staged paths, commit:
    `fix(local-libvirt): label kdive images svirt_image_t for session-mode sVirt`

### Acceptance criteria

- `kdive_label_svirt_image` exists in one file and is called by both scripts; neither script
  still contains a `semanage fcontext` invocation of its own.
- No `virt_image_t` literal remains in `examples/local-libvirt/`.
- The four focused tests pass; `just lint-shell`, `just lint`, `just type` are green.

---

## Task 2 — the per-disk seclabel in the rendered domain XML

**Where it fits.** Task 1 makes an enforcing host work. This states the contract Task 1 relies on
— that kdive owns the image label statically — so that a privileged daemon, or a session daemon
that later gains the capability, does not relabel a **shared backing file** with one System's MCS
categories and deny it to the next (ADR-0639).

**Modifies:** `src/kdive/providers/local_libvirt/lifecycle/xml.py`,
`tests/adversarial/test_provider_xml.py`,
`tests/providers/local_libvirt/lifecycle/test_xml.py`,
`deploy/ansible/roles/live_vm_host/tasks/main.yml`,
`deploy/ansible/inventory/group_vars/live_vm_runners.yml`

**Interfaces.** Consumes nothing from Task 1. Modifies the existing private helper

```python
def _append_root_disk(devices: ET.Element, disk_path: str) -> None
```

confirmed present at `src/kdive/providers/local_libvirt/lifecycle/xml.py:276` and called by both
`_build_baseline_domain` (used by `render_domain_xml`) and `render_customization_domain_xml`.
Nothing later relies on a new name.

### Verification

| Contract | Mode | Detail |
|---|---|---|
| The System domain's disk source carries `<seclabel model='selinux' relabel='no'/>` | `focused-test` | `tests/adversarial/test_provider_xml.py::test_disk_source_declares_static_seclabel`; red now (`./devices/disk/source/seclabel` finds nothing); green via `uv run python -m pytest tests/adversarial/test_provider_xml.py -q` |
| The customization domain's disk source carries the same element | `focused-test` | `tests/providers/local_libvirt/lifecycle/test_xml.py::test_customization_disk_source_declares_static_seclabel`; same red observation |
| The disk `<source>` still carries `file` verbatim and spawns no other element | `focused-test` | the existing `test_provider_xml.py` hostile-path assertions still pass unchanged — they assert `len(sources) == 1` and the `file` attribute, which a child element must not disturb |
| Ansible comments | `task-test-not-applicable` | comment text in a task file and a vars file; no executable consumer, and `just lint-ansible` / `just test-ansible` cover the playbooks' behavior, which is unchanged |

### Steps

1. In `src/kdive/providers/local_libvirt/lifecycle/xml.py`, replace `_append_root_disk`:

```python
def _append_root_disk(devices: ET.Element, disk_path: str) -> None:
    """Append the rootfs disk as the lone virtio disk, with its static-label contract.

    ``relabel='no'`` states that kdive owns this image's SELinux label statically
    (``svirt_image_t:s0``, applied by host preparation — ADR-0639). The unprivileged session
    daemon relabels nothing, so this changes no behavior there; it matters for a *privileged*
    daemon, which would otherwise stamp the shared backing file with the first domain's MCS
    categories and deny it to the next System that backs onto the same base image.
    """
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    source = ET.SubElement(disk, "source", file=disk_path)
    ET.SubElement(source, "seclabel", model="selinux", relabel="no")
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
```

2. Add to `tests/adversarial/test_provider_xml.py`, beside the other `render_domain_xml` tests:

```python
def test_disk_source_declares_static_seclabel() -> None:
    """The disk source declares kdive's static image label (ADR-0639, #2424)."""
    root = ET.fromstring(  # noqa: S314 - self-rendered
        render_domain_xml(
            _SYS,
            _profile(),
            disk_path="/var/lib/kdive/rootfs/base.qcow2",
            kernel_path=Path("/var/lib/kdive/rootfs/k/kernel"),
            ssh_port=22022,
        )
    )
    sources = root.findall("./devices/disk/source")
    assert len(sources) == 1
    assert sources[0].get("file") == "/var/lib/kdive/rootfs/base.qcow2"
    seclabels = sources[0].findall("./seclabel")
    assert len(seclabels) == 1
    assert seclabels[0].get("model") == "selinux"
    assert seclabels[0].get("relabel") == "no"
```

3. Add to `tests/providers/local_libvirt/lifecycle/test_xml.py`:

```python
def test_customization_disk_source_declares_static_seclabel() -> None:
    """The build domain's disk carries the same static-label contract (ADR-0639, #2424)."""
    root = ET.fromstring(
        render_customization_domain_xml(
            BID,
            arch="x86_64",
            disk_path="/var/lib/kdive/rootfs/local/base.qcow2",
            kernel_path=Path("/k/vmlinuz"),
            initrd_path=Path("/k/initrd"),
            accel="kvm",
            emulator=None,
        )
    )
    seclabels = root.findall("./devices/disk/source/seclabel")
    assert len(seclabels) == 1
    assert seclabels[0].get("model") == "selinux"
    assert seclabels[0].get("relabel") == "no"
```

4. In `deploy/ansible/roles/live_vm_host/tasks/main.yml`, change the closing clause of the
   comment at line 1929 from

   `sVirt label (the SELinux virt_image_t equivalent RHEL required).`

   to

   `sVirt label. The SELinux equivalent RHEL requires is a static svirt_image_t under the
   unprivileged session daemon (ADR-0639); virt_image_t suffices only where a privileged system
   daemon relabels each disk dynamically.`

5. In `deploy/ansible/inventory/group_vars/live_vm_runners.yml`, change
   `Both labeled virt_image_t, both traversable.` to
   `Both labeled svirt_image_t under a session daemon (ADR-0639), both traversable.`

6. Run the two focused tests red first, before step 1:
   `uv run python -m pytest tests/adversarial/test_provider_xml.py::test_disk_source_declares_static_seclabel tests/providers/local_libvirt/lifecycle/test_xml.py::test_customization_disk_source_declares_static_seclabel -q`
   Expect: `2 failed`, each on `assert len(seclabels) == 1` with `0 == 1`.
7. After step 1, run the same command. Expect: `2 passed`.
8. `just format && just lint && just type` — expect ruff `All checks passed!` and no `ty`
   diagnostics.
9. `just lint-ansible < /dev/null` — expect exit 0. (ansible-core aborts on non-blocking stdin;
   redirect it.)
10. Stage, `prek run`, re-add exactly the staged paths, commit:
    `fix(local-libvirt): declare the static image seclabel per disk`

### Acceptance criteria

- Both renderers emit exactly one `<seclabel model="selinux" relabel="no"/>` per disk `<source>`.
- The existing XML tests still pass unchanged.
- `just lint`, `just type`, `just lint-ansible` are green.

---

## Rollback and cleanup

The change is `git revert`-clean in the repository. On a host already migrated, reverting the
repository does **not** restore the old label: an operator who needs that runs
`sudo semanage fcontext -m -t virt_image_t '/var/lib/kdive/rootfs(/.*)?' && sudo restorecon -R
/var/lib/kdive/rootfs`. Note that this returns the host to the failing state #2424 describes.

## Deferrals carried into this plan

None recorded at plan time. Any `$trial-loop` deferral taken during the build is appended here
with its owning record path or tracker issue.
