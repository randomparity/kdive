# ADR 0445 — A rootfs checksum mismatch is `infrastructure_failure` on every staging path

- **Status:** Accepted
- **Date:** 2026-07-25
- **Amends:** [ADR-0438](0438-rootfs-transport-strip-streaming-fetch.md) §2, whose streaming
  strip-decode raised a single `configuration_error` for all three of its failure outcomes. The
  transport-checksum mismatch moves to `infrastructure_failure`; the gzip-bomb bound and the
  corrupt/truncated/multi-member stream keep `configuration_error`, and every other ADR-0438
  decision (the ranged single pass, the output cap, the qcow2 magic check, the declaration-time
  cap) is untouched.
- **Leaves standing:** [ADR-0434](0434-local-libvirt-agent-uploaded-rootfs-staging.md) §2. Its
  choice of `infrastructure_failure` for the identity path's recomputed-hash rejection is
  **confirmed, not superseded** — this ADR extends it to the gzip path and records the reasoning
  that §2 asserted without argument.
- **Depends on:** [ADR-0118](0118-wait-on-resource-mechanisms.md) (the derived `retryable` field —
  a pure function of the failure category),
  [ADR-0437](0437-transport-encoding-canonical-object-model.md)
  (the transport-encoding model and the checksum the signed PUT binds),
  [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) §5 (the staging gate order).
- **Spec:** [`../specs/2026-07-25-reconcile-checksum-mismatch-category-1523-design.md`](../specs/2026-07-25-reconcile-checksum-mismatch-category-1523-design.md)

## Context

An uploaded rootfs is staged by one of two paths, chosen by the declared transport encoding. Both
end by comparing a SHA-256 recomputed over the bytes they read against the checksum the signed PUT
bound. Until now they disagreed about what that comparison failing *means*:

- **identity** — `_stage_identity` raised `infrastructure_failure` ⇒ `retryable: true`
  (ADR-0434 §2).
- **gzip** — `strip_gzip_to_writer` raised `configuration_error` ⇒ `retryable: false`.

`mcp/responses.py`'s `_RETRYABLE_BY_CATEGORY` maps the category to an agent-visible `retryable`
boolean, so the codec a System's object happened to be uploaded under decided whether the calling
agent was told to try again. The object is byte-identically as damaged either way. This is a
contract the agent reads, not a job-queue auto-retry policy.

Neither record reconciled the other: ADR-0434 §2 predates the gzip path, and ADR-0438 §2 never
discussed the identity precedent.

## Decision

### 1. A checksum mismatch is `infrastructure_failure` on both paths

The identity path was right, and the gzip path changes.

The decisive fact is that `_decode_error()` was a **uniform helper** covering three outcomes: a
gzip bomb, a corrupt/truncated/multi-member stream, and the end-of-stream hash mismatch. The first
two are defects in the object the agent uploaded — retrying the same key re-reads the same defect,
so `configuration_error` is correct and stays. The hash mismatch is a claim of a different kind,
and it took its category from the helper it sat next to. The gzip path never *chose*
`configuration_error` for a checksum mismatch; it inherited it by proximity. That asymmetry is what
this ADR removes.

The substantive argument for the terminal reading is that `head.checksum_sha256` is the value S3
itself verified at PUT, so a mismatch proves the stored bytes changed *after* the PUT — bit rot,
tampering, an out-of-band overwrite — and no retry of the same key fixes any of those. That is
sound but partial. ADR-0434 §2's own stated rationale for recomputing at all is that it "catches
**transport corruption** and post-PUT bit-rot that the PUT-time signature alone does not".
Transport corruption on the *GET* is transient, and it is precisely what a bare re-invocation
clears. Two failure modes share one observation, and only one of them is permanent.

ADR-0118 biases terminal when transience is ambiguous, on the grounds that the flag exists to stop
an agent hammering a permanent failure. That bias does not decide this case, because the asymmetry
of consequences runs the other way here and the hammering is already bounded: a retryable verdict
on a permanently damaged object costs the queue's `max_attempts` re-attempts and then fails
terminally regardless, while a terminal verdict on a transient GET corruption fails a provision
that one retry would have completed — and tells the agent to re-upload a multi-GiB object that was
never actually wrong.

