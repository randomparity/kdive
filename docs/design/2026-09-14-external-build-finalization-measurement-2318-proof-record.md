# External-build finalization measurement — proof record (#2318)

Measured attribution for `runs.complete_build` over the real MCP path, for a 103-MB-class and a
2-GB-class x86_64 kernel bundle. Written to settle the completion-contract decision recorded in
[ADR-0655](../adr/0655-external-build-completion-contract.md), which parent #2314 could not make
from the reports it had.

> **Status: measurements pending.** The harness and instrumentation land first; this record is
> filled in from the recorded rows and the ADR is advanced to Accepted in the same pull request.
> The schema below is the contract the driver emits, so a reader can tell what will be recorded
> and what deliberately will not be.

## Supported client request budget

**30 seconds.** `LiveStackClient.over_http` (`src/kdive/mcp/dev_harness.py:203-208`) passes no
timeout, and fastmcp 3.4.4 preserves MCP's 30-second default for regular operations. The driver
runs under a deliberately larger timeout so the 2-GB arm can complete and be measured at all; the
30-second figure is the threshold the decision is tested against, never a bound on the run. Both
numbers appear in every row.

## Environment

| Fact | Value |
|---|---|
| Deployed revision (`/readyz`) | _pending_ |
| Host CPU count | _pending_ |
| Host kernel | _pending_ |
| Object-store deployment shape | SeaweedFS container on the same host, reached over loopback (`KDIVE_BACKEND_SERVICES`, `scripts/live-stack/lib.sh`) |
| Staging filesystem and free space | _pending_ |
| Kernel source | linux 7.2.6 (`cdn.kernel.org`, stable) |
| Both classes' base config | the measurement host's distro config (`/boot/config-<uname -r>`), `olddefconfig`, with module signing and the system trusted/revocation keyrings disabled — this tree has no distro signing key |
| The one variable between them | whether modules carry DWARF debug info |
| Small class | `CONFIG_DEBUG_INFO_NONE=y` |
| Large class | `CONFIG_DEBUG_INFO=y` (the distro config's `DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT`) |

Using one config and one variable is deliberate: it makes the two rows differ in bundle size
rather than in an unrelated kernel configuration, so the attribution can be compared across them.

Note on reproducing the small class: `CONFIG_DEBUG_INFO` is a bare bool selected by the "Debug
information" choice, and the distro config carries `DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT=y`.
Enabling the `NONE` member alone does not clear it — `olddefconfig` re-resolves the choice back
to the toolchain default. Every non-`NONE` member has to be disabled explicitly, and the
resulting `.config` verified, or the build silently produces the large tree.

## Rows

One row per bundle. Fields marked _server_ come from the finalization measurement record; the
rest are recorded by the driver.

| Field | 103-MB class | 2-GB class |
|---|---|---|
| arch | x86_64 | x86_64 |
| bundle compressed bytes | _pending_ | _pending_ |
| bundle member count | _pending_ | _pending_ |
| supported budget (ms) | 30000 | 30000 |
| driver timeout (ms) | _pending_ | _pending_ |
| client elapsed (ms) | _pending_ | _pending_ |
| `prepare_ms` _(server)_ | _pending_ | _pending_ |
| `reassemble_ms` _(server)_ | _pending_ | _pending_ |
| `queue_wait_ms` _(server)_ | _pending_ | _pending_ |
| `scan_ms` _(server)_ | _pending_ | _pending_ |
| `publish_ms` _(server)_ | _pending_ | _pending_ |
| `total_ms` _(server)_ | _pending_ | _pending_ |
| `store_requests` _(server)_ | _pending_ | _pending_ |
| `store_bytes` _(server)_ | _pending_ | _pending_ |
| mean per-request latency (ms) | _pending_ | _pending_ |
| `chunked` _(server)_ | _pending_ | _pending_ |
| `outcome` _(server)_ | _pending_ | _pending_ |

## What these rows do not establish

- **No universal size threshold.** #2314 and #2318 both exclude one. The rows say what these two
  bundles cost on this host and deployment shape, not where a general boundary lies.
- **No decompression-only attribution.** Elapsed scan time covers range reads, gzip and tar
  traversal, sha256 over both the compressed object and the decompressed members, and the ELF
  parse. The record does not apportion it to decompression.
- **Both bundles are single-PUT.** Each is under `SINGLE_PUT_MAX_BYTES` (5 GiB,
  `src/kdive/artifacts/uploads/uploads.py:9`), so no chunk reassembly is in the measured path and
  `reassemble_ms` is 0. The chunked path is instrumented but unmeasured here.
- **ppc64le is not measured.** No ppc64le bundle or cross-toolchain exists on the measurement
  host. The harness is arch-parameterized and re-runs unchanged under `KDIVE_PPC64LE_BUNDLE`;
  the arm is owned as this issue's follow-up.
- **The kernel-build toolchain is outside the repository.** Both trees were built outside the
  checkout, so the compilers and headers that produced them are not covered by the Ansible
  declaration this change makes for the driver's own host binaries (`make` and GNU `tar`, in
  `deploy/ansible/roles/libvirt_stack`). Disclosed rather than silent.
- **Those two binaries were a pre-existing undeclared dependency, repaired here — not one this
  harness introduced.** The existing live-stack spine already invokes `combined_kernel_tar`
  (`tests/integration/live_stack/spine.py:505`), and neither `make` nor `tar` appeared in any of
  `libvirt_stack`'s three per-family package lists. This change depends on both, so it repairs
  the gap; it does not get credit for discharging an obligation that predates it.
- **`modules_install` does not strip.** `combined_kernel_tar` runs the plain recipe, so both
  bundles carry unstripped modules. The sizes below are what this repository's own upload lane
  produces, not a tuned figure.

## Reproducing

_pending — the exact commands for each run._
