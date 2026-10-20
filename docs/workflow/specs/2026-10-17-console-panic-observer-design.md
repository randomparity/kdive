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

The requirements below govern the explicit force-crash opt-in. The generic marker-only
call keeps its existing loop/content eligibility in the same entry point, including marker
success beyond 128 inspected parts and replacement-decoded content. It creates no retained
diagnostic state and consumes none of the force-crash diagnostic budget. Its outside caller
and existing timeout/tuple contract remain unchanged; no second observer is introduced.

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
caller expectations remain covered without broadening its qualification contract, including
marker part 129 after 128 nonmatches and marker text accompanied by U+FFFD.

Use the existing focused test modules, `just lint`, whole-tree `just type`, doc links/path
gates and `just records`. The mandatory pre-push hook owns the later full `just ci` run.
Unit evidence establishes harness behavior only. Issue Expected criteria still require
original cause evidence and a genuine successful cell or an explicitly reviewed correction
of an invalid proof contract. That later proof needs a separately approved exact candidate,
host, existing genuine #3219 fixtures, bounded crash/evidence/export/restore plan and cleanup;
no live result is asserted by this design or its focused tests.

## Amendment 1 (2026-10-20): per-start console boot identity

### Evidence

One genuine candidate-bound force-crash cell on a disposable Fedora 44 x86_64 host passed
install, boot identity, modules, reconnect and the crash job, then this observer failed closed
with `generation_change`. The baseline held `console-part-0-000000`; the post-crash listing
held `console-part-1-000000` and `console-part-1-000001`. The two `console_rotate` payloads'
`boot_id` values differed only in the mtime component, with the same device and inode.

`reconciler/repairs/console_rotation.py::_boot_id` returns `dev:ino:mtime`.
`artifacts/console/rotation.py::_detect_new_boot` treats any change as a new boot: it bumps the
generation and re-cuts the whole log from offset 0. Under ADR-0576 the worker truncates one
surviving inode before every start, so device and inode never change across boots. mtime was
the only per-boot component, and it also advances on every append. ADR-0273 decision 4 already
requires "a per-boot signal independent of file size". The observer's same-generation rule is
correct; the production identity is not.

### Design

1. **Record a per-start nonce wherever the log is truncated.** Two functions truncate the
   console before a start: `storage._prepare_console_log` (provisioning, booter power-cycle,
   install and customization boots, control power-on, snapshot revert) and
   `readiness.prepare_console_readiness_window` (the external boot session and the system
   authority lane, which replace the seam). After its `ftruncate`, each calls one helper,
   `storage.record_console_boot(log_path)`. The helper writes a fresh 128-bit hex nonce to a
   sibling marker, `<system_id>.boot`, beside `<system_id>.log`.
   `runtime_paths.console_boot_marker_path(log_path)` is the single path derivation. The write
   is atomic: a uniquely named temp file opened `O_CREAT|O_EXCL|O_NOFOLLOW`, `fchmod 0644`,
   then `os.replace`. A failed write removes the temp file and fails the start with the seam's
   existing `PROVISIONING_FAILURE` identity error naming the marker. Truncate comes first: a
   rotation that reads the truncated log with the old nonce resets through the existing shrink
   check, with no bytes to misattribute. The new nonce then resets once more. That can leave
   one generation with no parts, but prior-boot bytes never enter the new boot's generation.
2. **Identity is `dev:ino:<nonce>`.** The reconciler's `_boot_id` keeps its `os.stat` of the
   log; a stat failure (not co-located, unreadable or absent) still degrades to the existing
   reset-forcing `""`. The nonce is opened `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`. It must be a
   regular file owned by the log's owner (so a planted FIFO cannot stall the serial sweep),
   is capped at 64 bytes, and is accepted only as exactly 32 lowercase hex characters. A missing, unreadable or malformed
   marker contributes an empty nonce, `dev:ino:`, which stays stable rather than resetting on
   every pass. Validation keeps arbitrary file content out of the persisted job payload.
3. **Unchanged contracts.** `ConsoleRotatePayload.boot_id` stays a string from the same producer.
   The sidecar, `rotate`, `_detect_new_boot`, the database and MCP surfaces are unchanged. No
   migration. The value format changes, but every consumer compares it as an opaque string
   (`rotation._detect_new_boot`); nothing parses it. The marker is non-contract host runtime
   state in the existing `KDIVE_LIBVIRT_CONSOLE_ROOT`, which the units' `ReadWritePaths` already
   cover. It needs no setting, env knob or provisioning change. It follows the log's lifecycle;
   nothing in `src/` reclaims console logs, so no new reclaim is added. The authority-fixture
   script that removes `<id>.log` also removes `<id>.boot`.

### Behavior

- Ordinary growth, including crash output, changes only mtime. Before this change, any rotation
  pass that observed new output bumped the generation. Now growth keeps the generation.
- A power-cycle writes a new nonce, so a new boot that regrows past the prior offset before
  the next rotation still starts a new generation. A shrink below the offset still resets
  through the existing check.
- Systems started before this change have no marker. Their first rotation after upgrade sees
  `dev:ino:` instead of `dev:ino:mtime` and bumps the generation once, then stays stable. A
  console observer that brackets the upgrade sees one `generation_change`. Mixed old/new
  workers are unsupported: an old worker's start keeps a stale nonce and only the shrink check
  remains.
