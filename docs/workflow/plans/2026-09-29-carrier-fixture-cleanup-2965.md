# Carrier fixture cleanup (#2965) — plan

Goal: a passing `create`-mode installed-authority carrier run removes the exact fixture domain and
artifacts it created, through its `ResourceLedger` and a root `--remove` mode of the fixture script.

Architecture: the fixture script derives the five fixture names from the System UUID, prints them
after `create`, and removes one exact name in `--remove` mode. The carrier support module records
each printed name as an `authority-fixture` ledger entry and, after a proof body that raised no
exception, calls `ledger.cleanup` with a callback that runs `--remove` for each entry. Spec:
`docs/workflow/specs/2026-09-29-carrier-fixture-cleanup-2965-design.md`.

Tech stack: Python 3.14, pydantic, pytest, libvirt-python (script only, already imported).

Expected implementation size: 190–260 changed lines (M) — about 60 script lines, 70 support lines,
120 test lines across two test files, and 25 runbook lines, from the file map below.

## Global Constraints

- No new dependency. No `KDIVE_*` variable. No ADR.
- Root deletion takes exact names only; never a prefix, glob, or caller path outside
  `_fixture_artifacts(system_id)`.
- The authority journal lane is never removed.
- Guardrails: `just lint`, `just type`, `just test-changed`, `just records` (after
  `git fetch origin main`), `just docs-links`, `just docs-paths`; pre-push `just ci`.
- Wording rules from AGENTS.md apply to docstrings and docs. Ruff line length 100.
- macOS: run the gates with Homebrew bash >= 4.4 and gnubin first on `PATH`.

## File map

| File | Change | Owns after |
|---|---|---|
| `scripts/live-vm/provision-authority-fixture.py` | modify | `_fixture_artifacts`, `_undefine_domain`, `_remove_fixture_artifact`, `--remove` mode, `create` output line |
| `tests/scripts/test_provision_authority_fixture.py` | modify | tests for the names, output line, and `--remove` |
| `tests/live_vm/installed_local_authority_support.py` | modify | `authority-fixture` kind and ledger rule, recording in `provision_authority_fixture`, `remove_authority_fixture`, the call in five carriers |
| `tests/live_vm/test_installed_local_authority_support.py` | modify | ledger rule, recording, removal, and carrier-call tests |
| `docs/operating/runbooks/live-testing.md` | modify | the cleanup contract in both carrier sections |

## Task 1: script names, output line, and `--remove` mode

Files: modify `scripts/live-vm/provision-authority-fixture.py`,
`tests/scripts/test_provision_authority_fixture.py`.

Interfaces (later tasks rely on the command-line contract only):

- `create` mode prints, as its last stdout line, `{"created": [<5 names in creation order>]}`.
- `sudo -n <python> <script> --remove <uuid> <identity>` exits 0 after it removes, or finds
  absent, exactly that one name; it exits nonzero for any other identity.

Verification:

- Contract: derived names. Mode: focused-test. `test_fixture_artifacts_are_exact_system_names`
  asserts the tuple for `_SYSTEM_ID` with the module roots patched to a temp tree. Red:
  `AttributeError: _fixture_artifacts`. Green:
  `uv run python -m pytest tests/scripts/test_provision_authority_fixture.py -q`.
- Contract: `--remove` refuses a foreign identity before mutation. Mode: focused-test.
  `test_remove_refuses_a_name_outside_the_fixture` (a sibling System's overlay path, and a
  prefix path) asserts `ValueError` and that no helper ran. Red: `SystemExit` from the old
  argument check. Green: same command.
- Contract: each object type is removed through its helper; absent is success. Mode:
  focused-test. `test_remove_deletes_one_exact_artifact` (temp tree: base, baseline, overlay,
  console removed one at a time; a second call is a no-op) and
  `test_remove_undefines_only_the_exact_authority_domain` (fake libvirt connection records
  `destroy`, `undefine`, the URI and the name). Red: missing function. Green: same command.
- Contract: `create` prints the set. Mode: focused-test.
  `test_create_reports_the_created_names` runs `main()` with the create-path helpers patched
  and reads `capsys`. Red: no stdout line. Green: same command.

Steps:

1. Write the four tests. Use the existing `_script()`, `_SYSTEM_ID`, and `_existing_fixture`
   helpers. For the domain test, patch `script.libvirt.open` with a connection whose
   `lookupByName` returns a domain object recording `isActive() -> 1`, `destroy()`,
   `undefine()`.
