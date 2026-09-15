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

**The supported client request budget is 300 seconds.** `LiveStackClient.over_http`
(`src/kdive/mcp/dev_harness.py:203-208`) and the CLI transport both construct `Client(transport)`
with no timeout override, leaving `read_timeout_seconds` as `None`. `StreamableHttpTransport`
then falls through to the MCP SDK's `create_mcp_http_client`, whose default is
`Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)`, and `BaseSession.send_request` with
both timeouts `None` applies no session-level timeout at all. The bound on a long finalization is
therefore the **read** timeout. The 30-second figure is the *connect* default, which a request
this long cleared at the start.

That distinction decided this record, and an earlier draft got it wrong — see *Considered &
rejected*.

Issue #2318 has now measured two finalizations over the real MCP path. The rows, their
environment, and what they do and do not establish are in
[the finalization measurement proof record](../design/2026-09-14-external-build-finalization-measurement-2318-proof-record.md).
Four results decide this record:

- A 1 845 478 477-byte bundle finalized in **34 820 ms** — 11.6% of the budget.
- `_EXTERNAL_BOOT_ARCHIVE_COMPRESSED_MAX_BYTES` is 2 GiB (`validation.py:59`), so the largest
  bundle the contract accepts extrapolates to **~40.5 s** at the measured per-byte rate. The
  ceiling turns one measurement into a bound over every accepted input.
- Scanning is **99.97%** of the large bundle's total. Queue wait was 0.006 ms and publication
  9.207 ms.
- Of the scan, **6914 ms** was time inside the object store across 1335 requests (5.18 ms each);
  the remaining 27 896 ms is decompression, hashing and parsing.

## Decision

**Synchronous completion is retained.** The rule fixed before the measurement — synchronous
completion is retained if the larger bundle's `total_ms` meets the supported budget — is met,
with 8.6× headroom on the measured bundle and 7.4× on the largest one the contract accepts.
Issue #2319's durable asynchronous finalization is **not** activated by this evidence.

No margin factor was applied, and none was needed in either direction: the charter reserved a
labelled judgement for a total landing close to the budget, and 11.6% of it is not that case.

What follows is the contract that synchronous completion now promises. Most of it is already
implemented; this record states it so that #2314's open question has an answer and a later change
cannot quietly drop a property nobody wrote down.

### Shape

One `runs.complete_build` request validates and publishes, and answers when it is done. A caller
waits; it is not asked to poll. The measured cost is a property of the bundle, not of the
deployment's mood, so the wait is bounded by size and the size is bounded by the contract.

### Retry and idempotency

The idempotency key is the pair **(Run, upload-window identity)**. The window identity is already
the fencing token this service uses, and a client-supplied token would let two callers disagree
about which attempt is which.

A `complete_build` on a Run whose build step is already recorded returns that result rather than
redoing the work — `existing_build_result` at `src/kdive/services/runs/complete_build.py:569-571`
and `:656`. That is what makes a retry safe after any interruption, and it is load-bearing for
the recovery property below rather than an optimisation.

Concurrent attempts on one Run serialize behind the Run-scoped advisory lock and the second finds
the result recorded. Attempts across Runs serialize behind
`_EXTERNAL_BUILD_VALIDATION_SLOTS` — see *Consequences*, where that is the sharpest limit on
this decision.

### Cancellation

