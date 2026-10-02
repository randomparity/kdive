# Plan: fixed live workers receive the remote-libvirt inventory (#3086)

Goal: carry the operator's inventory path to fixed slot workers so they register the
remote-libvirt runtime ([spec](../specs/2026-10-02-worker-remote-inventory-3086-design.md)).

Architecture: one optional `WorkerSettings.systems_toml` field crosses the peer-authenticated
lifecycle socket. The root witness checks the path's metadata before any slot mutation and writes
it into `worker.env`; the gate passes it through. The launcher sends it only when a remote instance
is declared.

Tech stack: Python 3.14, pydantic, bash, pytest. Guardrails: `just test-changed`, `just lint`,
`just type`, `git fetch origin main && just records`, full `just ci`.

Expected implementation size: 180–260 changed lines (M) — derived from the file map below (about
90 source lines across five files, 130 test lines, 40 doc lines).

## Global Constraints

- `LIFECYCLE_PROTOCOL_VERSION` stays `1`; only the schema hash moves.
- Do not add `KDIVE_SECRETS_ROOT`, `HOME`, or `XDG_CONFIG_HOME` to the gate allowlist or to
  `worker.env`.
- The witness never opens the inventory file: `os.lstat` only.
- No new dependency. Script-internal shell variables stay unprefixed (env-docs-check).
- No Ansible change in this PR, so the runner-task fixtures are untouched.

## File map

| File | Change | Owns |
|---|---|---|
| `src/kdive/processes/lifecycle/systemd/systemd_worker_contract.py` | modify | `systems_toml` field and validation |
| `src/kdive/processes/lifecycle/systemd/systemd_worker_state.py` | modify | `KDIVE_SYSTEMS_TOML` in `_environment` |
| `src/kdive/processes/lifecycle/systemd/systemd_worker_inventory.py` | create | `require_trusted_inventory`, `UntrustedInventory`, `slot_principals` |
| `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py` | modify | call the check first in `start` |
| `deploy/systemd/bin/kdive-live-worker-gate` | modify | allowlist entry |
| `scripts/live-stack/worker-lifecycle.sh` | modify | send the path; fail fast on the secrets root |
| `docs/adr/0574-…md`, `docs/operating/runbooks/remote-live-stack.md`, `deploy/systemd/README.md` | modify | decision and operator docs |

## Task 1 — contract field, environment, gate allowlist

Verification:
- Contract: absolute-path validation. Mode: focused-test. Add
  `test_settings_reject_relative_systems_toml` to `test_systemd_worker_contract.py`; it is red
  with "no field" until the field exists. Green: `uv run python -m pytest
  tests/processes/lifecycle/systemd/test_systemd_worker_contract.py -q`.
- Contract: env rendering with and without the field (AC1, AC3). Mode: focused-test. In
  `test_systemd_worker_state.py`, `test_environment_carries_inventory_only_when_set` asserts
  `KDIVE_SYSTEMS_TOML='/etc/kdive/systems.toml'` is present with the field and that the rendering
  without it equals the `SlotStore._environment` output for `start_payload()` with no inventory
  key. It also asserts no `KDIVE_SECRETS_ROOT` line. Red until `_environment` renders the key.
- Contract: gate parity (AC1, AC4). Mode: focused-test. `test_gate_allowlist_matches_slot_environment`
  in `tests/deploy/test_live_worker_gate.py` loads the gate with `runpy.run_path`. It renders
  `SlotStore._environment` with every optional field set (authority route, geometry, health bind,
  `systems_toml`) and asserts `namespace["_WORKER_ENV_NAMES"] == keys - {"KDIVE_WORKER_PYTHON",
  "KDIVE_WORKER_INCARNATION_ID", "KDIVE_WORKER_INCARNATION_KIND"}`, with `KDIVE_SECRETS_ROOT`
  absent from both. Extend the exec test's env and expected mapping with `KDIVE_SYSTEMS_TOML`.
  Red until the gate lists the name.

Steps:
1. Write the three tests and run them: red.
2. Contract: add `systems_toml: str | None = None` after `external_boot_capacity_bytes`. Add it
   to the `validate_string_bytes` field list, and add this validator:

   ```python
   @field_validator("systems_toml")
   @classmethod
   def validate_inventory_path(cls, value: str | None) -> str | None:
       """Accept only an absolute inventory path; the witness checks its metadata at start."""
       if value is not None and not value.startswith("/"):
           raise ValueError("worker inventory path must be absolute")
       return value
   ```
