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
`artifacts` row whose object key is `…/console-part-<gen>-<index>`, where `<gen>` is the boot generation
(R6b) and `<index>` is a **zero-padded, monotonic per-generation part index** carried in the rotation
sidecar (R6a) and advanced only after a part's row commits, so a crash-retry reuses the same index
(R6c). The padded `(gen, index)` orders an object-store listing numerically, but it is **not**
what `artifacts.list` orders by (that is `created_at DESC`, R8) — an agent identifies the live tail by
the maximum `(gen, index)` among the returned object keys (`refs.object`), not by list position. A
sealed part is immutable. The in-flight tail (the held-back seam carry plus any sub-threshold remainder, not yet sealed) is not
a row.

R2. **Bounded part size.** A part covers at most one rotation threshold of **plaintext**
(`DEFAULT_ROTATION_THRESHOLD = 64 KiB`, the existing collector constant); its stored redacted size may
differ (redaction is not length-preserving) but stays close to one `artifacts.get` inline window after
inflation.

R3. **Redaction before storage, durable seam.** The console bytes are untrusted guest output. Redaction
runs on the plaintext before the part is stored, so every part object is `REDACTED`. Seam safety reuses
the remote collector's proven carry mechanism (`collector._rotate`, ADR-0095): the last `SEAM_OVERLAP`
**raw** bytes of each part are held back and emitted — redacted — prepended to the **next** part, so a
secret straddling any boundary (internal or job) is redacted contiguously and the overlap is emitted
exactly once, never raw. Because the local rotation job is stateless across invocations, this held-back
raw `carry` is persisted in the sidecar (R6a) so the next job reproduces the collector's in-memory
behavior. There is **no** "redact a prefix and drop its length" subtraction (that misaligns precisely
when a secret straddles the boundary).

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
the worker owns the host console file it alone can read, ADR-0223). The job reads the plaintext bytes
**past a tracked plaintext offset**, prepends the held-back raw `carry` from the sidecar, and feeds the
result through the shared seam-carry primitive (R3) to seal full parts and produce a new `carry`. The
sidecar advances by the plaintext consumed. Re-redacting already-emitted bytes is never needed: the
held-back `carry` is the only re-considered region, and it is emitted (redacted) exactly once with the
part that follows it. The redactor is not length-preserving (it replaces a secret with the fixed string
`[REDACTED]`), so part keys are a monotonic index (R1), never a byte-length offset.

