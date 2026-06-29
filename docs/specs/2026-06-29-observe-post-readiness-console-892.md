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

R6. **Local-libvirt capture.** A worker-driven periodic rotation, for each running local-libvirt
System, reads the growing worker-host console file, redacts it, slices the redacted bytes at the
rotation threshold, and registers each newly sealed slice as a part row. The worker owns the file
(server/reconciler co-location is not assumed, ADR-0223). The resume offset for the next sweep is the
sum of the System's existing console-part rows' sizes — derived, not stored, so no migration.

R7. **Best-effort capture.** Part capture (both providers) never fails the workload, the boot, or any
tool call. A store outage, a permission wall (ADR-0223), or an absent console degrades to no new parts,
not an error surfaced to the agent; the existing per-Run evidence path is unaffected.

R8. **Observation surface (no new tool).** `artifacts.list(system_id)` returns the ordered console-part
series alongside the per-Run evidence; an agent reads the tail by taking the highest-index part and
paging it with `artifacts.get`, and searches history with `artifacts.search_text`. No new MCP tool, no
new public field beyond the additional `artifacts.list` rows.

R9. **Per-Run evidence immutability.** `console-<run>` (ADR-0235) is neither mutated nor re-snapshotted;
the live parts are a separate System-owned series. Both stay immutable.

R10. **No schema, migration, RBAC, tool-surface, or config-setting change** beyond the rotation
threshold reuse. Console parts are ordinary `artifacts` rows; the resume offset is derived (R6).

## Approach

### Part object keying and ordering

A console part's object key is `…/console-part-<index>` where `<index>` is zero-padded (e.g. 6 digits)
so lexical order is numeric order. The part row is `owner_kind='systems'`, `owner_id=<system_id>`,
`sensitivity=REDACTED`, mirroring the per-Run evidence row's owner shape. `artifacts.list(system_id)`
already filters to `REDACTED` System-owned rows, so the parts appear with the evidence; the agent
distinguishes the live tail (highest index) from the frozen evidence by key prefix.

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

### Local-libvirt (worker-driven rotation)

A worker periodic task, for each running local-libvirt System, reads `console_log_path(system_id)`,
redacts the whole current file, and slices it at the rotation threshold into sealed parts. It registers
only parts beyond the already-registered series: the resume offset is the sum of the existing
console-part rows' sizes (R6), so a sweep produces only the newly-grown sealed slices. The trailing
sub-threshold remainder is the unsealed tail and is left for the next sweep (so a slow workload's tail
appears once it crosses the threshold or the System tears down). A permission wall reading the file is
the ADR-0223 `CONFIGURATION_ERROR` path; capture degrades best-effort (R7).

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
- Local-libvirt produces sealed part rows for a running System as its console grows; the resume offset
  is the sum of existing part sizes (a second sweep with no new console growth produces no new parts).
  (R6)
- A store outage / permission wall / absent console produces no new parts and surfaces no error to the
  agent; the per-Run evidence path is unaffected. (R7)
- An agent observes a post-readiness workload via `artifacts.list` → newest part → `artifacts.get`
  paging, and finds a later console line that the frozen per-Run evidence does not contain. (R8)
- `console-<run>` evidence is byte-identical before and after live parts exist for the same System;
  `refs.console` on `runs.get` is unchanged. (R9)
- No migration file is added; no RBAC/tool-surface/config change. (R10)
- Live (`live_vm`, operator-run): a local-libvirt System with a long-running post-readiness workload
  surfaces new console parts via `artifacts.list`/`artifacts.get` that show workload progress past the
  `kdive-ready` marker.

## Risks

- **Part-row explosion on a chatty 12-hour run.** Many ~64 KiB parts produce many `artifacts.list`
  rows. Mitigated by newest-first reads (the tail is one part) and `artifacts.search_text` for history;
  `artifacts.list` already carries a `truncated` field for a future pagination need. Not blocking for
  the #892 repro, which is a single workload, but noted as the append-only cost (vs. the rejected
  single-mutable-artifact's O(size) re-upload).
- **Local worker-driven rotation cadence.** Too slow and the tail lags; too fast and it re-reads the
  file often. The file read is bounded (one file per running System per sweep) and best-effort; the
  cadence is a tuned interval, not a per-byte stream. A sub-threshold tail is not visible until it
  seals or the System tears down — an inherent latency of size-threshold rotation, shared with remote.
- **Compression vs. `download_uri`.** The minted URL serves the compressed object; an agent fetching it
  directly gets gzip bytes. Inline `artifacts.get` content is always inflated; the download path is for
  whole-object retrieval where the agent (or a `Content-Encoding` follow-up) handles inflation. Noted
  so the download contract is explicit.
- **Wrong tail identification.** An agent must read the highest-index part for the live tail, not the
  per-Run evidence. Mitigated by the distinct `console-part-<index>` key prefix and ordered listing.
