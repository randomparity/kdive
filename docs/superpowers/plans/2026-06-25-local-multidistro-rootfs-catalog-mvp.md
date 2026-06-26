# Multi-distro local rootfs catalog — MVP (#817) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix #817 by shipping a Fedora 44 local rootfs whose makedumpfile (1.7.9) captures a complete kernel-7.0 vmcore via the default `kdump` method, built through a new declarative multi-distro rootfs catalog with a per-family customizer seam, plus incomplete-core disclosure on the harvest.

**Architecture:** Replace the single hardcoded Fedora `build-fs` path with: a file-authoritative `rootfs_catalog.toml`, a loader (`images/rootfs_catalog.py`), a base-source acquirer (`images/base_source.py`, virt-builder template OR sha256-pinned cloud-image URL), and a `FamilyCustomizer` seam (`images/families/`, MVP ships `rhel`). The harvest in `providers/local_libvirt/retrieve.py` detects `vmcore-incomplete` and returns a cause-neutral `READINESS_FAILURE`.

**Tech Stack:** Python 3.14, `uv`/`ruff`/`ty`/`pytest`; libguestfs (`virt-builder`, `virt-customize`, `virt-tar-out`, `virt-make-fs`, `guestfish`); libvirt/QEMU; tomllib.

## Global Constraints

- Python 3.14, managed with `uv`. Tests under `tests/` mirroring the package tree.
- Guardrails (run before every commit): `just lint` (ruff check + format), `just type` (ty whole-tree), `just test` (excludes `live_vm`). Single test: `uv run python -m pytest <path>::<name> -q`.
- `ty` is whole-tree (src + tests) — never narrow it.
- ≤100 lines/function, cyclomatic ≤8, ≤5 positional params, 100-char lines, absolute imports only, Google-style docstrings on non-trivial public APIs.
- Return `CategorizedError` with the most specific `ErrorCategory`; never invent strings. Fail-closed on untrusted input.
- Doc/comment prose: no "robust"/"comprehensive"/"critical"/"significant"/"elegant"; use **Milestone** not "Sprint".
- `live_vm`-marked tests are gated and only run on this KVM host; never un-gate them.
- Branch: `feat/local-kdump-in-guest-817` (already created). Conventional commits ending with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## Task 1: De-risk spike — hand-build & kdump-prove Fedora 44 (live, gates everything)

The cloud-image→bare-ext4 path was never exercised in diagnosis. Prove it manually before writing production code. **No production code in this task** — it produces a working `fedora-kdive-ready-44.qcow2` and the exact `virt-customize` argv that works, which Task 4 encodes.

**Files:** none committed (scratchpad scripts + the built qcow2 staged to `/var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2`).

- [ ] **Step 1: Download + verify the F44 cloud base.**
  Download `https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2`, fetch the matching `Fedora-Cloud-44-1.7-x86_64-CHECKSUM`, and record the sha256 (it becomes the catalog pin in Task 2).

- [ ] **Step 2: Inspect the base layout.**
  Run `virt-filesystems -a <base> --long --parts --filesystems`. Confirm whether root is btrfs (expected for Fedora Cloud) and note `/boot`, ESP. This determines the repack handling.

- [ ] **Step 3: Customize via virt-customize.**
  On a copy of the base, run `virt-customize -a <copy>` with: `--install drgn,kexec-tools,makedumpfile,kdump-utils,keyutils,openssh-server`; `--run-command 'systemctl enable sshd.service'`; `--run-command 'systemctl enable kdump.service'`; disable cloud-init (`--run-command 'systemctl mask cloud-init.service cloud-init-local.service cloud-config.service cloud-final.service'` or `--uninstall cloud-init`); write `/etc/sysctl.d/99-kdive-kdump.conf` = `kernel.unknown_nmi_panic=1`; set kdump `final_action poweroff`; `--ssh-inject root:file:<managed pubkey>`; upload+enable the `kdive-ready` oneshot unit; set SELinux permissive. Verify `makedumpfile --version` in-guest is ≥ 1.7.9.

