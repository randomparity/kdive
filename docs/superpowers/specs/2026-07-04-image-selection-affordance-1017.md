# Agent-facing image-selection affordance (#1017)

- **Issue:** #1017 (reframed; `BLACK_BOX_REVIEW.md` Finding 2(a), Epic #1018)
- **ADR:** [ADR-0311](../../adr/0311-image-selection-affordance.md)
- **Status:** Draft

## Problem

An AI agent driving the platform consistently selects one image
(`fedora-kdive-ready-43`) regardless of task fit. The root cause is **not**
documentation and **not** the `direct_kernel` provenance gap the issue was
originally filed against — it is an agent-facing affordance problem with a
single concrete driver plus a supporting vacuum.

**Driver — `systems.profile_examples` anoints the first-declared image.**
`profile_examples` emits exactly one ready-to-edit local example, whose rootfs
is the **first `PUBLIC`-visibility `[[image]]` in `systems.toml` declaration
order** (`_public_image`, `mcp/tools/lifecycle/systems/profile_examples.py:221`
— first match wins). An operator whose inventory declares `fedora-kdive-ready-43`
first hands every agent that name as *the* worked example, which the agent edits
and reuses. Cross-check: `images.list`/`fixtures.list` order alphabetically by
`(provider, name, arch)`, so a list-ordering cause would surface *centos* first,
not fedora — confirming `profile_examples`, not list order, is the driver.

**Vacuum — nothing lets an agent choose on merit.** `images.list` returns
identity and publish state only (`_row_envelope`,
`mcp/tools/catalog/images.py:62`) — zero capability data — so comparing images
means an N+1 `images.describe` fan-out, and no surface ranks, scores, or
otherwise says "when to choose image X over Y". With no basis to override the
example, the agent doesn't.

**Verified non-causes.** No agent-visible surface names any image: there are
zero image-name literals under `src/kdive/mcp/` (tool descriptions, `Field`
text, MCP resources, MCP prompts are all generic). The over-featuring of
`fedora-kdive-ready-43` lives only in **internal operator/developer docs**
(the walkthrough, example inventories, runbooks), which an agent never sees.

**Relationship to the original ask.** The filed issue asked to make the
`direct_kernel` signal definite for shipped fixtures by curating a static
`boot_kernel_count`. That ask is closed as won't-fix-as-specified: it reproduces
the drift-prone static column ADR-0295/0296 explicitly rejected, and a curated
`boot_kernel_count=1` for the multi-kernel `fedora-kdive-ready-43` would be
confidently wrong (`provisionable` when the honest answer is
`not_provisionable`). This spec is **orthogonal** to that signal: it does not
touch `direct_kernel` provenance, and unbuilt fixtures stay honestly
`unverified` (ADR-0228/0286).

## Goal / acceptance

Give an agent honest, structured, per-image information to select on merit, and
stop `profile_examples` from silently presenting one image as the default.

Acceptance:

- `images.list` returns, per row, `capabilities` (build-fact tags), a compact
  `os` identity, and an operator `description` — so an agent compares images in
  one call without an N+1 `describe` fan-out.
- A built image's `images.describe`/`images.list` carries verified OS identity
  (`id`, `version_id`, `pretty_name`) derived from `/etc/os-release` at build
  time; an unbuilt row simply omits it (no fabricated value).
- An operator can attach freeform `description` context to an `[[image]]` in
  `systems.toml`; it is reconciled onto the catalog row and surfaced (labelled
  operator-attested) in `images.list`, `images.describe`, and the
  `profile_examples` example that uses that image.
- `systems.profile_examples` discloses that its example image was chosen by
  declaration order, reports how many public images are available, and points
  the agent to `images.list` to choose deliberately.
- No computed ranking, recommendation, or curated capability value is
  introduced (honesty invariant, ADR-0286/0295).

## Design decisions

### 1. `images.list` becomes a comparison surface

Add three fields to `_row_envelope` (`mcp/tools/catalog/images.py:62`):

