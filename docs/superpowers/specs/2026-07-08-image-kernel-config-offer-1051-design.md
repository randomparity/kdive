# Advertise default-kernel version + offer the image's kernel `.config` — design (Spec 2 of 3)

- **Status:** Draft
- **Date:** 2026-07-08
- **Issue:** #1051
- **ADR:** [ADR-0317](../../adr/0317-image-kernel-config-offer.md)
- **Scope:** Spec 2 of the three-spec redesign of kernel build & config handling.
  Spec 1 ([remove server-build lane](2026-07-08-remove-server-build-lane-design.md),
  merged) made building the agent's job; Spec 3 (debug-feature advertise + gate) is
  out of scope here.

## Context

Spec 1 made the agent responsible for building the kernel locally and uploading the
artifacts. To build a kernel that boots a given rootfs, the agent needs two things
kdive does not yet hand them:

1. **Which kernel the image ships by default** — so the agent knows what version it
   is starting from. Today the default kernel version is computed transiently at
   provision time in `providers/local_libvirt/lifecycle/baseline_kernel.py`
   (`version = kernel[len("vmlinuz-"):]`) and **never persisted** to the catalog.
2. **A known-good starting `.config`** — the image's own `/boot/config-<ver>`, which
   by definition already boots that image. Extraction and offer **do not exist**.

Distro **name** and **version** are already surfaced: `images.list` / `images.describe`
emit a compact `os` identity (`id` / `version_id`) from the verified `/etc/os-release`
provenance (ADR-0311). This spec adds the kernel half.

## Requirements addressed

From issue #1051:

> **R1.** Rootfs images advertise distro name, distro version, and **default kernel
> version** for informed agent selection. (name/version already done; this adds
> default kernel version.)
>
> **R2.** After selecting an image, kdive provides a method to hand the agent that
> image's kernel **config file** (`/boot/config-<ver>`), which the agent then manages
> themselves.
>
> **R3.** kdive **does not validate** the config — the offered config is a starting
> point only.

## Goal

At image-build time, capture the image's default kernel version and its
`/boot/config-<ver>` bytes. Persist the version in catalog provenance (surfaced by
`images.list`/`describe`); store the config in the object store and add a read tool,
`images.kernel_config`, that hands the agent a short-lived download URL for it. kdive
never inspects or validates the config.

## Decisions

1. **Default kernel version is an advisory provenance operand**, captured at build
   time exactly like `boot_kernel_count` / `os_release` / `makedumpfile_version`
   (ADR-0295/0311/0253): a `probe_*` read in `images/planes/_build_common.py`, a
   `_capture_*` on the build plane that degrades any failure to `None`, and conditional
   inclusion in `_provenance`. It is the single non-rescue `vmlinuz-<ver>` in `/boot`
   (classified by the existing `baseline_kernel_names`) — the same kernel a
   direct-kernel provision would boot. Zero or more-than-one non-rescue kernel → omitted
   (ambiguous; `boot_kernel_count` already flags this image as non-provisionable).

2. **The `.config` is a separate object-store artifact, not inline in provenance.** A
   kernel `.config` is ~250 KB of text. `provenance` is surfaced verbatim by
   `images.describe`, so embedding the config there would bloat every describe/list
   response. It is written as a sibling object of the qcow2 during publish, at
   `images/{owner_kind}/{name}/{arch}.config`, and its key is persisted on a new
   nullable `image_catalog.kernel_config_key` column (withheld from the agent surface,
   like `object_key`).
   - *Rejected:* lazy on-demand guestfs extraction at fetch time — the server would have
     to download the multi-GB qcow2 and run libguestfs to answer a read call.

