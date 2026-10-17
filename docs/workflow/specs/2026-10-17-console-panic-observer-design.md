# Bounded console panic observation (#3254)

## Authority and problem

The operator-approved #3254 observer-correction proposal (SHA256
`a13f7e0cc6bff61ddc48d79c59758135eddd534f0346894f11df09449bf49e61`)
and claim-bound WORK:SCOPE `q3254-d0e3fcd2` authorize this harness correction.
The original Fedora cell failed; its rotating bytes were reclaimed. Its cause remains
unestablished. Actual-helper HTTP-fake diagnostics prove two observer defects: a panic
marker split across parts is missed, and finding a whole marker returns before a later
CPU/release record. Correcting those defects does not establish historical causation.

`spine.poll_for_new_console_part` currently reads the first listing page and tests each
new part separately. `capture_cells.force_crash_body` checks `panic_seen` only afterwards.
`panic_seen` requires the exact fixture release on the panic's CPU record within its
existing eight-line window. That predicate, arming, job success, durable crashed state,
fixture identities and deployment identities remain requirements.

## Scope and ownership

Extend the existing observer; keep its positional marker argument and tuple result for
`tests/integration/test_console_parts_live.py`, an unchanged caller outside this surface.
Force-crash opts into the full existing predicate and complete precrash baseline metadata.
Change only `spine.py`, `capture_cells.py`, their existing focused unit modules, and this
spec. The implementation plan and contracts are private workflow artifacts.
The existing evidence writer and CellRun remain unchanged consumers. No new general
collector, API, schema, dependency, privileged interface, ADR or migration is required.

Production NMI/control/console collector or kernel panic-contract changes require an
operator-reviewed scope amendment within #3254 after actual evidence. New kernel
baselines/image families require a separate operator decision. Native POWER proof belongs
to #2818; broader capture qualification belongs to existing #2803 scenario owners.
Future read-only NMI/sysctl observations may be proposed for the later proof plan; this
change does not add or execute guest commands. No host/build/crash/restore operation is
authorized by this design phase.

## Observation contract

1. Before crash, drain `artifacts.list(system_id, cursor, limit)` to a terminal page.
   Record the console part IDs and parse `console-part-<generation>-<index>` at the end
   of each returned object key, using numeric components. A missing/invalid continuation,
   repeated cursor, duplicated ID/key or malformed console-part key is incomplete evidence.
   Do not invent store versions. An empty baseline cannot bind the crash generation and
   fails before invoking crash.
2. Pin the newest baseline generation and greatest index in it. After crash, only absent
   baseline IDs with the same generation and index greater than that high-water mark are
   eligible. A newly observed generation change fails incomplete. Older generation/index
   entries are stale and cannot contribute, even if newly listed. An exact baseline ID is
   skipped. Keys from a different System prefix cannot contribute.
3. Complete each paginated listing before reading or matching its parts. Page order may
   differ from numeric part order: sorting a complete set recovers order. Duplicate entries
   invalidate the observation rather than contributing a second copy. A reordered page must
   never create a false positive. Listings are not snapshots; new newer rows missed during
   pagination are found on the next sweep. A missing intermediate eligible index is an
   explicit incomplete failure; do not fabricate continuity. This deliberately fails closed
   if publication briefly presents a gap; a later separately authorized attempt may recover.
4. Read each eligible immutable ID once, including every `artifacts.get` byte window.
   Require actual string content; reject omitted/unavailable content, non-increasing or
   missing/skipped next offsets, incompatible object refs, and error envelopes. Check
   consistent returned size_bytes, exact UTF-8 byte advancement and terminal size; a window
   containing U+FFFD cannot establish its original byte length after replacement decoding
   and fails incomplete. Complete a part before joining the same-generation consecutive
   stream without inserted separators. Apply unchanged `panic_seen` only to the prefix
   ending at the last observed newline: an unfinished CPU release token can acquire a
   wrong-release suffix in the next part. Return the
   final contributing ID and joined text only when the whole predicate holds. No marker,
   wrong release or absent CPU continuation remains failure, even with durable crashed.
