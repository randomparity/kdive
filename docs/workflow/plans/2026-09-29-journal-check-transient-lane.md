# Periodic journal check retries a torn or vanished lane — plan

Goal: implement `docs/workflow/specs/2026-09-29-journal-check-transient-lane-design.md` (#2933).
Architecture: one private `HostReadinessError` subclass marks a vanished lane entry in
`_local_lanes`; `validate_current`'s retry predicate adds `invalid-lane` and that subclass.
Tech stack: Python 3.14, asyncio, pytest.

Expected implementation size: 170–230 changed lines (M) — about 20 lines in `host.py`, about
170 lines of tests (two race tests with thread/event-loop handshakes, two persistent-case tests),
and the ADR-0584 amendment.

## Global Constraints

- No new ADR number: ADR-0584 gains a dated amendment section. No migration.
- Each cause keeps its `journal: <reason>` message, `component`/`reason` attributes, error
  category, and details; a retried failure reports what the quiesced pass observes.
- Do not edit `service.py`, `journal.py`, the startup check, or `check_authority_host_once`.
- Guardrails: `just lint`, `just type`, `just records`, focused
  `just test-verbose tests/providers/external_boot_authority/test_host.py`; the pre-push hook
  runs `just ci`.

## Task 1 — the periodic retry covers a torn or vanished lane

Files: modify `src/kdive/providers/external_boot_authority/host.py`,
`tests/providers/external_boot_authority/test_host.py`,
`docs/adr/0584-provider-host-authority-fences-external-boot-mutations.md` (amendment, already
written with the spec).
Interfaces: consumes the existing `host.JournalInventoryValidator(anchor_quiescence=...)`,
`validate_current(config)`, `host._database_heads`, `host._restore_journal_inventory(config,
heads, cache, *, deadline)`, `ExternalBootAuthorityService.quiesce_anchors`, and the test helpers
`_config`, `_chain`, `_write_lane`, `_head_of`, `_anchoring_service`, `_patch_trusted_heads`,
`_CountingQuiescence`, `_mutation` (all present in `test_host.py` / `service_support.py` at
c2e92102b). Adds private `host._LaneVanished` and `host._may_be_in_flight_anchor(error) -> bool`,
used only by `validate_current` (host.py:619). `_is_head_divergence` keeps both its body and its
startup call site (`run_authority_host`, host.py:1798), which this task must not touch.

Verification:

- Success 1, `Mode: focused-test`: `test_periodic_check_retries_an_anchor_torn_mid_append`.
  Red on c2e92102b: `HostReadinessError: journal: invalid-lane`.
- Success 2, `Mode: focused-test`: `test_periodic_check_retries_a_lane_retracted_mid_listing`.
  Red on c2e92102b: `HostReadinessError: journal: unsafe-tree`.
- Success 3–5, `Mode: focused-test`: `test_periodic_check_refuses_a_persistent_lane_fault`
  (torn at rest, mode 0644, unarmed vanished). Red on c2e92102b: the torn case reads heads once,
  not twice.
- Startup unchanged, `Mode: focused-test`: the existing
  `test_periodic_check_waits_out_an_anchor_between_append_and_advance` stays green.
- ADR-0584 amendment, `Mode: task-test-not-applicable`: prose record; no executable consumer
  reads its text beyond `just records`' shape check.

Steps:

1. Add the three tests below the #2899 tests at the end of `test_host.py`. Import
   `AuthorityServiceError` from `kdive.providers.external_boot_authority.service` beside the
   existing `AuthenticatedPeer` import. `host.os` is the `os` module, so each patch filters to
   the one call it targets (thread and argument type) and passes every other call through.

   ```python
   def test_periodic_check_retries_an_anchor_torn_mid_append(
       monkeypatch: pytest.MonkeyPatch, tmp_path: Path
   ) -> None:
       """#2933: a load that reads an anchor's half-written record retries under quiescence."""
       config = replace(_config(tmp_path), authority_instance="host-a")
       half_written = threading.Event()
       first_load_done = threading.Event()
       first_load: list[str] = []
       write = os.write
       restore = host._restore_journal_inventory  # noqa: SLF001

       def tearing_write(descriptor: int, data: bytes) -> int:
           if (
               threading.current_thread() is not threading.main_thread()
               or half_written.is_set()
               or bytes(data[:1]) != b"{"
           ):
               return write(descriptor, data)
           written = write(descriptor, data[: len(data) // 2])
           half_written.set()
           assert first_load_done.wait(5)
           return written

       def first_load_after_tear(*args: Any, **kwargs: Any) -> None:
           if first_load_done.is_set():
               return restore(*args, **kwargs)
           assert half_written.wait(5)
           try:
               return restore(*args, **kwargs)
           except HostReadinessError as error:
               first_load.append(str(error))
               raise
           finally:
               first_load_done.set()

       async def scenario() -> None:
           service, repository, peer, request = _anchoring_service(config)
           _patch_trusted_heads(monkeypatch, repository)
           await service.acknowledge_takeover(peer, request)
           repository.current = True
           monkeypatch.setattr(host.os, "write", tearing_write)
           monkeypatch.setattr(host, "_restore_journal_inventory", first_load_after_tear)
           validator = host.JournalInventoryValidator(anchor_quiescence=service.quiesce_anchors)
           # FIFO task order: the check submits its load before the mutation's append blocks.
           check = asyncio.create_task(validator.validate_current(config))
           mutation = asyncio.create_task(service.execute_mutation(peer, _mutation(request)))
           async with asyncio.timeout(10):
               await check
               assert (await mutation).category == "target"

       asyncio.run(scenario())
       assert first_load == ["journal: invalid-lane"]


   def test_periodic_check_retries_a_lane_retracted_mid_listing(
       monkeypatch: pytest.MonkeyPatch, tmp_path: Path
   ) -> None:
       """#2933: a refused first record's lane unlinked between listing and stat retries."""
       config = replace(_config(tmp_path), authority_instance="host-a")
       listed = threading.Event()
       retracted = threading.Event()
       listdir = os.listdir

       def pausing_listdir(path: Any) -> list[str]:
           names = listdir(path)
           if (
               isinstance(path, int)
               and threading.current_thread() is not threading.main_thread()
               and not listed.is_set()
           ):
               listed.set()
               assert retracted.wait(5)
           return names

       async def scenario() -> None:
           service, repository, peer, request = _anchoring_service(config)
           _patch_trusted_heads(monkeypatch, repository)
           repository.advance_status = "superseded"
           repository.pause_phase = JournalPhase.WATERMARK_INSTALLED
           repository.phase_release.clear()
           takeover = asyncio.create_task(service.acknowledge_takeover(peer, request))
           await repository.phase_entered.wait()
           lane = config.journal_dir / f"{request.system_id}.jsonl"
           assert lane.exists()
           monkeypatch.setattr(host.os, "listdir", pausing_listdir)
           validator = host.JournalInventoryValidator(anchor_quiescence=service.quiesce_anchors)
           check = asyncio.create_task(validator.validate_current(config))
           async with asyncio.timeout(10):
               assert await asyncio.to_thread(listed.wait, 5)
               repository.phase_release.set()
               with pytest.raises(AuthorityServiceError, match="superseded"):
                   await takeover
               assert not lane.exists()
               retracted.set()
               await check

       asyncio.run(scenario())


   @pytest.mark.parametrize(
       ("case", "armed", "reason", "reads", "entered"),
       [
           ("torn", True, "invalid-lane", 2, 1),
           ("mode", True, "unsafe-tree", 1, 0),
           ("vanished", False, "unsafe-tree", 1, 0),
       ],
   )
   def test_periodic_check_refuses_a_persistent_lane_fault(
       monkeypatch: pytest.MonkeyPatch,
       tmp_path: Path,
       case: str,
       armed: bool,
       reason: str,
       reads: int,
       entered: int,
   ) -> None:
       config = _config(tmp_path)
       records = _chain(config, uuid4(), 2)
       lane = _write_lane(config, records)
       if case == "torn":
           lane.write_bytes(lane.read_bytes()[:-1])
       elif case == "mode":
           lane.chmod(0o644)
       else:
           listdir = os.listdir
           absent = f"{uuid4()}.jsonl"

           def listing(path: Any) -> list[str]:
               names = listdir(path)
               return [*names, absent] if isinstance(path, int) else names

           monkeypatch.setattr(host.os, "listdir", listing)
       heads_read = 0

       async def heads(_config: AuthorityHostConfig) -> tuple[JournalHead, ...]:
           nonlocal heads_read
           heads_read += 1
           return (_head_of(records[-1]),)

       monkeypatch.setattr(host, "_database_heads", heads)
       quiescence = _CountingQuiescence()
       validator = host.JournalInventoryValidator(anchor_quiescence=quiescence if armed else None)
       with pytest.raises(HostReadinessError, match=f"journal: {reason}"):
           asyncio.run(validator.validate_current(config))
       assert (heads_read, quiescence.entered) == (reads, entered)
   ```

   Run
   `just test-verbose tests/providers/external_boot_authority/test_host.py -k "torn_mid_append or retracted_mid_listing or persistent_lane_fault"`;
   expect the red failures named above.
2. Tests and implementation commit together after step 5, so the branch never carries a red
   commit.
3. In `host.py`, after `HostReadinessError`, add:

   ```python
   class _LaneVanished(HostReadinessError):
       """A listed lane that no longer exists; a retraction racing the listing produces it."""

       def __init__(self) -> None:
           super().__init__("journal", "unsafe-tree")
   ```

   In `_local_lanes`, split the per-name stat handler:

   ```python
           try:
               status = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
           except FileNotFoundError:
               raise _LaneVanished from None
           except OSError:
               raise HostReadinessError("journal", "unsafe-tree") from None
   ```

   Below `_is_head_divergence` (left unchanged), add:

   ```python
   def _may_be_in_flight_anchor(error: HostReadinessError) -> bool:
       """What a lane mid-append, mid-advance, or mid-retraction shows (ADR-0584 amendments)."""
       return (
           _is_head_divergence(error)
           or isinstance(error, _LaneVanished)
           or (error.component == "journal" and error.reason == "invalid-lane")
       )
   ```

   and use it in `validate_current` only, whose comment becomes "An anchor between its append
   and the end of its advance or retraction looks exactly like this."
4. Run the focused command from step 1; expect all pass. Run the whole file:
   `just test-verbose tests/providers/external_boot_authority/test_host.py`; expect all pass.
5. Run `just lint`, `just type`, `just records`; expect exit 0. Commit
   `fix(authority): retry a torn or vanished lane in the periodic check`.
6. Controlled faults (after the commit): drop the `invalid-lane` clause → the mid-append and
   torn-at-rest cases fail; replace `except FileNotFoundError: raise _LaneVanished` with the plain
   error → the mid-listing race test fails; retry every `unsafe-tree` → the mode-0644 case
   fails. Restore with `git checkout -- src/kdive/providers/external_boot_authority/host.py`
   after each.

Rollback: revert the commit; no persisted state changes.
