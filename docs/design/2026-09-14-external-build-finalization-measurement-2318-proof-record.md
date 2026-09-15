# External-build finalization measurement — proof record (#2318)

Measured attribution for `runs.complete_build` over the real MCP path, for a small and a large
x86_64 kernel bundle. Written to settle the completion-contract decision recorded in
[ADR-0655](../adr/0655-external-build-completion-contract.md), which parent #2314 could not make
from the reports it had.

Both rows were recorded on 2026-09-15 against deployed commit `4883cfdce`. They select durable
asynchronous finalization: the large bundle's `total_ms` is **39 146 ms against a 30 000 ms
supported budget**, and it does so *below* the compressed-size ceiling the contract itself
enforces.

## Supported client request budget

**30 seconds.** `LiveStackClient.over_http` (`src/kdive/mcp/dev_harness.py:203-208`) passes no
timeout, and fastmcp 3.4.4 preserves MCP's 30-second default for regular operations. The driver
runs under a deliberately larger timeout so the large arm can complete and be measured at all; the
30-second figure is the threshold the decision is tested against, never a bound on the run. Both
numbers appear in every row.

## The contract's own size ceiling

`_EXTERNAL_BOOT_ARCHIVE_COMPRESSED_MAX_BYTES` (`src/kdive/build_artifacts/validation.py:59`) is
**2 GiB = 2 147 483 648 bytes**, checked at `validation.py:418` before the archive is scanned. A
bundle above it is rejected, not slow.

This was established here rather than read off the source: the first large bundle this harness cut
was 2 509 255 859 bytes and `runs.complete_build` rejected it with
`build_failure: kernel bundle exceeds the external-boot compressed byte limit`
(`max_bytes: 2147483648`). The tree was then pruned (below) and re-measured.

The ceiling bounds the worst case synchronous finalization could ever face, which is what makes
the large row decisive rather than merely suggestive: the measured bundle is *smaller* than the
largest bundle the contract accepts, and already exceeds the budget.

## Environment

| Fact | Value |
|---|---|
| Deployed revision (`/readyz`) | version `0.4.1`, commit `4883cfdce`, `is_release: false`, started `2026-09-15T03:22:30Z` |
| Host CPU count | 48 (Intel Xeon w7-2495X) |
| Host RAM | 250 GiB |
| Host kernel | `7.2.5-200.fc44.x86_64` (Fedora 44) |
| Object-store deployment shape | SeaweedFS container on the same host, reached over loopback (`KDIVE_BACKEND_SERVICES`, `scripts/live-stack/lib.sh`) |
| Staging filesystem and free space | btrfs on local NVMe; 1.9 TB total, 938 GB free at measurement time |
| Kernel source | linux 7.2.6 (`cdn.kernel.org`, stable) |
| Both classes' base config | the measurement host's distro config (`/boot/config-<uname -r>`), `olddefconfig`, with module signing and the system trusted/revocation keyrings disabled — this tree has no distro signing key |
| The one variable between them | whether modules carry DWARF debug info |
| Small class | `CONFIG_DEBUG_INFO_NONE=y` |
| Large class | `CONFIG_DEBUG_INFO=y` via `CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT=y`, the distro config's member |
| Kernel image compression, both classes | `CONFIG_KERNEL_GZIP=y` — see below |

Using one config and one variable is deliberate: it makes the two rows differ in bundle size
rather than in an unrelated kernel configuration, so the attribution can be compared across them.

Note on reproducing the small class: `CONFIG_DEBUG_INFO` is a bare bool selected by the "Debug
information" choice, and the distro config carries `DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT=y`.
Enabling the `NONE` member alone does not clear it — `olddefconfig` re-resolves the choice back
to the toolchain default. Every non-`NONE` member has to be disabled explicitly, and the
resulting `.config` verified, or the build silently produces the large tree.

### Why both kernels are gzip-compressed, not zstd

