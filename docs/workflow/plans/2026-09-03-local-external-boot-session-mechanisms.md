# Local external-boot session mechanisms — implementation plan

**Goal.** Give `LocalExternalBootSessionFactory` five concrete host mechanisms and give
`build_external_boot_session_factory` its first production caller, leaving `observe_running` on its
fail-closed default.

**Architecture.** One new module, `src/kdive/providers/local_libvirt/lifecycle/boot/
session_mechanisms.py`, holding a nominal operation lane, a recovery-root-confined artifact-root
opener, a bounded payload cleaner, and a libguestfs handle opener. One new builder in
`composition.py` assembles them from settings and calls the existing
`build_external_boot_session_factory`. Nothing in `session.py` changes.

**Tech stack.** Python 3.14, `uv`. Stdlib only — `os`, `stat`, `uuid`, `contextlib`. No new
dependency.

Design: [spec](../specs/2026-09-03-local-external-boot-session-mechanisms-design.md),
[ADR-0591](../../adr/0591-local-external-boot-session-mechanisms-bind-to-the-recovery-root.md).

Expected implementation size: 600–850 changed lines (L) — derived from the file map below: ~240
lines of new production module, ~35 lines in `composition.py`, ~420 lines of new tests, ~60 lines
added to `test_composition.py`.

## Global Constraints

Transcribed from `AGENTS.md` and the spec, values included:

- **Python 3.14**, managed with `uv`. Target architectures **x86_64 and ppc64le**.
- **Ruff line length 100**, lint set `E,F,I,UP,B,SIM`. `ty` runs with strict defaults.
- Guardrails: `just lint`, `just type` (**whole tree, src + tests**), `just test-changed` while
  iterating; `just ci` as the pre-push gate. Run gates **bare** — never piped through
  `tail`/`head`, never with a trailing `; echo $?`. Capture with
  `just ci > <file> 2>&1 < /dev/null`.
- **Prerequisite in a fresh worktree:** `npm ci --prefix .github/scripts/mermaid-check`, or
  `just ci` fails at `check-mermaid` with `ERR_MODULE_NOT_FOUND: jsdom` (exit 123) before ever
  reaching the test suite. `node_modules/` is gitignored, so a worktree never inherits it.
- Before `git commit`: run `just format` for Python-only changes; for Markdown, stage first then
  run `prek run` and re-add exactly the staged paths.
- **Doc-style guard:** use *Milestone*, never "Sprint"; avoid "critical", "robust",
  "comprehensive", "elegant" in prose, ADRs, specs, commit messages and code comments.
- Error messages must carry **no host path, no libvirt/libguestfs text, no guest byte, no secret**.
- Test directories are created with `mkdir()` then `chmod(0o700)` — **never** `mkdir(mode=0o700)`,
  whose mode argument is masked by the umask.
- Conventional Commits 1.0.0, imperative, ≤72-char subject.

## File map

| file | disposition | answerable for |
| --- | --- | --- |
| `src/kdive/providers/local_libvirt/lifecycle/boot/session_mechanisms.py` | **new** | the five mechanisms and their confinement |
| `src/kdive/providers/local_libvirt/composition.py` | modify | resolving settings and assembling the mechanisms |
| `tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py` | **new** | per-mechanism behaviour, confinement, bite proofs |
| `tests/providers/local_libvirt/test_composition.py` | modify | the production caller, and `external_boot is None` |

`session.py` is **not** modified. `external_boot.py` is **not** modified — its helpers are imported.

## Interfaces

Produced by this change and relied on by later tasks and by #2212:

```python
# session_mechanisms.py
class LocalOperationLease:
    system_id: UUID
    binding: ExternalBootActivationBinding
    def release(self) -> None: ...

class LocalOperationLane:
    def pin(self, lease: LocalExternalBootOperationLease) -> PinnedOperationOwnership: ...

class LocalArtifactRoot:
    def __init__(self, recovery_root: Path) -> None: ...
    def open(self, ownership: OperationOwnership) -> int: ...

class LocalPayloadCleanup:
    def __init__(self, recovery_root: Path) -> None: ...
    def cleanup(self, root_fd: int, binding: ExternalBootActivationBinding) -> None: ...

def open_libguestfs_guest() -> _Guest: ...

PAYLOAD_NAMES: tuple[str, ...] = ("kernel", "initrd", "modules")

# composition.py
def build_external_boot_session_mechanisms() -> LocalExternalBootSessionFactory: ...
```

