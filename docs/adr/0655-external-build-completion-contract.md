# 0655 — External-build completion contract

## Status

Accepted (2026-09-15)

## Context

`runs.complete_build` validates and publishes an external build inside one synchronous MCP
request. Parent issue #2314 established the object-store read amplification analytically and
recorded six request timeouts across 103-MB and 2-GB ppc64le bundles, but with no server timings,
no deployed revision, and no attribution between semaphore wait, object-store I/O, archive
scanning, and publication. #2317 has since landed a 4 MiB read-ahead buffer
(`_RANGE_CHUNK_BYTES`, `src/kdive/build_artifacts/validation.py:57`).

The supported client request budget is **30 seconds**. `LiveStackClient.over_http`
(`src/kdive/mcp/dev_harness.py:203-208`) constructs `StreamableHttpTransport` and
`Client(transport)` with no timeout override, and fastmcp 3.4.4 preserves MCP's 30-second
default for regular operations. That figure is a property of the client this repository ships,
not a number the measurement chose.

Issue #2318 has now measured two finalizations over the real MCP path. The rows, their
environment, and what they do and do not establish are in
[the finalization measurement proof record](../design/2026-09-14-external-build-finalization-measurement-2318-proof-record.md).
Three results decide this record:

- A 1 845 478 181-byte bundle finalized in **39 146 ms**, against the 30 000 ms budget.
- It did so **below the contract's own ceiling**:
  `_EXTERNAL_BOOT_ARCHIVE_COMPRESSED_MAX_BYTES` is 2 GiB (`validation.py:59`), so every accepted
  bundle between 1.85 GB and that ceiling costs more than the row recorded, not less.
- Scanning is **99.99%** of the large bundle's total. Queue wait was 0.003 ms and publication
  4.962 ms; preparation, waiting and publishing together are under a quarter of one percent.

## Decision

**Durable asynchronous finalization is required.** The rule this record fixed before the
measurement — synchronous completion is retained only if the larger bundle's `total_ms` meets the
30 000 ms supported budget — is not met, at a size the contract itself accepts. Issue #2319
implements the mechanism; this record defines what it must promise.

No margin factor was applied. None was needed: the measured total exceeds the budget outright, so
the in-between case this record reserved a labelled judgement for did not arise.

### Shape

`runs.complete_build` accepts a finalization and returns without waiting for the scan. The scan
and publication run in a durable, resumable unit of work owned server-side; the caller observes
progress and outcome through the Run. The 30-second client budget stops bounding the scan, which
is the only property of the current contract the measurement falsified.

### Retry and idempotency

The idempotency key is the pair **(Run, upload-window identity)**, not a client-supplied token:
the window identity is already the fencing token this service uses, and a second key would let
two clients disagree about which attempt is which.

The existing recorded-result behaviour is preserved rather than redesigned — a
`complete_build` on a Run whose build step is already recorded returns that result instead of
redoing the work (`src/kdive/services/runs/complete_build.py:569-571` and `:656`). It extends to
the in-flight case: a retry while a finalization for the same pair is running joins that attempt
and never starts a second scan of the same window.

### Cancellation

A finalization in flight ends in exactly one of three ways: it completes, its upload window
lapses, or an operator cancels it. Cancellation before publication leaves the Run in `CREATED`
with nothing published and the window intact, so the caller may re-mint and retry. After
publication it is refused, because publication is the commit point and the Run has succeeded.
There is no state in which a partially published build is observable.

### Upload-window fencing

The fencing at `src/kdive/services/runs/complete_build.py:553-586` is preserved exactly: the
deadline refresh under `advisory_xact_lock(conn, LockScope.RUN, run_id)`, the extension cap, the
reaper's collection of lapsed windows, `upload_window_replaced` when a re-mint races a running
finalize, and `_require_unreaped_window` immediately before publication.

Moving the scan off the request thread must not move it out of that lock discipline. The
asynchronous owner takes the same Run-scoped advisory lock and re-checks the window before
publishing, because the scan now runs for longer and a window is therefore *more* likely to lapse
underneath it, not less.

### Publication ownership

Publication stays server-side, transactional, and single-owner: `publish_or_reuse_build` under
the Run lock, performed by whichever worker owns that (Run, window) pair. A client never
publishes, and two owners never publish the same pair. The measured cost makes this cheap to
honour — publication is ~5 ms of a ~39 s finalization.

### Recovery after transport interruption

