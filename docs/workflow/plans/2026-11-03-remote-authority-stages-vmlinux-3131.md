# Remote authority stages the uploaded vmlinux (#3131) implementation plan

Goal: the remote-libvirt authority installs the plan's uploaded `vmlinux` at
`/lib/modules/<release>/vmlinux` through the unchanged module appliance.

Architecture: plan member gains a staged tree manifest computed at build completion; a stager
wrapper fetches and verifies the file; the volume builder links it into the module source tree;
all manifest bindings read one plan property. Spec:
`docs/workflow/specs/2026-11-03-remote-authority-stages-vmlinux-3131-design.md`.

Tech stack: Python 3.14, pydantic, pytest, ruff, ty; recipes in `justfile`.

Expected implementation size: 450–700 changed lines (L) — ports (~25), validation (~45), plan
construction (~10), stager (~110), volumes (~40), host (~25), composition (~5), tests (~350),
docs and ADR amendments (~80).

## Global Constraints

- Branch `feat/stage-vmlinux-remote-authority-3131`; worktree
  `/home/dave/src/kdive-worktrees/feat-stage-vmlinux-remote-authority-3131`.
- No appliance, schema or migration change. Code cites #3131, never ADR-0723/0724 (Proposed).
- Do not edit the finalize / `_finalize_cleanup_quarantine` / `_activation_files` region of
  `external_boot_authority.py`, nor `local_libvirt/` files (PR #3136 owns them).
- Doc wording: no "critical", "robust", "comprehensive", "elegant"; say "Milestone".
- Guardrails: `just lint`, `just type`, focused `just test-verbose <paths>`; before push
  `git fetch origin main && just records`. Commit before any controlled-fault arm.

## File map

| File | Change | Criterion |
|---|---|---|
| `src/kdive/providers/ports/external_boot.py` | `DebuginfoSource.staged_module_source_manifest`; `ExternalBootPlan.module_tree_debuginfo`, `.module_tree_manifest` | 1, 2 |
| `src/kdive/build_artifacts/validation.py` | `_module_source_manifest(entries)` helper; staged manifest in `debuginfo` evidence | 2 |
| `src/kdive/services/external_boot/plan.py` | copy key into the member | 2 |
| `src/kdive/providers/remote_libvirt/debuginfo_module_tree.py` | new stager wrapper | 1 |
| `src/kdive/providers/external_boot_authority/host.py` | wrap `artifact_stager` (fetch for the module host, not the materializer) | 1 |
| `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_volumes.py` | `VolumeRequest.debuginfo`, link into tree | 1, 3 |
| `src/kdive/providers/remote_libvirt/remote_module_authority_host.py` | derive, `_volume_request(directory)` | 2 |
| `src/kdive/services/remote_module_authority_preparation.py`, `src/kdive/providers/remote_libvirt/external_boot_authority.py` (`_operation_matches_authority` only) | worker check, request validator | 2 |
| docs: ADR-0723, ADR-0724 amendments; `docs/operating/external-build-upload.md`, `docs/operating/providers/remote-libvirt.md` | | 4 |

## Task 1: plan member and properties

Verification: `Mode: focused-test`, contract: member with the key round-trips canonically, omits
it when `None`; `module_tree_manifest` returns staged else module manifest; `module_tree_debuginfo`
is `None` without the key. Test `tests/providers/ports/test_external_boot.py`
(`test_staged_manifest_*`); red: `AttributeError`; green:
`just test-verbose tests/providers/ports/test_external_boot.py`.

Interfaces (later tasks rely on): `ExternalBootPlan.module_tree_debuginfo -> DebuginfoSource | None`,
`ExternalBootPlan.module_tree_manifest -> str`.

```python
class DebuginfoSource(ArtifactSource):
    size_bytes: Annotated[int, Field(ge=1, le=_DEBUGINFO_MAX_BYTES)]
    # Omitted when absent so plans stored before #3131 keep their identity.
    staged_module_source_manifest: Digest | None = Field(
        None, exclude_if=lambda value: value is None
    )
```
```python
@property
def module_tree_debuginfo(self) -> DebuginfoSource | None:
    member = self.debuginfo
    return member if member and member.staged_module_source_manifest else None


@property
def module_tree_manifest(self) -> str:
    member = self.module_tree_debuginfo
    if member is None:
        return self.module_obligation.source_manifest
    return cast(str, member.staged_module_source_manifest)
```
Add a golden test: a plan whose `debuginfo` member lacks the key serializes to the exact bytes and
identity it had before this change (literal digest taken from `main` before the edit).
Steps: write tests, see red, implement, green, commit `feat(external-boot): plan carries the staged module-tree manifest (#3131)`.

## Task 2: build evidence and plan construction

Verification: `Mode: focused-test`. (a) `_scan_external_boot_archive(..., staged_debuginfo=(sha, size))`
returns `staged_module_source_manifest` equal to `_source_tree_manifest` (from
`remote_module_volumes`) of a real directory holding the extracted modules plus a `vmlinux` file
(no mkfs needed); omitted for a tree with a `vmlinux` entry, for a combined tree over the entry or
byte module bounds, and when not requested. File
`tests/providers/local_libvirt/test_validate_external_artifacts.py`; red `TypeError`; green
`just test-verbose tests/providers/local_libvirt/test_validate_external_artifacts.py`. (b) plan
construction copies `evidence["debuginfo"]["staged_module_source_manifest"]`; file
`tests/services/external_boot/`.

Implementation: factor the hashing at `validation.py:798-816` into
`_module_source_manifest(entries) -> str` (sort by `path.encode()`, `module-source-manifest-v1`,
domain `kdive-module-source-manifest-v1\0`); `_scan_external_boot_archive` gains
`staged_debuginfo: tuple[str, int] | None = None` and, when given and no entry has path
`vmlinux`, adds key `staged_module_source_manifest` using entry
`{"mode": "0644", "path": "vmlinux", "sha256": sha, "size": size, "type": "file"}`. The caller
computes `_debuginfo_evidence` before the scan and passes `(sha256, size_bytes)`; when the result
carries the key it is stored under `evidence["debuginfo"]`. `_debuginfo_source` in `plan.py` adds
the key when present in the evidence. The scan compares `module_bytes + size` and
`module_members + 1` with the module limits (`MAX_MODULE_REGULAR_BYTES`, `MAX_MODULE_ENTRIES`;
`providers/shared/external_boot_bounds.py` imports `validation`, so the scan reads them from
their definition site, not through that module).

## Task 3: stager wrapper

Verification: `Mode: focused-test`, `tests/providers/remote_libvirt/test_debuginfo_module_tree.py`:
no member passthrough (inner result unchanged, no file); staged member writes `debuginfo` with
verified bytes and returns `module_tree_manifest` and the composite installed digest; digest or
size mismatch raises `ValueError` and leaves no `debuginfo`; an existing matching `debuginfo` is
reused (store untouched); an existing mismatching one is refused. Red: `ModuleNotFoundError`;
green: `just test-verbose tests/providers/remote_libvirt/test_debuginfo_module_tree.py`.

Interfaces: `class DebuginfoModuleTreeStager: __init__(self, inner: ExternalBootArtifactStager,
object_store: ObjectStore, *, fetch: bool)`; composite installed digest
`"sha256:" + hashlib.sha256(b"kdive-remote-installed-with-debuginfo-v1\0" + base.encode() +
b"\0" + sha.encode() + b"\0" + str(size).encode()).hexdigest()`; `materialize_artifacts(plan, directory_fd) -> tuple[dict[str,
object], str]`. Store call confirmed in the repo: `store.get_artifact_stream(key, None,
version_id=...)` yielding `.reader`. Writes `.debuginfo.next` (`O_EXCL|O_NOFOLLOW`, 0600), fsync,
rename to `debuginfo`, fsync directory. `host.py:1777` builds one wrapper with `fetch=True` for `RemoteModuleAuthorityHostFactory` and
one with `fetch=False` for `ConcreteRemoteExternalBootMaterializer`; a composition test asserts
each. A missing object version raises the store's categorized error before any System stop.

## Task 4: volume build and binding

Verification: `Mode: focused-test`. (a) `Ext4SourceFilesystemWriter.build_from_archive(operation,
archive, debuginfo)` image whose readback manifest equals `_source_tree_manifest` of the
extracted tree plus the file (skip when `mkfs.ext4` is absent, like the existing writer tests);
`_build_request_image` passes `debuginfo=` only when set, so existing doubles (the fake at
`test_remote_module_authority_host.py:158`) change only for a staged-plan case; an existing `vmlinux` in the
archive and a byte-bound overflow raise. File `tests/providers/remote_libvirt/lifecycle/` (the
existing volume tests). (b) `derive_operation` returns `plan.module_tree_manifest`; the worker
check and `RemoteModuleVolumePreparationRequestV1` accept it and reject the module-only manifest
for a staged plan (and still accept it for an unstaged one); execute and restore pass
`directory/"debuginfo"` only for a staged plan: `test_remote_module_authority_host.py`,
`test_external_boot_authority.py`. Finish with `rg "module_obligation.source_manifest" src`.

Implementation: `VolumeRequest.debuginfo: Path | None = None`; protocol and writer
`build_from_archive(self, operation, archive, debuginfo=None)`; after
`_extract_canonical_module_archive`, `_link_debuginfo(modules, debuginfo)` refuses an existing
`vmlinux`, chmods the source `0644`, `os.link`s it, returns its size, and the caller re-runs
`validate_module_tree_bounds(entry_count=count + 1, content_bytes=bytes + size)`.
`_volume_request(self, authority, operation, directory)` builds `archive=directory / "modules"`.

## Task 5: documentation

Verification: `Mode: task-test-not-applicable`; reason: prose with no executable consumer; the
doc guards and `just records` check form. Append amendments to ADR-0723 and ADR-0724 (path
`/lib/modules/<release>/vmlinux` on remote-libvirt, why, upgrade order); update the
`external-build-upload.md` paragraph (remote-libvirt authority stages it) and add a short section
to `docs/operating/providers/remote-libvirt.md`.

## Deferrals

None recorded.
