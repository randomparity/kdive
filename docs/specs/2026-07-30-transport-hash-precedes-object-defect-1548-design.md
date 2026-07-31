# The transport hash rules before the gzip object-defect verdict (#1548)

Design record for [ADR-0523](../adr/0523-transport-hash-precedes-the-gzip-object-defect-verdict.md),
which carries the decision, the seam change and the rejected alternatives. This file carries the
requirement, the surfaces touched, and how each claim is verified.

## Requirement

A gzip-encoded uploaded rootfs whose *stored* bytes were damaged after the signed PUT must report
the same error category as the identity path reports for the same damage — `infrastructure_failure`,
`retryable: true` — regardless of which of `strip_gzip_to_writer`'s four object-defect branches the
damage happens to reach first. An object the agent really did upload broken keeps
`configuration_error`.

The issue carries `effort:S`, which is wrong: two of the four branches (`zlib.error`, the bomb
bound) raise from inside `_drain`, a frame with no access to the store, the key or the hasher. The
work is a seam change, not a reordering of three `if`s. See ADR-0523 §1–2.

## Acceptance criteria

1. A gzip object with corrupted *stored* bytes and a correct signed checksum reports
   `INFRASTRUCTURE_FAILURE` with `details["gate"] == TRANSPORT_CHECKSUM_GATE` and the transport
   message, across all four branches — truncated, trailing-data/multi-member, corrupt deflate, and
   the gzip-bomb bound.
2. A genuinely defective *uploaded* gzip (the digest matches what was PUT) keeps
   `CONFIGURATION_ERROR` and its original message for all four branches, including the bomb's
   `uncompressed_size` remediation.
3. The extra read on the `zlib.error` and bomb paths is hash-only and bounded by the compressed
   size: no decompression is resumed, nothing further is written, and no bomb is expanded past the
   cap.
4. The unread ranges are hashed when the pass stopped early — on a mid-pass defect, and on the
   trailing-data branch when `eof` ended the loop with ranges still unread.
5. `test_stage_checksum_mismatch_on_gzip_corrupt_bytes_is_a_known_residual` is retired or inverted,
   and ADR-0445 §6 is amended.
6. The gzip path's stored-object-damage WARNING fires for the widened set, with no change to
   `_stage_gzip`'s gate-keyed condition.

## Surfaces

| Surface | Change |
|---|---|
| `artifacts/transport_encoding.py` | `_ObjectDefect` (private, never escapes) and `_DecodePass`; `strip_gzip_to_writer` compares the digest first; new `_decode_pass`, `_framing_defect`, `_hash_remaining`; `_drain` raises `_ObjectDefect` instead of `_object_error`. |
| `providers/local_libvirt/.../rootfs_upload_fetch.py` | `_log_checksum_mismatch` gains an optional `decode_detail`, which `_stage_gzip` lifts off the raised error's `__cause__` (ADR-0523 §5). The gate-keyed condition itself is unchanged — it already covers the widened set. |
| `tests/artifacts/test_transport_encoding.py` | Exhaustive single-byte sweep; per-branch transport-verdict tests; per-branch digest-agrees tests; hash-only/tiling assertions. `_FakeRangedStore` gains `max_read`. |
| `tests/providers/local_libvirt/test_rootfs_upload_fetch.py` | The residual test is inverted into `test_stage_gzip_damaged_stored_bytes_report_the_identity_verdict` plus its converse `test_stage_gzip_defective_upload_keeps_the_terminal_verdict`; `_flip_reaching` probes with the damaged object's own checksum. |
| [ADR-0445](../adr/0445-reconcile-checksum-mismatch-error-category.md) | Append-only amendment: §7 closes §6's residual and points at ADR-0523. |

No schema, migration, MCP-surface, config or dependency change.

## Verification

| Claim | How |
|---|---|
| AC1, all four branches | `test_strip_gzip_every_single_byte_flip_reports_the_transport_gate` sweeps every byte of a gzip fixture under nine masks and asserts the transport gate on every one — header, deflate body and trailer, so all four branches are covered without naming which flip reaches which. Plus the named per-branch tests for truncated and trailing-data. |
| AC1 at the staging seam | `test_stage_gzip_damaged_stored_bytes_report_the_identity_verdict`, parametrised over the deflate-CRC and bomb-bound shapes the ADR-0445 §6 sweep found. |
| AC2 | `test_strip_gzip_mid_pass_defect_keeps_configuration_error_when_the_digest_agrees` and the four pre-existing object-defect tests, plus `test_stage_gzip_defective_upload_keeps_the_terminal_verdict` at the seam. |
| AC3 | `test_strip_gzip_hash_only_drain_never_expands_a_bomb_and_reads_each_byte_once` — output stays `<= bound + 1` on a bomb whose stored bytes also rotted, read starts are strictly increasing with no repeats, and the read lengths sum to exactly `compressed_size`. |
| AC4 | The *converse* assertions carry this: a genuinely multi-member object, or a genuinely corrupt one, only keeps `CONFIGURATION_ERROR` if the unread tail reached the hasher. Delete `_hash_remaining` and the digest comes up short and those tests redden as a transport mismatch. |
| The retired backstop (ADR-0523 §3) | `test_strip_gzip_boundary_aligned_trailing_member_is_still_rejected` constructs the case where `unused_data` is empty and the whole second member is unread — the only case in which `_framing_defect`'s `offset < compressed_size` clause is the sole guard — and asserts on the read pattern so it cannot pass on the other clause instead. |
| AC6 | The staging tests assert on `caplog` in both directions: the WARNING fires for damaged stored bytes and does not fire for a defective upload. |
| A store fault during the drain (ADR-0523 Consequences) | `test_strip_gzip_store_fault_during_the_drain_keeps_the_decode_diagnosis_on_the_chain` — the store's own error passes through unreclassified, and the decode diagnosis it would otherwise have destroyed survives on `__cause__`. |
| The decode diagnosis reaches the operator (ADR-0523 §5) | `test_stage_gzip_damaged_stored_bytes_report_the_identity_verdict` asserts the branch's own message is *absent* from the error and from `details`, and *present* in the WARNING behind "the decode also failed". The clean-hash test asserts that clause is absent when there was no decode failure to report. |

Mutation-verified, `__pycache__` cleared between every run and the restored tree re-confirmed green
(115 passed) at the end:

| Mutation | Reddened |
|---|---|
| Digest compared *last* again — the pre-ADR-0523 order | 6 tests, including both staging-seam parametrisations and the bomb tiling test |
| `_hash_remaining` made a no-op | 4 tests — every one of them a *converse* assertion, which is the point: an unhashed tail shows up as a false transport mismatch, not as a missing one |
| A second full pass added to `_hash_remaining` | the tiling test, on both the read-length sum and the no-repeat check |
| `_framing_defect`'s `offset < compressed_size` clause deleted | the boundary-aligned trailing-member test, and *only* it — the review pass that surfaced this found the pre-fix suite green under the same mutation |
| The `from decoded.defect` chain dropped | both staging-seam parametrisations, on the carried-diagnosis assertion |
| The chain-preserving `except` around `_hash_remaining` dropped | the store-fault test |
| `_drain` stops writing entirely | both bomb tests, on the `0 < len(...)` lower bound the review pass added — under the bare `<=` only a sibling test would have caught it |