Consumed from existing code, each **confirmed to exist with this signature** on this branch:

```python
# lifecycle/boot/external_boot.py
def _require_private_owned_directory(fd: int, label: str) -> None
def _open_private_directory(parent_fd: int, name: str) -> int
def _open_or_create_private_child(parent_fd: int, name: str) -> int
# lifecycle/boot/readiness.py
def _real_readiness(system_id: UUID) -> ReadinessResult
# lifecycle/boot/session.py
type PinOperationLease  = Callable[[LocalExternalBootOperationLease], PinnedOperationOwnership]
type OpenArtifactRoot   = Callable[[OperationOwnership], int]
type OpenGuest          = Callable[[], _Guest]
type CleanupPayloads    = Callable[[int, ExternalBootActivationBinding], None]
@dataclass(frozen=True) class OperationOwnership:        system_id: UUID; binding: ExternalBootActivationBinding
@dataclass(frozen=True) class PinnedOperationOwnership:  ownership: OperationOwnership; _pin: LocalExternalBootOperationPin
# providers/local_libvirt/settings.py
LIBVIRT_RECOVERY_ROOT: Setting   # parse -> Path, no default; require() by name
LIBVIRT_URI: Setting
```

Re-derive every line number by `grep` against this branch immediately before writing it into code
or a comment; cite stable identities (function names, literal error strings) in preference to line
numbers. The `:1484` / `:1888` / `:1915-1922` citations in issue #2211's body are stale against
every revision in recent history and must not be carried forward.

## Task 1 — the operation lane

**Creates** `session_mechanisms.py` with `LocalOperationLease`, `LocalOperationLane`, and a private
pin. **Tests** `test_session_mechanisms.py::TestOperationLane`.

Steps, one action each:

1. Write `test_pin_refuses_a_foreign_lease`: `LocalOperationLane().pin(object())` raises
   `TypeError` matching `"foreign operation lease"`. Also assert a *structural impostor* — a simple
   object carrying `system_id` and `binding` attributes — is refused, which is what makes the
   typing nominal rather than structural.
2. Run `uv run python -m pytest tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py -q`.
   Expect collection to succeed and the test to FAIL with `ImportError`/`AttributeError` — then
   write the minimum and expect it to fail on the assertion, not the import.
3. Implement `LocalOperationLease` (plain class, `system_id`, `binding`, `_pins = 0`, `_released`)
   and `LocalOperationLane.pin` raising `TypeError("foreign operation lease")` for a
   non-`LocalOperationLease`.
4. Re-run; expect `1 passed`.
5. Write `test_pin_refuses_a_released_lease` → `RuntimeError` matching
   `"operation lease is released"`; implement; re-run.
6. Write `test_release_is_refused_while_a_pin_is_outstanding` → `lease.release()` raises
   `RuntimeError` matching `"operation lease is pinned"`; after `pinned._pin.close()`,
   `lease.release()` succeeds. Implement the pin counter; re-run.
7. Write `test_pin_returns_the_exact_lease_identity`: the returned
   `PinnedOperationOwnership.ownership` equals `OperationOwnership(lease.system_id, lease.binding)`
   **by value**, and `ownership.binding is lease.binding`. Implement; re-run.
8. Write `test_lane_exposes_no_issue_method`: `not hasattr(LocalOperationLane, "issue")` and
   `"issue" not in dir(LocalOperationLane)`. This guards ADR-0591's refusal to let a mechanism mint
   its own lease. Re-run.
9. `just lint` then `just type`, both bare. Expect `All checks passed!` and a clean `ty` run.
10. Commit: `feat(local-libvirt): add the external-boot operation lane`.

**Acceptance.** Foreign and released leases are refused; a structural impostor is refused; release
is blocked while pinned; no `issue()` exists.

## Task 2 — the artifact root

**Modifies** `session_mechanisms.py` (adds `LocalArtifactRoot`). **Tests**
`test_session_mechanisms.py::TestArtifactRoot`.

A shared fixture builds a valid root:

```python
@pytest.fixture
def recovery_root(tmp_path: Path) -> Path:
    root = tmp_path / "recovery"
    root.mkdir()          # never mkdir(mode=...) — the umask masks it
    root.chmod(0o700)
    return root
```

Steps:

