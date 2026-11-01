# System teardown keeps an adopted receipt — implementation plan (#3467)

**Goal:** System teardown leaves an adopted (`managed`) cleanup receipt in place on both
providers and still completes.

**Architecture:** Local teardown's discard path gains a managed-receipt branch that removes the
activation's intent, preparation and tombstone but keeps the receipt. System teardown's
recovery-absence check treats a recovery directory holding only that receipt as absent. Remote
already keeps the receipt; a test pins it.

**Tech stack:** Python 3, pytest (anyio for remote), the repository `just` recipes.

Spec: [2026-11-01-teardown-keeps-adopted-receipt-3467-design.md](../specs/2026-11-01-teardown-keeps-adopted-receipt-3467-design.md)

Expected implementation size: 140–200 changed lines (M) — two local store/IO edits of about
50 lines, three tests of about 110 lines, and a 35-line ADR amendment.

## Global Constraints

- No schema, migration, protocol, or SQL change.
- Do not edit the remote lifecycle-completion region (#3469, PR #3480) or add remote store-file
  collection (#3472).
- Doc wording: no "critical", "robust", "comprehensive", "elegant".
- Guardrails while iterating: `just lint`, `just type`, `just test-changed`; before push
  `git fetch origin main && just records`.

## File map

| File | Change | Owns after |
|---|---|---|
| `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` | modify | discard path's managed branch; `RecoveryMetadataStore.retire_adopted_tombstone`; `exact_recovery_absence(..., adopted_receipt_absent=)`; five teardown callers pass the flag |
| `tests/providers/local_libvirt/test_external_boot.py` | modify | two local tests |
| `tests/providers/remote_libvirt/test_external_boot_authority.py` | modify | one remote test |
| `docs/adr/0787-abortable-finalize-proof-checkpoint.md` | modify | 2026-11-01 amendment (written with the design) |

No ownership move: the local store keeps its absence proof; the discard path stays its only
teardown caller.

## Task 1 — Local teardown keeps an adopted receipt

**Verification:**

- Contract: discard + teardown with a managed receipt keeps it and completes.
  `Mode: focused-test` — `test_system_teardown_keeps_an_adopted_receipt`; red before the fix
  with `assert held == adopted` failing (`held` is `None`); green command
  `uv run pytest -q tests/providers/local_libvirt/test_external_boot.py -k adopted_receipt`.
- Contract: retire is repeatable after a crash before the tombstone unlink.
  `Mode: focused-test` — `test_system_teardown_keeps_an_adopted_receipt_after_a_crash`; red
  before the fix because base `finalize_tombstone` unlinks the receipt before the injected
  tombstone crash, so the retry ends with no receipt and the directory assert fails; same green
  command.
- Contract: unmanaged teardown unchanged. `Mode: focused-test` — existing
  `-k system_teardown` cases stay green.

**Interfaces:**

- New `RecoveryMetadataStore.retire_adopted_tombstone(self, recovery: RecoveryPoint) -> None`.
- `RecoveryMetadataStore.exact_recovery_absence(self, binding: ExternalBootActivationBinding,
  *, adopted_receipt_absent: bool = False) -> bool` (keyword added; default keeps behaviour).
- Existing, confirmed: `read_cleanup_quarantine(binding) -> CleanupQuarantineReceiptV1 | None`,
  `_validate_tombstone_residue(directory_fd, entries, expected)`,
  `prune_empty_activation_parents(binding)`, `_open_private_directory`, `_read_tombstone`,
  module names `_TOMBSTONE_NAME`, `_INTENT_NAME`, `_PREPARATION_NAME`, `_QUARANTINE_NAME`.
  Test helpers `_quarantined`, `_system_teardown_io`, `_SystemTeardownSession`,
  `_teardown_intent`, `_cleanup_proof_for`, `LocalLibvirtExternalBoot._quarantine_observation`.

Steps:

1. Add the tests to `tests/providers/local_libvirt/test_external_boot.py` after
   `test_re_recording_never_un_adopts_a_cleanup_quarantine`:

```python
def _adopted_teardown(
    root: Path,
) -> tuple[
    RecoveryPoint, CleanupQuarantineReceiptV1, RealLocalExternalBootIO, _SystemTeardownSession
]:
    point, receipt = _quarantined(root)
    with RecoveryMetadataStore(root) as store:
        store.adopt_cleanup_quarantine(receipt)
    session = _SystemTeardownSession()
    return (
        point,
        receipt.model_copy(update={"managed": True}),
        _system_teardown_io(root, session),
        session,
    )


def _assert_teardown_kept(
    root: Path,
    point: RecoveryPoint,
    adopted: CleanupQuarantineReceiptV1,
    io: RealLocalExternalBootIO,
) -> None:
    intent = _teardown_intent()
    authority = OpaqueProviderRef(ref="authority/current")
    assert io.system_teardown_recovery_is_absent(intent, authority)
    result = io.teardown_system(intent, authority)
    assert result.complete
    directory = root / recovery_directory_name(point.recovery_ref, point.binding)
    assert sorted(path.name for path in directory.iterdir()) == ["cleanup-quarantine.json"]
    with RecoveryMetadataStore(root) as store:
        assert store.read_cleanup_quarantine(point.binding) == adopted
        assert not store.exact_recovery_absence(point.binding)


def test_system_teardown_keeps_an_adopted_receipt(tmp_path: Path) -> None:
    """#3467: teardown leaves an adopted receipt, as release finalize does (#3435)."""
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    point, adopted, io, _session = _adopted_teardown(root)
    intent = _teardown_intent()
    authority = OpaqueProviderRef(ref="authority/current")
    assert not io.system_teardown_recovery_is_absent(intent, authority)

    io.discard_system_teardown_recovery(intent, point, _cleanup_proof_for(point), authority)

    _assert_teardown_kept(root, point, adopted, io)
```

   and the crash case, which fails `os.unlink` of the tombstone once, then retries:

```python
def test_system_teardown_keeps_an_adopted_receipt_after_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    point, adopted, io, _session = _adopted_teardown(root)
    intent = _teardown_intent()
    authority = OpaqueProviderRef(ref="authority/current")
    proof = _cleanup_proof_for(point)
    original = external_boot_module.os.unlink

    def crash_on_tombstone(path: str, *, dir_fd: int | None = None) -> None:
        if path == external_boot_module._TOMBSTONE_NAME:
            raise OSError("crashed before the tombstone unlink")
        original(path, dir_fd=dir_fd)

    monkeypatch.setattr(external_boot_module.os, "unlink", crash_on_tombstone)
    with pytest.raises(OSError, match="crashed before the tombstone unlink"):
        io.discard_system_teardown_recovery(intent, point, proof, authority)
    monkeypatch.setattr(external_boot_module.os, "unlink", original)

    io.discard_system_teardown_recovery(intent, point, proof, authority)

    _assert_teardown_kept(root, point, adopted, io)
```

2. Run `uv run pytest -q tests/providers/local_libvirt/test_external_boot.py -k adopted_receipt`.
   Expect both new tests to fail (receipt `None` / no `OSError` raised).
3. Implement in `external_boot.py`. In `discard_system_teardown_recovery`, after the proof
   check and before `cleanup_complete`:

```python
            standing = store.read_cleanup_quarantine(recovery.binding)
            if standing is not None and standing.managed:
                # An adopted receipt is the operator's; teardown keeps it (ADR-0787, #3467).
                if standing.tombstone.recovery_point != recovery:
                    raise ValueError("adopted cleanup receipt does not match recovery point")
                store.retire_adopted_tombstone(recovery)
                return
```

   Add to `RecoveryMetadataStore`, after `finalize_tombstone`:

```python
    def retire_adopted_tombstone(self, recovery: RecoveryPoint) -> None:
        """Remove the activation's cleanup files but its adopted receipt (ADR-0787, #3467)."""
        self._require_open()
        name = recovery_directory_name(recovery.recovery_ref, recovery.binding)
        expected = CleanupTombstoneV1(
            binding=recovery.binding,
            recovery_point=recovery,
            point_digest=LocalLibvirtExternalBoot.point_digest(recovery),
        )
        receipt = self.read_cleanup_quarantine(recovery.binding)
        if receipt is None or not receipt.managed or receipt.tombstone != expected:
            raise ValueError("adopted cleanup receipt does not match recovery point")
        directory_fd = _open_private_directory(self._root_fd, name)
        try:
            entries = set(os.listdir(directory_fd))
            if entries - {_TOMBSTONE_NAME, _INTENT_NAME, _PREPARATION_NAME, _QUARANTINE_NAME}:
                raise ValueError("cleanup tombstone directory contains unexpected payload")
            if entries != {_QUARANTINE_NAME}:
                if self._read_tombstone(directory_fd) != expected:
                    raise ValueError("cleanup tombstone changed before teardown")
                self._validate_tombstone_residue(directory_fd, entries, expected)
                for residual in (_INTENT_NAME, _PREPARATION_NAME, _TOMBSTONE_NAME):
                    if residual in entries:
                        os.unlink(residual, dir_fd=directory_fd)
                os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self.prune_empty_activation_parents(recovery.binding)
```

   Change `exact_recovery_absence` to take `*, adopted_receipt_absent: bool = False`; for the
   `name` candidate only, when the flag is set and the directory's entries are exactly
   `{_QUARANTINE_NAME}` and `read_cleanup_quarantine(binding)` is `managed`, continue instead of
   returning `False`. Pass `adopted_receipt_absent=True` at the five teardown callers:
   `begin_system_teardown`, `observe_system_teardown`, `system_teardown_recovery_is_absent`, and
   the two calls in `teardown_system`.
4. Re-run the step 2 command; expect `2 passed`. Run
   `uv run pytest -q tests/providers/local_libvirt/test_external_boot.py -k "system_teardown or adopted or quarantine"`;
   expect all passed.
5. `just lint && just type`; commit `fix(local-libvirt): keep an adopted receipt at System teardown`.

## Task 2 — Remote teardown keeps an adopted receipt

**Verification:**

- Contract: remote teardown after adoption completes and the receipt observes as adopted.
  `Mode: focused-test` — `test_remote_system_teardown_keeps_an_adopted_receipt`; passes on
  base, so its red is a controlled fault: patch
  `RemoteExternalBootCoordinator.remove_system_artifacts` in the test to also call
  `self._store.finalize_cleanup_quarantine(receipt)` for the adopted receipt, observe the
  observation assert fail, then revert. Green: `uv run pytest -q tests/providers/remote_libvirt/test_external_boot_authority.py -k adopted_receipt`.

**Interfaces:** existing, confirmed helpers `_cleaned_release`, `_native_context`,
`authority_reference`, `_CompletingTeardownOperations`, `_remote_teardown_request`,
`_remote_teardown_context`, `_REMOTE_TEARDOWN_RESERVATION`,
`RemoteExternalBootAuthorityAdapter(coordinator, executor, *, remote_module_host=..., teardown_clock=...)`.

Steps:

1. Add after `test_remote_finalize_leaves_an_adopted_receipt_in_place`:

```python
class _ReapingTeardownOperations(_CompletingTeardownOperations):
    def unfinished_preparation_absent(self, _state: object) -> bool:
        self.calls.append("unfinished-preparation")
        return True


class _ReapingModuleHost:
    async def execute_system_teardown(self, _exact: object) -> object:
        return object()


@pytest.mark.anyio
async def test_remote_system_teardown_keeps_an_adopted_receipt(tmp_path: Path) -> None:
    """#3467: teardown leaves an adopted receipt in place, as release finalize does (#3435)."""
    adapter, record, request, _context = await _cleaned_release(tmp_path)
    store = adapter._coordinator._store
    (observed,) = await adapter.cleanup_quarantine_inventory(request, _native_context(request))
    authority = authority_reference(request)
    adopted = await adapter.adopt_recovery_object(
        observed.binding, authority, observed.observed_digest
    )
    teardown_adapter = RemoteExternalBootAuthorityAdapter(
        RemoteExternalBootCoordinator(
            cast(Any, _ReapingTeardownOperations()), store, lambda: 300.0
        ),
        RemoteModulePreparationExecutor(),
        remote_module_host=cast(Any, _ReapingModuleHost()),
        teardown_clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    teardown = _remote_teardown_request()

    result = await teardown_adapter.execute_system_teardown(
        teardown, _remote_teardown_context(teardown), _REMOTE_TEARDOWN_RESERVATION
    )

    assert result.complete
    assert await adapter.observe_recovery_object(observed.binding, authority) == adopted
    receipt = store.read_cleanup_quarantine(record.binding, record.plan_identity)
    assert receipt is not None and receipt.managed
    adapter.close()
```

2. Run the green command; expect `1 passed`. Apply the controlled fault, expect a failure on the
   observation assert, revert.
3. `just lint && just type`; commit `test(remote-libvirt): pin that teardown keeps an adopted receipt`.

## Task 3 — Records

The ADR-0787 amendment is written with the design. `Mode: task-test-not-applicable` — prose with
no executable consumer; `git fetch origin main && just records` checks the record shape.
