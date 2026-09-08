# fadump provisioning parity — implementation plan (#2381)

## Goal

Declare the two operator-side fixes the native-POWER fadump proof required in the source code
and documentation, so a clean host reprovision reproduces the proof without hand steps.

## Architecture

Two change surfaces:

1. **`build-fs` Python path** (`src/kdive/images/families/`): `RhelFamily.customize_steps()` in
   `rhel.py` injects customization steps. Add a `fadump-capture.service` constant in
   `_fedora_customize.py` and inject it when `kexec-tools` is in the package set.

2. **Ansible `guest_base_image` role** (`deploy/ansible/roles/guest_base_image/`): add the unit
   file and a conditional `virt-customize` task gated on `fadump_capture: true`.

**Tech stack:** Python 3.14, Ansible YAML, systemd unit syntax, Markdown.

## Global constraints

- Base branch: `main`; branch: `feat/fadump-provisioning-parity-2381`.
- Host architecture: `ppc64le`; declared targets include `ppc64le`; relationship: `included`.
- Guardrails: `just lint`, `just type`, `just lint-ansible`, `just test-ansible`.
- No new ADR, no schema change, no migration, no API change.
- Ruff line length 100, lint set `E,F,I,UP,B,SIM`. `ty` runs whole-tree.

Expected implementation size: 80–110 changed lines (M) — derived from: constants + unit string
in `_fedora_customize.py` (~27 lines), import + step edits in `rhel.py` + new tests (~19 lines),
`fadump-capture.service` unit file in the Ansible role (~16 lines), two tasks in `build_one.yml`
(~24 lines), default-variable lines (~2 lines), runbook update (~10 lines).

## File map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/kdive/images/families/_fedora_customize.py` | **modify** | `FADUMP_CAPTURE_SERVICE_PATH` and `FADUMP_CAPTURE_SERVICE_CONTENT` constants |
| `src/kdive/images/families/rhel.py` | **modify** | Two new steps in `customize_steps()` when `kexec-tools` is present |
| `deploy/ansible/roles/guest_base_image/files/fadump-capture.service` | **create** | Systemd unit for the Ansible role path |
| `deploy/ansible/roles/guest_base_image/defaults/main.yml` | **modify** | `guest_base_image_fadump_capture: false` |
| `deploy/ansible/roles/guest_base_image/tasks/build_one.yml` | **modify** | Resolve `guest_base_image_fadump_capture`; add upload + enable tasks |
| `deploy/ansible/inventory/group_vars/all.yml` | **modify** | `fadump_capture: false` in `kdive_image_defaults` |
| `docs/operating/runbooks/live-testing.md` | **modify** | Document `--exclude='lib/modules/*/vmlinuz'` for ppc64le bundle |

---

## Task 1 — Add fadump-capture constants to `_fedora_customize.py`

**File:** `src/kdive/images/families/_fedora_customize.py`

**Interfaces:** Provides `FADUMP_CAPTURE_SERVICE_PATH` (str) and
`FADUMP_CAPTURE_SERVICE_CONTENT` (str) consumed by Task 2.

**Verification:**
- `Mode: focused-test` — observable contract: `just lint` and `just type` pass; a syntax error
  on the new constants would fail both.
  ```
  just lint && just type
  ```
  Expected: no errors.

**Steps:**

1. After the `KDUMP_FINAL_ACTION_CMD` block in `_fedora_customize.py` (~line 193), add:

```python
# fadump capture-kernel boot: kdump.service cannot rebuild the fadump initrd in the kdive
# initrd environment. This unit supersedes it on the capture-kernel second boot
# (ConditionPathExists=/proc/vmcore), runs makedumpfile, and powers off.
# Harmless on x86_64 where /proc/vmcore never appears outside a genuine kdump capture.
# Declared per AGENTS.md provisioning-parity rule (#2381, proved in #2312).
FADUMP_CAPTURE_SERVICE_PATH = "/etc/systemd/system/fadump-capture.service"
FADUMP_CAPTURE_SERVICE_CONTENT = """\
[Unit]
Description=fadump vmcore capture (kdive)
Documentation=https://github.com/randomparity/kdive
Before=kdump.service
ConditionPathExists=/proc/vmcore
DefaultDependencies=no
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c 'mkdir -p /var/crash && makedumpfile -c -d 31 /proc/vmcore /var/crash/vmcore && poweroff -f'

[Install]
WantedBy=basic.target
"""
```

