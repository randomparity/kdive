# Session-pinned cleanup quarantine and finalization (#3012) — implementation plan

Goal: `LocalLibvirtExternalBoot.record_cleanup_quarantine` and `finalize_cleanup_tombstone` make
their store writes under `self._io.open(authority, _expected_binding(recovery.binding))`.

Architecture: two coordinator ports in `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`
gain the same `with self._io.open(...)` wrapper their sibling ports use. The proof check stays
first. The `RealLocalExternalBootIO` store calls are unchanged. Spec:
[2026-09-30-session-pinned-cleanup-finalization-design.md](../specs/2026-09-30-session-pinned-cleanup-finalization-design.md).
Decision: ADR-0710 amendment (2026-09-30), already on the branch.

Tech stack: Python 3.14, pytest, `uv`, `just`.

Expected implementation size: 120–180 changed lines (M) — two port bodies (~20 lines), two
revised tests (~60 lines), one new production-factory test (~70 lines).

## Global Constraints

- Edit only `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` and
  `tests/providers/local_libvirt/test_external_boot.py` (dispatch file scope).
- No change to `RealLocalExternalBootIO`, `RecoveryMetadataStore`, `LocalExternalBootIO`, the
  authority adapter, or any persisted record (spec Design 2).
- Prose rule: avoid "critical", "robust", "comprehensive", "elegant" in comments.
- Guardrails: `just lint`, `just type`,
  `just test-verbose tests/providers/local_libvirt/test_external_boot.py`, then
  `just test-changed`. Run the full gate as `just ci > <file> 2>&1 < /dev/null`.
- A test may not import helpers from another test module (`test_test_module_dependencies.py`).
  Import the libvirt doubles from `tests.providers.local_libvirt.lifecycle.boot.session_support`.

## Task 1: pin both ports to the operation session

Files: modify `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`
(`record_cleanup_quarantine` ~L2961, `finalize_cleanup_tombstone` ~L3105); modify
`tests/providers/local_libvirt/test_external_boot.py`.

Interfaces (existing, confirmed at `b1a8cd8c6`):
- `LocalExternalBootIO.open(authority, expected, *, local_timing=None) -> AbstractContextManager[LocalExternalBootOperation]`
- `_expected_binding(binding: ExternalBootActivationBinding) -> ExpectedOperationOwnership`
- `RealLocalExternalBootIO(recovery_root, materializer, recovery_writer, resolve_operation_lease, session_factory, capacity_bytes)`
- `LocalExternalBootSessionFactory(*, pin_lease, connect, open_artifact_root, open_guest, worker_pid=..., open_overlay=..., fstat_overlay=..., close_overlay_descriptor=...)` in `lifecycle/boot/session.py`
- `LocalOperationLane().pin`, `LocalOperationLeaseScope().issue(authority, binding)` / `.resolve`, `LocalArtifactRoot(root).open` in `lifecycle/boot/session_mechanisms.py`
- `overlay_path(system_id)` in `kdive.providers.shared.runtime_paths`
- `Conn(events, domain)`, `Domain(events, xml)`, `Guest(events)`, `_xml(*, overlay, system_id)` in `tests/providers/local_libvirt/lifecycle/boot/session_support.py`
- Test helpers in `test_external_boot.py`: `_metadata`, `_point`, `_BINDING`, `_cleanup_proof_for`,
  `_RealPreparation`, `_RealSession` (`close_attempts`), `_RealSessionFactory` (`expected` list),
  `_RecordingRecoveryWriter`.

### Verification

- Contract S1 (one lease, one session with the expected ownership, closed once). Mode: focused-test.
  Tests `test_real_adapter_finalization_replays_exact_proof_under_session` and
  `test_real_adapter_quarantine_records_under_session_without_artifact_parents`. Red before the
  source edit: `assert resolutions == [...]` fails with `[]`, because the ports resolve nothing.
  Green: `uv run python -m pytest tests/providers/local_libvirt/test_external_boot.py -q -k "under_session"`.
- Contract S2 (no write when lease or session open fails). Mode: focused-test.
  `test_cleanup_writes_refuse_without_an_operation_session`. Red before the edit: `pytest.raises`
  reports DID NOT RAISE. Green: the same `-k` command with `session_refuses`.
