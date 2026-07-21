# 0402 — Explicit read_timeout on the streamed object-store GET (rejected)

Status: Rejected

- **Date:** 2026-07-21
- **Issue:** #1354 (proposed closing the ADR-0400 read-timeout residual; closed
  invalid-premise by this ADR).
- **Relates to:** [ADR-0400](0400-streaming-object-read-for-combined-tar-extract.md)
  (streaming combined-tar read; recorded the residual this ADR investigated) and
  [ADR-0399](0399-single-pass-kernel-bundle-and-scratch-staging.md) (the opt-in
  tmpfs scratch whose slow-write case the residual invoked).

## Context

`object_store_from_env` builds `boto3.client("s3", ...)` with no
`botocore.client.Config`, so botocore's default `read_timeout` of 60 s applies to
S3 reads. ADR-0400 made `extract_kernel_bundle` consume the `GetObject` body in
`tarfile` stream mode, so on a modules-needed install the body is pulled at the
pace of the member-by-member `lib/modules/` repack — bounded by scratch-write
throughput. ADR-0400 recorded an accepted, **unobserved** residual: when
`KDIVE_INSTALL_SCRATCH` (ADR-0399) points at a slow or near-full tmpfs, "a
scratch-write stall that delays the next read past that window trips a mid-stream
`BotoCoreError`" mapped to `INFRASTRUCTURE_FAILURE`, and named "raising the read
timeout on the streamed GET" as the deferred fix. #1354 filed that fix.

This ADR was drafted to implement it (a dedicated stream client carrying a 300 s
`read_timeout`). During its own adversarial review the load-bearing premise —
that a slow *consumer* trips `read_timeout` — was challenged, and on verification
it does not hold. The proposed fix is a no-op for the scenario it targets, so the
change is **rejected** and #1354 is closed invalid-premise.

## Decision

**We will not change the object-store client's `read_timeout` (nor add a
dedicated stream client) for #1354. `read_timeout` does not fire on a
slow-but-progressing streaming consumer, so raising it does not address the
residual ADR-0400 described.**

### Why the premise is wrong

`read_timeout` is a **socket-level, per-operation** timeout, not a wall clock
between successive `read()` calls. botocore constructs
`urllib3.Timeout(connect=<connect_timeout>, read=<read_timeout>)`
(`botocore/httpsession.py`), and urllib3 applies the `read` value as the socket
timeout for each `recv()`; botocore sets no `total` timeout. The clock therefore
counts only while a `recv()` is actually blocked waiting for the **server** to
send. When `extract_kernel_bundle` is blocked *writing* the repacked module tree
to slow/near-full tmpfs it is not calling `_StreamingBodyReader.readinto` →
`body.read()`, so no `recv()` is outstanding and no timeout is counting. TCP flow
control fills the client receive buffer and back-pressures the server; when the
extractor resumes and issues the next `read()`, buffered bytes return promptly. A
slow-but-progressing repack thus never trips `read_timeout` regardless of scratch
speed. `read_timeout` guards a slow/hung **server**, not a slow **consumer**.

### Evidence

- **Empirical (socket semantics).** A `socket.socketpair()` with a 1 s timeout: a
  3 s consumer stall with no `recv()` outstanding then returned buffered data in
  ~0.000 s (no timeout); a `recv()` blocked on a silent peer timed out at ~1.001 s.
  Time *between* reads is not counted; only time *blocked in* a read waiting on the
  peer is.
- **Source.** `botocore/httpsession.py` builds `Timeout(connect=timeout[0],
  read=timeout[1])`; urllib3 applies `read` per socket operation, with no
  whole-stream `total` deadline set by botocore.
- The residual is self-described as unobserved (#1354 is P3, "currently
  unobserved"), so there is no production signal contradicting the analysis.

### What the real exposure is (and why raising read_timeout still doesn't fix it)

During a long client-side stall the plausible genuine fault is the server, load
balancer, or MinIO dropping an **idle connection**, surfacing as
`ConnectionClosedError` (a `BotoCoreError`, mapped to `INFRASTRUCTURE_FAILURE` on
the streaming-body path). A larger `read_timeout` does nothing to prevent an idle
reset. The one condition a larger streaming-GET `read_timeout` *would* help is a
genuinely slow/overloaded server taking > 60 s to send the next chunk **while the
extractor is blocked in `recv()`** — a server-latency case, not the
scratch-throughput case #1354 describes. No observation motivates pre-building for
it.

## Consequences

- **No code change.** `object_store_from_env` and `ObjectStore` are unchanged; the
  60 s default stands on all paths.
- **ADR-0400's residual note is corrected**, not left standing: its
  "held-connection / per-read-timeout" residual bullet gains a pointer to this ADR
  noting that the described consumer-stall trip does not occur, so the residual as
  written overstates the risk. The genuine residuals ADR-0400 records (the S3
  connection held open across the repack; interleaved-extraction atomicity) are
  unaffected — the connection *is* held open; it simply is not clipped by
  `read_timeout` on a slow consumer.
- **Recovery is unchanged.** For any real streaming fault (idle reset, server
  slowness, corrupt gzip) the mapping to `INFRASTRUCTURE_FAILURE` and the new-Run
  recovery (ADR-0030 §2) are exactly as ADR-0400 shipped.
- **The number is retained, not reused** (monotonic ADR rule). A Rejected ADR is
  the durable record that prevents this fix from being re-proposed on the same
  mischaracterized premise.

## Considered & rejected

- **Raise `read_timeout` (client-wide, or on a dedicated stream client) — the
  #1354 proposal.** Rejected as a no-op for the stated scenario: a slow consumer
  does not trip `read_timeout` (see "Why the premise is wrong"). Implementing it
  would add surface (a value, and — for the tight-scoped variant that avoids
  retry-inflating the non-streaming paths — a second client and a routing seam) to
  change behavior that does not occur. Building a fix whose mechanism is verifiably
  absent is a phantom feature.
- **Reframe #1354 to the server-slowness case and bump the streaming GET's
  `read_timeout` anyway.** Rejected now, deferrable later: a longer streaming
  window is genuinely more exposed to a transient slow *server* than the old
  buffered read (which drained fast), so a modest streaming-GET `read_timeout` is
  defensible *if that failure is ever observed*. It is not, and pre-building
  against an unobserved server-latency case is the same speculation the original
  residual note deferred. If a real slow-server streaming failure appears, a new
  issue can raise exactly the streaming GET's timeout with the true mechanism on
  record here.
- **Bounded-buffer background-drain reader.** The one design that would make
  `read_timeout` irrelevant *and* close ADR-0400's held-open-connection residual:
  a background thread drains the S3 socket at network speed into a bounded buffer
  while the extractor pulls from the buffer at scratch pace, decoupling socket-read
  pace from scratch throughput and keeping the connection continuously drained.
  Rejected here because it reintroduces a bounded resident buffer (counter to
  ADR-0400's whole-tar-never-resident goal), adds a producer/consumer thread and
  its failure handling, and still does nothing for the near-full/OOM tmpfs case —
  disproportionate surface for an unobserved residual. Named so the survey is
  honest; it would be its own issue and ADR if a real pace-mismatch or idle-reset
  failure is ever observed.
- **Do nothing and leave ADR-0400's residual note as written.** Rejected: the note
  is mechanically wrong and would keep inviting a re-file of this no-op fix. The
  minimal honest action is to correct the note and record the analysis — which is
  what this Rejected ADR plus the ADR-0400 correction do, at zero code cost.
