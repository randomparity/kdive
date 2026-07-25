# Reconcile the checksum-mismatch error category across the rootfs staging paths (#1523)

- **Issue:** [#1523](https://github.com/randomparity/kdive/issues/1523) — P3 bug
- **ADR:** [ADR-0445](../adr/0445-reconcile-checksum-mismatch-error-category.md)
- **Amends:** [ADR-0438](../adr/0438-rootfs-transport-strip-streaming-fetch.md) §2 (the gzip
  strip-decode's error taxonomy)
- **Leaves standing:** [ADR-0434](../adr/0434-local-libvirt-agent-uploaded-rootfs-staging.md) §2

## Problem

The two uploaded-rootfs staging paths report the *same physical failure* — the bytes read back
from the store do not hash to the SHA-256 the signed PUT bound — under two different error
categories, and `mcp/responses.py`'s `_RETRYABLE_BY_CATEGORY` turns that category into an
agent-visible `retryable` boolean. So which staging path a System happens to take decides whether
the calling agent is told to retry.

- **identity** — `_stage_identity` (`providers/local_libvirt/lifecycle/rootfs/rootfs_upload_fetch.py`)
  raises `INFRASTRUCTURE_FAILURE` ⇒ `retryable: true`. ADR-0434 §2 decided this.
- **gzip** — `strip_gzip_to_writer` (`artifacts/transport_encoding.py`) raises
  `CONFIGURATION_ERROR` ⇒ `retryable: false`.

Nothing about the object differs between the two; only the transport codec does.

## Which side is wrong

The gzip side.

`_decode_error()` is a **uniform helper**. It categorises three distinct outcomes identically:
a gzip bomb (output exceeds the declared `uncompressed_size`), a corrupt or truncated gzip stream
(including trailing data after the single member), and the end-of-stream transport-hash mismatch.
The first two are defects in *the object the agent uploaded* — no retry of the same key can fix
them, so `CONFIGURATION_ERROR` is right for them. The hash mismatch is a different kind of claim
entirely, and it inherited its category from the helper it happened to sit next to. That asymmetry
is the bug; the gzip path never *decided* `CONFIGURATION_ERROR` for a checksum mismatch.

The issue's own argument for the gzip side is that `head.checksum_sha256` is what S3 verified at
PUT, so a mismatch means the stored bytes changed *after* the PUT — bit rot, tampering, an
out-of-band overwrite — none of which retrying fixes. That covers only half the failure modes.
ADR-0434 §2's stated rationale is that the recomputed hash "catches **transport corruption** and
post-PUT bit-rot that the PUT-time signature alone does not". Transport corruption on the *GET* is
transient and is exactly what a retry clears. A category has to cover both modes, and the
retryable one is the safe direction here: a retryable verdict on a permanent fault costs the
bounded `jobs/queue.py` `max_attempts` re-attempts and then fails terminally anyway, whereas a
terminal verdict on a transient fault fails a provision that a single retry would have completed.

A third precedent already agrees with identity: the catalog path
`images/rootfs/fetch.py` raises `INFRASTRUCTURE_FAILURE` when downloaded bytes do not match the
row's digest.

PR #1547 (#1525) reached the same principle independently for the staging free-space check: one
physical condition should not report two categories depending on which side of a race window it
was observed from. This is that principle applied to checksum mismatch, across codecs rather than
across a race window.

## The cost of the status quo (why now)

Since #1520 the identity path streams to disk. Each bounded retry re-downloads *and* re-writes the
full multi-GiB object into the shared staging directory before failing again; the old buffered path
never touched disk on this failure. That raises the price of being on the retryable side, but it
is an argument about retry *cost*, not about which category is true — and #1525's staging
free-space precheck (ADR-0450) now refuses a stage the volume cannot hold before its first byte,
which bounds the worst case. The category question is decided on what the failure *is*.

## Requirements

- **R1** — A gzip transport-checksum mismatch raises `INFRASTRUCTURE_FAILURE` (`retryable: true`).
- **R2** — A gzip bomb keeps `CONFIGURATION_ERROR`.
- **R3** — A corrupt, truncated, or multi-member gzip stream keeps `CONFIGURATION_ERROR`.
- **R4** — The identity path's category is unchanged.
- **R5** — Both checksum-mismatch messages carry the same remediation advice: retry; if it
  persists, the stored object is damaged and must be re-uploaded.
- **R6** — No schema change, no migration, no MCP tool-surface change.

## Options considered

**(a) Split the helper so only the checksum branch is `INFRASTRUCTURE_FAILURE`** — chosen. The
split is the point: the two categories now name two different claims about the world, so the
helper's name says which one it is asserting.

**(b) Converge the identity path onto `CONFIGURATION_ERROR` instead** — rejected. It would have to
overturn ADR-0434 §2 *and* the catalog precedent, and it is wrong on the transport-corruption
half of the failure modes.

**(c) Introduce a distinct `TRANSPORT_FAILURE`-style category for both paths** — rejected as
scope. `TRANSPORT_FAILURE` exists and is retryable, but it is the SSH/console transport's
category throughout the codebase; borrowing it for object-store integrity would blur an
established vocabulary to gain nothing over `INFRASTRUCTURE_FAILURE`, which both the identity and
catalog precedents already use for exactly this.

## Design

### `artifacts/transport_encoding.py`

`_decode_error` splits into two named constructors, so the category is chosen by what is being
asserted rather than by which function is nearest:

- `_object_error(detail)` → `CONFIGURATION_ERROR`. The uploaded object is defective: the bomb
  guard, the corrupt/truncated stream, the trailing-data (multi-member) branch. Retrying the same
  key re-reads the same defect.
- `_transport_error(detail)` → `INFRASTRUCTURE_FAILURE`. The bytes read back are not the bytes
  signed at PUT. Only the end-of-stream hash comparison uses it.

The mismatch message drops "do not retry the same corrupt bytes" — which contradicted the new
category — for "retry; if it persists the stored object is damaged, re-upload it", matching the
identity path.

`strip_gzip_to_writer`'s docstring stops claiming a single `CONFIGURATION_ERROR` for all three
outcomes and states the split, with the ADR reference.

### `providers/local_libvirt/.../rootfs_upload_fetch.py`

`_stage_identity` keeps `INFRASTRUCTURE_FAILURE`. Its docstring carries a note that the category is
"unsettled" because the gzip path disagrees, pointing at this issue; that note is now false and is
replaced with the ADR-0445 reference. Its message gains the same remediation clause as gzip (the
existing `"checksum verification"` substring is preserved).

The same stale cross-reference in
`tests/.../test_rootfs_upload_fetch.py::test_stage_corrupt_object_reports_the_checksum_gate_not_the_format_gate`
is updated: that test pins *gate precedence*, and it can now also state that the category is the
reconciled one rather than one side of a disagreement.

## Test plan

`tests/artifacts/test_transport_encoding.py` — the split, asserted per branch:

- transport-hash mismatch ⇒ `INFRASTRUCTURE_FAILURE`, and the message no longer tells the agent
  not to retry;
- gzip bomb ⇒ `CONFIGURATION_ERROR` (unchanged);
- truncated stream, corrupt deflate body, trailing data after the member ⇒ `CONFIGURATION_ERROR`
  (unchanged).

`tests/providers/local_libvirt/test_rootfs_upload_fetch.py` — the convergence itself, end to end
through `stage_uploaded_rootfs`: a gzip object whose stored bytes do not match the declared
checksum and an identity object with the same defect both raise `INFRASTRUCTURE_FAILURE`, staging
nothing and leaving no `.partial`. Asserted as *one* parametrised claim over both codecs so that
re-diverging the two paths cannot pass.

Mutation check: collapsing the split back to a single category must redden a test. Reverting the
checksum branch to `CONFIGURATION_ERROR` reddens the mismatch tests on both files; widening
`_transport_error` over the bomb/corrupt branches reddens the four unchanged-branch tests.

## Out of scope

- The retry *cost* on a genuinely damaged object (bounded by `max_attempts`; the free-space
  precheck of ADR-0450 bounds the disk side). Not re-opened here.
- The `ErrorCategory` vocabulary itself.
