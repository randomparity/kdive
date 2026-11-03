# Quarantine-row port opens a domain-free pinned session (#3489)

## Problem

`LocalLibvirtExternalBoot.quarantined_objects`, `observe_object`, `delete_recovery_object` and
`adopt_object` each open a full operation session (`RealLocalExternalBootIO.open` ->
`LocalExternalBootSessionFactory.open`). That open looks up the System domain, parses its XML and
opens the overlay. After System teardown the domain and overlay are gone, so the open fails and a
`managed` receipt that teardown kept (#3467) can no longer be observed, deleted or adopted. The
methods only read or write the quarantine receipt in the recovery metadata store.

## Scope

Add a lease-pinned open that does exactly the ownership half of `open` and nothing else:

- `LocalExternalBootSessionFactory.open_pinned(lease, expected)` pins the lease, runs
  `_require_expected_ownership`, and returns the pin. No libvirt connection, domain or overlay.
  `open` keeps its own checks; this adds no change to it.
- `LocalExternalBootIO.open_pinned(authority, expected)` is a context manager resolving the lease,
  calling the factory, and closing the pin on exit with the same secondary-failure note as `open`.
- The four port methods use `open_pinned` in place of `open`; delete and adopt still re-read and
  write inside it (pinned-write property, ADR-0710 2026-10-01 amendment). ADR-0710 gains an
  append-only amendment. The #3467 design's accepted class is struck with a pointer.
- Not changed: `record_cleanup_quarantine` (#3492), the remote port, the pending-cleanup map.

## Failure model

1. **Actors and deployments.** Local operator, the orphan-resolution service, and the mutation
   service's release-path inventory call (`quarantined_objects`), on a local-libvirt host, through
   the authority seam with an operation lease issued per call. Release now gets the same answer a
   live domain gives (a `managed` receipt lists as `()`) instead of failing closed.
2. **Invariants and assets.** `expected` ownership is still checked against the lease before any
   store access. Delete and adopt write only while an issued lease scope is pinned. A `managed`
   receipt is never deleted by the port. Fencing otherwise rests on what the domain never gave: DB
   selection and the authority-instance check, the System-lane serializer, `observe_object`'s
   full-binding match and the store `flock` (ADR-0787 2026-11-01 amendment).
3. **Accepted failure classes.** A domain or overlay that is absent, wrong or foreign no longer
   fails these four calls. Accepted: they never touch either, and the receipt is keyed by binding.
   A call outside an issued lease scope is refused by the pin (the lease is per-call, so the
   pin proves scope, not identity).
4. **Covered elsewhere.** Cleanup-record session open: #3492. Remote port: no owner (excluded).

## Success

1. With the domain and overlay absent, the four methods observe, list, delete (unmanaged) and
   adopt a kept receipt and return the same results as with a live domain. Fixtures: a `managed` receipt kept by teardown
   (observe; delete refused; list `()`) and an unmanaged receipt of another activation (all four).
2. A lease whose ownership does not match `expected` raises the existing "does not match expected
   ownership" `ValueError` from `open_pinned` and closes the pin.
3. The pin closes exactly once on success and on failure of the body, as with `open`.
4. delete and adopt write while the pin is open.
5. ADR-0710 and the #3467 design reflect the change; ADR-0710's amendment states it overrides
   ADR-0787's 2026-11-01 remark that the local port needs the domain (ADR-0787 itself is outside
   the charter surface).

## Validation

- Contract: `open_pinned` ownership match/mismatch/pin close. `Mode: focused-test`;
  `tests/providers/local_libvirt/lifecycle/boot/test_session.py::test_open_pinned_*`; red:
  `AttributeError`; green: `just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_session.py`.
- Contract: IO `open_pinned` closes the pin and notes a close failure. `Mode: focused-test`;
  `test_external_boot.py::test_open_pinned_*`; red `AttributeError`; same recipe on that file.
- Contract: port methods reach a kept receipt with no domain. `Mode: focused-test`;
  `test_external_boot.py::test_quarantine_port_*_after_teardown`; red: session factory `open`
  raises; green: same file. Includes order evidence (write while pinned).
- Contract: ADR and design-note edits. `Mode: task-test-not-applicable`; prose-only; `just records`
  and `just lint` already validate their shape.
