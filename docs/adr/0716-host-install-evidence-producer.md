# 0716 — Host-installation evidence comes from a controller runner and an on-host boot node

## Status

Accepted (2026-10-01)

Produces evidence for the `host-install/local-libvirt/<arch>/<family>` cells of the
[ADR-0686](0686-independent-coverage-obligations.md) coverage contract (#2807). It does not
change that contract, its cell shape, or the qualifier.

## Context

The contract requires one result per host family carrying six assertions — `clean-install`,
`first-boot`, `repeat-setup`, `second-boot`, `confinement`, `cleanup` — plus the candidate
SHA, deployed `server`/`worker`/`reconciler`/`authority` revisions, the host and guest
platform, the accelerator and the six kernel identity inputs. Its `node_id` must name an
existing pytest function.

Two facts pull in opposite directions. The installation itself happens on a host that starts
without a checkout, Python environment or stack, so nothing on that host can run pytest until
the installation succeeds. The boot proof and the deployed-revision probe need the installed
stack: each process reports its build on a loopback-only aux `/readyz` (ADR-0090 §5), and the
installed lifecycle venv is a local file tree. The later native POWER phase (#2818) must reuse
the same producer on a different host architecture.

## Decision

1. **A controller-side runner** (`scripts/host_install_proof.py`) owns the installation. Over
   SSH with pinned host keys it runs only the documented entry points, in order: clean-host
   observation, an optional operator prerequisite file, the documented bootstrap, `just setup`,
   `just prepare-local-libvirt-host`, `just check-local-libvirt`,
   `examples/local-libvirt/demo-up.sh` and `examples/local-libvirt/build-image.sh`. After the
   first boot it repeats setup, preparation, preflight and bring-up. The runner never edits
   host configuration itself; anything the documented path does not install arrives through the
   operator prerequisite file, whose bytes are part of the evidence.
2. **An on-host pytest node**
   (`tests/integration/test_host_install_live.py::test_installed_host_boots_pinned_kernel`)
   owns each boot phase. It reads the deployed revisions, checks the installed worker
   prerequisites, uploads the pinned kernel bundle, installs and boots it, observes guest
   confinement, releases the allocation and checks owned-resource cleanup. It writes one phase
   record and does not judge the cell.
3. **The runner composes the single result.** It records each assertion's retained artifact
   digest. A cell succeeds only when every step exits 0, both phase records pass, and both
   phases report the same context and the candidate for every role. It writes the independent
   binding before either boot phase. That binding takes kernel identities from the verified
   pinned fixture (ADR-0693), the guest platform from the catalog row, and the host platform
   from the pre-install observation.
4. **The kernel comes from the pinned external fixture.** The runner cuts the documented
   combined `kernel` tar on the controller and copies it to the host. Kernel compilation stays
   outside KDIVE (ADR-0316).

## Consequences

- The `host-install` scenario has one node ID, shared by every family and architecture. The
  node skips unless the runner supplies its phase inputs, so the ordinary `live_stack` recipe
  collects it without running it.
- A failed step stops the run and yields `failure`. An unreachable or non-clean target, or a
  missing input, yields `blocked` with `missing-prerequisite`. No partial run yields `success`.
- Lab reset stays outside KDIVE. The lab repository's reset target consumes the runner CLI and
  output directory; this repository never restores or recreates hosts.
- The deployed-revision helper is local to this node. A sibling helper for catalog smoke
  (#2808) may duplicate it; consolidation is follow-up work.

## Considered & rejected

- **One pytest node that drives the whole installation remotely.** verified: `readyz_urls` in
  `tests/integration/live_stack/skew.py` documents the aux listener as loopback/pod-local
  (ADR-0090 §5), so a controller-side node cannot read the deployed builds. It would also need
  SSH and sudo fixtures inside the test.
- **Build the kernel on each target host.** judgment: cost. It adds a kernel toolchain to every
  host and roughly a build per family, and kernel compilation belongs to the caller's
  environment (ADR-0316).
- **Drop the kernel inputs from the host-install cells.** judgment: fit. The contract owner
  defined the cell; this change produces evidence for it rather than narrowing it.
- **Put the runner in the lab repository.** judgment: fit. Candidate identity, the evidence
  schema and the documented entry points live here, and the lab only resets hosts.
