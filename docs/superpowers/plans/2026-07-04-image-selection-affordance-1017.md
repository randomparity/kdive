# Agent-facing image-selection affordance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans
> (inline, chosen) to implement this plan task-by-task. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Give an agent honest, structured, per-image data to select an image on
merit, and stop `systems.profile_examples` silently presenting the
first-declared image as the default.

**Architecture:** Additive fields on the `images.list`/`images.describe`
envelopes (`capabilities`, compact `os`, operator `description`); a new
build-time `/etc/os-release` capture into `provenance["os_release"]`; an operator
`description` on inventory `[[image]]` reconciled to a new nullable
`image_catalog.description` column (migration 0060); and a conditional disclosure
in `profile_examples`. No computed ranking (honesty invariant, ADR-0286/0295).

**Tech Stack:** Python 3.14, pydantic v2 (`DomainModel`, `extra="forbid"`),
psycopg3, PostgreSQL, guestfish (offline image probe), FastMCP tools, pytest.

**Spec:** `docs/superpowers/specs/2026-07-04-image-selection-affordance-1017.md`
**ADR:** `docs/adr/0311-image-selection-affordance.md`

## Global Constraints

- Guardrails (run per task): `just lint`, `just type`, `just test`; docs gates
  `just docs-check`, `just resources-docs-check`, `just adr-status-check` after
  doc/registry changes. CI runs each individually.
- Honesty invariant: no computed ranking/recommendation; new fields are build
  facts (`capabilities`, `os_release`) or operator-attested (`description`).
- `description` is **reconcile-owned**; `publish_image` must never write it.
- `description` length cap: `_MAX_IMAGE_DESCRIPTION = 280`, validated at
  inventory load.
- os-release partial-key policy: record `os_release` only when `ID` present;
  include `version_id`/`pretty_name` only when present; never emit `""`.
- Deploy sequencing: migration 0060 ships with the code and is applied via the
  advisory-locked `apply_migrations` step (ADR-0015) before write traffic.
- Migration numbering: **0060** (highest existing is 0059). ADR **0311**.
- Line length ≤100; absolute imports; Google-style docstrings on public APIs.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Migration 0060 + `ImageCatalogEntry.description`

**Files:**
- Create: `src/kdive/db/schema/0060_image_description.sql`
- Modify: `src/kdive/domain/catalog/images.py` (add `description` field)
- Test: `tests/db/test_migration_0060_image_description.py`

**Interfaces:**
- Produces: `ImageCatalogEntry.description: str | None = None`; DB column
  `image_catalog.description text` (nullable).

- [ ] **Step 1: Write the failing migration test** (mirror
  `tests/db/test_migration_0057_check_ssh_reachable.py`): apply migrations to a
  temp DB, assert `image_catalog` has a nullable `description` column of type
  `text`, and that an existing row reads `description IS NULL`.

- [ ] **Step 2: Run it — expect FAIL** (`column does not exist`).
  Run: `uv run pytest tests/db/test_migration_0060_image_description.py -v`

- [ ] **Step 3: Write the migration SQL.**
```sql
-- 0060_image_description.sql — operator-attested image description (ADR-0311, #1017).
-- Additive nullable column reconciled from systems.toml [[image]].description.
ALTER TABLE image_catalog ADD COLUMN description text;
```

- [ ] **Step 4: Add the model field.** In `ImageCatalogEntry`, add
  `description: str | None = None` (below `path`).

- [ ] **Step 5: Run tests — expect PASS.** Also `uv run pytest tests/domain -q`.

- [ ] **Step 6: Commit** (`feat(1017): add image_catalog.description column (mig 0060)`).

---

### Task 2: Inventory `ImageEntry.description` + length cap

**Files:**
- Modify: `src/kdive/inventory/model.py` (`ImageEntry`, add field + validator)
- Test: `tests/inventory/test_model.py` (or the existing inventory-model test)

**Interfaces:**
- Consumes: nothing.
- Produces: `ImageEntry.description: str = ""`; module constant
  `_MAX_IMAGE_DESCRIPTION = 280`.

