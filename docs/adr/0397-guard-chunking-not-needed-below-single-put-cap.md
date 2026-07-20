# 0397 — Reject a chunked upload declared for an object that fits a single PUT

Status: Accepted

## Context

`artifacts.create_run_upload` / `create_system_upload` accept an optional per-artifact
`chunks` array for a *multi-part* upload. The chunked path exists for one reason: real S3
caps a single PUT at 5 GiB (`SINGLE_PUT_MAX_BYTES`), so an object above that ceiling must be
uploaded in parts and reassembled at finalize (ADR-0104 §5).

`_validate_chunks` validated the parts' internal consistency — part count in `1..MAX_PARTS`,
each part `<= MAX_PART_BYTES`, non-final parts `>= MIN_PART_BYTES`, and the summed sizes
equal to the declared total within the configured cap — but never compared the declared total
against the single-PUT threshold. So declaring a multi-chunk upload for an object that fits a
single PUT (declared total `<= SINGLE_PUT_MAX_BYTES`) passed silently.

In a black-box agent review (#1340) an agent that (mis)believed a 234 MB object was too large
for the presign window declared 5 chunks; the API minted 5 part URLs without objection and the
upload then failed manifest assembly. A single PUT would have sufficed. Chunking is a *size*
mechanism, not a remedy for a short presign window — but nothing at declaration time signalled
that, so the agent invested round-trips in a multi-part upload that dead-ended. #1336
(ADR-0394) added docstring language stating the rule; this is the server-side guard that
catches the agent that did not read it.

## Decision

**Hard-reject a multi-part declaration for an object that fits a single PUT.** In
`_validate_chunks`, when more than one chunk is declared and the declared total is
`<= SINGLE_PUT_MAX_BYTES`, return a `configuration_error` with the stable machine-readable
reason `chunking_not_needed` and an actionable `detail` telling the agent that a single PUT
always succeeds below the 5 GiB single-object cap, so it should omit `chunks` and declare a
single-PUT upload instead.

The check runs immediately after the part-count bound and before per-part validation, so a
small multi-part declaration is always diagnosed as "you should not be chunking at all" rather
than surfacing an incidental per-part error (e.g. `chunk_too_small`) that would send the agent
down a fix-the-parts path when the whole multi-part shape is the mistake.

Reject rather than warn: below the cap a single PUT always works, so a multi-part declaration
there is pure added failure surface (the parts upload but reassembly fails) and almost always
a client bug. A hard reject prevents the failed-assembly dead end and matches the repo's
fail-fast norm; it is the issue's own recommendation.

Scope: the guard keys on `declared_total`, the authoritative whole-object size. A single-chunk
declaration (one-element `chunks`) is unaffected — it mints one URL and is equivalent to a
single PUT. A genuinely large object (declared total above the cap) declared chunked, and any
object declared as a single PUT (no `chunks`), both still pass unchanged.

## Consequences

- An agent that declares chunks for a sub-5 GiB object is rejected at declaration time with a
  self-correcting reason, before minting part URLs that dead-end at reassembly.
- The rejection reason `chunking_not_needed` is a new stable string in the upload validator's
  reason vocabulary, alongside `too_many_chunks` / `chunk_too_small` / `chunk_size_mismatch` /
  `size_out_of_range`.
- Two existing validator tests that used sub-cap totals purely as fixtures to trigger
  `chunk_too_small` / `chunk_size_mismatch` now use above-cap totals so they still exercise
  those per-part paths without tripping the new guard.
- Validation only; no schema change, no migration, no response-shape change on the success path.
