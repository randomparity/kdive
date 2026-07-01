# Rootfs Capability Tags (S1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make rootfs capability tags describe, per distro, what each OS family actually bakes — dropping the false `agent` tag from local images, adding `ssh`/`selinux`/`apparmor`, and replacing the fixed `_KIND_CAPABILITIES` table with a family-declared `capabilities()` seam guarded against build-recipe evidence.

**Architecture:** Add a `capabilities(kind, distro, version)` method to the `FamilyCustomizer` protocol (sibling of `packages()`), implemented per family from its own package set and `guest_mac`. `catalog_rootfs_build` calls it instead of indexing a constant. A registry-iterated guard test ties every declared tag to concrete evidence (`packages()` membership or `guest_mac`). Pure metadata: no boot, no migration, no image-byte change.

**Tech Stack:** Python 3.14, `uv`, Pydantic v2 (enum is a plain `StrEnum`), `pytest`, `ruff`, `ty`, `just`.

**Spec:** `docs/superpowers/specs/2026-07-01-rootfs-capability-tags-s1-design.md`
**ADR:** `docs/adr/0287-per-distro-capability-tags.md`

## Global Constraints

- Guardrails per commit: `just lint`, `just type`, and the touched tests green. Run `just ci` before the final push. Zero warnings.
- ≤100 lines/function, cyclomatic complexity ≤8, ≤100-char lines, absolute imports only (no `..`).
- No ADR-NNNN references in agent-facing tool/field/enum descriptions (guard `test_no_adr_leak`).
- `Capability` is a closed `StrEnum`; tags are stored as `text[]` (no migration).
- The `agent` enum member stays (remote guest contract, `GUEST_CONTRACT_PATHS`); only the *local families* stop declaring it.
- Honesty scope: tags mean "the build installs this" (declaration↔recipe), not "this works" (efficacy is S2).
- Conventional commits, imperative subject ≤72 chars, end each with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

---

## File Structure

- `src/kdive/domain/catalog/images.py` — add `SSH`/`SELINUX`/`APPARMOR` to `Capability`.
- `src/kdive/images/families/base.py` — add `capabilities()` to the `FamilyCustomizer` protocol; add the shared `_mac_tag()` helper.
- `src/kdive/images/families/rhel.py` — implement `RhelFamily.capabilities()`.
- `src/kdive/images/families/debian.py` — implement `DebianFamily.capabilities()`.
- `src/kdive/images/rootfs_specs.py` — call `family.capabilities(...)`; delete `_KIND_CAPABILITIES`.
- `tests/images/families/test_capability_evidence.py` — new registry-iterated anti-drift guard.
- `tests/images/test_families_capabilities.py` — new per-family unit tests.
- `systems.toml.example`, `docs/operating/providers/examples/systems-local-libvirt.toml`, `examples/local-libvirt/README.md`, `src/kdive/admin/default_fixtures.py` — converge staged-path capability sets.
- Existing tests referencing the old tuple / `_KIND_CAPABILITIES` — update.

---

### Task 1: Add `ssh`/`selinux`/`apparmor` to the `Capability` enum

**Files:**
- Modify: `src/kdive/domain/catalog/images.py:16-28`
- Test: `tests/domain/catalog/test_images_capability.py`

**Interfaces:**
- Produces: `Capability.SSH == "ssh"`, `Capability.SELINUX == "selinux"`, `Capability.APPARMOR == "apparmor"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/domain/catalog/test_images_capability.py`:

```python
def test_new_static_tags_present() -> None:
    from kdive.domain.catalog.images import Capability

    assert Capability.SSH == "ssh"
    assert Capability.SELINUX == "selinux"
    assert Capability.APPARMOR == "apparmor"


def test_guest_contract_paths_still_subset_of_capability() -> None:
    from kdive.domain.catalog.images import Capability
    from kdive.images.validation import GUEST_CONTRACT_PATHS

    assert set(GUEST_CONTRACT_PATHS) <= {c.value for c in Capability}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/domain/catalog/test_images_capability.py::test_new_static_tags_present -q`