1. Write `test_open_creates_and_returns_the_system_run_directory`: `LocalArtifactRoot(root).open(
   OperationOwnership(SYSTEM_ID, BINDING))` returns a descriptor whose `os.fstat` `st_dev`/`st_ino`
   equal those of `root / str(SYSTEM_ID) / BINDING.run_id`, and both created directories are mode
   0700. Close the descriptor in the test.
2. Run the file; expect FAIL on the missing attribute, then on the assertion once the stub exists.
3. Implement `LocalArtifactRoot.__init__` (stores the `Path`) and `.open` as the three-step walk:
   `os.open(self._root, O_RDONLY|O_DIRECTORY|O_NOFOLLOW)` →
   `_require_private_owned_directory(fd, "artifact root")` →
   `_open_or_create_private_child(root_fd, str(ownership.system_id))` →
   `_open_or_create_private_child(system_fd, ownership.binding.run_id)`, closing each intermediate
   in a `finally`.
4. Re-run; expect `1 passed`.
5. Write `test_open_refuses_a_symlinked_component`, parametrized over the `system_id` and `run_id`
   components: pre-create the component as a symlink to a valid 0700 directory elsewhere; expect
   `OSError` with `errno.ELOOP`. Implement nothing — `O_NOFOLLOW` in `_open_private_directory`
   already does it — and confirm the test passes for that reason by asserting the `errno`, not just
   that something raised.
6. Write `test_open_refuses_a_wide_mode_component` (chmod 0o755) and
   `test_open_refuses_a_non_owner_root`. The mode case expects `ValueError` matching
   `"owner-only service-owned directory"`. The owner case is skipped unless the test can find a
   directory owned by another uid — mark it `pytest.mark.skipif` on `os.geteuid() != 0` rather than
   faking `os.fstat`, so it never passes vacuously.
7. Write `test_open_refuses_a_root_that_is_not_a_directory` and
   `test_open_refuses_a_missing_root` → `NotADirectoryError` / `FileNotFoundError`.
8. Write `test_open_leaks_no_descriptor_on_failure`: capture `len(os.listdir("/proc/self/fd"))`
   before and after a failing `open`, asserting equality. This is the test most likely to be
   written so it cannot fail — verify its bite by deliberately dropping the `finally` close and
   observing it go red.
9. Write `test_open_refuses_a_non_canonical_component`: construct an `OperationOwnership` whose
   binding carries a `run_id` of `"../escape"`, and expect `ValueError`. Since
   `ExternalBootActivationBinding` validates `CanonicalUuid`, build the impostor with
   `dataclasses.replace` on a plain object or `model_construct`, so the mechanism's own guard is
   what is exercised rather than pydantic's. Implement the explicit canonical-UUID check.
10. `just lint`; `just type`; commit `feat(local-libvirt): confine the external-boot artifact root`.

**Acceptance.** Every component is re-validated; symlink, mode, owner, missing and non-canonical
cases each refuse; no descriptor leaks; the message names no host path.

## Task 3 — payload cleanup, and the archive it must remove

**Modifies** `session_mechanisms.py` (adds `PAYLOAD_NAMES`, `LocalPayloadCleanup`). **Tests**
`test_session_mechanisms.py::TestPayloadCleanup`.

This task carries the change's bite proof, so its order matters: the descriptor-scoped behaviour is
implemented and proven insufficient **before** the archive removal is added.

Steps:

1. Write `test_cleanup_removes_only_the_payload_names_under_the_descriptor`: create `kernel`,
   `initrd`, `modules` and a foreign `keep-me` under the artifact directory; after cleanup only
   `keep-me` remains.
2. Run; expect FAIL. Implement the first half — unlink each of `PAYLOAD_NAMES` with
   `dir_fd=root_fd`, `FileNotFoundError` treated as success. Re-run; expect `1 passed`.
3. Write `test_cleanup_is_idempotent`: run twice; the second run does not raise, and
   `sorted(os.listdir(...))` is identical before and after the second run.
4. **Write the reachability proof, and watch it fail.**
   `test_finalize_tombstone_succeeds_after_cleanup_of_an_archived_activation` drives a real
   `RecoveryMetadataStore`: `publish_pre_stop(intent)` → `recovery_archive_sink(...)` →
   `sink.publish(io.BytesIO(b"..."))` (this really writes `modules.tar`) → build a
   `ModuleArchiveCapture` from the returned digest/size → `complete_preparation` →
   `record_phase(..., "recovered")` → `LocalPayloadCleanup(root).cleanup(artifact_fd, BINDING)` →
   `publish_tombstone(...)` → `finalize_tombstone(...)`, asserting it returns without raising and
   that the recovery directory no longer exists.
