# Image kernel-config offer (spec 2 of 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Advertise each rootfs image's default kernel version and offer its
`/boot/config-<ver>` to the agent via a new `images.kernel_config` read tool that mints a
presigned download URL.

**Architecture:** At image-build time (local-libvirt build plane) capture the default
kernel version (advisory provenance operand) and the `/boot/config-<ver>` bytes. Persist
the version in catalog `provenance` (surfaced by `images.list`/`describe`); store the
config as a best-effort sibling object of the qcow2, keyed on a new nullable
`image_catalog.kernel_config_key`. A new read tool resolves the row under the
`images.describe` visibility predicate and presigns a short-lived GET. kdive never
validates the config.

**Tech Stack:** Python 3.14, `uv`, FastMCP, psycopg (async), Postgres, S3/MinIO object
store, libguestfs (`guestfish`, live_vm only), pytest.

**Spec:** `docs/superpowers/specs/2026-07-08-image-kernel-config-offer-1051-design.md`
**ADR:** `docs/adr/0317-image-kernel-config-offer.md`

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Absolute imports only (no `..`).
- `ty check` strict, whole-tree (src + tests). No project-wide relaxations.
- Google-style docstrings on non-trivial public APIs. Doc-style: use "Milestone" not
  "Sprint"; avoid "critical/robust/comprehensive/elegant" in prose, ADRs, commit messages.
- Guardrails per commit: `just lint`, `just type`, `just test` (relevant subset), and
  `just docs-check` after any tool/wrapper-docstring change. CI runs these recipes
  **individually**, so each must pass on its own.
- Migrations are additive, forward-only (ADR-0015); single-migration-owner rule. Next free
  migration number is **0063**.