- [ ] **Step 1: Write failing tests.**
```python
def test_image_entry_default_description_is_empty():
    entry = ImageEntry(provider="local-libvirt", name="x", arch="x86_64",
                       visibility=ImageVisibility.PUBLIC, source=<valid source>)
    assert entry.description == ""

def test_image_entry_rejects_overlong_description():
    with pytest.raises(ValidationError):
        ImageEntry(..., description="z" * 281)

def test_image_entry_accepts_max_length_description():
    ImageEntry(..., description="z" * 280)  # no raise
```

- [ ] **Step 2: Run — expect FAIL** (`description` not a field).

- [ ] **Step 3: Implement.** Add to `ImageEntry`:
```python
description: str = ""

@field_validator("description")
@classmethod
def _check_description(cls, value: str) -> str:
    if len(value) > _MAX_IMAGE_DESCRIPTION:
        raise ValueError(
            f"image description exceeds {_MAX_IMAGE_DESCRIPTION} characters "
            f"({len(value)}); keep it to a one-line operator hint"
        )
    return value
```
Add `_MAX_IMAGE_DESCRIPTION = 280` near the top of the module.

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit** (`feat(1017): operator description on inventory [[image]] with 280-char cap`).

---

### Task 3: Reconcile `description` (create/update) + publish-non-clobber

**Files:**
- Modify: `src/kdive/inventory/reconcile/images.py` (`_load_config_rows`,
  `_create_entry`, `_update_entry`)
- Test: `tests/inventory/reconcile/test_images.py` (existing reconcile test module)

**Interfaces:**
- Consumes: `ImageEntry.description` (Task 2), `image_catalog.description`
  (Task 1).
- Produces: reconciled `description` on the row.

- [ ] **Step 1: Write failing tests.**
  - `test_reconcile_creates_row_with_description`: declare an `[[image]]` with
    `description="RHEL debug host"`, reconcile, assert the row's `description`.
  - `test_reconcile_updates_description_on_edit`: reconcile with a description,
    then reconcile again with a changed one; assert the row updates.
  - `test_reconcile_clears_description_when_removed`: description set, then
    absent → row `description == ""` (or NULL).
  - `test_publish_does_not_clobber_description`: create a row via reconcile with
    a description, run a `publish_image` for the same identity, assert the
    description is unchanged.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.** Add `description` to the `_load_config_rows` SELECT
  column list; add `entry.description` to the `_create_entry` INSERT column list
  and params; add `description` to the `_update_entry` `desired`/UPDATE set
  (mirror how `capabilities` is handled at `images.py:245,264-270`). Do **not**
  touch `services/images/publish.py`.

- [ ] **Step 4: Run — expect PASS.** Also full `uv run pytest tests/inventory -q`.

- [ ] **Step 5: Commit** (`feat(1017): reconcile operator description onto image_catalog row`).

---

### Task 4: Build-time `/etc/os-release` capture

**Files:**
- Modify: `src/kdive/providers/local_libvirt/rootfs_build.py`
  (`_capture_os_release`, `_provenance` param, call site) and the build-tools
  seam that defines `probe_boot_entries` (add `probe_os_release`)
- Test: `tests/providers/local_libvirt/test_rootfs_build.py` (parser + provenance)

**Interfaces:**
- Consumes: `self._tools.probe_os_release(scratch) -> str | None` (raw file text).
- Produces: `provenance["os_release"] = {"id", ["version_id"], ["pretty_name"]}`
  when `ID` present, else key omitted.

