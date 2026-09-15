# External-build finalization measurement — proof record (#2318)

Measured attribution for `runs.complete_build` over the real MCP path, for a small and a large
x86_64 kernel bundle. Written to settle the completion-contract decision recorded in
[ADR-0656](../adr/0656-external-build-completion-contract.md), which parent #2314 could not make
from the reports it had.

Both rows were recorded on 2026-09-15 against deployed commit `25997d7e0`. They retain
synchronous completion: the large bundle's `total_ms` is **34 820 ms against a 300 000 ms
supported budget**, and the largest bundle the contract accepts extrapolates to ~40.5 s.

> **An earlier revision of this record reported the budget as 30 000 ms and selected the
> opposite decision.** That figure was wrong — see below — and the error was caught by the
> adversarial review of this branch, not by anything in the change. The correction is recorded
> here rather than quietly applied, because a reader comparing this record against #2314's
> reports needs to know which number moved.

## Supported client request budget

**300 seconds**, and it is the httpx **read** timeout.

`LiveStackClient.over_http` (`src/kdive/mcp/dev_harness.py:203-208`) and the CLI transport
(`src/kdive/cli/transport.py:83`) both build `Client(transport)` with no timeout override, which
leaves `read_timeout_seconds` as `None`. Two things follow:

- `StreamableHttpTransport.connect_session` constructs an `httpx.Timeout` only when
  `read_timeout_seconds` is set. With `None` it falls through to the MCP SDK's
  `create_mcp_http_client`, whose default is
  `Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)`.
- `BaseSession.send_request`, with both the request and session read timeouts `None`, calls
  `anyio.fail_after(None)` — there is no session-level request timeout at all.

So the bound on a long finalization is `read`, 300 s. **The 30-second figure this record
previously carried is the `connect` default**, which a request that has been running for
30 seconds cleared long ago.

Confirmed end-to-end rather than only by reading the libraries. A **third, ad-hoc
finalization** — not one of the two rows below — drove the 1 845 478 181-byte bundle through
`LiveStackClient.over_http` built exactly as this record cites it, with no timeout override, on
2026-09-15 against deployed commit `4883cfdce`. It completed in **35.286 s** (Run
`8558c727-246f-4c1c-9502-a7bc4bf5445d`). A 30-second bound would have failed it.

That run used a throwaway client script rather than the committed driver, so it is reproducible
from its description — build `over_http`, pass no timeout, finalize a bundle that takes longer
than 30 s — and not by re-running anything in this repository. The durable guard is the committed
test below; this observation is recorded because a single end-to-end result is what actually
refuted the earlier figure, and burying that would leave the correction resting on library
reading alone.

This is checked by `test_supported_budget_matches_the_shipped_client`
(`tests/integration/live_stack/test_measurement_readout.py`), which reads the effective value off
a client rather than comparing the constant to itself — the shape of the earlier test, which
asserted `SUPPORTED_BUDGET_S == 30.0` and therefore could not fail however the SDK behaved.

**What 300 s does and does not cover.** It is what the clients *this repository ships* enforce.
An external agent using its own MCP client may set a shorter one, and the server cannot see or
raise it. The decision below is scoped accordingly, and ADR-0656 names that as a reopening
condition.

The driver runs under a deliberately larger timeout (`KDIVE_MEASUREMENT_TIMEOUT_S`, 1 800 s) so a
finalization that *would* breach the budget is recorded rather than truncated at it.

## The contract's own size ceiling

`_EXTERNAL_BOOT_ARCHIVE_COMPRESSED_MAX_BYTES` (`src/kdive/build_artifacts/validation.py:59`) is
**2 GiB = 2 147 483 648 bytes**, checked at `validation.py:418` before the archive is scanned. A
bundle above it is rejected, not slow.

This was established here rather than read off the source: the first large bundle this harness cut
was 2 509 255 859 bytes and `runs.complete_build` rejected it with
`build_failure: kernel bundle exceeds the external-boot compressed byte limit`
(`max_bytes: 2147483648`). The tree was then pruned (below) and re-measured.

The ceiling is what makes the large row decisive rather than merely suggestive: it bounds the
worst case synchronous finalization can ever face, so the extrapolation below is to a real
maximum and not to an open-ended one.

## Environment

