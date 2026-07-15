# Runbook: four-method live run

Operator guide for validating all four capture methods — `kdump`, `gdbstub`, `console`, and
`host_dump` — on a System running a kernel you built locally and uploaded on the build lane
(ADR-0234). Like prior milestones' real-hardware runs, it is **operator-run, not CI**:
the `live_stack` suite skips cleanly on hosts without the prerequisites.

For the stack bring-up steps shared by every live run (backends, env, VM fixtures, host processes),
follow the [local live-stack runbook](live-stack.md) §1–4 first. The remote `qemu+tls://` variant
additionally needs the steps in [remote-live-stack.md](remote-live-stack.md) §1–4. Run this runbook
**after** the stack is up.

## Prerequisites

All prerequisites from the [local live-stack runbook](live-stack.md), plus:

- A locally-built kernel to upload: a combined `kernel` tar (`boot/vmlinuz` + `lib/modules/`)
  with the kdump/debug symbols armed (`CONFIG_KEXEC`, `CONFIG_CRASH_DUMP`, `CONFIG_VMCORE_INFO`,
  `CONFIG_FW_CFG_SYSFS`, …). See the [build-lane recipe](../external-build-upload.md).
- The remote provider configured with a base-OS qcow2 and TLS, if running against a remote
  `qemu+tls://` host (see [remote-live-stack.md §1–4](remote-live-stack.md)).
- For **local-libvirt** `kdump` capture only: the worker venv must import `guestfs` (the
  `libguestfs` Python binding) and `drgn`. Wiring both is a one-time step —
  see [Wire the worker venv](#wire-the-worker-venv-drgn--libguestfs) in §4b and
  [ADR-0203](../../adr/0203-local-libvirt-kdump-overlay-harvest.md).

## 1. Provision and create the Run

Allocate a System (`allocations.request` then `systems.provision`), open an investigation, and
create a Run:
`runs.create(investigation_id=<inv>, system_id=<B>, build_profile={"schema_version": 1})`.

## 2. Upload the kernel

Upload your locally-built artifacts, then finalize the Run:

```
# 1. artifacts.create_run_upload(run_id=<run>, artifacts=[{name, sha256, size_bytes}, ...])
#    → PUT each object to its presigned upload_url (see the build-lane recipe)
# 2. runs.complete_build(run_id=<run>)  → validates the upload, marks the Run installable
```

`runs.complete_build` validates the uploaded artifacts' **structure** (bzImage magic, gzip layout,
a `lib/modules` member, manifest `sha256`/`size_bytes`, and the optional `vmlinux` build-id). It
never inspects your `.config`: arming kdump is your responsibility at build time — a kernel missing
`CONFIG_KEXEC`/`CONFIG_CRASH_DUMP`/`CONFIG_VMCORE_INFO` simply captures no vmcore.

## 3. Install and boot the uploaded kernel

Call `runs.install` then `runs.boot`, both keyed on the `run_id`. Poll `jobs.wait` until the
System reaches `ready`.

```
runs.install(run_id=<...>)  → job: queued → wait → completed
runs.boot(run_id=<...>)     → job: queued → wait → System state: ready (or boot_timeout)
```

If the System reaches `boot_timeout`, check the console artifact for boot messages.

## 4. Drive the four capture methods

