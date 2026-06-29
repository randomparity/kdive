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
`artifacts` row whose object key is `…/console-part-<index>` with a zero-padded index, so the object
store and `artifacts.list` order parts lexically. A sealed part is immutable. The in-flight tail
(bytes below the rotation threshold, not yet sealed) is not a row.

R2. **Bounded part size.** A part holds at most the rotation threshold of redacted bytes
(`DEFAULT_ROTATION_THRESHOLD = 64 KiB`, the existing collector constant), so each part is one
`artifacts.get` inline window after inflation.

R3. **Redaction before storage.** The console bytes are untrusted guest output. Redaction runs on the
plaintext before the part is stored, so every part object is `REDACTED`. A secret straddling a
rotation boundary is held back and redacted with the next part (the collector's existing seam-overlap
rule, ADR-0095), so it is never stored raw on either side of the seam.

R4. **Compression: decompress-on-read.** A sealed part is gzip-compressed in the object store.
`artifacts.get` inflates a console-part object transparently before windowing; the windowed-read,
`next_offset` paging, `download_uri`, and `sensitivity == REDACTED` gate semantics are preserved
against the inflated bytes. A non-console artifact is unaffected.

R5. **Remote-libvirt capture.** The reconciler-resident collector registers each part as an
`artifacts` row when it seals the part (it holds the DB connection). The existing rotation, redaction,
seam-overlap, and per-Run assembly are unchanged; this adds a row write per sealed part.

R6. **Local-libvirt capture.** The reconciler's periodic sweep discovers running local-libvirt Systems
and dispatches a per-System console-rotation **worker job** (the reconciler owns periodic discovery;
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
migration. The worker reads the sidecar at the start of a rotation job and writes it back after sealing
parts. (Deriving the offset from the part rows is rejected — units mismatch, R6.)

R6b. **Power-cycle truncation.** The local console file holds only the current boot: libvirt renders
`<log append='off'>` and truncates it on every domain power-cycle (ADR-0258). When a rotation job
observes the file **shorter than** the tracked plaintext offset, it treats this as a power-cycle —
resets the plaintext offset to 0 and increments the boot generation, starting a fresh part series for
the new boot. A reboot during a long-running workload (an in-guest reboot, `runs.power`, or
`force_crash` to trigger the bug) therefore neither strands the new boot's console nor corrupts the
index.

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

`_artifact_content` gzip-inflates a console-part object after the `REDACTED`-sensitivity head check and
before windowing. The inflate is keyed off the part object (key prefix or a stored content-encoding),
so non-console artifacts take the existing raw path unchanged. The `download_uri` is still minted (it
serves the compressed object; the agent inflates, or a future enhancement sets `Content-Encoding`).
The windowed inline `content`, `content_truncated`, and `next_offset` are computed on the inflated
plaintext, so paging semantics are unchanged.

### Remote-libvirt (`providers/remote_libvirt/console/collector.py`, reconciler wiring)

On `put_part`, also register the part `artifacts` row (gzip-compressed object) on the reconciler's DB
connection. The redacted bytes are already produced; compress before upload. Assembly into the per-Run
`console-<run>` evidence at finalize is unchanged.

### Local-libvirt (reconciler-dispatched worker rotation job)

The reconciler's periodic sweep — which already enumerates Systems for drift repair — selects
local-libvirt Systems that are running (`ready` with an active Run) and dispatches a per-System
console-rotation **worker job** (`JobKind.CONSOLE_ROTATE`). To avoid piling up jobs over a long-lived
System, the reconciler skips dispatch when a `console_rotate` job for that System is already
pending/running, so at most one is in flight per System. The reconciler decides *when* and *for which
System*; the worker does the host-file I/O it alone can read (ADR-0223). The job:

1. reads the rotation sidecar (`plaintext_offset`, `next_index`, `boot_gen`; absent → all zero), then
   `os.stat`s `console_log_path(system_id)`;
2. if `file_size < plaintext_offset`, treats it as a power-cycle (ADR-0258): resets `plaintext_offset`
   to 0 and `next_index` to 0, increments `boot_gen` (R6b);
3. reads the plaintext **delta** `[plaintext_offset : file_size]`, prepends any held-back seam-overlap
   bytes from the prior delta, and redacts the combined delta once (R3/R6);
4. seals full ~64 KiB parts from the redacted delta, registering each as a `console-part-<gen>-<index>`
   row (gzip-compressed object); the trailing sub-threshold remainder stays the unsealed tail (held
   back, surfaced when it next crosses the threshold or at teardown);
5. advances `plaintext_offset` by the plaintext bytes consumed (not the redacted size) and writes the
   sidecar back.

`read_console_log`'s `CONFIGURATION_ERROR` raise on a permission wall is caught and logged once; the
job produces no parts and does not fail (R7). The job never re-reads or re-redacts bytes below
`plaintext_offset`, so a non-length-preserving redactor (`[REDACTED]`,
`security/secrets/redaction.py`) cannot shift a sealed part's content.

### Reuse, not reinvention

The 64 KiB threshold, the seam-overlap redaction, and the part-index resume are the remote collector's
existing mechanisms (ADR-0095). The local rotation reuses the same threshold and redaction; the
artifact-row registration reuses `register_artifact_row` (the per-Run evidence path's helper).

## Acceptance criteria

- A sealed console part is a System-owned `REDACTED` `artifacts` row with a zero-padded
  `console-part-<index>` key; `artifacts.list(system_id)` returns the parts in index order. (R1)
- A part holds at most 64 KiB of redacted bytes. (R2)
- Part bytes are redacted before storage; a secret straddling a rotation boundary is stored redacted on
  both sides (seam-overlap). (R3)
- `artifacts.get` on a console part returns the inflated, windowed, redacted content with correct
  `next_offset` paging and the `REDACTED` gate intact; a gzip-stored part is never returned as raw gzip
  bytes inline. A non-console artifact read is byte-identical to before. (R4)
- Remote-libvirt registers a part row per sealed part; per-Run assembly is unchanged. (R5)
- Local-libvirt produces sealed part rows for a running System as its console grows, reading only the
  plaintext delta past the sidecar `plaintext_offset` and redacting that delta once; a second sweep with
  no new console growth produces no new parts, and no sealed part is ever recomputed/re-redacted. (R6,
  R6a)
- A rotation job that observes the console file shorter than the tracked `plaintext_offset` resets the
  offset and increments the boot generation, starting a fresh `console-part-<gen+1>-…` series; the new
  boot's console is captured and the prior generation's parts are untouched. (R6b)
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
- **Wrong tail identification.** An agent must read the highest-index part for the live tail, not the
  per-Run evidence. Mitigated by the distinct `console-part-<index>` key prefix and ordered listing.
