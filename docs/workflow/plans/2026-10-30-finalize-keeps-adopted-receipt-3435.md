# Release finalize leaves an adopted receipt in place: implementation plan (#3435)

**Goal.** Release `finalize` leaves a `managed` (adopted) cleanup quarantine receipt in place on
both the remote-libvirt and the local-libvirt provider.

**Architecture.** There is one early return in each provider's release-path finalizer:
`RemoteExternalBootAuthorityAdapter.finalize` and `LocalLibvirtExternalBoot.finalize_cleanup_tombstone`.
The store-level removal functions do not change. The spec is
`docs/workflow/specs/2026-10-30-finalize-keeps-adopted-receipt-3435-design.md`, and the
decision is the ADR-0787 amendment dated 2026-10-30.

**Tech stack.** Python 3.14, pytest with anyio, `uv`, and `just`.

Expected implementation size: 50–80 changed lines (M) — two early returns of 3–5 lines each, plus two focused tests of about 25 lines each.

## Global Constraints

- Ruff line length is 100, and `ty` runs strict over the whole tree (`just type`).
- Doc style: no "critical", "robust", "comprehensive" or "elegant".
- No schema, protocol, SQL or service change.
- Do not edit `RecoveryMetadataStore.finalize_tombstone` or
  `RemoteModuleVolumePreparationStore.finalize_cleanup_quarantine`. System teardown and the
  recovery-object `delete` share them.
- Guardrails: `just lint`, `just type`, `just test-changed`, `git fetch origin main && just records`.

## File map

| File | Change |
|---|---|
| `src/kdive/providers/remote_libvirt/external_boot_authority.py` | `finalize` returns on a managed receipt |
| `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` | `finalize_cleanup_tombstone` returns on a managed receipt |
| `tests/providers/remote_libvirt/test_external_boot_authority.py` | add one test |
| `tests/providers/local_libvirt/test_external_boot.py` | add one test |

## Task 1 — Remote finalize leaves an adopted receipt

**Verification.**

- Contract: after an adopt, remote `finalize` changes nothing.
  - Mode: `focused-test`.
  - Test: `test_remote_finalize_leaves_an_adopted_receipt_in_place`.
  - Red: `AssertionError` on the first assertion, because the observation is no longer the adopted one.
  - Green: `uv run python -m pytest tests/providers/remote_libvirt/test_external_boot_authority.py -q -k "finalize"`.

**Interfaces.** This task uses these existing functions:

- `_cleaned_release(tmp_path) -> (adapter, record, request, context)`;
- `adapter.cleanup_quarantine_inventory(request)`;
- `adapter.adopt_recovery_object(binding, authority, digest)`;
- `adapter.finalize(request, context)`;
- `authority_reference(request)`;
- `store.read_cleanup_quarantine(binding, plan_identity)`;
- `store._finalized_name(binding, plan_identity)`;
- `store._read(name)`.

Step 1. Add this test after `test_remote_recovery_record_delete_and_adopt_are_digest_fenced`:

```python
@pytest.mark.anyio
async def test_remote_finalize_leaves_an_adopted_receipt_in_place(tmp_path: Path) -> None:
    adapter, record, request, context = await _cleaned_release(tmp_path)
    store = adapter._coordinator._store
    (observed,) = await adapter.cleanup_quarantine_inventory(request)
    authority = authority_reference(request)
    adopted = await adapter.adopt_recovery_object(
        observed.binding, authority, observed.observed_digest
    )

    await adapter.finalize(request, context)

    assert await adapter.observe_recovery_object(observed.binding, authority) == adopted
    receipt = store.read_cleanup_quarantine(record.binding, record.plan_identity)
    assert receipt is not None and receipt.managed
    assert adapter._coordinator.recovery_point(record.binding, record.plan_identity)
    assert store._read(store._finalized_name(record.binding, record.plan_identity)) is None
    adapter.close()
```

Step 2. Run the green command and confirm that the new test fails with `AssertionError` on the `observe_recovery_object(...) == adopted` line.

Step 3. In `finalize`, replace `if receipt is None:` / `return` with this:

```python
        if receipt is None or receipt.managed:
            # An adopted receipt is the operator's; finalize leaves it in place (ADR-0787).
            return
```

Step 4. Run the green command again. Expect every selected test to pass. Then run `just lint`
and `just type`, and commit.

## Task 2 — Local finalize leaves an adopted receipt

**Verification.**

- Contract: after an adopt, `finalize_cleanup_tombstone` changes nothing.
  - Mode: `focused-test`.
  - Test: `test_real_adapter_finalization_leaves_an_adopted_receipt_in_place`.
  - Red: the assertion that the tombstone directory exists fails.
  - Green: `uv run python -m pytest tests/providers/local_libvirt/test_external_boot.py -q -k "finalization or quarantine or adopt"`.

**Interfaces.** This task uses these existing helpers and methods:

- `_quarantined(root) -> (point, receipt)`;
- `_cleanup_proof_for(point)`;
- `_metadata`;
- `_RealPreparation`;
- `_RealSession`;
- `_resolving_io(root, host, session, resolutions)`;
- `RecoveryMetadataStore.adopt_cleanup_quarantine(receipt)`;
- `RecoveryMetadataStore.read_cleanup_quarantine(binding)`;
- `recovery_directory_name(reference, binding)`;
- `self._io.read_cleanup_quarantine(binding)`, which already exists on the `_io` protocol and is
  used by `quarantined_objects`.

Step 1. Add this test after `_assert_disposed`:

```python
def test_real_adapter_finalization_leaves_an_adopted_receipt_in_place(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    point, receipt = _quarantined(root)
    with RecoveryMetadataStore(root) as store:
        store.adopt_cleanup_quarantine(receipt)
    host = _RealPreparation(_metadata("recovered"), root)
    io, _factory = _resolving_io(root, host, _RealSession(host), [])
    ports = LocalLibvirtExternalBoot(io)

    ports.finalize_cleanup_tombstone(
        point, _cleanup_proof_for(point), OpaqueProviderRef(ref="authority/current")
    )

    assert (root / recovery_directory_name(point.recovery_ref, point.binding)).is_dir()
    with RecoveryMetadataStore(root) as store:
        held = store.read_cleanup_quarantine(point.binding)
    assert held == receipt.model_copy(update={"managed": True})
```

Step 2. Run the green command and confirm that the new test fails on the `is_dir()` assertion.

Step 3. Replace the body of `finalize_cleanup_tombstone` after its comment with this:

```python
        with self._io.open(authority, _expected_binding(recovery.binding)):
            receipt = self._io.read_cleanup_quarantine(recovery.binding)
            if receipt is not None and receipt.managed:
                # An adopted receipt is the operator's; finalize leaves it in place (ADR-0787).
                return
            self._io.finalize_tombstone(recovery, proof)
```

Step 4. Run the green command again. Expect every selected test to pass. Then run `just lint`,
`just type` and `just test-changed`, and commit.

## Rollback

Revert the two commits. No persisted format changes. A managed receipt carries the same bytes
before and after this change.
