# 0523 — The transport hash is consulted before the gzip path declares an object defect

## Status

Accepted (2026-07-30)

## Context

[ADR-0445](0445-reconcile-checksum-mismatch-error-category.md) converged the two uploaded-rootfs
staging paths on `infrastructure_failure` for a checksum mismatch, so the transport codec no longer
decided the agent-visible `retryable` boolean. Its own §6 recorded that on the gzip path the
convergence reaches much less than §1 alone reads, because the hash comparison in
`strip_gzip_to_writer` was the *last* gate and zlib's framing and the output cap tripped first.

An exhaustive single-bit sweep of the deflate body of the residual test's fixture, with a
**correct** signed checksum and damaged stored bytes, landed on three branches:

| branch reached | share | reported |
|---|---|---|
| corrupt deflate stream (`zlib.error`) | 225/248 | `configuration_error` |
| **gzip-bomb bound** (`_drain`'s output cap) | 13/248 | `configuration_error` |
| transport checksum mismatch | 10/248 | `infrastructure_failure` |

The exact split is a property of the object's content, not a constant, but the shape holds: the
digest was reached by a small minority. Trailer CRC/ISIZE rot and post-PUT truncation likewise
reported `configuration_error`. The identity path, having no framing to trip, reports every one of
these as `infrastructure_failure`. So for most damage the codec still decided the verdict — the
asymmetry ADR-0445 set out to remove, surviving in the majority of cases.

This is wrong rather than merely inconsistent. Those four branches assert *"the object the agent
uploaded is defective"*, and that claim is unfounded whenever the bytes read back are not the bytes
signed at PUT: the object may be fine and the read may be damaged. ADR-0445 §2's own principle —
the category follows from what is being asserted — decides it; the branches simply did not have the
digest consulted before they asserted it.

The bomb branch was the worst of the three, and not only for its category. Its message reads
"re-declare with the correct `uncompressed_size`" — affirmatively wrong advice when the declaration
was right and the stored bytes rotted. It also compounds: [ADR-0450](0450-uploaded-rootfs-staging-free-space-precheck.md)
makes `uncompressed_size` the gzip path's free-space budget, so an agent that follows the advice and
re-declares upward can have its next provision refused by the free-space precheck for a base the
volume can hold.

ADR-0445 could not close this. #1523's campaign brief constrained it explicitly — "do NOT change
bomb/corrupt-stream categories for any caller" — so §6 documented the residual, pointed at #1548,
and `test_stage_checksum_mismatch_on_gzip_corrupt_bytes_is_a_known_residual` pinned both the
`zlib.error` and bomb shapes so the gap was visible rather than silent. This ADR is that decision.

## Decision

### 1. The transport digest is the first verdict, not the last

`strip_gzip_to_writer` compares the transport hash **before** any object-defect branch raises. A
framing or bound defect only reports `configuration_error` once the stored bytes have been proven to
be the bytes the signed PUT bound; otherwise the call raises the transport error, with the same
`TRANSPORT_CHECKSUM_GATE` marker, message and category the identity path gives for byte-identical
damage.

This is not a reordering of the existing checks. Two of the four branches — `zlib.error` and the
bomb bound — raise from inside `_drain`, a frame that sees a decompressor and one input range and
has no access to the store, the key, or the running hash. It cannot know whose fault the damage is.
So the ordering follows from a seam change, not from moving three `if`s (§2).

### 2. A mid-pass defect is a returned value; the caller decides what it means

`_drain` and the end-of-pass framing checks raise or return a private `_ObjectDefect`, which never
escapes the module. `strip_gzip_to_writer` — the one frame holding the store, the request and the
hasher — receives it, completes the digest, and picks the constructor. The two public constructors
ADR-0445 §2 introduced are unchanged; what changes is that `_object_error` is now reachable only
after the digest has agreed, which is what makes its claim true rather than merely nearest.

The decode is split into `_decode_pass` (read, hash, gunzip, stop at the first defect) and
`_hash_remaining` (feed the unread tail to the hasher and nothing else). Carrying the defect as a
value rather than raising it at the point of discovery is what lets the module keep one comparison
site for the digest instead of four.

### 3. The extra read is hash-only, so the bomb bound is never reopened

A defect stops the pass mid-object, leaving the digest incomplete. `_hash_remaining` re-reads only
the ranges the pass never reached — bounded by `compressed_size`, touching neither the decompressor
nor the writer. The cap exists so a bomb is never expanded, and a drain that resumed decompression
to finish the hash would defeat exactly that. Between the two loops every byte of the stored object
is read exactly once; the cost of the new verdict is at most the compressed size the stage was
already going to read, never a second pass.

The truncated branch costs nothing at all: `offset` has already reached `compressed_size`, so the
hasher has absorbed every stored byte and `_hash_remaining` is a no-op. The trailing-data branch
needs the drain only when `eof` stopped the pass early with ranges still unread.

### 4. A genuinely defective upload keeps `configuration_error`

The convergence is one-directional. When the digest agrees, the bomb, corrupt-stream, truncated and
multi-member branches keep their terminal category and their messages verbatim — including the
bomb's `uncompressed_size` advice, which is correct precisely when the object really is what was
signed. Widening the retryable constructor over these would tell an agent to retry a key that
re-reads the same defect forever. Both directions are pinned, parametrised over the same two branch
shapes, so neither can drift without reddening.

### 5. The operator log widens with the category

ADR-0445's Consequences noted that the gzip path's checksum WARNING inherits §6's reach exactly, so
framing-first damage logged nothing and absence of the line was not evidence of an intact object.
That follows the gate, and the gate moved: `_stage_gzip` keys the warning on
`details["gate"] == TRANSPORT_CHECKSUM_GATE` and needs no change to log the widened set. Keying it
on the gate rather than the category is what makes this free, and remains necessary — the store's
own `get_range` faults propagate through the utility as `infrastructure_failure` too.

## Consequences

- Damaged stored bytes under a gzip upload now report `retryable: true` with the transport
  message, for **every** shape of damage rather than the minority that left the framing intact. An
  exhaustive single-byte sweep of a gzip fixture — header, deflate body and CRC/ISIZE trailer, nine
  masks per byte — reports the transport gate on every one, replacing ADR-0445 §6's three-way table.
- The bomb branch no longer emits `uncompressed_size` remediation for a declaration that was
  correct, so the ADR-0450 free-space compounding it fed is closed for the rotted-bytes case.
- A staging failure on damaged gzip bytes now emits the stored-object-damage WARNING it previously
  suppressed (§5). Operator-visible volume rises on exactly the condition the log exists to name.
- The `zlib.error` and bomb paths issue additional ranged GETs — the tail the pass never read,
  bounded by `compressed_size` and hash-only. A stage that fails on the first range of a multi-GiB
  object now reads the object through before reporting, where it used to fail immediately. That is
  the price of the verdict; it is bounded by what a successful stage reads anyway, and it buys the
  agent a truthful category rather than a re-upload of a multi-GiB object that was never wrong.
- No schema change, no migration, no MCP tool-surface change. The only externally visible changes
  are the `retryable` boolean and the message text on the four branches, in the subset where the
  digest disagrees.
- `strip_gzip_to_writer` keeps its signature, its result type and both error categories, so its one
  production consumer (`_stage_gzip`) is unchanged apart from a docstring. The upload-declaration
  validator imports only the codec constants.
- `test_stage_checksum_mismatch_on_gzip_corrupt_bytes_is_a_known_residual` is retired; the flip
  probe it introduced survives, inverted, as the fixture for both halves of §4.

## Considered & rejected

- **Move the hash comparison ahead of the framing checks and leave `_drain` alone.** The `effort:S`
  reading of the issue, and it closes only the truncated branch. `zlib.error` and the bomb bound
  raise from a frame with no store, key or hasher, and they are 238 of the 248 flips ADR-0445 §6
  measured — the reorder would leave the whole majority reporting the wrong category.
- **Hash the whole object up front, then decode.** One comparison site and no seam change, but it
  doubles the ranged reads on the *success* path, which is every stage. Paying that on every
  provision to improve the message on a failure is the wrong trade; the drain is paid only when a
  defect is actually found.
- **Resume decompression while draining the tail.** Would let the bomb branch report how far the
  object really expands, and defeats the guard's entire purpose — the cap exists so a bomb is never
  expanded, and the drain must not reopen it.
- **A new category for "damaged stored bytes" distinct from both.** ADR-0445 already rejected
  `TRANSPORT_FAILURE` for this gate on the grounds that it is the SSH/console transport's category
  throughout the tree; nothing here changes that, and a third category would re-split the pair
  ADR-0445 converged.
- **Leave the residual and widen only the log.** An operator would see the damage while the agent
  is still told the failure is permanent and pointed at re-uploading an object that was never
  wrong. The agent-visible contract is the thing ADR-0445 §1 identified as the only thing the
  category controls.