2. Run `just lint` to confirm no ruff errors.

---

## Task 2 — Inject fadump-capture steps in `rhel.py`

**File:** `src/kdive/images/families/rhel.py`

**Interfaces:** Consumes `FADUMP_CAPTURE_SERVICE_PATH`, `FADUMP_CAPTURE_SERVICE_CONTENT`
from Task 1. Provides two new `WriteFile`/`RunCommand` steps in `customize_steps()`.

**Verification:**
- `Mode: focused-test` — observable contract: existing `test_fedora_debug_steps_enable_kdump_and_sshd`
  and new test both pass; `FADUMP_CAPTURE_SERVICE_PATH` appears in `rendered(steps)`.
  Red: new test fails because the constant is not written/enabled in steps.
  ```
  uv run python -m pytest tests/images/families/test_rhel.py -q
  ```
  Expected: all tests pass.

**Steps:**

1. Add `FADUMP_CAPTURE_SERVICE_CONTENT` and `FADUMP_CAPTURE_SERVICE_PATH` to the
   `from kdive.images.families._fedora_customize import (...)` block in `rhel.py`
   (alphabetically before `FSTAB`).

2. Inside `customize_steps()`, in the `if "kexec-tools" in ctx.packages:` block (lines
   123-126), append after the existing `RunCommand(KDUMP_FINAL_ACTION_CMD)` line:
   ```python
       # fadump capture: supersede kdump.service on the capture-kernel second boot.
       # ConditionPathExists=/proc/vmcore makes this a no-op on normal boots.
       # Declared per AGENTS.md provisioning-parity rule (#2381, proved in #2312).
       steps.append(WriteFile(FADUMP_CAPTURE_SERVICE_PATH, FADUMP_CAPTURE_SERVICE_CONTENT))
       steps.append(RunCommand("systemctl enable fadump-capture.service"))
   ```