- [ ] **Step 4: Repack to bare ext4 + normalize.**
  `virt-tar-out -a <copy> / <tar>` then `virt-make-fs --type=ext4 --format=qcow2 --size=10G <tar> <out>`; then guestfish-normalize: fstab → lone `/dev/vda / ext4`, remove crypttab, SELinux permissive, **and `setfiles`/`restorecon`-relabel** (tar→ext4 drops xattrs). Stage to `/var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2`.

- [ ] **Step 5: Boot + kdump-prove (the gate).**
  Reuse the diagnosis method: build the v7.0 kernel (`make defconfig` + `src/kdive/build_configs/data/kdump.config` merge), inject `/boot/vmlinuz-7.0.0` + `/lib/modules/7.0.0` into an overlay of the F44 image, direct-kernel boot at **≥8 GB RAM** (large enough that 1.7.8 would overrun — use the same size the live-proof will use), SSH in, confirm `kdumpctl status` = operational and `makedumpfile --version` ≥1.7.9, then `virsh inject-nmi` and watch the console.
  **Pass criteria:** console shows **no** "The kernel version is not supported" line; `/var/crash/<ts>/vmcore` (complete, filtered) exists in the overlay. **If it fails, stop and fix the customize/repack argv before any production code.**

- [ ] **Step 6: Record the working argv.** Save the exact `virt-customize` flag list and any repack/normalize deltas to the scratchpad; Task 4 encodes them in the `rhel` customizer. No commit.

---

## Task 2: Catalog loader + `rootfs_catalog.toml`

**Files:**
- Create: `fixtures/local-libvirt/rootfs_catalog.toml`
- Create: `src/kdive/images/rootfs_catalog.py`
- Test: `tests/images/test_rootfs_catalog.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True, slots=True) class VirtBuilderSource: template: str`
  - `@dataclass(frozen=True, slots=True) class CloudImageSource: url: str; sha256: str`
  - `type RootfsSource = VirtBuilderSource | CloudImageSource`
  - `@dataclass(frozen=True, slots=True) class RootfsCatalogEntry: name: str; distro: str; version: str; family: str; arch: str; kind: str; source: RootfsSource`
  - `def load_rootfs_catalog() -> dict[str, RootfsCatalogEntry]` (keyed by `name`; raises `CategorizedError` `CONFIGURATION_ERROR` on a malformed catalog)
  - `def resolve_rootfs_entry(name: str) -> RootfsCatalogEntry` (raises `CONFIGURATION_ERROR` naming `name` + available names when absent)
  - `_VALID_FAMILIES: frozenset[str] = frozenset({"rhel", "debian", "suse"})`

- [ ] **Step 1: Write the catalog file.**

```toml
# Local-libvirt rootfs image catalog (ADR-0250). File-authoritative: build-fs --image <name>
# resolves a row here. source.kind = "virt-builder" carries a template; "cloud-image" carries a
# sha256-pinned url. family selects the FamilyCustomizer (rhel|debian|suse).

[[image]]
name = "fedora-kdive-ready-43"
distro = "fedora"
version = "43"
family = "rhel"
arch = "x86_64"
kind = "debug"
source = { kind = "virt-builder", template = "fedora-43" }

[[image]]
name = "fedora-kdive-ready-44"
distro = "fedora"
version = "44"
family = "rhel"
arch = "x86_64"
kind = "debug"
source = { kind = "cloud-image", url = "https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2", sha256 = "<PIN FROM TASK 1 STEP 1>" }
```

- [ ] **Step 2: Write failing tests.**

```python
# tests/images/test_rootfs_catalog.py
import pytest
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.rootfs_catalog import (
    CloudImageSource, VirtBuilderSource, load_rootfs_catalog, resolve_rootfs_entry,
)

def test_loads_both_fedora_entries():
    cat = load_rootfs_catalog()
    assert {"fedora-kdive-ready-43", "fedora-kdive-ready-44"} <= set(cat)

def test_virt_builder_and_cloud_image_sources_parse():
    cat = load_rootfs_catalog()
    assert isinstance(cat["fedora-kdive-ready-43"].source, VirtBuilderSource)
    f44 = cat["fedora-kdive-ready-44"].source
    assert isinstance(f44, CloudImageSource) and f44.url.endswith(".qcow2") and len(f44.sha256) == 64

def test_resolve_unknown_name_is_config_error():
    with pytest.raises(CategorizedError) as e:
        resolve_rootfs_entry("nope")
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR
```

  Plus, using a `tmp_path` catalog injected via an optional `path` arg on `load_rootfs_catalog(path=...)`: unknown `family` → error; `source.kind` missing required field (cloud-image w/o sha256; virt-builder w/o template) → error; duplicate `name` → error; bad `source.kind` → error.

