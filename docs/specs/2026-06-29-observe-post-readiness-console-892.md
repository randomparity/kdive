# Spec: post-readiness console observation via rotating part artifacts (#892)

- Issue: #892
- ADR: [ADR-0273](../adr/0273-observe-rotating-console-parts.md)
- Status: Draft

## Problem

`~/src/linux/BLACK_BOX_REVIEW.md` reports that a Run reached `ready` at the `kdive-ready` console
marker before a custom-initrd workload finished, and the exposed console then appeared to stop near
that marker. Follow-up black-box evidence confirmed it: repeated `runs.get` returned the same
`refs.console` artifact, and paging `artifacts.get` to EOF produced a 34,495-byte console whose final
lines ended shortly after `cgwb-repro iter=0`, while the System was `ready` and the active Run had
`succeeded`. The surface did not show whether the guest workload was still running after boot
readiness.

Root cause: the per-Run console artifact (`console-<run>`, ADR-0235) is an immutable snapshot the boot
worker assembles **once**, at boot-step completion. The console keeps growing on the provider side
after that, but nothing re-captures it into anything an agent can read:

- Local-libvirt: the live console is a worker-host file (`console_log_path`, truncated per
  power-cycle, ADR-0258) that keeps growing as the workload runs. Only the worker can read it
  (ADR-0223). It is snapshotted into the per-Run evidence once and never again.
- Remote-libvirt: the reconciler-resident `ConsoleCollector` already streams and rotates the console
  into numbered, redacted, 64 KiB parts in the object store (`console-parts-<n>`, ADR-0095), but those
  parts are collector-internal — only their assembled concatenation becomes an `artifacts` row.

The read/search/download primitives an agent needs already exist: `artifacts.list` enumerates a
System's redacted artifacts, `artifacts.get` returns a byte-windowed redacted slice with paging and a
`download_uri`, and `artifacts.search_text` searches content. They have nothing to read because the
live console is never registered as durable artifacts.

## Goal

Expose the live console after boot readiness as **append-only, redacted, ~64 KiB System-owned console
*part* artifacts**, observable through the existing `artifacts.{list,get,search_text}` surface, with
**no new MCP tool**. The per-Run console evidence stays immutable and unchanged. This is the issue's
"console tail/read-current" option, realized on the existing artifact surface and the existing remote
parts model rather than a new tool.

## Non-goals

- A new MCP tool. The platform already has too many tools (a top-level review is pending); the design
  reuses `artifacts.{list,get,search_text}`.
- Mutating or refreshing the immutable per-Run `console-<run>` evidence (ADR-0235). It stays the frozen
  boot-window assembly; `refs.console` on `runs.get` is unchanged.
- Live push streaming. Paging an append-only part series over `artifacts.get` meets the need without a
  new transport.
- A new error category, RBAC role, or destructive-op gate. Console parts reuse the existing
  System-scoped `artifacts.*` authorization.

## Requirements

R1. **Part artifacts.** A sealed console part is a System-owned (`owner_kind='systems'`) `REDACTED`
`artifacts` row whose object key is `…/console-part-<gen>-<start>`, where `<gen>` is the boot generation
(R6b) and `<start>` is the **zero-padded plaintext start offset** of the bytes the part covers — a
deterministic function of position, not a free-running counter (so re-sealing the same delta yields the
same key, R6c). The padded `(gen, start)` orders an object-store listing numerically, but it is **not**
what `artifacts.list` orders by (that is `created_at DESC`, R8) — an agent identifies the live tail by
the maximum `(gen, start)` among the returned object keys (`refs.object`), not by list position. A
sealed part is immutable. The in-flight tail (bytes below the rotation threshold, not yet sealed) is not
a row.

R2. **Bounded part size.** A part holds at most the rotation threshold of redacted bytes
(`DEFAULT_ROTATION_THRESHOLD = 64 KiB`, the existing collector constant), so each part is one
`artifacts.get` inline window after inflation.