3. Add a test to `tests/images/families/test_rhel.py` (after
   `test_fedora_debug_steps_enable_kdump_and_sshd`):
   ```python
   def test_debug_steps_install_fadump_capture_service(tmp_path: Path) -> None:
       steps = _steps(_ctx(tmp_path, is_cloud_image=True))
       text = rendered(steps)
       assert "fadump-capture.service" in text
       assert "systemctl enable fadump-capture.service" in commands(steps)


   Negative test dropped: the build context excludes `kexec-tools` so the guard
   already prevents injection — a negative test over that set can't go red before the change
   and gives no signal. The positive test is the discriminating assertion.

4. Run `just lint && just type` and then the focused test.

---

## Task 3 — Ansible role: unit file, default, variable resolution, and tasks

**Files:**
- `deploy/ansible/roles/guest_base_image/files/fadump-capture.service` (create)
- `deploy/ansible/roles/guest_base_image/defaults/main.yml` (modify)
- `deploy/ansible/roles/guest_base_image/tasks/build_one.yml` (modify)
- `deploy/ansible/inventory/group_vars/all.yml` (modify)

**Interfaces:** Provides `fadump-capture.service` installed and enabled for images with
`fadump_capture: true`. The unit content matches Task 1's `FADUMP_CAPTURE_SERVICE_CONTENT`.

**Verification:**
- `Mode: focused-test` — `just lint-ansible` and `just test-ansible` both pass; a YAML syntax
  error or undefined variable in `build_one.yml` causes the play to fail.
  ```
  just lint-ansible && just test-ansible
  ```
  Expected: no errors.

**Steps:**

1. Create `deploy/ansible/roles/guest_base_image/files/fadump-capture.service` with the
   same unit content as `FADUMP_CAPTURE_SERVICE_CONTENT` (Task 1).

2. Add to `deploy/ansible/roles/guest_base_image/defaults/main.yml`:
   ```yaml
   guest_base_image_fadump_capture: false
   ```

3. In the `Resolve effective image fields` `set_fact` task in `build_one.yml` (after the
   `guest_base_image_kdump_service` line ~line 16), add:
   ```yaml
       guest_base_image_fadump_capture: >-
         {{ image.fadump_capture | default(kdive_image_defaults.fadump_capture) }}
   ```

4. After the "Allow the guest-exec RPCs" task (~line 170) and before the "Set the
   crashkernel reservation" task (~line 175), add:
   ```yaml
   # --- Install the fadump capture service when the image is fadump-capable. ---
   # kdump.service cannot rebuild the fadump initrd in the kdive-supplied initrd environment.
   # The fadump-capture unit supersedes it on the capture-kernel boot
   # (ConditionPathExists=/proc/vmcore), invokes makedumpfile, and powers off.
   # Declared per AGENTS.md provisioning-parity rule (#2381, proved in #2312).
   - name: Upload and enable the fadump capture service for {{ image.name }}
     ansible.builtin.command:  # noqa: command-instead-of-module
       argv:
         - virt-customize
         - -a
         - "{{ guest_base_image_qcow2 }}"
         - --upload
         - "{{ role_path }}/files/fadump-capture.service:/etc/systemd/system/fadump-capture.service"
         - --run-command
         - systemctl enable fadump-capture.service
     when:
       - guest_base_image_fadump_capture | bool
       - (not guest_base_image_staged.stat.exists) or guest_base_image_force | bool
     changed_when: true
   ```
   Note: merged into one `virt-customize` call (design review finding: two calls with same `when:`
   guard leave a partial-failure window; one call is idempotent-on-retry).

5. In `deploy/ansible/inventory/group_vars/all.yml`, add after the `kdump_service` line in
   `kdive_image_defaults`:
   ```yaml
     fadump_capture: false  # set true for fadump-capable images (installs fadump-capture.service)
   ```

6. Run `just lint-ansible && just test-ansible`.

---

## Task 4 — Document the `--exclude` flag in the live-testing runbook

**File:** `docs/operating/runbooks/live-testing.md`

**Interfaces:** Operator guidance to avoid the 128 MiB scan-cap failure when building the
ppc64le bundle.

**Verification:**
- `Mode: task-test-not-applicable` — changed surface is Markdown prose; `just docs-links`
  checks link targets only, not bundle-build instruction content.

**Steps:**

1. In the §"ppc64le spine on native POWER" section (~line 336), the passage:
   > The tar contains the ppc64le ELF at `boot/vmlinuz` and matching `lib/modules/<version>/`;
   > the initramfs must match that kernel. See the [recorded bundle proof]...

   Replace with:
   > The tar contains the ppc64le ELF at `boot/vmlinuz` and matching `lib/modules/<version>/`;
   > the initramfs must match that kernel. **When building the bundle from a Fedora ppc64le
   > kernel RPM, exclude the duplicate `vmlinuz` under `lib/modules/<rel>/vmlinuz`:** that
   > member (~63 MiB) and `boot/vmlinuz` (~63 MiB) together push the first `.ko.xz` past the
   > 128 MiB module-scan cap, causing upload validation to reject the bundle. Pass
   > `--exclude='lib/modules/*/vmlinuz'` to your `tar` invocation. See the
   > [recorded bundle proof]...

2. Run `just docs-links` to confirm no broken links.

---

## Commit sequence

1. Tasks 1 + 2: `fix: inject fadump-capture.service into kdump-capable rhel images (#2381)`
2. Task 3: `fix: declare fadump-capture.service in guest_base_image role (#2381)`
3. Task 4: `docs: document --exclude for ppc64le bundle build (#2381)`