Expected: FAIL — `AttributeError: SSH` (member does not exist yet).

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/domain/catalog/images.py`, extend the enum (keep `AGENT` — it stays for the remote contract):

```python
class Capability(StrEnum):
    AGENT = "agent"
    KDUMP = "kdump"
    DRGN = "drgn"
    BUILD = "build"
    HELPERS = "helpers"
    SSH = "ssh"
    SELINUX = "selinux"
    APPARMOR = "apparmor"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/domain/catalog/test_images_capability.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/domain/catalog/images.py tests/domain/catalog/test_images_capability.py
git commit -m "feat(957-s1): add ssh/selinux/apparmor to the Capability vocabulary

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Add the `_mac_tag()` helper

**Files:**
- Modify: `src/kdive/images/families/base.py`
- Test: `tests/images/families/test_capability_evidence.py` (create; adds the `_mac_tag` unit tests here, guard follows in Task 6)

**Interfaces:**
- Consumes: `Capability` (Task 1).
- Produces: `kdive.images.families.base._mac_tag(guest_mac: str) -> Capability`.

> Ordering note: the `capabilities()` method is added to the `FamilyCustomizer` **protocol** in
> Task 5, *after* both families implement it (Tasks 3–4). Widening the protocol earlier would make
> `just type` red, because `_FAMILIES: dict[str, FamilyCustomizer]` would no longer type-check until
> the concrete families satisfy the wider protocol. The families' concrete `capabilities()` methods
> (Tasks 3–4) do not require the protocol to declare it.

- [ ] **Step 1: Write the failing test**

Create `tests/images/families/test_capability_evidence.py`:

```python
"""_mac_tag mapping and (Task 6) the registry-iterated anti-drift guard (ADR-0287)."""

from __future__ import annotations

import pytest

from kdive.domain.catalog.images import Capability
from kdive.images.families.base import _mac_tag


def test_mac_tag_selinux_permissive() -> None:
    assert _mac_tag("selinux-permissive") == Capability.SELINUX


def test_mac_tag_apparmor() -> None:
    assert _mac_tag("apparmor") == Capability.APPARMOR


def test_mac_tag_unmapped_raises_naming_posture() -> None:
    with pytest.raises(ValueError, match="tomoyo"):
        _mac_tag("tomoyo")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/images/families/test_capability_evidence.py -q`
Expected: FAIL — `ImportError: cannot import name '_mac_tag'`.

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/images/families/base.py`, add the import and the helper (do **not** touch the
`FamilyCustomizer` protocol yet — that is Task 5):

```python
from kdive.domain.catalog.images import Capability


