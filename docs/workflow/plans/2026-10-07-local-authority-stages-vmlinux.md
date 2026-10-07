# Plan: the local-libvirt authority stages the uploaded vmlinux (#3130)

Spec: [2026-10-07-local-authority-stages-vmlinux-design.md](../specs/2026-10-07-local-authority-stages-vmlinux-design.md).

**Goal:** with a plan `debuginfo` member, the local external-boot authority publishes the
uploaded `vmlinux` at `/usr/lib/debug/lib/modules/<release>/vmlinux` and recovery restores the
prior file or its absence.

**Architecture:** optional port members carry the debuginfo through materialization and the
recovery state; a new layout-driven unit `GuestDebuginfoFile` owns three guest names; the
operation calls it after module publication and before module recovery.

**Tech stack:** Python 3.14, pydantic v2 (`Field(exclude_if=...)`), libguestfs Python binding,
pytest.

Expected implementation size: 750–1000 changed lines (L) — four source files (~320 lines) and
six test files (~600 lines), from the file map below.

## Global Constraints

- New port members default to `None` and use `Field(None, exclude_if=lambda value: value is
  None)`; stored canonical bytes and identities must not change (existing golden vectors stay).
- ADR-0724 is Proposed: cite `#3130` in `src/` and `tests/`, never `ADR-0724`.
- No new dependency. Guardrails: `just lint`, `just type`, `just test-changed`, `just records`,
  `just docs-check`, `just adr-status-check`. Full suite: `just test-linux HEAD`.
- Guest paths: live `/usr/lib/debug/lib/modules/<release>/vmlinux`; staging
  `.kdive-<activation_id>-vmlinux-staging`; old `.kdive-<activation_id>-vmlinux-old`, both in
  the same directory.

## File map

| File | Change |
|---|---|
| `src/kdive/providers/ports/external_boot.py` | `ProviderStateIdentity.debuginfo`; `MaterializedArtifacts.debuginfo`; `ExternalBootMaterialization.verified_debuginfo_sha256` + co-presence check |
| `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` | fetch/commit/validate `debuginfo`; owned names; `GuestDebuginfoFile`; prepare/activate/recover wiring |
| `src/kdive/providers/local_libvirt/lifecycle/boot/session.py` | `checksum`, `upload_projection_artifact` on `InactiveGuest`/`_GuardedGuest`; `_Guest.checksum` |
| `src/kdive/providers/local_libvirt/lifecycle/boot/session_mechanisms.py` | `PAYLOAD_NAMES` gains `debuginfo` |
| `tests/providers/ports/test_external_boot.py` | identity and co-presence tests |
| `tests/providers/local_libvirt/lifecycle/boot/test_materializer.py` | debuginfo materialize tests |
| `tests/providers/local_libvirt/lifecycle/boot/test_session.py` | upload/checksum seam test |
| `tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py` | payload cleanup removes `debuginfo` |
| `tests/providers/local_libvirt/lifecycle/boot/test_debuginfo_file.py` (new) | layout table tests |
| `tests/providers/local_libvirt/test_external_boot.py` | wiring tests |

## Task 1 — port members

**Interfaces:** later tasks use `ProviderStateIdentity(definition=..., modules=...,
debuginfo=ComponentState | None)`, `MaterializedArtifacts.debuginfo`,
`ExternalBootMaterialization.verified_debuginfo_sha256`.

**Verification:**
- Contract: absent members keep identities. Mode: focused-test —
  `test_materialization_matches_adr_golden_vector_and_identity` and a new
  `test_recovery_point_without_debuginfo_keeps_identity` (golden digest computed before the
  change); red: none expected for goldens, new test red only if the member serializes `null`.
  Green: `uv run python -m pytest tests/providers/ports/test_external_boot.py -q`.
- Contract: co-presence. Mode: focused-test — `test_materialization_debuginfo_ref_and_digest_are_paired`
  expects `ValidationError` for each half alone; red before the validator.

Steps:
1. Write the two tests; run the command; expect the pairing test to fail.
2. Add to `ProviderStateIdentity`:
   `debuginfo: ComponentState | None = Field(None, exclude_if=lambda value: value is None)`.
