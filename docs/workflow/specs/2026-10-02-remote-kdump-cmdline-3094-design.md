# Remote kdive slot drops an inherited crashkernel= (#3094)

## Problem

`kdive-install-kernel` adds the kdive grub slot with `grubby --copy-default`, so the slot keeps
the base image's default arguments. Rocky 10 images default to a `crashkernel=` range. A
`gdbstub` Run then boots with memory reserved, and the #1610 gate in `boot()` waits for a kdump
that never arms: `boot_timeout`. ADR-0082 promises the slot carries `crashkernel=` iff the
method is kdump; the helper breaks that promise.

## Scope

- `install`: after `grubby --add-kernel`, when the requested `--cmdline` has no `crashkernel=`
  substring, run `grubby --update-kernel=/boot/vmlinuz-<ver> --remove-args=crashkernel`
  (`|| die`). Keying on the requested cmdline, not `--method`, removes only an inherited
  reservation and never one the worker asked for. The worker adds `crashkernel=` only for
  kdump-family methods and refuses it in user overrides, so on remote this is "non-kdump".
- A kdump install (cmdline carries `crashkernel=`) runs exactly the commands it runs today.
- The #1610 gate and `install.py` are unchanged; their docstrings are already correct.
- ADR-0082 gains an amendment line in §2 saying the helper enforces the iff.
- Both remote runbooks say images staged before this change must be rebuilt
  (`force_image_rebuild=true`).
- How a kdump install merges its `crashkernel=` with an image default that has one is unchanged
  and outside this remedy.

### Failure model

1. **Actors and deployments.** The worker runs the helper as root in a remote-libvirt guest via
   the guest agent; the operator bakes it into the Fedora 43 and Rocky 10 catalog images.
2. **Invariants.** A kdump System keeps its reservation and the #1610 gate. The base image's own
   default entry is never edited.
3. **Accepted failure classes.**
   - An image staged before this change keeps the old helper until rebuilt; the runbooks say so.
   - A failing `--remove-args` exits 1 (`install_failure`), the same class as other grubby steps.
4. **Covered elsewhere.** Debian/SUSE remote cells: #3081/#3082. Arming kdump on fixture
   kernels: #3101.

## Success

1. A non-kdump install leaves no `crashkernel=` in the kdive slot, whatever the default carried.
2. A kdump install leaves the requested `crashkernel=` in the slot and issues no removal.
3. Live: rebuilt images carry the helper byte-identical to source; the Rocky deep-lifecycle cells
   pass boot and the full cycle on both baselines; Fedora cells do not regress; a gdbstub Run's
   `/proc/cmdline` has no `crashkernel=`; a kdump install's slot carries its `crashkernel=`.

## Validation

- Success 1 — `Mode: focused-test`. `tests/deploy/test_install_kernel_helper.py`: stub `grubby`,
  `dracut`, `depmod`, `install`, `cp` on PATH; the grubby stub models the slot's args (copy the
  default on `--add-kernel`, drop on `--remove-args`). Red: delete the removal call. Green:
  `uv run pytest tests/deploy/test_install_kernel_helper.py`.
- Success 2 — `Mode: focused-test`. Same file, kdump case asserts the requested token survives
  and no `--remove-args` ran. Red: make the removal unconditional.
- Success 3 — `Mode: task-test-not-applicable`. Real grubby and guest boot exist only on lab
  hosts; proven by the live arms.
- ADR amendment and runbook lines — `Mode: task-test-not-applicable`. Prose with no executable
  consumer; `just lint` covers links.
