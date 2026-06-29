# ADR-0273: Post-readiness console observation via rotating System-owned part artifacts

- Status: Proposed
- Issue: #892
- Spec: [observe-post-readiness-console-892](../specs/2026-06-29-observe-post-readiness-console-892.md)
- Supersedes nothing; extends ADR-0235 (per-Run console evidence), ADR-0095 (remote console
  collector parts), ADR-0226/0247/0257 (`artifacts.get` windowed redacted reads), ADR-0223
  (worker console-file readability), ADR-0258 (local `<log append="off">` per-power-cycle truncation).

## Context

A long-running in-guest workload keeps emitting console output after the `kdive-ready` marker fires,
but the agent-facing surface freezes at readiness. The per-Run console artifact (`console-<run>`,
ADR-0235) is an immutable snapshot the boot worker assembles once at boot-step completion; nothing
re-captures the console afterward. A black-box repro (#892) confirmed it: repeated `runs.get` returned
the same `refs.console`, and paging `artifacts.get` to EOF showed a 34 KB console that ended shortly
after the workload's first iteration, even though the System was `ready` and the workload was still
running. Agents have no way to watch post-readiness repro logic.

Two facts shape the fix:

- The live console keeps growing on the **provider side**, not in the immutable evidence. For
  local-libvirt it is a worker-host file (`console_log_path`, truncated per power-cycle, ADR-0258);
  for remote-libvirt it is a reconciler-resident stream the `ConsoleCollector` already rotates into
  numbered, redacted, 64 KiB parts in the object store (`console-parts-<n>`, ADR-0095). Only the
  *assembled* console becomes an `artifacts` row today; the live parts are collector-internal.
- The read/search/download semantics an agent needs already exist as tools: `artifacts.list`
  enumerates a System's redacted artifacts, `artifacts.get` returns a byte-windowed redacted slice
  with paging (`next_offset`) and a `download_uri`, and `artifacts.search_text` searches content with
  context caps. The platform already has too many tools; a new one is unwanted.

So the gap is narrow: the continuously-growing console is not exposed as durable, listable artifacts
while the System runs. The existing `artifacts.*` surface can serve it — there is nothing to read.

## Decision

Expose the live console as **append-only, redacted, ~64 KiB System-owned console *part* artifacts**,
read through the existing `artifacts.{list,get,search_text}` surface. **No new MCP tool.**

1. **Part artifacts.** Each sealed part is a System-owned (`owner_kind='systems'`) `REDACTED`
   `artifacts` row whose object key is `…/console-part-<index>` with a zero-padded index so the
   object store and `artifacts.list` order them lexically. A sealed part is immutable; the in-flight
   tail (bytes below the rotation threshold, not yet sealed) is not a row. `artifacts.list(system_id)`
   returns the ordered part series alongside the per-Run evidence, so an agent reads the tail by
   taking the highest-index part and paging it with `artifacts.get`, and searches history with
   `artifacts.search_text`. Append-only means each console byte is uploaded once — a 12-hour workload
   does not re-upload its backlog on every observation.

2. **Capture driver differs by provider (locality).**
   - **Remote-libvirt:** the reconciler-resident collector already rotates 64 KiB redacted parts on a
     size threshold with seam-overlap redaction (ADR-0095). Register each part as an `artifacts` row
     when it is sealed (the reconciler holds the DB connection). This promotes the existing
     collector-internal parts to first-class rows; assembly into the per-Run evidence is unchanged.
   - **Local-libvirt:** the reconciler's periodic sweep discovers running local-libvirt Systems and
     dispatches a per-System `console_rotate` **worker job** (the reconciler owns periodic discovery and
     dedups to ≤1 in-flight per System; the worker owns the host console file it alone can read,
     ADR-0223). The job reads only the plaintext bytes **past a tracked plaintext offset**, redacts that
     delta once with a held-back seam overlap (mirroring the collector), seals ~64 KiB parts from the
     redacted delta, and registers each. The offset, next index, and a **boot generation** are persisted
     in an object-store sidecar (not a DB column). A file shorter than the tracked offset is a
     power-cycle truncation (ADR-0258): the offset resets to 0 and the boot generation increments,
     starting a fresh part series. The resume offset is **never** the sum of redacted part sizes — the
     redactor (`[REDACTED]`) is not length-preserving, so a redacted-byte count is not a valid
     plaintext-file offset and a sealed part is derived once and never re-redacted.

3. **Compression: decompress-on-read.** Sealed parts are gzip-compressed in the object store;
   `artifacts.get` inflates a part transparently before windowing. Redaction runs on the **plaintext**
   before compression, so the stored object is always redacted and the `artifacts.get`
   `sensitivity == REDACTED` gate still holds. Every part stays inline-windowable with uniform
   semantics — the agent never sees a hot/cold read distinction.

4. **Per-Run evidence is untouched.** `console-<run>` (ADR-0235) stays the frozen boot-window
   assembly. The live parts are a separate System-owned series; both the per-Run evidence and each
   sealed part remain immutable. `refs.console` on `runs.get` is unchanged.

5. **Authorization and redaction reuse the existing path.** Parts are served by the same `artifacts.*`
   project-scoped authorization and the same redactor as every other System-owned artifact. No new
   authz path, no new public surface.

6. **Retention.** Console parts are ordinary artifacts and expire through the existing artifact-expiry
   reconciler (#768). No new per-System cap is introduced.

Scope: the public observation surface (`artifacts.{list,get,search_text}` over System-owned console
parts) is identical for both providers. Capture is implemented local-libvirt first (the #892 repro
path); remote part-registration follows on the same surface. The only schema change is the additive
migration **0053** widening `jobs_kind_check` for the internal `console_rotate` job kind (forward-only,
ADR-0015, as 0051/0052 did); parts are ordinary `artifacts` rows and the local rotation offset lives in
an object-store sidecar, not a DB column.

## Consequences

- An agent watching a long-running workload calls `artifacts.list(system_id)`, takes the newest
  console part, and pages it with `artifacts.get`; the part series grows as the workload runs. Searching
  for a panic across the run uses `artifacts.search_text`.
- `artifacts.list` for a running System now returns more rows of an existing kind (one per sealed
  ~64 KiB part). A long, chatty run produces many parts; the list is ordered and newest-first reads are
  cheap, but a future `artifacts.list` pagination need is noted (the list already carries a `truncated`
  field). This is the cost of append-only efficiency.
- `artifacts.get` gains a gzip inflate step on console-part objects. The 64 KiB part bound keeps the
  inflate trivial and the windowed read after inflate is unchanged.
- Local-libvirt gains a worker-side periodic rotation per running System; remote-libvirt's reconciler
  collector gains a DB row write per sealed part. Both are best-effort and must never fail the
  workload or the boot.
- The redaction seam runs once per part on plaintext; a secret straddling a rotation boundary is held
  back and redacted with the next part, mirroring the collector's existing seam-overlap rule (ADR-0095).

## Considered & rejected

- **A new `systems.observe_console` / `runs.tail_console` tool.** Rejected: the platform already has
  too many tools and a top-level tool review is pending. The existing `artifacts.{list,get,search_text}`
  surface already provides windowed reads, paging, download URIs, search, redaction, and project
  authorization; a new tool would duplicate that surface for one artifact kind.
- **One mutable "live console" artifact, periodically re-snapshotted (overwritten).** A single
  System-owned row kept fresh by overwriting it with the whole current console. Simpler enumeration
  (one row) and it reuses `artifacts.get` paging directly, but each refresh re-uploads the entire
  console — O(size) per observation, which is wasteful for a multi-hour, tens-of-MB console and the
  opposite of why S3-backed capture rotates parts. Rejected for long-run cost; append-only uploads
  each byte once.
- **Hot/cold compression split.** Keep the active tail part raw and inline-windowable, compress a part
  only once sealed and then serve it download-only (no inline window). Cheapest storage, but the agent's
  read semantics differ between the hot tail and cold history, complicating the observation loop.
  Rejected for uniform read UX; decompress-on-read keeps every part inline-windowable.
- **Defer compression entirely.** Ship raw parts and lean on artifact expiry for the 12-hour growth.
  Viable as a first phase, but the issue calls out compression for long runs; it is folded in as
  decompress-on-read rather than dropped, so the stored footprint is bounded from the start.
- **Live streaming with a bounded window.** A push channel for console bytes. Heaviest option; it adds
  a transport concern MCP's request/response surface does not have, for a need that paging an
  append-only part series already meets.
- **Reconciler-driven local capture (symmetry with remote).** Have the reconciler read the local
  console file on a sweep. Rejected: the local console file is a worker-host path the reconciler cannot
  assume to read (ADR-0223); the worker owns the file, so local rotation is worker-driven.
- **Deriving the local resume offset from the existing part rows' sizes (no stored offset).** Tempting
  as "no extra state," but wrong: the part rows store **redacted** bytes and the redactor (`[REDACTED]`)
  is not length-preserving, so a summed redacted size is not a valid offset into the **plaintext** file;
  and the local file is truncated per power-cycle (ADR-0258), so a lifetime sum exceeds the current
  file. Rejected for correctness — the plaintext offset and boot generation are tracked explicitly in an
  object-store sidecar instead.
- **A persisted per-System rotation offset in a DB column (migration).** Store the offset in a new
  `systems` column. Rejected: the offset is internal capture bookkeeping with no query/authz need, so an
  object-store sidecar co-located with the parts is lighter than a schema column. (The one migration
  this work does take, 0053, is only the additive `jobs_kind_check` widening for the job kind.)