- [ ] **Step 3: Run tests, verify they fail.** `uv run python -m pytest tests/images/test_rootfs_catalog.py -q` → FAIL (module missing).

- [ ] **Step 4: Implement `rootfs_catalog.py`.** Parse with `tomllib`; default catalog path is the packaged fixture (`importlib.resources` or a repo-relative `Path` consistent with how `load_fixture_catalog` resolves `fixtures/local-libvirt/`). Validate each row; build typed sources by `source.kind`; reject duplicates; raise `CONFIGURATION_ERROR` with `details` naming the offending field (not its value, per the ref-error convention).

- [ ] **Step 5: Run tests, verify pass.** Then `just lint && just type`.

- [ ] **Step 6: Commit.** `feat(images): add declarative rootfs catalog loader (ADR-0250)`

---

## Task 3: Base-source acquirer

**Files:**
- Create: `src/kdive/images/base_source.py`
- Test: `tests/images/test_base_source.py`

**Interfaces:**
- Consumes: `VirtBuilderSource`, `CloudImageSource` (Task 2).
- Produces:
  - `type Downloader = Callable[[str, Path], None]` (url, dest)
  - `def acquire_base(source: RootfsSource, scratch: Path, *, releasever: str, arch: str, virt_builder: Callable[..., None], downloader: Downloader) -> None` — for `VirtBuilderSource` calls `virt_builder(template=..., output=scratch)`; for `CloudImageSource` calls `downloader(url, scratch)` then verifies sha256.
  - sha256 mismatch → `CONFIGURATION_ERROR` (`reason="base_sha256_mismatch"`); a `downloader` raising a not-found/HTTP error → `CONFIGURATION_ERROR` (`reason="base_unreachable"`, details name the url).

- [ ] **Step 1: Write failing tests** (inject fake `downloader`/`virt_builder`; never hit network):