- Accepted residuals: the identity is read when the job is enqueued and the log is read when it
  is handled, and that skew predates this change. A marker that becomes readable between two
  passes causes one extra reset. Neither duplicates evidence beyond one re-cut generation.

### Validation

Red-green focused tests: identity stability across appends with an advanced mtime; identity
change across a second prepare with regrowth past the prior size; rotation keeping the
generation on growth and bumping it on prepare+regrow; a nonce from both truncating entry points
(the seam and the readiness window); marker atomic write, mode and failure category; the
symlinked, FIFO, foreign-owned, oversized and malformed marker degrading to an empty nonce; and
the unchanged stat-failure degradation. ADR-0576 gains an amendment. The genuine force-crash cell is
the end-to-end proof.

## Amendment 2 (2026-10-20): crash-kernel evidence on kdump-armed guests

### Evidence

The rerun with amendment 1 deployed kept the console identity stable through the crash: about
25 rotations after the crash carried the same nonce, and the only generation change was the
cell's own reboot into the fixture kernel. It still failed (`deadline`). The serial log runs from
the login prompt straight into the kdump crash kernel's boot banner (`kdive login: [ 0.000000]
Linux version 6.18.54-g1b357ecb3213 …`). Its next record is `Command line: elfcorehdr=0x… …
irqpoll nr_cpus=1 reset_devices …`, and the log later reaches `kdump[…]: saving vmcore
complete`. It has no `Kernel panic`, `sysrq` or `panic` text at all. With kdump loaded, the
crashing kernel enters the crash kernel before its console output is flushed, so the panic
exists only in the dump.

The one eligible post-crash part (`console-part-1-000001`, 61,440 bytes read in four windows)
holds that banner and the `elfcorehdr=` command line. It does not hold the saved line. A part
seals only when 64 KiB are pending, minus the 4 KiB held back (`rotation.py`). The kdump final
action powers the guest off about 3.8 KB after the saved line, so the tail is never sealed. A
contract that waits for the saved line cannot pass.

### Contract

The force-crash observation accepts, in this order, over the closed post-crash stream (same
generation, consecutive parts after the baseline high-water mark, unchanged rules 1–6):

1. **Console panic:** unchanged `panic_seen`. The panic marker, and the fixture release on its
   `CPU:` record within the existing line window.
2. **Crash kernel:** a kernel boot banner `[    0.000000] Linux version <release>`. It may start
   mid-line after an unterminated prompt, and the release must match exactly, bounded as in
   `panic_seen`. The first `Command line:` record after that banner must carry `elfcorehdr=` as
   a parameter, and the record must be complete (newline-terminated). Only kexec into a
   crash-dump kernel passes `elfcorehdr=`. A normal boot or reboot into the fixture kernel does
   not, so it cannot satisfy this form. The `Command line:` record is x86's (`setup_arch`);
   force-crash cells run only on x86_64 today, and another arch would match its
   `Kernel command line:` record instead.

Anything else stays a failure: no marker, a normal fixture boot, a banner for another release,
a release-prefix collision, a crash-kernel command line before any banner, an unterminated
command line, or a generation change (`generation_change`). Kexec into the crash kernel does not
pass through a start path, so it keeps the console generation under amendment 1.

`force_crash_body` passes the combined predicate to the observer. It then re-checks the
returned stream with the same predicate and classifies the form. The result records
`crash_evidence: "console_panic" | "crash_kernel"` in place of the former `console_panic`
flag; the only reader is the scenario's own unit test.

What each form proves:

- The crashed kernel's identity is already bound before the crash by the cell's boot-identity
  check and `armed()`.
- The banner binds the crash kernel's release. On this fixture that is the fixture kernel,
  because it loads itself as its crash kernel.
- Form 2 shows the crash entered kdump's capture kernel. It does not show that the dump was
  written; the vmcore cells own dump capture and fetch.
- Reading the panic from the dump's dmesg is not part of this cell.

### Residuals

- A generation starts at truncation, so its boot banner is in part index 0, which is always in
  the baseline (an empty baseline fails, rule 1). The carry ahead of the first post-crash part
  is therefore later output of the same boot. That boot's own banner has no `elfcorehdr=`.
  Only an earlier crash-kernel boot in the same generation could satisfy form 2 early. The cell
  boots a fresh generation and crashes once.
- The guest is the harness's own fixture. Guest user space printing a fake banner and command
  line to the serial console is an accepted residual.
- Production console panic detection has the same blind spot on kdump-armed guests. The
  live-testing runbook records it; changing production detection is out of scope.

### Validation

Red-green tests use sanitized excerpts of real console content:

- **Rerun's sealed post-crash part:** must match form 2. The predicate is also run over the
  retained observation records of that run, to show it would have passed.
- **First run's pre-crash boot snapshot:** has the fixture banner with a normal command line
  and must not match.

Negative cases also cover:

- a part that ends inside the `Command line:` record;
- a banner for another release, or a release prefix;
- a crash-kernel command line before any banner;
- kdump text without a banner, such as `saving vmcore-dmesg.txt complete`.