R3. **Redaction before storage, durable seam.** The console bytes are untrusted guest output. Redaction
runs on the plaintext before the part is stored, so every part object is `REDACTED`. A secret straddling
a part boundary **within one job** is held back and redacted with the next part (the collector's
seam-overlap rule, ADR-0095). The local rotation job is stateless across invocations (its only persisted
state is the sidecar offset), so the in-memory carry the remote collector relies on does not survive
across jobs; to protect a secret split **across a job boundary**, each local job re-reads a fixed
overlap window of already-consumed plaintext before its start offset (`read [offset - SEAM_OVERLAP,
file_size]`, with `SEAM_OVERLAP` ≥ the collector's seam window) and redacts the combined window, but
**only seals/advances for bytes ≥ offset** — the overlap is re-read for redaction context and never
re-emitted. A secret is therefore never stored raw on either side of a part or a job seam.

R4. **Compression: decompress-on-read, metadata-driven.** A sealed part is gzip-compressed in the
object store and tagged with a `content_encoding=gzip` **user-metadata** entry at write time (alongside
the existing `sensitivity`/`retention_class` metadata the store already records). `artifacts.get`
inflates strictly when `head` reports `content_encoding=gzip` — it never parses the object key, so the
generic reader stays kind-agnostic and a non-`gzip` artifact takes the existing raw path byte-for-byte.
After inflation the windowed-read, `next_offset` paging, `download_uri`, and `sensitivity == REDACTED`
gate semantics are preserved against the inflated plaintext. (`content_encoding` is object user-metadata,
not a DB column — no migration.)

R5. **Remote-libvirt capture.** When the reconciler-resident collector seals a part, it additionally
writes a **separate** compressed part artifact (a new `console-part-<gen>-<index>` object + `artifacts`
row) — distinct from the collector's internal `console-parts-<n>` object that `finalize()` concatenates
raw into the per-Run evidence (`collector.py` `finalize` reads `read_part` and concatenates). The
internal parts and the finalize assembly are therefore **byte-for-byte unchanged**; this work only adds
a parallel compressed, registered copy per sealed part. (Compressing the internal parts in place would
make `finalize()` concatenate gzip streams and corrupt the immutable `console-<run>` evidence — that is
explicitly not done, R9.)

R6. **Local-libvirt capture.** The reconciler's periodic sweep discovers **live** local-libvirt Systems
— a System that is booted/`ready` and not torn down — and dispatches a per-System console-rotation
**worker job**. Liveness is keyed on **System** state, not on a Run being non-terminal: the #892 repro
had the System `ready` with the most recent Run already `succeeded` while the in-guest workload kept
emitting console, so a succeeded (or otherwise terminal) Run must **not** stop rotation. Rotation stops
only when the System is torn down. (The reconciler owns periodic discovery;
the worker owns the host console file it alone can read, ADR-0223). The job reads only the plaintext
bytes **past a tracked plaintext offset**, redacts that delta with a held-back seam overlap (mirroring
the collector, ADR-0095, so a secret straddling a read boundary is never stored raw), seals full
~64 KiB parts from the **redacted delta**, registers each as a part row, and advances the offset.
Re-redacting already-sealed bytes is forbidden: the redactor replaces a secret with the fixed string
`[REDACTED]` and so is **not length-preserving** — a resume offset can never be the sum of redacted
part sizes (that conflates redacted-output length with plaintext-file length), and a sealed part is
derived once from its redacted delta and never recomputed.

R6a. **Rotation state.** The plaintext offset, the next part index, and a boot-generation marker are
persisted per System in a small internal object-store **sidecar** object (not a DB row), so there is no
DB migration for the offset. The worker reads the sidecar at the start of a rotation job and writes it
back after the delta's parts commit. (Deriving the offset from the part rows is rejected — units
mismatch, R6.)

