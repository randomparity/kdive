# Release scan bound (#3475) implementation plan

Goal: remote release finalize refuses past 4096 counted store names with a remedy that works,
and System teardown is never refused by store size.

Architecture: one count in `RemoteModuleVolumePreparationStore._finalize_cleanup_quarantine`
under the store lock, before any write; the `teardown_preparation` bound is deleted; one refusal
literal replaces another in `ProviderRecoveryRefusalReason`. Spec:
`docs/workflow/specs/2026-11-03-release-scan-bound-3475-design.md`.

Tech stack: Python 3, pytest (anyio), pyright, ruff; recipes in `justfile`.

Expected implementation size: 150–220 changed lines (M) — two source files (~25 lines), three
tests (~90 lines), the ADR-0787 amendment (~35 lines) and one runbook paragraph (~6 lines).

## Global Constraints

- Branch `fix/release-scan-bound-3475` off `main`; worktree
  `/home/dave/src/kdive-worktrees/fix/release-scan-bound-3475`.
- No migration, no new ADR: ADR-0787 gets an append-only amendment; earlier text is untouched.
- Refusal text is one fixed literal (no interpolation, at most 512 characters, ADR-0707) and is
  a member of `ProviderRecoveryRefusalReason`.
- Doc wording: no "critical", "robust", "comprehensive", "elegant"; say "Milestone".
- Guardrails while iterating: `just lint`, `just type`, `just test-changed`; before push:
  `git fetch origin main && just records`. Commit before every controlled-fault arm.

## File map

| File | Change | Criterion |
|---|---|---|
| `src/kdive/providers/remote_libvirt/external_boot_authority.py` | add `_FINALIZE_SCAN_BOUND`, `_FINALIZE_COUNTED_SUFFIXES`, the count in `_finalize_cleanup_quarantine`; delete the `teardown_preparation` bound; update `_scan_owned` docstring | 1, 2, 4 |
| `src/kdive/providers/external_boot_authority/teardown.py` | swap the retired literal for the new one | 3 |
| `tests/providers/remote_libvirt/test_external_boot_authority.py` | `_pad_requests`, `_counted_names`, three tests; marker test docstring | 6 |
| `docs/adr/0787-abortable-finalize-proof-checkpoint.md` | append amendment | 5 |
| `docs/operating/runbooks/remote-libvirt-host-setup.md` | name the release refusal as a trigger | 3 |

## Task 1 — Bound finalize, unbound teardown

Verification:

- Contract: finalize refuses past 4096 counted names and writes nothing. Mode: focused-test —
  `test_remote_finalize_refuses_past_the_scan_bound_and_names_the_remedy`; red before the count
  exists (finalize succeeds, `pytest.raises` fails). Green:
  `uv run pytest -q tests/providers/remote_libvirt/test_external_boot_authority.py -k "scan_bound or over_the_scan_bound or accumulated_finalized_markers"`.
- Contract: exactly 4096 finalizes; freed names let the same release finalize. Mode:
  focused-test — `test_remote_finalize_at_the_scan_bound_and_after_collection_frees_names`;
  red under the `>=` fault arm; same green command.
- Contract: teardown completes and collects over the bound. Mode: focused-test —
  `test_remote_system_teardown_collects_over_the_scan_bound`; red before the bound is deleted
  (`teardown_preparation` refuses, teardown not complete). Same green command.
- Contract: refusal literal is in the allowlist set. Mode: focused-test — existing
  `test_remote_refusal_reasons_are_never_raised_as_plain_value_errors` plus `just type`
  (pyright rejects a literal outside `ProviderRecoveryRefusalReason`).

Interfaces consumed (exist on main f945bda8e): `_finalized_release_store(tmp_path)` → `(adapter,
record, request, context, store)`; `_store_names(store) -> set[str]`;
`_system_teardown(store, operations) -> bool`; `_ProvingTeardownOperations()`;
`_teardown_name(store) -> str`; `store.reopen_terminal(binding, plan_identity)`;
`store.stage(request, local_deadline)`; `store.cleanup_finalized(binding, plan_identity)`;
`store.collect_torn_down_activation(binding, plan_identity, *, keep_plan=False)`;
`store._finalized_name(binding, plan_identity)`.

Steps:

1. Add the helpers and tests after
   `test_remote_finalize_and_teardown_scans_ignore_accumulated_finalized_markers`. The fixture
   store already holds 3 counted names (one of each suffix).