5. Run it against the **current** descriptor-scoped implementation. Expect it to FAIL with
   `ValueError: cleanup tombstone directory contains unexpected payload`. Record that exact output
   — it is the demonstrated defect, and this step is the one that must not be skipped.
6. Implement the second half of `cleanup`: `_open_private_directory(root_fd_of_recovery_root,
   f"{binding.system_id}.{binding.activation_id}")`, then `os.unlink("modules.tar",
   dir_fd=recovery_fd)`, with `FileNotFoundError` on either the directory or the unlink treated as
   success, and the descriptor closed in a `finally`.
7. Re-run step 4's test; expect `1 passed`. Re-run step 3's idempotence test; expect it still
   passes, now covering both removals.
8. Write `test_cleanup_leaves_foreign_files_in_the_recovery_directory`: place `foreign.json` beside
   `modules.tar`; after cleanup `modules.tar` is gone and `foreign.json` remains. This is what
   bounds the deletion.
9. Write `test_cleanup_refuses_a_symlinked_recovery_directory` → `OSError` with `errno.ELOOP`,
   asserting the `errno`.
10. Write `test_cleanup_refuses_a_non_canonical_binding` for a binding whose `activation_id` is not
    a canonical UUID → `ValueError`, before any unlink happens (assert the payloads are still
    present after the refusal).
11. `just lint`; `just type`; commit
    `feat(local-libvirt): remove the activation recovery archive on cleanup`.

**Acceptance.** Both removals happen; both are by exact name; both treat absence as success;
foreign files survive; a symlinked or non-canonical recovery directory is refused; and step 5's
recorded failure output demonstrates why the second removal exists.

**Rollback.** None needed — the mechanism creates nothing durable.

## Task 4 — the guest opener, and the production caller

**Modifies** `session_mechanisms.py` (adds `open_libguestfs_guest`) and `composition.py` (adds
`build_external_boot_session_mechanisms`). **Tests** `test_session_mechanisms.py::TestOpenGuest`
and additions to `test_composition.py`.

Steps:

1. Implement `open_libguestfs_guest` following the pattern already used three times in this package
   (`retrieve/guestfs.py`, `lifecycle/rootfs/baseline_kernel.py`,
   `lifecycle/boot/guest_kernel_writer.py`): a function-local
   `import guestfs  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided`, then
   `cast("_Guest", guestfs.GuestFS(python_return_dict=True))`. It attaches no drive, launches
   nothing and mounts nothing — `_ConcreteSession._open_guest_context` owns all of that after
   `require_inactive()`.
2. Write `test_open_guest_attaches_nothing`, monkeypatching a fake `guestfs` module into
   `sys.modules` and asserting the returned handle recorded **no** `add_drive_opts`, `launch`,
   `inspect_os` or `mount` call.
3. Write `test_session_refuses_guest_access_while_the_domain_is_active` in
   `test_session_mechanisms.py`, building a factory with the existing `test_session.py` helpers and
   an active `Domain`, asserting `RuntimeError` matching `"domain must be inactive"` — proving the
   refusal comes from the existing `require_inactive` path and not from a new check in the opener.
4. Implement `build_external_boot_session_mechanisms()` in `composition.py`: resolve
   `config.require(LIBVIRT_RECOVERY_ROOT)`, construct `LocalOperationLane()`,
   `LocalArtifactRoot(root)`, `LocalPayloadCleanup(root)`, and call the existing
   `build_external_boot_session_factory(pin_lease=..., open_artifact_root=...,
   open_guest=open_libguestfs_guest, readiness=_real_readiness, cleanup_payloads=...)`.
   **Do not pass `observe_running`.** `build_external_boot_session_factory` currently declares
   `observe_running` as a required keyword — change that one parameter to
   `RunningObserver | None = None` and forward it, so the factory's own
   `or _unconfigured_observation` fallback selects the default. That is the only edit to the
   existing builder.
5. Add `test_build_external_boot_session_mechanisms_opens_nothing`: monkeypatch
   `composition.config.require` and `composition.libvirt.open` exactly as
   `test_external_boot_session_factory_builder_is_lazy_and_unadvertised` already does, assert the
   returned object is a `LocalExternalBootSessionFactory`, and assert the recording list is `[]` —
   no descriptor, connection or guest opened at build time.