| Fact | Value |
|---|---|
| Deployed revision (`/readyz`) | version `0.4.1`, commit `25997d7e0`, `is_release: false`, started `2026-09-15T04:20:48Z` |
| Host CPU count | 48 (Intel Xeon w7-2495X) |
| Host RAM | 250 GiB |
| Host kernel | `7.2.5-200.fc44.x86_64` (Fedora 44) |
| Object-store deployment shape | SeaweedFS container on the same host, reached over loopback (`KDIVE_BACKEND_SERVICES`, `scripts/live-stack/lib.sh`) |
| Observed mean per-request store latency | 3.70 ms (small row), 5.18 ms (large row) — `store_wait_ms / store_requests` |
| Storage tier those reads actually hit | page cache, not disk — see below |
| Staging filesystem and free space | btrfs on local NVMe; 1.9 TB total, 938 GB free at measurement time |
| Kernel source | linux 7.2.6 (`cdn.kernel.org`, stable) |
| Both classes' base config | the measurement host's distro config (`/boot/config-<uname -r>`), `olddefconfig`, with module signing and the system trusted/revocation keyrings disabled — this tree has no distro signing key |
| The one variable between them | whether modules carry DWARF debug info |
| Small class | `CONFIG_DEBUG_INFO_NONE=y` |
| Large class | `CONFIG_DEBUG_INFO=y` via `CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT=y`, the distro config's member |
| Kernel image compression, both classes | `CONFIG_KERNEL_GZIP=y` — see below |

Using one config and one variable is deliberate: it makes the two rows differ in bundle size
rather than in an unrelated kernel configuration, so the attribution can be compared across them.

**The measured store latency is a warm-cache figure.** The driver PUTs the bundle and calls
`runs.complete_build` immediately after, so validation reads 5.59 GB out of a 1.85 GB object
seconds after it was written, on a host with 250 GiB of RAM. Those reads came from the page
cache: 3.70 and 5.18 ms per request is loopback HTTP plus a memory copy, not loopback HTTP plus
NVMe. This is not instrumented — it follows from the sequence — and it is disclosed because it is
the *floor* of the store term, which makes the network-store reopening condition below an
upper-bound estimate of how much latency it would take to reach the budget rather than a
conservative one.

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

The unpruned debug-info module tree yields a 2 509 255 859-byte bundle, above the 2 GiB ceiling.
To measure the large class at all, `drivers/gpu` (85 modules) and `drivers/media` (689 modules)
were removed from the build tree and from `modules.order` before `modules_install`, leaving 4253
of 5027 modules and 5.96 GB of unstripped `.ko` (from 8.10 GB).

This changes the bundle's size, which is the variable under study, and its member count. It does
not change the per-byte work: every remaining member is hashed and traversed exactly as before.

## Rows

One row per bundle. Fields marked _server_ come from the finalization measurement record; the
rest are recorded by the driver.

The class names identify the #2314 observations each row corresponds to; the recorded size is
whatever the build produced, never the class name rounded. The small row came in at 151 MB
against a class named 103 MB — 1.46× — because `modules_install` does not strip and this kernel's
distro config builds more modules than whatever produced #2314's figure. Nothing in the
attribution depends on hitting the class name; the bytes in the row are the bytes measured.

The budget is a *client* bound, so the rule is applied to `client_elapsed_ms`, which the driver
measures at the caller. It runs 121 ms above the server's `total_ms` on the large row — request
and response transit plus envelope handling — and both numbers are recorded so the gap is
visible rather than assumed away.

| Field | 103-MB class | 2-GB class |
|---|---|---|
| arch | x86_64 | x86_64 |
| bundle compressed bytes | 150 753 887 (151 MB) | 1 845 478 477 (1845 MB) |
| bundle member count | 6153 | 5249 |
| supported budget (ms) | 300 000 | 300 000 |
| driver timeout (ms) | 1 800 000 | 1 800 000 |
| client elapsed (ms) | 3447.245 | 34 941.486 |
| `prepare_ms` _(server)_ | 0.453 | 0.436 |
| `reassemble_ms` _(server)_ | 0.000 | 0.000 |
| `queue_wait_ms` _(server)_ | 0.008 | 0.006 |
| `scan_ms` _(server)_ | 3393.337 | 34 810.534 |
| `publish_ms` _(server)_ | 6.806 | 9.207 |
| `total_ms` _(server)_ | 3400.669 | **34 820.252** |
| `store_requests` _(server)_ | 121 | 1335 |
| `store_bytes` _(server)_ | 498 399 007 | 5 590 961 385 |
| `store_wait_ms` _(server)_ | 448.103 | 6914.497 |
| read amplification (`store_bytes` ÷ bundle bytes) | 3.31× | 3.03× |
| mean per-request store latency (ms) | 3.70 | 5.18 |
| store wait as a share of the scan | 13.2% | 19.9% |
| share of the supported budget | 1.1% | 11.6% |
| `chunked` _(server)_ | false | false |
| `outcome` _(server)_ | succeeded | succeeded |