R6c. **Single-flight and idempotent retry.** A `console_rotate` job holds the **per-System advisory
lock** (the platform's per-System serialization, CLAUDE.md) for the whole read-sidecar → seal-parts →
write-sidecar critical section, so two rotations for one System never overlap even if the reconciler
dedup races a dispatch. The sidecar (object store) and the part rows (Postgres) are different stores with
no shared transaction, so the offset advances **only after** the delta's part rows commit. Idempotency
is guaranteed by the **offset-derived key**: a part's key is `console-part-<gen>-<start>` where `<start>`
is the plaintext start offset it covers (R1). On a crash between committing the part rows and writing the
sidecar, the retry re-reads the same un-advanced delta and produces parts with the **same** `(gen,
start)` keys; registration is insert-if-absent (like the per-Run evidence path's `_existing_console_row`),
so the re-seal is a true no-op — no duplicated content and no key collision. The key must **not** be a
free-running counter, which would assign new keys to the re-sealed bytes and duplicate them.

R6b. **Power-cycle detection.** The local console file holds only the current boot: libvirt renders
`<log append='off'>` and truncates it on every domain power-cycle (ADR-0258). A rotation job detects a
new boot by a **boot-identity signal independent of current file size** — recorded as `boot_id` in the
sidecar — not by size alone. Size comparison (`file_size < plaintext_offset`) alone is racy: a
truncate-then-regrow that crosses the old offset between two sweeps would never be observed as a shrink,
so the new boot's early console (its `kdive-ready` marker, an early panic) would be skipped and the rest
mislabeled. The `boot_id` is a stable per-boot signal (e.g. the libvirt domain's boot/start identity or
timestamp, the file inode/identity, or a hash of the file's first N bytes — the boot banner changes per
boot); the precise source is chosen in the plan, but it MUST flip on every power-cycle regardless of
whether the new file has already grown past the old offset. On a `boot_id` change **or** a size shrink,
the job resets the plaintext offset to 0, increments the boot generation, and records the new `boot_id`,
starting a fresh `console-part-<gen+1>-…` series. A reboot during a long-running workload (an in-guest
reboot, `runs.power`, or `force_crash` to trigger the bug) therefore neither strands the new boot's
console nor corrupts the offset mapping.

R7. **Best-effort capture.** Part capture (both providers) never fails the workload, the boot, the
rotation job, or any tool call. A store outage or an absent console produces no new parts. A permission
wall is the `CONFIGURATION_ERROR` that `read_console_log` **raises** under ADR-0223; the rotation job
catches it, logs once (not per sweep), and produces no parts. The existing per-Run evidence path is
unaffected in every case.

R8. **Observation surface (no new tool).** `artifacts.list(system_id)` returns the System's redacted
artifacts ordered by `created_at DESC`, so the newest console part is first; an agent reads the live
tail from the leading part(s) with `artifacts.get` and searches history with `artifacts.search_text`.
No new MCP tool, no new public field beyond the additional `artifacts.list` rows. Note: `artifacts.list`
orders by `created_at`, **not** by object key — the zero-padded index orders the object store and
disambiguates a part from the per-Run `console-<run>` evidence by key prefix, but it does not drive the
`artifacts.list` order.

R8a. **Bounded retention, disclosed.** Live console parts are ordinary artifacts and age out through the
existing artifact-expiry reconciler (#768), which time-bounds the series. This work does **not** add
`artifacts.list` pagination (`data.truncated` stays `False`), so within a retention window a chatty
multi-hour run can make `artifacts.list` return many part rows; the agent's non-enumerating path for
"find the problem" is `artifacts.search_text`, and the newest-first order makes the recent tail cheap.
A hard per-System live-part cap is named as possible future work, not built here.

R9. **Per-Run evidence immutability.** `console-<run>` (ADR-0235) is neither mutated nor re-snapshotted;
the live parts are a separate System-owned series. Both stay immutable.

R10. **One additive migration; no RBAC, tool-surface, or config-setting change.** Console parts are
ordinary `artifacts` rows and the local rotation state lives in an object-store sidecar (R6a), not a DB
column. The only schema change is migration **0053**, which widens the additive `jobs_kind_check`
CHECK constraint to admit the new internal `console_rotate` `JobKind` (forward-only, ADR-0015, exactly
as 0051/0052 did for prior job kinds). The rotation job is internal (reconciler-dispatched), not an
agent-facing tool, so it adds no MCP surface and no RBAC role; it is **not** a destructive job kind.

## Approach

### Part object keying and ordering

A console part's object key is `…/console-part-<gen>-<index>` where `<gen>` is the boot generation
(R6b) and `<index>` is zero-padded (e.g. 6 digits) so object-store listings are numerically ordered.
The part row is `owner_kind='systems'`, `owner_id=<system_id>`, `sensitivity=REDACTED`, mirroring the
per-Run evidence row's owner shape. `artifacts.list(system_id)` filters to `REDACTED` System-owned rows
and orders them `created_at DESC` (`services/artifacts/listing.py`), so the newest part is first and a
part is distinguished from the frozen `console-<run>` evidence by its key prefix. The zero-padded index
is the object-store order and the within-generation sequence; it is **not** what `artifacts.list`
orders by.

### Compression in `artifacts.get` (`mcp/tools/catalog/artifacts/reads.py`)

`_artifact_content` reads the object `head` (which already carries `sensitivity`); when the head's
user-metadata reports `content_encoding=gzip`, it inflates the fetched bytes after the
`REDACTED`-sensitivity gate and before windowing. Detection is **metadata-driven, not key-driven**, so
the generic reader never special-cases the console-part key and a non-`gzip` artifact is byte-for-byte
unchanged. The `download_uri` is still minted (it serves the compressed object; the agent inflates, or a
future enhancement sets the HTTP `Content-Encoding` header — noted in Risks). The windowed inline
`content`, `content_truncated`, and `next_offset` are computed on the inflated plaintext, so paging
semantics are unchanged. This requires the object-store `head` to surface user-metadata
`content_encoding` (an additive read of metadata the put already supports), not a new DB column.

### Remote-libvirt (`providers/remote_libvirt/console/collector.py`, reconciler wiring)

On `put_part`, the collector continues to write its internal `console-parts-<n>` object **unchanged**
(this is what `finalize()` reads and concatenates raw into `console-<run>`), and **additionally** writes
a separate gzip-compressed registered part object and registers its `artifacts` row on the reconciler's
DB connection. The redacted bytes already exist; they are compressed only for the new registered copy.
Because the internal parts and `finalize()`'s assembly are untouched, the per-Run evidence stays
byte-identical (R5/R9). Remote shares the one key grammar `console-part-<gen>-<start>`: remote has no
per-power-cycle truncation, so `<gen>` is fixed at `0`, and `<start>` is the registered part's byte
offset in the assembled stream (the collector's resume already keeps part offsets monotonic per System,
so failover does not collide keys). The local-only generation counter (R6b) is what makes `<gen>` vary;
remote never increments it.

### Local-libvirt (reconciler-dispatched worker rotation job)

The reconciler's periodic sweep — which already enumerates Systems for drift repair — selects **live**
local-libvirt Systems (booted/`ready`, not torn down; **independent of whether their most recent Run is
terminal** — see R6) and dispatches a per-System console-rotation **worker job**
(`JobKind.CONSOLE_ROTATE`). To avoid piling up jobs over a long-lived
System, the reconciler skips dispatch when a `console_rotate` job for that System is already
pending/running, so at most one is in flight per System. The reconciler decides *when* and *for which
System*; the worker does the host-file I/O it alone can read (ADR-0223). The job:

1. reads the rotation sidecar (`plaintext_offset`, `boot_gen`, `boot_id`; absent → offset 0, gen 0, no
   id), then `os.stat`s `console_log_path(system_id)` for the size and reads a boot-identity signal
   (R6b);
2. if the boot identity differs from the sidecar's `boot_id` **or** `file_size < plaintext_offset`,
   treats it as a power-cycle (ADR-0258): resets `plaintext_offset` to 0, increments `boot_gen`, and
   records the new `boot_id` (R6b);
3. reads a redaction window `[max(0, plaintext_offset - SEAM_OVERLAP) : file_size]`, redacts the whole
   window once (R3/R6), then **discards the overlap prefix** so only redacted bytes corresponding to
   plaintext ≥ `plaintext_offset` are eligible to seal — the overlap is re-read for redaction context
   across the job seam and never re-emitted;
4. seals full ~64 KiB parts from the eligible redacted bytes, keying each
   `console-part-<gen>-<start>` where `<start>` is the part's **plaintext start offset** (R1, R6c —
   offset-derived, not a counter); registration is insert-if-absent (gzip-compressed object,
   `content_encoding=gzip` metadata). The trailing sub-threshold remainder stays the unsealed tail,
   surfaced when it next crosses the threshold or at teardown;
5. advances `plaintext_offset` by the plaintext bytes consumed (**not** the redacted size) and writes
   the sidecar back, only after the sealed parts' rows commit (R6c).

The whole job runs under the per-System advisory lock (R6c). `read_console_log`'s `CONFIGURATION_ERROR`
raise on a permission wall is caught and logged once; the job produces no parts and does not fail (R7).
The job never *emits* bytes below `plaintext_offset` and keys parts by plaintext offset, so a
non-length-preserving redactor (`[REDACTED]`, `security/secrets/redaction.py`) cannot shift or duplicate
a sealed part's content.

### Reuse, not reinvention

The 64 KiB threshold and the seam-overlap redaction are the remote collector's existing mechanisms
(ADR-0095). The local rotation reuses the same threshold and redaction (re-reading the overlap window
across job seams instead of carrying it in memory, R3); the artifact-row registration reuses
`register_artifact_row` (the per-Run evidence path's helper).

## Acceptance criteria

- A sealed console part is a System-owned `REDACTED` `artifacts` row keyed
  `console-part-<gen>-<start>` (`<start>` = zero-padded plaintext start offset, deterministic from
  position); `artifacts.list(system_id)` returns it ordered by `created_at DESC`, and the tail is the
  maximum `(gen, start)` among the returned `refs.object` keys (not the list position). (R1, R8)
- A part holds at most 64 KiB of redacted bytes. (R2)
- Part bytes are redacted before storage; a secret straddling a rotation boundary is stored redacted on
  both sides (seam-overlap). (R3)
- `artifacts.get` on a part whose `head` metadata is `content_encoding=gzip` returns the inflated,
  windowed, redacted content with correct `next_offset` paging and the `REDACTED` gate intact; a
  gzip-stored part is never returned as raw gzip bytes inline. An artifact without that metadata is read
  byte-identical to before (detection is metadata-driven, not key-driven). (R4)
- Remote-libvirt registers a separate compressed `console-part-0-<start>` artifact per sealed part;
  the collector's internal `console-parts-<n>` objects and the `finalize()` assembly are byte-identical,
  so `console-<run>` evidence is unchanged. (R5, R9)
- Two concurrent rotation attempts for one System cannot both seal parts (per-System advisory lock); a
  job retried after a crash between committing part rows and writing the sidecar re-seals nothing — the
  re-read delta produces the same `(gen, start)` keys and registration is insert-if-absent (offset
  advanced only after parts commit). A free-running counter key would fail this test. (R6c)
- A secret value split across a rotation-**job** boundary is stored redacted on both sides: the local
  job re-reads a `SEAM_OVERLAP` window of already-consumed plaintext before its offset for redaction
  context, emitting only bytes ≥ offset. (R3)
- Rotation continues for a live System whose most recent Run is `succeeded` (the #892 case): a workload
  still emitting console after Run success keeps producing new parts until the System is torn down. (R6)
- Local-libvirt produces sealed part rows for a running System as its console grows, reading only the
  plaintext delta past the sidecar `plaintext_offset` and redacting that delta once; a second sweep with
  no new console growth produces no new parts, and no sealed part is ever recomputed/re-redacted. (R6,
  R6a)
- A rotation job detects a power-cycle by a `boot_id` change (a boot-identity signal independent of
  file size) **or** a size shrink, then resets the offset and increments the boot generation, starting a
  fresh `console-part-<gen+1>-…` series; a truncate-then-regrow that already crossed the old offset is
  still detected (via `boot_id`), the new boot's early console is captured, and the prior generation's
  parts are untouched. (R6b)
- A store outage / absent console / permission-wall `CONFIGURATION_ERROR` (caught from
  `read_console_log`) produces no new parts and surfaces no error to the agent or as a job failure; the
  per-Run evidence path is unaffected. (R7)
- An agent observes a post-readiness workload via `artifacts.list` (newest-first by `created_at`) →
  newest part → `artifacts.get` paging, and finds a later console line that the frozen per-Run evidence
  does not contain. (R8)
- `console-<run>` evidence is byte-identical before and after live parts exist for the same System;
  `refs.console` on `runs.get` is unchanged. (R9)
- One additive migration (0053, widening `jobs_kind_check` for `console_rotate`); no other schema
  change, no RBAC/tool-surface/config change; `console_rotate` is not a destructive job kind. (R10)
- Live (`live_vm`, operator-run): a local-libvirt System with a long-running post-readiness workload
  surfaces new console parts via `artifacts.list`/`artifacts.get` that show workload progress past the
  `kdive-ready` marker.

## Risks

- **Part-row count on a chatty 12-hour run.** Many ~64 KiB parts produce many `artifacts.list` rows,
  and `artifacts.list` is **not paginated** (`data.truncated` is always `False`, no `LIMIT` in
  `_LIST_REDACTED_SYSTEM_SQL`). Bounded only by artifact expiry (#768) over time and by the
  newest-first order making the recent tail cheap; the non-enumerating history path is
  `artifacts.search_text`. Disclosed in R8a as an accepted limitation — a per-System live-part cap or
  real `artifacts.list` pagination is future work, not built here. This is the append-only cost vs. the
  rejected single-mutable-artifact's O(size) re-upload.
- **Local rotation cadence vs. tail latency.** The rotation job runs only when the reconciler sweep
  dispatches it; the live tail lags by up to one sweep interval plus the time for a sub-threshold tail
  to cross 64 KiB. This is the inherent latency of size-threshold rotation, shared with remote; the
  sub-threshold tail is surfaced at the next crossing or at teardown.
- **Incremental redaction correctness.** The local job redacts only the plaintext delta and never
  re-reads sealed bytes (R6), so the non-length-preserving `[REDACTED]` substitution cannot shift a
  sealed part. A secret straddling a read boundary is caught by the held-back seam overlap (R3). A bug
  that advanced `plaintext_offset` by the **redacted** size instead of the plaintext size would silently
  drop or duplicate console bytes — called out as the load-bearing invariant the unit tests must pin.
- **Compression vs. `download_uri`.** The minted URL serves the compressed object; an agent fetching it
  directly gets gzip bytes. Inline `artifacts.get` content is always inflated; the download path is for
  whole-object retrieval where the agent (or a `Content-Encoding` follow-up) handles inflation. Noted
  so the download contract is explicit.
- **Wrong tail identification.** `artifacts.list` orders by `created_at DESC`, and parts sealed in one
  rotation run can share a `created_at`, so list position is not a reliable strict order within a batch.
  The agent therefore selects the tail by the maximum `(gen, index)` among the returned
  `console-part-<gen>-<index>` keys (exposed as `refs.object`), distinguishing parts from the per-Run
  `console-<run>` evidence by key prefix. This is the deterministic tail rule, not "the first listed row".