- `capabilities`: `[cap.value for cap in entry.capabilities]` — the closed
  build-fact vocabulary (`agent/kdump/drgn/build/ssh/selinux/apparmor`). The
  column is already `SELECT *`-ed and `model_validate`-parsed, so **no SQL and
  no migration**. Matches the `capabilities` key `images.describe` already emits.
- `os`: a compact identity projected from `provenance["os_release"]` when
  present — `id` plus `version_id` **only when that sub-key exists** (absent
  sub-keys are omitted, never emitted as `""`); the whole `os` field is omitted
  when no `os_release` record exists. Never fabricated.
- `description`: `entry.description or ""` — operator-attested (decision 3).

Keyset pagination is unchanged (cursor stays on `(provider, name, arch)`,
ADR-0192). The envelope grows; the fielded-output and snapshot tests are updated.

### 2. Build-time `/etc/os-release` capture

At build-fs, capture verified OS identity from inside the built image, modelled
exactly on `_capture_boot_kernel_count`/`_capture_makedumpfile`
(`providers/local_libvirt/rootfs_build.py`):

- Add a `probe_os_release(scratch)` method to the injected build-tools seam
  (`self._tools`) that reads `/etc/os-release` from the staged qcow2 (guestfish,
  the same offline-probe mechanism as `probe_boot_entries`), following the
  `/usr/lib/os-release` symlink fallback.
- Add `_capture_os_release(scratch) -> dict | None`: parse shell-style
  `KEY=VALUE` lines (handling single/double-quoted values and skipping blank
  and `#`-comment lines), keeping `ID`, `VERSION_ID`, `PRETTY_NAME` when
  present. **Partial-key policy:** record `os_release` when **at least `ID`** is
  present; include `version_id`/`pretty_name` **only when present** (a distro
  such as Debian testing/sid ships `ID` with no `VERSION_ID` — that is a valid
  record, not a failure). Absent `ID` → treat as no record (`None`).
  **Advisory**: any probe failure (`CategorizedError`), a missing file, or an
  unparseable body degrades to `None` so the build still publishes.
- `_provenance` gains an `os_release: dict | None` parameter, added to the
  record **only when captured** — byte-identical to a pre-feature build when
  absent (the established ADR-0252/0253/0295 degradation contract).