The host distro config sets `CONFIG_KERNEL_ZSTD=y`, and the first bundle built from it was
rejected by `runs.complete_build` with `build_failure: boot/vmlinuz does not contain a supported
gzip, bzip2, xz, or zstd ELF kernel payload` — even though the payload decodes. That is a
validator defect, filed as **#2476**; fixing it is outside this issue's frozen scope and #2314
excludes changes to validation behaviour.

Both trees were therefore relinked with `CONFIG_KERNEL_GZIP=y`, a legitimate and common
configuration, and that is what the rows below measure. It is recorded here so the rows are not
misread as having been taken on a distro-default kernel.

The choice does not confound the measurement: `boot/vmlinuz` is ~19.8 MB of a 151 MB and a
1845 MB bundle, and both classes carry the identical image, so the compression algorithm is held
constant across the two rows.

### Why the large tree is pruned

The unpruned debug-info module tree yields a 2 509 255 859-byte bundle, above
the 2 GiB ceiling. To measure the large class at all, `drivers/gpu` (85 modules) and
`drivers/media` (689 modules) were removed from the build tree and from `modules.order` before
`modules_install`, leaving 4253 of 5027 modules and 5.96 GB of unstripped `.ko` (from 8.10 GB).

This changes the bundle's size, which is the variable under study, and its member count. It does
not change the per-byte work: every remaining member is hashed and traversed exactly as before.

## Rows

One row per bundle. Fields marked _server_ come from the finalization measurement record; the
rest are recorded by the driver.

| Field | Small class | Large class |
|---|---|---|
| arch | x86_64 | x86_64 |
| bundle compressed bytes | 150 756 575 (151 MB) | 1 845 478 181 (1845 MB) |
| bundle member count | 6153 | 5249 |
| supported budget (ms) | 30 000 | 30 000 |
| driver timeout (ms) | 1 800 000 | 1 800 000 |
| client elapsed (ms) | 3860.466 | 39 238.139 |
| `prepare_ms` _(server)_ | 0.301 | 0.288 |
| `reassemble_ms` _(server)_ | 0.000 | 0.000 |
| `queue_wait_ms` _(server)_ | 0.007 | 0.003 |
| `scan_ms` _(server)_ | 3815.883 | 39 141.133 |
| `publish_ms` _(server)_ | 7.336 | 4.962 |
| `total_ms` _(server)_ | 3823.596 | **39 146.446** |
| `store_requests` _(server)_ | 121 | 1335 |
| `store_bytes` _(server)_ | 498 407 071 | 5 590 960 497 |
| read amplification (`store_bytes` ÷ bundle bytes) | 3.31× | 3.03× |
| scan time ÷ store requests (ms) | 31.54 | 29.32 |
| `chunked` _(server)_ | false | false |
| `outcome` _(server)_ | succeeded | succeeded |

The last derived row is work per request, not store round-trip latency: on a loopback object
store the round trip is near zero, so almost all of it is decompression and hashing. See the
first reopening condition in ADR-0655.

### What the rows say

**Scanning is the whole cost.** `scan_ms` is 99.80% of `total_ms` for the small bundle and
99.99% for the large one. Preparation, queue wait and publication together are 7.6 ms and 5.3 ms
— under a quarter of one percent in both. Optimising anything but the scan cannot move the
outcome.

**Queue wait was not the cause.** `queue_wait_ms` is 0.007 ms and 0.003 ms. These runs were
uncontended, so this measures the uncontended floor rather than the behaviour under load; what it
rules out is the reading that #2314's timeouts were semaphore starvation on an otherwise idle
server.

**The scan is linear in bundle size.** 25.36 ns per compressed byte for the small bundle and
21.21 ns for the large one — a 12.2× size increase produced a 10.2× time increase. Nothing about
the cost curve flattens with size.