2. Run the focused command; expect the four new tests to fail.
3. In the script, add after `_fixture_base_destination`:

   ```python
   def _fixture_artifacts(system_id: UUID) -> tuple[str, ...]:
       """The exact names one create run makes for ``system_id``, in creation order."""
       return (
           str(_AUTHORITY_ROOTFS_ROOT / f"{system_id}-fixture-base.qcow2"),
           baseline_dir(system_id),
           overlay_path(system_id),
           str(console_log_path(system_id)),
           domain_name_for(system_id),
       )
   ```

4. Rename `_undefine_worker_domain(system_id)` to `_undefine_domain(uri: str, system_id: UUID)`;
   open `uri` instead of `_WORKER_URI`; the create path calls
   `_undefine_domain(_WORKER_URI, system_id)`. Update the one existing test that patches
   `_undefine_worker_domain`.
5. Add:

   ```python
   def _remove_fixture_artifact(
       system_id: UUID, identity: str, *, authority_uid: int, authority_gid: int
   ) -> None:
       """Remove one exact name of this System's fixture; refuse every other value."""
       if identity not in _fixture_artifacts(system_id):
           raise ValueError("refusing a name outside this System's authority fixture")
       if identity == domain_name_for(system_id):
           _undefine_domain(_AUTHORITY_URI, system_id)
           return
       path = Path(identity)
       _require_private_directory(path.parent, authority_uid, authority_gid)
       if identity == baseline_dir(system_id):
           _remove_directory(path)
       else:
           _remove_regular(path)
   ```

6. In `main()`, parse the mode (`None`, `--verify-existing`, `--remove`) with the argument
   counts 2, 3, 4. `--remove` resolves `pwd.getpwnam(_AUTHORITY)`, calls
   `_remove_fixture_artifact(system_id, sys.argv[3], ...)`, and returns before reading stdin.
   At the end of the create path add
   `print(json.dumps({"created": list(_fixture_artifacts(system_id))}))`.
7. Run the focused command; expect all tests in the file to pass. Run `just lint` and
   `just type`; expect exit 0. Commit `feat(live-vm): add exact-name remove mode to authority fixture`.

## Task 2: ledger records and removes the fixture

Files: modify `tests/live_vm/installed_local_authority_support.py`,
`tests/live_vm/test_installed_local_authority_support.py`.

Interfaces:

- Consumes Task 1's output line and `--remove` argument order.
- `ResourceLedger(prefix: str, *, fixture_system: UUID | None = None)`.
- `async def provision_authority_fixture(db_url: str, config: NativeAuthorityConfig, ledger: ResourceLedger) -> None`.
- `def remove_authority_fixture(config: NativeAuthorityConfig, ledger: ResourceLedger) -> None`.

Verification:

- Contract: ledger scope rule. Mode: focused-test.
  `test_ledger_scopes_authority_fixture_entries_to_the_configured_system` — accepted with the
  UUID in the identity; `ValueError("outside")` for another UUID and for a ledger without
  `fixture_system`. Red: pydantic rejects the new kind. Green:
  `uv run python -m pytest tests/live_vm/test_installed_local_authority_support.py -q`.
- Contract: `create` records the printed names in order; `verify-existing` records none; a
  missing line fails. Mode: focused-test. Extend the two existing
  `provision_authority_fixture` tests (fake `subprocess.run` returns the JSON line for create)
  and add `test_fixture_create_without_reported_names_fails`. Red: `TypeError` for the third
  argument. Green: same command.
- Contract: removal runs `--remove` per fixture entry in reverse order and skips other kinds;
  a nonzero exit fails that entry and the others still run. Mode: focused-test.
  `test_remove_authority_fixture_calls_exact_remove_in_reverse_order`. Red: missing function.
  Green: same command.
- Contract: carriers remove the fixture only after a proof body with no exception. Mode:
  focused-test. Extend `test_native_carriers_supply_the_required_cleanup_summary` to assert one
  `remove_authority_fixture` call, and add
  `test_native_carrier_keeps_the_fixture_after_a_failed_proof` (fake
  `assert_root_release_completion` raises; the error propagates; no removal call). Red: no
  call recorded. Green: same command.

Steps:

