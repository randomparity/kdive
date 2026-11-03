# Quarantine-row port domain-free open Implementation Plan (#3489)

**Goal:** the four local quarantine-row port methods work after System teardown.
**Architecture:** add `open_pinned` on the session factory and on the IO adapter; switch four call
sites. **Tech stack:** Python, pytest, `just`.

## Global Constraints

Zero lint/type warnings (`just lint`, `just type`); do not touch `record_cleanup_quarantine`;
ADR-0710 is append-only (new `### Amendment` section only).

Expected implementation size: 150–230 changed lines (M) — one factory method, one IO method, four
call-site edits, fake updates and about six tests including rewrites of pinned-write tests.

## File map

- `src/kdive/providers/local_libvirt/lifecycle/boot/session.py`: factory `open_pinned`.
- `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`: IO protocol + real IO
  `open_pinned`; port calls at the four sites (`quarantined_objects`, `observe_object`,
  `delete_recovery_object`, `adopt_object`).
- tests: `tests/providers/local_libvirt/lifecycle/boot/test_session.py`,
  `tests/providers/local_libvirt/test_external_boot.py`: IO fakes (`_ExternalIO`) and
  session-factory fakes (`_RealSessionFactory`, `_SecondOpenRefused`) gain a genuine `open_pinned`
  that does not delegate to `open`, with a pin open/close counter; also
  `tests/jobs/handlers/external_boot/test_orphan_resolution.py` (`_LocalSessionFactoryBoundary`),
  `test_external_boot_authority.py` and `tests/providers/contract/bindings/local_libvirt.py`.
  Rewrite targets: `test_recovery_object_writes_refuse_when_the_write_session_cannot_open[session]`
  (replace with an ownership-mismatch arm; keep the lease arm), the
  `domain.open` count and `factory.expected`/`close_attempts` assertions in
  `test_session_pinned_recovery_object_writes_with_the_production_factory`, and
  `_WriteObservingIO.open_sessions_at_write` (count pins, not sessions).
- docs: ADR-0710 amendment; #3467 design accepted-class line.

## Task 1: factory `open_pinned`

Interfaces: consumes `self._pin_lease(lease)` and `_require_expected_ownership(facts, expected)`
(both exist in `session.py`); provides
`def open_pinned(self, lease: LocalExternalBootOperationLease, expected: ExpectedOperationOwnership) -> LocalExternalBootOperationPin`.

Verification: `Mode: focused-test` for ownership match, mismatch (pin closed, ValueError). Red:
`AttributeError: ... open_pinned`. Green: `just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_session.py`.

Code, placed after `open_teardown`:

```python
    def open_pinned(
        self,
        lease: LocalExternalBootOperationLease,
        expected: ExpectedOperationOwnership,
    ) -> LocalExternalBootOperationPin:
        """Pin the lease and check ownership only: no libvirt, domain or overlay."""
        ownership = self._pin_lease(lease)
        pin = ownership._pin
        try:
            _require_expected_ownership(ownership.ownership, expected)
        except BaseException as exc:
            try:
                pin.close()
            except BaseException as close_error:
                exc.add_note(f"cleanup failed: {close_error!r}")
            raise
        return pin
```

## Task 2: IO `open_pinned` and call sites

Interfaces: consumes Task 1; provides
`def open_pinned(self, authority: OpaqueProviderRef, expected: ExpectedOperationOwnership) -> AbstractContextManager[None]`
on `LocalExternalBootIO` and `RealLocalExternalBootIO`.

Verification: `Mode: focused-test` for pin close on success, on body failure with note, and the
port methods after teardown (fake factory with `open` raising; `open_pinned` returning a pin).
Red: `AttributeError` / the raising `open`. Green: `just test-verbose tests/providers/local_libvirt/test_external_boot.py`.

Real IO code, after `open`:

```python
    @contextmanager
    def open_pinned(
        self, authority: OpaqueProviderRef, expected: ExpectedOperationOwnership
    ) -> Iterator[None]:
        pin = self._session_factory.open_pinned(self._resolve_operation_lease(authority), expected)
        try:
            yield
        except BaseException as primary:
            try:
                pin.close()
            except BaseException as close_error:
                primary.add_note(f"cleanup failed: {close_error!r}")
            raise
        else:
            pin.close()
```

Replace `self._io.open(authority, _expected_binding(...))` with `self._io.open_pinned(...)` in the
four methods. Update fakes (`_ExternalIO`, authority and contract fakes) to provide `open_pinned`.

## Task 3: docs

Append `### Amendment (2026-11-03): quarantine-row port opens pinned without a domain (#3489)` to
ADR-0710 (decision; consequence: the 2026-10-01 amendment's "missing domain already fails" no longer holds
for these four, and ADR-0787's 2026-11-01 remark that the local port needs the domain is
overridden; fencing is DB selection, authority-instance check, System-lane serializer,
`observe_object` binding match, store `flock`, and the pin requiring an issued lease scope;
rejected: reuse `open_teardown` -- judgment: opens a libvirt connection these methods never use;
skip the pin -- judgment: the write would run outside an issued lease scope).
Strike the accepted class in the #3467 design with a pointer to #3489. Follow-up candidate: an
ADR-0787 append-only pointer (outside this charter's surface).
Verification: `Mode: task-test-not-applicable` prose; `just records`, `just lint`.
