# Host-arch guests in provider and spine live tests (#2694)

## Problem

Seven native live test files pin `arch="x86_64"` (two also pin machine `pc-q35-9.0`). On a
native POWER host they define x86_64 KVM domains, which libvirt refuses, or provision an x86_64
spine profile against a ppc64le kernel and image. The docs that send POWER hosts to this tier do
not say which proofs run there.

## Scope

- `tests/live_vm/__init__.py` gains `require_native_guest_arch() -> str`: `platform.machine()`
  when it is in `SUPPORTED_ARCHES`, else `pytest.skip` naming the host arch. It is test-side, so
  the ADR-0354 guard (which scans `src/kdive`) does not apply.
- Snapshot, traffic-capture (2) and console-inode pass the resolved arch to
  `boot_throwaway_domain`, which already renders from `arch_traits`.
- Preserve-attach and external-boot drop `domain_xml_params.machine`, so `render_domain_xml` uses
  `arch_traits(arch).machine`. Preserve-attach takes `console=` from `console_device`.
  `_render_preserve_domain` and `_transient_xml` take `arch`.
- Spine: the `_provision_profile` factories in `test_live_stack.py` and
  `test_console_parts_live.py` take `arch` and use `arch_traits(arch).default_crashkernel`. Live
  tests pass the same arch to `build_profile` and `build_and_upload_kernel`.
- Docs: both sections list these proofs as host-arch, and the gdbstub debug proofs as
  x86_64-only until #2695.
- No arch skip is added. No proof here has an x86-only step. On libvirt 12.0, a `pseries` domain
  with `<panic model='pvpanic'>` starts up to the kernel load, as `pvpanic-pci`.
- Exclusions: `tests/mcp/debug/**` (#2695); `live_vm_tcg` changes are out of scope.

### Failure model

- Actors and deployments: an operator or CI job running `just test-live` or `live_stack` on a
  native x86_64 or ppc64le KVM host.
- Invariants: x86_64 hosts keep x86_64 guests. The machine becomes the production `q35` alias.
- Accepted: the native ppc64le arm is not run in this change because no POWER host is available.
  A POWER-only failure in a proof's own mechanism (for example snapshot on KVM-HV) is a new
  defect, not arch pinning.
- Covered elsewhere: gdbstub debug tests (#2695); TCG tier (unchanged).

## Success

1. None of the listed tests pins `x86_64`. Each resolves its arch through
   `require_native_guest_arch`.
2. A host arch outside `SUPPORTED_ARCHES` skips with the arch named.
3. None of the listed tests pins a machine; the rendered machine is `arch_traits(arch).machine`.
4. x86_64 hosts boot x86_64 guests, as before.
5. The docs name the host-arch proofs and the x86_64-only debug proofs.

## Validation

- Resolver (1, 2). Mode: focused-test. `tests/live_vm/test_gates.py` patches `platform.machine`
  to `ppc64le` (returned) and to `aarch64` (skip names it). Red: the import fails.
- Render (3). Mode: focused-test. Parametrize the unmarked preserve contract test over both
  arches and assert that the machine and `console=` match the traits. Red on ppc64le.
- Spine factories (1). Mode: focused-test. Parametrize the disk-equality unit test over both
  arches and assert `arch` and `crashkernel`.
- x86_64 behaviour (4). Mode: focused-test. `just test-live` on a provisioned x86_64 host.
- Docs (5). Mode: task-test-not-applicable. The change is prose; `just docs-links` and `just
  docs-paths` only.