```python
# tests/images/test_base_source.py — sketch
def test_cloud_image_sha256_match(tmp_path):
    data = b"qcow2-bytes"
    src = CloudImageSource(url="https://x/y.qcow2", sha256=hashlib.sha256(data).hexdigest())
    def dl(url, dest): dest.write_bytes(data)
    acquire_base(src, tmp_path/"scratch", releasever="44", arch="x86_64",
                 virt_builder=_unused, downloader=dl)  # no raise

def test_cloud_image_sha256_mismatch_fails_closed(tmp_path):
    src = CloudImageSource(url="https://x/y.qcow2", sha256="0"*64)
    def dl(url, dest): dest.write_bytes(b"other")
    with pytest.raises(CategorizedError) as e:
        acquire_base(src, tmp_path/"s", releasever="44", arch="x86_64",
                     virt_builder=_unused, downloader=dl)
    assert e.value.details["reason"] == "base_sha256_mismatch"

def test_unreachable_url_named(tmp_path):
    src = CloudImageSource(url="https://x/missing.qcow2", sha256="0"*64)
    def dl(url, dest): raise FileNotFoundError("404")
    with pytest.raises(CategorizedError) as e:
        acquire_base(src, tmp_path/"s", releasever="44", arch="x86_64",
                     virt_builder=_unused, downloader=dl)
    assert e.value.details["reason"] == "base_unreachable" and "missing.qcow2" in str(e.value.details)

def test_virt_builder_source_invokes_template(tmp_path):
    calls = {}
    def vb(*, template, output): calls["t"] = template; Path(output).write_bytes(b"x")
    acquire_base(VirtBuilderSource(template="fedora-43"), tmp_path/"s",
                 releasever="43", arch="x86_64", virt_builder=vb, downloader=_unused)
    assert calls["t"] == "fedora-43"
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement `base_source.py`.** Real default downloader = `urllib.request.urlopen` streamed to `dest` (separate `_real_download`, not covered by unit tests). sha256 via streaming read. Wrap the downloader call; map `URLError`/`HTTPError`/`FileNotFoundError`/`OSError` → `base_unreachable`.
- [ ] **Step 4: Run, verify pass.** `just lint && just type`.
- [ ] **Step 5: Commit.** `feat(images): add dual base-source acquirer (template + cloud-image) (ADR-0250)`

---

## Task 4: `rhel` FamilyCustomizer

**Files:**
- Create: `src/kdive/images/families/__init__.py`
- Create: `src/kdive/images/families/base.py` (the `FamilyCustomizer` protocol + `CustomizeContext`)
- Create: `src/kdive/images/families/rhel.py`
- Test: `tests/images/families/test_rhel.py`

**Interfaces:**
- Consumes: the working argv from Task 1 Step 6; the relocated constants from `rootfs_build.py` (`_READINESS_MARKER`, `_READINESS_UNIT`, `_KDUMP_SYSCTL_PATH/_CONTENT`, `_KDUMP_FINAL_ACTION_CMD`, `_FSTAB`, `_SELINUX_CONFIG`, `_debug_image_args`).
- Produces:
  - `@dataclass(frozen=True, slots=True) class CustomizeContext: kind: str; packages: tuple[str,...]; authorized_key: Path; readiness_unit_path: Path; is_cloud_image: bool; cleanup: list[Path]`
  - `class FamilyCustomizer(Protocol): family: str; def packages(self, kind: str) -> tuple[str,...]; def customize_argv(self, ctx: CustomizeContext) -> list[str]; def normalize(self, qcow2: Path) -> None`
  - `class RhelFamily: family = "rhel"` implementing the protocol.

- [ ] **Step 1: Write failing tests** for `RhelFamily.customize_argv`:

```python
# tests/images/families/test_rhel.py — sketch
def test_rhel_debug_argv_enables_kdump_and_sshd(tmp_path):
    fam = RhelFamily()
    ctx = CustomizeContext(kind="debug", packages=fam.packages("debug"),
                           authorized_key=tmp_path/"key.pub", readiness_unit_path=tmp_path/"u.service",
                           is_cloud_image=True, cleanup=[])
    argv = fam.customize_argv(ctx)
    j = " ".join(argv)
    assert "kdump-utils" in j and "makedumpfile" in j
    assert "systemctl enable kdump.service" in argv
    assert "systemctl enable sshd.service" in argv
    assert "99-kdive-kdump.conf" in j and "unknown_nmi_panic=1" in j

def test_rhel_cloud_image_disables_cloud_init(tmp_path):
    fam = RhelFamily()
    ctx = CustomizeContext(kind="debug", packages=fam.packages("debug"),
                           authorized_key=tmp_path/"k", readiness_unit_path=tmp_path/"u",
                           is_cloud_image=True, cleanup=[])
    assert any("cloud-init" in a for a in fam.customize_argv(ctx))

def test_rhel_virt_builder_source_skips_cloud_init(tmp_path):
    fam = RhelFamily()
    ctx = CustomizeContext(kind="debug", packages=fam.packages("debug"),
                           authorized_key=tmp_path/"k", readiness_unit_path=tmp_path/"u",
                           is_cloud_image=False, cleanup=[])
    assert not any("cloud-init" in a for a in fam.customize_argv(ctx))
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** Move the inline Fedora argv-building from `rootfs_build.py::_real_virt_builder` into `RhelFamily.customize_argv` (dnf `--install`, sshd enable, kdump enable + sysctl + final_action, `_debug_image_args`, ssh-inject, kdive-ready upload+enable, SELinux permissive). Add the cloud-init mask when `ctx.is_cloud_image`. `RhelFamily.normalize` moves `_real_normalize_guest` (fstab/crypttab/SELinux) here and adds a SELinux relabel. Relocate the shared constants into a module both `rhel.py` and `rootfs_build.py` import (or into `families/rhel.py` and import back). Keep `RootfsBuildSpec.distro`/family mapping intact.
- [ ] **Step 4: Run, verify pass.** `just lint && just type`.
- [ ] **Step 5: Commit.** `feat(images): add rhel FamilyCustomizer; relocate Fedora customization (ADR-0250)`

---