3. Add to `MaterializedArtifacts`: `debuginfo: OpaqueProviderRef | None = Field(None,
   exclude_if=lambda value: value is None)`; to `ExternalBootMaterialization`:
   `verified_debuginfo_sha256: Digest | None = Field(None, exclude_if=lambda value: value is
   None)`; in `_consistent` raise `"debuginfo reference and verified digest must have the same
   presence"` when `(self.artifacts.debuginfo is None) != (self.verified_debuginfo_sha256 is
   None)`.
4. Run the command; expect pass. Commit `feat(external-boot): carry debuginfo in port state`.

## Task 2 — materialize the debuginfo payload

**Interfaces:** projection payload name `"debuginfo"`; `_projection_ref(projection,
"debuginfo")` accepted by `_artifact_ref_parts`.

**Verification:**
- Contract: exact fetch and commit. Mode: focused-test —
  `test_materialize_fetches_exact_debuginfo_version` in `test_materializer.py`: plan with
  `debuginfo={key: "build/vmlinux", version: "vmlinux-v1", sha256, size_bytes}`; expects
  `debuginfo` in the digest directory with the object bytes, `artifacts.debuginfo` ending in
  `/debuginfo`, `verified_debuginfo_sha256 == plan.debuginfo.sha256`, a restart returning an
  equal materialization, and the vmlinux requested once. Red: `debuginfo` missing.
- Contract: size bound. Mode: focused-test — `test_materialize_rejects_debuginfo_size_mismatch`
  (`size_bytes` one more than the bytes) expects `ValueError("exact debuginfo version size")`
  and no `debuginfo` file.
- Contract: cleanup/abort owned names. Mode: focused-test — extend
  `tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py` payload cleanup test
  to place a `debuginfo` file and expect it removed.

Steps:
1. Write the tests; run `uv run python -m pytest tests/providers/local_libvirt/lifecycle/boot/test_materializer.py -q`; expect failures.
2. `_OWNED_TEMPORARY_NAMES` add `".debuginfo.next"`; `_cleanup_uncommitted_payloads` allowed
   add `"debuginfo"`; `_artifact_ref_parts` allowed add `"debuginfo"`;
   `session_mechanisms.PAYLOAD_NAMES = ("kernel", "initrd", "modules", "debuginfo")`;
   `remove_abortable_activation` appends `materialization.artifacts.debuginfo` when present.
3. `_stream_exact_version`: replace the initrd size check with
   `if isinstance(source, InitrdSource | DebuginfoSource) and size != source.size_bytes:`
   raising `"exact {initrd|debuginfo} version size does not match external-boot plan"`.
4. `_fetch_and_validate`: after the initrd block,
   `if plan.debuginfo is not None: fd = _stream_exact_version(store, plan.debuginfo, directory_fd, ".debuginfo.next"); os.close(fd); _commit_private_artifact(directory_fd, ".debuginfo.next", "debuginfo")`.
5. `validate_materialized_artifacts` calls new `_validate_local_debuginfo(plan, directory_fd)`:
   `digest, size = _descriptor_digest(directory_fd, "debuginfo")`; mismatch with
   `plan.debuginfo.sha256/size_bytes` raises `"materialized debuginfo bytes do not match
   external-boot plan"`. No second stream (spec item 2).
6. `materialize` sets `verified_debuginfo_sha256` and `artifacts.debuginfo`;
   `inspect_prepare` additionally requires `materialization.artifacts.debuginfo` to be `None`
   exactly when `verified_debuginfo_sha256` is `None`, and otherwise to equal
   `_projection_ref(projection, "debuginfo")` (same digest directory as the kernel).
7. Run the tests; expect pass. Commit `feat(local-libvirt): materialize the debuginfo payload`.

## Task 3 — session seam

**Interfaces:** `InactiveGuest.checksum(csumtype: str, path: str) -> str`;
`InactiveGuest.upload_projection_artifact(artifact: OpaqueProviderRef, guest_destination: str)
-> None`.

**Verification:** Mode: focused-test — `test_guest_uploads_projection_artifact_by_descriptor` in
`test_session.py` using the existing session fakes: the fake `_Guest.upload` receives a
`/proc/self/fd/<n>` path whose bytes equal the projection payload, and `checksum` delegates.
Red: attribute missing. Green: `uv run python -m pytest tests/providers/local_libvirt/lifecycle/boot/test_session.py -q`.

