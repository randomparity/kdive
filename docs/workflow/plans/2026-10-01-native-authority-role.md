# Native authority role — implementation plan (#3066)

Goal: native coverage cells stop requiring the `authority` role. The live evidence seam fails
closed when an installed authority revision cannot be read.

Architecture: one change to `_native_cell` roles in the contract, one guard in `run_identity`, and
the doc amendments. `results.py` is unchanged. Spec:
[2026-10-01-native-authority-role-design.md](../specs/2026-10-01-native-authority-role-design.md).

Tech stack: Python 3.14, pytest, `uv`, `just`.

Expected implementation size: 60–110 changed lines (M) — two one-line-to-twelve-line source edits,
about six focused tests, three doc paragraphs.

## Global Constraints

- Ruff line length 100; `ty` strict over the whole tree (`just type`).
- No new dependency. No new `KDIVE_*` variable.
- Prose: no "critical", "robust", "comprehensive", "elegant"; use Milestone, never Sprint.
- ADR-0715 is append-only outside `## Status`; the amendments are already on the branch.
- Do not edit `scripts/host_install_proof.py`, `tests/integration/test_host_install_live.py`, or
  `obligations.toml` (PR #3071 owns them).

## File map

| File | Change | Criterion |
|---|---|---|
| `scripts/coverage_campaign/contract.py` | `_native_cell` roles drop `authority` | 1 |
| `tests/scripts/test_coverage_contract.py` | one test | 1 |
| `tests/integration/live_stack/evidence.py` | `_present` plus a `run_identity` guard | 2, 3 |
| `tests/integration/live_stack/test_evidence.py` | `present` injection, new cases | 2 |
| `tests/scripts/test_results.py` | unrequired stale role still fails | 3 |
| `docs/development/coverage-qualification.md` | `deployed_roles` row | 4 |
| `docs/operating/runbooks/live-testing.md` | drop the "until #3066" claim | 4 |

## Task 1 — native cells require server, worker and reconciler

Interfaces: none consumed; `Cell.roles` keeps its type `tuple[str, ...]`.

Verification:
- `Mode: focused-test`. Contract: no native cell lists `authority`; every functional tool cell in an
  `authority = true` group does. Test:
  `tests/scripts/test_coverage_contract.py::test_authority_role_follows_the_scenario`.
  Red: the assertion on native cells fails, because they carry `authority`. Green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.

Steps:
1. Add the test:

```python
_NATIVE = {
    "image-smoke",
    "deep-lifecycle",
    "tcg-upload-boot",
    "host-install",
    "failure-resource",
    "kernel-corpus",
}


def test_authority_role_follows_the_scenario(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells
    native = [c for c in cells if c.operation in _NATIVE]
    assert {c.operation for c in native} == _NATIVE
    assert all(c.roles == ("server", "worker", "reconciler") for c in native)
    routed = {name for g in load_mapping().groups if g.authority for name in g.tools}
    tool = [c for c in cells if c.operation in routed and c.kind == "functional"]
    assert tool and all("authority" in c.roles for c in tool)
```

2. Run it and confirm the red failure.
3. In `_native_cell`, change `roles=("server", "worker", "reconciler", "authority"),` to
   `roles=("server", "worker", "reconciler"),`.
4. Run it and confirm green, then `just lint` and `just type`. Commit as
   `fix(coverage): require authority only where the scenario routes through it (#3066)`.

## Task 2 — an installed authority revision is recorded or the run stops

Interfaces: `run_identity(..., present: Callable[[str], bool] = _present)`. The new parameter comes
last, so existing callers (`test_image_smoke_live.py`) are unchanged. `_present(path: str) -> bool`.

Verification:
- `Mode: focused-test`. Contract: the four `run_identity` authority cases. Tests in
  `tests/integration/live_stack/test_evidence.py`:
  - `test_an_absent_authority_is_not_read_or_recorded`
  - `test_an_installed_authority_that_cannot_be_identified_stops_the_run` (parametrized: unreadable
    `None`, empty `""`, unresolvable `"zzz"`)
  - the existing `test_run_identity_resolves_every_role_to_the_candidate` (present, resolvable)
  - `test_a_stale_authority_is_recorded_as_a_mismatch`

  Red: the stop-the-run cases do not raise. Green:
  `uv run python -m pytest tests/integration/live_stack/test_evidence.py -q`.
- `Mode: focused-test`. Contract: `_present` is false only for a provably absent path. Test:
  `test_presence_is_false_only_when_absence_is_proven`, using `tmp_path` with an absent file, a
  present file, and a file under a mode-000 directory (skipped when euid is 0). Red: no `_present`
  exists, so the import fails. Green: the same command.
- `Mode: focused-test`. Contract: a stale recorded role the cell does not require still fails
  qualification. Test: `tests/scripts/test_results.py::test_a_recorded_role_the_cell_does_not_require_is_still_judged`.
  Red: not applicable, because it pins unchanged `results.py` behavior. Bite check: narrowing
  `results.py` line 77 to `cell.roles` turns it red. Green:
  `uv run python -m pytest tests/scripts/test_results.py -q`.

Steps:
1. In `test_evidence.py`, give `_identity` a `present: Callable[[str], bool] = lambda _p: True`
   keyword and pass `present=present` to `run_identity`. Change
   `test_unreadable_roles_are_omitted_and_reported_missing` to pass `present=lambda _p: False` and
   `read=_unexpected_read`, with `def _unexpected_read(_p: str) -> str | None: raise AssertionError`.
   Add the tests:

```python
def _unexpected_read(_path: str) -> str | None:
    raise AssertionError("an absent authority must not be read")


def test_an_absent_authority_is_not_read_or_recorded() -> None:
    identity = _identity(present=lambda _p: False, read=_unexpected_read)
    assert "authority" not in identity.deployed_roles


@pytest.mark.parametrize("installed", [None, "", "zzz"])
def test_an_installed_authority_that_cannot_be_identified_stops_the_run(
    installed: str | None,
) -> None:
    with pytest.raises(RuntimeError, match="provider-authority/revision"):
        _identity(read=lambda _p: installed)


def test_a_stale_authority_is_recorded_as_a_mismatch() -> None:
    identity = _identity(read=lambda _p: "def5678")
    assert identity.deployed_roles["authority"] == _OTHER
    assert identity_problems(identity, ()) == []


def test_presence_is_false_only_when_absence_is_proven(tmp_path: Path) -> None:
    assert not _present(str(tmp_path / "absent"))
    assert not _present(str(tmp_path / "absent" / "revision"))
    (tmp_path / "revision").write_text("x")
    assert _present(str(tmp_path / "revision"))
    if os.geteuid() == 0:
        pytest.skip("root reads through a mode-000 directory")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert _present(str(locked / "revision"))
    finally:
        locked.chmod(0o700)
```

   `"zzz"` is not a key in `resolve`'s table, so it resolves to `None`. A stale authority does not
   appear in `identity_problems` when the cell does not require it; `results.py` reports it as
   `deployed-revision-mismatch`. No existing test pins that for a role the cell does not require
   (`test_results.py` and `test_coverage_cli.py` mutate required roles only), so add to
   `tests/scripts/test_results.py`:

```python
def test_a_recorded_role_the_cell_does_not_require_is_still_judged() -> None:
    contract, bindings, results = complete_evidence()
    cells = tuple(replace(c, roles=("server", "worker", "reconciler")) for c in contract.cells)
    contract = replace(contract, cells=cells)
    assert qualify(contract, bindings, results).passed
    roles = {**results[0].deployed_roles, "authority": "d" * 40}
    results[0] = results[0].model_copy(update={"deployed_roles": roles})
    report = qualify(contract, bindings, results)
    assert "deployed-revision-mismatch" in report.cells[0].reasons
```

   It is green from the start: it pins `results.py`, which this change relies on but does not
   modify.
2. Run the tests and confirm red.
3. In `evidence.py`, add `_present` after `_read_privileged`:

```python
def _present(path: str) -> bool:
    """False only when ``path`` is provably absent; an unprovable absence counts as present."""
    try:
        os.lstat(path)
    except FileNotFoundError, NotADirectoryError:
        return False
    except OSError:
        return True
    return True
```

   Add `present: Callable[[str], bool] = _present,` as the last `run_identity` keyword. Replace the
   authority block with:

```python
    if present(AUTHORITY_REVISION):
        installed = read(AUTHORITY_REVISION)
        authority = resolve(installed) if installed else None
        if authority is None:
            raise RuntimeError(
                f"{AUTHORITY_REVISION} is installed but its revision cannot be read with "
                "`sudo -n` or resolved in this checkout; grant passwordless read, fetch the "
                "installed commit, or remove the stale install"
            )
        roles["authority"] = authority
```

4. Confirm green, then run `just lint`, `just type` and `just test-changed`. Commit as
   `fix(live-stack): stop when an installed authority cannot be identified (#3066)`.

## Task 3 — guide and runbook

Verification:
- `Mode: task-test-not-applicable`. Surface: prose in two docs. Reason: no executable consumer
  parses these sentences. The doc checks in `just ci` gate links and formatting.

Steps:
1. In `docs/development/coverage-qualification.md`, change the `deployed_roles` row to:
   "Map of deployed `server`, `worker`, `reconciler` and, when installed, `authority` revisions to
   full SHAs. It must include the cell's required roles."
2. In `docs/operating/runbooks/live-testing.md`, replace the sentence starting "The default demo-up
   lane installs no provider authority" with: "Image-smoke cells do not require the provider
   authority. When one is installed, its revision must be the candidate, and the evidence seam must
   be able to read `/opt/kdive-provider-authority/revision` through `sudo -n`. Otherwise the run
   stops (ADR-0715)."
3. Stage the files, run `prek run` and commit as `docs: describe the scenario-scoped authority role (#3066)`.

## Task 4 — live proof (native x86_64 demo-up host)

Verification:
- `Mode: task-test-not-applicable` for the source tree. Surface: operator procedure. Reason: this
  task produces evidence, not code. The outcome is recorded and redacted in the PR body.

Steps:
1. Push the branch to the host checkout. Run `demo-down.sh`, `just prepare-local-libvirt-host` and
   `demo-up.sh`. Confirm that server, reconciler and every worker slot report the branch head on
   `/readyz`.
2. Recompute bindings and run the image smoke and `qualify` per the runbook section "Catalog image
   smoke and coverage evidence". Expected: the passing rows qualify, with no `deployed-role-missing`.
3. Before Fault 1, confirm `sudo -n true` succeeds. Fault 1: create
   `/opt/kdive-provider-authority` with sudo and write `git rev-parse HEAD~1` (a commit the checkout
   resolves) to its `revision` file, then rerun one cell. Expected: `qualify` reports `deployed-revision-mismatch`. Fault 2: replace the file with a
   dangling symlink, which `lstat` finds and `sudo -n cat` cannot read. Expected: `run_identity`
   raises and the cell records nothing.
4. Remove the fault files and directory. Confirm there are no leftover domains or volumes.

## Deferrals

None. Exclusions and their owners are in the spec's failure model.
