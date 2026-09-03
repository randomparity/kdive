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

Expected implementation size: 800–1050 changed lines (L) — derived from the file map and task list
below: ~290 lines of new production module (five mechanisms, the canonical-UUID guard, the
`ValueError`-wrapping walk, and the readiness redactor), ~45 lines in `composition.py` (the builder
plus the `LocalExternalBootMechanisms` result), ~570 lines of new tests across Tasks 1–5, and ~65
lines added to `test_composition.py`. Revised twice: 600–850 in the first revision, 700–950 after
round 1 added five tests, and 800–1050 after round 2 added Task 2a's readiness redactor with its
three tests, Task 3's blocked-while-active companion, and the provenance-based rewrite of the
criterion-8 test.

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
def redacted_readiness(system_id: UUID) -> ReadinessResult: ...

PAYLOAD_NAMES: tuple[str, ...] = ("kernel", "initrd", "modules")
READINESS_PROBE_FAILURES: frozenset[str]   # the closed set redacted_readiness may report

# composition.py
@dataclass(frozen=True, slots=True)
class LocalExternalBootMechanisms:
    factory: LocalExternalBootSessionFactory
    recovery_root: Path        # the SAME value LocalPayloadCleanup holds; #2212 passes this
                               # to RecoveryMetadataStore rather than re-resolving the setting

def build_external_boot_session_mechanisms() -> LocalExternalBootMechanisms: ...
```

Modified in place (the only edit to existing code outside the new module):

```python
# composition.py — observe_running widens to `| None` and STAYS REQUIRED (no default)
def build_external_boot_session_factory(
    *,
    pin_lease: PinOperationLease,
    open_artifact_root: OpenArtifactRoot,
    open_guest: OpenGuest,
    readiness: ReadinessProbe,
    observe_running: RunningObserver | None,   # was: RunningObserver
    cleanup_payloads: CleanupPayloads,
) -> LocalExternalBootSessionFactory: ...
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
    root.mkdir()
    root.chmod(0o700)     # two-step, never mkdir(mode=...) — see the note below
    return root