Steps:
1. Write the test; expect failure.
2. `_Guest` protocol: add `def checksum(self, csumtype: str, path: str) -> str: ...`.
   `InactiveGuest`: add both methods. `_GuardedGuest.checksum` returns
   `self._handle().checksum(csumtype, path)`. `_GuardedGuest.upload_projection_artifact` calls
   `self._owner._session._upload_projection_artifact(self._owner, self._guest, artifact,
   guest_destination)`, which guards, opens `open_projection_artifact(artifact, os.O_RDONLY)`
   and runs `_transfer_with_close(descriptor, lambda path: guest.upload(path, destination))`.
3. Run; pass. Commit with Task 4.

## Task 4 — `GuestDebuginfoFile`

**Interfaces:** `GuestDebuginfoFile(guest: InactiveGuest, *, binding:
ExternalBootActivationBinding, release: str)`; `.observe_live() -> ComponentState`;
`.stage(source: ComponentState, target: PresentComponentState, upload: Callable[[str], None])`;
`.publish(source, target)`; `.restore(source, target)`; module function
`debuginfo_file_state(*, size: int, sha256: str, mode: int, uid: int, gid: int) ->
PresentComponentState`. `InactiveGuest` gains `checksum` and `upload_projection_artifact` in
Task 3, so Task 3 lands first.

**Verification:** Mode: focused-test — new `test_debuginfo_file.py` with an in-memory fake guest
(dict of path → (bytes, mode, uid, gid), a directory set, and a symlink map resolved by
`exists`/`is_dir(followsymlinks=True)`) covering: stage from `(P,—,—)`, `(—,—,—)` with missing
directories, `(P,partial,—)`, `(P,T,—)` (no upload); publish from `(P,T,—)`, `(—,T,P)`, `(T,—,P)`
(no-op), and with P absent; restore from `(T,—,P)`, `(T,—,—)`, `(—,T,P)`, `(P,T,—)`, `(P,—,—)`
(no-op); P == T for all three; `/usr/lib/debug/lib -> usr/lib` symlinked ancestor publishes and
restores; conflicts (live changed, live a symlink, release directory a symlink, staging missing
at publish) raise `ValueError` with no mutation; a read-back mismatch after upload raises
before any move. Red: import error. Green: `uv run python -m pytest
tests/providers/local_libvirt/lifecycle/boot/test_debuginfo_file.py -q`.

Code (in `external_boot.py`, after `_SessionModulePublicationIO`):

