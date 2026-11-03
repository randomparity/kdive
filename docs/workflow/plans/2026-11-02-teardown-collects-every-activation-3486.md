# Teardown collects every activation of the System (#3486) — implementation plan

Goal: a `complete` remote System teardown collects the store files of every activation of the
System it can prove safe, and refuses late stages for all of them.

Architecture: the store (`RemoteModuleVolumePreparationStore`) gains System-wide refusal,
activation discovery by file content, per-activation volume claims, and a two-group unlink order.
The libvirt operations class gains a read-only pool-absence proof. The coordinator's existing
`collect_torn_down_activation(record)` proves each sibling, then asks the store to collect.

Tech stack: Python 3.14, pydantic models, `uv`, pytest with `anyio`, `just` recipes.

Spec: [2026-11-02-teardown-collects-every-activation-3486-design.md](../specs/2026-11-02-teardown-collects-every-activation-3486-design.md).
ADR: [ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md) amendment (#3486), written
with this design.

Expected implementation size: 450–650 changed lines (L) — from the file map below: about 170
source lines in the store and coordinator, 20 in operations, 260–400 test lines, 30 runbook lines.

## Global Constraints

- Python 3.14; no new dependency.
- Ruff line length 100, lint set `E,F,I,UP,B,SIM`; `ty` strict over src and tests (`just type`).
- Prose: no "critical", "robust", "comprehensive", "elegant"; "Milestone", never "Sprint".
- No migration and no new ADR; ADR-0787 is amended by appending only.
- Exclusions (operator, 2026-11-02): no `managed` re-check in remote finalize; no orphan
  `.recovery` handling; no scan bounds (#3475); no GC of markers or `.teardown` records; no
  local-libvirt change; no sweep or scheduler; never remove a sibling's pool volume.
- Guardrails while iterating: `just lint`, `just type`, `just test-changed`. Before push:
  `git fetch origin main && just records`. Push runs `just ci` through the pre-push hook.
- Commit after each task. Commit before any controlled-fault arm: `git checkout --` restores
  HEAD.

## File map

| file | today | after |
|---|---|---|
| `src/kdive/providers/remote_libvirt/external_boot_authority.py` | store collects the named activation; per-activation `torn_down` | System-wide `system_torn_down`; `system_activations`, `activation_claims`, `collect_proven_activation`; anchors-last unlink order; coordinator proves siblings |
| `src/kdive/providers/remote_libvirt/external_boot_operations.py` | teardown proofs for the named activation | adds `activation_volumes_absent` |
| `tests/providers/remote_libvirt/test_external_boot_authority.py` | #3472 collection tests | sibling discovery, claims, proof, mixed-System and crash tests; `_system_teardown_key` callers updated |
| `docs/operating/runbooks/remote-libvirt-host-setup.md` | no store-maintenance section | "Clear store files of torn-down Systems" procedure |

No caller outside these files uses `torn_down`, `_system_teardown_key`, `_recovery_files` or the
store's `collect_torn_down_activation` (checked with
`rg -n "torn_down\(|_system_teardown_key|_recovery_files|collect_torn_down_activation" src tests`).

## Task 1 — System-wide teardown refusal

Files: modify `src/kdive/providers/remote_libvirt/external_boot_authority.py`; test in
`tests/providers/remote_libvirt/test_external_boot_authority.py`.

Interfaces produced: `RemoteModuleVolumePreparationStore.system_torn_down(system_id: str) -> bool`;
`RemoteModuleVolumePreparationStore._system_teardown_key(system_id: str) -> str` (was
`binding`). Later tasks call `system_torn_down`.

Verification:

- Contract: a stage of an activation the `.teardown` record does not name is refused once the
  record is `complete`. Mode: focused-test. Test
  `test_remote_stage_of_a_sibling_after_system_teardown_is_refused`. Red: `DID NOT RAISE
  RemoteCleanupFinalizedError`. Green: `uv run python -m pytest
  tests/providers/remote_libvirt/test_external_boot_authority.py -q -k
  "after_system_teardown_is_refused or finalize_after_teardown"` passes.
- Contract: finalize of a sibling writes nothing after teardown. Mode: focused-test, the same
  test asserts the store names are unchanged after `finalize_cleanup_quarantine`.

Steps:

1. Add the test after `test_remote_stage_after_system_teardown_is_refused`:

   ```python
   @pytest.mark.anyio
   async def test_remote_stage_of_a_sibling_after_system_teardown_is_refused(
       tmp_path: Path,
   ) -> None:
       """#3486: the refusal covers every activation of the torn-down System."""
       adapter, record, _request, _context, store = await _finalized_release_store(tmp_path)
       sibling = _sibling(record, "00000000-0000-4000-8000-0000000000aa")
       _publish_terminal(store, sibling)
       staged = store.reopen_terminal(sibling.binding, sibling.plan_identity).request
       assert await _system_teardown(store, _ProvingTeardownOperations())

       with pytest.raises(external_boot_authority.RemoteCleanupFinalizedError):
           store.stage(staged, 1.0)
       assert store.cleanup_finalized(sibling.binding, sibling.plan_identity)
       adapter.close()
   ```

   Add the two helpers it needs near `_collecting_teardown_adapter` (Task 4 reuses them):

   ```python
   def _sibling(
       record: RemoteExternalBootRecoveryRecord, activation_id: str
   ) -> RemoteExternalBootRecoveryRecord:
       return record.model_copy(
           update={"binding": record.binding.model_copy(update={"activation_id": activation_id})}
       )


   class _ProvingTeardownOperations(_CollectingTeardownOperations):
       """Collecting teardown host whose pool still holds the volumes of ``keep`` (#3486).

       Every fixture activation renders the same module volume names, so presence is keyed by
       activation id; Task 3 tests the real name-based proof.
       """

       def __init__(self, *, keep: frozenset[str] = frozenset()) -> None:
           super().__init__()
           self.keep = keep
           self.proved: list[str] = []

       def activation_volumes_absent(self, binding: Any, refs: Any) -> bool:
           assert refs.module_volumes
           self.proved.append(binding.activation_id)
           return binding.activation_id not in self.keep
   ```

   Until Task 4 the coordinator never calls `activation_volumes_absent`; the test still fails red
   at the stage assertion.
2. Run the focused command; expect `DID NOT RAISE`.
3. In the store, change `_system_teardown_key` to take `system_id: str` and hash
   `system_id.encode()` (same bytes as before). Update its three store callers to pass
   `intent.binding.system_id`, `anchor.binding.system_id` and `record.intent.binding.system_id`,
   and the test helper `_teardown_name` to `store._system_teardown_key(_record().binding.system_id)`.
4. Replace `torn_down` with:

   ```python
   def system_torn_down(self, system_id: str) -> bool:
       """Whether this System's teardown completed; it covers every activation (#3486).

       A System id is never re-activated: ``systems.id`` is generated per row and ``torn_down``
       has no outgoing edge, so refusing every activation refuses nothing live.
       """
       data = self._read(f"{self._system_teardown_key(system_id)}.teardown")
       if data is None:
           return False
       return RemoteSystemTeardownRecordV1.from_canonical_json(data).phase == "complete"
   ```

   `cleanup_finalized` returns `... or self.system_torn_down(binding.system_id)`; its docstring
   reads "Whether finalize removed, or is removing, its records, or its System's teardown
   completed." `finalize_cleanup_quarantine` checks
   `if not self.system_torn_down(receipt.binding.system_id):` and its docstring says "for any
   activation of a torn-down System". In `RemoteExternalBootCoordinator.system_teardown_state`
   use `if self._store.system_torn_down(binding.system_id):`.
5. Run the focused command; expect pass. Run `uv run python -m pytest
   tests/providers/remote_libvirt/test_external_boot_authority.py -q`; expect pass (the
   existing #3472 tests use the named activation, which stays refused).
6. `just lint && just type`, then commit `fix(remote-libvirt): refuse every activation of a
   torn-down System (#3486)`.

## Task 2 — Store discovery, claims and anchors-last collection

Files: modify `src/kdive/providers/remote_libvirt/external_boot_authority.py`; tests in
`tests/providers/remote_libvirt/test_external_boot_authority.py`.

Interfaces consumed: `system_torn_down` (Task 1). Interfaces produced (Task 4 calls them):

```python
def system_activations(self, system_id: str) -> list[tuple[ExternalBootActivationBinding, str]]
def activation_claims(
    self, binding: ExternalBootActivationBinding, plan_identity: str
) -> RemoteFinalizedTeardownRefsV1 | None
def collect_torn_down_activation(
    self, binding: ExternalBootActivationBinding, plan_identity: str, *, keep_plan: bool = False
) -> None
def collect_proven_activation(
    self,
    binding: ExternalBootActivationBinding,
    plan_identity: str,
    proven: RemoteFinalizedTeardownRefsV1 | None,
    *,
    keep_plan: bool,
) -> bool
```

Verification:

- Contract: discovery returns each activation of the System once and none of another System.
  Mode: focused-test `test_remote_system_activations_find_every_activation_by_content`. Red:
  `AttributeError: ... has no attribute 'system_activations'`. Green: `uv run python -m pytest
  tests/providers/remote_libvirt/test_external_boot_authority.py -q -k "system_activations or
  activation_claims or proven_activation or keep_plan"`.
- Contract: claims are `None` for a finalized activation and otherwise name the terminal's module
  volumes, the nonce and the boot artifacts. Mode: focused-test
  `test_remote_activation_claims_name_every_volume_and_none_once_finalized`.
- Contract: a sibling whose claims grew after the proof is not collected. Mode: focused-test
  `test_remote_proven_activation_is_kept_when_its_claims_grew`.
- Contract: `keep_plan` keeps the plan's materialization pair. Mode: focused-test
  `test_remote_collection_with_keep_plan_keeps_the_materialization`.
- Contract: anchors-last order. Mode: focused-test — Task 4's crash test
  `test_remote_teardown_collection_resumes_after_a_crash_at_any_unlink` with siblings.

Steps:

1. Add the four tests:

   ```python
   @pytest.mark.anyio
   async def test_remote_system_activations_find_every_activation_by_content(
       tmp_path: Path,
   ) -> None:
       adapter, record, _request, _context, store = await _finalized_release_store(tmp_path)
       sibling = _sibling(record, "00000000-0000-4000-8000-0000000000aa")
       _publish_terminal(store, sibling)
       foreign = record.model_copy(
           update={
               "binding": record.binding.model_copy(
                   update={"system_id": "00000000-0000-4000-8000-0000000000bb"}
               )
           }
       )
       store.record_cleanup_quarantine(
           RemoteCleanupQuarantineV1(
               binding=foreign.binding,
               plan_identity=foreign.plan_identity,
               recovery_ref=OpaqueProviderRef(ref="remote-external-boot/" + "c" * 64),
               operation_identity="cleanup-foreign",
               attempt_id=uuid4(),
               journal_sequence=3,
               journal_digest="sha256:" + "d" * 64,
           )
       )

       found = store.system_activations(record.binding.system_id)

       assert found == sorted(
           [(record.binding, record.plan_identity), (sibling.binding, sibling.plan_identity)],
           key=lambda item: (item[0].run_id, item[0].activation_id, item[1]),
       )
       adapter.close()


   @pytest.mark.anyio
   async def test_remote_activation_claims_name_every_volume_and_none_once_finalized(
       tmp_path: Path,
   ) -> None:
       adapter, record, request, context, store = await _finalized_release_store(tmp_path)
       claims = store.activation_claims(record.binding, record.plan_identity)
       module = store.reopen_terminal(record.binding, record.plan_identity).response.recovery

       assert claims is not None
       assert {
           (module.pool.ref, module.source_volume.ref),
           (module.pool.ref, module.scratch_volume.ref),
       } <= set(claims.module_volumes)
       assert claims.operation_nonce is not None
       assert {item.ref for item in claims.boot_artifacts} >= {
           record.materialization.artifacts.kernel.ref
       }
       await adapter.finalize(request, context)
       assert store.activation_claims(record.binding, record.plan_identity) is None
       adapter.close()


   @pytest.mark.anyio
   async def test_remote_proven_activation_is_kept_when_its_claims_grew(tmp_path: Path) -> None:
       adapter, record, _request, _context, store = await _finalized_release_store(tmp_path)
       sibling = _sibling(record, "00000000-0000-4000-8000-0000000000aa")
       proven = RemoteFinalizedTeardownRefsV1(
           module_volumes=(), operation_nonce=None, boot_artifacts=()
       )
       _publish_terminal(store, sibling)
       before = _store_names(store)

       assert not store.collect_proven_activation(
           sibling.binding, sibling.plan_identity, proven, keep_plan=False
       )
       assert _store_names(store) == before
       adapter.close()


   @pytest.mark.anyio
   async def test_remote_collection_with_keep_plan_keeps_the_materialization(
       tmp_path: Path,
   ) -> None:
       adapter, record, _request, _context, store = await _finalized_release_store(tmp_path)
       stem = record.plan_identity.removeprefix("sha256:")
       identity = record.materialization.identity.removeprefix("sha256:")

       store.collect_torn_down_activation(record.binding, record.plan_identity, keep_plan=True)

       assert {f"{stem}.materialization-index", f"{identity}.materialization"} <= _store_names(store)
       adapter.close()
   ```

   Import `RemoteFinalizedTeardownRefsV1` from
   `kdive.providers.remote_libvirt.external_boot_authority` in the test module if it is not
   already imported; `uuid4` and `RemoteCleanupQuarantineV1` are.
2. Run the focused command; expect `AttributeError` for `system_activations`,
   `activation_claims` and `collect_proven_activation`, and `TypeError` for `keep_plan`.
3. Add module-level helpers after `_object_index_name`:

   ```python
   def _boot_artifacts(
       materialization: ExternalBootMaterialization,
   ) -> list[RemoteFinalizedBootArtifactV1]:
       artifacts = materialization.artifacts
       boot = [
           RemoteFinalizedBootArtifactV1(
               ref=artifacts.kernel.ref,
               kind="kernel",
               sha256=materialization.extracted_vmlinuz_sha256,
           )
       ]
       if artifacts.initrd is not None:
           boot.append(
               RemoteFinalizedBootArtifactV1(
                   ref=artifacts.initrd.ref,
                   kind="initrd",
                   sha256=materialization.verified_initrd_sha256,
               )
           )
       return boot


   def _claims_within(
       current: RemoteFinalizedTeardownRefsV1 | None, proven: RemoteFinalizedTeardownRefsV1 | None
   ) -> bool:
       """Whether an activation's records name nothing the proof did not (#3486).

       Records only shrink while collection runs, so a subset is still proven.
       """
       if proven is None or current is None:
           return current is None or proven is not None
       return (
           set(current.module_volumes) <= set(proven.module_volumes)
           and set(current.boot_artifacts) <= set(proven.boot_artifacts)
           and current.operation_nonce in (None, proven.operation_nonce)
       )
   ```

   `RemoteFinalizedBootArtifactV1` is frozen, so it is hashable. Note the first branch: proven
   `None` (finalized) admits only a still-finalized activation (`current is None`); a current
   `None` is always within. Use `_boot_artifacts` in `_teardown_refs` in place of its inline
   kernel/initrd block.
4. Extract the `.request` nonce scan from `_teardown_refs` into:

   ```python
   def _owned_operation_nonce(
       self, binding: ExternalBootActivationBinding, plan_identity: str
   ) -> str | None:
       operations: list[RemoteModuleOperationV1] = []

       def collect(data: bytes) -> bool:
           request = _PersistedRemoteModulePreparationV1.from_canonical_json(data).request
           if owned := _authority_owned(request.authority, binding, plan_identity):
               operations.append(request.operation)
           return owned

       self._scan_owned(".request", collect, strict=True)
       # The same comparison teardown_preparation applies, so finalize captures no nonce that
       # teardown would have refused.
       if any(operation != operations[0] for operation in operations[1:]):
           raise ProviderRecoveryRefusal(
               "remote finalize found conflicting preparation requests for one activation; "
               "resolve the duplicate .request records before finalizing"
           )
       return operations[0].operation_nonce if operations else None
   ```

   `_teardown_refs` calls it for `operation_nonce`.
5. Add `system_activations` beside `_scan_owned`:

   ```python
   def system_activations(self, system_id: str) -> list[tuple[ExternalBootActivationBinding, str]]:
       """Every activation whose anchor file content names this System (#3486).

       An unparseable file is left in place with ``_owns``'s warning: nothing proves whose it is.
       """
       found: dict[tuple[str, str, str], tuple[ExternalBootActivationBinding, str]] = {}

       def note(binding: ExternalBootActivationBinding, plan_identity: str) -> bool:
           if binding.system_id != system_id:
               return False
           found[(binding.run_id, binding.activation_id, plan_identity)] = (binding, plan_identity)
           return True

       def authority(
           value: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1,
       ) -> bool:
           return note(_authority_binding(value), value.plan_identity)

       readers: tuple[tuple[str, Callable[[bytes], bool]], ...] = (
           (
               ".lifecycle-request",
               lambda data: authority(
                   _PersistedRemoteModuleLifecycleV1.from_canonical_json(data).request.authority
               ),
           ),
           (
               ".request",
               lambda data: authority(
                   _PersistedRemoteModulePreparationV1.from_canonical_json(data).request.authority
               ),
           ),
           (
               ".preparation",
               lambda data: note(
                   (value := ExternalBootPreparationObservation.from_canonical_json(data)).binding,
                   value.plan_identity,
               ),
           ),
           (
               ".terminal",
               lambda data: authority(
                   RemoteModuleTerminalRecord.from_canonical_json(data).request.authority
               ),
           ),
           (
               ".recovery",
               lambda data: note(
                   (value := RemoteExternalBootRecoveryRecord.from_canonical_json(data)).binding,
                   value.plan_identity,
               ),
           ),
           (
               ".cleanup-quarantine",
               lambda data: note(
                   (value := RemoteCleanupQuarantineV1.from_canonical_json(data)).binding,
                   value.plan_identity,
               ),
           ),
           (
               ".cleanup-finalized",
               lambda data: note(
                   (value := _parse_finalized_marker(data)[0]).binding, value.plan_identity
               ),
           ),
       )
       for suffix, read in readers:
           self._scan_owned(suffix, read)
       return [found[key] for key in sorted(found)]
   ```

   If `ty` rejects a walrus inside a lambda, replace each such lambda with a nested `def`.
6. Add `activation_claims` after `finalized_teardown_refs`:

   ```python
   def activation_claims(
       self, binding: ExternalBootActivationBinding, plan_identity: str
   ) -> RemoteFinalizedTeardownRefsV1 | None:
       """Every pool volume the activation's records name; ``None`` once finalized (#3486)."""
       if self._read(self._finalized_name(binding, plan_identity)) is not None:
           return None
       volumes: set[tuple[str, str]] = set()
       materializations: list[ExternalBootMaterialization] = []
       try:
           module = self.reopen_terminal(binding, plan_identity).response.recovery
           volumes |= {
               (module.pool.ref, module.source_volume.ref),
               (module.pool.ref, module.scratch_volume.ref),
           }
       except FileNotFoundError:
           pass
       if (recovery := self.teardown_recovery(binding, plan_identity)) is not None:
           module = recovery.module_recovery
           volumes |= {
               (module.pool.ref, module.source_volume.ref),
               (module.pool.ref, module.scratch_volume.ref),
           }
           materializations.append(recovery.materialization)
       if (record := self._claimed_materialization(binding, plan_identity)) is not None:
           materializations.append(record.materialization)

       def preparation(data: bytes) -> bool:
           receipt = ExternalBootPreparationObservation.from_canonical_json(data)
           owned = receipt.binding == binding and receipt.plan_identity == plan_identity
           if owned and receipt.materialization is not None:
               materializations.append(receipt.materialization)
           return owned

       self._scan_owned(".preparation", preparation, strict=True)
       boot = {item for value in materializations for item in _boot_artifacts(value)}
       return RemoteFinalizedTeardownRefsV1(
           module_volumes=tuple(sorted(volumes)),
           operation_nonce=self._owned_operation_nonce(binding, plan_identity),
           boot_artifacts=tuple(sorted(boot, key=lambda item: (item.ref, item.kind))),
       )


   def _claimed_materialization(
       self, binding: ExternalBootActivationBinding, plan_identity: str
   ) -> RemoteExternalBootMaterializationRecord | None:
       """The plan's materialization; ``None`` also once collection removed it before its index."""
       index = self._read(f"{plan_identity.removeprefix('sha256:')}.materialization-index")
       if index is not None:
           identity = index.decode("ascii", errors="replace")
           if (
               len(identity) == 64
               and all(value in "0123456789abcdef" for value in identity)
               and self._read(f"{identity}.materialization") is None
           ):
               return None
       return self.teardown_materialization(binding, plan_identity)
   ```

   A malformed index still reaches `teardown_materialization`, which refuses it.
7. Replace `_activation_files` with a two-group method and keep `_activation_files` for finalize:

   ```python
   def _activation_files(
       self, binding: ExternalBootActivationBinding, plan_identity: str
   ) -> list[str]:
       """Every per-activation file but the recovery record; anchors after the rest (#3486)."""
       derived, anchors = self._activation_file_groups(binding, plan_identity, keep_plan=False)
       return derived + anchors


   def _activation_file_groups(
       self, binding: ExternalBootActivationBinding, plan_identity: str, *, keep_plan: bool
   ) -> tuple[list[str], list[str]]:
       """Files no content scan finds, then the anchors ``system_activations`` scans.

       Removing the first group first keeps an interrupted collection's activation findable.
       ``keep_plan`` keeps the plan's materialization pair, which every activation of the Run
       shares.
       """

       def owned(
           authority: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1,
       ) -> bool:
           return _authority_owned(authority, binding, plan_identity)

       derived: list[str] = []
       anchors: list[str] = []
       for suffix, dependents, parse in (
           (
               ".lifecycle-request",
               (".lifecycle-completion",),
               lambda data: owned(
                   _PersistedRemoteModuleLifecycleV1.from_canonical_json(data).request.authority
               ),
           ),
           (
               ".request",
               (".result", ".completion"),
               lambda data: owned(
                   _PersistedRemoteModulePreparationV1.from_canonical_json(data).request.authority
               ),
           ),
       ):
           for name in self._scan_owned(suffix, parse):
               stem = name.removesuffix(suffix)
               derived += [f"{stem}{dependent}" for dependent in dependents]
               anchors.append(name)
       anchors += self._scan_owned(
           ".preparation",
           lambda data: (
               (receipt := ExternalBootPreparationObservation.from_canonical_json(data)).binding
               == binding
               and receipt.plan_identity == plan_identity
           ),
       )
       key = self._terminal_key(
           binding.system_id, binding.activation_id, binding.run_id, plan_identity
       )
       derived += [f"{key}.{suffix}" for suffix in ("restored", "prior-power-owner", "prior-power")]
       anchors.append(f"{key}.terminal")
       if not keep_plan:
           derived += self._materialization_files(binding, plan_identity)
       return derived, anchors
   ```
8. Change `_recovery_files` to return the object indexes and the record files separately:

   ```python
   def _recovery_files(
       self, identity: str, binding: ExternalBootActivationBinding, plan_identity: str
   ) -> tuple[list[str], list[str], bool]:
       """Object indexes naming the record; the record and receipt unless adopted; ``managed``."""
   ```

   Body as today, but return `(indexes, [] if managed else [f"{identity}.recovery",
   self._quarantine_name(reference)], managed)`.
9. Replace the store's `collect_torn_down_activation` with the lock wrapper, the proven variant
   and the shared body:

   ```python
   def collect_torn_down_activation(
       self,
       binding: ExternalBootActivationBinding,
       plan_identity: str,
       *,
       keep_plan: bool = False,
   ) -> None:
       """Remove the activation the teardown proved; keep an adopted receipt and what finds it.

       The store lock makes the ``managed`` read and the unlinks atomic against an adoption
       (ADR-0787, #3472, #3486).
       """
       with self._exclusive():
           self._collect(binding, plan_identity, keep_plan=keep_plan)


   def collect_proven_activation(
       self,
       binding: ExternalBootActivationBinding,
       plan_identity: str,
       proven: RemoteFinalizedTeardownRefsV1 | None,
       *,
       keep_plan: bool,
   ) -> bool:
       """Collect another activation of the System while its records name only proven volumes.

       The proof ran outside the lock; ``False`` keeps every file because the records changed.
       """
       with self._exclusive():
           if not _claims_within(self.activation_claims(binding, plan_identity), proven):
               return False
           self._collect(binding, plan_identity, keep_plan=keep_plan)
           return True


   def _collect(
       self, binding: ExternalBootActivationBinding, plan_identity: str, *, keep_plan: bool
   ) -> None:
       """Unlink non-anchors, then anchors, then the recovery index and marker that name them."""
       index = f"{self._recovery_key(binding, plan_identity)}.recovery-index"
       marker = self._finalized_name(binding, plan_identity)
       derived, anchors = self._activation_file_groups(binding, plan_identity, keep_plan=keep_plan)
       records: list[str] = []
       adopted = False
       for identity in self._collected_identities(index, marker):
           indexes, files, managed = self._recovery_files(identity, binding, plan_identity)
           derived += indexes
           records += files
           adopted = adopted or managed
       names = [*derived, *anchors, *records, *([] if adopted else [index]), marker]
       for name in names:
           with suppress(FileNotFoundError):
               os.unlink(name, dir_fd=self._root_fd)
       os.fsync(self._root_fd)
   ```
10. Run the focused command; expect pass. Run the whole module; the #3472 test
    `test_remote_system_teardown_collects_a_never_finalized_activation` still passes (Task 4
    rewrites it). `just lint && just type`. Commit `fix(remote-libvirt): find and claim every
    activation of a System in the store (#3486)`.

## Task 3 — Pool-absence proof for an activation's named volumes

Files: modify `src/kdive/providers/remote_libvirt/external_boot_operations.py` and the
`RemoteSystemTeardownOperations` protocol in `external_boot_authority.py`; test in
`tests/providers/remote_libvirt/test_external_boot_authority.py`.

Interfaces produced: `ConcreteRemoteExternalBootOperations.activation_volumes_absent(binding:
ExternalBootActivationBinding, refs: RemoteFinalizedTeardownRefsV1) -> bool`, declared on
`RemoteSystemTeardownOperations`.

Verification:

- Contract: reads every module, nonce-rendered and boot name absent and returns false when one is
  present; deletes nothing. Mode: focused-test
  `test_remote_activation_volumes_absent_reads_every_named_volume`. Red: `AttributeError`.
  Green: `uv run python -m pytest tests/providers/remote_libvirt/test_external_boot_authority.py
  -q -k activation_volumes_absent`.

Steps:

1. Add the test:

   ```python
   def test_remote_activation_volumes_absent_reads_every_named_volume() -> None:
       binding = _record().binding
       nonce = _remote_preparation_request().operation.operation_nonce
       rendered = {
           render_module_volume_name(binding.system_id, binding.run_id, nonce, kind)
           for kind in ("source.ext4", "scratch.ext4")
       }
       refs = RemoteFinalizedTeardownRefsV1(
           module_volumes=(("modules", "kept-source"),),
           operation_nonce=nonce,
           boot_artifacts=(RemoteFinalizedBootArtifactV1(ref="kernel-a", kind="kernel", sha256=None),),
       )
       looked: list[tuple[str, str]] = []
       present: set[str] = set()

       class Pool:
           def __init__(self, name: str) -> None:
               self.pool = name

           def storageVolLookupByName(self, name: str) -> object:  # noqa: N802
               looked.append((self.pool, name))
               if name in present:
                   return object()
               error = libvirt.libvirtError("absent")
               error.err = (libvirt.VIR_ERR_NO_STORAGE_VOL,) + (0,) * 8
               raise error

       class Connection:
           def __enter__(self) -> Connection:
               return self

           def __exit__(self, *_exc: object) -> None:
               return None

           def storagePoolLookupByName(self, name: str) -> Pool:  # noqa: N802
               return Pool(name)

       operations = ConcreteRemoteExternalBootOperations(
           cast(Any, object()), cast(Any, Connection), "boot", lambda: 1.0, cast(Any, object())
       )

       assert operations.activation_volumes_absent(binding, refs)
       assert set(looked) == {("modules", "kept-source"), ("boot", "kernel-a")} | {
           ("boot", name) for name in rendered
       }
       present.add("kernel-a")
       assert not operations.activation_volumes_absent(binding, refs)
   ```

   Import `render_module_volume_name` from
   `kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names` and
   `RemoteFinalizedBootArtifactV1` from the authority module if absent.
2. Run it; expect `AttributeError: ... 'activation_volumes_absent'`.
3. Add after `unfinished_preparation_absent` in `ConcreteRemoteExternalBootOperations`:

   ```python
   def activation_volumes_absent(
       self, binding: ExternalBootActivationBinding, refs: RemoteFinalizedTeardownRefsV1
   ) -> bool:
       """Whether every pool volume an activation's records name reads absent (#3486).

       Reads only: a present volume keeps the activation's records, the only pointer to it.
       """
       volumes = set(refs.module_volumes)
       volumes |= {(self._pool_name, item.ref) for item in refs.boot_artifacts}
       if refs.operation_nonce is not None:
           volumes |= self._module_volume_names(
               refs.operation_nonce, binding.system_id, binding.run_id
           )
       with self._connection() as connection:
           return all(
               self._teardown_volume_absent(connection, pool, name) for pool, name in sorted(volumes)
           )
   ```

   Import `RemoteFinalizedTeardownRefsV1` from
   `kdive.providers.remote_libvirt.external_boot_authority` (the module already imports from
   there) and `ExternalBootActivationBinding` from `kdive.providers.ports.external_boot` if it is
   not already imported. Add to `RemoteSystemTeardownOperations`:

   ```python
   def activation_volumes_absent(
       self, binding: ExternalBootActivationBinding, refs: RemoteFinalizedTeardownRefsV1
   ) -> bool: ...
   ```
4. Run it; expect pass. `just lint && just type`. Commit `feat(remote-libvirt): prove an
   activation's named volumes absent from the pool (#3486)`.

## Task 4 — Coordinator collects every proven activation

Files: modify `RemoteExternalBootCoordinator.collect_torn_down_activation` in
`src/kdive/providers/remote_libvirt/external_boot_authority.py`; tests in
`tests/providers/remote_libvirt/test_external_boot_authority.py`.

Interfaces consumed: `system_activations`, `activation_claims`, `collect_torn_down_activation`,
`collect_proven_activation` (Task 2); `activation_volumes_absent` (Task 3); `_sibling` and
`_ProvingTeardownOperations` (Task 1).

Verification:

- Contract: a mixed System ends with exactly the `.teardown` record, the never-finalized sibling
  whose volume is present (all its files), and the adopted trio. Mode: focused-test
  `test_remote_system_teardown_collects_every_provable_activation`. Red: the finalized and
  absent siblings' files remain. Green: `uv run python -m pytest
  tests/providers/remote_libvirt/test_external_boot_authority.py -q -k "every_provable or
  crash_at_any_unlink or never_finalized_activation"`.
- Contract: a crash at any unlink followed by an observation ends in the same file set,
  siblings included. Mode: focused-test, the extended
  `test_remote_teardown_collection_resumes_after_a_crash_at_any_unlink`.
- Contract: a kept sibling sharing the named plan keeps the materialization pair. Mode:
  focused-test — asserted in `test_remote_system_teardown_collects_every_provable_activation`.

Steps:

1. Rewrite `test_remote_system_teardown_collects_a_never_finalized_activation` so the sibling it
   seeds is collected: pass `_ProvingTeardownOperations()` to `_system_teardown`,
   and replace the trailing `other_key ... in after` and `sum(...) == 3` assertions with
   `assert after == {_teardown_name(store)}`.
2. Add the mixed test:

   ```python
   @pytest.mark.anyio
   async def test_remote_system_teardown_collects_every_provable_activation(
       tmp_path: Path,
   ) -> None:
       """#3486: finalized and proven siblings go; an unproven one and an adopted trio stay."""
       adapter, record, request, context, store = await _finalized_release_store(tmp_path)
       finalized = _sibling(record, "00000000-0000-4000-8000-0000000000a1")
       absent = _sibling(record, "00000000-0000-4000-8000-0000000000a2")
       kept = _sibling(record, "00000000-0000-4000-8000-0000000000a3")
       for sibling in (finalized, absent, kept):
           _publish_terminal(store, sibling)
       store._publish(
           store._finalized_name(finalized.binding, finalized.plan_identity),
           RemoteCleanupQuarantineV1(
               binding=finalized.binding,
               plan_identity=finalized.plan_identity,
               recovery_ref=OpaqueProviderRef(ref="remote-external-boot/" + "e" * 64),
               operation_identity="cleanup-finalized",
               attempt_id=uuid4(),
               journal_sequence=5,
               journal_digest="sha256:" + "f" * 64,
           ).to_canonical_json(),
       )
       (observed,) = await adapter.cleanup_quarantine_inventory(request, _native_context(request))
       await adapter.adopt_recovery_object(
           observed.binding, authority_reference(request), observed.observed_digest
       )
       kept_names = _activation_names(store, kept)
       absent_request = store.reopen_terminal(absent.binding, absent.plan_identity).request
       operations = _ProvingTeardownOperations(keep=frozenset({kept.binding.activation_id}))

       assert await _system_teardown(store, operations)

       receipt = store.read_cleanup_quarantine(record.binding, record.plan_identity)
       assert receipt is not None and receipt.managed
       identity = external_boot_authority._recovery_identity(receipt.recovery_ref)
       index = f"{store._recovery_key(record.binding, record.plan_identity)}.recovery-index"
       assert _store_names(store) == {
           _teardown_name(store),
           f"{identity}.recovery",
           f"{identity}.cleanup-quarantine",
           index,
           *kept_names,
       }
       assert sorted(operations.proved) == [absent.binding.activation_id, kept.binding.activation_id]
       with pytest.raises(external_boot_authority.RemoteCleanupFinalizedError):
           store.stage(absent_request, 1.0)
       adapter.close()
   ```

   with helpers:

   ```python
   def _activation_names(
       store: RemoteModuleVolumePreparationStore, record: RemoteExternalBootRecoveryRecord
   ) -> set[str]:
       """The kept activation's own files plus the plan's shared materialization pair."""
       derived, anchors = store._activation_file_groups(
           record.binding, record.plan_identity, keep_plan=False
       )
       return {name for name in derived + anchors if store._read(name) is not None}
   ```

   `_activation_names` and `absent_request` are read before teardown, as written. The adopted
   activation is the named one (`_cleaned_release` builds its receipt); siblings reach the same
   `_collect`, so the adopted-trio rule is the same code for both. The kept sibling shares the
   named plan, so its expected names include the plan's materialization pair (kept by
   `keep_plan`).
3. Extend `test_remote_teardown_collection_resumes_after_a_crash_at_any_unlink`: after
   `_finalized_release_store(root)`, add a sibling
   `sibling = _sibling(record, "00000000-0000-4000-8000-0000000000aa")`,
   `_publish_terminal(store, sibling)` and `_seed_activation_residue(store, sibling)`; use
   `operations = _ProvingTeardownOperations()`; keep the final
   `_store_names(store) == {_teardown_name(store)}`; raise the floor to
   `crashes >= (10 if finalized else 20)`.
4. Run the focused command; expect the mixed test and the rewritten #3472 test red (siblings
   remain).
