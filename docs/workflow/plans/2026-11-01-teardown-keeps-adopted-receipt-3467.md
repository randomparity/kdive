# System teardown keeps an adopted receipt — implementation plan (#3467)

**Goal:** System teardown leaves an adopted (`managed`) cleanup receipt in place on both
providers and still completes.

**Architecture:** Local teardown's discard path returns without a store write when the standing
receipt is `managed`. System teardown's recovery-absence check counts a recovery directory
holding exactly the tombstone and that managed receipt as absent. Remote already keeps the
receipt; a test pins it.

**Tech stack:** Python 3, pytest (anyio for remote), the repository `just` recipes.

Spec:
[2026-11-01-teardown-keeps-adopted-receipt-3467-design.md](../specs/2026-11-01-teardown-keeps-adopted-receipt-3467-design.md)

Expected implementation size: 110–150 changed lines (M) — about 35 lines of local store and IO
change, about 75 lines across two tests, and the 40-line ADR amendment.

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
| `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` | modify | discard path's managed branch; `exact_recovery_absence(..., adopted_receipt_absent=)`; five teardown callers pass it |
| `tests/providers/local_libvirt/test_external_boot.py` | modify | one local test |
| `tests/providers/remote_libvirt/test_external_boot_authority.py` | modify | one remote test |
| `docs/adr/0787-abortable-finalize-proof-checkpoint.md` | modify | amendment, written with the design |

No ownership move.

## Task 1 — Local teardown keeps an adopted receipt

**Verification:**

- Contract: with a managed receipt, discard writes nothing, teardown completes, and the store
  reads the adopted receipt beside its tombstone; with an unmanaged receipt, teardown absence
  is false. `Mode: focused-test` — `test_system_teardown_keeps_an_adopted_receipt`. Red before
  the fix: `assert directory.is_dir()` fails after discard. Green:
  `uv run pytest -q tests/providers/local_libvirt/test_external_boot.py -k "system_teardown or adopted"`.
- Contract: unmanaged teardown unchanged. `Mode: focused-test` — the existing
  `test_system_teardown_*` cases in the same command stay green.

**Interfaces (existing, confirmed in the repo):** `RecoveryMetadataStore.read_cleanup_quarantine(binding)`,
`adopt_cleanup_quarantine(receipt)`, `_read_tombstone(directory_fd)`, `_open_private_directory`,
module names `_TOMBSTONE_NAME`, `_QUARANTINE_NAME`; test helpers `_quarantined(root)`,
`_system_teardown_io(root, session)`, `_SystemTeardownSession`, `_teardown_intent()`,
`_cleanup_proof_for(point)`, `recovery_directory_name`. New keyword:
`exact_recovery_absence(self, binding, *, adopted_receipt_absent: bool = False) -> bool`.

Steps:

1. Add after `test_re_recording_never_un_adopts_a_cleanup_quarantine`:

```python
def test_system_teardown_keeps_an_adopted_receipt(tmp_path: Path) -> None:
    """#3467: teardown leaves an adopted receipt, as release finalize does (#3435)."""
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    point, receipt = _quarantined(root)
    io = _system_teardown_io(root, _SystemTeardownSession())
    intent = _teardown_intent()
    authority = OpaqueProviderRef(ref="authority/current")
    assert not io.system_teardown_recovery_is_absent(intent, authority)
    with RecoveryMetadataStore(root) as store:
        store.adopt_cleanup_quarantine(receipt)

    io.discard_system_teardown_recovery(intent, point, _cleanup_proof_for(point), authority)

    directory = root / recovery_directory_name(point.recovery_ref, point.binding)
    assert directory.is_dir()
    assert sorted(path.name for path in directory.iterdir()) == [
        "cleanup-quarantine.json",
        "tombstone.json",
    ]
    assert io.system_teardown_recovery_is_absent(intent, authority)
    assert io.teardown_system(intent, authority).complete
    with RecoveryMetadataStore(root) as store:
        assert store.read_cleanup_quarantine(point.binding) == receipt.model_copy(
            update={"managed": True}
        )
        assert not store.exact_recovery_absence(point.binding)
```

2. Run the green command; expect the new test to fail at `assert directory.is_dir()`.
3. In `discard_system_teardown_recovery`, after the proof check:

```python
standing = store.read_cleanup_quarantine(recovery.binding)
if standing is not None and standing.managed:
    # An adopted receipt is the operator's; teardown keeps it (ADR-0787, #3467).
    if standing.tombstone.recovery_point != recovery:
        raise ValueError("adopted cleanup receipt does not match recovery point")
    return
```

   and use `standing` in place of the later `store.read_cleanup_quarantine(...) is None` read.
   In `exact_recovery_absence`, add the keyword. For the `name` candidate only, when the flag is
   set and a new helper `_holds_only_adopted_receipt(directory_fd, binding)` is true, close the
   descriptor and continue instead of returning `False`. The helper returns true when
   `set(os.listdir(directory_fd)) == {_TOMBSTONE_NAME, _QUARANTINE_NAME}`, the receipt from
   `read_cleanup_quarantine(binding)` is `managed`, and `_read_tombstone(directory_fd)` equals
   `receipt.tombstone`. Pass `adopted_receipt_absent=True` at `begin_system_teardown`,
   `observe_system_teardown`, `system_teardown_recovery_is_absent`, and both calls in
   `teardown_system`.
4. Re-run the green command; expect all selected tests to pass.
5. `just lint && just type`; commit `fix(local-libvirt): keep an adopted receipt at System teardown`.

## Task 2 — Remote teardown keeps an adopted receipt

**Verification:** Contract: remote teardown after adoption completes and the port observes the
adopted receipt. `Mode: focused-test` — `test_remote_system_teardown_keeps_an_adopted_receipt`.
It passes on base; red is a controlled fault: in the test host's `remove_system_artifacts`, call
`store.finalize_cleanup_quarantine(receipt)` for the adopted receipt, observe the observation
assert fail, revert. Green:
`uv run pytest -q tests/providers/remote_libvirt/test_external_boot_authority.py -k adopted_receipt`.

**Interfaces (existing, confirmed):** `_cleaned_release`, `_native_context`,
`authority_reference`, `_CompletingTeardownOperations`, `_remote_teardown_request`,
`_remote_teardown_context`, `_REMOTE_TEARDOWN_RESERVATION`,
`RemoteExternalBootAuthorityAdapter(coordinator, executor, *, remote_module_host=, teardown_clock=)`.
`_cleaned_release` publishes a terminal record, so teardown takes the module-host branch.

Steps:

1. Add after `test_remote_finalize_leaves_an_adopted_receipt_in_place`:

```python
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
            cast(Any, _CompletingTeardownOperations()), store, lambda: 300.0
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

2. Run the green command; expect `1 passed`. Apply the controlled fault, expect the observation
   assert to fail, revert.
3. `just lint && just type`; commit `test(remote-libvirt): pin that teardown keeps an adopted receipt`.

## Task 3 — Records

The ADR-0787 amendment is written with the design. `Mode: task-test-not-applicable` — prose with
no executable consumer; `git fetch origin main && just records` checks the record shape.