- [ ] **Step 1: Write failing unit tests for the parser** (`_parse_os_release`):
```python
def test_parse_os_release_quoted_and_unquoted():
    text = 'ID=fedora\nVERSION_ID=43\nPRETTY_NAME="Fedora Linux 43"\n'
    assert _parse_os_release(text) == {
        "id": "fedora", "version_id": "43", "pretty_name": "Fedora Linux 43"}

def test_parse_os_release_id_only_partial():
    assert _parse_os_release("ID=debian\n") == {"id": "debian"}

def test_parse_os_release_missing_id_returns_none():
    assert _parse_os_release('PRETTY_NAME="X"\n') is None

def test_parse_os_release_skips_comments_and_blanks():
    assert _parse_os_release("# c\n\nID=rocky\n") == {"id": "rocky"}

def test_parse_os_release_malformed_returns_none_or_partial():
    assert _parse_os_release("garbage-no-equals\n") is None
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement `_parse_os_release`** (module-level helper): split
  lines, skip blank/`#`, split on first `=`, strip matching single/double
  quotes, keep `ID`→`id`, `VERSION_ID`→`version_id`, `PRETTY_NAME`→`pretty_name`;
  return `None` unless `id` present.

- [ ] **Step 4: Add `_capture_os_release(self, scratch) -> dict | None`**
  mirroring `_capture_boot_kernel_count` (lines 316-334): call
  `self._tools.probe_os_release(scratch)` inside `try/except CategorizedError`,
  return `None` on failure/empty, else `_parse_os_release(raw)`.

- [ ] **Step 5: Thread into `_provenance`.** Add `os_release: dict | None`
  parameter; append `record["os_release"] = os_release` only when truthy (mirror
  `boot_kernel_count`, lines 411-412). Pass `self._capture_os_release(scratch)`
  at the `_provenance(...)` call site.

- [ ] **Step 6: Add `probe_os_release` to the build-tools protocol/impl**
  (`_real_*` seam and the test double) reading `/etc/os-release` (falling back to
  `/usr/lib/os-release`) via the same guestfish mechanism as `probe_boot_entries`.

- [ ] **Step 7: Write a provenance test**: a fake tools double returning a known
  os-release yields `provenance["os_release"] == {...}`; a `None` probe omits the
  key (row byte-identical to pre-feature).

- [ ] **Step 8: Run — expect PASS.** `uv run pytest tests/providers/local_libvirt -q`.

- [ ] **Step 9: Commit** (`feat(1017): capture /etc/os-release into build provenance`).

---

### Task 5: `images.list` envelope — capabilities + os + description

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/images.py` (`_row_envelope`)
- Test: `tests/mcp/tools/catalog/test_images.py` (list envelope + any snapshot)

**Interfaces:**
- Consumes: `ImageCatalogEntry.capabilities`, `.provenance["os_release"]`,
  `.description`.
- Produces: `images.list` rows with `capabilities`, `os`, `description`.

- [ ] **Step 1: Write failing tests.**
  - `test_list_row_includes_capabilities`: a row with capabilities → envelope
    `data["capabilities"] == ["kdump", ...]`.
  - `test_list_row_includes_compact_os`: `provenance={"os_release":{"id":"fedora",
    "version_id":"43","pretty_name":"..."}}` → `data["os"] == {"id":"fedora",
    "version_id":"43"}`.
  - `test_list_row_omits_os_when_absent`: no os_release → `"os"` absent (or `{}`).
  - `test_list_row_os_id_only`: os_release `{"id":"debian"}` → `data["os"] ==
    {"id":"debian"}` (no empty `version_id`).
  - `test_list_row_includes_description`: `description="hint"` → echoed; `None` →
    `""`.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.** Add to `_row_envelope` data dict:
```python
"capabilities": [cap.value for cap in entry.capabilities],
"os": _compact_os(entry.provenance),
"description": entry.description or "",
```
Add module helper `_compact_os(provenance) -> dict`: read
`provenance.get("os_release")`; return `{}` when absent/not a dict; else
`{"id": ...}` plus `version_id` only when present.

- [ ] **Step 4: Run — expect PASS.** Update any list snapshot fixture.

- [ ] **Step 5: Commit** (`feat(1017): surface capabilities/os/description on images.list`).

---

### Task 6: `images.describe` — compact os + description

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/images.py` (`_describe_envelope`)
- Test: `tests/mcp/tools/catalog/test_images.py`

**Interfaces:**
- Consumes: same as Task 5. `capabilities` and `provenance` are already emitted;
  add compact `os` and `description`.

