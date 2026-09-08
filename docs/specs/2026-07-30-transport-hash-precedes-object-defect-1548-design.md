# Historical verification — The transport hash rules before the gzip object-defect verdict (#1548)

Recorded 2026-07-30 for the then-current checkout. These are historical results; they do not
establish a passing result or supported procedure for the current release.

Mutation-verified. Every row below was **re-run as one set against the final tree**, not carried
forward from the round that added it — the review loop added a test and a row three times, so a
figure taken at the first commit would attest coverage for tests that did not yet exist. The
selection is `tests/artifacts/test_transport_encoding.py` plus
`tests/providers/local_libvirt/test_rootfs_upload_fetch.py`: **118 passed** on the restored tree,
before and after the set, with `__pycache__` cleared between every run.

| Mutation | Reddened (of 118) |
|---|---|
| Digest compared *last* again — the pre-ADR-0523 order | 6, including both staging-seam parametrisations, the exhaustive sweep, and the truncated- and trailing-data transport tests |
| `_hash_remaining` made a no-op | 6 — every one a *converse* assertion, which is the point: an unhashed tail shows up as a false transport mismatch, not as a missing one |
| A second full pass added to `_hash_remaining` | 2, on the read-length sum and the no-repeat check |
| `_framing_defect`'s `offset < compressed_size` clause deleted | 1 — the boundary-aligned trailing-member test, and *only* it. The review pass that surfaced this found the pre-fix suite fully green under the same mutation |
| The `add_note` on the drain's store fault dropped | 1 — the store-fault test |
| The decode pass's short read reverted to `break` | 1 — the `decode-pass` parametrisation of the empty-range test, and only it |
| The drain's short read reverted to `return` | 1 — the `drain` parametrisation, and only it |
| `_drain` stops writing entirely | 7, including both bomb tests via the `0 < len(...)` lower bound a review pass added — under the bare `<=` only unrelated siblings would have caught it |

Decision and executable owners:

- [0445-reconcile-checksum-mismatch-error-category.md](../adr/0445-reconcile-checksum-mismatch-error-category.md)
- [0523-transport-hash-precedes-the-gzip-object-defect-verdict.md](../adr/0523-transport-hash-precedes-the-gzip-object-defect-verdict.md)
- [test_transport_encoding.py](../../tests/artifacts/test_transport_encoding.py)
- [test_rootfs_upload_fetch.py](../../tests/providers/local_libvirt/test_rootfs_upload_fetch.py)