3. State: in `_environment`, after the health-bind line, add
   `if settings.systems_toml is not None: values["KDIVE_SYSTEMS_TOML"] = settings.systems_toml`.
4. Gate: add `"KDIVE_SYSTEMS_TOML",` to `_WORKER_ENV_NAMES` in sorted position.
5. Run the focused tests: green. Commit `feat(lifecycle): carry the worker inventory path`.

## Task 2 — witness metadata check (AC2)

Interfaces: `require_trusted_inventory(path: str, *, principals: Callable[[], tuple[frozenset[int],
frozenset[int]]] = slot_principals) -> None` raises `UntrustedInventory(ValueError)`.
`slot_principals() -> tuple[frozenset[int], frozenset[int]]` returns (uids, gids).
`SystemdWorkerLifecycle.__init__` gains `check_inventory: Callable[[str], None] =
require_trusted_inventory`.

Verification:
- Contract: metadata rules. Mode: focused-test. Create
  `tests/processes/lifecycle/systemd/test_systemd_worker_inventory.py` using real files under
  `tmp_path`, with `principals` injected as `(frozenset({uid}), frozenset({gid}))` built from the
  test user or a sentinel. Cases: trusted file accepted (principals that do not match); a
  non-normal path (`/a/../b`, `//x`) rejected; a symlink at the leaf and in an ancestor rejected;
  a directory, FIFO, or missing file rejected; a file owned by a forbidden uid rejected;
  group-writable with a forbidden gid rejected, and with an allowed gid accepted; world-writable
  file rejected; an ancestor that is group-writable by a forbidden gid rejected. A sticky
  world-writable ancestor is accepted, because `/tmp` is one. Use a `monkeypatch` of `os.lstat` in
  the module to present a sticky 1777 directory where needed. Also prove that `open` is never
  called: patch `builtins.open` and `os.open` in the module to raise. Red: the module does not
  exist.
- Contract: rejection before any slot mutation. Mode: focused-test. In
  `test_systemd_worker_lifecycle.py`, `test_start_rejects_untrusted_inventory_before_any_slot_action`
  builds the coordinator with `check_inventory` raising `UntrustedInventory`. It asserts
  `code == "invalid_request"`, `retry_action == "correct_request"`, and `events == []`.
  `test_start_without_inventory_skips_the_check` asserts the checker is not called when the field
  is absent.

Steps:
1. Write the tests: red.
2. Create the module:

   ```python
   """Metadata-only trust check for the operator inventory path (ADR-0574, #3086)."""

   _SLOT_ACCOUNTS = tuple(f"kdive-worker-{slot}" for slot in range(1, 9))
   _LIBVIRT_GROUP = "kdive-live-libvirt"


   class UntrustedInventory(ValueError):
       """The inventory path fails the fixed-worker trust rules."""


   def slot_principals() -> tuple[frozenset[int], frozenset[int]]:
       accounts = [pwd.getpwnam(name) for name in _SLOT_ACCOUNTS]
       gids = {grp.getgrnam(_LIBVIRT_GROUP).gr_gid}
       for account in accounts:
           gids.update(os.getgrouplist(account.pw_name, account.pw_gid))
       return frozenset(a.pw_uid for a in accounts), frozenset(gids)


   def require_trusted_inventory(path, *, principals=slot_principals):
       if not path.startswith("/") or os.path.normpath(path) != path or path.startswith("//"):
           raise UntrustedInventory("worker inventory path must be absolute and normalized")
       uids, gids = principals()
       parts = Path(path).parts
       for depth in range(1, len(parts) + 1):
           current = Path(*parts[:depth])
           try:
               metadata = os.lstat(current)
           except OSError as exc:
               raise UntrustedInventory("worker inventory path must exist") from exc
           leaf = depth == len(parts)
           _require_kind(metadata, leaf=leaf)
           _require_unwritable(metadata, uids, gids)
   ```

   `_require_kind` rejects `S_ISLNK` ("must not traverse a symlink"), a non-directory ancestor,
   and a non-regular leaf. `_require_unwritable` rejects `st_uid in uids`,
   `S_IWGRP and st_gid in gids`, and `S_IWOTH` unless the entry is a sticky directory. Messages are
   fixed strings; no metadata values are echoed.
3. Lifecycle: add the `check_inventory` constructor parameter. At the top of `start`, after the
   operation guard, add:

   ```python
   if request.settings is not None and request.settings.systems_toml is not None:
       try:
           self._check_inventory(request.settings.systems_toml)
       except UntrustedInventory as exc:
           return LifecycleResponse(
               ok=False, code="invalid_request", message=str(exc), retry_action="correct_request"
           )
   ```