5. Replace the coordinator's `collect_torn_down_activation`:

   ```python
   def collect_torn_down_activation(self, record: RemoteSystemTeardownRecordV1) -> None:
       """Collect every activation of a completed teardown's System that is proven safe.

       The named activation's volumes were proven absent before ``complete``. Another activation
       is collected only when finalized or when every volume its records name reads absent;
       otherwise its records stay, since they are the only pointer to those volumes. A failure
       is logged, not raised: the System is already destroyed and the ``complete`` record keeps
       refusing late stages (ADR-0787, #3472, #3486).
       """
       if record.phase != "complete":
           return
       try:
           proven, kept_plans = self._prove_siblings(record)
       except Exception:  # noqa: BLE001 - the service's following observe collects again
           logger.exception("remote store collection after System teardown failed")
           return
       binding, plan_identity = record.intent.binding, record.intent.plan_identity
       collections: list[Callable[[], object]] = [
           lambda: self._store.collect_torn_down_activation(
               binding, plan_identity, keep_plan=plan_identity in kept_plans
           )
       ]
       collections += [
           lambda sibling=sibling, plan=plan, claims=claims: self._store.collect_proven_activation(
               sibling, plan, claims, keep_plan=plan in kept_plans
           )
           for sibling, plan, claims in proven
       ]
       for collect in collections:
           try:
               collect()
           except Exception:  # noqa: BLE001 - one activation's failure keeps the others going
               logger.exception("remote store collection after System teardown failed")


   def _prove_siblings(
       self, record: RemoteSystemTeardownRecordV1
   ) -> tuple[
       list[tuple[ExternalBootActivationBinding, str, RemoteFinalizedTeardownRefsV1 | None]],
       set[str],
   ]:
       """Siblings proven safe to collect, and the plans a kept sibling still uses."""
       named = (record.intent.binding, record.intent.plan_identity)
       operations = cast(RemoteSystemTeardownOperations, self._operations)
       proven: list[
           tuple[ExternalBootActivationBinding, str, RemoteFinalizedTeardownRefsV1 | None]
       ] = []
       kept_plans: set[str] = set()
       for binding, plan_identity in self._store.system_activations(named[0].system_id):
           if (binding, plan_identity) == named:
               continue
           try:
               claims = self._store.activation_claims(binding, plan_identity)
               absent = claims is None or operations.activation_volumes_absent(binding, claims)
           except Exception:  # noqa: BLE001 - an unprovable activation is kept, not collected
               logger.warning("remote store cannot prove an activation's volumes", exc_info=True)
               claims, absent = None, False
           if absent:
               proven.append((binding, plan_identity, claims))
               continue
           kept_plans.add(plan_identity)
           logger.warning(
               "remote store keeps System %s run %s activation %s: a volume it names may remain",
               binding.system_id,
               binding.run_id,
               binding.activation_id,
           )
       return proven, kept_plans
   ```

   If `ruff` flags the list-comprehension lambda default arguments, use a nested `def` per
   sibling instead; behavior is identical.
