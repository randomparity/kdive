# RHEL-family kdump advisory — implementation plan (#2762)

Goal: warn at `runs.complete_build` when the uploaded config lacks `crash_capture_rhel_guest`
symbols and the target image is (or may be) RHEL-family, and point empty kdump captures at it.

Architecture: the System → catalog image resolver moves to `images/cataloging/catalog.py`; a new
`kernel_config/gate.py` advisory consumes the image's `os_release.id`; `complete_build` wires it
into the success envelope; both retrieve providers attach a static hint to a no-core failure.
Spec: `docs/workflow/specs/2026-09-25-rhel-guest-kdump-advisory-design.md`. ADR-0678.

Tech stack: Python 3.14, psycopg async, pytest (+ testcontainers Postgres), `uv`, `just`.

Expected implementation size: 220–320 changed lines (M) — five tasks: ~40 resolver move, ~60
advisory, ~50 envelope, ~40 hint, ~20 spine, plus tests and doc text.

## Global Constraints

- Ruff line length 100; `ty` strict; `just lint`, `just type` green at every commit.
- No refusal anywhere: `runs.complete_build` always succeeds over config (ADR-0330).
- Payload shape `{reason, missing, remediation}` + optional `built_in_required` (ADR-0330/#1860).
- Doc style: no "critical/robust/comprehensive/elegant".
- Generated artifacts regenerate, never hand-edit: `just resources-docs`, `just docs`,
  `just cli-verbs`.

## File map

| File | Now owns | Change |
|---|---|---|
| `src/kdive/images/cataloging/catalog.py` | image catalog resolvers | gains `resolve_system_catalog_rootfs`, `image_os_id` |
| `src/kdive/mcp/tools/lifecycle/vmcore/_vmcore_kdump_gate.py` | kdump capability gate + private resolver | private resolver and SQL deleted; imports the moved one |
| `src/kdive/kernel_config/requirements.py` | feature registry | rhel entry `UPLOAD_ADVISORY`, summary; `EMPTY_CAPTURE_CONFIG_HINT` |
| `src/kdive/kernel_config/gate.py` | config advisories | `RHEL_FAMILY_OS_IDS`, `rhel_guest_crash_warning` |
| `src/kdive/mcp/tools/lifecycle/runs/complete_build.py` | success envelope | resolves `os_id`, adds `rhel_guest_crash_config` |
| `src/kdive/mcp/tools/lifecycle/runs/registrar.py` | wrapper docstring | names the field |
| `src/kdive/providers/local_libvirt/retrieve/provider.py` | `_no_core` | hint for kdump-family methods |
| `src/kdive/providers/remote_libvirt/retrieve/{common,kdump_capture}.py` | readiness failures | `config_hint` flag |
| `tests/integration/live_stack/spine.py` | build upload | uploads `effective_config` |
| `docs/operating/external-build-upload.md` (+ generated copies) | recipe | describes advisory and hint |

## Task 1 — Move the catalog-rootfs resolver; add `image_os_id`

Verification:
- Mode: focused-test — `resolve_system_catalog_rootfs` returns the registered public arch row for
  a local catalog profile and `None` for an unparsable profile; `image_os_id` returns the id or
  `None` for missing/non-dict/empty. Tests in `tests/images/test_catalog_resolver.py`; red is
  `ImportError`; green `just test-verbose tests/images/test_catalog_resolver.py`.
- Mode: focused-test — kdump gate unchanged: `tests/mcp/tools/test_vmcore_kdump_gate.py` patches
  `gate.resolve_system_catalog_rootfs`; green `just test-verbose tests/mcp/tools/test_vmcore_kdump_gate.py`.

Interfaces (produced):
`async def resolve_system_catalog_rootfs(conn: AsyncConnection, system: System) -> ImageCatalogEntry | None`;
`def image_os_id(entry: ImageCatalogEntry) -> str | None`.

Steps:
1. Write the tests: seed a `systems`-free stub (`cast(System, obj)` with `provisioning_profile`)
   holding a local-libvirt catalog profile and an `IMAGE_CATALOG` row via the file's existing
   `_entry`/insert helpers; assert the row resolves and `"::bad::"` gives `None`. Add
   `image_os_id` cases: `{"os_release": {"id": "fedora"}}` → `"fedora"`; `{}`,
   `{"os_release": "x"}`, `{"os_release": {"id": ""}}` → `None`. Run; expect ImportError.
2. In `catalog.py` add (reusing `_RESOLVE_PUBLIC_SYNC_SQL`, renamed `_RESOLVE_PUBLIC_ARCH_SQL`):

```python
async def resolve_system_catalog_rootfs(
    conn: AsyncConnection, system: System
) -> ImageCatalogEntry | None:
    """The registered public arch-matched image a System's local-libvirt catalog rootfs names.

    ``None`` on every resolution gap: unparsable profile, non-local-libvirt section, a rootfs that
    is not ``catalog``, or no visible row (ADR-0361, ADR-0678).
    """
    try:
        profile = ProvisioningProfile.parse(system.provisioning_profile)
    except CategorizedError:
        return None
    section = profile.provider.local_libvirt_section
    if section is None or not isinstance(section.rootfs, CatalogComponentRef):
        return None
    params = {
        "provider": section.rootfs.provider,
        "name": section.rootfs.name,
        "arch": profile.arch,
        "registered": ImageState.REGISTERED.value,
        "public": ImageVisibility.PUBLIC.value,
    }
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RESOLVE_PUBLIC_ARCH_SQL, params)
        row = await cur.fetchone()
    return None if row is None else ImageCatalogEntry.model_validate(row)


def image_os_id(entry: ImageCatalogEntry) -> str | None:
    """The build-recorded os-release ``ID`` (ADR-0311), or ``None`` when absent or malformed."""
    record = entry.provenance.get(PROVENANCE_OS_RELEASE)
    os_id = record.get("id") if isinstance(record, dict) else None
    return os_id if isinstance(os_id, str) and os_id else None
```
3. In `_vmcore_kdump_gate.py` delete `_RESOLVE_PUBLIC_ARCH_SQL` and `_resolve_catalog_rootfs`,
   import `resolve_system_catalog_rootfs`, call it in `refusing_kdump_capability`; update the
   gate test's monkeypatch target and move its unparsable-profile test to step 1's file.
4. Run both green commands, `just lint`, `just type`; commit `refactor(images): share the
   System catalog-rootfs resolver (#2762)`.

## Task 2 — The advisory and the registry entry

Verification:
- Mode: focused-test — `rhel_guest_crash_warning` in `tests/kernel_config/test_gate.py`
  (patching `kdive.kernel_config.gate.load_effective_config` as the file already does): `"fedora"`
  + config without `SQUASHFS_ZSTD` → payload `missing` contains it, `guest_family == "rhel"`,
  reason `kernel_missing_rhel_guest_crash_config`; `"debian"` → `None` and the loader is never
  called; full seven-symbol config → `None`; loader `None` → `None`; `os_id=None` → `"unknown"`.
  Red: ImportError. Green: `just test-verbose tests/kernel_config/test_gate.py`.
- Mode: focused-test — served entry: `tests/mcp/catalog/test_external_build_contract_resource.py`
  `test_served_contract_advertises_the_rhel_guest_kdump_symbols_ungated` now expects
  `Enforcement.UPLOAD_ADVISORY` (red before step 3).

Interfaces (produced): `RHEL_FAMILY_OS_IDS: frozenset[str]`,
`RHEL_GUEST_CRASH_CONFIG_REASON = "kernel_missing_rhel_guest_crash_config"`,
`async def rhel_guest_crash_warning(conn, run_id, *, os_id: str | None) -> dict[str, JsonValue] | None`,
`EMPTY_CAPTURE_CONFIG_HINT: Final[dict[str, JsonValue]]` (requirements.py).

Steps:
1. Write the tests; run; expect red.
2. `gate.py`:

```python
RHEL_FAMILY_OS_IDS: frozenset[str] = frozenset({"fedora", "rhel", "centos", "rocky", "almalinux"})
RHEL_GUEST_CRASH_CONFIG_REASON = "kernel_missing_rhel_guest_crash_config"
_RHEL_GUEST_REMEDIATION = (
    "the target System boots a RHEL-family image: build the missing CONFIG_* in or kdump's "
    "capture kernel cannot mount its dracut initramfs or the XFS root and writes no vmcore "
    f"(see {_EXTERNAL_BUILD_CONTRACT_URI})"
)
_UNKNOWN_GUEST_REMEDIATION = (
    "kdive could not tell which OS the target System boots (the Run is not bound yet, or its "
    "rootfs is not a registered catalog image with a recorded os-release). If the guest is "
    "RHEL-family (Fedora, RHEL, Rocky, AlmaLinux, CentOS Stream), build the missing CONFIG_* in "
    "or kdump writes no vmcore; for any other guest ignore this "
    f"(see {_EXTERNAL_BUILD_CONTRACT_URI})"
)


async def rhel_guest_crash_warning(
    conn: AsyncConnection, run_id: UUID, *, os_id: str | None
) -> dict[str, JsonValue] | None:
    """Non-fatal ``crash_capture_rhel_guest`` advisory for ``runs.complete_build`` (ADR-0678)."""
    if os_id is not None and os_id not in RHEL_FAMILY_OS_IDS:
        return None
    config = await load_effective_config(conn, run_id)
    if config is None:
        return None
    unmet = unmet_advertised_clauses(config, feature_requirement(CRASH_CAPTURE_RHEL_GUEST))
    if not unmet:
        return None
    known = os_id is not None
    payload = _clause_payload(
        config,
        unmet,
        reason=RHEL_GUEST_CRASH_CONFIG_REASON,
        remediation=_RHEL_GUEST_REMEDIATION if known else _UNKNOWN_GUEST_REMEDIATION,
    )
    payload["guest_family"] = "rhel" if known else "unknown"
    return payload
```
3. `requirements.py`: set `enforcement=Enforcement.UPLOAD_ADVISORY` on the rhel entry; replace
   "they are advisory, never gated: kdive cannot tell which OS your guest runs" with "they are
   advisory, never gated: runs.complete_build warns when the target System boots a RHEL-family
   catalog image, or when it cannot tell which OS the guest runs"; add

```python
EMPTY_CAPTURE_CONFIG_HINT: Final[dict[str, JsonValue]] = {
    "feature": CRASH_CAPTURE_RHEL_GUEST,
    "contract": "resource://kdive/contracts/external-build",
    "note": (
        "kdump wrote no core. On a RHEL-family guest a likely cause is a kernel missing the "
        "crash_capture_rhel_guest symbols: the capture kernel boots, cannot mount the dracut "
        "kdump initramfs, and panics before writing. runs.complete_build reports them in "
        "data.rhel_guest_crash_config when the uploaded effective_config lacks them."
    ),
}
```
4. Update the module docstring's consumer list; green, `just lint`, `just type`; commit
   `feat(kernel-config): add the RHEL-family kdump advisory (#2762)`.

## Task 3 — Wire the advisory into `runs.complete_build`

Verification:
- Mode: focused-test — in `tests/mcp/lifecycle/test_complete_build_tool.py`, with
  `_patched_config_load` and `monkeypatch.setattr(complete_build_module, "_target_os_id", ...)`:
  `"fedora"` + a config lacking the set → `data["rhel_guest_crash_config"]["guest_family"] ==
  "rhel"` and the contract ref; the replay carries the same value; `"debian"` → absent; a config
  that also lacks `VIRTIO_BLK` carries both warnings; with no config the nudge appears and the new
  key is absent. A second test runs the real `_target_os_id` on an unbound seeded Run and asserts
  `guest_family == "unknown"`. Red: `KeyError`. Green:
  `just test-verbose tests/mcp/lifecycle/test_complete_build_tool.py`.
- Mode: task-test-not-applicable — the wrapper docstring sentence: agent-facing prose; the
  generated reference check (`just docs-check`, `just cli-verbs-check`) guards the copies.

Steps:
1. Write the tests; run; expect red.
2. In `complete_build.py`:

```python
async def _target_os_id(conn: AsyncConnection, run: Run) -> str | None:
    """The target image's os-release id, or ``None`` when kdive cannot resolve it (ADR-0678)."""
    if run.system_id is None:
        return None
    system = await SYSTEMS.get(conn, run.system_id)
    entry = None if system is None else await resolve_system_catalog_rootfs(conn, system)
    return None if entry is None else image_os_id(entry)
```
   In `_success_envelope`, after `warning`/`nudge`:
   `rhel = None if nudge is not None else await rhel_guest_crash_warning(conn, uid,
   os_id=await _target_os_id(conn, run))`, pass `rhel=rhel` to `_complete_envelope`, which sets
   `data["rhel_guest_crash_config"] = rhel` and `refs["external_build_contract"]` when non-`None`.
   Extend the method docstring: the nudge excludes both warnings; the two warnings are independent.
3. `registrar.py` wrapper docstring, new paragraph: "An uploaded effective_config is checked, never
   refused: `data.missing_boot_config` names boot symbols the guest cannot mount root without, and
   `data.rhel_guest_crash_config` names crash_capture_rhel_guest symbols a RHEL-family guest needs
   to write a kdump vmcore (`guest_family` is `rhel`, or `unknown` when kdive cannot tell the
   guest's OS)."
4. `just docs`, `just cli-verbs`; green; `just lint`, `just type`; commit
   `feat(runs): report the RHEL-family kdump advisory at complete_build (#2762)`.

## Task 4 — Point empty kdump captures at the advisory

Verification:
- Mode: focused-test — local: `test_capture_empty_crashdir_keeps_no_core_path` also asserts
  `details["kernel_config_hint"] == EMPTY_CAPTURE_CONFIG_HINT`; a new test drives
  `LocalLibvirtRetrieve._no_core(_SYS, CaptureMethod.HOST_DUMP)` and asserts the key is absent.
  Green: `just test-verbose tests/providers/local_libvirt/test_retrieve_kdump.py`.
- Mode: focused-test — remote: `test_capture_no_core_present_is_readiness_failure` and
  `test_capture_readiness_window_exhausted_is_readiness_failure` assert the hint. Green:
  `just test-verbose tests/providers/remote_libvirt/retrieve/test_retrieve.py`.

Steps:
1. Write the assertions; run; expect `KeyError`.
2. Local: `_no_core(system_id: UUID, method: CaptureMethod)`; details
   `{"system_id": ...}` plus `"kernel_config_hint": EMPTY_CAPTURE_CONFIG_HINT` when
   `method is not CaptureMethod.HOST_DUMP`; the one caller passes `method`.
3. Remote: `readiness_failure(system_id, reason, *, config_hint: bool = False)` adds the same key
   when `True`; both calls in `kdump_capture.py` pass `config_hint=True`.
4. Green; `just lint`, `just type`; commit
   `feat(retrieve): point an empty kdump capture at the RHEL-family set (#2762)`.

## Task 5 — Spine upload and docs

Verification:
- Mode: task-test-not-applicable — `build_and_upload_kernel` runs only against a live stack; the
  live arm proves it.
- Mode: focused-test — packaged doc copy: `just resources-docs-check` fails before regeneration.

Steps:
1. In `build_and_upload_kernel`, keep `config_bytes = check_spine_kernel_config(...)`, write them
   to `Path(scratch) / "effective_config"`, append a `{"name": "effective_config", "sha256",
   "size_bytes"}` declaration, and `put_presigned(by_name["effective_config"], path)`.
2. In `docs/operating/external-build-upload.md`, after the RHEL-family block, replace "kdive
   advertises it and never refuses on it" with a paragraph naming `data.rhel_guest_crash_config`,
   `guest_family` `rhel`/`unknown`, and the `kernel_config_hint` on a no-core `vmcore.fetch`.
3. `just resources-docs`; `just resources-docs-check`; commit
   `docs(external-build): describe the RHEL-family kdump advisory (#2762)`.

Final: `git fetch origin main && just records`; `just ci > <file> 2>&1 < /dev/null`.