- [ ] **Step 1: Write failing tests** for `data["os"]` (compact, reusing
  `_compact_os`) and `data["description"]` on `images.describe`.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.** Add `"os": _compact_os(entry.provenance)` and
  `"description": entry.description or ""` to the `_describe_envelope` data dict
  (full `os_release` remains available via the verbatim `provenance`).

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit** (`feat(1017): surface compact os + description on images.describe`).

---

### Task 7: `profile_examples` — available_images + conditional note + description

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/profile_examples.py`
- Test: `tests/mcp/tools/lifecycle/systems/test_profile_examples.py`

**Interfaces:**
- Consumes: `InventoryDoc`, `_public_image`, `ImageEntry.description`.
- Produces: local example item `data` with `available_images: int`,
  `selection_note: str`, and `description` when set.

- [ ] **Step 1: Write failing tests** for the three arities:
  - many (≥2 public local images): `available_images == N`, `selection_note`
    contains "declaration order" and points to `images.list`.
  - one: `available_images == 1`, note says "the only public image", no list steer.
  - zero (no public local image, placeholder rootfs): `available_images == 0`,
    note has no `images.list` steer.
  - `description` echoed when the chosen `[[image]]` has one.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.** Add `_count_public_images(doc, provider) -> int`.
  In `_local_profile`, compute the count and build `selection_note` conditioned
  on it; thread `available_images`, `selection_note`, and the chosen image's
  `description` into the item `data` (via `_example_item`/`_local_profile`
  return). Keep the emitted profile runnable.

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit** (`feat(1017): profile_examples discloses declaration-order pick`).

---

### Task 8: Agent guidance in `toolsets-images.md`

**Files:**
- Modify: `docs/guide/toolsets/images.md` (canonical source; registrar maps it to
  the `_content/toolsets-images.md` snapshot, `registrar.py:188-190`)
- Regenerate: `src/kdive/mcp/resources/_content/toolsets-images.md` via
  `just resources-docs`
- Test: `just resources-docs-check`

- [ ] **Step 1:** Add a "Choosing an image" subsection: compare on
  `capabilities` (tag→task: `kdump` crash-dump, `build` kernel-build host,
  `drgn`/`agent` live introspection), read `os` for the target release, treat
  `description` as operator context. No image names.

- [ ] **Step 2:** `just resources-docs` to regenerate the snapshot.

- [ ] **Step 3:** `just resources-docs-check` — expect PASS.

- [ ] **Step 4: Commit** (`docs(1017): image-selection guidance in toolsets-images resource`).

---

### Task 9: Regenerate tool reference + full guardrails

**Files:**
- Regenerate: agent-facing tool reference (`just docs`) — `images.list`/
  `images.describe`/`profile_examples` output schemas changed.
- Verify: `just docs-check`, `just lint`, `just type`, `just test`.

- [ ] **Step 1:** `just docs` to regenerate the committed tool reference.

- [ ] **Step 2:** `just docs-check`, `just resources-docs-check`,
  `just adr-status-check` — expect PASS.

- [ ] **Step 3:** Full suite: `just lint && just type && just test` — expect PASS.

- [ ] **Step 4: Commit** any regenerated docs (`docs(1017): regenerate tool reference`).

---

## Self-review notes

- **Spec coverage:** decision 1 → Tasks 5/6; decision 2 → Task 4; decision 3 →
  Tasks 1/2/3; decision 4 → Task 7; decision 5 → Task 8; compatibility/sequencing
  → Task 1 (migration) + Global Constraints; token-safety cap → Task 2.
- **Ordering:** schema (1) → input (2) → reconcile (3) → capture (4) → surfaces
  (5,6) → examples (7) → guidance (8) → regen (9). Tasks 5/6 read
  `os_release`/`description` but test with injected provenance, so they do not
  block on 4's live probe.
- **Shared files:** `images.py` (Tasks 5,6) and `reconcile/images.py` (Task 3)
  are serial by construction (single implementer).
- **Rollback:** each task is a standalone commit; migration 0060 is additive
  nullable (inert if unused).