5. The postcrash observation has one 600-second monotonic deadline starting on observer
   entry. Listing pages, artifact windows, waits and matching share that deadline; awaited
   calls are bounded by its remaining duration and a success after expiry is rejected.
   The precrash snapshot has a separate bounded 600-second acquisition phase and consumes
   the same per-cell diagnostic budget. Arming and crash/job draining retain existing limits.
   Timeout is failed/incomplete proof; recovery is evidence review and a separately approved
   candidate-bound retry, not a silent reset of the timer.
6. Per force-crash cell, retain at most 128 distinct postcrash parts and 16 MiB (16*1024*1024
   bytes) of UTF-8 canonical JSON diagnostic response records, including already-redacted
   content and retained metadata. Baseline IDs/metadata are also charged to the byte budget;
   already-read IDs do not reset either budget. Enforce before retaining/appending, and stop
   at the first overflow with explicit incomplete failure. No truncation can yield success.
   A received response beyond the bound is represented by a small fixed overflow category,
   without retaining its overflowing content. This is a retention bound; the existing API's
   individual response ceiling still bounds in-flight acquisition.

## Failure evidence and cleanup

The force-crash caller owns a bounded diagnostic accumulator for its observation. Preserve
returned artifact IDs, object refs, requested content-window offsets/list cursors and
returned stable error_category, with available metadata, plus
SHA256 hashes of retained content. Exclude download URIs and unrelated refs; never collect
private guest keys or environment. Store only existing redacted API content, through
`run.writer.artifact(payload)`; append the returned digest to `run.artifacts` before leaving
the body and entering its existing mandatory owned cleanup. Preserve incomplete parts as
responses, marked incomplete, so a window failure does not erase already-read bytes.

Persist in a finally path around baseline/crash/observation so crash or acquisition failures
also retain available context. Catch diagnostic writer failures independently: on an existing
failure, add a safe exception-class note to that original exception and re-raise it unchanged;
on success, evidence-writer failure fails the cell. Failure reason and completion flag enter
the bounded private payload; API error envelopes set a closed acquisition_failure reason
before propagation, without exception text that could include private values.
An unavailable writer cannot promise durable bytes: report that acquisition/persistence gap
explicitly and still perform cleanup. Never preserve evidence by leaking a System.

## Failure model

- Actors and deployments: operator-run local x86_64 force-crash cells over real MCP HTTP;
  focused tests with existing fake clients; the unchanged generic marker caller remains
  supported through the existing observer signature.
- Invariants and assets: honest fixture-bound panic proof; no cross-generation/gap/stale
  joins; bounded redacted diagnostics before cleanup; original exception identity preserved.
- Accepted failure classes: transient list publication gaps can fail a valid crash rather
  than risk a false positive; replacement-decoded ambiguous byte lengths fail incomplete;
  writer/storage failure may leave only an explicit diagnostic
  gap, while cleanup remains mandatory. Original historical cause is not recoverable from
  reclaimed parts and is not claimed here.
- Covered elsewhere: runtime NMI/kernel contract by separately approved #3254 amendment;
  native POWER by #2818; broader capture scenarios by #2803 owners; deployment/fixture
  identity, arming, job and state assertions by existing functional carrier checks.

## Validation and completion

Focused unit tests exercise marker split and CPU continuation, numeric indices 9/10,
complete pagination, split content windows and preservation ordering. Reject skipped byte
windows, premature terminal windows and release suffixes beyond unfinished CPU records;
retain safe requested-window metadata and distinct returned error categories. Adversarial cases
cover gaps, duplicates, malformed/repeated/missing continuation, stale precrash IDs/keys,
generation reset, wrong release/no marker, degraded content, non-progressing windows,
deadline expiry during a call, and both diagnostic bounds. Controlled faults must make
new contract tests fail before their corrections are retained. Existing generic marker
caller expectations remain covered without broadening its qualification contract.

Use the existing focused test modules, `just lint`, whole-tree `just type`, doc links/path
gates and `just records`. The mandatory pre-push hook owns the later full `just ci` run.
Unit evidence establishes harness behavior only. Issue Expected criteria still require
original cause evidence and a genuine successful cell or an explicitly reviewed correction
of an invalid proof contract. That later proof needs a separately approved exact candidate,
host, existing genuine #3219 fixtures, bounded crash/evidence/export/restore plan and cleanup;
no live result is asserted by this design or its focused tests.