A third precedent already sat on this side: the catalog path (`images/rootfs/fetch.py`) raises
`infrastructure_failure` when downloaded bytes do not match the registered row's digest. With this
change, every integrity check over object-store bytes in the tree reports one category.

The same principle was reached independently in ADR-0450 (#1525) for the staging free-space
precheck: one physical condition should not report two categories depending on which side of a
race window it was observed from. This is that principle across codecs rather than across a race.

### 2. The split is structural, not a category edit

`_decode_error` is replaced by two named constructors, so the category follows from what is being
asserted rather than from what is nearest in the file:

- `_object_error` → `configuration_error` — the uploaded object is defective. Used by the
  bomb-bound guard, the corrupt-deflate branch, the truncated-stream branch, and the
  trailing-data (concatenated/multi-member) branch.
- `_transport_error` → `infrastructure_failure` — the bytes read back are not the bytes signed at
  PUT. Used by the end-of-stream hash comparison, and only there.

A future failure mode added to this utility has to pick one, which is the drift this asymmetry
came from. Note the ordering this preserves: the trailing-data branch is checked *before* the hash
comparison precisely so a multi-member object reports the object defect rather than falling
through to the mismatch branch — that branch's comment already said so, and the split makes the
consequence (terminal, not retryable) explicit rather than incidental.

### 3. Both messages carry the same remediation

The gzip message dropped "re-upload the object (do not retry the same corrupt bytes)", which
directly contradicted the new category, and the identity message was bare
("uploaded rootfs object failed checksum verification"). Both now say the same thing: the stored
bytes do not match the checksum signed at upload; retry, and if it persists the stored object is
damaged and must be re-uploaded. That is the honest rendering of two failure modes behind one
observation, and it is what the agent needs in order to act — the `retryable` flag alone cannot
express "retry once, then re-upload".

### 4. Gate precedence is unchanged

ADR-0441 §5 and ADR-0438 §3 put the checksum comparison ahead of the qcow2-magic gate, so
store-side corruption that also destroys the magic is reported as the checksum failure rather than
as a format failure. That ordering is untouched; only the category the checksum gate reports
changes on the gzip side. What changes downstream is that a corrupt object now surfaces as
retryable rather than as a wrong-format `configuration_error` on either path.

## Consequences

- An agent that hits a rootfs checksum mismatch on a gzip-encoded upload is now told
  `retryable: true` and gets the same advice it would have got for the same object uploaded
  identity-encoded.
- A genuinely damaged stored object costs the queue's bounded `max_attempts` re-downloads before
  failing terminally, where it previously failed on the first attempt. On the gzip path those
  retries are ranged GETs writing the decompressed stream to staging; ADR-0450's free-space
  precheck refuses a stage the volume cannot hold before its first byte, which bounds the disk
  cost. Nothing here changes `max_attempts`.
- No schema change, no migration, no MCP tool-surface change. The only externally visible change is
  the `retryable` boolean and the message text on one failure path.
- `artifacts/transport_encoding.py` is a shared module, but `strip_gzip_to_writer` has exactly one
  production consumer (the local-libvirt uploaded-rootfs fetch); the upload-declaration validator
  imports only the codec constants. No other caller depended on the old category.

## Considered & rejected

- **Converge the identity path onto `configuration_error` instead.** Would overturn ADR-0434 §2
  and diverge from the catalog precedent, and is wrong on the transport-corruption half of the
  failure modes.
- **A distinct category for object-store integrity failures.** `TRANSPORT_FAILURE` exists and is
  retryable, but it is the SSH/console transport's category throughout the tree; borrowing it here
  would blur an established vocabulary to gain nothing over `infrastructure_failure`, which both
  the identity and catalog precedents already use for this exact check.
- **Leave the disagreement and document it.** The issue asked for a decision precisely because the
  test pinning the identity side had to disclaim that it was pinning one side of an open question.
