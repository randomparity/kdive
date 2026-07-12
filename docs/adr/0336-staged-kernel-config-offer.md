# ADR 0336 — Offer the kernel config for staged-path and staged-volume images

- **Status:** Accepted
- **Date:** 2026-07-12
- **Issue:** #1132
- **Builds on:** ADR-0317 (image kernel-config offer), ADR-0331 (`has_kernel_config` disclosure), ADR-0228 (staged-path catalog source)
- **Related:** #1133 (adopt S3 as a required backend)

## Context

ADR-0317 lets an agent fetch a selected image's `/boot/config-<ver>` as a
known-good kernel-build starting point, and ADR-0331 advertises whether an image
offers one via a derived `has_kernel_config` boolean. Both are wired only through
the S3 publish path: `publish_image` uploads the config as a `.config` sibling
object and persists its `kernel_config_key` on the row. `images.kernel_config`
presigns a short-lived download URL for that object.

The two **staged** registration lanes never populate `kernel_config_key`, so their
images always report `has_kernel_config: false`:

- **local-libvirt staged-path** — `build-fs` extracts the config into
  `RootfsBuildOutput.kernel_config` but discards it (the provenance sidecar carries
  only `provenance`), and inventory reconcile's `_RealizedImage` has no
  `kernel_config_key` field.
- **remote-libvirt staged volume** — the operator stages a libvirt volume out of
  band; there is no build/capture step and kdive reaches the host only over
  `qemu+tls`, so nothing extracts the config.

On a typical dev host every distro `kdive-ready` image is staged-path, so the offer
is never available there.

## Decision

Keep the storage and serving contract exactly as ADR-0317 defined it — a `.config`
S3 object under `kernel_config_key`, served as a presigned whole-file URL — and
wire only the two staged **capture** paths to upload the captured config and set
`kernel_config_key`. The config is a build input delivered machine-to-machine, so
the whole-file URL is the right contract; an inline or windowed read would force a
~250 KB / ~65 K-token file through model context for no benefit.

No schema change: `kernel_config_key` already exists and migration `0063` made it
independent of the `object_key`/`volume`/`path` exactly-one CHECK, so a staged row
may carry it.

A pure `config_object_key(provider, name, arch, visibility, owner)` helper is
extracted from `kernel_config_object_key(request)` so publish, reconcile, and the
new command compute the same `images/{provider}[__{owner}]/{name}/{arch}.config`
key. Staged images are public, so the key omits the owner segment.

### Capture path 1 — local-libvirt staged-path

`build-fs` writes `output.kernel_config` to a DB-free `<dest>.config` sibling
beside the qcow2 (parallel to the provenance sidecar, and advisory like it: a write
failure is logged and swallowed; a `None` config writes nothing). Inventory
reconcile's staged-path realization reads the sibling with a bounded
`read_config_sibling`, and under one rule:

> If the row's `kernel_config_key` is absent **and** a `<path>.config` sibling is
> present, upload it to `config_object_key(...)` and set the key; otherwise
> **preserve** the row's existing key.

The upload is fully advisory — no object store (a no-S3 deployment), a put failure,
or an unreadable sibling degrades to no offer and never fails reconcile. Gating on
"key absent" avoids re-uploading on every reconcile tick. `_RealizedImage` gains a
`kernel_config_key` field and the reconcile INSERT/UPDATE carries it; for
non-staged-path sources the realized value preserves the row's existing key, so
reconcile never clobbers a `publish_image`- or `stage-volume`-owned key — the same
preserve-don't-wipe discipline already applied to `provenance`.

### Capture path 2 — remote-libvirt staged volume: `kdive stage-volume`

A new operator command captures at the only controllable moment — while the built
qcow2 is still local:

1. Probe `/boot/config-<ver>` locally on the qcow2 (the existing guestfish
   boot-facts probe), advisory.
2. `volUpload` the qcow2 into the remote host's storage pool over the existing
   mutual-TLS connection. **Fatal** — the volume must land.
3. When a config was captured, upload it to `config_object_key(...)` and `UPDATE`
   `kernel_config_key` on the **existing** `staged` row. The `[[image]]` must be
   declared and reconciled first (fail fast otherwise — you cannot stage a volume
   for an unknown image). Capture/attach is advisory; the upload having landed, a
   capture or attach miss leaves the volume staged with no offer.

Reconcile preserves the written key on later passes (a `staged` volume row has no
sibling, so the preserve branch always applies).

## Consequences

- Every staged image with a real `/boot/config-<ver>` now offers it identically to
  a published image; `has_kernel_config`, `images.kernel_config`, and the object
  layout are unchanged, so no agent-surface or serving change ships.
- The offer requires an object store. A genuinely no-S3 deployment (ADR-0228's
  minimal mode) keeps `has_kernel_config: false` for staged images; capture
  degrades silently rather than failing provisioning. #1133 tracks making S3 a
  required backend and removing this caveat.
- A staged-path rebuild that changes the config is not auto-refreshed while the key
  is set (staged-path carries no digest to detect the change). Clearing the row's
  `kernel_config_key` triggers a re-upload on the next reconcile; this is documented
  in the operator note.
- `stage-volume` introduces the volume-upload capability kdive previously left to
  operators, scoped to this one command and paired with the config capture.

## Rejected alternatives

- **DB blob + inline/windowed serving.** Considered while the staged lane was
  wrongly assumed to have no byte egress. The deployment has S3, the config is a
  whole-file build input that should never enter model context, and windowing hurts
  the dominant full-file fetch. Rejected as over-engineering that also tears down
  the working ADR-0317 serving path.
- **Reconcile probes the qcow2 directly.** Rejected: duplicates the probe build-fs
  already runs, couples reconcile to guestfish, and adds latency to every reconcile.
- **libvirt `volDownload` on-demand probe** for the remote volume. Rejected: a
  multi-GB transfer per image, impractical partial reads on qcow2, and unverified
  feasibility over `qemu+tls`.
- **Operator-supplied config in `systems.toml`.** Rejected: an operator claim rather
  than a KDIVE-captured known-good config, plus operator burden.