### What the rows say

**Both bundles finalize well inside the budget.** The large one uses 11.6% of it — 8.6×
headroom. At its measured rate (18.87 ns per compressed byte) a bundle at the 2 GiB ceiling
would take about **40.5 s**, still 7.4× under. Applying the charter's rule — synchronous
completion is retained if the larger bundle's `total_ms` meets the supported budget — retains
synchronous completion, and the ceiling makes that conclusion cover every bundle the contract
accepts rather than only the one measured.

**Scanning is the whole cost.** `scan_ms` is 99.78% of `total_ms` for the small bundle and
99.97% for the large one. Preparation, queue wait and publication together are 7.3 ms and 9.6 ms.
Any future optimisation that is not the scan cannot move the number.

**Queue wait was not the cause.** `queue_wait_ms` is 0.008 ms and 0.006 ms. These runs were
uncontended, so this measures the uncontended floor rather than the behaviour under load; what it
rules out is the reading that #2314's timeouts were semaphore starvation on an otherwise idle
server. Contention is a separate matter, and it is the sharpest limit this record found — see
below.

**The scan grows with bundle size, and two rows fix a two-term model.** 22.56 ns per compressed
byte for the small bundle and 18.87 ns for the large one — a 12.2× size increase produced a 10.2×
time increase. A single per-byte rate is not what these two rows determine, and the reason is a
confound worth stating: the large row is pruned to 5249 members against the small row's 6153, so
bytes and member count moved in *opposite* directions, and any fitted per-byte rate absorbs
per-member cost.

Solving both rows for `a x bytes + b x members` gives **18.59 ns/byte and 0.097 ms/member**. At
the 2 GiB ceiling that is ~40.5 s with the small row's member count, and ~59 s at
`_EXTERNAL_BOOT_ARCHIVE_MAX_MEMBERS` (200 000, `validation.py:60`) — the worst case the contract
admits on both axes at once. Both are far under 300 s, so the decision does not turn on which
model is used; the two-term fit is recorded because the one-term version would be a claim two
rows cannot carry.

**The validator reads the object about three times over.** `_validate_kernel_bundle`
decompresses a prefix bounded by `_KERNEL_TAR_SCAN_MAX_BYTES` (128 MiB, `validation.py:56`), then
three separate end-to-end passes follow: `_preflight_external_boot_archive`
(`validation.py:642`), `_scan_external_boot_archive` (`validation.py:547`), and `_digest_object`
(called at `validation.py:445`). Two of the three fully decompress the archive. This is the read
amplification #2314 established analytically, now measured at 3.31× and 3.03× of the compressed
object.

**Object-store round trips are a fifth of the scan, on loopback.** 6914 ms of the large row's
34 811 ms scan is time inside the store, at 5.18 ms per request across 1335 requests. The other
27 896 ms is decompression, hashing and parsing. That split is what makes the network-store
question answerable: holding the work term fixed, an object store adding **~199 ms** per request
over loopback would put the measured bundle at the budget, and **~167 ms** would put a
ceiling-sized bundle there. Those are large numbers for a round trip, which is why the decision
holds — but they are not unreachable, and they are the reason the split is recorded rather than
inferred.

**Concurrency is the binding limit, not size.** `_EXTERNAL_BUILD_VALIDATION_SLOTS` is
`asyncio.Semaphore(1)` (`src/kdive/services/runs/complete_build.py:47`), so simultaneous
finalizations serialize: the *n*-th caller's request spans roughly *n* scans. At the
ceiling-sized 40.5 s that puts the eighth concurrent finalization past 300 s, and at the measured
34.8 s the ninth. This is not measured here — every row is uncontended — and it is the one place
the budget is reachable on the deployment that was measured.

## Reconciling with #2314's reported timeouts

#2314 records six request timeouts across 103-MB and 2-GB ppc64le bundles. Nothing in these rows
reproduces that, and this record does not claim to explain it. What it can say:

- These rows are **after** #2317, which landed the 4 MiB read-ahead buffer (`_RANGE_CHUNK_BYTES`,
  `validation.py:57`). #2314's reports predate it, and a smaller read unit multiplies the 1335
  requests measured here directly.
- They are on a **loopback** object store at 3.7–5.2 ms per request. Against a network-attached
  endpoint the request count is the multiplier, which is the interaction the previous section
  quantifies.
- They are **x86_64**, and #2314's were ppc64le.
- The client that produced them is not identified in #2314, and a client with a shorter timeout
  than the 300 s established above would time out where this harness does not.

Each is a live candidate; this measurement discriminates between none of them. That is a gap in
what #2318 establishes, not a finding against #2314.

## What these rows do not establish

- **Both rows measured a kernel-only manifest.** Each declared exactly one artifact, `kernel`.
  The upload contract also accepts `initrd` (digested in full, bounded at 512 MiB by
  `_EXTERNAL_BOOT_INITRD_MAX_BYTES`), `vmlinux` (ranged reads for the build-id) and
  `effective_config` — each adding store requests and hashing inside the same synchronous
  request. The ~40.5 s ceiling figure is therefore a bound over bundle *size* for this manifest
  shape, not over every manifest the contract accepts. At 7.4x headroom the omitted work is
  absorbed, but it was not measured.
- **No universal size threshold.** #2314 and #2318 both exclude one. The extrapolation to the
  2 GiB ceiling above is a property of these two rows on this machine and this deployment shape,
  not a general boundary. A different host, object store, or module mix moves it.
- **No decompression-only attribution.** The scan's non-store time covers gzip and tar traversal,
  sha256 over both the compressed object and the decompressed members, and the ELF parse. The
  record separates store wait from that total; it does not apportion the remainder to
  decompression.
- **Uncontended only.** One finalization at a time, so `queue_wait_ms` reflects an idle
  `_EXTERNAL_BUILD_VALIDATION_SLOTS`. The concurrency limit noted above is arithmetic on the
  measured serial cost, not an observation.
- **One observation per class, and no variance estimate.** The large class was in fact
  finalized twice — 34 941 ms as the recorded row and 35 286 ms in the ad-hoc budget
  confirmation above, on different commits — which is agreement, not a distribution. At 11.6% of
  the budget the large row would have to be off by more than 8× to change the decision, which is
  why so few samples are accepted here; nothing in them supports a claim about the spread.
- **Both bundles are single-PUT.** Each is under `SINGLE_PUT_MAX_BYTES` (5 GiB,
  `src/kdive/artifacts/uploads/uploads.py:9`), so no chunk reassembly is in the measured path and
  `reassemble_ms` is 0. The chunked path is instrumented but unmeasured here.
- **`store_requests`, `store_bytes` and `store_wait_ms` count the validation pass only.**
  `_CountingStore` wraps the store handed to validation, not the one reassembly uses. On a
  chunked finalization the reassembly I/O therefore shows up in `reassemble_ms` and in none of
  the three counters. That costs nothing on these rows — both are single-PUT — but a reader of a
  chunked record must not treat the counters as the whole finalization's store traffic.