- The value flows through the existing `RootfsBuildOutput.provenance` →
  `publish_image` and the staged sidecar → reconcile path (#977/ADR-0296)
  without change. **No migration.**

`os_release` is a build fact (verified from the image), distinct from the
operator-assigned catalog `name` and the build-input `distro`/`releasever`
already in provenance — so it also serves as a cross-check on a mislabelled name.

### 3. Operator `description` channel

Let an operator annotate the images they curate, reconciled to the catalog row:

- **Inventory:** add `description: str = ""` to `ImageEntry`
  (`src/kdive/inventory/model.py`), mirroring the existing
  `BuildConfigEntry.description`. Absent field → empty string (back-compatible).
  **Length cap:** validated at inventory-load time to a bounded maximum
  (`_MAX_IMAGE_DESCRIPTION` — 280 chars, a one-line hint, well under the
  worker's 1000-char value cap); an over-long value is rejected with a clear
  `configuration_error` naming the image and the limit. This keeps `images.list`
  token-safe: the field is echoed on every paginated row, so an unbounded
  operator paragraph would multiply across a page and blow an agent's context
  budget (the failure the repo's `ARTIFACT_GET_WINDOW_MAX_BYTES` / `search_text`
  caps already guard against).
- **Schema:** add a nullable `description` column to `image_catalog` (migration
  `0060`), defaulting `NULL`. `ImageCatalogEntry` gains `description: str | None
  = None`.
- **Reconcile:** plumb `entry.description` through `_create_entry` INSERT and
  `_update_entry` UPDATE (`inventory/reconcile/images.py:199`) exactly as
  `capabilities` is reconciled from the inventory entry, so editing the
  description in `systems.toml` and re-reconciling updates the row (removing it
  resets the row to `""` — reconcile is inventory-authoritative for this field).
- **Ownership invariant:** `description` is **reconcile-owned**. `publish_image`
  today writes a fixed column list that omits `description`
  (`services/images/publish.py:188-192`, and its state-only UPDATEs), so a build
  or publish of the same image **must not** clobber an operator description. This
  is a stated invariant, not an accident: publish must never be extended to write
  `description`. A test asserts a publish of an image that already has a
  description leaves it intact.
- **Surface:** include `description` in the `images.list` row envelope
  (decision 1) and the `images.describe` envelope, and echo it in the
  `profile_examples` example item (decision 4). It is **operator-attested** —
  advisory context, never a capability or liveness guarantee — and framed as
  such in the `Field`/guide text (parallels #893 `client_attested`, #867 client
  labels).

Rows created by build/publish/s3 with no matching inventory `[[image]]` simply
carry no description (`NULL` → `""`).

### 4. `profile_examples` de-anoints the first-declared image

`mcp/tools/lifecycle/systems/profile_examples.py`:

- Keep emitting one runnable example (still useful as a shape), but add to the
  local example item:
  - `available_images`: the count of public local-libvirt images in the
    inventory;
  - a `selection_note` whose wording is **conditioned on that count**, so it
    never asserts a choice that does not exist:
    - `> 1` → "chosen by declaration order; one of N public images — call
      `images.list`/`images.describe` to choose deliberately by
      `capabilities`/`os`/`description`";
    - `== 1` → "the only public image in this inventory" (no "choose from the
      list" steer);
    - `== 0` (placeholder `local` rootfs) → the existing placeholder note, with
      **no** `images.list` steer (the list is empty);
  - the chosen image's operator `description` when set (decision 3).
- The existing single-image and no-public-image (placeholder `local` rootfs)
  behaviours are preserved; `available_images` is `1` / `0` respectively. Tests
  cover all three arities (0, 1, many).

This reframes the example from *the* default to *an* example without removing
its ready-to-edit value.

### 5. Agent guidance

Add a short "choosing an image" section to the `toolsets-images.md` MCP resource
(`mcp/resources/_content/`): compare on `capabilities` (match the tag to the
task — `kdump` for crash-dump work, `build` for a kernel-build host, `drgn`/
`agent` for live introspection), read `os` for the target distro/release, and
treat `description` as operator context. No image names (keeps the resource
inventory-neutral and honest).

## Compatibility & deploy sequencing

`image_catalog` reads use `SELECT *` (`_LIST_SQL`/`_DESCRIBE_SQL`,
`reconcile/images.py::_load_config_rows`, `publish` `RETURNING *`) and validate
into `ImageCatalogEntry`, whose base `DomainModel` is `extra="forbid"`
(`domain/_records.py:12`). So the moment migration `0060` adds the `description`
column, any process still running **old** code (no `description` field) will
raise a `ValidationError` on every `image_catalog` read. This is the general
property of every `image_catalog` column addition under `extra="forbid"` +
`SELECT *`, not new to this change.

Sequencing invariant: **deploy the code (with `ImageCatalogEntry.description`)
before applying migration `0060`.** The migration is additive and nullable, so
new code reads a not-yet-migrated DB fine (`description` simply absent → `None`
default is never populated because the column read is via `SELECT *`; the field
defaults on the model). A new-code + migrated DB is the steady state. The only
unsafe window — migrated DB read by old code — is closed by ordering code first.
Migrations are forward-only (`db/migrate.py` applies `NNNN_*.sql` ascending; no
down-migrations); "rollback" of `0060` means leaving the unused nullable column
in place, which old *and* new code before this feature both tolerate as long as
they are not simultaneously live with the strict model — hence the ordering rule
above is the operative safeguard.

## Out of scope

- Any change to the `direct_kernel` signal or a curated `boot_kernel_count`
  (the original ask; unbuilt fixtures stay honestly `unverified`).
- A computed suitability score, ranking, or "best image for task X"
  recommendation (would re-introduce the editorialising ADR-0286/0295 reject).
- The operator-doc inconsistency (walkthrough provisions `-44` while its `cp`
  step installs a `-43`-only starter) — a real human-doc follow-up, not
  agent-facing; tracked separately.
- Capturing os-release for non-local-libvirt providers (only the local-libvirt
  build plane probes images here).
