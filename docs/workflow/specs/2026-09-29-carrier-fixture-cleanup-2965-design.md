# Carrier fixture cleanup (#2965) — design

Issue: #2965 (child of #2954). Plan:
`docs/workflow/plans/2026-09-29-carrier-fixture-cleanup-2965.md`.

## Problem

In `create` mode, the installed-authority carrier runs
`scripts/live-vm/provision-authority-fixture.py` as root. The script recreates the selected
System's domain and artifacts on the private authority daemon. The carrier's `ResourceLedger`
records none of them, and no carrier calls `ledger.cleanup`. After a passing run the domain
stays running and its files stay under `/var/lib/kdive/provider-authority/` (#2865 native
ppc64le proof). The runbook forbids a prefix-wide or host-wide reaper, so an operator removes
them by hand.

## Scope

The fixture that one `create` run makes is this exact set of names, in creation order
(`<uuid>` is the configured `system_id`):

| Identity | Kind of object |
|---|---|
| `/var/lib/kdive/provider-authority/rootfs/<uuid>-fixture-base.qcow2` | regular file |
| `/var/lib/kdive/provider-authority/rootfs/<uuid>-baseline` | directory |
| `/var/lib/kdive/provider-authority/rootfs/<uuid>-overlay.qcow2` | regular file |
| `/var/lib/kdive/provider-authority/console/<uuid>.log` | regular file |
| `kdive-<uuid>` | domain on the authority daemon |

1. **Script owns the names.** A new `_fixture_artifacts(system_id)` in the script returns the
   table above. It uses the helpers that `create` mode already uses
   (`_fixture_base_destination` naming, `baseline_dir`, `overlay_path`, `console_log_path`,
   `domain_name_for`). After a successful `create`, the script prints one last stdout line,
   `{"created": [<identities in creation order>]}`.
2. **`--remove <uuid> <identity>` mode.** The script runs as root. It requires `identity` to be
   exactly one member of `_fixture_artifacts(uuid)`, and it refuses any other value before a
   mutation. It reads no stdin. It removes only that one object:
   - The domain: destroy it if it is active, then undefine it on the authority daemon. An
     absent domain is success.
   - The baseline: `_remove_directory` (a symlink is refused; an absent directory is success).
   - A file: `_remove_regular` (a non-regular file or a hard-linked file is refused; an absent
     file is success).
   Before it removes a file, the script requires the parent (`rootfs` or `console`) to be the
   authority-owned mode-0700 directory (`_require_private_directory`). The worker-domain
   helper `_undefine_worker_domain` becomes `_undefine_domain(uri, system_id)`, and both modes
   use it.
3. **Ledger kind.** `OwnedResource.kind` gets `authority-fixture`. `ResourceLedger` gets a
   keyword `fixture_system: UUID | None = None`. `record` accepts an `authority-fixture` entry
   only when `fixture_system` is set and `str(fixture_system)` is in the identity. The five
   carriers that call `provision_authority_fixture` pass `fixture_system=config.system_id`.
4. **Recording.** `provision_authority_fixture(db_url, config, ledger)` parses the last stdout
   line in `create` mode and records each name as an `authority-fixture` entry, in order. A
   missing or malformed line fails the carrier. `verify-existing` records nothing, because
   that run did not create the fixture.
5. **Removal.** A new `remove_authority_fixture(config, ledger)` calls `ledger.cleanup`. The
   callback runs `sudo -n <authority python> <script> --remove <uuid> <identity>` for each
   `authority-fixture` entry and ignores the other kinds. Reverse order removes the domain
   first. A nonzero exit fails that entry, and the callback then refuses every later fixture
   entry without running the script. A domain that did not go away thus keeps its backing
   files for diagnosis.
6. **When.** Each carrier calls `remove_authority_fixture` after its Investigation close, and
   only when the proof body and the close both raised no exception. A failed run keeps the
   fixture for diagnosis. The runbook gives the manual `--remove` command for that case.
7. **Runbook.** Both carrier sections of `docs/operating/runbooks/live-testing.md` state this
   contract, the kept journal lane, and the manual command.

The authority journal lane `journal/<uuid>.jsonl` is not in the set. The authority creates it,
not the fixture script. The authority's startup requires an exact match between the local
journal lanes and the database heads (`_restore_journal_inventory` in
`src/kdive/providers/external_boot_authority/host.py`). If the lane is removed, the authority
refuses to start with `inventory-mismatch`. The lane stays as an audit record.

No transition of ownership: the script stays the only root actor, and the support module stays
the carrier's orchestrator.

## Failure model

1. **Actors and deployments**
   - An operator on a disposable native live host (x86_64 or ppc64le KVM) runs one carrier
     with passwordless `sudo -n`.
   - The fixed worker identities and the control identity are not trusted with authority files.
2. **Invariants and assets at stake**
   - Root deletes only the five exact names of the configured System. It never deletes a
     prefix, a glob, a caller path outside the set, or a symlink target.
   - The authority journal lane and the database rows stay intact.
   - A failed proof keeps its fixture for diagnosis.
3. **Accepted failure classes**
   - A `create` that fails part way leaves the names it made. Accepted: the run failed, the
     diagnosis needs them, and the runbook gives the manual command.
   - The authority identity can race a path check against the unlink. Accepted: that identity
     owns the directories and is the trusted actor for them.
   - The System row stays `ready` after the domain is removed. Accepted: the operator releases
     the disposable System, and its ordinary teardown finds no domain (#2865).
4. **Covered elsewhere**
   - Teardown routing for a domain on the authority daemon: #2966.
   - Reuse of a System after release: #2968.
   - The carrier's database DSN preflight: #2953.

### Threat model

- **Boundaries.** One added boundary: the carrier (operator identity) passes an identity string
  to a root process. One widened boundary: the root script now deletes, not only creates.
- **Actors.** The operator is trusted. The worker and control identities cannot write in the
  0700 authority directories. The ledger identity is data that the script re-derives. It is not
  trusted.
- **Controls.** The script accepts only an exact member of its own derived set, for a UUID that
  `UUID()` parses. Files must be regular with one link, and their parents must be authority
  owned and mode 0700. A directory must not be a symlink. The domain name is exact.
- **Out of scope.** A compromised root or authority identity; a prefix-wide reaper (refused by
  the runbook).

## Success

- After a passing `create`-mode carrier run, the five names in the table are absent. `virsh`
  on the authority daemon lists no `kdive-<uuid>`. The journal lane and the other Systems'
  files are unchanged.
- `--remove` with a value outside the derived set exits nonzero before any mutation.
- `verify-existing` records no fixture entry and removes nothing.

## Validation

- Script unit tests (`tests/scripts/test_provision_authority_fixture.py`): the derived set and
  its order; `create` prints the set; `--remove` refuses a foreign identity before mutation;
  each object type is removed through its helper; an absent object is success.
- Support unit tests (`tests/live_vm/test_installed_local_authority_support.py`): the ledger
  scope rule for `authority-fixture`; `create` records the printed names and
  `verify-existing` records none; `remove_authority_fixture` calls `--remove` in reverse order
  and skips other kinds.
- Live proof: one ppc64le carrier run (`fixture_mode=create`, freshly minted System) with a
  before/after listing of the authority daemon domains, the two authority directories, and the
  journal lane.