Today a dropped connection loses the outcome even when the scan finished, and the client cannot
tell that from a scan that never ran. Under this contract the outcome is recorded in the
state-of-record before any response, so a client that lost its connection recovers it by reading
the Run — or by re-calling `complete_build`, which returns the recorded result under the
idempotency rule above.

A process crash mid-scan leaves nothing published. The work is resumed or restarted by the owner,
bounded by the upload window; if the window lapsed first, the caller re-mints through
`artifacts.create_run_upload` exactly as the expiry path already directs.

### Instrumentation

The finalization measurement instrumentation this decision rests on is **retained permanently**,
not gated to the measurement run: every deployment running a `server` process emits one
`external_build_finalization_measured` record per finalization attempt. Finalization is a rare
operator-initiated action rather than a hot path, and the phase attribution an operator needs to
diagnose a slow finalization in production is the attribution #2314 could not produce. The
record's payload shape is frozen by a closed-vocabulary test and joins the ADR-0014 / ADR-0090
log schema without amending it. It also remains the instrument that would falsify this decision.

## Consequences

- **The client contract changes.** `runs.complete_build` stops meaning "wait for the answer". The
  tool docstring at `src/kdive/mcp/tools/lifecycle/runs/registrar.py:511-534` states the
  synchronous promise and must change with the implementation in #2319.
- **The scan cost is moved, not reduced.** The read amplification #2314 identified is unchanged:
  the validator makes a capped 128 MiB prefix scan plus three end-to-end passes
  (`_preflight_external_boot_archive`, `_scan_external_boot_archive`, `_digest_object`), measured
  at 3.03× the compressed object. Fusing those passes is a real lever and is left as a separate
  follow-up; it is not a prerequisite for this decision, and by itself would not have delivered
  one of the properties above.
- **Operators gain an observable finalization.** The phase attribution is emitted per attempt in
  every deployment, so a slow finalization is diagnosable without reproducing it.

Three conditions reopen this decision, and they are named because the evidence behind it is
bounded:

- **A ppc64le confirmation.** No ppc64le bundle or cross-toolchain exists on the measurement
  host, so the recorded rows are x86_64 only. The measurement harness is arch-parameterized and
  re-runs unchanged under `KDIVE_PPC64LE_BUNDLE`. The unrun arm is owned by
  [debt record 0015](../debt/0015-ppc64le-finalization-measurement-unrun.md), which outlives
  issue #2318 — the issue closes with this pull request, so without that record this reopening
  condition would have no surviving owner.
- **A network-attached object store.** The measured rows come from a SeaweedFS container on the
  same host, reached over loopback, so the per-request latency term is near zero. Against a
  network-attached endpoint that term becomes `store_requests × RTT` — 1335 requests for the
  large bundle — and phase dominance can invert. Note the direction: this makes synchronous
  completion worse, never better, so it cannot reopen the decision toward the rejected branch.
- **A single-pass scan.** If the three end-to-end passes are fused and re-measured below the
  budget with margin against the 2 GiB ceiling, the first rejected alternative below becomes live
  again on its own evidence.

## Considered & rejected

- **Retain synchronous completion and optimize the scan.** verified: the scan is 99.99% of the
  large bundle's `total_ms`, so optimising anything else cannot help; and a fused single-pass
  scan, even assuming it costs a third of the measured 39 146 ms, leaves ~13 s at 1.85 GB with
  no margin on a *loopback* store, where 1335 requests cost ~0 in latency (measured 2026-09-15,
  commit `4883cfdce`, 48-core x86_64 host, SeaweedFS over loopback — see the proof record). The
  decisive ground is not the arithmetic: a 13-second synchronous call still loses its result on a
  dropped connection, and #2318 requires this record to define recovery after transport
  interruption. No amount of scan optimisation supplies that.
- **Raise the client request timeout instead.** verified: the timeout belongs to the client, not
  the server. `dev_harness.py:203-208` passes none and fastmcp 3.4.4 preserves MCP's 30-second
  default, so a server-side change cannot move it; every external agent would have to opt in
  individually. It also leaves transport interruption unaddressed.
- **Lower the accepted bundle-size ceiling below the measured crossover.** judgment: this would
  turn the 2-GB ppc64le bundles #2314 reports into outright rejections in order to make the
  remainder fast — trading away the inputs the parent epic exists to support for a latency
  property, and answering "can we finalize large bundles" with "do not send them".
- **Leave the completion contract undocumented.** judgment: #2314 could not choose between these
  branches precisely because no record said what synchronous completion promised. Leaving it
  unstated reproduces the condition #2318 exists to end, whichever mechanism ships.