4. Green; `just type`; commit `feat(lifecycle): reject an untrusted worker inventory at start`.

## Task 3 — launcher selection and fail-fast

Verification:
- Contract: send only with a remote instance; refuse a non-default secrets root. Mode:
  focused-test. In `tests/scripts/test_live_stack_scripts.py`, source the script with stubbed
  `require_compatible_lifecycle`, `require_start_prerequisites`, and
  `load_published_libvirt_uri`, as `test_lifecycle_start_rejects_mismatched_authority_geometry_before_request`
  does. Stub `request_path` with a `sitecustomize.py` that writes `request.settings.systems_toml`
  to a probe file. Cases: (a) an inventory in `tmp_path` with a `[[remote_libvirt]]` block →
  probe equals the absolute path; (b) an inventory with only `schema_version = 2` → probe `None`;
  (c) a remote block plus `KDIVE_SECRETS_ROOT=/elsewhere` → exit 2 and stderr naming
  `KDIVE_SECRETS_ROOT`, with no probe written. Extend
  `test_lifecycle_launcher_covers_required_worker_settings_and_authority_geometry` to require
  `systems_toml` among the settings keys. Red until the script changes.

Steps:
1. Write the tests: red.
2. In the heredoc, before `request = LifecycleRequest.model_validate(...)` in the start branch:

   ```python
   from kdive.config.core_settings import SECRETS_ROOT
   from kdive.inventory.path import systems_toml_path
   from kdive.providers.remote_libvirt.config import is_remote_libvirt_configured
   from kdive.security.secrets.secrets import secrets_root_from_env

   systems_toml = None
   if is_remote_libvirt_configured():
       if secrets_root_from_env() != Path(SECRETS_ROOT.default):
           print(
               "fixed workers read remote TLS refs only under the default "
               "KDIVE_SECRETS_ROOT; unset it",
               file=sys.stderr,
           )
           raise SystemExit(2)
       systems_toml = str(systems_toml_path())
   ```

   Add `"systems_toml": systems_toml,` to the settings dictionary. A relative resolved path fails
   pydantic validation and keeps the existing "construction failed safely" exit 2. Confirm that
   `Setting` exposes `.default` before relying on it (`rg -n "class Setting" src/kdive/config`).
3. Green; commit `feat(live-stack): send the remote inventory to fixed workers`.

## Task 4 — decision record and operator docs (AC7)

Verification: Mode: task-test-not-applicable. These are prose for operators; the doc and records
gates (`just records`, the doc checks in `just ci`) are the only executable consumers.

Steps:
1. Append the ADR-0574 amendment (2026-10-02) recording the setting, the metadata check, the
   fixed secrets root, and the rejected alternatives.
2. Remote runbook §1: state that fixed workers receive the inventory via `KDIVE_SYSTEMS_TOML`
   from the launcher. Place the file at an operator- or root-owned path the slot accounts can read
   but not write (for example `/etc/kdive/systems.toml` mode `0644`) and point the server and
   reconciler at it. Give the `remote-libvirt/` TLS layout and refs, and say that changes need a
   worker restart.
3. `deploy/systemd/README.md` lifecycle contract: one paragraph on the optional inventory setting
   and its trust rules.
4. `git fetch origin main && just records`; commit `docs: document worker inventory delivery`.

## Task 5 — live proof (AC6)

Verification: Mode: task-test-not-applicable — needs the lab hosts; outcomes are recorded
(sanitized) in the PR body.

Steps: on the lab control-plane host, check out the branch. Run `just prepare-local-libvirt-host`
and confirm the witness serves the new identity by checking for a symbol only this branch adds
(`systemd_worker_inventory.py` in the installed venv). Install the inventory and the TLS layout,
then run `demo-up.sh`. Check that `worker.env` and `/proc/<pid>/environ` carry
`KDIVE_SYSTEMS_TOML` and no `KDIVE_SECRETS_ROOT` or `HOME`. Run resources.list →
allocations.request (kind remote-libvirt) → systems.provision → ready → release → torn_down, and
confirm the remote host has no domain or volume left. Confirm the two stuck Systems tear down. Run
the negative arms: a symlinked inventory, a slot-writable inventory, and an omitted field.

## Deferrals

- Ansible provisioning of the `remote-libvirt/` TLS subdirectory → needs-triage follow-up under
  #2803 (filed at ship time).
