# Remote System teardown collects the activation's store files — implementation plan (#3472)

**Goal:** A completed remote System teardown removes the torn-down activation's store files,
keeps an adopted receipt and its recovery record, and still refuses a late stage.

**Architecture:** One store method collects the files under a new store-directory `flock`, which
remote adoption also takes. The per-System `.teardown` record, once `complete`, becomes a second
"finalized" fact for `cleanup_finalized`/`refuse_finalized`, and makes `system_teardown_state`
read no activation records. The teardown adapter collects whenever it returns `complete`.

**Tech stack:** Python 3, pytest with anyio, the repository `just` recipes.

Spec: [2026-11-02-teardown-store-gc-3472-design.md](../specs/2026-11-02-teardown-store-gc-3472-design.md)

Expected implementation size: 230–330 changed lines (L) — about 90 lines of store, coordinator
and adapter change and 140–240 lines across seven tests and one helper rewrite, from the file map
and tasks below.

## Global Constraints

- No schema, migration, protocol, or SQL change; no new ADR file (amend ADR-0787 only).
- Do not add finalize/teardown scan bounds (#3475), local-libvirt collection, a sweep of earlier
  stores, or teardown lane serialization.
- Doc wording: no "critical", "robust", "comprehensive", "elegant".
- Guardrails while iterating: `just lint`, `just type`, `just test-changed`; before push
  `git fetch origin main && just records`. Commit before any controlled-fault arm.

## File map

| File | Change | Owns after |
|---|---|---|
| `src/kdive/providers/remote_libvirt/external_boot_authority.py` | modify | store: `_exclusive`, `torn_down`, `cleanup_finalized` widening, `collect_torn_down_activation` (+ two private helpers), locked `adopt_cleanup_quarantine`; coordinator: `collect_torn_down_activation`, torn-down short-circuit in `system_teardown_state`; adapter: collection call in `execute_system_teardown` |
| `tests/providers/remote_libvirt/test_external_boot_authority.py` | modify | new tests; `_seed_system_teardown_intent` writes a valid record |
| `docs/adr/0787-abortable-finalize-proof-checkpoint.md` | modify | 2026-11-02 amendment (written with the design) |

No ownership move: the store already owns finalize's file removal; collection extends it.

## Task 1 — Torn-down fact, store lock, and collection

**Verification:**

- Contract: completed teardown of a never-finalized activation leaves only the `.teardown`
  record of it and keeps another activation's files. `Mode: focused-test` —
  `test_remote_system_teardown_collects_a_never_finalized_activation`. Red: the activation's
  files remain (`before - after` is empty). Green: command below.
- Contract: completed teardown of a finalized activation removes its marker and files.
  `Mode: focused-test` — `test_remote_system_teardown_collects_a_finalized_activation`. Red:
  marker remains.
- Contract: adopted receipt and its record kept, port observation unchanged. `Mode:
  focused-test` — extended `test_remote_system_teardown_keeps_an_adopted_receipt`. Red under a
  controlled fault replacing `receipt is None or not receipt.managed` with `True` in
  `_recovery_files`: the receipt is gone.
- Contract: late stage and lifecycle stage refused after teardown, nothing written. `Mode:
  focused-test` — `test_remote_stage_after_system_teardown_is_refused`. Red: `stage` succeeds.
- Contract: crash at any collection unlink, then retry, converges; stage refused and
  observation `complete` while half collected. `Mode: focused-test` —
  `test_remote_teardown_collection_resumes_after_a_crash_at_any_unlink`. Red before the
  `system_teardown_state` short-circuit: a retry raises "plan index is incomplete".
- Contract: adoption and collection wait for the store lock; adoption after collection fails as
  changed. `Mode: focused-test` — `test_remote_receipt_decisions_wait_for_the_store_lock`
  (`adopt`, `collect`) and `test_remote_adoption_after_teardown_collection_fails_as_changed`.
  Red: the worker finishes while the lock is held.

Green command for all:
`uv run pytest -q tests/providers/remote_libvirt/test_external_boot_authority.py -k "teardown or finalize or adopt or stage_after or store_lock"`
Expected: all selected pass.

**Interfaces (existing, confirmed in the repo):** store `_read`, `_replace`, `_root_fd`,
`_system_teardown_key(binding)`, `_terminal_key(system_id, activation_id, run_id, plan)`,
`_recovery_key(binding, plan)`, `_finalized_name(binding, plan)`, `_quarantine_name(ref)`,
`_activation_files(binding, plan)`, `_recovery_record(identity)`, `_receipt(ref)`,
module `_parse_finalized_marker(data)`, `_recovery_identity(ref)`, `_object_index_name(ref)`,
`RemoteSystemTeardownRecordV1.from_canonical_json`, `RemoteCleanupFinalizedError`; tests
`_finalized_release_store`, `_cleaned_release`, `_seed_activation_residue`, `_publish_terminal`,
`_store_names`, `_module_lifecycle_request`, `_remote_teardown_request`,
`_remote_teardown_context`, `_REMOTE_TEARDOWN_RESERVATION`, `_CompletingTeardownOperations`,
`_ReapingModuleHost`. New: `RemoteModuleVolumePreparationStore.torn_down(binding,
plan_identity) -> bool`, `.collect_torn_down_activation(binding, plan_identity) -> None`,
`RemoteExternalBootCoordinator.collect_torn_down_activation(binding, plan_identity) -> None`.

Steps:

1. Add `import fcntl`, `from collections.abc import Callable, Iterator` and
   `from contextlib import contextmanager, suppress` to the module imports.
2. In the store, after `close`, add:

   ```python
   @contextmanager
   def _exclusive(self) -> Iterator[None]:
       """Hold an exclusive lock on the store directory (ADR-0787, #3472).

       Each acquisition opens its own file description, so threads of one process exclude
       each other too; closing it releases the lock.
       """
       descriptor = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=self._root_fd)
       try:
           fcntl.flock(descriptor, fcntl.LOCK_EX)
           yield
       finally:
           os.close(descriptor)
   ```

3. Replace `cleanup_finalized` and the `refuse_finalized` error message, and add `torn_down`:

   ```python
   def torn_down(self, binding: ExternalBootActivationBinding, plan_identity: str) -> bool:
       """Whether this System's teardown completed for exactly this activation (#3472)."""
       data = self._read(f"{self._system_teardown_key(binding)}.teardown")
       if data is None:
           return False
       record = RemoteSystemTeardownRecordV1.from_canonical_json(data)
       return (
           record.phase == "complete"
           and record.intent.binding == binding
           and record.intent.plan_identity == plan_identity
       )


   def cleanup_finalized(self, binding: ExternalBootActivationBinding, plan_identity: str) -> bool:
       """Whether finalize or a completed System teardown removed, or is removing, its records."""
       return self._read(self._finalized_name(binding, plan_identity)) is not None or (
           self.torn_down(binding, plan_identity)
       )
   ```

   In `refuse_finalized` the message becomes
   `"remote activation records were removed by cleanup finalization or System teardown"`.
4. Wrap `adopt_cleanup_quarantine`'s `_replace` call in `with self._exclusive():`.
5. After `finalize_cleanup_quarantine`, add:

   ```python
   def collect_torn_down_activation(
       self, binding: ExternalBootActivationBinding, plan_identity: str
   ) -> None:
       """Remove a torn-down activation's files; keep an adopted receipt and its record.

       Each anchor goes after what it names, so a retry after a crash finds what remains: the
       recovery index and the marker after the record and receipt, the terminal record last
       because a ref-less marker is readable only beside it (ADR-0787, #3472).
       """
       terminal_key = self._terminal_key(
           binding.system_id, binding.activation_id, binding.run_id, plan_identity
       )
       terminal = f"{terminal_key}.terminal"
       index = f"{self._recovery_key(binding, plan_identity)}.recovery-index"
       marker = self._finalized_name(binding, plan_identity)
       with self._exclusive():
           names = [
               name for name in self._activation_files(binding, plan_identity) if name != terminal
           ]
           identities = self._collected_identities(index, marker)
           for identity in identities:
               names += self._recovery_files(identity, binding, plan_identity)
           if self._read(index) in {identity.encode("ascii") for identity in identities}:
               names.append(index)
           names += [marker, terminal]
           for name in names:
               with suppress(FileNotFoundError):
                   os.unlink(name, dir_fd=self._root_fd)
           os.fsync(self._root_fd)


   def _collected_identities(self, index: str, marker: str) -> list[str]:
       """Recovery identities the index or the marker names, without duplicates."""
       references: list[OpaqueProviderRef] = []
       if (named := self._read(index)) is not None:
           identity = named.decode("ascii", errors="replace")
           references.append(OpaqueProviderRef(ref=f"remote-external-boot/{identity}"))
       if (data := self._read(marker)) is not None:
           references.append(_parse_finalized_marker(data)[0].recovery_ref)
       return list(dict.fromkeys(_recovery_identity(reference) for reference in references))


   def _recovery_files(
       self, identity: str, binding: ExternalBootActivationBinding, plan_identity: str
   ) -> list[str]:
       """Object indexes naming the record, then the record and receipt unless adopted."""
       reference = OpaqueProviderRef(ref=f"remote-external-boot/{identity}")
       record = self._recovery_record(identity)
       receipt = self._receipt(reference)
       for owner in (record, receipt):
           if owner is not None and (owner.binding != binding or owner.plan_identity != plan_identity):
               raise ProviderRecoveryRefusal(
                   "remote recovery record differs from the torn-down activation"
               )
       owned = identity.encode("ascii")
       names = (
           []
           if record is None
           else [
               name
               for object_ref in record.recovery_objects
               if self._read(name := _object_index_name(object_ref)) == owned
           ]
       )
       if receipt is None or not receipt.managed:
           names += [f"{identity}.recovery", self._quarantine_name(reference)]
       return names
   ```

6. In `RemoteExternalBootCoordinator`, add after `checkpoint_system_teardown`:

   ```python
   def collect_torn_down_activation(
       self, binding: ExternalBootActivationBinding, plan_identity: str
   ) -> None:
       self._store.collect_torn_down_activation(binding, plan_identity)
   ```

   and at the top of `system_teardown_state`:

   ```python
   if self._store.torn_down(binding, plan_identity):
       # A complete teardown proved its volumes absent; a half-collected store's records are
       # neither read nor trusted (ADR-0787, #3472).
       return RemoteSystemTeardownState(
           binding=binding,
           plan_identity=plan_identity,
           preparation=None,
           terminal=None,
           materialization=None,
           recovery=None,
       )
   ```

7. In `RemoteExternalBootAuthorityAdapter.execute_system_teardown`, before the final
   `return self._teardown_facts(record.intent, record, inspection)`:

   ```python
   if record.phase == "complete":
       await self._executor.run(
           lambda: self._coordinator.collect_torn_down_activation(
               record.intent.binding, record.intent.plan_identity
           )
       )
   ```

8. In the test module, import `fcntl` and `RemoteSystemTeardownIntentV1`, and replace
   `_seed_system_teardown_intent` (its callers drop the `"intent"` argument):

   ```python
   def _seed_system_teardown_intent(store: RemoteModuleVolumePreparationStore) -> str:
       """A real in-progress teardown record, so the torn-down check can parse it."""
       request = _remote_teardown_request()
       anchor = RemoteExternalBootAuthorityAdapter._teardown_anchor(
           request, _remote_teardown_context(request)
       )
       store.begin_system_teardown(
           RemoteSystemTeardownIntentV1.model_validate(
               anchor.model_dump(exclude={"schema_"}) | {"reservation": _REMOTE_TEARDOWN_RESERVATION}
           )
       )
       return f"{store._system_teardown_key(_record().binding)}.teardown"
   ```

9. Add the helpers and tests after `test_remote_system_teardown_keeps_an_adopted_receipt`:

   ```python
   def _collecting_teardown_adapter(
       store: RemoteModuleVolumePreparationStore,
       operations: _CompletingTeardownOperations,
   ) -> RemoteExternalBootAuthorityAdapter:
       return RemoteExternalBootAuthorityAdapter(
           RemoteExternalBootCoordinator(cast(Any, operations), store, lambda: 300.0),
           RemoteModulePreparationExecutor(),
           remote_module_host=cast(Any, _ReapingModuleHost()),
           teardown_clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
       )


   async def _system_teardown(
       store: RemoteModuleVolumePreparationStore,
       operations: _CompletingTeardownOperations | None = None,
   ) -> bool:
       adapter = _collecting_teardown_adapter(store, operations or _CompletingTeardownOperations())
       request = _remote_teardown_request()
       try:
           result = await adapter.execute_system_teardown(
               request, _remote_teardown_context(request), _REMOTE_TEARDOWN_RESERVATION
           )
       finally:
           adapter.close()
       return result.complete


   def _teardown_name(store: RemoteModuleVolumePreparationStore) -> str:
       return f"{store._system_teardown_key(_record().binding)}.teardown"


   @pytest.mark.anyio
   async def test_remote_system_teardown_collects_a_never_finalized_activation(
       tmp_path: Path,
   ) -> None:
       """#3472: teardown removes every file of an activation finalize never ran for."""
       adapter, record, _request, _context, store = await _finalized_release_store(tmp_path)
       other = record.model_copy(
           update={
               "binding": record.binding.model_copy(
                   update={"activation_id": "00000000-0000-4000-8000-0000000000aa"}
               )
           }
       )
       _publish_terminal(store, other)
       _seed_activation_residue(store, other)
       before = _store_names(store)

       assert await _system_teardown(store)

       after = _store_names(store)
       assert after - before == {_teardown_name(store)}
       assert {name.rsplit(".", 1)[1] for name in before - after} == {
           "terminal",
           "restored",
           "request",
           "result",
           "completion",
           "lifecycle-request",
           "lifecycle-completion",
           "materialization",
           "materialization-index",
           "preparation",
           "prior-power",
           "prior-power-owner",
           "recovery",
           "recovery-index",
           "object-index",
           "cleanup-quarantine",
       }
       other_key = store._terminal_key(
           other.binding.system_id,
           other.binding.activation_id,
           other.binding.run_id,
           other.plan_identity,
       )
       assert f"{other_key}.terminal" in after
       assert sum(name.endswith((".request", ".result", ".completion")) for name in after) == 3
       adapter.close()


   @pytest.mark.anyio
   async def test_remote_system_teardown_collects_a_finalized_activation(
       tmp_path: Path,
   ) -> None:
       adapter, record, request, context, store = await _finalized_release_store(tmp_path)
       await adapter.finalize(request, context)
       assert store._finalized_name(record.binding, record.plan_identity) in _store_names(store)

       assert await _system_teardown(store)

       assert _store_names(store) == {_teardown_name(store)}
       adapter.close()


   @pytest.mark.anyio
   async def test_remote_stage_after_system_teardown_is_refused(tmp_path: Path) -> None:
       adapter, record, _request, _context, store = await _finalized_release_store(tmp_path)
       terminal = store.reopen_terminal(record.binding, record.plan_identity)
       lifecycle = _module_lifecycle_request(terminal.request, action="restore")
       assert await _system_teardown(store)
       names = _store_names(store)
       finalized_error = external_boot_authority.RemoteCleanupFinalizedError

       with pytest.raises(finalized_error, match="System teardown"):
           store.stage(terminal.request, 1.0)
       with pytest.raises(finalized_error):
           store.stage_lifecycle(lifecycle, 1.0)
       assert _store_names(store) == names
       adapter.close()


   @pytest.mark.anyio
   @pytest.mark.parametrize("finalized", [False, True])
   async def test_remote_teardown_collection_resumes_after_a_crash_at_any_unlink(
       tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finalized: bool
   ) -> None:
       real_unlink = os.unlink
       crashes = 0
       for crash_at in range(1, 100):
           root = tmp_path / f"crash-{crash_at}"
           root.mkdir()
           adapter, record, request, context, store = await _finalized_release_store(root)
           terminal_request = _record_terminal_request(store, record)
           if finalized:
               await adapter.finalize(request, context)
           operations = _CompletingTeardownOperations()
           seen = 0

           def failing(path: str, *args: Any, _at: int = crash_at, **kwargs: Any) -> None:
               nonlocal seen
               if not path.startswith("."):
                   seen += 1
                   if seen == _at:
                       raise OSError("injected crash")
               real_unlink(path, *args, **kwargs)

           monkeypatch.setattr(external_boot_authority.os, "unlink", failing)
           try:
               await _system_teardown(store, operations)
               crashed = False
           except OSError:
               crashed = True
           finally:
               monkeypatch.setattr(external_boot_authority.os, "unlink", real_unlink)
           if crashed:
               crashes += 1
               with pytest.raises(external_boot_authority.RemoteCleanupFinalizedError):
                   store.stage(terminal_request, 1.0)
               observer = _collecting_teardown_adapter(store, operations)
               teardown = _remote_teardown_request()
               observed = await observer.observe_system_teardown(
                   teardown, _remote_teardown_context(teardown)
               )
               observer.close()
               assert observed.complete
               assert await _system_teardown(store, operations)
           assert _store_names(store) == {_teardown_name(store)}
           adapter.close()
           if not crashed:
               break
       assert crashes >= 10
   ```

   `_record_terminal_request` is defined beside it, read before teardown removes the
   terminal record:

   ```python
   def _record_terminal_request(
       store: RemoteModuleVolumePreparationStore, record: RemoteExternalBootRecoveryRecord
   ) -> RemoteModuleVolumePreparationRequestV1:
       return store.reopen_terminal(record.binding, record.plan_identity).request
   ```

   The `finalized` case runs finalize first, so both the never-finalized and the finalized
   collection orders are crashed at every unlink until a run no longer crashes. Expected: at
   least ten crashes per case, every iteration converging on the `.teardown` record alone.

   ```python
   @pytest.mark.anyio
   @pytest.mark.parametrize("operation", ["adopt", "collect"])
   async def test_remote_receipt_decisions_wait_for_the_store_lock(
       tmp_path: Path, operation: str
   ) -> None:
       """#3472: adoption and collection serialize on one lock over the store directory."""
       adapter, record, _request, _context = await _cleaned_release(tmp_path)
       store = adapter._coordinator._store
       receipt = store.read_cleanup_quarantine(record.binding, record.plan_identity)
       assert receipt is not None
       name = store._quarantine_name(receipt.recovery_ref)
       before = store._read(name)

       def run() -> None:
           if operation == "adopt":
               store.adopt_cleanup_quarantine(receipt)
           else:
               store.collect_torn_down_activation(record.binding, record.plan_identity)

       held = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=store._root_fd)
       try:
           fcntl.flock(held, fcntl.LOCK_EX)
           worker = threading.Thread(target=run)
           worker.start()
           worker.join(timeout=0.5)
           assert worker.is_alive()
           assert store._read(name) == before
       finally:
           os.close(held)
       worker.join(timeout=10)
       assert not worker.is_alive()
       adapter.close()


   @pytest.mark.anyio
   async def test_remote_adoption_after_teardown_collection_fails_as_changed(
       tmp_path: Path,
   ) -> None:
       adapter, record, _request, _context = await _cleaned_release(tmp_path)
       store = adapter._coordinator._store
       receipt = store.read_cleanup_quarantine(record.binding, record.plan_identity)
       assert receipt is not None
       assert await _system_teardown(store)

       with pytest.raises(ProviderRecoveryRefusal, match="changed"):
           store.adopt_cleanup_quarantine(receipt)
       assert _store_names(store) == {_teardown_name(store)}
       adapter.close()
   ```

10. Extend `test_remote_system_teardown_keeps_an_adopted_receipt` after its last assertion:

    ```python
    identity = external_boot_authority._recovery_identity(receipt.recovery_ref)
    assert _store_names(store) == {
        _teardown_name(store),
        f"{identity}.recovery",
        f"{identity}.cleanup-quarantine",
    }
    ```

11. Run the green command; expect every selected test to pass. Run the full module
    `uv run pytest -q tests/providers/remote_libvirt/test_external_boot_authority.py`; expect it
    to pass. Commit (`fix(remote-libvirt): collect a torn-down activation's store files (#3472)`).
12. Controlled faults, each after the commit and reverted with
    `git checkout -- src/kdive/providers/remote_libvirt/external_boot_authority.py`:
    (a) `if True:` in place of `if receipt is None or not receipt.managed:` — the adopted test
    fails; (b) drop the `with self._exclusive():` in `adopt_cleanup_quarantine` — the `adopt`
    lock case fails; (c) return `False` from `torn_down` — the stage test fails.

**Acceptance:** all Task 1 contracts green; `just lint`, `just type`, `just test-changed` exit 0.
**Rollback:** revert the commit; no persisted format changes.