3. **`images.kernel_config` returns a presigned download URL, not inline bytes**,
   borrowing the presigned-URL *return shape* of `artifacts.fetch_raw` (ADR-0243). The
   agent needs the whole file to feed `make`, so a windowed inline read (`artifacts.get`)
   is useless and 250 KB inline is large. It resolves the row under the **same visibility
   predicate** as `images.describe` (public, or owned-private with `viewer`), HEADs the
   config object, presigns a short-lived GET (`KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS`), and
   returns the URL under `refs.download_uri` with `data.default_kernel_version` /
   `data.size_bytes` / `data.ttl`.
   - **The egress is intentionally not audited**, unlike `fetch_raw`. `fetch_raw` audits
     because its assets are `SENSITIVE` and contributor-gated (a project-scoped
     `audit.record` under the Run's project). A kernel `.config` is REDACTED-class and
     visibility-gated identically to `images.describe`/`images.list`, which surface the
     same image — including full `provenance` — with no audit. A project-scoped
     `audit.record` is in fact **unavailable** here: a public image has `owner=None` and a
     legitimate reader may hold zero granted projects, so `audit.record` (which requires
     `event.project in ctx.projects`) would raise for the common path. Auditing the config
     fetch but not the richer `describe` of the same row would be inconsistent; the config
     is not audited.

4. **No validation, anywhere.** kdive stores the config bytes verbatim and never parses
   or checks them. The config is REDACTED-class (kernel `CONFIG_*` symbols carry no
   secrets); the gate is image visibility, not sensitivity.

5. **Config presence is optional even for a built S3 image.** An image whose `/boot`
   has no single baseline kernel, or no `config-<ver>` file, or that never went through
   the build plane (operator-staged `path`/`volume` images), simply has no stored
   config. `images.kernel_config` returns a `configuration_error` with reason
   `kernel_config_unavailable` in that case (mirroring `vmcore_unavailable`).

6. **The config object joins the existing object lifecycle by extending the leaked-object
   cross-check.** The catalog's reclamation model (ADR-0092) is: a row-deletion path may
   delete the object eagerly *and* `repair_leaked_images` reclaims any image-prefix object
   no row references, past the publish grace. Both mechanisms key **only** off `object_key`
   today, so a second object under `images/` would be (a) deleted as "leaked" for a live,
   registered image, and (b) never a false-positive reclaim only because it *is* referenced
   — the opposite failures. The fix is one change plus one symmetry:
   - **Leaked-sweep cross-check** (`reconciler/cleanup/images.py`): protect an object
     referenced by `object_key` **OR** `kernel_config_key` (`SELECT EXISTS(... WHERE
     object_key = %s OR kernel_config_key = %s)`). This protects a live image's config and,
     because the check is per-object, still reclaims a config whose owning row is gone (no
     row carries that key) — so every row-deletion path is backstopped exactly as the qcow2
     is.
   - **Eager delete on private expiry** (`services/images/retention.py`): `expire_one_
     private_image` deletes the config object alongside the qcow2 when `kernel_config_key`
     is set (prompt reclamation, mirroring the qcow2's eager delete). Dangling-row removal
     and inventory prune delete only the row and rely on the leaked-sweep backstop — the
     same treatment those paths already give the qcow2.

7. **`kernel_config_key` is persisted at adopt/insert, before the object write — true
   symmetry with `object_key`.** The qcow2's `object_key` is set on the `pending` row
   *before* the qcow2 is written, so the leaked-sweep cross-check protects a pending row's
   object the instant the row exists. The config leg does the same: when
   `request.kernel_config` is present, `_adopt_or_insert_pending` / `_insert_pending` set
   the deterministic `{arch}.config` key on the `pending` row alongside `object_key`,
   *before* either object is written. The config object is then written after the qcow2
   HEAD-gate. So during the whole `pending` window the config object — once written — is
   **row-protected**, not merely grace-protected, exactly like the qcow2. A crash before
   the object write leaves a key on the row with no object (recovered by the
   deterministic-key idempotent re-publish); a crash after leaves a row-protected object
   the re-publish overwrites. `kernel_config_key` does not participate in the
   `object_key`/`volume`/`path` exactly-one CHECK, so setting it on a `pending` row is
   unconstrained.

## What changes

### New

- **`images/planes/_build_common.py`** — `probe_kernel_config(qcow2_path, version)
  -> str | None`: read `/boot/config-<version>` read-only via `guestfish -i cat`,
  returning the text or `None` when absent (mirrors `probe_makedumpfile_marker`).
  Raises only `MISSING_DEPENDENCY` / `INFRASTRUCTURE_FAILURE`, caught by the advisory
  caller. Exposed as a `KernelConfigProbeSeam` with a `DEFAULT_KERNEL_CONFIG_PROBE`.
- **`db/schema/0063_image_catalog_kernel_config_key.sql`** — additive, forward-only
  (ADR-0015): `ALTER TABLE image_catalog ADD COLUMN kernel_config_key text;` No CHECK
  change — the column is independently optional; it does not participate in the
  `object_key`/`volume`/`path` exactly-one invariant.
- **`mcp/tools/catalog/images.py`** — `images.kernel_config` read tool +
  `kernel_config` handler, registered in the existing `register(app, pool)` alongside
  `images.list`/`describe`. Uses `object_store_from_env` as a default `store_factory`
  (like `fetch_raw`); no registrar signature change.

### Surgically changed (boundary files)

- **`providers/local_libvirt/rootfs_build.py`** — fold the existing single
  `probe_boot_entries` read into one `_capture_boot_facts(scratch)` that returns the
  `boot_kernel_count`, the `default_kernel_version` (the lone non-rescue kernel, else
  `None`), and the `.config` text (via `probe_kernel_config` for that version, else
  `None`). Add `default_kernel_version` to `_provenance` when present; thread the
  `.config` bytes out on `RootfsBuildOutput`. Wire `probe_kernel_config` into
  `RootfsBuildTools`.
- **`images/planes/base.py`** (`RootfsBuildOutput`) — add `kernel_config: bytes | None
  = None` (the extracted config, `None` when absent). Provenance already carries the
  version.
- **`services/images/publish.py`** — add `kernel_config: bytes | None` to
  `PublishRequest` and a `kernel_config_object_key(request)` helper (the deterministic
  `{arch}.config` key). When `request.kernel_config` is present,
  `_adopt_or_insert_pending` / `_insert_pending` set `kernel_config_key` on the `pending`
  row alongside `object_key` (before any object write, Decision 7); after the qcow2
  HEAD-gate, write the config object and HEAD-gate it. A config-write/HEAD failure fails
  the publish (the row stays `pending` for the reconciler); recovery is the
  deterministic-key idempotent re-run + leaked-sweep backstop of Decisions 6–7 — not a
  bespoke rollback. When `request.kernel_config` is absent, no key is set and no second
  object is written.
- **`reconciler/cleanup/images.py`** — extend the `_delete_if_leaked` cross-check to
  protect an object referenced by `object_key` **OR** `kernel_config_key` (Decision 6),
  so the leaked-sweep never deletes a live image's config and still reclaims an orphaned
  one.
- **`services/images/retention.py`** — `repair_expired_private_images` selects
  `kernel_config_key` alongside `object_key`; `expire_one_private_image` deletes the
  config object (when set) alongside the qcow2, object-before-row like today (Decision 6).
- **`jobs/handlers/image_build.py`** — pass `output.kernel_config` into
  `PublishRequest`.
- **`domain/catalog/images.py`** (`ImageCatalogEntry`) — add `kernel_config_key: str |
  None = None`.
- **`mcp/tools/catalog/images.py`** — surface `default_kernel_version` in the
  `images.list` row envelope and the `images.describe` envelope (projected from
  provenance, `""` when absent), and update the wrapper docstrings.

## Data flow

```
build plane (live_vm, libguestfs)
  probe_boot_entries → baseline_kernel_names → default_kernel_version
  probe_kernel_config(version) → .config text
        │                                   │
        ▼                                   ▼
  provenance["default_kernel_version"]   RootfsBuildOutput.kernel_config (bytes)
        │                                   │
        ▼ publish_image                     ▼
  image_catalog.provenance            object images/…/{arch}.config
                                       image_catalog.kernel_config_key
        │                                   │
        ▼ images.list/describe              ▼ images.kernel_config
  data.default_kernel_version         refs.download_uri (presigned GET)
```

## Testing strategy

- **Unit (no libguestfs):** inject a fake `probe_kernel_config` / `probe_boot_entries`
  into `RootfsBuildTools`; assert `default_kernel_version` present for a single-kernel
  `/boot`, omitted for zero/multi-kernel, and `RootfsBuildOutput.kernel_config` carries
  the fake bytes / is `None` when the probe returns `None`.
- **Publish:** a `PublishRequest` with `kernel_config` writes a second object and sets
  `kernel_config_key`; without it, no second write and the column stays `NULL`. Reuse
  the existing fake object store.
- **MCP `images.kernel_config`:** row with `kernel_config_key` + present object →
  `refs.download_uri` + `data.default_kernel_version`/`size_bytes`; row without the key,
  or object absent → `configuration_error` reason `kernel_config_unavailable`; a
  private image the caller cannot view → `not_found` (byte-identical to absent, reusing
  the describe predicate); malformed id → `configuration_error`.
- **No-validation:** a config whose bytes are arbitrary (e.g. drop a symbol the old
  server-build gate required) round-trips through publish → `images.kernel_config`
  unchanged and unchecked.
- **Migration:** `tests/db/test_migrate.py` covers the additive column; a pre-feature
  row (no `kernel_config_key`) reads back `None`.
- **Config-object lifecycle:** the leaked-sweep leaves a registered image's config object
  in place past grace (protected via `kernel_config_key`) but reclaims one whose row was
  deleted; a private-image expiry deletes both the qcow2 and the config object; a
  crash-before-flip config object (no persisted key) is reclaimed by the leaked-sweep.
- **Guardrails:** `just lint`, `just type`, `just test`, `just docs-check` green; the
  generated MCP tool reference regenerated for the new `images.kernel_config` tool.
- **Live smoke (not CI-gated):** build a real image → `images.describe` shows
  `default_kernel_version` → `images.kernel_config` mints a URL that downloads the
  image's `/boot/config-<ver>`.

## Risks

- **Config absent for non-build-plane images.** Staged `path`/`volume` images and
  pre-feature rows have no config. Mitigation: `kernel_config_key` is nullable and the
  fetch degrades to `kernel_config_unavailable`; no reader assumes presence.
- **Second object-store write in publish.** Adds a failure point to a two-write that is
  currently one write. Mitigation: the config write is ordered after the qcow2 HEAD-gate
  and before the `registered` flip, so a config failure leaves the row `pending` (the
  reconciler's existing recovery path), never a half-registered image (Decision 7).
- **Second object under the `images/` prefix breaks the object-key-only sweeps.** The
  leaked-sweep and the private-expiry delete both key only off `object_key` today, so an
  unmodified sweep would delete a live image's config as "leaked" and orphan a deleted
  image's config. Mitigation: Decision 6 extends the leaked cross-check to
  `object_key OR kernel_config_key` and the private-expiry path to delete the config too;
  covered by the config-object-lifecycle tests. This is the load-bearing correctness fix,
  not an afterthought.
- **Object-store growth.** One ~250 KB text object per built image. Negligible next to
  the multi-GB qcow2; shares the image's retention class and owner-scoped prefix, and is
  reclaimed with the image via the extended sweep (Decision 6).

## Relationship to the other specs

```
Spec 1 (merged)  Remove server-build lane        → upload-only, no validation
Spec 2 (this)    Image metadata + config offer   → default kernel version + /boot/config-* hand-off
Spec 3           Debug-feature advertise + gate   → per-feature CONFIG manifest; arm only what the kernel supports
```

Spec 3 will *read* the agent's uploaded `effective_config` (advisory) to arm only the
features the kernel supports; this spec hands the agent the starting config it reads
back. The two are independent: Spec 2 offers, Spec 3 gates.