R6a. **Rotation state.** The sidecar (a small internal object-store object, not a DB row, so no DB
migration) holds `{plaintext_offset, carry (the held-back raw seam bytes, base64), next_index,
boot_gen, boot_id}`. The worker reads it at the start of a rotation job and writes it back after the
delta's parts commit. The `carry` may contain a partial unredacted secret, so the sidecar object is
internal — never registered as an `artifacts` row and never returned by `artifacts.get`/`list` — and is
removed at teardown (R9-adjacent, see the plan's teardown task).

R6c. **Single-flight and idempotent retry.** A `console_rotate` job holds the **per-System advisory
lock** (the platform's per-System serialization, CLAUDE.md) for the whole read-sidecar → seal-parts →
write-sidecar critical section, so two rotations for one System never overlap even if the reconciler
dedup races a dispatch. The sidecar (object store) and the part rows (Postgres) are different stores with
no shared transaction, so the sidecar (carrying `next_index`) advances **only after** the delta's part
rows commit. On a crash between committing the part rows and writing the sidecar, the retry reads the
**unchanged** sidecar (same `plaintext_offset`/`carry`/`next_index`), reproduces the identical parts, and
re-registers the **same** `(gen, index)` keys insert-if-absent (like the per-Run evidence path's
`_existing_console_row`) — a true no-op. The `next_index` is carried in the sidecar and advanced only on
commit; it is **not** re-derived from the existing part rows (that anti-pattern would assign new indices
to the re-sealed bytes and duplicate them).

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
tail from the leading part(s) with `artifacts.get` and searches history with `artifacts.search_text`
per part (it is per-artifact, R8a).
No new MCP tool, no new public field beyond the additional `artifacts.list` rows. Note: `artifacts.list`
orders by `created_at`, **not** by object key — the zero-padded index orders the object store and
disambiguates a part from the per-Run `console-<run>` evidence by key prefix, but it does not drive the
`artifacts.list` order.

R8a. **Bounded retention and search ergonomics, disclosed.** Console parts are owned by the System
(`owner_kind='systems'`) and are reclaimed at **teardown** (the teardown handler deletes the
System's console-part rows + objects and the sidecar). There is **no in-life expiry sweep**: the
existing artifact-expiry reconciler (#768) pins `owner_kind='runs'` and deliberately excludes
system-owned `console`/`vmcore` evidence (`reconciler/cleanup/gc.py`), so console parts grow for the
System's lifetime and are bounded only by teardown — a long-lived, chatty System accumulates parts
until it is torn down. This work does **not** add `artifacts.list` pagination (`data.truncated` stays
`False`), so a chatty multi-hour run can make `artifacts.list` return many part rows. `artifacts.get`
on the newest part (cheap, newest-first) covers the live-tail case. For finding an older event across
the run, note that `artifacts.search_text` is **per-artifact** (keyed by one `artifact_id`,
`reads.py`) — it does **not** span a System's parts — so cross-run search means listing the parts and
searching (or downloading via `download_uri`) each, newest-first. A System-spanning console search and a
hard per-System live-part cap are named as possible future work, not built here; this spec does not
claim either exists.

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
byte-identical (R5/R9). Remote shares the one key grammar `console-part-<gen>-<index>`: remote has no
per-power-cycle truncation, so `<gen>` is fixed at `0`, and `<index>` is the collector's existing
monotonic part index (`_take_index`, which already resumes past the highest part on failover, so
failover does not collide keys). The local-only generation counter (R6b) is what makes `<gen>` vary;
remote never increments it. The collector's `_rotate` carry logic is the shared seam primitive both
providers call (R3).

### Local-libvirt (reconciler-dispatched worker rotation job)

The reconciler's periodic sweep — which already enumerates Systems for drift repair — selects **live**
local-libvirt Systems (booted/`ready`, not torn down; **independent of whether their most recent Run is
terminal** — see R6) and dispatches a per-System console-rotation **worker job**
(`JobKind.CONSOLE_ROTATE`). To avoid piling up jobs over a long-lived
System, the reconciler skips dispatch when a `console_rotate` job for that System is already
pending/running, so at most one is in flight per System. The reconciler decides *when* and *for which
System*; the worker does the host-file I/O it alone can read (ADR-0223). The job:

1. reads the rotation sidecar (`plaintext_offset`, `carry`, `next_index`, `boot_gen`, `boot_id`; absent
   → zero state), then `os.stat`s `console_log_path(system_id)` for the size and reads `boot_id` from
   the job payload (R6b);
2. if the payload `boot_id` differs from the sidecar's **or** `file_size < plaintext_offset`, treats it
   as a power-cycle (ADR-0258): resets `plaintext_offset`/`next_index`/`carry`, increments `boot_gen`
   (R6b);
3. forms `pending = carry + file_bytes[plaintext_offset:]` and feeds it through the shared seam-carry
   primitive (R3): it seals full parts as `redact(carry_i + chunk_i)` while holding back each part's
   trailing `SEAM_OVERLAP` raw bytes to prepend (redacted) to the next part, and returns the leftover
   raw tail as the new `carry`;
4. registers each sealed part keyed `console-part-<gen>-<index>` (R1, R6c — monotonic `next_index`,
   incremented per part), insert-if-absent (gzip-compressed object, `content_encoding=gzip` metadata);
5. advances `plaintext_offset` to the file end and stores the new `carry`/`next_index`, writing the
   sidecar back only after the sealed parts' rows commit (R6c).

The whole job runs under the per-System advisory lock (R6c). `read_console_log`'s `CONFIGURATION_ERROR`
raise on a permission wall is caught and logged once; the job produces no parts and does not fail (R7).

### Reuse, not reinvention

The 64 KiB threshold and the seam-overlap **carry** are the remote collector's existing mechanism
(`collector._rotate`, ADR-0095) — Task 4 extracts that logic into a shared primitive both the local job
and the collector call, so the seam guarantee has one implementation, not two. The artifact-row
registration reuses `register_artifact_row` (the per-Run evidence path's helper).

## Acceptance criteria

- A sealed console part is a System-owned `REDACTED` `artifacts` row keyed
  `console-part-<gen>-<index>` (`<index>` = zero-padded monotonic per-gen index from the sidecar);
  `artifacts.list(system_id)` returns it ordered by `created_at DESC`, and the tail is the
  maximum `(gen, index)` among the returned `refs.object` keys (not the list position). (R1, R8)
- A part covers at most one rotation threshold (64 KiB) of plaintext; its redacted size may differ. (R2)
- Part bytes are redacted before storage; a secret straddling a part boundary — internal or job — is
  stored redacted on both sides (the collector's carry mechanism, R3). (R3)
- `artifacts.get` on a part whose `head` metadata is `content_encoding=gzip` returns the inflated,
  windowed, redacted content with correct `next_offset` paging and the `REDACTED` gate intact; a
  gzip-stored part is never returned as raw gzip bytes inline. An artifact without that metadata is read
  byte-identical to before (detection is metadata-driven, not key-driven). (R4)
- Remote-libvirt registers a separate compressed `console-part-0-<index>` artifact per sealed part;
  the collector's internal `console-parts-<n>` objects and the `finalize()` assembly are byte-identical,
  so `console-<run>` evidence is unchanged. (R5, R9)
- Two concurrent rotation attempts for one System cannot both seal parts (per-System advisory lock); a
  job retried after a crash between committing part rows and writing the sidecar re-seals nothing — it
  reads the unchanged sidecar (`plaintext_offset`/`carry`/`next_index`), reproduces the same `(gen,
  index)` keys, and registers insert-if-absent. Re-deriving `next_index` from existing rows would fail
  this test. (R6c)
- A secret value split across a rotation-**job** boundary is stored redacted on both sides: the held-back
  raw `carry` from the prior job is persisted in the sidecar and emitted (redacted) with the next part
  (R3). (R3)
- Rotation continues for a live System whose most recent Run is `succeeded` (the #892 case): a workload
  still emitting console after Run success keeps producing new parts until the System is torn down. (R6)
- Local-libvirt produces sealed part rows for a running System as its console grows, feeding
  `carry + file_bytes[plaintext_offset:]` through the shared carry primitive; a second sweep with
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

- **Part-row count on a chatty long-lived System.** Many ~64 KiB parts produce many `artifacts.list`
  rows, and `artifacts.list` is **not paginated** (`data.truncated` is always `False`, no `LIMIT` in
  `_LIST_REDACTED_SYSTEM_SQL`). There is **no in-life expiry** (the #768 sweeps are run-owned and
  exclude system-owned console evidence, R8a), so the series is bounded only by **teardown** reclaim and
  by the newest-first order making the recent tail cheap; the non-enumerating history path is
  `artifacts.search_text`. Disclosed in R8a as an accepted limitation — a per-System live-part cap or
  real `artifacts.list` pagination is future work, not built here. This is the append-only cost vs. the
  rejected single-mutable-artifact's O(size) re-upload.
- **Local rotation cadence vs. tail latency.** The rotation job runs only when the reconciler sweep
  dispatches it; the live tail lags by up to one sweep interval plus the time for a sub-threshold tail
  to cross 64 KiB. This is the inherent latency of size-threshold rotation, shared with remote; the
  sub-threshold tail is surfaced at the next crossing or at teardown.
- **Seam-carry correctness.** The seam guarantee comes from the collector's proven carry mechanism
  reused via one shared primitive (R3), not a re-derived byte arithmetic. The load-bearing invariant the
  unit tests pin: a secret straddling any part boundary (internal or job) is redacted contiguously and
  never stored raw — including the persisted `carry` across stateless job invocations. A bug that
  dropped a redacted prefix by length (rather than carrying raw bytes forward) would misalign on a
  straddling secret and leak it; the internal- and job-boundary seam tests guard against this.
- **Compression vs. `download_uri`.** The minted URL serves the compressed object; an agent fetching it
  directly gets gzip bytes. Inline `artifacts.get` content is always inflated; the download path is for
  whole-object retrieval where the agent (or a `Content-Encoding` follow-up) handles inflation. Noted
  so the download contract is explicit.
- **Wrong tail identification.** `artifacts.list` orders by `created_at DESC`, and parts sealed in one
  rotation run can share a `created_at`, so list position is not a reliable strict order within a batch.
  The agent therefore selects the tail by the maximum `(gen, index)` among the returned
  `console-part-<gen>-<index>` keys (exposed as `refs.object`), distinguishing parts from the per-Run
  `console-<run>` evidence by key prefix. This is the deterministic tail rule, not "the first listed row".
