# ADR 0317 — Advertise default-kernel version and offer the image's kernel `.config`

- **Status:** Accepted
- **Date:** 2026-07-08
- **Deciders:** kdive maintainers
- **Issue:** #1051 (redesign spec 2 of 3)
- **Spec:** [image-kernel-config-offer-1051](../superpowers/specs/2026-07-08-image-kernel-config-offer-1051-design.md)
- Extends ADR-0092 (image catalog), ADR-0243 (raw-asset presigned egress), ADR-0295
  (`boot_kernel_count` advisory operand), ADR-0311 (`os_release` provenance +
  agent-facing image selection). Follows ADR-0316 (remove server-build lane), which
  made local kernel building the agent's job. Supersedes nothing.

## Context

ADR-0316 removed the server-build lane: the agent now builds the kernel locally and
uploads the artifacts. To build a kernel that boots a chosen rootfs, the agent needs
two facts kdive does not surface — the version of the kernel the image ships by
default, and a known-good starting `.config` (the image's own `/boot/config-<ver>`,
which by construction already boots that image).

Distro name/version already reach the agent as a compact `os` identity on
`images.list`/`describe` (ADR-0311). The default kernel version is computed only
transiently at provision time (`baseline_kernel.py`) and never persisted; the
`/boot/config-<ver>` file is never extracted. The cross-cutting rule from the redesign
holds: kdive does not validate the config in any way.

## Decision

**1. Default kernel version is a build-time advisory provenance operand.** Captured in
the local-libvirt build plane exactly like `boot_kernel_count` / `os_release`
(ADR-0295/0311): a read-only `guestfish` probe, a `_capture_*` that degrades any
failure to `None`, and conditional inclusion in `provenance`. It is the single
non-rescue `vmlinuz-<ver>` in `/boot` (classified by the existing `baseline_kernel_names`,
the same rule provisioning uses); zero or more-than-one candidate omits it, because the
default is then ambiguous and `boot_kernel_count` already reports the image
non-provisionable. `images.list` and `images.describe` surface it as
`data.default_kernel_version`.

**2. The `.config` is stored as a separate object-store artifact.** During publish the
extracted config bytes are written as a sibling object of the qcow2 at
`images/{owner_kind}/{name}/{arch}.config`, ordered after the qcow2 HEAD-gate and before
the `registered` flip. Its key is persisted on a new nullable
`image_catalog.kernel_config_key` column (additive, forward-only), withheld from the
agent surface like `object_key`.

**3. `images.kernel_config` hands the agent a presigned download URL.** A new read tool
resolves the row under the same visibility predicate as `images.describe` (public, or
owned-private with `viewer`), HEADs the config object, presigns a short-lived GET, and
returns it under `refs.download_uri` with `data.default_kernel_version` / `size_bytes` /
`ttl`. It never returns inline bytes and never inspects the config.

**4. No validation.** kdive stores and serves the config verbatim. It is REDACTED-class
(kernel `CONFIG_*` symbols carry no secrets); the gate is image visibility, not
sensitivity.

## Consequences

- The agent selects an image on merit (distro, version, **default kernel version**) in
  one `images.list` call, then fetches its known-good starting config in one
  `images.kernel_config` call — no N+1, no libguestfs on the read path.
- One ~250 KB text object per built image, owner-scoped and sharing the image's
  retention class, so it is swept with the image. Negligible next to the qcow2.
- The config offer is best-effort: staged `path`/`volume` images, pre-feature rows, and
  images whose `/boot` lacks a single baseline kernel or a `config-<ver>` file have no
  stored config, and the fetch degrades to a `kernel_config_unavailable`
  `configuration_error`. No reader assumes presence.
- Publish gains a second object write. It is ordered so a config-write failure leaves the
  row `pending` (the reconciler's existing recovery), never a half-registered image — the
  row-first two-write invariant is preserved.

## Considered & rejected

- **Inline the config in `provenance`.** `provenance` is surfaced verbatim by
  `images.describe`; a ~250 KB config would bloat every describe/list response. Rejected
  for a separate object keyed off the row.
- **Return the config inline from `images.kernel_config` (like `artifacts.get`).** The
  agent needs the whole file to feed `make`, so a windowed inline read is useless and a
  full inline read is large. Rejected for the `artifacts.fetch_raw` presigned-URL idiom
  (ADR-0243).
- **Lazy on-demand extraction at fetch time.** The server would download the multi-GB
  qcow2 and run libguestfs to answer a read call. Rejected for build-time capture, where
  the image is already mounted for the other probes.
- **Derive the config object key deterministically instead of persisting it.** The
  catalog's stance (ADR-0092) is to persist object keys and never recompute them.
  Rejected for an explicit nullable column.
- **Validate the offered config against the old server-build requirements.** Forbidden
  by the redesign's no-validation rule (ADR-0316); the image's own config already boots
  the image, so bootability is delivered by offering a known-good starting point, not by
  checking.