1. Write or extend the tests above. Change every fake `provision(_db_url, _config)` in the file
   to `provision(*_args: object) -> None`.
2. Run the focused command; expect the new assertions to fail.
3. Add `"authority-fixture"` to `OwnedResource.kind`. Add the `fixture_system` keyword and this
   rule at the start of `ResourceLedger.record`'s scope check:

   ```python
   if resource.kind == "authority-fixture":
       if self.fixture_system is None or str(self.fixture_system) not in resource.identity:
           raise ValueError("authority fixture identity is outside the configured System")
   elif self.prefix not in resource.identity and resource.kind not in {
       "investigation",
       "run",
       "activation",
   }:
       raise ValueError("resource identity is outside the invocation ownership scope")
   ```

4. Add module constants `_AUTHORITY_PYTHON = "/opt/kdive-provider-authority/.venv/bin/python"`
   and `_FIXTURE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/live-vm/provision-authority-fixture.py"`;
   use them in `provision_authority_fixture`.
5. Give `provision_authority_fixture` the `ledger` parameter. After the return-code check, in
   `create` mode, parse the last stdout line:

   ```python
   if config.fixture_mode == "create":
       lines = result.stdout.strip().splitlines()
       try:
           created = json.loads(lines[-1])["created"]
       except (IndexError, ValueError, KeyError, TypeError):
           created = None
       if not isinstance(created, list) or not all(isinstance(n, str) for n in created):
           raise RuntimeError("authority fixture create did not report its created names")
       for identity in created:
           ledger.record(OwnedResource(kind="authority-fixture", identity=identity))
   ```

6. Add `remove_authority_fixture`: `ledger.cleanup` with a callback that returns for other
   kinds and otherwise runs
   `["sudo", "-n", _AUTHORITY_PYTHON, str(_FIXTURE_SCRIPT), "--remove", str(config.system_id), resource.identity]`
   with `text=True, capture_output=True, check=False`, raising
   `RuntimeError(f"authority fixture remove failed for {resource.identity}: {detail}")` on a
   nonzero exit (`detail` is the last 1000 characters of stderr or stdout).
7. In the five carriers (`run_installed_local_authority_normal_operations`, `..._ppc64le_...`,
   `..._restart_recovery`, `..._unresolved_call_takeover`, `..._journal_restore_recovery`):
   construct `ResourceLedger(config.ownership_prefix, fixture_system=config.system_id)`, pass
   `ledger` to `provision_authority_fixture`, and after the Investigation close add

   ```python
   if primary is None:
       try:
           remove_authority_fixture(config, ledger)
       except Exception as exc:
           cleanup_failures.append(exc)
   ```

   (the journal-restore carrier appends to `failures`; the takeover carrier, which has no
   `primary`, calls `remove_authority_fixture(config, ledger)` after its close assertion inside
   the `try`).
8. Run the focused command; expect pass. Run `just lint`, `just type`; expect exit 0. Commit
   `fix(live-vm): record and remove the carrier's authority fixture`.

## Task 3: runbook contract

Files: modify `docs/operating/runbooks/live-testing.md`.

Verification:

- Contract: runbook prose for both carrier sections. Mode: task-test-not-applicable. The
  changed surface is operator prose with no executable consumer; `just docs-links` and
  `just docs-paths` check only its links and paths.

Steps:

1. In the x86_64 section, after the re-provision paragraph, add a paragraph: in `create` mode
   the carrier records the five exact names; after a proof with no exception it removes them
   in reverse order through `provision-authority-fixture.py --remove <uuid> <name>`; a failed
   proof keeps them for diagnosis; the journal lane `journal/<uuid>.jsonl` stays because the
   authority requires every lane to match its database head; `verify-existing` removes nothing.
   Give the manual command as a loop over the five names.
2. In the ppc64le section, add one sentence that the same fixture cleanup contract applies.
3. Run `just docs-links` and `just docs-paths`; expect exit 0. Commit
   `docs(runbook): state the carrier fixture cleanup contract`.

## Task 4: live proof (no code)

On the native ppc64le host, mint a fresh System, run
`tests/live_vm/test_installed_local_authority_ppc64le.py` with `fixture_mode=create`, and record
before/after listings of `virsh list --all` on the authority daemon,
`/var/lib/kdive/provider-authority/{rootfs,console,journal}`, and the worker daemon's domains.
Use the migration-owner DSN for the carrier's evidence queries (#2953).