The four methods require **two** Systems because `host_dump` and `kdump` are both vmcore methods
and `ensure_method_match` (ADR-0050) binds the first captured method per System. Drive them as
in the [M2.5 capstone](remote-live-stack.md#6-four-method-capture-capstone-m25):

| method | System | how |
|--------|--------|-----|
| `gdbstub` | **B** (booted, uploaded kernel) | `debug.start_session transport=gdbstub` → gdb-MI ops (`debug.set_breakpoint`, `debug.continue`, `debug.read_registers`) → `debug.end_session` |
| `kdump` | **B** (after gdbstub, or a fresh boot) | `control.force_crash` → `vmcore.fetch method=kdump` → `introspect.from_vmcore run_id=<B's run>` |
| `console` | **B** (over the same boot lifetime) | `artifacts.list` for the console artifact after teardown/finalize |
| `host_dump` | **A** (separate System, provisioned and crashed) | `control.force_crash` → `vmcore.fetch method=host_dump` via host-side `virDomainCoreDumpWithFormat` |

`control.force_crash` is a destructive op: it requires the `admin` role and the provisioning
profile's `force_crash` opt-in. Provision Systems A and B from a profile that opts into
`force_crash` and drive the crash with an admin token.

### 4a. gdbstub

With the Run booted and System B in the `ready` state, open a single-attach gdbstub session,
keyed on the **run_id** (not the System):

```
debug.start_session(run_id=<B's run>, transport=gdbstub)
```

Confirm the session reaches the `live` state. Then drive the gdb-MI ops the gdbstub transport
exposes — for example set a breakpoint on a kernel symbol, continue, and read registers when it
hits:

```
debug.set_breakpoint(session_id=<...>, location=<kernel symbol>)
debug.continue(session_id=<...>)
debug.read_registers(session_id=<...>, registers=["rip", "rsp"])
debug.end_session(session_id=<...>)
```

The DWARF debuginfo from `CONFIG_DEBUG_INFO_DWARF5` (in the kdump fragment) is what makes the
symbol-name breakpoint resolve. (Live drgn introspection — `introspect.run` — runs over a
`drgn-live` session, not a gdbstub one, so it is a separate transport; the gdbstub leg here proves
the gdb-MI attach + symbolization.)

> **Developing or testing a `debug.*` tool?** `scripts/live-debug.py` collapses this whole
> lifecycle into one command: `uv run python scripts/live-debug.py stopped --reuse` drives a
> Run to a stopped gdbstub session and prints its `SESSION_ID` (reusing an already-booted Run
> when present). `call <tool> '<json>'` invokes any tool, `transcript <session_id>` prints the
> raw gdb/MI exchange (ground truth when a parser disagrees with gdb), and `reload` restarts
> only the server to pick up a code change. See the script's module docstring.

### 4b. kdump

Force a crash on System B:

```
control.force_crash system_id=<B>
```

Poll until the System reaches `crashed`. Then fetch the vmcore:

```
vmcore.fetch system_id=<B> method=kdump
```

The worker queues a capture job that waits out the guest's crash→reboot→upload window
(see the [capture budget note](remote-live-stack.md#5-run-the-suite)). Poll `jobs.wait` until
`completed`. Confirm the artifact appears in `artifacts.list system_id=<B>`.

Then run the postmortem, keyed on the build **run_id** (it resolves the Run's `debuginfo_ref` and
its System's captured core):

```
introspect.from_vmcore(run_id=<B's run>)
```

Confirm a non-empty `tasks` dict in the response's `report`. A missing `VMCOREINFO` is a
`configuration_error` and is **not** a 4/4 pass — do not accept a missing-build-id skip as success.

**local-libvirt kdump (ADR-0203, ADR-0206).** On local-libvirt the capture is host-side, not an
in-guest upload: the uploaded `kernel` tar carries the matching `/lib/modules/<ver>` tree.
`runs.install` injects the modules into the per-System qcow2 overlay via
host-side libguestfs, the guest's `kdumpctl` builds a crash initramfs in-guest, and on
`control.force_crash` the guest writes a real `/var/crash/<ts>/vmcore`. The worker then
force-stops System B's domain (over `KDIVE_LIBVIRT_URI`) and harvests the vmcore host-side
from the qcow2 overlay with a read-only libguestfs mount, extracting build-id + redacted
dmesg with drgn. There is no crash→reboot→upload window to wait out, but three prerequisites
apply on the **worker host**:

- `libguestfs` (the `guestfs` Python binding) must be importable by the **worker venv**
  alongside `drgn`; absence is a `missing_dependency`, not a silent skip. Wiring both into the
  venv is a one-time step — see [Wire the worker venv](#wire-the-worker-venv-drgn--libguestfs)
  below.
- The provisioning profile must set `crashkernel` so `capture_method` selects `kdump` and the
  boot cmdline reserves crash memory. The install gate is satisfied when either the build
  supplied injected modules (`modules_ref`) or a separate initrd was uploaded (`initrd_ref`);
  a local kdump System with neither is a `configuration_error` (see ADR-0206).
- The install-staging and console host directories must be prepared for the worker user and the
  `qemu` user — see [Prepare the worker-host directories](#prepare-the-worker-host-directories-install-staging--console)
  below. This applies to **every** local install/boot, not only kdump.

#### Wire the worker venv (drgn + libguestfs)

The worker imports `drgn` and the `guestfs` binding from the project venv (`.venv`, the
interpreter the host-process worker runs). `drgn` is in the optional `live` dependency-group and
the `guestfs` binding is a **system** package (not pip-installable), so neither is wired by
default. Do both once on the worker host:

1. Pull `drgn` into the venv:

   ```bash
   uv sync --group live
   ```

   On an arch with no drgn wheel (e.g. `ppc64le`), drgn builds from source and needs
   `libkdumpfile` to open kdump-**compressed** vmcores — without it, kdump capture fails
   `drgn was built without libkdumpfile support` even though ELF cores read. Install
   `libkdumpfile-dev` (Debian/Ubuntu) / `libkdumpfile-devel` (Fedora) **before** the build; see the
   [POWER host bring-up runbook](power-host-bringup.md).

2. Install the libguestfs Python binding as a system package:

   ```bash
   sudo apt-get install python3-guestfs      # Debian/Ubuntu
   sudo dnf install python3-libguestfs       # Fedora/RHEL
   ```

3. Make the system binding importable from the venv. `uv` creates the venv with
   `include-system-site-packages = false`, so the system `guestfs.py` is invisible to it. Pick
   one:

   - **Symlink the binding into the venv** (keeps the venv isolated). The system and venv Python
     **minor versions must match** (the `libguestfsmod` extension is built for a specific ABI):

     ```bash
     py=.venv/bin/python
     site=$("$py" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
     # Debian/Ubuntu install the apt binding to the dpkg dist-packages dir, NOT the `purelib` path
     # `/usr/bin/python3` reports (that is the pip-local `/usr/local/...` tree). Fedora/RHEL install
     # to purelib, so prefer the dist-packages dir when it exists, else fall back to purelib:
     sys_site=/usr/lib/python3/dist-packages
     [[ -e "$sys_site/guestfs.py" ]] || sys_site=$(/usr/bin/python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
     ln -s "$sys_site"/guestfs.py "$site"/
     ln -s "$sys_site"/libguestfsmod*.so "$site"/
     ```

   - **Recreate the venv with system site-packages** (simpler, but the venv then sees every
     system package):

     ```bash
     uv venv --system-site-packages
     uv sync --group live
     ```

4. Verify against the venv interpreter (the same probe `scripts/check-local-libvirt.sh` runs):

   ```bash
   .venv/bin/python -c "import guestfs, drgn"
   ```

   `scripts/check-local-libvirt.sh` fails with this exact fix hint when the import does not
   resolve. On a host-services deployment where the worker runs a venv outside the checkout, set
   `KDIVE_PYTHON=/opt/kdive/.venv/bin/python` so the preflight probes the worker's interpreter.

#### Prepare the worker-host directories (install staging + console)

`runs.install` stages the built kernel/initrd under `KDIVE_INSTALL_STAGING` (default
`/var/lib/kdive/install`) before defining the domain, and `runs.boot` reads the guest serial
console log libvirt writes under `/var/lib/kdive/console`. On `qemu:///system` the VM runs as the
`qemu` user and `virtlogd` writes the console log as `root`, so both directories carry
host-permission constraints a non-root worker must satisfy:

- **Install staging** must be a directory the worker user can write **and** the `qemu` user can
  traverse to read the staged kernel. Create it once under a world-traversable path — never under a
  private `$HOME` (mode `0700` hides the staged kernel from `qemu`, and the VM fails to start with
  `could not open kernel file … Permission denied`):

  ```bash
  sudo install -d -o "$USER" -m 0755 /var/lib/kdive/install
  ```

  `scripts/check-local-libvirt.sh` fails with this fix when the directory is missing or unwritable.
  To stage elsewhere, set `KDIVE_INSTALL_STAGING` for the worker to another world-traversable,
  worker-writable path (again, not `$HOME`).

- **Console log** at `/var/lib/kdive/console/<system>.log` is created by `virtlogd` as `root:root`
  mode `0600`; the boot job reads it to capture the redacted console artifact, so the worker user
  must be able to read it. The simplest arrangement is to **run the worker as `root`** (the natural
  identity for managing `qemu:///system` domains, libguestfs, and kexec). If the worker runs
  unprivileged, a default POSIX ACL alone does **not** help — the `0600` create-mode zeroes the ACL
  mask; instead pre-create each `…/console/<system>.log` as the worker user before boot (a
  worker-owned file inherits the directory's `virt_log_t` SELinux type and stays worker-readable
  while `virtlogd` appends to it), or grant the worker read access via your site's policy.

Fetching the core force-stops the domain. For a `crashed` System this is benign — kdive does
not auto-recover a crashed System — but a guest that kdump-rebooted back to multi-user is
stopped when its core is fetched. The core was written to the overlay before any reboot, so it
survives the force-off.

### 4c. console

The console artifact assembles on System teardown. After System B's lifecycle ends:

```
artifacts.list system_id=<B>
```

Confirm a `console` artifact is present. If the artifact is absent, check whether the reconciler
collected the console stream — the reconciler-hosted `virDomainOpenConsole` collector (ADR-0095)
assembles the artifact on teardown-finalize, so assert it **after** the System is `torn_down`.

### 4d. host_dump

On a **separate** System A (provisioned and in the `ready` state):

```
control.force_crash system_id=<A>
vmcore.fetch system_id=<A> method=host_dump
```

The worker uses `virDomainCoreDumpWithFormat` on the host side and streams the resulting
storage-pool volume through the object store (ADR-0094). Poll `jobs.wait` until `completed`,
then confirm the artifact in `artifacts.list system_id=<A>`.

## 5. Record evidence

A successful four-method run proves the platform captures a crash four ways on an uploaded
kernel. Attach the following as the recorded evidence:

- The `runs.complete_build` response from Step 2 (the upload validated and the Run became
  installable).
- The `artifacts.list` output for each System showing the vmcore and console artifacts.

The passing run confirms that a locally-built, kdump-armed kernel is kdump-capable, symbolizable
(gdbstub/DWARF), and console-observable — the three methods that work alongside `host_dump`.