```python
_DEBUGINFO_ROOT = "/usr/lib/debug/lib/modules"
_DEBUGINFO_ANCESTORS = ("/usr/lib/debug", "/usr/lib/debug/lib", _DEBUGINFO_ROOT)
type _DebuginfoLayout = tuple[
    PresentComponentState | None, PresentComponentState | None, PresentComponentState | None
]


def debuginfo_file_state(
    *, size: int, sha256: str, mode: int, uid: int, gid: int
) -> PresentComponentState:
    """Identity of one regular guest file: content plus the metadata publish sets (#3130)."""
    facts = {"gid": gid, "mode": f"{mode:04o}", "sha256": sha256, "size": size, "uid": uid}
    data = json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(b"kdive-debuginfo-file-v1\0" + data).hexdigest()
    return PresentComponentState(manifest=f"sha256:{digest}")


class GuestDebuginfoFile:
    """Activation-owned live/staging/old names for one release's DWARF vmlinux (#3130)."""

    def __init__(
        self, guest: InactiveGuest, *, binding: ExternalBootActivationBinding, release: str
    ) -> None:
        if not release or "/" in release or release in {".", ".."}:
            raise ValueError("debuginfo release is invalid")
        self._guest = guest
        self._directory = f"{_DEBUGINFO_ROOT}/{release}"
        prefix = f"{self._directory}/.kdive-{binding.activation_id}-vmlinux"
        self.live = f"{self._directory}/vmlinux"
        self.staging = f"{prefix}-staging"
        self.old = f"{prefix}-old"

    def observe_live(self) -> ComponentState:
        state = self._layout()[0]
        return AbsentComponentState() if state is None else state

    def stage(
        self, source: ComponentState, target: PresentComponentState, upload: Callable[[str], None]
    ) -> None:
        prior = _layout_component(source)
        live, staging, old = self._layout()
        if live != prior or old is not None:
            raise ValueError("external-boot debuginfo staging conflict")
        if staging == target:
            return
        if staging is not None:
            self._guest.rm_rf(self.staging)
        self._directories(create=True)
        upload(self.staging)
        self._guest.chmod(0o644, self.staging)
        self._guest.chown(0, 0, self.staging)
        if self._observe(self.staging) != target:
            raise ValueError("staged debuginfo does not match the materialized vmlinux")
        self._guest.sync()

    def publish(self, source: ComponentState, target: PresentComponentState) -> None:
        prior = _layout_component(source)
        layout = self._layout()
        if layout == (prior, target, None) and prior is not None:
            self._guest.mv(self.live, self.old)
            layout = (None, target, prior)
        if layout == (None, target, prior):
            self._guest.mv(self.staging, self.live)
            self._guest.sync()
        elif layout != (target, None, prior):
            raise ValueError("external-boot debuginfo publication conflict")
        if self._layout() != (target, None, prior):
            raise ValueError("external-boot debuginfo publication did not complete")

    def restore(self, source: ComponentState, target: PresentComponentState) -> None:
        prior = _layout_component(source)
        layout = self._layout()
        if layout == (prior, None, None):
            return
        if layout in ((target, None, prior), (None, target, prior)):
            if prior is not None:
                self._guest.mv(self.old, self.live)
            elif layout[0] is not None:
                self._guest.rm_rf(self.live)
            layout = (prior, layout[1], None)
        if layout[0] != prior or layout[2] is not None:
            raise ValueError("external-boot debuginfo recovery conflict")
        if layout[1] is not None:
            self._guest.rm_rf(self.staging)
        self._guest.sync()
        if self._layout() != (prior, None, None):
            raise ValueError("external-boot debuginfo recovery did not complete")

    def _layout(self) -> _DebuginfoLayout:
        if not self._directories(create=False):
            return None, None, None
        return self._observe(self.live), self._observe(self.staging), self._observe(self.old)

    def _directories(self, *, create: bool) -> bool:
        # Distro symlinks above the release directory are followed (Fedora ships
        # /usr/lib/debug/lib -> usr/lib); libguestfs keeps every path inside the guest root.
        walk = [(path, True) for path in _DEBUGINFO_ANCESTORS] + [(self._directory, False)]
        for path, follow in walk:
            if not self._guest.exists(path):
                if not create:
                    return False
                self._guest.mkdir(path)
            elif not self._guest.is_dir(path, followsymlinks=follow):
                raise ValueError("external-boot debuginfo directory is not a directory")
        return True

    def _observe(self, path: str) -> PresentComponentState | None:
        if not self._guest.exists(path):
            return None
        status = self._guest.lstatns(path)
        if not stat.S_ISREG(status["st_mode"]):
            raise ValueError("external-boot debuginfo name is not a regular file")
        return debuginfo_file_state(
            size=status["st_size"],
            sha256="sha256:" + self._guest.checksum("sha256", path),
            mode=stat.S_IMODE(status["st_mode"]),
            uid=status["st_uid"],
            gid=status["st_gid"],
        )
```

`_directories` walks `(path, follow)` pairs `(/usr/lib/debug, True)`, `(/usr/lib/debug/lib,
True)`, `(_DEBUGINFO_ROOT, True)`, `(self._directory, False)`: when `exists(path)` is true it
requires `is_dir(path, followsymlinks=follow)` else raises `ValueError("external-boot
debuginfo directory is not a directory")`; when false it returns `False` if `create` is false
and calls `mkdir(path)` otherwise; it returns `True` when every directory exists. The no-follow
check on the release directory uses `is_dir(path, followsymlinks=False)`, which lstat-s the last
component only. `_observe(path)`: `exists` false → `None`; `lstatns` mode not `S_ISREG` →
`ValueError("external-boot debuginfo name is not a regular file")`; otherwise
`debuginfo_file_state(size=st_size, sha256="sha256:" + checksum("sha256", path),
mode=S_IMODE(st_mode), uid=st_uid, gid=st_gid)`.

Steps: write the tests (red: import error), add the code, run green, commit with Task 3 as
`feat(local-libvirt): stage, publish and restore one guest debuginfo file`.