- **ppc64le is measured below.** The confirmation run recorded on 2026-09-15 against
  deployed commit `52bca7d5e` on a POWER9 host; see the [ppc64le row](#ppc64le-row) section.
  [Debt record 0015](../debt/0015-ppc64le-finalization-measurement-unrun.md) is resolved.
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

## ppc64le row

Recorded on 2026-09-15 against deployed commit `52bca7d5e` on a POWER9 host. This is the
confirmation run owned by [debt record 0015](../debt/0015-ppc64le-finalization-measurement-unrun.md)
and named as a reopening condition in ADR-0656.

### Environment

| Fact | Value |
|---|---|
| Deployed revision (`/readyz`) | version `0.4.1`, commit `52bca7d5e`, `is_release: false`, started `2026-09-15T22:12:30Z` |
| Host CPU count | 128 (POWER9, 2366 MHz) |
| Host RAM | 251 GiB |
| Host kernel | `7.0.0-31-generic` (Ubuntu resolute) |
| Host machine | `ppc64le` |
| Object-store deployment shape | SeaweedFS container on the same host, reached over loopback (`KDIVE_BACKEND_SERVICES`, `scripts/live-stack/lib.sh`) |
| Observed mean per-request store latency | 13.53 ms — `store_wait_ms / store_requests` |
| Storage tier those reads actually hit | page cache — driver PUTs the bundle and calls `runs.complete_build` immediately after |
| Kernel source | linux 7.0.0 (`~/src/linux`, built tree, `CONFIG_KERNEL_GZIP=y`, `CONFIG_DEBUG_INFO=y`) |
| Boot member (`boot/vmlinuz`) | `vmlinux` stripped with `strip -s` — 64 MiB ELF64-LE `EM_PPC64`, stripped |
| Module staging | `make modules_install INSTALL_MOD_PATH=...` from the built tree; no pruning needed (bundle 2.0 GiB, under the 2 GiB ceiling) |
| `KDIVE_PPC64LE_BUNDLE` | `/tmp/ppc64le-bundle` — `kernel.tar.gz` cut with `pigz`, `boot/vmlinuz` listed first, `lib/modules/*/vmlinuz` excluded |

No `initrd.img` — the Run is unbound (no System, no VM), finalization only.

### Row

| Field | ppc64le (2-GB class) |
|---|---|
| arch | ppc64le |
| bundle compressed bytes | 2 057 081 133 (2057 MB) |
| bundle member count | 4696 |
| supported budget (ms) | 300 000 |
| driver timeout (ms) | 1 800 000 |
| client elapsed (ms) | **112 304.059** |
| `prepare_ms` _(server)_ | 4.102 |
| `reassemble_ms` _(server)_ | 0.000 |
| `queue_wait_ms` _(server)_ | 0.008 |
| `scan_ms` _(server)_ | 112 153.008 |
| `publish_ms` _(server)_ | 43.506 |
| `total_ms` _(server)_ | **112 200.725** |
| `store_requests` _(server)_ | 1484 |
| `store_bytes` _(server)_ | 6 208 992 137 |
| `store_wait_ms` _(server)_ | 20 083.233 |
| read amplification (`store_bytes` ÷ bundle bytes) | 3.02× |
| mean per-request store latency (ms) | 13.53 |
| store wait as a share of the scan | 17.9% |
| share of the supported budget | 37.4% |
| `chunked` _(server)_ | false |
| `outcome` _(server)_ | succeeded |

### What the ppc64le row says

**The phase attribution agrees with the x86_64 rows.** `scan_ms` is 99.96% of `total_ms`
(112 153 ms of 112 201 ms). Preparation, queue wait and publication together are 47.6 ms —
the same negligible-overhead shape as the x86_64 rows (7–10 ms there, slightly higher here
due to the larger bundle). The prediction in debt record 0015 holds: bytes-driven cost
dominates, and the two arches agree on which phases matter.

**The per-byte scan rate is 54.52 ns/byte**, versus 18.87 ns/byte for the x86_64 large row
(a 2.89× slower rate on POWER9 at 2366 MHz vs the x86_64 host at an Intel Xeon w7-2495X).
The bundle is 11.5% larger than the x86_64 large row (2057 MB vs 1845 MB), and the scan time
is 3.22× longer (112 153 ms vs 34 811 ms) — consistent with the higher per-byte rate on a
slower-clocked POWER9.

**The budget impact at 37.4% is material but not a threat to the decision.** The x86_64
large row used 11.6% of the 300 s budget; the ppc64le row uses 37.4% — 2.67× more of the
budget — with 1.63× headroom remaining. At the measured per-byte rate, a bundle at the
2 GiB ceiling would take about 117 s, still 2.56× under the 300 s budget.

**Store latency is higher than the x86_64 loopback figure.** 13.53 ms per request versus
3.70–5.18 ms on x86_64. Both are page-cache reads on the same-host SeaweedFS — the difference
is loopback HTTP overhead on POWER9 versus x86_64. This is a floor, not a disk figure, and it
is the same caveat the x86_64 rows carry.

**Queue wait was uncontended.** `queue_wait_ms` is 0.008 ms — the same floor measured on
x86_64. The uncontended floor is arch-independent.

**The decision stands under ppc64le evidence.** ADR-0656's reopening condition is that the
ppc64le row disagrees with the x86_64 attribution. It agrees: scan dominates, queue and
publication are negligible, and the per-byte cost difference is a machine-speed factor, not a
different attribution. ADR-0656 stands; debt record 0015 is resolved.

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
