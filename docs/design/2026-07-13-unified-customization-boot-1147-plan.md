# Unified Customization Boot — Implementation Plan (#1147)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `virt-customize --install/--run-command` execution path with a boot-to-self-customize mechanism, converting the rhel family; keep debian on virt-customize transiently.

**Architecture:** A family emits one ordered list of typed customization `Step`s. Two renderers consume it: an **argv renderer** (virt-customize path, byte-identical to today, used by debian) and an **offline injector + firstboot renderer** (rhel path — pure file steps applied offline via guestfish, exec steps collected into a firstboot script). For the rhel path the build repacks + normalizes the base to whole-disk-ext4 first, boots a transient `kdive-build-<uuid>` domain (KVM native / TCG foreign) to self-customize, waits a `kdive-customize-ok`/`-failed` console marker, then seals (reset cloud-init state, touch `/.autorelabel`, assert the firstboot unit removed).

**Tech Stack:** Python 3.14, `uv`, libvirt-python, libguestfs (guestfish/virt-*), pytest, ruff, ty.

**Spec:** `docs/design/2026-07-13-unified-customization-boot-1147.md`. **ADR:** `docs/adr/0345-unified-customization-boot.md` (read the Decision + Rejected alternatives; do not reopen settled choices).

## Global Constraints

- Guardrails (run before every commit): `just lint` (ruff check + format-check), `just type` (ty, whole tree src+tests), `just test` (suite excluding `live_vm`/`live_stack`). Single test: `uv run python -m pytest <path>::<name> -q`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; Google-style docstrings on non-trivial public APIs; absolute imports only (no `..`).
- ≤100 lines/function, cyclomatic complexity ≤8, ≤5 positional params.
- Every module that implements an ADR cites it in the module docstring (`ADR-0345`, plus reused ADR-0251/0272/0288/0340/0341).
- Real host-only seams (libguestfs, libvirt) are `# pragma: no cover - live_vm`; all orchestration is exercised through **injected seams** in unit tests (no libguestfs/qemu/network).
- Conventional-commit subjects ≤72 chars; end every commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Native x86_64 behavior is **not** byte-identical (it now boots to customize); debian's virt-customize **argv** stays byte-identical until the fast-follow.

## Known preconditions (new for the boot path)