6. Run the focused command, then the whole module; expect pass. Then the controlled-fault arms,
   one at a time, each after a commit, each reverted with
   `git checkout -- src/kdive/providers/remote_libvirt/external_boot_authority.py`:
   - in `_prove_siblings`, set `absent = True` unconditionally → the mixed test fails (kept
     sibling collected);
   - in `_collect`, move `*anchors` before `*derived` → the crash test fails (orphans remain);
   - in `collect_proven_activation`, drop the `_claims_within` check → the claims-grew test fails;
   - in `cleanup_finalized`, replace `self.system_torn_down(binding.system_id)` with the #3472
     per-activation check `False` → the Task 1 sibling-stage test fails.
7. `just lint && just type && just test-changed`. Commit `fix(remote-libvirt): collect every
   proven activation of a torn-down System (#3486)`.

## Task 5 — Runbook: clear store files of torn-down Systems

Files: modify `docs/operating/runbooks/remote-libvirt-host-setup.md`.

Verification:

- Contract: the procedure's commands and paths match the code (store directory, unit name,
  volume prefixes, kept files). Mode: task-test-not-applicable — operator prose with no
  executable consumer; checked by review against `host.py` (`remote_module_store_dir`),
  `deploy/systemd/system/kdive-external-boot-authority.service`, and the prefixes in
  `remote_module_volume_names.py` and `boot_artifact_name.py`. `just ci` runs the doc link
  checks.

