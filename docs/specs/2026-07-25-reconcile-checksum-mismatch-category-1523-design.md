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
retryable one is the safe direction here.

Note what the category does *not* buy: any automatic re-attempt. The provision handler sets
`terminal` on any `CategorizedError` from the provider call (`jobs/handlers/systems.py`), and
`queue.py` dead-letters on `terminal or attempt >= max_attempts`, so the job dead-letters on the
**first** attempt under either category and nothing is re-downloaded. The whole effect is on what
the calling agent is told. A terminal verdict on transient corruption tells an agent to re-upload a
multi-GiB object that was never wrong; a retryable verdict on real bit rot costs one re-provision
before the message tells the agent to re-upload. The second error is cheap and self-correcting.

A third precedent already agrees with identity: the catalog path
`images/rootfs/fetch.py` raises `INFRASTRUCTURE_FAILURE` when downloaded bytes do not match the
row's digest.

PR #1547 (#1525) reached the same principle independently for the staging free-space check: one
physical condition should not report two categories depending on which side of a race window it
was observed from. This is that principle applied to checksum mismatch, across codecs rather than
across a race window.

## The retry-cost argument does not apply

The issue argues that since #1520 the identity path streams to disk, so each bounded retry
re-downloads *and* re-writes the full multi-GiB object before failing again, raising the price of
being on the retryable side.

That premise does not hold. As above, a staging failure is marked `terminal` by the provision
handler and dead-letters immediately, so there are no queue re-attempts to pay for under either
category. The disk cost the issue describes is the cost of the *first* stage, which happens either
way, and #1525's free-space precheck (ADR-0450) already refuses a stage the volume cannot hold
before its first byte. The category question is decided on what the failure *is*.

## Reach of the convergence

The gate order limits how far R1 actually reaches, and this is recorded rather than papered over.
On the gzip path the hash comparison is the last gate; zlib's framing trips first. An exhaustive
single-bit sweep of the deflate body of the residual test's fixture, with a correct signed
checksum, splits three ways: 225/248 corrupt-stream (`zlib.error`), **13/248 the gzip-bomb
bound**, 10/248 the checksum gate — content-dependent in the exact ratio, but the digest is
always reached by a small minority.
Trailer CRC/ISIZE rot and post-PUT truncation also report `CONFIGURATION_ERROR`. The identity path
reports all of these as `INFRASTRUCTURE_FAILURE`.

What *does* reach the digest is damage leaving the decoded stream and framing intact: gzip header
fields (MTIME/XFL/OS) and deflate padding bits after the final end-of-block code.

An **out-of-band overwrite does not reach this gate at all**, on either path. `_stage_uploaded_object`
re-`HEAD`s at provision time and compares against `head.checksum_sha256` — read off the *live*
object — not against the declared content address the key is derived from (that comparison exists
only in `complete_rootfs_upload`). An API-level re-`PUT` updates the stored checksum along with the
bytes, so both match and the gate passes. Only a storage-layer substitution that leaves S3's
recorded checksum stale is caught, which is the same mechanism as bit rot. Closing that properly
means comparing the declared content address at stage time — a new gate, not a category question,
and out of scope here.

The bomb branch is the worst case: besides the category, its message blames the declared
`uncompressed_size` when the declaration was right, and ADR-0450 reads that same field as the gzip
path's free-space budget, so following the advice can get the next provision refused.

Closing the residual means consulting the digest before declaring an object defect, which changes
the bomb and corrupt/truncated branches' categories in a subset of cases — outside this issue's
brief. **#1548** carries it; tests here pin both the `zlib.error` and bomb shapes so the gap is not
silent.

## Requirements

- **R1** — A gzip transport-checksum mismatch raises `INFRASTRUCTURE_FAILURE` (`retryable: true`).
- **R2** — A gzip bomb keeps `CONFIGURATION_ERROR`.
- **R3** — A corrupt, truncated, or multi-member gzip stream keeps `CONFIGURATION_ERROR`.
- **R4** — The identity path's category is unchanged.
- **R5** — Both checksum-mismatch messages carry the same remediation advice: retry; if it
  persists, the stored object is damaged and must be re-uploaded.
- **R6** — No schema change, no migration, no MCP tool-surface change.
- **R7** — A gzip staging failure carries `system_id` in its `details`, as the identity path does.
- **R8** — A checksum mismatch is logged at `WARNING` on both paths, since the category no longer
  distinguishes it from routine infrastructure noise.

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
outcomes and states the split, the gate order that limits its reach, and the ADR reference. The
utility keeps raising with empty `details` — it is consumer-agnostic — and the docstring says so,
pointing callers at annotating on the way out.

### `providers/local_libvirt/.../rootfs_upload_fetch.py`

`_stage_identity` keeps `INFRASTRUCTURE_FAILURE`. Its docstring carries a note that the category is
"unsettled" because the gzip path disagrees, pointing at this issue; that note is now false and is
replaced with the ADR-0445 reference. Its message gains the same remediation clause as gzip (the
existing `"checksum verification"` substring is preserved).

`_stage_gzip` catches `CategorizedError` around `strip_gzip_to_writer` and `setdefault`s
`details["system_id"]` before re-raising, so both paths land in the job row's `failure_context`
with the field an operator correlates on (R7). It also routes an `INFRASTRUCTURE_FAILURE` — the
utility's only retryable branch, so the category identifies the checksum mismatch without
re-matching message text — into the shared `_log_checksum_mismatch` the identity path calls
directly (R8).

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
through `stage_uploaded_rootfs`: a gzip object and an identity object, each with a declared
checksum that does not match, both raise `INFRASTRUCTURE_FAILURE` carrying `system_id`, staging
nothing and leaving no `.partial`. Asserted as *one* parametrised claim over both codecs so that
re-diverging the two paths cannot pass.

Plus a test pinning the **limit**, parametrised over the two shapes body corruption reaches: a
flipped bit that fails the deflate CRC, and one that desynchronises the Huffman decode into the
gzip-bomb bound. Both keep a pristine declared checksum and both still report
`CONFIGURATION_ERROR`, because zlib's framing and the output cap trip before the digest is
compared. Named as a known residual against #1548, so the gap is asserted rather than merely absent
from the suite.

Mutation check: collapsing the split back to a single category must redden a test. Reverting the
checksum branch to `CONFIGURATION_ERROR` reddens the mismatch tests on both files; widening
`_transport_error` over the bomb/corrupt branches reddens the four unchanged-branch tests. Both
were run against the branch and both were killed.

## Out of scope

- Consulting the digest before the object-defect branches, which would extend the convergence to
  damage that breaks the gzip framing. Deferred to **#1548** — see "Reach of the convergence".
- The `ErrorCategory` vocabulary itself.
- Whether a staging failure should dead-letter immediately at all (`jobs/handlers/systems.py`
  forcing `terminal`). That predates this change and applies equally to both categories.
