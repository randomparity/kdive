# Plan: fixed live workers receive the remote-libvirt inventory (#3086)

Goal, architecture, and rules: the [spec](../specs/2026-10-02-worker-remote-inventory-3086-design.md)
(design 1–5, failure model). Tech stack: Python 3.14, pydantic, bash, and pytest.

The guardrails are `just test-changed`, `just lint`, and `just type`, then
`git fetch origin main && just records`, then the full `just ci`.

Expected implementation size: 220–300 changed lines (M) — from the file map: about 100 source
lines across five files, 150 test lines across five files, and 40 runbook/README lines (the ADR
amendment is already written).

## Global Constraints

- `LIFECYCLE_PROTOCOL_VERSION` stays `1`; only the schema hash moves.
- Do not add `KDIVE_SECRETS_ROOT`, `HOME`, or `XDG_CONFIG_HOME` to the gate allowlist or to
  `worker.env`.
- The witness never opens the inventory file; it uses `os.lstat` only.
- No new dependency. Script-internal shell variables stay without the `KDIVE_` prefix
  (env-docs-check).
- No Ansible change, so the runner-task fixtures are untouched.

## File map

| File | Change |
|---|---|
| `src/kdive/processes/lifecycle/systemd/systemd_worker_contract.py` | `systems_toml` field and validation |
| `src/kdive/processes/lifecycle/systemd/systemd_worker_state.py` | `KDIVE_SYSTEMS_TOML` in `_environment` |
| `src/kdive/processes/lifecycle/systemd/systemd_worker_inventory.py` | new: the metadata trust check |
| `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py` | run the check first in `start` |
| `deploy/systemd/bin/kdive-live-worker-gate` | allowlist entry |
| `scripts/live-stack/worker-lifecycle.sh` | resolve, check, and send the path |
| `tests/processes/lifecycle/systemd/test_systemd_worker_{contract,state,inventory,lifecycle}.py` | focused tests |
| `tests/deploy/test_live_worker_gate.py`, `tests/scripts/test_live_stack_scripts.py` | focused tests |
| `docs/operating/runbooks/remote-live-stack.md`, `deploy/systemd/README.md` | operator docs |

## Task 1: contract field, environment, gate allowlist, diagnostics (AC1, AC3, AC4, AC5)

Verification, all focused tests:

- `test_systemd_worker_contract.py`: a relative `systems_toml` is rejected. It is red until the
  field exists, because `extra="forbid"` rejects the unknown key.
- `test_systemd_worker_state.py`: with `systems_toml="/etc/kdive/systems.toml"`, `_environment`
  contains `KDIVE_SYSTEMS_TOML=/etc/kdive/systems.toml`. Without the field, the output has no
  inventory line and equals the rendering of `start_payload()`. No `KDIVE_SECRETS_ROOT` line
  appears in either case.
- `tests/deploy/test_live_worker_gate.py`:
  - `test_gate_allowlist_matches_slot_environment`. Load the gate with `runpy.run_path`, render
    `SlotStore._environment` with every optional field set (authority route and geometry, health
    bind, `systems_toml`), and assert that `_WORKER_ENV_NAMES` equals those keys minus
    `KDIVE_WORKER_PYTHON`, `KDIVE_WORKER_INCARNATION_ID`, and `KDIVE_WORKER_INCARNATION_KIND`.
    Assert that `KDIVE_SECRETS_ROOT` is in neither.
  - Extend the exec test so it carries `KDIVE_SYSTEMS_TOML` through.
- `test_systemd_worker_lifecycle.py`: a `worker.env` written through the existing
  `load_slot_redaction_values` fixture with a `KDIVE_SYSTEMS_TOML` line does not return that
  value.

Steps:

1. Write the tests; confirm they are red.
2. Contract: add `systems_toml: str | None = None`. Add it to the `validate_string_bytes` list,
   and add a validator that rejects a value not starting with `/` ("worker inventory path must be
   absolute").
3. State: render `KDIVE_SYSTEMS_TOML` when the field is set.
4. Gate: insert `"KDIVE_SYSTEMS_TOML"` in sorted position.
5. Confirm green and commit.

## Task 2: witness metadata check (AC2)

Interfaces:

- `UntrustedInventory(ValueError)`.
- `slot_principals() -> tuple[frozenset[int], frozenset[int]]` returns the uids of
  `kdive-worker-1`..`8`, and the gids from `os.getgrouplist` for each account plus
  `kdive-live-libvirt`.
- `require_trusted_inventory(path: str, *, principals=slot_principals) -> None` applies the rules
  in spec design 3, with fixed messages.
- `SystemdWorkerLifecycle.__init__(..., check_inventory: Callable[[str], None] =
  require_trusted_inventory)`.

Verification, all focused tests:

- New `test_systemd_worker_inventory.py`, on real files under `tmp_path` with `principals`
  injected. Accepted cases:
  - a trusted file;
  - a group-writable file whose gid is outside the forbidden set;
  - a sticky world-writable ancestor (the real `/tmp`).

  Rejected cases:
  - a relative, `..`, or `//` path;
  - a symlink at the leaf or in an ancestor;
  - a directory, a FIFO, or a missing path;
  - a file owned by a forbidden uid;
  - a group-writable file with a forbidden gid;
  - a world-writable file;
  - an ancestor that is group-writable by a forbidden gid.

  `builtins.open` and `os.open` are patched to raise, which proves the check never opens the file.
  The tests are red because the module does not exist yet.
- `test_systemd_worker_lifecycle.py`:
  - a `check_inventory` that raises gives `invalid_request` / `correct_request` with
    `events == []`;
  - the checker is not called when the field is absent.

Steps:

1. Write the tests; confirm they are red.
2. Create the module.
3. In `start`, after the operation guard and before `unmanaged_workers`, call the checker when
   `request.settings.systems_toml` is set. On `UntrustedInventory`, return `invalid_request` /
   `correct_request` with the exception message.
4. Confirm green, run `just type`, and commit.

## Task 3: launcher (spec design 4)

Verification, focused tests in `tests/scripts/test_live_stack_scripts.py`. Source the script with
stubbed `require_compatible_lifecycle`, `require_start_prerequisites`, and
`load_published_libvirt_uri`, as `test_lifecycle_start_rejects_mismatched_authority_geometry_before_request`
does. Stub `request_path` through `sitecustomize.py` to write `request.settings.systems_toml` to a
probe file, and stub `require_worker_path_access` with a bash function that records its arguments.
Cases:

- (a) A remote inventory: the probe holds the absolute path, and access checks ran for
  `<path> r` and `/var/lib/kdive/secrets x`.
- (b) A local-only inventory: the probe holds `None` and no access check ran.
- (c) A remote inventory with `KDIVE_SECRETS_ROOT=/elsewhere`: exit 2, stderr names
  `KDIVE_SECRETS_ROOT`, and no probe is written.
- (d) A remote inventory with a relative `KDIVE_SYSTEMS_TOML`: exit 2, stderr names
  `KDIVE_SYSTEMS_TOML`.
- (e) The access-check stub fails: exit 1 and no probe.

Also extend the settings-key coverage test to require `systems_toml`. Every case is red before
the change.

Steps:

1. Write the tests; confirm they are red.
2. Add `resolve_worker_inventory`. It runs `"$py" -c` and prints nothing when
   `is_remote_libvirt_configured()` is false. Otherwise it exits 2 with a message when
   `systems_toml_path()` is not absolute, or when `secrets_root_from_env() !=
   Path(SECRETS_ROOT.default)`. If neither applies, it prints the path.
3. In `request start`, after `require_start_prerequisites`, set `inventory`. When it is non-empty,
   run `require_worker_path_access "$inventory" r "worker inventory"` and
   `require_worker_path_access /var/lib/kdive/secrets x "worker secrets root"`. Pass the path as
   `LIFECYCLE_SYSTEMS_TOML`, and in the heredoc set
   `"systems_toml": os.environ.get("LIFECYCLE_SYSTEMS_TOML") or None`.
4. Confirm green, run `shellcheck` through `just lint`, and commit.

## Task 4: operator docs (AC7)

Verification: task-test-not-applicable. This is operator prose, and the doc gates in `just ci` are
its only consumer.

Steps:

1. Remote runbook §1 covers:
   - how fixed workers receive the inventory;
   - placing it at a root- or operator-owned path that slots can read but not write (for example
     `/etc/kdive/systems.toml`, mode `0644`), with `KDIVE_SYSTEMS_TOML` pointing the server and
     reconciler at it;
   - the `remote-libvirt/` TLS layout and refs, with the secrets root at `0711`;
   - restarting after adding or removing a block, and editing entries atomically.
2. Add one paragraph to the `deploy/systemd/README.md` lifecycle contract.
3. Run `just records`, then commit.

## Task 5: live proof (AC6)

Verification: task-test-not-applicable. It needs lab hosts; the sanitized outcomes go in the PR
body.

Steps:

1. Check out the branch on the control-plane host, then run `just prepare-local-libvirt-host`.
2. Confirm that the installed venv has `systemd_worker_inventory.py` (only this branch adds it)
   and that the witness serves the new identity.
3. Install the inventory and the TLS layout, then run `demo-up.sh`.
4. Check that `worker.env` and `/proc/<pid>/environ` contain `KDIVE_SYSTEMS_TOML` and contain
   neither `KDIVE_SECRETS_ROOT` nor `HOME`.
5. Run a remote allocate → provision → `ready` → release → `torn_down`, and confirm the provider
   host has no domain or volume left.
6. Confirm that the stuck Systems tear down.
7. Run the negative arms: a symlinked inventory, a slot-writable one, a slot-unreadable one, and an
   omitted field.

## Deferrals

- Ansible provisioning of the `remote-libvirt/` TLS subdirectory → needs-triage follow-up under
  #2803.