**The validator reads the object about three times over.** `_validate_kernel_bundle` decompresses
a prefix bounded by `_KERNEL_TAR_SCAN_MAX_BYTES` (128 MiB, `validation.py:56`), then three
separate end-to-end passes follow: `_preflight_external_boot_archive` (`validation.py:642`),
`_scan_external_boot_archive` (`validation.py:547`), and `_digest_object` (called at
`validation.py:445`). Two of the three fully decompress the archive. This is the read
amplification #2314 established analytically, now measured at 3.31× and 3.03× of the compressed
object.

**The budget is exceeded below the contract's own ceiling.** The large bundle is 1 845 478 181
bytes; the ceiling is 2 147 483 648. At the large arm's measured rate the ceiling-sized bundle
would take roughly 45.6 s — about 1.5× the budget. Every bundle the contract accepts between
those two sizes is worse than the row recorded here, not better.

## What these rows do not establish

- **No universal size threshold.** #2314 and #2318 both exclude one. On this host and deployment
  shape the measured rate crosses 30 000 ms at roughly 1.41 GB, and the extrapolation to the
  2 GiB ceiling above uses the same rate — both are properties of these two rows on this machine,
  not a general boundary. A different host, object store, or module mix moves them.
- **No decompression-only attribution.** Elapsed scan time covers range reads, gzip and tar
  traversal, sha256 over both the compressed object and the decompressed members, and the ELF
  parse. The record does not apportion it to decompression.
- **Uncontended only.** One finalization at a time, so `queue_wait_ms` reflects an idle
  `_EXTERNAL_BUILD_VALIDATION_SLOTS`. Concurrent finalizations are not measured.
- **Both bundles are single-PUT.** Each is under `SINGLE_PUT_MAX_BYTES` (5 GiB,
  `src/kdive/artifacts/uploads/uploads.py:9`), so no chunk reassembly is in the measured path and
  `reassemble_ms` is 0. The chunked path is instrumented but unmeasured here.
- **ppc64le is not measured.** No ppc64le bundle or cross-toolchain exists on the measurement
  host. The harness is arch-parameterized and re-runs unchanged under `KDIVE_PPC64LE_BUNDLE`;
  the arm is owned by [debt record 0015](../debt/0015-ppc64le-finalization-measurement-unrun.md).
  ADR-0655 names the confirmation as a reopening condition rather than treating these rows as
  standing in for it.
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
  bundles carry unstripped modules. The sizes above are what this repository's own upload lane
  produces, not a tuned figure.

## Reproducing

Both arms run from the same driver; the environment selects the bundle. `$PROOF` is a scratch
directory outside the checkout, `$WORKTREE` the branch checkout.

Build the two trees (`build.sh` verifies the `DEBUG_INFO` choice survived `olddefconfig` and
fails rather than silently producing the wrong class):

```sh
"$PROOF"/build.sh nodebug     # small class
"$PROOF"/build.sh debug       # large class
```

Relink both with `CONFIG_KERNEL_GZIP=y` (see #2476), then prune the large tree:

```sh
cd "$PROOF"/build-fedora
cp -n modules.order modules.order.full
mkdir -p "$PROOF"/pruned-modules
mv drivers/gpu drivers/media "$PROOF"/pruned-modules/
grep -v -e '^drivers/gpu/' -e '^drivers/media/' modules.order.full > modules.order
```

Bring the stack up, then run each arm:

```sh
cd "$WORKTREE"
. "$PROOF"/stack-env.sh
export KDIVE_KERNEL_SRC="$PROOF"/build-nodebug \
       KDIVE_MEASUREMENT_STAGE_DIR="$PROOF"/stage-nodebug \
       KDIVE_MEASUREMENT_OUT="$PROOF"/rows-small.jsonl
.venv/bin/python -m pytest tests/integration/test_finalization_measurement.py -p no:randomly -x -s
```

Repeat with `build-fedora` / `stage-fedora` / `rows-large.jsonl` for the large arm. Each run
prints its row as `MEASUREMENT {...}` and appends it to `KDIVE_MEASUREMENT_OUT`. The `ppc64le`
arm skips unless `KDIVE_PPC64LE_BUNDLE` names a directory holding `kernel.tar.gz`.
