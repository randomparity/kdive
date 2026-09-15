# 0655 — External-build completion contract

## Status

Proposed

Opens as Proposed and becomes Accepted in this same pull request, once the measurement
recorded by
[the finalization measurement proof record](../design/2026-09-14-external-build-finalization-measurement-2318-proof-record.md)
selects the decision. The decision is evidence-selected by construction — issue #2318 freezes
it that way — so this record reserves the number and states the rule the measurement will
settle, rather than asserting an outcome ahead of the evidence.

## Context

`runs.complete_build` validates and publishes an external build inside one synchronous MCP
request. Parent issue #2314 established the object-store read amplification analytically and
recorded six request timeouts across 103-MB and 2-GB bundles, but with no server timings, no
deployed revision, and no attribution between semaphore wait, object-store I/O, archive
scanning, and publication. #2317 has since landed a 4 MiB read-ahead buffer
(`_RANGE_CHUNK_BYTES`, `src/kdive/build_artifacts/validation.py:57`).

The supported client request budget is **30 seconds**. `LiveStackClient.over_http`
(`src/kdive/mcp/dev_harness.py:203-208`) constructs `StreamableHttpTransport` and
`Client(transport)` with no timeout override, and fastmcp 3.4.4 preserves MCP's 30-second
default for regular operations. That figure is a property of the client this repository ships,
not a number the measurement chose, and it is the most likely proximate cause of the timeouts
#2314 reported.

## Decision

Pending the measurement. The rule that selects it is fixed here so the conclusion is
falsifiable either way, and it is the charter's rule rather than one this design invents:

**Synchronous completion is retained only if the larger bundle's `total_ms` meets the 30 000 ms
supported budget.** Otherwise durable asynchronous finalization is required and #2319
implements it.

No margin factor is built into the rule. Issue #2318's outcome says "meets the supported client
request budget"; a factor the charter does not supply would decide the in-between case on this
design's authority instead. Where the measured total lands close enough to the budget that
headroom matters, that is recorded below as a labelled judgement, not folded into the threshold.

Whichever branch the evidence selects, this record then defines retry and idempotency,
cancellation, upload-window fencing (preserving the object-identity and remint fencing at
`src/kdive/services/runs/complete_build.py:553-586`), publication ownership, and recovery after
transport interruption.

The finalization measurement instrumentation this decision rests on is **retained permanently**,
not gated to the measurement run: every deployment running a `server` process emits one
`external_build_finalization_measured` record per finalization attempt. Finalization is a rare
operator-initiated action rather than a hot path, and the phase attribution an operator needs to
diagnose a slow finalization in production is the attribution #2314 could not produce. The
record's payload shape is frozen by a closed-vocabulary test and joins the ADR-0014 / ADR-0090
log schema without amending it.

## Consequences

Pending the measurement. Two conditions reopen this decision whichever way it lands, and both
are named here because the evidence behind it is bounded:

- **A ppc64le confirmation.** No ppc64le bundle or cross-toolchain exists on the measurement
  host, so the recorded rows are x86_64 only. The measurement harness is arch-parameterized and
  re-runs unchanged under `KDIVE_PPC64LE_BUNDLE`. The unrun arm is owned by
  [debt record 0015](../debt/0015-ppc64le-finalization-measurement-unrun.md), which outlives
  issue #2318 — the issue closes with this pull request, so without that record this reopening
  condition would have no surviving owner.
- **A network-attached object store.** The measured rows come from a SeaweedFS container on the
  same host, reached over loopback, so the per-request latency term is near zero. Against a
  network-attached endpoint that term becomes `store_requests × RTT` and phase dominance can
  invert. The decision is scoped to the measured deployment shape.

## Considered & rejected

Pending the measurement. Each bullet will carry a `verified:` ground with its command, result,
and environment, or a `judgment:` ground; the branch not selected by the rule above and the null
"keep the contract undocumented" option are both recorded.