- Contract S3 (a malformed proof opens no session). Mode: focused-test. The quarantine test keeps
  its mismatched-proof `pytest.raises`, then asserts `resolutions == []` before the valid call.
  It is green before and after the edit; it guards the order of check and open.
- Contract S4 (absence after quarantine, finalization and replay with the production factory).
  Mode: focused-test. `test_session_pinned_cleanup_writes_keep_exact_recovery_absence`. Red
  before the edit: running outside a lease scope does not raise, and the test's `pytest.raises(RuntimeError, match="operation lease is not active")` arm reports DID NOT RAISE.
  Green: the same `-k` command with `keep_exact_recovery_absence`.

### Steps

1. In `test_external_boot.py`, replace `test_real_adapter_finalization_replays_exact_proof_without_session`
   with:

```python
def test_real_adapter_finalization_replays_exact_proof_under_session(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("recovered")
    point = _point(metadata)
    host = _RealPreparation(metadata, root)
    session = _RealSession(host)
    factory = _RealSessionFactory(session)
    resolutions: list[str] = []

    def resolve(reference: OpaqueProviderRef) -> LocalExternalBootOperationLease:
        resolutions.append(reference.ref)
        return cast(LocalExternalBootOperationLease, object())

    io = RealLocalExternalBootIO(
        root,
        host,
        _RecordingRecoveryWriter(host),
        resolve,
        cast(LocalExternalBootSessionFactory, factory),
        32 * 1024**3,
    )
    ports = LocalLibvirtExternalBoot(io)
    proof = _cleanup_proof_for(point)
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
        store.publish_tombstone(reference, metadata.binding, metadata, proof.point_digest)

    authority = OpaqueProviderRef(ref="authority/authenticated-by-2140")
    ports.finalize_cleanup_tombstone(point, proof, authority)
    ports.finalize_cleanup_tombstone(point, proof, authority)

    expected = ExpectedOperationOwnership(
        UUID(_BINDING.system_id), UUID(_BINDING.run_id), UUID(_BINDING.activation_id)
    )
    assert resolutions == [authority.ref] * 2
    assert factory.expected == [expected] * 2
    assert session.close_attempts == 2
    assert not (root / recovery_directory_name(point.recovery_ref, point.binding)).exists()
```

2. Replace `test_real_adapter_quarantine_records_without_session_or_artifact_parents` with
   `test_real_adapter_quarantine_records_under_session_without_artifact_parents`: same setup as
   step 1 (`resolutions`, `resolve`, `factory`); keep its two `pytest.raises` blocks unchanged;
   after them assert `resolutions == []`; then call
   `ports.record_cleanup_quarantine(point, proof, authority)` and assert
   `resolutions == [authority.ref]`, `factory.expected == [expected]`,
   `session.close_attempts == 1`, `not (root / metadata.binding.system_id).exists()`, and the
   existing receipt readback. Docstring: "A session open creates no artifact parents
   (ADR-0710), so quarantine records under the session and exact absence stays provable."
3. Add:

```python
def test_cleanup_writes_refuse_without_an_operation_session(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("recovered")
    point = _point(metadata)
    host = _RealPreparation(metadata, root)

    def refuse(_authority: OpaqueProviderRef) -> LocalExternalBootOperationLease:
        raise RuntimeError("operation lease is not active")

    io = RealLocalExternalBootIO(
        root,
        host,
        _RecordingRecoveryWriter(host),
        refuse,
        cast(LocalExternalBootSessionFactory, _RealSessionFactory(_RealSession(host))),
        32 * 1024**3,
    )
    ports = LocalLibvirtExternalBoot(io)
    proof = _cleanup_proof_for(point)
    authority = OpaqueProviderRef(ref="authority/current")
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
        store.publish_tombstone(reference, metadata.binding, metadata, proof.point_digest)

    with pytest.raises(RuntimeError, match="operation lease is not active"):
        ports.record_cleanup_quarantine(point, proof, authority)
    with pytest.raises(RuntimeError, match="operation lease is not active"):
        ports.finalize_cleanup_tombstone(point, proof, authority)

    with RecoveryMetadataStore(root) as store:
        assert store.read_cleanup_quarantine(point.binding) is None
        assert store.cleanup_complete(point.recovery_ref, point)
```