## Task 5: Wire `rootfs_build.py` to catalog + base-source + family

**Files:**
- Modify: `src/kdive/providers/local_libvirt/rootfs_build.py`
- Test: `tests/providers/local_libvirt/test_rootfs_build.py` (extend existing)

**Interfaces:**
- Consumes: `acquire_base` (T3), `RhelFamily`/`FamilyCustomizer` (T4), `RootfsCatalogEntry` (T2).
- Produces: build pipeline `acquire base (source) → virt-customize(family argv) → repack ext4 → family.normalize → output + provenance`. Provenance `source_image_digest` = `"cloud-image:<url>@sha256:<digest>"` or `"virt-builder:<template>"`.

- [ ] **Step 1: Write failing orchestration test** with all seams faked (no libguestfs): assert ordering (acquire → customize → repack → normalize), that the family customizer (not a hardcoded SELinux edit) runs, and provenance content for both a cloud-image and a virt-builder entry.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** Add a `family_for(name)` resolver (`{"rhel": RhelFamily()}`); thread the catalog entry's `source` + `family` into the build. Replace the inline virt-builder call with `acquire_base(...)` + a `virt-customize` invocation built from `family.customize_argv(ctx)`. Keep the existing `RootfsBuildSpec`/`RootfsBuildOutput` contract; extend `RootfsBuildSpec` only if needed to carry the resolved `source`/`family` (or resolve them inside the plane from the catalog). Keep `live_vm`-bound real seams `# pragma: no cover - live_vm`.
- [ ] **Step 4: Run, verify pass.** Run the full `tests/providers/local_libvirt/` + `tests/images/`; `just lint && just type`.
- [ ] **Step 5: Commit.** `feat(local-libvirt): build rootfs from catalog via base-source + family seam (ADR-0250)`

---

## Task 6: `build-fs --image <name>` CLI

**Files:**
- Modify: `src/kdive/images/rootfs_command.py`
- Test: `tests/images/test_rootfs_command.py` (extend/create)

- [ ] **Step 1: Write failing test:** `build-fs --image fedora-kdive-ready-44` resolves the catalog entry and the plane is invoked with that entry's source/family/name/version; an unknown `--image` raises `CONFIGURATION_ERROR`. (Inject the plane seam `_build_local_rootfs_plane`.)
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** Add `--image`; when present, resolve via `resolve_rootfs_entry` and derive `name/distro/version/dest`. Keep `--distro/--releasever/--name/--dest/--kind/--package` as overrides/back-compat for the default. Replace `resolve_base_template`-based `source_image_digest` with the entry's source digest. Delete `images/distros.py` (replaced by the catalog) and update its importers (`rootfs_command.py`, any test) — no shim.
- [ ] **Step 4: Run, verify pass.** Grep for remaining `distros` / `resolve_base_template` imports and clear them. `just lint && just type`.
- [ ] **Step 5: Commit.** `feat(images): build-fs --image resolves the rootfs catalog; drop distros.py (ADR-0250)`

---

## Task 7: Incomplete-core handling in the harvest

**Files:**
- Modify: `src/kdive/providers/local_libvirt/retrieve.py`, `src/kdive/providers/local_libvirt/retrieve_kdump.py` (if `harvest_vmcore`/`VmcoreEntry` selection lives there)
- Test: `tests/providers/local_libvirt/test_retrieve_kdump.py` (extend)

**Interfaces:**
- Produces: a new constant `KDUMP_CORE_INCOMPLETE_REMEDIATION` (cause-neutral) and a `_incomplete_core(system_id)` `CategorizedError` (`READINESS_FAILURE`, `details={reason:"kdump_core_incomplete", remediation, system_id}`). The reader globs both `/var/crash/*/vmcore` and `/var/crash/*/vmcore-incomplete`; harvest prefers a complete `vmcore`.