```

**The umask rule, stated accurately.** `mkdir(mode=...)` masks its argument with the process
umask, but a mode of `0o700` carries only owner bits and no realistic umask clears those, so the
one-step form would in fact produce `0o700` here. Measured on this host (Python 3.14.7, Linux
7.1.12-200.fc44, x86_64): `mkdir(mode=0o700)` yields `0o700` under umask `0o022`, `0o077` and
`0o007` alike. The two-step form is used anyway because it is the repository's convention
(`test_composition.py` states it) and because it *is* load-bearing the moment a directory needs
group or other bits — the same measurement shows `mkdir(mode=0o711)` yielding `0o700` under umask
`0o077`, and `mkdir(mode=0o755)` yielding `0o750` under umask `0o007`. Any parent directory these
tests create with `0o711` must therefore use the two-step form or it will silently be `0o700` on a
developer machine with a restrictive umask. Do not write a code comment claiming the `0o700` call
itself is masked; that claim is false and would be a fresh instance of the defect it warns about.

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
   `NotADirectoryError` with `errno.ENOTDIR`.

   **Measured, not assumed** (Python 3.14.7, Linux 7.1.12-200.fc44, x86_64): opening a
   symlink-to-directory with `O_RDONLY|O_DIRECTORY|O_NOFOLLOW` raises `NotADirectoryError`
   (`errno` 20, `ENOTDIR`) — **not** `ELOOP`. On Linux `O_DIRECTORY` reports `ENOTDIR` for the
   symlink before the `O_NOFOLLOW` `ELOOP` path is reached, and the same holds for the
   `dir_fd`-relative form `_open_private_directory` actually uses. Asserting `ELOOP` here would
   make the test fail against correct code.

   Because `ENOTDIR` is also what a regular-file component raises, the errno alone does not prove
   the symlink was refused *for being a symlink*. Pin that separately in the same test: assert
   that the identical open **without** `O_NOFOLLOW` succeeds. Measured on the same host, it does —
   which is what makes `O_NOFOLLOW` load-bearing rather than decorative, and is the assertion that
   would go red if someone dropped the flag from `_open_private_directory`.
6. Write `test_open_refuses_a_wide_mode_component` (chmod 0o755), expecting `ValueError` matching
   `"owner-only service-owned directory"`.

   Do **not** write a non-owner-root test that an ordinary process cannot set up. An unprivileged
   test cannot create a directory owned by another uid, and faking `os.fstat` would assert against
   the mock rather than against `_require_private_owned_directory`'s euid check. The euid case is
   therefore **not claimed as covered** — the spec says so explicitly, and no test may imply
   otherwise by skipping. A `pytest.mark.skipif(os.geteuid() != 0)` test is worse than none here:
   it never runs in CI while reading, in the file, as though the case were covered.
6a. Write `test_open_is_confined_to_its_constructed_root`. The obvious version of this test **cannot
   fail** and must not be written: asserting that a `LocalArtifactRoot` constructed on root A opens
   under A rather than under an unrelated root B is true of every implementation consistent with the
   constructor, because nothing in the call path can name B. Asserting it would repeat, one level
   up, the tautological-gate error ADR-0591 rejects `recovery_directory_name` for.

   Write the version with a failing input instead: construct on A, create A's `<system_id>/<run_id>`
   tree **and** an identical tree under B, then `chmod` A's root to `0o755`. Assert (i) the
   resolution raises `ValueError`, and (ii) B's tree is untouched — no descriptor opened under it,
   verified by comparing B's subtree `st_atime`/contents before and after, or by asserting the
   raised error rather than a successful open. An implementation that fell back to a second root,
   or that resolved the setting a second time at call time instead of using the constructed value,
   goes red here; the current design goes green. That is charter criterion 4's confinement case with
   an input that can actually distinguish the two.
7. Write `test_open_refuses_a_root_that_is_not_a_directory` and
   `test_open_refuses_a_missing_root` → `NotADirectoryError` / `FileNotFoundError`.
8. Write `test_open_leaks_no_descriptor_on_failure`: capture `len(os.listdir("/proc/self/fd"))`
   before and after a failing `open`, asserting equality. This is the test most likely to be
   written so it cannot fail — verify its bite by deliberately dropping the `finally` close and
   observing it go red.
9. Write `test_open_refuses_a_non_canonical_component`: construct an `OperationOwnership` whose
   binding carries a `run_id` of `"../escape"`, and expect `ValueError`. Build the impostor binding
   with `ExternalBootActivationBinding.model_construct(...)`, which bypasses validation — **not**
   `dataclasses.replace`, which raises `TypeError` here: `ExternalBootActivationBinding` is a
   pydantic `_ClosedValue`, not a dataclass. (`OperationOwnership` *is* a frozen dataclass, which is
   where that confusion comes from — `replace` works on the ownership, never on the binding.)
   Implement the explicit canonical-UUID check so the mechanism's own guard is exercised rather
   than pydantic's.
10. `just lint`; `just type`; commit `feat(local-libvirt): confine the external-boot artifact root`.

**Acceptance.** Every component is re-validated; symlink, mode, owner, missing and non-canonical
cases each refuse; no descriptor leaks; the message names no host path.

## Task 2a — the redacting readiness wrapper

**Modifies** `session_mechanisms.py` (adds `redacted_readiness` and `READINESS_PROBE_FAILURES`).
**Tests** `test_session_mechanisms.py::TestRedactedReadiness`. Nothing in `readiness.py` or
`install.py` changes.

1. Write `test_redacted_readiness_drops_libvirt_text_and_host_paths`: stub the wrapped probe to
   return `ReadinessResult(answered=False, ok=False, probe_error="error: Failed to connect socket "
   "to '/run/libvirt/libvirt-sock': No such file or directory")`, and assert the wrapper's result
   has the same `answered` and `ok` but a `probe_error` that (i) is one of the closed set, (ii)
   contains no `/`, and (iii) contains neither `libvirt` nor `socket`. Assert on the *absence* of
   the substrings, not merely that the value changed — a wrapper that returned a different leaky
   string would otherwise pass.
2. Run the file; expect FAIL on the missing name, then on the assertion once the stub exists.
3. Implement `redacted_readiness(system_id)`: call `_real_readiness(system_id)`, return it unchanged
   when `probe_error is None`, otherwise `result._replace(probe_error=<classification>)`.
   `ReadinessResult` is a `NamedTuple`, so `_replace` is its documented API. The classification is
   drawn from a module-level closed set — probe timed out, probe tool absent, hypervisor
   unreachable, probe exited non-zero — chosen by matching the *shape* the probe already
   distinguishes, never by substring-matching guest or hypervisor text into the message.
4. Re-run; expect `1 passed`.
5. Write `test_redacted_readiness_passes_through_answered_and_ok`: for
   `ReadinessResult(True, True, None)` the wrapper returns an equal value, so no readiness semantics
   change. Re-run.
6. Write `test_redacted_readiness_classification_is_closed`: every value the wrapper can produce for
   `probe_error` is in `READINESS_PROBE_FAILURES`, asserted by driving each branch. This is what
   stops a later edit reintroducing interpolated text.
7. `just lint`; `just type`; commit
   `feat(local-libvirt): redact the external-boot readiness probe error`.

**Acceptance.** `answered`/`ok` are untouched; `probe_error` is always a closed-set value;
`readiness.py` and `install.py` are unmodified, so the existing boot-failure diagnostics keep their
full text.

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
   `test_finalize_tombstone_succeeds_after_cleanup_of_an_archived_activation` drives a **real**
   `RecoveryMetadataStore` — never a stub, because a stubbed store is exactly the vacuous form this
   proof exists to avoid. Every operand it needs, with the real signatures:

   **Drive `_ConcreteSession.cleanup_payloads`, not `LocalPayloadCleanup.cleanup` directly.**
   Calling the mechanism directly bypasses the session's `require_inactive()` gate, so the proof
   would go green whether or not that gate blocks the real path — the "passes because the thing it
   exercises is a no-op" shape this plan exists to avoid. Build a session with the `test_session.py`
   doubles, bind `cleanup_payloads=LocalPayloadCleanup(root).cleanup`, and call
   `session.cleanup_payloads()`.

   **Use `prior_power="inactive"` for this proof.** The helper's default is `"running"`, and for
   that value `restore_power` leaves the domain active, so `require_inactive()` raises before any
   deletion and finalization is unreachable for a reason above this change (Task 3 step 4a records
   that case separately). A proof built on the default would fail for the wrong reason and invite
   "fixing" it by bypassing the gate.

   - `reference = store.publish_pre_stop(intent)` where `intent` is a `LocalPreStopIntentV1`.
     Reuse the helpers in `tests/providers/local_libvirt/test_external_boot.py` — note the path:
     that file is one directory **above** `test_session.py`, not beside it. `_metadata(...)` is at
     line 557, `_pre_stop(metadata: LocalRecoveryMetadataV1) -> LocalPreStopIntentV1` at 685 (it
     takes the metadata, it is not nullary), and `_point(metadata)` at 1376. Re-derive all three
     line numbers before writing them into a comment. `_metadata()` defaults
     `capture={"state": "absent"}` and `prior_power="running"`, which is why no existing test
     reaches this path and why both must be overridden here.
   - `sink = store.recovery_archive_sink(reference, intent)`, then
     `archive_sha256, archive_bytes = sink.publish(io.BytesIO(payload))` — this is what really
     writes `modules.tar`.
   - `capture = ModuleArchiveCapture(manifest=..., entry_count=0, uncompressed_bytes=0,
     archive_sha256=archive_sha256, archive_bytes=archive_bytes)`, then
     `metadata = template.model_copy(update={"capture": capture})`.
   - `store.complete_preparation(reference, intent, metadata)` — enforces
     `_metadata_extends_intent`, so `metadata` must keep every shared field of `intent`.
   - `recovered = store.record_phase(reference, binding, expected, "recovered")` — re-reads and
     compares the exact prior metadata, so pass the metadata `complete_preparation` returned.
   - `LocalPayloadCleanup(root).cleanup(artifact_fd, BINDING)`.
   - `store.publish_tombstone(reference, binding, recovered, point_digest)` — additionally requires
     `recovered.phase == "recovered"`, which is why the `record_phase` step above is not optional.
   - `store.finalize_tombstone(reference, recovery, proof)` — needs a `RecoveryPoint` and a
     `FinalizeCleanupProof` whose `binding` matches and whose `point_digest` equals
     `LocalLibvirtExternalBoot.point_digest(recovery)`. `test_external_boot.py`'s
     `_point(metadata)` helper builds the point; the proof also needs `operation_id`, `attempt_id`,
     `journal_sequence` and `journal_digest`.

   Assert `finalize_tombstone` returns without raising and that the recovery directory no longer
   exists.
4a. Write `test_cleanup_is_blocked_while_the_domain_is_active`, the companion that makes the
   limitation visible in the suite instead of only in the ADR. Same setup with
   `prior_power="running"` and an **active** domain double; assert `session.cleanup_payloads()`
   raises `RuntimeError` matching `"domain must be inactive"`, that the payloads are still present,
   and that no tombstone was written. This documents that `finalize_tombstone` stays unreachable for
   a restored-to-running System for a reason one layer above this change, and it goes red if anyone
   later narrows that gate without revisiting the ADR's reachability claim.
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
9. Write `test_cleanup_refuses_a_symlinked_recovery_directory` → `NotADirectoryError` with
   `errno.ENOTDIR`, for the reason measured in Task 2 step 5 — **not** `ELOOP`. Assert also that
   the payloads under `root_fd` were still removed first, so the refusal is scoped to the second
   removal and does not silently skip the first.
10. Write `test_cleanup_refuses_a_non_canonical_binding` for a binding whose `activation_id` is not
    a canonical UUID (built with `model_construct`, per Task 2 step 9) → `ValueError`, before any
    unlink happens (assert the payloads are still present after the refusal).
11. Write `test_payload_names_match_the_target_projection_filenames`. The set must be **read**, not
    restated, and `_artifact_ref_parts` cannot supply it: it holds `{"kernel", "modules", "initrd"}`
    as an inline literal inside a boolean expression, with no module-level constant and no accessor,
    so a test can only restate it or probe it with candidates it has already restated. Either way
    the test would restate the thing it exists to check.

    Read `TargetProjectionV1`'s annotations instead, which are genuinely introspectable:

    ```python
    from typing import get_args
    fields = TargetProjectionV1.model_fields
    expected = {
        get_args(fields["kernel_filename"].annotation)[0],
        get_args(fields["modules_filename"].annotation)[0],
        # initrd_filename is `Literal["initrd"] | None`
        get_args(get_args(fields["initrd_filename"].annotation)[0])[0],
    }
    assert set(PAYLOAD_NAMES) == expected
    ```

    Confirm the exact unwrapping against the branch before relying on it — the `| None` arm makes
    the nesting easy to get wrong, and a `get_args` expression that silently yields an empty set
    would make this pass vacuously. Because cleanup treats a missing name as success, a projection
    that renames or adds an artifact would otherwise make cleanup skip it silently; this is what
    turns that into a red.
12. `just lint`; `just type`; commit
    `feat(local-libvirt): remove the activation recovery archive on cleanup`.

**Acceptance.** Both removals happen; both are by exact name; both treat absence as success;
foreign files survive; a symlinked or non-canonical recovery directory is refused; and step 5's
recorded failure output demonstrates why the second removal exists.

**Rollback.** `LocalPayloadCleanup` itself creates nothing. The change as a whole does:
`LocalArtifactRoot.open` creates `<system_id>` and `<run_id>` directories, mode 0700, under the
per-slot recovery root, and nothing in `lifecycle/boot/` removes them — `finalize_tombstone` rmdirs
only `<system>.<activation>`. Reverting the code leaves any such directories in place; they are
empty once cleanup has run, and removing them is an operator action. Reclamation has no owner
today and is reported for routing rather than invented here.

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
4. Implement `build_external_boot_session_mechanisms()` in `composition.py`. Resolve
   `config.require(LIBVIRT_RECOVERY_ROOT)` **exactly once** into a local `root`, construct
   `LocalOperationLane()`, `LocalArtifactRoot(root)` and `LocalPayloadCleanup(root)` from that one
   value, call `build_external_boot_session_factory(...)`, and return
   `LocalExternalBootMechanisms(factory=factory, recovery_root=root)`.

   Returning the root is not decoration: it is what stops cleanup's second removal and
   `RecoveryMetadataStore` from ever resolving different roots. #2212 passes
   `mechanisms.recovery_root` to the store rather than re-resolving the setting. Do not resolve the
   setting in more than one place in this function.

   Pass `observe_running=None` **explicitly**. `build_external_boot_session_factory` currently
   declares `observe_running: RunningObserver` as a required keyword with no default; widen that
   one parameter's type to `RunningObserver | None` and **do not give it a default**. The factory's
   own `or _unconfigured_observation` fallback then selects the default. Giving it a default would
   make every mechanism omittable by any caller, so a caller that forgot `readiness` or
   `cleanup_payloads` would get a factory that looks built and fails only mid-operation; keeping it
   required preserves the build-time failure for everyone and makes this one omission explicit at
   the call site.
5. Add `test_build_external_boot_session_mechanisms_opens_nothing`. Monkeypatch
   `composition.config.require` **per setting**, not with a blanket lambda:

   ```python
   def _require(setting: object) -> object:
       if setting is composition.LIBVIRT_RECOVERY_ROOT:
           return recovery_root          # a real mode-0700 tmp_path directory
       if setting is composition.LIBVIRT_URI:
           return "qemu:///session"
       raise AssertionError(f"unexpected setting: {setting}")
   ```

   The existing `test_external_boot_session_factory_builder_is_lazy_and_unadvertised` patches
   `require` with `lambda _setting: "qemu:///session"` for *every* setting. Copying that idiom here
   would construct `LocalArtifactRoot` and `LocalPayloadCleanup` with the string `"qemu:///session"`
   as their recovery root; the test would still pass, because nothing opens it, and would still
   type-check, because the patched callable is untyped — asserting laziness while proving nothing
   about the value actually resolved. Assert the returned object is a `LocalExternalBootMechanisms`,
   that `mechanisms.recovery_root == recovery_root`, and that the recording list is `[]`.
5a. Add `test_mechanisms_share_one_recovery_root`: assert the `recovery_root` the builder returns is
   the same value the cleanup mechanism holds, so the single-source property is checked rather than
   described. This is the only in-change control on the #2212 divergence, and it must be a real
   assertion on the built object, not a comment.
5b. Add `test_production_builder_binds_readiness_and_open_guest`: assert by identity that the built
   factory's `_readiness is redacted_readiness` and `_open_guest is open_libguestfs_guest`. Without
   this, dropping either argument leaves the class falling back to `_unconfigured_readiness` and
   nothing goes red.
5c. Add `test_configuration_comes_only_from_the_composition_seam` (charter criterion 8).

   **Do not test this with `inspect.signature`.** `LocalArtifactRoot` and `LocalPayloadCleanup`
   *do* take a filesystem path as a call argument — `recovery_root: Path` — so an assertion that
   "no parameter accepts a path" would have to fail on the very constructors it inspects, and the
   shape-based version passes precisely *because* the path parameter is there. Signature shape
   reports arity, never provenance: it cannot tell "the composition seam supplied this `Path`" from
   "a protocol handler supplied it". Criterion 8 is about provenance.

   Test the provenance directly, which is what actually controls it:
   - assert `build_external_boot_session_mechanisms` is the **only** construction site of
     `LocalArtifactRoot` and `LocalPayloadCleanup` in `src/` — a source scan over `src/` asserting
     no other module names them;
   - assert the value it constructs them with came from `config.require(LIBVIRT_RECOVERY_ROOT)`, by
     monkeypatching `composition.config.require` per setting (as in step 5) with a sentinel path and
     asserting the built mechanisms carry that sentinel;
   - assert the builder takes **no parameters at all**, so no caller can inject configuration into
     it.

   Together those say what the criterion asks: the only path into these mechanisms is the setting,
   resolved at one site the test names.
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

   **Do not use `test_session.py`'s `_factory` helper for these.** It accepts only `events`,
   `domain` and `pin_lease`, so it omits `readiness`, `observe_running` and `cleanup_payloads`
   *together* and cannot express "omit exactly one". Construct `LocalExternalBootSessionFactory`
   directly, supplying the other two mechanisms explicitly, so each test isolates the default it
   names. Reuse `_factory` freely for the tests that do not turn on which mechanisms are bound.
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

## Criterion-to-step mapping

Every charter criterion, against the step that discharges it. A prose claim that "each requirement
maps to a task" is not checkable and the previous revision of this plan made that claim while three
requirements were unmapped; this table is checkable line by line instead.

| criterion | discharged by |
| --- | --- |
| 1 — five aliases implemented, passed to the builder, each with a test | Tasks 1–4; per-alias tests: lane T1.1–T1.8, artifact root T2.1–T2.9, cleanup T3.1–T3.11, guest opener T4.2–T4.3, **readiness T4.5b** |
| 1 — `RunningObserver` not bound, keeps `_unconfigured_observation` | T4.7, T5.1 (`test_unconfigured_observation_raises`) |
| 2 — three fail-closed defaults, independently, plus identity | T5.1, T5.2 |
| 3 — `pin_lease` refuses foreign/released, returns exact identity | T1.1, T1.5, T1.7; the `ExpectedOperationOwnership` comparison itself is the factory's, already tested in `test_session.py` |
| 4 — artifact root confined; foreign root, symlink, traversal refused | T2.5 (symlink, both components), **T2.6a (foreign root)**, T2.6 (mode), T2.7 (non-directory/missing), T2.9 (non-canonical) |
| 5 — cleanup removes payloads **and** the archive; bounded; idempotent | T3.1, T3.6, T3.7, T3.8, T3.9, T3.10 |
| 6 — `open_guest` returns `_Guest`; guest access refused while active via the existing path | T4.2, T4.3 |
| 7 — `readiness` returns `ReadinessResult` from a real read; failure carries no libvirt text or host path | **T2a** (`redacted_readiness` replaces `probe_error` with a closed-set classification); binding asserted at **T4.5b** |
| 8 — no mechanism takes configuration from protocol input | **T4.5c** |
| 9 — `ProviderRuntime.external_boot` still `None` | T4.6 |
| 10 — guardrails pass | Task 6 |
| 11 — bite proof: `finalize_tombstone` fails descriptor-scoped, passes fixed | T3.4 (operands named), T3.5 (**record the failure**), T3.7 |

Three steps are bold because they did not exist in the first revision of this plan: the readiness
binding assertion, the foreign-root test, and the constructor-shape test for criterion 8. The
single-root control (T4.5a) and the payload-name coupling (T3.11) are likewise new.

Names are consistent across the Interfaces block and every task that uses them: `PAYLOAD_NAMES`,
`LocalOperationLane.pin`, `LocalArtifactRoot.open`, `LocalPayloadCleanup.cleanup`,
`open_libguestfs_guest`, `build_external_boot_session_mechanisms`, `LocalExternalBootMechanisms` —
all public, matching the spec, since #2212 is a named consumer of several of them.

## Deferrals carried into this change

- **Render the qemu-guest-agent channel into local domain XML
  (`local_libvirt/lifecycle/xml.py`) and bind `observe_running` on it — owner #2212.** The guest
  half is already done (`guest_base_image` enables `qemu-guest-agent.service` on both build paths);
  only the host-side channel is missing. Adding it does not retrofit already-provisioned domains,
  so #2212 must choose redefine-on-next-boot or an explicit migration.
- **Bind `LocalOperationLane` behind the per-System Postgres advisory lock — owner #2212.**
  ADR-0587 assigns lease issuance to the serialization-lane context; this change supplies the lane
  object and its invariants only.
- **Pass `LocalExternalBootMechanisms.recovery_root` to `RecoveryMetadataStore` — owner #2212.**
  Cleanup's archive removal and the store must resolve one root. The builder now returns the
  resolved value so a mismatch requires discarding it rather than merely forgetting an invariant,
  but #2212 could still re-resolve the setting itself. This is the residual, and it is the reason
  the value is returned at all.
- **`_real_readiness` leaks raw libvirt stderr, including host paths, to its other caller — owner:
  routing pending.** `_bounded_probe_error` truncates to 200 characters and redacts nothing, so an
  unreachable hypervisor puts `error: Failed to connect socket to '<host path>': ...` into
  `probe_error`, which `LocalLibvirtInstall` places in a boot-failure `details` payload. This change
  wraps the probe for the external-boot path only (Task 2a) and modifies neither `readiness.py` nor
  `install.py`, so the pre-existing exposure on the install path is untouched and is reported rather
  than inherited silently. Deciding it means choosing between operator diagnostics and redaction for
  a consumer outside this surface.
- **Narrow or justify `_ConcreteSession.cleanup_payloads`'s `require_inactive()` gate — owner:
  routing pending.** The gate blocks cleanup for any System restored to running, so
  `finalize_tombstone` stays unreachable for `prior_power == "running"` regardless of how
  `CleanupPayloads` is bound. The fence's own message is about overlay mutation, while
  `cleanup_payloads` removes only host-side files, so it reads as wider than its justification —
  but deciding that means editing `session.py`, declared unmodified here.
- **Reclaim the `<system_id>` and `<run_id>` artifact directories — owner #2212.**
  `LocalArtifactRoot.open` creates them under the per-slot recovery root and nothing removes them;
  `finalize_tombstone` rmdirs only `<system>.<activation>`.
- **The interrupted-prepare `.partial` archive residue — owner #2212; converges with
  #2207.** `RecoveryArchiveSink.publish` writes `modules.tar` into
  `.{system}.{activation}.partial`, and only `complete_preparation` renames it into place. A worker
  dying in between leaves a partial that `_publish_initial_intent` refuses on every same-activation
  retry, and that a fresh-activation retry orphans. `cleanup_payloads` cannot reach either state —
  it runs only after `record_phase(..., "recovered")` on a completed directory — so this is a gap
  in the recovery model, not in this mechanism, and widening cleanup to sweep partials is
  explicitly refused. Neither this nor the directory residue can manifest before #2212 merges,
  because `ProviderRuntime.external_boot` is `None` until then and no production cleanup or
  `.partial` is ever produced; both are fixed inside this queue rather than deferred out of it.