def _mac_tag(guest_mac: str) -> Capability:
    """Map a family's ``guest_mac`` posture to its capability tag.

    Deriving the tag from ``guest_mac`` (rather than a second literal) keeps the declared
    tag and the recorded provenance from disagreeing.
    """
    if guest_mac.startswith("selinux"):
        return Capability.SELINUX
    if guest_mac == "apparmor":
        return Capability.APPARMOR
    raise ValueError(f"unmapped guest_mac posture: {guest_mac!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/images/families/test_capability_evidence.py -q`
Expected: PASS.
Run: `just type`
Expected: PASS (only a module-level helper was added; the protocol is unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/images/families/base.py tests/images/families/test_capability_evidence.py
git commit -m "feat(957-s1): add _mac_tag guest-mac-to-capability helper

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Implement `RhelFamily.capabilities()`

**Files:**
- Modify: `src/kdive/images/families/rhel.py`
- Test: `tests/images/test_families_capabilities.py` (create)

**Interfaces:**
- Consumes: `_mac_tag` (Task 2), `Capability` (Task 1).
- Produces: `RhelFamily().capabilities(kind, distro, version)` → debug: `(SSH, SELINUX, KDUMP, DRGN)`; build: `(SELINUX, BUILD)`.

- [ ] **Step 1: Write the failing test**

Create `tests/images/test_families_capabilities.py`:

```python
"""Per-family capabilities() declarations (ADR-0287)."""

from __future__ import annotations

from kdive.domain.catalog.images import Capability
from kdive.images.families.rhel import RhelFamily


def test_rhel_debug_capabilities() -> None:
    caps = RhelFamily().capabilities("debug", "fedora", "44")
    assert set(caps) == {
        Capability.SSH,
        Capability.SELINUX,
        Capability.KDUMP,
        Capability.DRGN,
    }
    assert Capability.AGENT not in caps


def test_rhel_build_capabilities() -> None:
    caps = RhelFamily().capabilities("build", "fedora", "44")
    assert set(caps) == {Capability.SELINUX, Capability.BUILD}


def test_rhel_capabilities_el_major_invariant() -> None:
    # EL8 and EL10 differ in packages() but not in the declared trait set.
    assert set(RhelFamily().capabilities("debug", "rocky", "8")) == set(
        RhelFamily().capabilities("debug", "rocky", "10")
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/images/test_families_capabilities.py -q`
Expected: FAIL — `AttributeError: 'RhelFamily' object has no attribute 'capabilities'`.

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/images/families/rhel.py`, make `Capability` and `_mac_tag` importable, then add the method to `RhelFamily` (after `packages`). The module **already** imports `CustomizeContext` from `kdive.images.families.base` (do not add a second import of it) — extend that existing line and add the `Capability` import:

- Change the existing `from kdive.images.families.base import CustomizeContext` to
  `from kdive.images.families.base import CustomizeContext, _mac_tag`.
- Add `from kdive.domain.catalog.images import Capability`.

```python
    def capabilities(self, kind: str, distro: str, version: str) -> tuple[Capability, ...]:
        """Return the tags this family bakes. EL-major-invariant, so distro/version unused."""
        del distro, version
        mac = _mac_tag(self.guest_mac)
        if kind == "build":
            return (mac, Capability.BUILD)
        return (Capability.SSH, mac, Capability.KDUMP, Capability.DRGN)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/images/test_families_capabilities.py -q`
Expected: the three rhel tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/images/families/rhel.py tests/images/test_families_capabilities.py
git commit -m "feat(957-s1): declare RhelFamily baked capabilities

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Implement `DebianFamily.capabilities()`

**Files:**
- Modify: `src/kdive/images/families/debian.py`
- Test: `tests/images/test_families_capabilities.py`

**Interfaces:**
- Consumes: `_mac_tag` (Task 2), `Capability` (Task 1).
- Produces: `DebianFamily().capabilities(kind, distro, version)` → debug: `(SSH, APPARMOR, KDUMP, DRGN)`; build: `(APPARMOR, BUILD)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/images/test_families_capabilities.py`:

```python
from kdive.images.families.debian import DebianFamily


def test_debian_debug_capabilities() -> None:
    caps = DebianFamily().capabilities("debug", "debian", "13")
    assert set(caps) == {
        Capability.SSH,
        Capability.APPARMOR,
        Capability.KDUMP,
        Capability.DRGN,
    }
    assert Capability.AGENT not in caps
    assert Capability.SELINUX not in caps


def test_debian_build_capabilities() -> None:
    caps = DebianFamily().capabilities("build", "debian", "12")
    assert set(caps) == {Capability.APPARMOR, Capability.BUILD}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/images/test_families_capabilities.py -k debian -q`
Expected: FAIL — `AttributeError: 'DebianFamily' object has no attribute 'capabilities'`.

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/images/families/debian.py`, make `Capability` and `_mac_tag` importable, then add to `DebianFamily`. The module **already** imports `CustomizeContext` from `kdive.images.families.base` (do not add a second import of it) — extend that existing line and add the `Capability` import:

- Change the existing `from kdive.images.families.base import CustomizeContext` to
  `from kdive.images.families.base import CustomizeContext, _mac_tag`.
- Add `from kdive.domain.catalog.images import Capability`.

```python
    def capabilities(self, kind: str, distro: str, version: str) -> tuple[Capability, ...]:
        """Return the tags this family bakes (distro/version unused, kept for parity)."""
        del distro, version
        mac = _mac_tag(self.guest_mac)
        if kind == "build":
            return (mac, Capability.BUILD)
        return (Capability.SSH, mac, Capability.KDUMP, Capability.DRGN)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/images/test_families_capabilities.py -q`
Expected: all PASS.
Run: `just type`
Expected: PASS (both families now satisfy the protocol).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/images/families/debian.py tests/images/test_families_capabilities.py
git commit -m "feat(957-s1): declare DebianFamily baked capabilities

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Declare the protocol method; wire `catalog_rootfs_build`; delete `_KIND_CAPABILITIES`

**Files:**
- Modify: `src/kdive/images/families/base.py` (add the protocol method now that both families implement it)
- Modify: `src/kdive/images/rootfs_specs.py:20-23,42-59`
- Test: `tests/images/test_catalog_resolver.py`

**Interfaces:**
- Consumes: `family.capabilities()` (Tasks 3–4).
- Produces: `FamilyCustomizer.capabilities(self, kind, distro, version) -> tuple[Capability, ...]` on the protocol; `catalog_rootfs_build(provider, name).spec.capabilities` derived from the family.

- [ ] **Step 1: Write the failing test**

Add to `tests/images/test_catalog_resolver.py`:

```python
def test_catalog_build_capabilities_are_per_distro() -> None:
    from kdive.domain.catalog.images import Capability
    from kdive.images.rootfs_specs import catalog_rootfs_build

    fedora = catalog_rootfs_build("local-libvirt", "fedora-kdive-ready-44").spec.capabilities
    debian = catalog_rootfs_build("local-libvirt", "debian-kdive-ready-13").spec.capabilities

    assert Capability.SELINUX in fedora and Capability.APPARMOR not in fedora
    assert Capability.APPARMOR in debian and Capability.SELINUX not in debian
    assert Capability.AGENT not in fedora and Capability.AGENT not in debian
    assert Capability.SSH in fedora and Capability.SSH in debian
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/images/test_catalog_resolver.py::test_catalog_build_capabilities_are_per_distro -q`
Expected: FAIL — `Capability.AGENT` is still present (old `_KIND_CAPABILITIES`), so the `AGENT not in` assertions fail.

- [ ] **Step 3: Write minimal implementation**

First, declare the method on the protocol in `src/kdive/images/families/base.py` (both families
now implement it, so `_FAMILIES: dict[str, FamilyCustomizer]` still type-checks). Add next to
`packages` in the `FamilyCustomizer` body:

```python
    def capabilities(self, kind: str, distro: str, version: str) -> tuple[Capability, ...]:
        """Return the capability tags this family bakes for ``kind`` on ``distro``/``version``."""
        ...
```

Then, in `src/kdive/images/rootfs_specs.py`, delete the `_KIND_CAPABILITIES` constant (lines 20-23) and its `Capability` import if now unused (it is not referenced elsewhere in the file, so remove it). Change the spec construction:

```python
    spec = RootfsBuildSpec(
        provider=provider,
        name=entry.name,
        arch=entry.arch,
        releasever=entry.version,
        packages=build_packages,
        source_image_digest=source_image_digest(entry.source),
        capabilities=family.capabilities(entry.kind, entry.distro, entry.version),
        distro=entry.distro,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/images/test_catalog_resolver.py -q`
Expected: PASS.
Run: `.venv/bin/rg -n "_KIND_CAPABILITIES" src/ tests/`
Expected: no matches (constant fully removed).
Run: `just lint && just type`
Expected: PASS (no unused `Capability` import left behind).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/images/families/base.py src/kdive/images/rootfs_specs.py tests/images/test_catalog_resolver.py
git commit -m "feat(957-s1): derive build-spec capabilities from the family

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Registry-iterated anti-drift guard

**Files:**
- Modify: `tests/images/families/test_capability_evidence.py` (add the guard alongside the `_mac_tag` tests)

**Interfaces:**
- Consumes: `kdive.images.families._FAMILIES`, each family's `packages()`/`capabilities()`/`guest_mac`.

- [ ] **Step 1: Write the failing test**

Append to `tests/images/families/test_capability_evidence.py`:

```python
from kdive.images.families import _FAMILIES
from kdive.images.families.base import FamilyCustomizer

_KINDS = ("debug", "build")
# EVERY (distro, version) pair whose packages() output is distinct, so the evidence check
# covers the EL-major branch in RhelFamily.packages() (EL8/EL9 vs EL10/Fedora) — not just one
# representative. A tag declared but unbacked on *any* of these fails the guard.
_PROBE_PAIRS: dict[str, tuple[tuple[str, str], ...]] = {
    "rhel": (("fedora", "44"), ("rocky", "8"), ("rocky", "9"), ("rocky", "10")),
    "debian": (("debian", "12"), ("debian", "13")),
}


def _evidenced(packages: tuple[str, ...], guest_mac: str, kind: str, tag: Capability) -> bool:
    if tag is Capability.SSH:
        return "openssh-server" in packages
    if tag in (Capability.SELINUX, Capability.APPARMOR):
        return tag == _mac_tag(guest_mac)
    if tag is Capability.KDUMP:
        return any(p in packages for p in ("kexec-tools", "kdump-tools"))
    if tag is Capability.DRGN:
        return any(p in packages for p in ("drgn", "python3-drgn"))
    if tag is Capability.BUILD:
        return kind == "build"
    return False  # any other declared tag has no evidence rule -> unbacked


@pytest.mark.parametrize("family", _FAMILIES.values(), ids=list(_FAMILIES))
def test_every_declared_tag_is_evidenced(family: FamilyCustomizer) -> None:
    for name, version in _PROBE_PAIRS[family.family]:
        for kind in _KINDS:
            packages = family.packages(kind, name, version)
            for tag in family.capabilities(kind, name, version):
                assert _evidenced(packages, family.guest_mac, kind, tag), (
                    f"{family.family}/{name}-{version}/{kind}: {tag} unbacked"
                )


@pytest.mark.parametrize("family", _FAMILIES.values(), ids=list(_FAMILIES))
def test_guest_mac_maps_to_a_tag(family: FamilyCustomizer) -> None:
    # A new family with an unmapped posture fails here, not at build-fs.
    assert _mac_tag(family.guest_mac) in (Capability.SELINUX, Capability.APPARMOR)


def test_no_local_family_declares_agent() -> None:
    for family in _FAMILIES.values():
        for name, version in _PROBE_PAIRS[family.family]:
            for kind in _KINDS:
                assert Capability.AGENT not in family.capabilities(kind, name, version)
```

- [ ] **Step 2: Run test to verify it fails, then passes**

Run: `.venv/bin/pytest tests/images/families/test_capability_evidence.py -q`
Expected: with Tasks 1–5 already merged these PASS immediately. To prove the guard bites, temporarily add `Capability.HELPERS` to `RhelFamily.capabilities()`'s debug return, run the guard, and confirm `test_every_declared_tag_is_evidenced[rhel]` FAILS with "rhel/debug: Capability.HELPERS unbacked"; then revert.

- [ ] **Step 3: Implementation**

None — the guard is test-only. If Step 2's temporary edit was made, ensure it is reverted (`git diff src/kdive/images/families/rhel.py` shows no change).

- [ ] **Step 4: Run the guard + full images tests**

Run: `.venv/bin/pytest tests/images/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/images/families/test_capability_evidence.py
git commit -m "test(957-s1): registry-iterated capability anti-drift guard

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Converge staged-path metadata, drop the phantom profile requirement, update stale assertions

**Files:**
- Modify: `systems.toml.example`, `docs/operating/providers/examples/systems-local-libvirt.toml`, `examples/local-libvirt/README.md`, `src/kdive/admin/default_fixtures.py`
- Modify: `fixtures/local-libvirt/profiles/console-ready_x86_64.yaml` and the profile YAML embedded in `src/kdive/admin/default_fixtures.py` — the `requires.rootfs.capabilities: [agent]` block (see Step 1b)
- Modify: any test asserting the prior literal capability set (found in Step 1)

**Interfaces:**
- Consumes: the per-distro image sets — Fedora/Rocky/CentOS-Stream → `["ssh","selinux","kdump","drgn"]`; Debian → `["ssh","apparmor","kdump","drgn"]`.

**Design decision (profile requirement):** two profile fixtures declare
`requires.rootfs.capabilities: [agent]` — a *requirement* that a matched image carry `agent`.
S1 drops `agent` from every local image, so this requirement would name a capability no local
image provides. No code enforces it against an image today (verified: `profiles/build.py` does
not read it; the only capability membership checks in the tree are the kdump signal and a log
label), so this is not a runtime break — but leaving it is the exact contradiction S1 removes.
Change the requirement to `[]` (empty): the profile's real gate is its `config`/`cmdline`
requirements, no current tag denotes "console-ready", and requiring `ssh` would be wrong for a
console-only profile.

- [ ] **Step 1: Find every staged-path capability declaration and stale assertion**

Run:
```bash
.venv/bin/rg -n 'capabilities' systems.toml.example docs/operating/providers/examples/systems-local-libvirt.toml examples/local-libvirt/README.md src/kdive/admin/default_fixtures.py
.venv/bin/rg -n 'capabilities' fixtures/local-libvirt/profiles/ src/kdive/admin/
.venv/bin/rg -n 'kdive-ready-console|"agent"|- agent|capabilities.*agent' tests/ src/ fixtures/
```
Confirm every staged `[[image]]`/fixture entry is a debug rootfs (no `kind = "build"` staged entry). Record the exact lines to edit, the two profile `requires.rootfs.capabilities: [agent]` blocks, and any test asserting `["agent", ...]`.

- [ ] **Step 1b: Empty the phantom profile requirement**

In `fixtures/local-libvirt/profiles/console-ready_x86_64.yaml` and the embedded profile YAML in
`src/kdive/admin/default_fixtures.py`, change:

```yaml
  rootfs:
    format: qcow2
    root_device: /dev/vda
    capabilities:
      - agent
```

to an empty requirement (drop the `capabilities` key, or set `capabilities: []`):

```yaml
  rootfs:
    format: qcow2
    root_device: /dev/vda
    capabilities: []
```

- [ ] **Step 2: Adjust the failing assertion in place**

Do not invent a new test or helper. Take each assertion Step 1's grep surfaced (e.g. in
`tests/admin/test_default_fixtures.py`, `tests/mcp/catalog/test_fixtures_validate.py`, or
`tests/inventory/*`) that pins a seeded/staged image's capability list to the old value, and
edit that exact assertion to the new per-distro set. For a Fedora seed the change is:

```python
# before:  assert <entry>.capabilities == ["agent", "kdump", "drgn"]   (or the prior literal)
# after:
assert <entry>.capabilities == ["ssh", "selinux", "kdump", "drgn"]
```

using whatever accessor the existing test already uses for `<entry>` (do not introduce a new
helper). Run the file(s) named by the grep, e.g.:

Run: `.venv/bin/pytest tests/admin/test_default_fixtures.py -q`
Expected: FAIL — current seed still uses the old set.

- [ ] **Step 3: Converge the declarations**

Edit each staged-path entry to its per-distro set. Fedora/Rocky/CentOS-Stream entries →
`capabilities = ["ssh", "selinux", "kdump", "drgn"]`; Debian entries →
`capabilities = ["ssh", "apparmor", "kdump", "drgn"]`. Apply the matching Python list in
`src/kdive/admin/default_fixtures.py`.

- [ ] **Step 4: Run tests + fixture validation**

Run: `.venv/bin/pytest tests/admin/ tests/mcp/catalog/ tests/inventory/ -q`
Expected: PASS (tokens are valid vocabulary; updated assertions match).
Run: `just docs-paths && just docs-links`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add systems.toml.example docs/operating/providers/examples/systems-local-libvirt.toml examples/local-libvirt/README.md src/kdive/admin/default_fixtures.py fixtures/local-libvirt/profiles/console-ready_x86_64.yaml tests/
git commit -m "feat(957-s1): converge staged capabilities; drop phantom agent requirement

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification (before the branch review / PR)

- [ ] Run the full suite once: `just ci`. Expected: green (architecture/doc/boundary tests included).
- [ ] `.venv/bin/rg -n "_KIND_CAPABILITIES" src tests` → no matches.
- [ ] `.venv/bin/rg -n '"agent"|- agent' systems.toml.example src/kdive/admin/default_fixtures.py fixtures/local-libvirt/profiles/` → no local staged image *or profile requirement* names `agent`.
- [ ] Confirm `test_exit_criteria.py::_REQUIRED` (the remote guest-contract `("agent","kdump","drgn","helpers")`) is untouched — S1 does not change the remote contract.

## Rollback

Each task is an isolated commit. Reverting Task 5 restores `_KIND_CAPABILITIES`; reverting Task 7 restores the prior staged-path sets. No migration, no image-byte change — already-built qcow2s are unaffected (tags are catalog metadata).
