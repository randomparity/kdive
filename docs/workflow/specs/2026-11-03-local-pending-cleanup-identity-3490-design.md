# Key the local pending cleanup point by the identity that ran (#3490)

## Problem

`LocalExternalBootAuthorityAdapter._apply` stores a pending cleanup point under
`context.operation_identity`; `finalize` and `_commit` read it under `request.operation_identity`.
For a release retry that adopted an earlier generation's cleanup the two differ: the service
sends `finalize` the retry's request with the context of the operation that ran
(`_proven_cleanup_context`, #3416, #3434). The lookup misses, finalization succeeds only through
the durable cleanup receipt, the earlier entry is never popped, and the map has no size bound
unlike `_pending_absence`.

## Design

- `finalize` and `_commit` read the map under `context.operation_identity`, the key `_apply`
  stores under. For a non-adopted operation the two identities are equal, so behavior is
  unchanged there. Only the `finalize` read is test-guarded; the `_commit` read is a fallback that
  runs only when no durable receipt resolves the point, and is switched for key consistency.
- `_observe` has no context and keeps its request-keyed read. It runs only after a commit with
  equal identities, or for an adopted retry, where the durable receipt resolves the point.
- Storing goes through `_remember_cleanup_point`, which drops the oldest entry at
  `_MAX_ADMITTED_LANES`, as `_pending_absence` does. Eviction is safe: a missing entry falls back
  to the durable receipt, exactly as after a restart.
- Finalization order is untouched: the entry is popped before the same durable finalize call.

## Tests

`test_an_adopted_release_cleanup_finalizes_once_and_leaves_no_pending_point` drives the real
service through a release aborted once and twice before finalize under successor takeovers,
parametrized over 0 and 1 interrupted retries, and asserts one cleanup, one finalize under the
proof that ran, and an empty pending map. `test_pending_cleanup_points_are_bounded` covers the
bound. Reverting `finalize` to the request key turns the first red.

## Out of scope

Remote provider (covered at `tests/providers/remote_libvirt/test_external_boot_authority.py`);
service context construction.