A finalization ends by completing, by its upload window lapsing, or by the caller disconnecting.
There is no separate cancel: publication is a single transactional commit
(`_finalize_external_build`'s `conn.transaction()`), so before it nothing is published and after
it the Run has succeeded. No state exposes a partially published build, and nothing a caller does
mid-scan can produce one.

### Upload-window fencing

Preserved exactly as implemented: the deadline refresh under
`advisory_xact_lock(conn, LockScope.RUN, run_id)`, the extension cap governed by
`KDIVE_UPLOAD_WINDOW_MAX_TTL_MULTIPLE`, the reaper's collection of lapsed windows, the
`upload_window_replaced` rejection when a re-mint races a running finalize, and
`_require_unreaped_window` immediately before publication. The symbols are the contract here, not
line numbers — an earlier draft of this record cited a line range that its own branch had already
moved.

Retaining synchronous completion keeps the scan inside the request that holds that lock
discipline, which is the cheapest place for it to be.

### Publication ownership

Publication is server-side, transactional, and single-owner: `publish_or_reuse_build` under the
Run lock. A client never publishes, and two callers never publish the same (Run, window).

### Recovery after transport interruption

Bounded to what the code supports, because the two windows differ:

- **After the publication commit,** the outcome is in the state of record. A client whose
  connection dropped recovers it by reading the Run, or by re-calling `complete_build`, which
  returns the recorded result under the idempotency rule above. This works today.
- **Before the commit,** nothing is published, and the client cannot distinguish "the scan never
  ran" from "the scan ran and the connection died before the commit". Both are answered the same
  way — re-call `complete_build`, which redoes the work if no result was recorded — so the
  ambiguity costs a repeated scan, not correctness.

If the upload window lapsed in the meantime, `artifacts.create_run_upload` re-mints and the
expiry path already directs callers there.

### Instrumentation

The finalization measurement instrumentation this decision rests on is **retained permanently**,
not gated to the measurement run: every deployment running a `server` process emits one
`external_build_finalization_measured` record per finalization attempt, carrying the phase split
and `store_requests`, `store_bytes` and `store_wait_ms`. Finalization is a rare
operator-initiated action rather than a hot path, and the phase attribution an operator needs to
diagnose a slow finalization is the attribution #2314 could not produce. The record's payload
shape is frozen by a closed-vocabulary test and joins the ADR-0014 / ADR-0090 log schema without
amending it.

It is also the instrument that would falsify this decision, which is the main reason it stays.

## Consequences

- **The public contract is unchanged.** `runs.complete_build` keeps meaning "wait for the
  answer", and the tool docstring at
  `src/kdive/mcp/tools/lifecycle/runs/registrar.py:511-534` stays accurate. #2319 is not
  activated by this evidence and should be closed or held against the conditions below rather
  than implemented on it.
- **Concurrency is the binding limit, not size.** `_EXTERNAL_BUILD_VALIDATION_SLOTS` is
  `asyncio.Semaphore(1)` (`complete_build.py:47`), so simultaneous finalizations serialize and
  the *n*-th caller's request spans roughly *n* scans. At the ceiling-sized 40.5 s the eighth
  concurrent finalization passes 300 s. Every measured row is uncontended, so this is arithmetic
  on the serial cost rather than an observation — and it is the first thing to measure if
  finalization timeouts are reported again.
- **Read amplification is unchanged and remains the obvious lever.** The validator makes a capped
  128 MiB prefix scan plus three end-to-end passes (`_preflight_external_boot_archive`,
  `_scan_external_boot_archive`, `_digest_object`), measured at 3.03× the compressed object.
  Fusing them would cut both the scan and the request count that the network-store condition
  below multiplies. It is a separate follow-up, not a prerequisite for this decision.
- **Operators gain an observable finalization.** The phase attribution is emitted per attempt in
  every deployment, so a slow finalization is diagnosable without reproducing it.
- **#2314's reported timeouts are not explained by these rows.** They predate #2317's read-ahead
  buffer, were ppc64le, and came from a client this record cannot identify. The proof record
  lists the candidates; this decision does not rest on choosing among them, but a reproduction
  would be evidence against it.

Four conditions reopen this decision. They are named because the evidence behind it is bounded,
and the first two are the ones a real deployment is most likely to meet:

- **Concurrent finalizations.** Measured contention at or above roughly eight simultaneous
  large-bundle finalizations, or any `queue_wait_ms` of operational size, reopens this — the
  semaphore turns a per-request budget into a queue.
- **A network-attached object store.** The measured rows come from a SeaweedFS container on the
  same host, at 5.18 ms per request. Holding the 27 896 ms of non-store work fixed, an endpoint
  adding ~199 ms per request over loopback puts the measured bundle at the budget, and ~167 ms
  puts a ceiling-sized one there. Those are large round trips, but the term is
  `store_requests x RTT` with 1335 requests, so it is the term most likely to move the answer.
- **A client with a shorter timeout.** 300 s is what the clients *this repository ships* enforce.
  An external agent may configure less, and the server can neither see nor raise it. A supported
  client whose bound is below the ceiling-sized 40.5 s reopens this.
- **A ppc64le confirmation.** No ppc64le bundle or cross-toolchain exists on the measurement
  host, so the recorded rows are x86_64 only. The harness is arch-parameterized and re-runs
  unchanged under `KDIVE_PPC64LE_BUNDLE`. The unrun arm is owned by
  [debt record 0015](../debt/0015-ppc64le-finalization-measurement-unrun.md), which outlives
  issue #2318 — the issue closes with this pull request, so without that record this reopening
  condition would have no surviving owner.

## Considered & rejected

- **Durable asynchronous finalization now (#2319).** verified: the measured 34 820 ms is 11.6% of
  the 300 000 ms bound the shipped clients enforce, and the 2 GiB ceiling caps any accepted
  bundle at ~40.5 s at the measured 18.87 ns/byte (2026-09-15, commit `25997d7e0`, 48-core
  x86_64 host, SeaweedFS over loopback — see the proof record). The charter's rule selects
  synchronous completion on that evidence, and adopting a durable worker, its state machine and
  its recovery paths to solve a budget with 7.4× headroom would add the machinery without the
  problem. The conditions above are where that changes.
- **An earlier draft of this record decided the opposite, on a 30-second budget.** verified: that
  figure is httpx's `connect` default, not the request bound — `create_mcp_http_client` returns
  `Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)` and `BaseSession.send_request`
  applies no session timeout when both are `None`. Confirmed end-to-end: `over_http`, built as
  this record cites it and with no timeout override, completed a 35.3-second finalization of the
  1.85 GB bundle, which a 30-second bound would have failed. It is recorded here rather than
  silently corrected because the draft is what a reader comparing this record against #2314
  would otherwise have to reconcile. The regression test that now guards it reads the bound off
  a client instead of asserting the constant against itself.
- **Raise the accepted bundle-size ceiling to remove the pruning this measurement needed.**
  judgment: the ceiling is what converts two rows into a bound over every accepted input, and
  raising it would trade that away for the convenience of one measurement.
- **Leave the completion contract undocumented.** judgment: #2314 could not choose between these
  branches precisely because no record said what synchronous completion promised. Retaining it
  without writing the promise down reproduces the condition #2318 exists to end, and would make
  the next reported timeout start from the same blank page.