- [ ] **Step 1: Write failing tests** with a fake reader returning entries:
  - only `vmcore` → success path (complete core harvested);
  - only `vmcore-incomplete` → `capture` raises `READINESS_FAILURE` with `details["reason"] == "kdump_core_incomplete"`;
  - both → the complete `vmcore` is chosen (incomplete ignored);
  - neither → existing `_no_core` (`details` without `reason="kdump_core_incomplete"`).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement.** Extend the glob set (the harvest selection in `retrieve_kdump.harvest_vmcore`/`list_vmcores`) to surface incomplete entries separately; in `_real_wait_for_vmcore`/`capture`, when no complete core but an incomplete one exists, raise `_incomplete_core(...)`. Keep `_no_core` for the genuinely-empty case. Wording is one shared constant (cause-neutral; names makedumpfile-too-old OR window-overrun; points at `host_dump`/newer image), interpolating no guest output.
- [ ] **Step 4: Run, verify pass.** `just lint && just type`; run `tests/providers/local_libvirt/ -q`.
- [ ] **Step 5: Commit.** `feat(retrieve): disclose an incomplete kdump core with an actionable remedy (ADR-0250)`

---

## Task 8: Register `fedora-kdive-ready-44` in the inventory

**Files:**
- Modify: `systems.toml.example`, `src/kdive/admin/default_fixtures.py`, `src/kdive/images/seed_data/` (image_catalog baseline rows) and/or `examples/local-libvirt/*` as the 43 entry is registered.
- Test: the existing inventory/guard tests (`tests/inventory/test_validate_systems.py`, `tests/admin/test_*`, `tests/guards/test_no_inventory_in_code.py`).

- [ ] **Step 1: Find every place `fedora-kdive-ready-43` is registered.** `rg -n "fedora-kdive-ready-43" src/ systems.toml.example examples/ fixtures/`.
- [ ] **Step 2: Add the 44 entry** alongside 43 in each inventory surface (systems.toml example image/system rows, `default_fixtures`, image_catalog seed), documenting 44 as the kdump-capable default and 43 as the regression reference (prose notes the makedumpfile-vs-kernel limitation; no `kdump_capable` field — deferred).
- [ ] **Step 3: Run the guard/inventory tests** (`uv run python -m pytest tests/inventory tests/admin tests/guards -q`); fix until green.
- [ ] **Step 4: Regenerate any committed config/tool reference** if these surfaces feed a generated doc (`just config-docs`, `just docs`); commit the regen with the change.
- [ ] **Step 5: Commit.** `feat(inventory): register fedora-kdive-ready-44 alongside 43 (ADR-0250)`

---

## Task 9: Live-proof gate (`live_vm`, this host)

**Files:** a `live_vm`-marked test under `tests/providers/local_libvirt/` (or a documented runbook step if a full lifecycle test is impractical); evidence captured to the PR.

- [ ] **Step 1: Reproduce the failure on F43 at pinned RAM (negative-proof).** At **≥8 GB** guest RAM, drive the v7.0 kernel through the lifecycle on `fedora-kdive-ready-43`, `vmcore.fetch` default `kdump`; confirm (a) in-guest console shows "The kernel version is not supported", (b) the fetch returns `kdump_core_incomplete`. If 43 captures cleanly, the RAM is too small — increase it; the proof is invalid otherwise.
- [ ] **Step 2: Prove the fix on F44.** Build `build-fs --image fedora-kdive-ready-44`; at the **same RAM**, run the lifecycle + default `kdump` `vmcore.fetch`; assert console shows **no** "kernel version is not supported", a complete vmcore is captured, and `postmortem.triage` runs on it.
- [ ] **Step 3: Capture evidence** (console + transcript) for both into the PR body.
- [ ] **Step 4: Commit** any `live_vm` test/runbook. `test(live): prove fedora-44 default kdump captures a complete 7.0 vmcore (#817)`

---

## Self-Review notes

- Spec coverage: catalog (T2), base-source incl. 404 (T3), `rhel` customizer + cloud-init + normalize (T4), pipeline (T5), `build-fs --image` + drop `distros.py` (T6), incomplete-core cause-neutral (T7), inventory (T8), pinned-RAM reproduce-first live-proof (T9), de-risk spike first (T1). `kdump_capable` intentionally absent (deferred). ✓
- Type consistency: `RootfsCatalogEntry`/`RootfsSource`/`CustomizeContext`/`FamilyCustomizer` names are reused verbatim across T2–T6. ✓
- The `sha256` pin is filled from T1 Step 1 (not a placeholder left in code — it is produced by Task 1 before Task 2 commits).