6. Add `test_mechanisms_builder_does_not_advertise_external_boot`: after building the mechanisms,
   `composition.build_runtime(secret_registry=SecretRegistry()).external_boot is None`.
7. Add `test_observe_running_is_left_unconfigured`: build the factory through the production
   builder with a stub recovery root, open a session with the existing doubles, and assert
   `session.observe_running()` raises `RuntimeError` matching
   `"local external-boot running observation is not configured"`. This is the deferral's guard.
8. `just lint`; `just type`; `just test-changed`. Commit
   `feat(local-libvirt): build the external-boot session mechanisms`.

**Acceptance.** The builder opens nothing; `external_boot` stays `None`; `observe_running` raises
its unconfigured error; guest access is refused while active through the existing path.

## Task 5 — fail-closed defaults, and the bite audit

**Modifies** `test_session_mechanisms.py` only. No production change.

Steps:

1. Write three independent tests — `test_unconfigured_readiness_raises`,
   `test_unconfigured_observation_raises`, `test_unconfigured_cleanup_raises` — each building a
   factory omitting exactly that one mechanism and asserting the specific message from
   `_unconfigured_readiness` / `_unconfigured_observation` / `_unconfigured_cleanup`. Match the
   message, not just `RuntimeError`, so a different `RuntimeError` cannot satisfy it.
2. Write `test_factory_defaults_are_the_unconfigured_functions`: build a factory with all three
   omitted and assert `factory._readiness is _unconfigured_readiness`,
   `factory._observe_running is _unconfigured_observation`,
   `factory._cleanup_payloads is _unconfigured_cleanup` — identity, so a permissive replacement
   fails even if it raises something.
3. Run `just test-changed`; expect all green.
4. **Bite audit over every test added in Tasks 1–5.** For each: with the implementation committed,
   inject one controlled fault in the production code, run only that test, and require a **clean
   assertion failure** — not a collection error, not an `ImportError`, not a connection error —
   then revert and confirm `sha256sum` of the reverted file matches the committed one. Record each
   pair. Any test that stays green under its fault is not a test; fix it or delete it.
5. Commit `test(local-libvirt): prove the session-mechanism defaults fail closed`.

**Acceptance.** Each of the three defaults is independently proven reachable; identity is asserted;
every new test has a recorded fault-injection pair.

## Task 6 — guardrails

1. `npm ci --prefix .github/scripts/mermaid-check` if this worktree has not run it.
2. `just ci > /tmp/<scratch>/ci-final.log 2>&1 < /dev/null` — **bare**, no pipe, no
   `; echo $?`. Expect exit 0.
3. If red, read the log and fix the cause; do not re-run hoping.
4. Commit any formatting the hooks rewrote.

## Self-review against the spec

Each spec requirement maps to a task: lane → Task 1; artifact root and its confinement → Task 2;
cleanup, its bound and the reachability proof → Task 3; guest opener, production caller, and the
`observe_running` deferral guard → Task 4; fail-closed defaults and the bite audit → Task 5;
guardrails → Task 6. The threat model's four boundary controls map to Task 2 (artifact-root walk),
Task 3 (recovery-dir open, payload deletion, archive deletion). No task serves a requirement the
spec does not state, and no spec requirement lacks a task.

Names used across tasks are consistent: `PAYLOAD_NAMES`, `LocalOperationLane.pin`,
`LocalArtifactRoot.open`, `LocalPayloadCleanup.cleanup`, `open_libguestfs_guest`,
`build_external_boot_session_mechanisms` appear with the same spelling in the Interfaces block and
in every task that uses them.

## Deferrals carried into this change

- **Render the qemu-guest-agent channel into local domain XML
  (`local_libvirt/lifecycle/xml.py`) and bind `observe_running` on it — owner #2212.** The guest
  half is already done (`guest_base_image` enables `qemu-guest-agent.service` on both build paths);
  only the host-side channel is missing. Adding it does not retrofit already-provisioned domains,
  so #2212 must choose redefine-on-next-boot or an explicit migration.
- **Bind `LocalOperationLane` behind the per-System Postgres advisory lock — owner #2212.**
  ADR-0587 assigns lease issuance to the serialization-lane context; this change supplies the lane
  object and its invariants only.