```python
_REMEDY = (
    "remote preparation store holds more than 4096 request, lifecycle-request and preparation "
    "records, so finalize is refused; tear down Systems on this host so teardown collects their "
    "records, or, once every System on this host is torn down, follow the runbook procedure "
    "Clear store files of torn-down Systems, then retry"
)
_PADDING_ACTIVATION = UUID("00000000-0000-4000-8000-0000000000c1")


def _counted_names(store: RemoteModuleVolumePreparationStore) -> int:
    return sum(
        1
        for name in _store_names(store)
        for suffix in (".request", ".lifecycle-request", ".preparation")
        if len(name) == 64 + len(suffix) and name.endswith(suffix)
    )


def _pad_requests(
    store: RemoteModuleVolumePreparationStore,
    record: RemoteExternalBootRecoveryRecord,
    activation_id: UUID,
    total: int,
) -> None:
    """Stage same-operation ``.request`` copies for ``activation_id`` until ``total`` count."""
    terminal = store.reopen_terminal(record.binding, record.plan_identity)
    for _ in range(total - _counted_names(store)):
        authority = terminal.request.authority.model_copy(
            update={"activation_id": activation_id, "attempt_id": uuid4()}
        )
        store.stage(terminal.request.model_copy(update={"authority": authority}), 999.0)


@pytest.mark.anyio
async def test_remote_finalize_refuses_past_the_scan_bound_and_names_the_remedy(
    tmp_path: Path,
) -> None:
    """#3475: release finalize refuses past 4096 counted names and writes nothing."""
    adapter, record, request, context, store = await _finalized_release_store(tmp_path)
    _pad_requests(store, record, _PADDING_ACTIVATION, 4097)
    before = _store_names(store)

    with pytest.raises(ProviderRecoveryRefusal) as refused:
        await adapter.finalize(request, context)

    assert str(refused.value) == _REMEDY
    assert len(_REMEDY) <= 512
    assert _store_names(store) == before
    assert not store.cleanup_finalized(record.binding, record.plan_identity)
    adapter.close()


@pytest.mark.anyio
@pytest.mark.parametrize("collect", [False, True])
async def test_remote_finalize_at_the_scan_bound_and_after_collection_frees_names(
    tmp_path: Path, collect: bool
) -> None:
    """#3475: 4096 counted names finalize; past it, collection frees names for the retry.

    The padding shares the released activation's System, so this proves only that unlinked
    files lower the count; teardown over the bound has its own test.
    """
    adapter, record, request, context, store = await _finalized_release_store(tmp_path)
    _pad_requests(store, record, _PADDING_ACTIVATION, 4097 if collect else 4096)
    if collect:
        with pytest.raises(ProviderRecoveryRefusal):
            await adapter.finalize(request, context)
        padding = record.binding.model_copy(update={"activation_id": str(_PADDING_ACTIVATION)})
        store.collect_torn_down_activation(padding, record.plan_identity, keep_plan=True)
        assert _counted_names(store) == 3

    await adapter.finalize(request, context)

    assert store.cleanup_finalized(record.binding, record.plan_identity)
    adapter.close()


@pytest.mark.anyio
async def test_remote_system_teardown_collects_over_the_scan_bound(tmp_path: Path) -> None:
    """#3475: teardown is the remedy, so store size never refuses it.

    4100 counted names hold 4098 ``.request`` names, past the bound teardown used to apply.
    """
    adapter, record, _request, _context, store = await _finalized_release_store(tmp_path)
    _pad_requests(store, record, UUID(record.binding.activation_id), 4100)

    assert await _system_teardown(store, _ProvingTeardownOperations())

    assert _store_names(store) == {_teardown_name(store)}
    adapter.close()
```

