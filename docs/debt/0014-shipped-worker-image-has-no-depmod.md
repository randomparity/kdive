# 0014 — The shipped worker image carries no depmod

## Status

Open
review-by: 2026-10-08

## Concern

Module staging runs the worker host's `depmod` (ADR-0346 §2), resolved against four explicit
directories (`Dockerfile` is not one of the readers, but the worker inside it is). The runtime
stage of `Dockerfile` is `python:3.14.6-slim-bookworm` (`Dockerfile:75`) and its
`apt-get install` list (`Dockerfile:88-91`) installs `gcc make binutils gdb libvirt-clients
openssh-client libseccomp2 libelf1 libdw1 zlib1g flex bison bc git rsync xz-utils libssl-dev
libelf-dev` — no `kmod`, which is the package that provides `depmod`. The image's worker-tool
guard (`Dockerfile:105`) verifies `drgn`, `gdb`, `virsh`, `gcc`, and `make`, and does not verify
`depmod`.

So a worker running from the shipped image cannot index kernel modules for staging. Before #2339
that surfaced only as a `MISSING_DEPENDENCY` `CategorizedError` mid-install, after a System had
been allocated and a guest booted. ADR-0635 adds the `depmod_toolchain` worker-vantage check, so
it now surfaces as a `fail` in `ops.diagnostics` and a nonzero `kdivectl doctor` exit on that
deployment.

The check is reporting a real gap correctly. What is missing is the provisioning.

## Why deferred

#2339's operator-approved surface covers the diagnostics vantage and the shared search contract it
reads. `Dockerfile` is outside it, and adding a runtime package to the shipped image changes what
every container deployment installs — a provisioning decision with its own image-size, build-time,
and supply-chain considerations that does not belong hidden inside a diagnostics change.

The repository's own convention points the same way: `AGENTS.md` assigns a new host tool or system
package to "the role that owns that layer", which for the container shape is `Dockerfile`. That is
not a diagnostics file.

## Non-regression boundary

- The `depmod_toolchain` check keeps its honest `fail` verdict on a host without `depmod`. It must
  not be softened to `not_applicable`, `error`, or a skip to make the shipped image green: the
  container genuinely cannot stage modules, and grading around that would restore exactly the
  blind spot #2339 removed.
- The check's search contract stays the four directories in
  `src/kdive/providers/shared/module_staging_tools.py`. Resolving this record must not widen them.
- `kdivectl doctor` on a container deployment is expected to exit nonzero for this check until the
  record is resolved. A gate that treats a `depmod_toolchain` `fail` as tolerable must say so
  explicitly rather than by omission.

## What would resolve it

Decide whether the container shape is meant to stage modules.

If it is: add `kmod` to the runtime `apt-get install` list in `Dockerfile` and `depmod --version`
to the worker-tool guard, so a missing package fails the image build rather than the first install
job. Then confirm `ops.diagnostics` reports `depmod_toolchain` as `pass` on a freshly built image.

The self-hosted KVM runner needs nothing: #2331 landed `kmod` in
`deploy/ansible/roles/live_vm_host/defaults/main.yml:13`, so the Ansible shape already declares it.
`Dockerfile` is the only remaining gap.

If it is not: record that decision where an operator reads it — `docs/operating/install.md` beside
the existing `guest_arch_accel` note — and state which deployment shapes are expected to fail this
check and why.

## Provenance

target: Dockerfile
target: src/kdive/diagnostics/contributions/depmod_toolchain.py
Found by the #2339 `$spellcraft` design review on 2026-09-08 — reproduced in pass 1
(`run_id: gauntlet-2339-depmod-toolchain-vantage-pass1-8f3c2a`, high) and independently in pass 2
(`run_id: gauntlet-2339-depmod-vantage-p2-a432a914-01`, medium), both by inspecting the runtime
stage's package list against the four search directories.