- Advisory build-capture rule (ADR-0252/0253/0295/0311): a probe/capture failure degrades
  to absent and the build still publishes; a degraded row is byte-identical to a
  pre-feature one (omit the operand, don't write an empty value).
- The wrapper docstring + `Field(description=...)` is the agent-facing contract — update it,
  not only the handler, when a tool's returned fields change.
- Commit trailer required: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

- `src/kdive/db/schema/0063_image_catalog_kernel_config_key.sql` — **create**: additive
  nullable column.
- `src/kdive/domain/catalog/images.py` — **modify**: add `kernel_config_key` field.
- `src/kdive/images/planes/_build_common.py` — **modify**: `probe_kernel_config` +
  `KernelConfigProbeSeam` + `DEFAULT_KERNEL_CONFIG_PROBE`.
- `src/kdive/images/planes/base.py` — **modify**: `RootfsBuildOutput.kernel_config`.
- `src/kdive/providers/local_libvirt/rootfs_build.py` — **modify**: capture default kernel
  version + config; wire the probe seam.
- `src/kdive/services/images/publish.py` — **modify**: `PublishRequest.kernel_config`,
  `kernel_config_object_key`, set key at adopt/insert, best-effort config write,
  clear-on-failure at the `registered` flip.
- `src/kdive/jobs/handlers/image_build.py` — **modify**: thread `output.kernel_config`.
- `src/kdive/reconciler/cleanup/images.py` — **modify**: leaked cross-check protects
  `kernel_config_key`.
- `src/kdive/services/images/retention.py` — **modify**: private-expiry deletes the config.
- `src/kdive/mcp/tools/catalog/images.py` — **modify**: surface `default_kernel_version` in
  list/describe; add `images.kernel_config` tool + handler.

---

## Task 1: Migration + catalog-model field for `kernel_config_key`

**Files:**
- Create: `src/kdive/db/schema/0063_image_catalog_kernel_config_key.sql`
- Modify: `src/kdive/domain/catalog/images.py` (`ImageCatalogEntry`)
- Test: `tests/db/test_migrate.py` (existing enum/column coverage), `tests/services/images/test_publish.py` (round-trip)

**Interfaces:**
- Produces: `ImageCatalogEntry.kernel_config_key: str | None` (defaults `None`); a new
  `image_catalog.kernel_config_key text` nullable column.

- [ ] **Step 1: Write the migration**

Create `src/kdive/db/schema/0063_image_catalog_kernel_config_key.sql`:

```sql
-- 0063_image_catalog_kernel_config_key.sql — image kernel-config offer (ADR-0317, #1051).
-- Additive, forward-only (ADR-0015). Object-store key of the image's extracted
-- /boot/config-<ver>, a sibling object of the qcow2. NULL when no config was captured
-- (a staged path/volume image, a pre-feature row, or a best-effort config-write failure).
-- Independent of the object_key/volume/path exactly-one invariant: not part of that CHECK.

ALTER TABLE image_catalog ADD COLUMN kernel_config_key text;
```

- [ ] **Step 2: Add the model field**

In `src/kdive/domain/catalog/images.py`, `ImageCatalogEntry`, after `path: str | None = None`:

```python
    kernel_config_key: str | None = None
```

Extend the class docstring's object-key sentence with: `` ``kernel_config_key`` is the
object-store key of the image's extracted ``/boot/config-<ver>`` (a sibling of the qcow2),
``None`` when no config was captured. ``

- [ ] **Step 3: Run the migration + model tests**

Run: `just test -- tests/db/test_migrate.py -q` (needs Docker/Postgres; skips cleanly if
absent — if it skips, verify with the model round-trip in Step 4 instead).
Expected: PASS (the additive column applies; existing rows read `kernel_config_key = None`).

- [ ] **Step 4: Run the publish round-trip**

Run: `uv run python -m pytest tests/services/images/test_publish.py -q`
Expected: PASS (model still validates; `kernel_config_key` defaults `None`).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/db/schema/0063_image_catalog_kernel_config_key.sql src/kdive/domain/catalog/images.py
git commit -m "feat(images): add nullable image_catalog.kernel_config_key (#1051)"
```

---

## Task 2: Read-only `probe_kernel_config` guestfish seam

**Files:**
- Modify: `src/kdive/images/planes/_build_common.py`
- Test: `tests/images/planes/test_build_common.py`

**Interfaces:**
- Consumes: existing `_GUESTFISH_TIMEOUT_S`, `CategorizedError`, `ErrorCategory`.
- Produces: `probe_kernel_config(qcow2_path: Path, version: str) -> str | None`;
  `type KernelConfigProbeSeam = Callable[[Path, str], str | None]`;
  `DEFAULT_KERNEL_CONFIG_PROBE: KernelConfigProbeSeam`.

The real probe is `# pragma: no cover - live_vm` (like `probe_makedumpfile_marker`); the
seam is exercised via injected fakes in Task 3. The unit test here asserts the seam type
and default binding exist and are wired.

- [ ] **Step 1: Write the failing test**

In `tests/images/planes/test_build_common.py`, add:

```python
def test_kernel_config_probe_default_is_the_real_probe():
    from kdive.images.planes._build_common import (
        DEFAULT_KERNEL_CONFIG_PROBE,
        probe_kernel_config,
    )

    assert DEFAULT_KERNEL_CONFIG_PROBE is probe_kernel_config
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/images/planes/test_build_common.py::test_kernel_config_probe_default_is_the_real_probe -v`
Expected: FAIL with `ImportError` / `cannot import name 'probe_kernel_config'`.

- [ ] **Step 3: Implement the probe + seam**

In `src/kdive/images/planes/_build_common.py`, after the `probe_makedumpfile_marker` block
(and its `type MakedumpfileProbeSeam` / `DEFAULT_MAKEDUMPFILE_PROBE` pattern), add:

```python
type KernelConfigProbeSeam = Callable[[Path, str], str | None]


def probe_kernel_config(  # pragma: no cover - live_vm
    qcow2_path: Path, version: str
) -> str | None:
    """Read ``/boot/config-<version>`` from ``qcow2_path``, read-only via ``guestfish`` (ADR-0317).

    The build-time operand of the kernel-config offer: the caller writes the returned text to
    the object store so the agent can fetch its selected image's known-good starting config.
    Reads with a read-only ``guestfish -i cat /boot/config-<version>``.

    Returns:
        The config file text, or ``None`` when it is absent (a non-zero ``guestfish`` exit or
        an empty body) — never raising for a merely-missing config; the caller treats ``None``
        as "no config offered" and omits it.

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` if ``guestfish`` is absent;
            ``INFRASTRUCTURE_FAILURE`` on timeout. Both are caught by the advisory caller and
            degrade to an omitted config, so a probe failure never fails a build.
    """
    guest_path = f"/boot/config-{version}"
    argv = ["guestfish", "--ro", "-a", str(qcow2_path), "-i", "cat", guest_path]
    try:
        result = subprocess.run(  # noqa: S603 - fixed guestfish argv; image path is a data arg
            argv,
            capture_output=True,
            text=True,
            timeout=_GUESTFISH_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            "guestfish is not installed; cannot read the kernel config",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"tool": "guestfish"},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "guestfish exceeded its timeout reading the kernel config",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": _GUESTFISH_TIMEOUT_S},
        ) from exc
    if result.returncode != 0:
        return None  # an absent /boot/config-<ver> is not an error; caller omits the config
    return result.stdout or None


DEFAULT_KERNEL_CONFIG_PROBE: KernelConfigProbeSeam = probe_kernel_config
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/images/planes/test_build_common.py::test_kernel_config_probe_default_is_the_real_probe -v`
Expected: PASS.

- [ ] **Step 5: Lint + type**

Run: `just lint && just type`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/images/planes/_build_common.py tests/images/planes/test_build_common.py
git commit -m "feat(images): add read-only probe_kernel_config guestfish seam (#1051)"
```

---

## Task 3: Capture default kernel version + config in the build plane

**Files:**
- Modify: `src/kdive/images/planes/base.py` (`RootfsBuildOutput`)
- Modify: `src/kdive/providers/local_libvirt/rootfs_build.py`
- Test: `tests/providers/local_libvirt/test_rootfs_build.py` (or the existing rootfs-build test)

**Interfaces:**
- Consumes: `probe_boot_entries` (existing), `probe_kernel_config` /
  `KernelConfigProbeSeam` / `DEFAULT_KERNEL_CONFIG_PROBE` (Task 2), `baseline_kernel_names`
  (existing).
- Produces: `RootfsBuildOutput.kernel_config: bytes | None`;
  `provenance["default_kernel_version"]` (present only when a single non-rescue kernel);
  `RootfsBuildTools.probe_kernel_config` seam.

- [ ] **Step 1: Add the `RootfsBuildOutput` field (with the test that reads it)**

In `src/kdive/images/planes/base.py`, `RootfsBuildOutput`, add after `provenance`:

```python
    kernel_config: bytes | None = None
```

Add to its docstring's Attributes: `` kernel_config: The image's extracted
``/boot/config-<ver>`` bytes, or ``None`` when no single baseline kernel / no config file /
a probe failure — publish stores it best-effort. ``

- [ ] **Step 2: Write the failing capture tests**

In the rootfs-build test module (mirror the existing `boot_kernel_count` capture tests —
find them with `rg -n "boot_kernel_count" tests/providers/local_libvirt/`), add tests that
inject fakes via `RootfsBuildTools`. Use the module's existing build fixture/harness; the
shape is:

```python
def test_single_kernel_captures_version_and_config(build_plane_factory):
    tools = _tools_with(
        probe_boot_entries=lambda _p: ["vmlinuz-6.11.4-301.fc41.x86_64", "config-6.11.4-301.fc41.x86_64"],
        probe_kernel_config=lambda _p, ver: f"# config for {ver}\nCONFIG_X=y\n",
    )
    output = build_plane_factory(tools=tools).build(_spec())
    assert output.provenance["default_kernel_version"] == "6.11.4-301.fc41.x86_64"
    assert output.kernel_config == b"# config for 6.11.4-301.fc41.x86_64\nCONFIG_X=y\n"


def test_multi_kernel_omits_version_and_config(build_plane_factory):
    tools = _tools_with(
        probe_boot_entries=lambda _p: ["vmlinuz-6.11.4-301.fc41.x86_64", "vmlinuz-6.10.0-1.fc41.x86_64"],
        probe_kernel_config=lambda _p, ver: "SHOULD-NOT-BE-CALLED",
    )
    output = build_plane_factory(tools=tools).build(_spec())
    assert "default_kernel_version" not in output.provenance
    assert output.kernel_config is None


def test_config_absent_keeps_version_drops_config(build_plane_factory):
    tools = _tools_with(
        probe_boot_entries=lambda _p: ["vmlinuz-6.11.4-301.fc41.x86_64"],
        probe_kernel_config=lambda _p, ver: None,
    )
    output = build_plane_factory(tools=tools).build(_spec())
    assert output.provenance["default_kernel_version"] == "6.11.4-301.fc41.x86_64"
    assert output.kernel_config is None
```

The names above are illustrative — use the module's **real** helpers:
`tests/providers/local_libvirt/test_rootfs_build.py` exposes `_plane(tmp_path, rec,
probe_boot_entries=..., ...)` and a `_tools(...)`/`RootfsBuildTools` builder (the existing
`boot_kernel_count` tests use `_plane` around lines 391–417). Add the new
`probe_kernel_config=` seam to that harness and drive it through `_plane`. `_capture_boot_
kernel_count` has no external/test callers, so replacing it with `_capture_boot_facts`
breaks nothing.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_rootfs_build.py -k "kernel" -v`
Expected: FAIL (`probe_kernel_config` not a `RootfsBuildTools` field; provenance key missing).

- [ ] **Step 4: Wire the seam + implement the capture**

In `src/kdive/providers/local_libvirt/rootfs_build.py`:

1. Import the seam alongside the existing probe imports:

```python
from kdive.images.planes._build_common import (
    DEFAULT_BOOT_ENTRIES_PROBE,
    DEFAULT_KERNEL_CONFIG_PROBE,
    DEFAULT_MAKEDUMPFILE_PROBE,
    DEFAULT_OS_RELEASE_PROBE,
    DEFAULT_VERSION_INSPECT,
    BootEntriesProbeSeam,
    KernelConfigProbeSeam,
    MakedumpfileProbeSeam,
    OsReleaseProbeSeam,
    VersionInspectSeam,
    build_workspace,
    digest_file,
    publish_qcow2,
    run_guestfs_tool,
    validate_image_name,
)
```

2. Add the seam to `RootfsBuildTools`:

```python
    probe_kernel_config: KernelConfigProbeSeam = DEFAULT_KERNEL_CONFIG_PROBE
```

3. Replace the single `boot_kernel_count = self._capture_boot_kernel_count(scratch)` call
   in `build()` with one boot-facts capture that reuses one `/boot` listing:

```python
            boot_facts = self._capture_boot_facts(scratch)
            qcow2 = publish_qcow2(self._workspace, image_name=spec.name, scratch=staged)
        digest = digest_file(qcow2)
        return RootfsBuildOutput(
            qcow2_path=qcow2,
            digest=digest,
            kernel_config=boot_facts.kernel_config,
            provenance=_provenance(
                spec,
                entry,
                family,
                size=self._size,
                package_versions=package_versions,
                makedumpfile_version=makedumpfile_version,
                boot_kernel_count=boot_facts.boot_kernel_count,
                default_kernel_version=boot_facts.default_kernel_version,
                os_release=self._capture_os_release(scratch),
            ),
        )
```

4. Replace `_capture_boot_kernel_count` with `_capture_boot_facts`, plus a small result
   type at module scope:

```python
@dataclass(frozen=True, slots=True)
class _BootFacts:
    """Facts derived from one read-only ``/boot`` listing (ADR-0295/0317)."""

    boot_kernel_count: int | None
    default_kernel_version: str | None
    kernel_config: bytes | None


def _default_kernel_version(entries: list[str]) -> str | None:
    """The lone non-rescue ``vmlinuz-<ver>`` version in ``entries``, else ``None`` (ambiguous)."""
    kernels = baseline_kernel_names(entries)
    if len(kernels) != 1:
        return None
    return kernels[0][len("vmlinuz-") :]
```

and the method:

```python
    def _capture_boot_facts(self, scratch: Path) -> _BootFacts:
        """Boot facts from one ``/boot`` listing: kernel count, default version, and config.

        Lists ``/boot`` once via the injected probe and derives (a) ``boot_kernel_count`` via
        ``baseline_kernel_names`` (ADR-0295), (b) the ``default_kernel_version`` — the lone
        non-rescue kernel, else ``None`` when zero/many (ambiguous), and (c) the
        ``/boot/config-<ver>`` bytes for that version via ``probe_kernel_config`` (ADR-0317).
        Advisory: any probe failure degrades every fact to absent so the build still publishes.
        """
        try:
            entries = self._tools.probe_boot_entries(scratch)
        except CategorizedError:
            _log.warning("boot-entries probe failed; provenance omits boot facts")
            return _BootFacts(None, None, None)
        if entries is None:
            return _BootFacts(None, None, None)
        count = len(baseline_kernel_names(entries))
        version = _default_kernel_version(entries)
        config = self._capture_kernel_config(scratch, version)
        return _BootFacts(count, version, config)

    def _capture_kernel_config(self, scratch: Path, version: str | None) -> bytes | None:
        """The image's ``/boot/config-<version>`` bytes, or ``None`` (ADR-0317).

        Only probed when ``version`` is known (a single baseline kernel). Advisory: a probe
        failure or an absent config degrades to ``None`` so the build still publishes.
        """
        if version is None:
            return None
        try:
            text = self._tools.probe_kernel_config(scratch, version)
        except CategorizedError:
            _log.warning("kernel-config probe failed; provenance omits the config offer")
            return None
        return text.encode("utf-8") if text is not None else None
```

5. Add `default_kernel_version: str | None` to `_provenance`'s signature and body (mirror
   `boot_kernel_count`):

```python
def _provenance(
    spec: RootfsBuildSpec,
    entry: RootfsCatalogEntry,
    family: FamilyCustomizer,
    *,
    size: str,
    package_versions: dict[str, str],
    makedumpfile_version: str | None,
    boot_kernel_count: int | None,
    default_kernel_version: str | None,
    os_release: dict[str, str] | None,
) -> dict[str, object]:
```

and, alongside the other conditional appends:

```python
    if default_kernel_version:
        record["default_kernel_version"] = default_kernel_version
```

Extend the `_provenance` docstring with a `default_kernel_version` sentence in the same
advisory style as the `boot_kernel_count` sentence (present when a single baseline kernel;
omitted otherwise so a degraded row stays byte-identical to a pre-feature one, ADR-0317).

- [ ] **Step 5: Run the capture tests**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_rootfs_build.py -k "kernel or boot" -v`
Expected: PASS (including the existing `boot_kernel_count` tests, now sourced from
`_capture_boot_facts`).

- [ ] **Step 6: Lint + type + focused test**

Run: `just lint && just type && uv run python -m pytest tests/providers/local_libvirt/test_rootfs_build.py -q`
Expected: clean + PASS.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/images/planes/base.py src/kdive/providers/local_libvirt/rootfs_build.py tests/providers/local_libvirt/test_rootfs_build.py
git commit -m "feat(images): capture default kernel version + config at build (#1051)"
```

---

## Task 4: Best-effort config write in publish + leaked-sweep protection

**Ordering invariant:** the leaked-sweep protection (`_delete_if_leaked` cross-check on
`kernel_config_key`) is committed **in this same task**, before/with the first config-object
write. The ADR requires the protector to exist the instant a config object can exist —
otherwise a config object older than the publish grace, in the window between "write ships"
and "protection ships", is deleted as leaked. Do not split these across commits.

**Files:**
- Modify: `src/kdive/services/images/publish.py`
- Modify: `src/kdive/reconciler/cleanup/images.py` (leaked cross-check — ships with the write)
- Test: `tests/services/images/test_publish.py`, `tests/reconciler/test_image_sweeps.py`

**Interfaces:**
- Consumes: `PublishRequest` (existing), `_FakeStore` test double (existing, `put_artifact`
  supports `fail_put`/`drop_object`).
- Produces: `PublishRequest.kernel_config: bytes | None`;
  `kernel_config_object_key(request) -> str`; publish sets `kernel_config_key` on the
  `pending` row at adopt/insert and clears it at the `registered` flip on a config-write
  failure.

- [ ] **Step 1: Write the failing tests**

In `tests/services/images/test_publish.py`:

```python
_CONFIG = b"# CONFIG_X is not set\nCONFIG_Y=y\n"


async def test_publish_writes_config_object_and_key(...):  # reuse the module's conn fixture
    store = _FakeStore()
    request = _request(kernel_config=_CONFIG)  # helper that fills the base fields + config
    entry = await publish_image(conn, store, request=request, source=_write_qcow2(tmp_path))
    from kdive.services.images.publish import kernel_config_object_key
    ckey = kernel_config_object_key(request)
    assert entry.kernel_config_key == ckey
    assert store._objects[ckey] == _CONFIG


async def test_publish_without_config_sets_no_key(...):
    store = _FakeStore()
    entry = await publish_image(conn, store, request=_request(kernel_config=None), source=...)
    assert entry.kernel_config_key is None
    assert len(store._objects) == 1  # qcow2 only


async def test_config_write_failure_registers_without_config(...):
    store = _FakeStore(fail_put=True_for_config_only)  # see Step 3 note
    entry = await publish_image(conn, store, request=_request(kernel_config=_CONFIG), source=...)
    assert entry.state is ImageState.REGISTERED
    assert entry.kernel_config_key is None  # cleared on best-effort failure
```

Note: `_FakeStore`'s `fail_put` fails *every* put. For the config-only-failure test, extend
`_FakeStore` with a `fail_keys_suffix` (e.g. `".config"`) that raises only for that suffix,
or add a `fail_config: bool` that raises when the key ends with `.config`. Keep the qcow2
put succeeding so the test isolates the config leg.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/services/images/test_publish.py -k "config" -v`
Expected: FAIL (`PublishRequest` has no `kernel_config`; no `kernel_config_object_key`).

- [ ] **Step 3: Implement**

In `src/kdive/services/images/publish.py`:

1. Add the field to `PublishRequest` (after `expires_at`): `kernel_config: bytes | None = None`
   and document it in the Attributes block (the extracted `/boot/config-<ver>` bytes, best-effort).

2. Add the deterministic key helper next to `image_object_key`:

```python
def kernel_config_object_key(request: PublishRequest) -> str:
    """The object-store key for the image's ``/boot/config-<ver>`` sibling of the qcow2.

    Same tenant/owner scoping as :func:`image_object_key`; the ``.config`` suffix distinguishes
    it from the ``{arch}.qcow2`` object. ``None`` config means no key and no object.
    """
    return artifact_types.ArtifactWriteRequest(
        tenant="images",
        owner_kind=_object_owner_kind(request),
        owner_id=request.name,
        name=f"{request.arch}.config",
        data=b"",
        sensitivity=Sensitivity.REDACTED,
        retention_class=_RETENTION_CLASS,
    ).key()
```

3. Thread `kernel_config_key` through adopt/insert. In `_adopt_or_insert_pending`, compute
   the config key when present and set it on both the adopt-UPDATE and the insert. Change the
   signature to also accept `config_key: str | None`, set it in the adopt UPDATE:

```python
            await cur.execute(
                "UPDATE image_catalog "
                "SET state = %s, object_key = %s, kernel_config_key = %s, pending_since = now() "
                "WHERE id = %s",
                (ImageState.PENDING.value, object_key, config_key, existing["id"]),
            )
```

and pass `config_key` into `_insert_pending`, whose INSERT adds the `kernel_config_key`
column/param (value `config_key`).

4. In `publish_image`, compute the config key and best-effort-write the object:

```python
    object_key = image_object_key(request)
    config_key = kernel_config_object_key(request) if request.kernel_config is not None else None
    row_id = await _adopt_or_insert_pending(conn, request, object_key, config_key)

    data = await asyncio.to_thread(source.read_bytes)
    _verify_source_digest(data, request.digest)
    await _write_object(store, request, data)

    head = await asyncio.to_thread(store.head, object_key)
    if head is None:
        raise CategorizedError(... INFRASTRUCTURE_FAILURE ...)  # unchanged, qcow2 fatal

    config_written = await _write_config_best_effort(store, request, config_key)
    return await _registered(conn, row_id, clear_config_key=not config_written)
```

5. Add the best-effort writer (config object via `put_artifact` with the `.config` name +
   a HEAD gate; any `CategorizedError` or missing HEAD returns `False`, logged):

```python
async def _write_config_best_effort(
    store: ImageObjectStore, request: PublishRequest, config_key: str | None
) -> bool:
    """Write the config sibling object; return whether it is present. Never raises (advisory)."""
    if config_key is None or request.kernel_config is None:
        return False
    write = artifact_types.ArtifactWriteRequest(
        tenant="images",
        owner_kind=_object_owner_kind(request),
        owner_id=request.name,
        name=f"{request.arch}.config",
        data=request.kernel_config,
        sensitivity=Sensitivity.REDACTED,
        retention_class=_RETENTION_CLASS,
    )
    try:
        await asyncio.to_thread(store.put_artifact, write)
        head = await asyncio.to_thread(store.head, config_key)
    except CategorizedError:
        _log.warning("image kernel-config write failed; registering with no config offered")
        return False
    if head is None:
        _log.warning("image kernel-config object absent after write; no config offered")
        return False
    return True
```

Add `import logging` + `_log = logging.getLogger(__name__)` if not present.

6. Extend `_registered` to clear the key on failure:

```python
async def _registered(
    conn: AsyncConnection, row_id: UUID, *, clear_config_key: bool = False
) -> ImageCatalogEntry:
    async with conn.cursor(row_factory=dict_row) as cur:
        if clear_config_key:
            await cur.execute(
                "UPDATE image_catalog SET state = %s, kernel_config_key = NULL "
                "WHERE id = %s RETURNING *",
                (ImageState.REGISTERED.value, row_id),
            )
        else:
            await cur.execute(
                "UPDATE image_catalog SET state = %s WHERE id = %s RETURNING *",
                (ImageState.REGISTERED.value, row_id),
            )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"image_catalog row {row_id} vanished before registration")
    return ImageCatalogEntry.model_validate(row)
```

- [ ] **Step 4: Run the publish tests**

Run: `uv run python -m pytest tests/services/images/test_publish.py -q`
Expected: PASS (new config tests + all existing publish tests).

- [ ] **Step 5: Lint + type**

Run: `just lint && just type`
Expected: clean.

- [ ] **Step 6: Add the leaked-sweep protection (same commit) + its tests**

The config object lives under the `images/` prefix, so `repair_leaked_images` lists it and
would delete it as "leaked" (its key is in `kernel_config_key`, never `object_key`). Ship the
protection now. In `tests/reconciler/test_image_sweeps.py` add:

```python
async def test_leaked_sweep_protects_live_image_config(...):
    # registered row with object_key=K_qcow2, kernel_config_key=K_cfg; both objects past grace
    deleted = await repair_leaked_images(conn, store, grace)
    assert store.deleted == []  # both protected via object_key OR kernel_config_key


async def test_leaked_sweep_reclaims_orphaned_config(...):
    # object K_cfg present past grace but NO row references it -> deleted
    await repair_leaked_images(conn, store, grace)
    assert K_cfg in store.deleted
```

Run: `uv run python -m pytest tests/reconciler/test_image_sweeps.py -k "leaked" -v`
Expected: FAIL (live config deleted).

In `src/kdive/reconciler/cleanup/images.py`, `_delete_if_leaked`, change the query:

```python
        await cur.execute(
            "SELECT EXISTS (SELECT 1 FROM image_catalog "
            "WHERE object_key = %s OR kernel_config_key = %s) OR %s >= now() - %s",
            (obj.key, obj.key, obj.last_modified, grace),
        )
```

Update the module docstring's `repair_leaked_images` bullet to note an object is protected
when referenced by `object_key` **or** `kernel_config_key`.

Run: `uv run python -m pytest tests/reconciler/test_image_sweeps.py -q`
Expected: PASS (new + existing sweep tests).

- [ ] **Step 7: Lint + type**

Run: `just lint && just type`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/kdive/services/images/publish.py src/kdive/reconciler/cleanup/images.py tests/services/images/test_publish.py tests/reconciler/test_image_sweeps.py
git commit -m "feat(images): best-effort publish of the kernel .config sibling (#1051)"
```

---

## Task 5: Thread the config through the build-job handler

**Files:**
- Modify: `src/kdive/jobs/handlers/image_build.py`
- Test: `tests/jobs/test_image_build_handler.py` (existing handler test; fakes the build plane + store, constructs `RootfsBuildOutput`)

**Interfaces:**
- Consumes: `RootfsBuildOutput.kernel_config` (Task 3), `PublishRequest.kernel_config` (Task 4).

- [ ] **Step 1: Write the failing test**

In the image-build handler test, extend the success-path test (or add one) so the fake
build plane returns a `RootfsBuildOutput` with `kernel_config=b"CONFIG_X=y\n"` and assert the
`PublishRequest` seen by a spy `publish` (or the fake store's `.config` object) carries it.
Mirror the module's existing handler-test harness (it already fakes the build plane +
store).

```python
async def test_handler_passes_kernel_config_to_publish(...):
    # fake build plane returns output with kernel_config set; capture the PublishRequest
    assert captured_request.kernel_config == b"CONFIG_X=y\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/jobs/test_image_build_handler.py -k "kernel_config" -v`
Expected: FAIL (request carries `None`).

- [ ] **Step 3: Implement**

In `src/kdive/jobs/handlers/image_build.py`, add to the `PublishRequest(...)` construction
in `image_build_handler`:

```python
        kernel_config=output.kernel_config,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/jobs/test_image_build_handler.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/jobs/handlers/image_build.py tests/jobs/test_image_build_handler.py
git commit -m "feat(images): thread kernel_config from build output to publish (#1051)"
```

---

## Task 6: Delete the config object on private-image expiry

(The leaked-sweep protection ships in Task 4 — see its Step 6. This task adds only the
prompt eager-delete on private-image expiry; a dangling/prune row leaves the config for the
Task 4 leaked-sweep backstop.)

**Files:**
- Modify: `src/kdive/services/images/retention.py` (private-expiry delete)
- Test: `tests/reconciler/test_image_sweeps.py`

**Interfaces:**
- Consumes: `ImageSweepStore` (existing: `list_image_objects`/`head_present`/`delete`).
- **Existing call sites of `expire_one_private_image` that gain the new arg:**
  `src/kdive/services/images/retention.py:60` (production) and three tests —
  `tests/reconciler/test_image_sweeps.py:398`, `:421`, `:445` (each calls
  `_expire_one_private_image(conn, store, row_id, key)` today; each must pass the new
  `config_key` — pass `None` where the test seeds no config).

- [ ] **Step 1: Write the failing test**

In `tests/reconciler/test_image_sweeps.py`:

```python
async def test_private_expiry_deletes_config(...):
    # expired private row with object_key + kernel_config_key -> both objects deleted, row gone
    pruned = await repair_expired_private_images(conn, store)
    assert {qcow2_key, config_key} <= set(store.deleted)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/reconciler/test_image_sweeps.py -k "private_expiry_deletes_config" -v`
Expected: FAIL (expiry leaves the config object).

- [ ] **Step 3: Implement the private-expiry delete**

In `src/kdive/services/images/retention.py`:

- `repair_expired_private_images`: select `kernel_config_key` too and pass it in:

```python
        await cur.execute(
            "SELECT id, object_key, kernel_config_key FROM image_catalog "
            "WHERE visibility = %s AND expires_at IS NOT NULL AND expires_at < now()",
            (_PRIVATE_VISIBILITY,),
        )
        candidates = await cur.fetchall()
    pruned = 0
    for cand in candidates:
        if await expire_one_private_image(
            conn, store, cand["id"], cand["object_key"], cand["kernel_config_key"]
        ):
            pruned += 1
```

- `expire_one_private_image`: add `config_key: str | None` param and delete it alongside the
  qcow2 (object-before-row, matching the existing order):

```python
async def expire_one_private_image(
    conn: AsyncConnection,
    store: ImageSweepStore,
    row_id: UUID,
    object_key: str | None,
    config_key: str | None,
) -> bool:
    ...
        if object_key is not None:
            await asyncio.to_thread(store.delete, object_key)
        if config_key is not None:
            await asyncio.to_thread(store.delete, config_key)
        await cur.execute("DELETE FROM image_catalog WHERE id = %s", (row_id,))
```

Update the three existing test call sites named in **Interfaces** above
(`tests/reconciler/test_image_sweeps.py:398/421/445`) to pass `None` as the new
`config_key` argument — they seed no config, so `_expire_one_private_image(conn, store,
row_id, key, None)`. (Confirm with `rg -n "expire_one_private_image" src/ tests/` that no
other caller remains.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/reconciler/test_image_sweeps.py -q`
Expected: PASS (new config test + existing expiry tests with the updated arity).

- [ ] **Step 5: Lint + type**

Run: `just lint && just type`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/services/images/retention.py tests/reconciler/test_image_sweeps.py
git commit -m "feat(images): delete the kernel .config object on private expiry (#1051)"
```

---

## Task 7: Surface `default_kernel_version` in `images.list`/`describe`

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/images.py`
- Test: `tests/mcp/catalog/test_images_list.py`, `tests/mcp/catalog/test_images_describe.py`

**Interfaces:**
- Consumes: `provenance["default_kernel_version"]` (Task 3).
- Produces: `data.default_kernel_version` on the list-row and describe envelopes.

- [ ] **Step 1: Write the failing tests**

In `tests/mcp/catalog/test_images_describe.py` and `..._list.py`, add a case whose seeded
row's `provenance` includes `{"default_kernel_version": "6.11.4-301.fc41.x86_64"}` and
assert `data.default_kernel_version` equals it; and a row without it asserts `""`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/catalog/test_images_describe.py tests/mcp/catalog/test_images_list.py -k "kernel" -v`
Expected: FAIL (`default_kernel_version` not in `data`).

- [ ] **Step 3: Implement**

In `src/kdive/mcp/tools/catalog/images.py`, add a projector next to `_compact_os`:

```python
def _default_kernel_version(provenance: dict[str, Any]) -> str:
    """The build-recorded default kernel version, or ``"" `` when absent (ADR-0317)."""
    value = provenance.get("default_kernel_version")
    return str(value) if value else ""
```

Add `"default_kernel_version": _default_kernel_version(entry.provenance),` to both
`_row_envelope`'s `data` and `_describe_envelope`'s `data`. Update the `images_list` and
`images_describe` **wrapper docstrings** to name `data.default_kernel_version` (the image's
default kernel version for informed selection).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/catalog/test_images_describe.py tests/mcp/catalog/test_images_list.py -q`
Expected: PASS.

- [ ] **Step 5: Docs regen + commit**

Run: `just docs-check` — if it fails, run the generator it names (regenerate the tool
reference) and re-run until green.

```bash
git add src/kdive/mcp/tools/catalog/images.py tests/mcp/catalog/test_images_describe.py tests/mcp/catalog/test_images_list.py docs/
git commit -m "feat(mcp): surface default_kernel_version on images.list/describe (#1051)"
```

---

## Task 8: `images.kernel_config` read tool

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/images.py`
- Test: `tests/mcp/catalog/test_images_kernel_config.py` (create)

**Interfaces:**
- Consumes: the `images.describe` visibility predicate (`_DESCRIBE_SQL`), `object_store_from_env`,
  `ARTIFACT_DOWNLOAD_TTL_SECONDS`, `ToolResponse`, `_as_uuid`/`_not_found`/`_config_error`.
- Produces: MCP tool `images.kernel_config` + async `kernel_config` handler.

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/catalog/test_images_kernel_config.py`. Mirror
`tests/mcp/catalog/test_images_describe.py`'s fixtures + a fake store exposing
`head`/`presign_get` (reuse `raw_fetch`'s test-double shape). Cases:

```python
async def test_present_config_returns_download_uri(...):
    # registered row with kernel_config_key set + object present, provenance has version
    resp = await kernel_config(pool, ctx, image_id)
    assert resp.refs["download_uri"] == "https://signed/…"
    assert resp.data["default_kernel_version"] == "6.11.4-301.fc41.x86_64"
    assert resp.data["size_bytes"] == 42


async def test_no_key_is_unavailable(...):
    resp = await kernel_config(pool, ctx, image_id_without_key)
    assert resp.status is a failure and resp.data["reason"] == "kernel_config_unavailable"


async def test_object_absent_is_unavailable(...):
    # key set but store.head -> None
    assert resp.data["reason"] == "kernel_config_unavailable"


async def test_invisible_private_is_not_found(...):
    # private row the caller cannot view -> not_found (byte-identical to absent)


async def test_malformed_id_is_config_error(...):
    resp = await kernel_config(pool, ctx, "not-a-uuid")
    # configuration_error
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/catalog/test_images_kernel_config.py -v`
Expected: FAIL (`kernel_config` handler does not exist).

- [ ] **Step 3: Implement the handler + tool**

In `src/kdive/mcp/tools/catalog/images.py`:

- Add imports: `object_store_from_env` (from `kdive.store.objectstore`); `import
  kdive.config as config` + `ARTIFACT_DOWNLOAD_TTL_SECONDS` (from
  `kdive.config.core_settings`, as `raw_fetch` does); `HeadResult` (from
  `kdive.artifacts.storage`); `asyncio`; **and the plain config-error helper the handler
  uses — `from kdive.mcp.tools._common import config_error as _config_error`** (the module
  imports only `config_error_reason as _config_error_reason` today, not the plain alias).
  Define a `_ConfigStore` `Protocol` mirroring `raw_fetch._RawStore` (`head(key) ->
  HeadResult | None` and `presign_get(key, *, expires_in: int) -> str`).

- Add the handler (visibility via the existing `_DESCRIBE_SQL`; note it selects `*`, so the
  row carries `kernel_config_key` and `provenance`):

```python
_KERNEL_CONFIG_TOOL = "images.kernel_config"
_KERNEL_CONFIG_UNAVAILABLE = "kernel_config_unavailable"


async def kernel_config(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    image_id: str,
    *,
    store_factory: Callable[[], _ConfigStore] = object_store_from_env,
) -> ToolResponse:
    """Mint a presigned download URL for a catalog image's kernel ``.config`` (ADR-0317).

    Resolves the row under the ``images.describe`` visibility predicate, HEADs the stored
    ``/boot/config-<ver>`` object, and presigns a short-lived GET. A row with no config
    (staged/pre-feature/absent) or a missing object is a ``configuration_error`` with reason
    ``kernel_config_unavailable``; the config is never inspected or validated.
    """
    uid = _as_uuid(image_id)
    if uid is None:
        return _invalid_uuid_error("image_id", image_id)
    with bind_context(principal=ctx.principal):
        params = {
            "id": str(uid),
            "public": ImageVisibility.PUBLIC.value,
            "private": ImageVisibility.PRIVATE.value,
            "projects": projects_with_role(ctx, Role.VIEWER),
        }
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_DESCRIBE_SQL, params)
            row = await cur.fetchone()
    if row is None:
        return _not_found(image_id)
    entry = ImageCatalogEntry.model_validate(row)
    if entry.kernel_config_key is None:
        return _config_error(image_id, data={"reason": _KERNEL_CONFIG_UNAVAILABLE})
    try:
        store = store_factory()
        head = await asyncio.to_thread(store.head, entry.kernel_config_key)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(image_id, exc)
    if head is None:
        return _config_error(image_id, data={"reason": _KERNEL_CONFIG_UNAVAILABLE})
    ttl = config.require(ARTIFACT_DOWNLOAD_TTL_SECONDS)
    try:
        url = await asyncio.to_thread(store.presign_get, entry.kernel_config_key, expires_in=ttl)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(image_id, exc)
    return ToolResponse.success(
        image_id,
        "available",
        suggested_next_actions=[_KERNEL_CONFIG_TOOL],
        refs={"download_uri": url},
        data={
            "default_kernel_version": _default_kernel_version(entry.provenance),
            "size_bytes": head.size_bytes,
            "ttl": ttl,
        },
    )
```

**Step 3 note (reason value):** the no-key and object-absent cases both use the free-form
`_config_error(image_id, data={"reason": _KERNEL_CONFIG_UNAVAILABLE})` — the same
free-string idiom `raw_fetch` uses for `vmcore_unavailable` (no reason enum needed).

- Register the tool inside the existing `register(app, pool)`:

```python
    @app.tool(
        name=_KERNEL_CONFIG_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def images_kernel_config(
        image_id: Annotated[str, Field(description="The catalog image row id (UUID).")],
    ) -> ToolResponse:
        """Return a short-lived download URL for the image's kernel ``.config`` starting point.

        The URL under ``refs.download_uri`` fetches the image's ``/boot/config-<ver>`` — a
        known-good config to build from, never validated by kdive. ``data.default_kernel_version``
        names the version. An image with no offered config returns a ``configuration_error`` with
        ``data.reason`` = ``kernel_config_unavailable``.
        """
        return await kernel_config(pool, current_context(), image_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/catalog/test_images_kernel_config.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + type + docs regen**

Run: `just lint && just type && just docs-check` (regen the tool reference if docs-check
fails — the new tool must appear in the generated reference). Re-run until green.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/tools/catalog/images.py tests/mcp/catalog/test_images_kernel_config.py docs/
git commit -m "feat(mcp): add images.kernel_config presigned-URL read tool (#1051)"
```

---

## Task 9: No-validation guard test + full guardrail sweep

**Files:**
- Test: `tests/services/images/test_publish.py` (or `tests/mcp/catalog/test_images_kernel_config.py`)

**Interfaces:** none new — a behavior assertion over Tasks 4/8.

- [ ] **Step 1: Write the no-validation test**

Assert an arbitrary config (bytes the old server-build gate would have rejected, e.g.
`b"# CONFIG_SQUASHFS is not set\n"`) round-trips through publish → `images.kernel_config`
unchanged and unchecked: publish stores it, the fetch mints a URL, and no code path parses
or rejects it.

```python
async def test_arbitrary_config_is_stored_unvalidated(...):
    weird = b"# CONFIG_SQUASHFS is not set\nCONFIG_NONSENSE=42\n"
    entry = await publish_image(conn, store, request=_request(kernel_config=weird), source=...)
    assert store._objects[entry.kernel_config_key] == weird  # byte-identical, unvalidated
```

- [ ] **Step 2: Run it**

Run: `uv run python -m pytest tests/services/images/test_publish.py -k "unvalidated" -v`
Expected: PASS.

- [ ] **Step 3: Full guardrail sweep**

Run: `just lint && just type && just test && just docs-check`
Expected: all green. (`just test` excludes `live_vm`; the real guestfish probe is
`pragma: no cover - live_vm` and covered by the injected fakes.)

- [ ] **Step 4: Commit**

```bash
git add tests/services/images/test_publish.py
git commit -m "test(images): assert the offered config is stored unvalidated (#1051)"
```

---

## Self-Review notes

- **Spec coverage:** AC1 (default kernel version on `images.describe`) → Tasks 3+7; AC2
  (MCP method returns the `.config`) → Tasks 2–5+8; AC3 (no validation) → Tasks 4/8/9 +
  the absence of any parse/gate. Config-object lifecycle (spec Decisions 6–8) → Tasks 4+6.
  Migration/model → Task 1.
- **Ordering:** Task 1 (schema/model) precedes every reader/writer of the column; Task 2
  (probe) precedes Task 3 (capture); Tasks 3→4→5 form the build→publish→handler chain.
  **The leaked-sweep `kernel_config_key` protection ships inside Task 4, in the same commit
  as the first config-object write** — the protector must exist the instant a config object
  can exist (ADR-0317 protect-before-write invariant), so it is never a strictly-later
  commit. Task 6 adds only the prompt private-expiry delete (dangling/prune rows rely on
  the Task 4 leaked-sweep backstop). Tasks 7–8 depend on the column + provenance.
  Guardrails stay green at each commit because each task is self-contained and additive.
- **Type consistency:** `kernel_config: bytes | None` is the wire type end-to-end
  (`RootfsBuildOutput` → `PublishRequest`); the probe returns `str | None` (text) and the
  build plane encodes to `bytes` (`_capture_kernel_config`); `kernel_config_key: str | None`
  is the persisted key; `default_kernel_version` is a `str` in provenance and a `str` (`""`
  when absent) in the MCP `data`.