## Task 5 — wire into prepare, activate and recover

**Interfaces:** consumes Tasks 1–4. `_complete_preparation_metadata(intent, materialization,
capture, target_manifest, debuginfo: tuple[ComponentState, PresentComponentState] | None)`.

**Verification:**
- Contract: prepare records both states. Mode: focused-test —
  `test_prepare_records_debuginfo_source_and_target` in `test_external_boot.py` with the
  `_RealSession` harness and a materialization carrying the debuginfo pair: expects
  `source_state.debuginfo == AbsentComponentState()` and `target_state.debuginfo ==
  debuginfo_file_state(size, sha256, mode=0o644, uid=0, gid=0)`; without the member both are
  `None` and the recovery record bytes have no `debuginfo` key.
- Contract: activate publishes, recover restores, no member is unchanged. Mode: focused-test —
  `test_activate_publishes_debuginfo_after_modules` and
  `test_recover_restores_debuginfo_before_modules`, plus
  `test_record_without_debuginfo_recovers_without_touching_debug_paths` asserting no guest call
  names `/usr/lib/debug`.
- Contract: pre-change record readable. Mode: focused-test —
  `test_recovery_metadata_from_before_debuginfo_parses_unchanged`: bytes of a record written by
  `_metadata()` parse and re-serialize byte-equal with `target_state.debuginfo is None`.
Green: `uv run python -m pytest tests/providers/local_libvirt/test_external_boot.py -q`.

Steps:
1. Write tests; expect red.
2. `prepare`: inside the existing `with self._session.guest() as guest:` block, when
   `materialization.artifacts.debuginfo is not None`, compute the target via
   `_materialized_debuginfo_state(self._session, materialization)` (digest and size of the
   payload descriptor, `ValueError` when the digest differs from
   `verified_debuginfo_sha256`) and the source via `GuestDebuginfoFile(guest, binding=...,
   release=intent.release).observe_live()`; pass both to `_complete_preparation_metadata`,
   which sets `debuginfo=` on both `ProviderStateIdentity` values.
3. `activate_modules`: in the `pre-stop-intent` block, after the module staging check and
   before `publication.guest_sync()`, when `metadata.target_state.debuginfo is not None`, call
   `GuestDebuginfoFile(...).stage(source, target, upload)` where
   `upload = lambda path: opened_guest.upload_projection_artifact(_debuginfo_ref(metadata), path)`
   and `_debuginfo_ref` returns `metadata.materialized_modules` with its last segment replaced
   by `debuginfo` (validated by `_artifact_ref_parts`). After `_finish_present_publication`,
   call `.publish(source, target)`. Test `test_failed_debuginfo_upload_recovers_from_pre_stop`
   injects an upload error and expects the record to stay `pre-stop-intent` and a following
   recover to reach `module-restored` with the staging name removed.
4. `recover_modules` (right after the guest opens) and `_settle_unpublished_modules` (before
   `discard_staging`): call `_restore_debuginfo(guest, metadata)` which runs `restore` when the
   target is present.
5. Run; green. Commit `feat(local-libvirt): stage the vmlinux on authority installs`.

## Task 6 — live proof (no code)

Mode: task-test-not-applicable for code: the contract is the deployed authority on real
hardware, which no unit test observes. Per arm, on a host whose deployed build equals HEAD:
1. Record the guest's prior `/usr/lib/debug/lib/modules/<release>/vmlinux` sha256 or absence
   (guestfish/virt-cat on the inactive overlay, or `sha256sum` in the guest).
2. Run `test_spine_live_script_over_the_wire` through the authority; expect pass with no
   `missing_debuginfo`/`debuginfo_unloadable`.
3. In the guest, `sha256sum` and `stat -c '%a %u %g %s'` of the live file: expect
   `plan.debuginfo.sha256`, `644 0 0 <size_bytes>`. Record the size and the authority's
   publish/restore wall time for #3125.
4. Recover (rollback, or a second install that recovers the first); expect the step 1 digest or
   absence and no `.kdive-*-vmlinux-*` names.
The ppc64le arm runs on the native POWER9 KVM-HV host. No x86_64 host runs the authority; the
x86_64 arm is reported to the operator through the campaign orchestrator as not run.