2. Change the marker test's docstring to `"""Markers are never counted, so neither finalize nor
   teardown refuses on them (#3419, #3475)."""`; keep its body.
3. Run the green command. Expect 3 failed, 2 passed: the refusal test and the `collect=True`
   case fail with `DID NOT RAISE`, the teardown test fails with `remote preparation store
   exceeds teardown scan bound`; `collect=False` and the marker test pass.
4. In `teardown.py`, replace the member `"remote preparation store exceeds teardown scan bound",`
   with the `_REMEDY` text as one implicitly concatenated literal inside the `Literal[...]`.
5. In `external_boot_authority.py`, after `_MAX_PREPARATION_BYTES`, add:

```python
# Names finalize parses by content and a completed System teardown collects (ADR-0787, #3475).
_FINALIZE_COUNTED_SUFFIXES = (".request", ".lifecycle-request", ".preparation")
_FINALIZE_SCAN_BOUND = 4096
```

6. In `teardown_preparation`, delete the comment `# Only .request names count: ...` and the
   `if len(names) > 4096:` block.
7. At the top of `_finalize_cleanup_quarantine`, before `identity = ...`, add:

```python
        counted = sum(
            1
            for name in os.listdir(self._root_fd)
            for suffix in _FINALIZE_COUNTED_SUFFIXES
            if len(name) == 64 + len(suffix)
            and name.endswith(suffix)
            and all(character in "0123456789abcdef" for character in name.removesuffix(suffix))
        )
        if counted > _FINALIZE_SCAN_BOUND:
            raise ProviderRecoveryRefusal(
                "remote preparation store holds more than 4096 request, lifecycle-request and "
                "preparation records, so finalize is refused; tear down Systems on this host so "
                "teardown collects their records, or, once every System on this host is torn "
                "down, follow the runbook procedure Clear store files of torn-down Systems, then "
                "retry"
            )
```

8. Replace the `_scan_owned` docstring's last two sentences with: `Only finalize's callers are
   bounded, by the count in _finalize_cleanup_quarantine; teardown and collection scans stay
   unbounded because they are the remedy (ADR-0787, #3475).`
9. Run the green command: all pass. Run `just lint`, `just type`, `just test-changed`: exit 0.
10. Commit `fix(remote-libvirt): bound release finalize, not System teardown (#3475)`.
11. Controlled faults, each reverted with `git checkout -- src/` and observed red with the green
    command: (a) replace the `raise ...` statement with `pass` → refusal test and
    `collect=True` red with `DID NOT RAISE`; (b) `>` to `>=` → `collect=False` red;
    (c) add `".cleanup-finalized"` to `_FINALIZE_COUNTED_SUFFIXES` → marker test red;
    (d) restore the teardown `4096` check → teardown test red. Record results in the ledger.

Rollback: revert the commit; nothing persisted changes shape.

## Task 2 — Records

Verification:

- Contract: ADR-0787 amendment and runbook paragraph. Mode: task-test-not-applicable — prose
  read by operators; no executable consumer validates its wording. `just records` and
  `just docs-links` check record status and link targets.

Steps:

1. Append to `docs/adr/0787-abortable-finalize-proof-checkpoint.md`:
   `### Amendment (2026-11-03): release finalize is bounded, System teardown is not (#3475)`,
   opening "This amendment supersedes the #3419 bullets "Accepted limitation: the finalize
   directory scans stay unbounded" and "System teardown's existing 4096-name bound in
   `teardown_preparation` is kept", and the 2026-11-02 sentence "The #3419 accepted limitation
   on unbounded scans stands until #3475 replaces it"." Bullets: the count, the counted
   suffixes and why `.recovery`, markers and `.teardown` records are not counted (spec section
   "What is counted, and why"); the refusal names both remedies and writes nothing; a refused
   release publishes the quarantine as any finalize failure does; teardown and collection stay
   unbounded because they are the remedy and a teardown bound strands the host; cost of an
   unbounded teardown, about (activations of the System) x (counted files) parses; the three
   kinds of counted file only the runbook clears. Considered & rejected, each tagged: bound teardown above release
   (judgment: the trap moves further out, and the only true remedy text is hand deletion);
   bound both at one value (judgment: no true remedy text for a host with a live non-finalized
   activation, per the attempt-1 park note); do nothing (judgment: the issue requires a bound
   on release).
2. In the runbook section "Clear store files of torn-down Systems", after its first paragraph,
   add: "Release and orphan delete refuse with `remote preparation store holds more than 4096
   ...` once the store holds more than 4096 `.request`, `.lifecycle-request` and
   `.preparation` files. A completed teardown collects its provable activations' files, so
   tearing down Systems lowers the count. It does not remove files of activations kept at
   teardown, of Systems torn down before #3472, or files that do not parse (the authority logs
   `unreadable and left in place`); when those hold the store over the limit, tearing down
   live Systems does not help: drain the host and use this procedure."
3. Run `just lint` and `git fetch origin main && just records` and `just docs-links`: exit 0.
4. Commit `docs(adr): amend ADR-0787 for the release scan bound (#3475)`.