Steps:

1. Append this section at the end of the file:

   ```markdown
   ## Clear store files of torn-down Systems

   The external-boot authority keeps provider-host records under
   `/var/lib/kdive/provider-authority/remote-module-preparations/evidence`. A completed System
   teardown removes the records of every activation of that System it can prove safe
   ([ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md), #3486). Stores of Systems
   torn down before #3472, and activations whose volumes were still present, keep their files.
   To clear them once:

   1. Tear down or drain every System on the host, so no System of this host is active, then stop
      the authority: `sudo systemctl stop kdive-external-boot-authority.service`.
   2. List the storage pool and confirm that no `kdive-module-<system id>-…` or
      `kdive-boot-v1-…-<system id>-…` volume remains for a torn-down System. Those records are the
      only pointer to such a volume; remove or account for the volume before its records.
   3. Copy the directory aside, then remove every file in it except:
      - each `<key>.teardown` record (it refuses a late stage of that System);
      - each `<identity>.cleanup-quarantine` whose JSON has `"managed":true`, its
        `<identity>.recovery`, and the `.recovery-index` file whose content is `<identity>`.
   4. Start the authority again: `sudo systemctl start kdive-external-boot-authority.service`.

   The operator who later removes an adopted receipt removes all three of its files together.
   ```

   Use exactly the heading `## Clear store files of torn-down Systems` (no number), so its
   anchor is `clear-store-files-of-torn-down-systems`, which the ADR-0787 amendment links. The
   section is maintenance, not a setup step, so it sits outside the numbered flow.
2. `prek run --files docs/operating/runbooks/remote-libvirt-host-setup.md`. Commit `docs(remote-
   libvirt): add the torn-down store remedy to the host runbook (#3486)`.

## Deferrals

none.
