# Host-arch guests in provider and spine live tests (#2694)

## Problem

Seven native live test files pin `arch="x86_64"` (two also pin `pc-q35-9.0`), so on POWER they
define x86_64 KVM domains or x86_64 spine profiles. The docs do not say which proofs run there.

## Scope

- `tests/live_vm/__init__.py` gains `require_native_guest_arch() -> str`: `platform.machine()`
  when it is in `SUPPORTED_ARCHES`, else `pytest.skip` naming the host arch. It is test-side, so
  the ADR-0354 guard (which scans `src/kdive`) does not apply.
- Snapshot, traffic-capture (2) and console-inode pass the resolved arch to
  `boot_throwaway_domain`, which already renders from `arch_traits`.
- Preserve-attach and external-boot drop `domain_xml_params.machine`, so `render_domain_xml` uses
  `arch_traits(arch).machine`. Preserve-attach rewrites the rendered `<cmdline>` (libvirt ignores
  the second one it appended) with `console=` from `console_device`.
- Spine: `_provision_profile` and `_live_script_provision_profile` (`test_live_stack.py`) and
  `_provision_profile` (`test_console_parts_live.py`) take `arch`; a `crashkernel` literal becomes
  `arch_traits(arch).default_crashkernel` (console-parts has none, keeping CONSOLE capture). Live
  tests pass the arch to `build_profile` and `build_and_upload_kernel`.
- Docs: list these proofs as host-arch; `test_local_guest_cpu_live` as skipping on POWER; the
  family-reachability and SUSE-kdump spines and gdbstub debug proofs (#2695) as x86_64-only.
- No arch skip is added: no listed proof has an x86-only step (libvirt 12.0 maps `pvpanic` to
  `pvpanic-pci` on `pseries`).
- Exclusions: `tests/mcp/debug/**` (#2695); `live_vm_tcg` changes are out of scope.

### Failure model

- Actors and deployments: an operator or CI job running `just test-live` or `live_stack` on a
  native x86_64 or ppc64le KVM host.
- Invariants: x86_64 hosts keep x86_64 guests. The machine becomes the production `q35` alias.
- Accepted: the native ppc64le arm is unrun (no POWER host); its proxy is a libvirt define of
  the rendered ppc64le preserve XML. A POWER-only failure in a proof's own mechanism is a new
  defect, not arch pinning.
- Covered elsewhere: gdbstub debug tests (#2695); TCG tier (unchanged).

## Success

1. None of the listed tests pins `x86_64`. Each resolves its arch through
   `require_native_guest_arch`.
2. A host arch outside `SUPPORTED_ARCHES` skips with the arch named.
3. None of the listed tests pins a machine; the rendered machine is `arch_traits(arch).machine`.
4. x86_64 hosts boot x86_64 guests, as before.
5. The docs name the host-arch, POWER-skipping, and x86_64-only native proofs.

## Validation

- Pins (1, 3). Mode: focused-test. `rg 'arch="x86_64"|"arch": "x86_64"|pc-q35'` over the seven
  files has no hit in a listed test. Red: two hits in `test_live_stack.py` on main.
- Resolver (1, 2). Mode: focused-test. `tests/live_vm/test_gates.py` patches `platform.machine`
  to `ppc64le` (returned) and to `aarch64` (skip names it). Red: the import fails.
- Render (3). Mode: focused-test. The preserve contract test, over both arches, asserts the
  machine and a single `<cmdline>` from the traits. Red on ppc64le.
- Spine factories (1). Mode: focused-test. Parametrize the disk-equality unit test over both
  arches and assert `arch` and `crashkernel`.
- x86_64 behaviour (4). Mode: focused-test. The throwaway proofs under `-m live_vm` on this
  x86_64 host. The spine arm needs a built kernel tree and is reported unrun.
- Docs (5). Mode: task-test-not-applicable. Prose; `just docs-links` and `docs-paths` only.