- **Console readability (ADR-0223).** The customization boot's completion handshake reads the
  serial `<log>` (`read_console_log`), which raises `CONFIGURATION_ERROR` on `PermissionError` —
  the virtlogd `root:0600` wall a non-root worker under `qemu:///system` hits. For provisioning
  this only degrades evidence; here it is **load-bearing** (the read *is* the handshake), so a
  non-root `qemu:///system` worker fails every build. This fails **fast, not after a 30-minute
  boot**: `run_customization_boot`'s **first poll** reads the console within seconds of
  `createXML` (virtlogd creates the log at domain start), so the unreadable read raises
  `CONFIGURATION_ERROR` on the first iteration — no separate preflight is needed (and none can
  work: before boot the log does not exist, so `read_console_log` returns `b""`, not a permission
  error). `run_customization_boot` must let that `CategorizedError` propagate (force-off + close
  in `finally`, then re-raise). Remediation (verified live, #1147 proof record): run the reader as
  **root** (the deployment worker's identity) or use `KDIVE_LIBVIRT_URI=qemu:///session` (session
  virtlogd writes the log worker-owned). On libvirt 12 the pre-touch + a `default:user:<worker>:r`
  ACL do **not** work — virtlogd unlinks+recreates the log `root:0600` and the `0600` create mode
  zeroes the ACL mask, masking the named-user entry; a non-root reader cannot then re-permission a
  root-owned file. (This dev host runs `qemu:///system`; the build-fs live proof used
  `qemu:///session`.)
- **Base image shape.** Boot-path customization requires a **cloud-init-enabled** base with no
  `network:{config:disabled}` drop-in (the injected `99-kdive.cfg` is the primary network config;
  `/etc/cloud/cloud-init.disabled` is removed offline pre-boot). Shipped Fedora Cloud Base rows
  satisfy this.

---

### Task 1: Typed customization Step model

**Files:**
- Create: `src/kdive/images/families/steps.py`
- Test: `tests/images/families/test_steps.py`

**Interfaces:**
- Produces: frozen-slots dataclasses `Mkdir(path: str)`, `WriteFile(path: str, content: str)`, `StageFile(path: str, content: str)`, `UploadFile(host_src: Path, dest: str, mode: str | None = None)`, `InstallPackages(names: tuple[str, ...])`, `RunCommand(sh: str)`; union `type Step = Mkdir | WriteFile | StageFile | UploadFile | InstallPackages | RunCommand`.

- [ ] **Step 1: Write the failing test**

```python
# tests/images/families/test_steps.py
from pathlib import Path
from kdive.images.families.steps import (
    InstallPackages, Mkdir, RunCommand, StageFile, UploadFile, WriteFile,
)

def test_steps_are_frozen_value_objects():
    assert Mkdir("/d").path == "/d"
    assert WriteFile("/f", "x").content == "x"
    assert StageFile("/f", "y").content == "y"
    assert UploadFile(Path("/h"), "/g", mode="0755").mode == "0755"
    assert InstallPackages(("a", "b")).names == ("a", "b")
    assert RunCommand("echo hi").sh == "echo hi"

def test_uploadfile_mode_defaults_none():
    assert UploadFile(Path("/h"), "/g").mode is None
```

- [ ] **Step 2: Run test — expect ImportError/FAIL.** `uv run python -m pytest tests/images/families/test_steps.py -q`
- [ ] **Step 3: Implement `steps.py`** — six `@dataclass(frozen=True, slots=True)` classes with the fields above, the `type Step = …` union, a module docstring citing ADR-0345, `from __future__ import annotations`.
- [ ] **Step 4: Run test — expect PASS.**
- [ ] **Step 5: `just lint && just type`, then commit** `git add src/kdive/images/families/steps.py tests/images/families/test_steps.py` → `feat(images): typed customization Step model (#1147)`.

---

### Task 2: Argv renderer + convert families to `customize_steps` (no behavior change)

This is the "one list, two renderers" refactor. **Deliverable:** the shared `_fedora_customize` primitives return `Step`s; rhel and debian expose `customize_steps`; `render_argv` reproduces **today's exact virt-customize argv**; all existing `test_rhel.py` / `test_debian.py` / `test_fedora_customize.py` / `test_rootfs_build.py` assertions still pass (the virt-customize path is unchanged).

**Files:**
- Create: `src/kdive/images/families/renderers.py`
- Modify: `src/kdive/images/families/_fedora_customize.py` (primitives return `list[Step]`), `src/kdive/images/families/base.py` (Protocol), `src/kdive/images/families/rhel.py`, `src/kdive/images/families/debian.py`
- Modify: `src/kdive/providers/local_libvirt/rootfs_build.py` (`_customize` calls `render_argv(family.customize_steps(ctx), cleanup=…)` on the virt-customize path)
- Test: `tests/images/families/test_renderers.py`; update `tests/images/families/test_rhel.py`, `tests/images/families/test_debian.py` to call `render_argv(family.customize_steps(ctx))` where they currently call `customize_argv`.

**Interfaces:**
- Produces: `render_argv(steps: list[Step], *, cleanup: list[Path]) -> list[str]`. `FamilyCustomizer` Protocol gains `customize_steps(self, ctx: CustomizeContext) -> list[Step]` and `customize_via: str` (`"boot"` for rhel, `"virt_customize"` for debian); the old `customize_argv` is **removed from the Protocol**. debian keeps a module-level `customize_argv(ctx)` helper implemented as `render_argv(self.customize_steps(ctx), cleanup=ctx.cleanup)` (the build plane's virt-customize path calls `render_argv` directly, so the family need not expose `customize_argv` at all — call `render_argv(family.customize_steps(ctx), cleanup=ctx.cleanup)`).
- Consumes: `Step` (Task 1).

**Argv mapping (must reproduce today's bytes):** `Mkdir(p)`→`["--mkdir", p]`; `WriteFile(p,c)`→`["--write", f"{p}:{c}"]`; `StageFile(p,c)`→stage `c` to a `NamedTemporaryFile` appended to `cleanup`, then `["--upload", f"{tmp}:{p}"]` (exactly `_staged_upload` today); `UploadFile(h,d,mode)`→`["--upload", f"{h}:{d}"]` then `["--run-command", f"chmod {mode} {d}"]` if `mode` (matches the drgn-helper's `--run-command chmod 0755`); `InstallPackages(names)`→`["--install", ",".join(names)]`; `RunCommand(s)`→`["--run-command", s]`.

- [ ] **Step 1: Write the failing renderer test**

```python
# tests/images/families/test_renderers.py
from pathlib import Path
from kdive.images.families.renderers import render_argv
from kdive.images.families.steps import (
    InstallPackages, Mkdir, RunCommand, StageFile, UploadFile, WriteFile,
)

def test_render_argv_maps_each_step():
    cleanup: list[Path] = []
    argv = render_argv([
        Mkdir("/seed"),
        InstallPackages(("drgn", "kexec-tools")),
        RunCommand("systemctl enable kdump.service"),
        WriteFile("/etc/machine-id", "0a1b"),
        UploadFile(Path("/h/u.service"), "/etc/systemd/system/kdive-ready.service"),
    ], cleanup=cleanup)
    assert argv == [
        "--mkdir", "/seed",
        "--install", "drgn,kexec-tools",
        "--run-command", "systemctl enable kdump.service",
        "--write", "/etc/machine-id:0a1b",
        "--upload", "/h/u.service:/etc/systemd/system/kdive-ready.service",
    ]

def test_stagefile_uploads_a_tempfile_with_content(tmp_path):
    cleanup: list[Path] = []
    argv = render_argv([StageFile("/etc/cloud/x.cfg", "datasource_list: [ NoCloud ]\n")],
                       cleanup=cleanup)
    assert argv[0] == "--upload"
    src, _, dest = argv[1].partition(":")
    assert dest == "/etc/cloud/x.cfg"
    assert Path(src).read_text() == "datasource_list: [ NoCloud ]\n"
    assert cleanup == [Path(src)]

def test_uploadfile_mode_appends_chmod():
    argv = render_argv([UploadFile(Path("/h/k"), "/usr/local/sbin/k", mode="0755")], cleanup=[])
    assert argv == ["--upload", "/h/k:/usr/local/sbin/k", "--run-command", "chmod 0755 /usr/local/sbin/k"]
```

- [ ] **Step 2: Run — FAIL (no `render_argv`).**
- [ ] **Step 3: Implement `render_argv`** per the mapping. Keep it ≤100 lines; a per-kind dispatch (`match step:`).
- [ ] **Step 4: Run the renderer test — PASS.**
- [ ] **Step 5: Refactor `_fedora_customize.py` primitives to return `list[Step]`.** `cloud_init_first_boot_args(ctx)` → returns Steps: `InstallPackages(("cloud-init",))` when `not ctx.is_cloud_image`; `Mkdir(NOCLOUD_SEED_DIR)`; `StageFile(KDIVE_CLOUD_CFG_PATH, KDIVE_CLOUD_CFG_CONTENT)`; `StageFile(f"{NOCLOUD_SEED_DIR}/meta-data", _NOCLOUD_META_DATA)`; `StageFile(f"{NOCLOUD_SEED_DIR}/user-data", _NOCLOUD_USER_DATA)`; `RunCommand(_STRIP_NET_DISABLE_CMD)`; `RunCommand("rm -f /etc/cloud/cloud-init.disabled")`; `WriteFile("/etc/machine-id", SEED_MACHINE_ID)`. Rename it `cloud_init_first_boot_steps` (drop the `cleanup` param — `StageFile` needs no host tempfile until argv-render time). `drgn_helper_args`→`drgn_helper_steps` returning `[UploadFile(drgn_helper_source(), DRGN_HELPER_GUEST_PATH, mode="0755")]` — **preserve the existing `is_file()` fail-loud guard**: `drgn_helper_steps` still raises `CategorizedError(CONFIGURATION_ERROR)` when `drgn_helper_source()` is not a readable file, before returning the `UploadFile` (do not defer a missing helper to guestfish runtime). Add a regression test asserting a missing helper raises. `makedumpfile_version_marker_args`/`drgn_version_marker_args`→`_steps` returning `[RunCommand(<the same shell string>)]`. `debug_image_args`→`debug_image_steps`. `readiness` unit stays a rendered file uploaded via `UploadFile(readiness_unit_path, f"/etc/systemd/system/{READINESS_MARKER}.service")`.
- [ ] **Step 6: Convert `rhel.py`.** Replace `customize_argv` with `customize_steps(ctx) -> list[Step]` building the same sequence with Steps (EPEL `RunCommand`, `InstallPackages(ctx.packages)`, `RunCommand("systemctl enable sshd.service")` when debug, the kdump `RunCommand`+`WriteFile(KDUMP_SYSCTL_PATH, KDUMP_SYSCTL_CONTENT)`+`RunCommand(KDUMP_FINAL_ACTION_CMD)`, `cloud_init_first_boot_steps(ctx)`, `debug_image_steps`, marker steps, `UploadFile(readiness_unit)`, `RunCommand(_SELINUX_PERMISSIVE_SED)`). Add `customize_via = "boot"`. Keep `packages`/`capabilities`/`normalize` unchanged.
- [ ] **Step 7: Convert `debian.py`** analogously to `customize_steps`; add `customize_via = "virt_customize"`.
- [ ] **Step 8: Update `base.py` Protocol** — `customize_steps` + `customize_via`; drop `customize_argv`.
- [ ] **Step 9: Update `rootfs_build.py` `_customize`** — build the readiness unit as today, build `ctx`, then `argv = render_argv(family.customize_steps(ctx), cleanup=cleanup)` and `self._tools.customize(scratch, argv)`. (This is still the virt-customize path; Task 9 adds the boot dispatch.)
- [ ] **Step 10: Update `test_rhel.py` / `test_debian.py`** to assert on `render_argv(fam.customize_steps(ctx), cleanup=[])` — the argv strings must be **unchanged** from the current assertions (byte-identical). This is the regression guard.
- [ ] **Step 11: Run** `just test` (families + rootfs_build suites), `just lint`, `just type`. All existing argv assertions must pass unchanged.
- [ ] **Step 12: Commit** the modified files → `refactor(images): families emit typed Steps; render_argv reproduces argv (#1147)`.

---

### Task 3: Firstboot renderer (offline file steps + firstboot script)

**Files:**
- Modify: `src/kdive/images/families/renderers.py`
- Test: `tests/images/families/test_renderers.py`

**Interfaces:**
- Produces (all in `renderers.py`, importing the shared constants below from `customization_boot.py` — Task 5): `partition_steps(steps: list[Step]) -> tuple[list[Step], list[Step]]` (file-ops = `Mkdir`/`WriteFile`/`StageFile`/`UploadFile`; exec-ops = `InstallPackages`/`RunCommand`, order preserved); `render_firstboot_script(exec_steps: list[Step], *, console_device: str, unit_name: str, script_path: str, ok_marker: str, fail_marker: str) -> str`; **`render_firstboot_unit(*, script_path: str) -> str`** (the systemd unit body).
- Shared constants (define in Task 5's `customization_boot.py`, import here + in Task 8/9): `CUSTOMIZE_UNIT = "kdive-customize.service"`, `CUSTOMIZE_SCRIPT_PATH = "/usr/local/sbin/kdive-customize"`. Both `render_firstboot_script` (self-`rm` targets) and `inject_offline` (write locations) MUST use the same two constants — a path skew leaves the unit `ExecStart`-ing a missing script (no marker → timeout) or mis-anchors Task 8's unit-removed assert.

**Firstboot script contract** (`render_firstboot_script` returns; `<script>` = `CUSTOMIZE_SCRIPT_PATH`, `<unit>` = `CUSTOMIZE_UNIT`):
```sh
#!/bin/sh
set -e
trap 'echo <fail_marker> > /dev/<console_device>; sync; systemctl poweroff' EXIT
dnf -y install <pkgs>            # one line per InstallPackages
<RunCommand sh>                  # one line per RunCommand, in order
rm -f /etc/systemd/system/<unit> /etc/systemd/system/multi-user.target.wants/<unit> <script>
trap - EXIT
echo <ok_marker> > /dev/<console_device>
sync
systemctl poweroff
```
The self-removal `rm`s the unit file, its **offline-created** `multi-user.target.wants` symlink (see below), and the script. The `trap … EXIT` fires the fail marker on any non-zero exit (`set -e`) OR early exit; the success path clears the trap (`trap - EXIT`) before echoing `ok`. `InstallPackages` renders `dnf -y install a b c` (space-separated).

**Firstboot unit contract** (`render_firstboot_unit` returns):
```ini
[Unit]
Description=kdive one-shot build customization
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=<script_path>
[Install]
WantedBy=multi-user.target
```
This unit is the **bootstrap** — there is no in-guest `systemctl enable` for it (that would run in the very firstboot it is trying to trigger), so `inject_offline` (Task 9) enables it **offline** via a guestfish symlink (below).

- [ ] **Step 1: Write failing tests**

```python
from kdive.images.families.renderers import (
    partition_steps, render_firstboot_script, render_firstboot_unit,
)
from kdive.images.families.steps import InstallPackages, Mkdir, RunCommand, WriteFile

def test_partition_separates_file_and_exec_ops():
    steps = [Mkdir("/d"), InstallPackages(("a",)), WriteFile("/f", "x"), RunCommand("y")]
    file_ops, exec_ops = partition_steps(steps)
    assert file_ops == [Mkdir("/d"), WriteFile("/f", "x")]
    assert exec_ops == [InstallPackages(("a",)), RunCommand("y")]

def test_firstboot_script_shape():
    s = render_firstboot_script(
        [InstallPackages(("drgn", "kexec-tools")), RunCommand("systemctl enable kdump.service")],
        console_device="hvc0", unit_name="kdive-customize.service",
        script_path="/usr/local/sbin/kdive-customize",
        ok_marker="kdive-customize-ok", fail_marker="kdive-customize-failed")
    assert s.startswith("#!/bin/sh\nset -e\n")
    assert "trap 'echo kdive-customize-failed > /dev/hvc0" in s
    assert "dnf -y install drgn kexec-tools" in s
    assert "systemctl enable kdump.service" in s      # an exec-step, runs in-guest
    # self-removal targets the unit, its wants-symlink, and the script
    assert "rm -f /etc/systemd/system/kdive-customize.service" in s
    assert "multi-user.target.wants/kdive-customize.service" in s
    assert "/usr/local/sbin/kdive-customize" in s
    assert "trap - EXIT" in s
    assert s.rstrip().endswith("systemctl poweroff")
    assert "echo kdive-customize-ok > /dev/hvc0" in s

def test_firstboot_unit_orders_after_network_and_wants_multiuser():
    u = render_firstboot_unit(script_path="/usr/local/sbin/kdive-customize")
    assert "After=network-online.target" in u
    assert "Wants=network-online.target" in u
    assert "Type=oneshot" in u
    assert "ExecStart=/usr/local/sbin/kdive-customize" in u
    assert "WantedBy=multi-user.target" in u
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement `partition_steps` + `render_firstboot_script` + `render_firstboot_unit`** (import `CUSTOMIZE_UNIT`/`CUSTOMIZE_SCRIPT_PATH` from Task 5's module — land those two constants in Task 5 first, or as a tiny prerequisite step here).
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: `just lint && just type`; commit** → `feat(images): firstboot renderer (offline file ops + exec script) (#1147)`.

---

### Task 4: Build-domain identity + customization domain XML

**Files:**
- Modify: `src/kdive/providers/shared/runtime_paths.py` (add `build_domain_name`)
- Modify: `src/kdive/providers/local_libvirt/lifecycle/xml.py` (add `render_customization_domain_xml` + `_append_egress_nic`)
- Test: `tests/providers/shared/test_runtime_paths.py`, `tests/providers/local_libvirt/lifecycle/test_xml.py` (match existing test module for xml)

**Interfaces:**
- Produces: `build_domain_name(build_id: UUID) -> str` returning `f"kdive-build-{build_id}"`. `render_customization_domain_xml(build_id: UUID, *, arch: str, disk_path: str, kernel_path: Path, initrd_path: Path | None, accel: str, emulator: str | None, memory_mb: int = 2048, vcpu: int = 2) -> str` — a minimal domain reusing the low-level element helpers (`_append_guest_cpu`, `_append_os`, `_append_direct_kernel`, `_append_emulator`, `_append_root_disk`, `_append_serial_console`) with: `<name>` = `build_domain_name(build_id)`, `<uuid>` = `build_id`, `<on_reboot>destroy</on_reboot>`, an egress NIC via `_append_egress_nic` (raw `-netdev user,id=kdivebuild,restrict=off` + `virtio-net-pci` on the qemu commandline — no hostfwd, no gdbstub, no ssh forward, no preserve-on-crash). **`_append_egress_nic` must replicate the q35 NIC-slot pin** from `_append_ssh_forward` (append `,addr=0x10` to the `virtio-net-pci` device when `arch_traits(arch).pin_nic_slot` is true) — without it an x86_64/q35 build domain fails `define`/`start` with a PCI slot-1 collision (xml.py:349-358). Console log path is `console_log_path(build_id)` (reused). `_append_serial_console` derives the console path from the UUID, so pass `build_id` — no change needed there.

**Note (reconciled with the ADR):** this is a **dedicated minimal renderer**, not an extension of `render_domain_xml` (which requires a `ProvisioningProfile`, an `ssh_port`, and renders the System SSH forward / gdbstub). `domain_name_for` stays System-only; `build_domain_name` is new. Reconciler-safety holds because `system_id_from_domain_name` already returns `None` for the `kdive-build-` form. The spec/ADR text is updated to say "a dedicated renderer is added" (was "extend render_domain_xml").

- [ ] **Step 1: Write failing tests**

```python
from uuid import UUID
from pathlib import Path
import xml.etree.ElementTree as ET
from kdive.providers.shared.runtime_paths import build_domain_name, system_id_from_domain_name
from kdive.providers.local_libvirt.lifecycle.xml import render_customization_domain_xml

BID = UUID("11111111-2222-3333-4444-555555555555")

def test_build_domain_name_is_reconciler_safe():
    name = build_domain_name(BID)
    assert name == f"kdive-build-{BID}"
    assert system_id_from_domain_name(name) is None  # never reaped as a System

def test_customization_domain_pseries_tcg():
    xml = render_customization_domain_xml(
        BID, arch="ppc64le", disk_path="/d.qcow2",
        kernel_path=Path("/k/vmlinuz"), initrd_path=Path("/k/initrd"),
        accel="tcg", emulator="/usr/bin/qemu-system-ppc64")
    root = ET.fromstring(xml)
    assert root.get("type") == "qemu"
    assert root.findtext("name") == f"kdive-build-{BID}"
    assert root.findtext("on_reboot") == "destroy"
    assert root.find("cpu") is None                      # TCG: no <cpu>
    assert root.findtext("devices/emulator") == "/usr/bin/qemu-system-ppc64"
    assert "root=/dev/vda console=hvc0 rw" in root.findtext("os/cmdline")
    # egress NIC present with restrict=off (namespaced qemu:arg)
    assert any("restrict=off" in (a.get("value") or "") for a in root.iter())

def test_customization_domain_x86_kvm_has_no_emulator_and_egress_on():
    xml = render_customization_domain_xml(
        BID, arch="x86_64", disk_path="/d.qcow2",
        kernel_path=Path("/k/vmlinuz"), initrd_path=Path("/k/initrd"),
        accel="kvm", emulator=None)
    root = ET.fromstring(xml)
    assert root.get("type") == "kvm"
    assert root.find("devices/emulator") is None
    vals = [a.get("value") or "" for a in root.iter()]
    assert any("restrict=off" in v for v in vals)
    assert any("virtio-net-pci" in v and "addr=0x10" in v for v in vals)  # q35 slot pin
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** `build_domain_name`, `render_customization_domain_xml`, `_append_egress_nic`. Reuse `_ensure_namespaces_registered()`, `arch_traits(arch)` for `console_device`/`machine`/`kvm_cpu_mode`/`emit_acpi_features`/`pin_nic_slot`. Emit `<on_reboot>destroy</on_reboot>` as a child of `<domain>`. Keep `_append_crash_capture_features` gating (x86-only) as in `_build_baseline_domain`.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: `just lint && just type`; commit** → `feat(local-libvirt): render minimal customization-boot domain XML (#1147)`.

---

### Task 5: Customization-boot console classifier (subtractive crash set)

**Files:**
- Create: `src/kdive/providers/local_libvirt/lifecycle/rootfs/customization_boot.py`
- Test: `tests/providers/local_libvirt/lifecycle/rootfs/test_customization_boot.py`

**Interfaces:**
- Produces: `OK_MARKER = "kdive-customize-ok"`, `FAIL_MARKER = "kdive-customize-failed"`, `CUSTOMIZE_UNIT = "kdive-customize.service"`, `CUSTOMIZE_SCRIPT_PATH = "/usr/local/sbin/kdive-customize"` (the shared constants Tasks 3/8/9 import from here); `class CustomizeVerdict(StrEnum): OK/FAILED/PENDING`; `classify_customization_console(data: bytes) -> CustomizeVerdict`.

**Classifier rules (order matters):** OK if the `OK_MARKER` line is present; else FAILED if the `FAIL_MARKER` line is present; else FAILED if a **genuine-fault** pattern is present; else PENDING. Genuine-fault regex = the provision `_CRASH_SIGNATURE` **minus** the two benign-under-TCG watchdog patterns: drop `detected stall`, and change the `BUG:` alternative to `(?<![A-Za-z])BUG:(?! soft lockup)` so `watchdog: BUG: soft lockup` no longer matches while a real `BUG: unable to handle …` still does. Keep `Kernel panic`, `Oops:`, `general protection fault`, `unable to handle kernel`, `KASAN:`, `KFENCE:`.

- [ ] **Step 1: Write failing tests**

```python
from kdive.providers.local_libvirt.lifecycle.rootfs.customization_boot import (
    CustomizeVerdict, classify_customization_console as C,
)

def test_ok_marker_wins():
    assert C(b"...\nkdive-customize-ok\n") is CustomizeVerdict.OK

def test_fail_marker():
    assert C(b"dnf: No match\nkdive-customize-failed\n") is CustomizeVerdict.FAILED

def test_genuine_oops_fails():
    assert C(b"Oops: 0000 [#1] SMP\n") is CustomizeVerdict.FAILED

def test_benign_tcg_stall_is_pending():
    assert C(b"rcu: INFO: rcu_sched detected stalls on CPUs\n") is CustomizeVerdict.PENDING
    assert C(b"watchdog: BUG: soft lockup - CPU#0 stuck for 22s!\n") is CustomizeVerdict.PENDING

def test_real_bug_still_fails():
    assert C(b"BUG: unable to handle kernel paging request\n") is CustomizeVerdict.FAILED

def test_pending_when_quiet():
    assert C(b"[  ok  ] Started systemd-logind\n") is CustomizeVerdict.PENDING
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** the markers, enum, `_GENUINE_FAULT` regex, and `classify_customization_console` (whole-line marker match like `classify_console`; fault search on the full text).
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: `just lint && just type`; commit** → `feat(local-libvirt): customization-boot console classifier (#1147)`.

---

### Task 6: Customization-boot deadline setting

**Files:**
- Modify: `src/kdive/providers/local_libvirt/settings.py`
- Test: `tests/providers/local_libvirt/test_settings.py` (or the settings test module in use)

**Interfaces:**
- Produces: `LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S = Setting(name="KDIVE_LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S", parse=_parse_positive_int, default="1800", group="local-libvirt", processes=_RT, help=…, suggest=…)`; append to `SETTINGS`. `1800` (30 min) native-KVM base window; the customization-boot poll multiplies it by `tcg_deadline_multiplier(accel)`. Doc the value is a live-proof-pinned default absorbing mirror/network fetch variance (spec §Deadline). **`1800` is a provisional default; Task 10 re-pins it to the measured native-KVM customization time × 3.** Land it now so Task 7 can consume it; Task 10 updates it (and the help text) if the measurement warrants.

- [ ] **Step 1: Write failing test** — assert the setting resolves to `1800` by default and rejects `<= 0`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** the setting + a `_parse_positive_int` (raise `ValueError` on `<= 0`).
- [ ] **Step 4: Run — PASS.** Also run the config-doc generation guard if the repo has one (`just docs-check` / the generated env-var doc) — a new `KDIVE_*` setting often must be registered in a generated config reference; if a test like `test_no_adr_leak`/config-doc drift fails, regenerate the doc per its failure message.
- [ ] **Step 5: `just lint && just type`; commit** → `feat(local-libvirt): customization-boot window setting (#1147)`.

---

### Task 7: Boot-customize-seal orchestration (injected seams)

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/rootfs/customization_boot.py`
- Test: `tests/providers/local_libvirt/lifecycle/rootfs/test_customization_boot.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class CustomizationBootSeams:
      open_conn: Callable[[], _Conn]                 # returns a libvirt conn held open for the call
      create_transient: Callable[[_Conn, str], _Domain]   # createXML(xml, AUTODESTROY)
      read_console: Callable[[UUID], bytes]          # read_console_log(console_log_path(build_id))
      domain_settled: Callable[[UUID], bool]         # readiness._domain_exit_probe: shut off OR crashed
      sleep: Callable[[float], None]
      window_polls: Callable[[str], int]             # base window / _POLL_INTERVAL_S, scaled by accel
  def run_customization_boot(build_id: UUID, domain_xml: str, *, accel: str, seams: CustomizationBootSeams) -> None
  ```
  Returns `None` on success; raises `CategorizedError(PROVISIONING_FAILURE)` with `details["console_tail"]` on a `kdive-customize-failed`/genuine-fault/settled-without-ok verdict, and `CategorizedError(BOOT_TIMEOUT)` on window exhaustion (also with the tail).
- Consumes: `classify_customization_console` (Task 5), `tcg_deadline_multiplier` (deadlines.py), `redacted_console_tail` (or a plain bounded tail of `read_console` output — a build has no `RequestContext`/secret registry, so use a local bounded-tail helper on the console bytes rather than `redacted_console_tail`).

**Control flow (the load-bearing part):** define `_POLL_INTERVAL_S = 10.0` in this module (coarser than the boot readiness 5.0s — a customization boot is minutes-to-tens-of-minutes; `window_polls(accel) = ceil((CUSTOMIZATION_BOOT_WINDOW_S / _POLL_INTERVAL_S) * tcg_deadline_multiplier(accel))`). Open ONE connection; `create_transient(conn, domain_xml)`; loop up to `window_polls(accel)` times: read console, classify; OK→break to seal; FAILED (fail-marker OR genuine-fault, both from `classify_customization_console`)→raise PROVISIONING_FAILURE + tail; else if `domain_settled(build_id)` (shut off **or** crashed, via the crashed-aware `readiness._domain_exit_probe`)→re-read+classify, OK→break else raise (**settled without ok-marker**, which subsumes a crash: no pvpanic is rendered, so a panic ends as shut-off/crashed and is caught here — there is no separate "crashed domstate" path); else `sleep(_POLL_INTERVAL_S)`. On loop exhaustion→raise BOOT_TIMEOUT + tail. In a `finally`, force-off the domain (destroy if active) and **only then** close the connection (closing triggers AUTODESTROY cleanup). The connection object stays open for the whole function — never opened/closed per poll.

- [ ] **Step 1: Write failing tests** (fake seams; a `FakeConn` records `closed`, a `FakeDomain` records `destroyed`):

```python
# success: console yields ok on the 2nd poll
def test_success_seals_and_holds_conn_open_until_end():
    events = []
    conn = FakeConn(events)
    reads = iter([b"booting...\n", b"booting...\nkdive-customize-ok\n"])
    seams = CustomizationBootSeams(
        open_conn=lambda: conn,
        create_transient=lambda c, x: FakeDomain(events),
        read_console=lambda _bid: next(reads),
        domain_settled=lambda _bid: False,
        sleep=lambda _s: events.append("sleep"),
        window_polls=lambda _a: 10)
    run_customization_boot(BID, "<domain/>", accel="tcg", seams=seams)
    assert conn.closed_after_force_off is True   # conn not closed before force-off

def test_fail_marker_raises_provisioning_failure_with_tail():
    seams = _seams_reading(b"dnf error: nothing provides libfoo\nkdive-customize-failed\n")
    with pytest.raises(CategorizedError) as ei:
        run_customization_boot(BID, "<domain/>", accel="tcg", seams=seams)
    assert ei.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert "libfoo" in ei.value.details["console_tail"]

def test_genuine_fault_raises():
    seams = _seams_reading(b"Oops: 0000 [#1]\n")
    with pytest.raises(CategorizedError):
        run_customization_boot(BID, "<domain/>", accel="tcg", seams=seams)

def test_settled_without_ok_marker_fails():
    seams = _seams(read=b"partial\n", settled=True)
    with pytest.raises(CategorizedError):
        run_customization_boot(BID, "<domain/>", accel="tcg", seams=seams)

def test_window_exhaustion_is_boot_timeout():
    seams = _seams(read=b"still booting\n", settled=False, polls=2)
    with pytest.raises(CategorizedError) as ei:
        run_customization_boot(BID, "<domain/>", accel="tcg", seams=seams)
    assert ei.value.category is ErrorCategory.BOOT_TIMEOUT

def test_unreadable_console_propagates_and_tears_down(monkeypatch):
    # ADR-0223 root:0600 wall: the first read raises CONFIGURATION_ERROR; it must
    # propagate (not be swallowed) AND the domain must still be force-off in finally.
    domain = FakeDomain(events := [])
    def raise_perm(_bid):
        raise CategorizedError("failed to read console log",
                               category=ErrorCategory.CONFIGURATION_ERROR)
    seams = _seams_custom(read_console=raise_perm, domain=domain)
    with pytest.raises(CategorizedError) as ei:
        run_customization_boot(BID, "<domain/>", accel="tcg", seams=seams)
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert domain.destroyed is True   # finally force-off ran despite the raise
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** `CustomizationBootSeams`, `run_customization_boot`, `_from_env()` classmethod/factory wiring the real seams (`# pragma: no cover - live_vm`): `open_conn` = `libvirt.open(config.require(LIBVIRT_URI))`; `create_transient` = `conn.createXML(xml, libvirt.VIR_DOMAIN_START_AUTODESTROY)`; `read_console` = `lambda bid: read_console_log(console_log_path(bid))`; `domain_settled` via the existing `retrieve.py` domstate-settled probe pattern; `window_polls` = `lambda accel: ceil((CUSTOMIZATION_BOOT_WINDOW_S / POLL_INTERVAL) * tcg_deadline_multiplier(accel))`. Keep `run_customization_boot` ≤100 lines / CC ≤8 (extract the poll body).
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: `just lint && just type`; commit** → `feat(local-libvirt): boot-customize-seal orchestration (#1147)`.

---

### Task 8: Offline seal ops (cloud-init reset, autorelabel, unit-removed assert)

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/rootfs/customization_boot.py` (or a sibling `customization_seal.py` if Task 7's module is near the size limit — keep files focused)
- Test: same test module

**Interfaces:**
- Produces: `seal_customized_image(qcow2: Path, *, unit_name: str, selinux: bool, run_guestfish: GuestfishRunner) -> None` where `GuestfishRunner = Callable[[Path, str], None]` runs a guestfish script string against the image (injected). It runs one guestfish script that: `rm-rf /var/lib/cloud/instances /var/lib/cloud/instance /var/lib/cloud/sem /var/lib/cloud/data`; if `selinux`, `touch /.autorelabel`; and asserts the firstboot unit is gone (`-- is-file /etc/systemd/system/<unit_name>` must be false → raise `PROVISIONING_FAILURE` if present, "customization firstboot unit was not self-removed; the build boot did not complete cleanly").

- [ ] **Step 1: Write failing tests** — a fake `run_guestfish` records the script; assert it contains the cloud-init `rm-rf`, the `/.autorelabel` touch iff `selinux=True`, and that a fake reporting the unit still present raises `PROVISIONING_FAILURE`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** `seal_customized_image` + the real guestfish runner (`# pragma: no cover - live_vm`) reusing `run_guestfs_tool` from `images/planes/_build_common`.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: `just lint && just type`; commit** → `feat(local-libvirt): offline seal (cloud-init reset, autorelabel, unit assert) (#1147)`.

---

### Task 9: Wire the boot path into the build plane

**Files:**
- Modify: `src/kdive/providers/local_libvirt/rootfs_build.py`
- Test: `tests/providers/local_libvirt/test_rootfs_build.py`

**Interfaces:**
- `RootfsBuildTools` gains injected boot seams: `inject_offline: Callable[[Path, list[Step], str, str], None]` (args: qcow2, file_ops, firstboot_script, firstboot_unit). Offline via guestfish it: applies the file-ops; writes `firstboot_script` to `CUSTOMIZE_SCRIPT_PATH` mode `0755`; writes `firstboot_unit` to `/etc/systemd/system/{CUSTOMIZE_UNIT}`; **enables the bootstrap unit offline** by creating the symlink `/etc/systemd/system/multi-user.target.wants/{CUSTOMIZE_UNIT}` → `/etc/systemd/system/{CUSTOMIZE_UNIT}` (guestfish `ln-s` — arch-safe, no in-guest `systemctl`); and `rm -f /etc/cloud/cloud-init.disabled` (network precondition). The build plane renders `firstboot_unit = render_firstboot_unit(script_path=CUSTOMIZE_SCRIPT_PATH)` and `firstboot_script = render_firstboot_script(exec, …, script_path=CUSTOMIZE_SCRIPT_PATH)`. `run_customization_boot: Callable[..., None]` (Task 7 `run_customization_boot`, defaulting to the `_from_env` seams), `seal_customized_image: Callable[..., None]` (Task 8), `extract_baseline_kernel: ExtractBaselineKernel` (reuse the provisioning seam type to get kernel+initrd from `staged`), and **`resolve_accel: Callable[[str], tuple[str, str | None]]`** — resolves `(accel, emulator)` for `spec.arch`. Its default (`# pragma: no cover - live_vm`) wraps the live `conn.getCapabilities()` → `parse_guest_arches(caps_xml, arch_traits.SUPPORTED_ARCHES)` → `resolve_accel_emulator(guest_arches, arch)` path (mirroring `provisioning.py`), returning `("kvm", None)` fail-open on empty guest_arches (exactly `resolve_accel_emulator`'s documented default). Defaults wire the real implementations.
- `build()` dispatches on `family.customize_via`:
  - `"virt_customize"` (debian): the **current** order (acquire → customize(argv) → repack → normalize → probes-from-scratch → publish), unchanged.
  - `"boot"` (rhel): acquire → **repack `staged`** → **normalize `staged` WITHOUT `/.autorelabel`** (see note) → `steps = family.customize_steps(ctx)`; `file_ops, exec = partition_steps(steps)`; `script = render_firstboot_script(exec, console_device=arch_traits(spec.arch).console_device, unit_name=CUSTOMIZE_UNIT, script_path=CUSTOMIZE_SCRIPT_PATH, ok_marker=OK_MARKER, fail_marker=FAIL_MARKER)`; `unit = render_firstboot_unit(script_path=CUSTOMIZE_SCRIPT_PATH)`; `inject_offline(staged, file_ops, script, unit)`; extract baseline kernel from `staged`; `accel, emulator = self._tools.resolve_accel(spec.arch)`; `xml = render_customization_domain_xml(build_id, arch=spec.arch, disk_path=str(staged), kernel_path=…, initrd_path=…, accel=accel, emulator=emulator)`; `run_customization_boot(build_id, xml, accel=accel, seams=…)`; `seal_customized_image(staged, unit_name=_CUSTOMIZE_UNIT, selinux=(family.guest_mac.startswith("selinux")))`; **probes read `staged`**; `verify_cloud_init(staged)`; publish.

**Network precondition (offline).** `inject_offline` removes `/etc/cloud/cloud-init.disabled` **offline** (guestfish `rm-f`, arch-safe) *before* the boot — that file disables cloud-init wholesale, so deferring its removal to a post-`network-online.target` firstboot would deadlock (no cloud-init → no DHCP → firstboot never runs). Boot-path customization requires a **cloud-init-enabled base with no `network:{config:disabled}` drop-in**; the injected `99-kdive.cfg` (`StageFile`) is the primary network config (highest-numbered drop-in wins). The `_STRIP_NET_DISABLE_CMD` `RunCommand` stays best-effort in the firstboot (a no-op on compliant bases). Shipped Fedora rows satisfy this (see Known preconditions).

**`normalize` change:** the boot path must NOT touch `/.autorelabel` before the boot (seal does it after). Split `RhelFamily.normalize` so the `/.autorelabel` touch is separable: add a `relabel: bool = True` parameter (`normalize(qcow2, *, relabel=True)`); the boot path calls `family.normalize(staged, relabel=False)` and relies on Task 8's seal for the touch; the virt-customize path calls `normalize(staged)` (relabel=True, unchanged). Debian's `normalize` ignores `relabel` (no SELinux). Update `base.py` Protocol signature.

- [ ] **Step 1: Write failing tests** — extend `test_rootfs_build.py` with a fake boot toolset. A rhel spec drives the boot path: assert `run_customization_boot` was called with a domain XML whose `<name>` is `kdive-build-<uuid>`, `inject_offline` received the firstboot script + file-ops, `seal_customized_image` was called with `selinux=True`, `normalize` was called with `relabel=False`, and the probes read the `staged` path (not `scratch`). A debian spec still drives the virt-customize path (assert `customize`/virt-customize seam called, `run_customization_boot` NOT called). A `run_customization_boot` that raises `PROVISIONING_FAILURE` fails the build (no publish).

```python
def test_rhel_build_uses_customization_boot(tmp_path):
    # _RecordingBootTools injects resolve_accel=lambda arch: ("kvm", None) (x86)
    # or ("tcg", "/usr/bin/qemu-system-ppc64") (ppc64le); no libvirt is touched.
    calls = _RecordingBootTools(accel=("kvm", None))
    plane = LocalLibvirtRootfsBuildPlane(workspace=tmp_path, tools=calls.as_tools())
    out = plane.build(_spec(name="fedora-kdive-ready-44", arch="x86_64"))
    assert calls.customization_boot_ran
    assert calls.boot_accel == "kvm"                # resolve_accel seam drove the branch
    assert calls.boot_domain_name.startswith("kdive-build-")
    assert calls.normalize_relabel is False
    assert calls.sealed and calls.seal_selinux is True
    assert calls.probed_path == calls.staged_path   # provenance from staged, not scratch
    assert not calls.virt_customize_ran

def test_ppc64le_build_boots_under_tcg(tmp_path):
    calls = _RecordingBootTools(accel=("tcg", "/usr/bin/qemu-system-ppc64"))
    plane = LocalLibvirtRootfsBuildPlane(workspace=tmp_path, tools=calls.as_tools())
    plane.build(_spec(name="fedora-kdive-ready-44-ppc64le", arch="ppc64le"))
    assert calls.boot_accel == "tcg"                # TCG branch is unit-covered

def test_debian_build_stays_on_virt_customize(tmp_path):
    calls = _RecordingBootTools()
    plane = LocalLibvirtRootfsBuildPlane(workspace=tmp_path, tools=calls.as_tools())
    plane.build(_spec(name="debian-kdive-ready-13", arch="x86_64", family="debian"))
    assert calls.virt_customize_ran and not calls.customization_boot_ran

def test_boot_failure_aborts_publish(tmp_path):
    calls = _RecordingBootTools(boot_raises=CategorizedError("dnf failed",
        category=ErrorCategory.PROVISIONING_FAILURE))
    plane = LocalLibvirtRootfsBuildPlane(workspace=tmp_path, tools=calls.as_tools())
    with pytest.raises(CategorizedError):
        plane.build(_spec(name="fedora-kdive-ready-44"))
    assert not calls.published
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** the dispatch + reordered boot path + `RootfsBuildTools` boot seams + `normalize(relabel=…)` split. Keep `build()` ≤100 lines by extracting `_build_via_boot(...)` and `_build_via_virt_customize(...)`. No separate console-readability preflight is added — the ADR-0223 wall fails fast on `run_customization_boot`'s first console read (Task 7 covers propagation + `finally` teardown; see Known preconditions).
- [ ] **Step 4: Run** the full `test_rootfs_build.py` + families suites — PASS (debian unchanged, rhel now boots).
- [ ] **Step 5: `just lint && just type && just test`; commit** → `feat(local-libvirt): build rhel rootfs via customization boot (#1147)`.

---

### Task 10: Live proof (x86_64 KVM + ppc64le TCG) + proof record

**Files:**
- Modify/Create: a `live_stack` or `live_vm` test (follow `tests/integration/test_live_stack.py` ppc64le pattern — preflight-skip on missing `qemu-system-ppc64` / `KDIVE_GUEST_IMAGE_PPC64LE`)
- Create: `docs/design/2026-07-13-unified-customization-boot-proof-record-1147.md`

**Acceptance:**
- x86_64 KVM: `build-fs` a Fedora x86_64 kdive-ready image via the customization boot, then provision + boot the built image; assert it reaches `ready` and that `resize_rootfs` ran at provision (the cloud-init-state-reset guarantee — e.g. the provisioned rootfs grew to the overlay size). Record the measured native-KVM customization time (pins the deadline default). Reaching the build's `kdive-customize-ok` marker **is** the proof that `inject_offline`'s offline `multi-user.target.wants` symlink enabled the bootstrap unit (a missed offline-enable manifests as `BOOT_TIMEOUT` here — the live gate the faked-seam unit tests cannot provide).
- ppc64le TCG: `build-fs` the Fedora ppc64le Cloud Base via the TCG customization boot on the x86_64 host, then provision + boot; assert `ready`. Record whether the boot TCG multiplier covered the install+rebuild workload.
- The proof record documents both outcomes (mirror the #1144/#1146 proof-record format) and updates the deadline default if the measurement warrants.

- [ ] **Step 1:** Write the live test(s) with preflight skips; run on this KVM/libvirt host (`just test-live-stack` / the `live_vm` dispatch). This host runs KVM/libvirt directly.
- [ ] **Step 2:** Capture the outcomes into the proof record; if the measured window differs materially from `1800s`, update `KDIVE_LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S`'s default and note it.
- [ ] **Step 3:** `just lint && just type`; commit → `test(local-libvirt): live proof of customization boot, x86_64 KVM + ppc64le TCG (#1147)`.

---

### Task 11: Docs + fast-follow issue

**Files:**
- Modify: the image-lifecycle / build-fs runbook + any agent-facing `build-fs` wrapper docstring that describes the customization mechanism (grep `docs/` and `mcp/tools` for "virt-customize" build descriptions; update to the boot mechanism where operator/agent-visible).
- ADR-0345: move `Status: Proposed → Accepted` and its README row when PR #1147 merges (done at ship, not here — but leave a checklist note).

**Acceptance:**
- Operator/agent docs describe the customization boot (no stale "virt-customize installs packages" claim on the rhel path). Operator walkthroughs use `python -m kdive`/`scripts/*.sh`, not `just`.
- File the fast-follow GitHub issue: "convert debian family to customization boot + delete the virt-customize argv path" (ADR-0345 rollout), referencing #1147 and epic #1139.

- [ ] **Step 1:** Update docs; run `just lint`, the doc guardrails (`python3 scripts/check_adr_status.py`, `./scripts/check-doc-links.sh`, `./scripts/check-doc-paths.sh`, `just check-mermaid`).
- [ ] **Step 2:** `gh issue create` the fast-follow (labels mirroring #1147: `type:feature`, `area:provisioning`, `provider:local-libvirt`).
- [ ] **Step 3:** Commit doc changes → `docs(local-libvirt): describe the customization-boot build mechanism (#1147)`.

---

## Self-Review

**Spec coverage:** one-list/two-renderers → T1–T3; pipeline reordering + probes-from-staged → T9; build-boot identity/transient-AUTODESTROY/on_reboot=destroy/egress → T4, T7; marker-authoritative subtractive classifier → T5; measured deadline → T6; seal (cloud-init reset, autorelabel-at-seal, unit assert) → T8; live proof (KVM + TCG, resize_rootfs) → T10; epic re-sequencing/docs/fast-follow → T11; debian byte-identical argv regression → T2. All spec sections map to a task.

**Placeholder scan:** no "TBD"/"handle edge cases"; each task carries real test code and named interfaces.

**Type consistency:** `Step` union (T1) consumed by `render_argv`/`partition_steps`/`render_firstboot_script` (T2/T3) and `customize_steps` (T2); `build_domain_name`/`render_customization_domain_xml` (T4) consumed by T9; `run_customization_boot`/`CustomizationBootSeams` (T7), `classify_customization_console`/markers (T5), `seal_customized_image` (T8) all consumed by T9. `customize_via`/`customize_steps`/`normalize(relabel=)` Protocol changes (T2/T9) are applied to both families.