4. Add the production-factory test. Imports to add: `LocalArtifactRoot`, `LocalOperationLane`,
   `LocalOperationLeaseScope` from `kdive.providers.local_libvirt.lifecycle.boot.session_mechanisms`;
   `overlay_path` from `kdive.providers.shared.runtime_paths`; `Conn`, `Domain`, `Guest`, and
   `_xml as _session_xml` from `tests.providers.local_libvirt.lifecycle.boot.session_support`.

```python
def test_session_pinned_cleanup_writes_keep_exact_recovery_absence(tmp_path: Path) -> None:
    """#3012: quarantine, finalization and its ADR-0586 replay leave nothing behind."""
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("recovered")
    point = _point(metadata)
    system_id = UUID(_BINDING.system_id)
    events: list[str] = []
    domain = Domain(events, _session_xml(overlay=overlay_path(system_id), system_id=system_id))
    factory = LocalExternalBootSessionFactory(
        connect=lambda: Conn(events, domain),
        pin_lease=LocalOperationLane().pin,
        open_artifact_root=LocalArtifactRoot(root).open,
        open_guest=lambda: Guest(events),
        worker_pid=4242,
        open_overlay=lambda _path: os.open(os.devnull, os.O_RDONLY),
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=os.close,
    )
    scope = LocalOperationLeaseScope()
    io = RealLocalExternalBootIO(
        root,
        cast(LocalExternalBootMaterializer, object()),
        cast(GuestRecoveryWriter, object()),
        scope.resolve,
        factory,
        32 * 1024**3,
    )
    ports = LocalLibvirtExternalBoot(io)
    proof = _cleanup_proof_for(point)
    authority = OpaqueProviderRef(ref="authority/current")
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
        store.publish_tombstone(reference, metadata.binding, metadata, proof.point_digest)

    with pytest.raises(RuntimeError, match="operation lease is not active"):
        ports.record_cleanup_quarantine(point, proof, authority)
    for write in (
        ports.record_cleanup_quarantine,
        ports.finalize_cleanup_tombstone,
        ports.finalize_cleanup_tombstone,
    ):
        with scope.issue(authority, _BINDING):
            write(point, proof, authority)

    assert events.count(f"domain.open:kdive-{system_id}") == 3
    assert list(root.iterdir()) == []
    with RecoveryMetadataStore(root) as store:
        assert store.exact_recovery_absence(_BINDING)
```

   `kdive-<system_id>` is `domain_name_for` in `kdive.providers.shared.runtime_paths` and the
   `<name>` that `session_support._xml` renders (both confirmed at `b1a8cd8c6`).
5. Run `uv run python -m pytest tests/providers/local_libvirt/test_external_boot.py -q -k "under_session or session_refuses or refuse_without or keep_exact_recovery_absence"`.
   Expected: the new and revised tests fail as listed under Verification. The S3 assertion passes.
6. Edit `record_cleanup_quarantine`: delete the seven-line comment and the bare call, and write

```python
        with self._io.open(authority, _expected_binding(recovery.binding)):
            # Pinned since ADR-0710's 2026-09-30 amendment: session open creates nothing.
            self._io.record_cleanup_quarantine(recovery, proof)
```

7. Edit `finalize_cleanup_tombstone`: keep the first comment paragraph (ADR-0592 proof),
   delete the second paragraph about `authority` being unused, and replace the bare call with

```python
        with self._io.open(authority, _expected_binding(recovery.binding)):
            self._io.finalize_tombstone(recovery, proof)
```

8. Re-run the step-5 command. Expected: all pass.
9. Run `just test-verbose tests/providers/local_libvirt/test_external_boot.py`. Expected: pass.
   A coordinator test whose fake IO (`_ExternalIO`, `_FakeIO`) now records an extra `open` is a
   consequence of this contract. Update its expected count or sequence. Do not change the fake
   to hide the open.
10. Run `just lint`, `just type`, `just test-changed`. Expected: exit 0 each.
11. Commit: `fix(local-libvirt): pin cleanup quarantine and finalization to the session`.

Acceptance: S1–S4 tests green; no remaining comment says these ports are session-free; the diff
touches only the two files named in Global Constraints.
Rollback: revert the commit; no persisted state changes.
