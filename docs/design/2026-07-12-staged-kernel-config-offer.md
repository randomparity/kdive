# Kernel-config offer for staged-path and staged-volume images

- **Issue:** #1132
- **ADR:** 0336
- **Builds on:** ADR-0317 (image kernel-config offer), ADR-0331 (`has_kernel_config` disclosure), ADR-0228 (staged-path catalog source)
- **Related direction:** #1133 (adopt S3 as a required backend)

## Problem

`images.list` / `images.describe` report `has_kernel_config: false` for every
local-libvirt **staged-path** and remote-libvirt **staged** (volume) image, so an
agent cannot fetch a known-good `/boot/config-<ver>` starting point for those
images. On a typical dev host every distro `kdive-ready` image is staged-path, so
the offer is never available there.

The offer (ADR-0317) is wired only into the S3 publish path
(`services/images/publish.py` → `publish_image`, which sets `kernel_config_key`).
The two staged registration lanes never populate `kernel_config_key`:

- **local-libvirt staged-path** — `build-fs` extracts the config into
  `RootfsBuildOutput.kernel_config` (`rootfs_build.py`) and then discards it: the
  provenance sidecar carries only `provenance`, not the config bytes, and inventory
  reconcile's `_RealizedImage` has no `kernel_config_key` field, so the row is
  registered with it `NULL`.
- **remote-libvirt staged volume** — the operator stages a libvirt volume out of
  band; there is no build/capture step, and kdive reaches the host only over
  `qemu+tls` (no exec channel), so nothing ever extracts the config.

## Goal

Every staged image that carries a real `/boot/config-<ver>` offers it through the
existing `images.kernel_config` tool, identically to an S3-published image.

## Non-goals

- No change to the storage or serving contract. The config stays an S3 object
  under `kernel_config_key`; `images.kernel_config` presigns a whole-file download
  URL. The bytes flow machine-to-machine (fetch → kernel build) and never pass
  through model context — the correct contract for a build input. No inline/windowed
  read, no search.
- No schema change. `kernel_config_key` already exists and is independent of the
  `object_key`/`volume`/`path` exactly-one CHECK (migration `0063`), so a staged
  row may carry it.
- No support for the offer in a genuinely no-S3 deployment. Capturing to S3
  requires an object store; a no-S3 deployment (ADR-0228's minimal mode) keeps
  `has_kernel_config: false`. This is documented, not worked around; #1133 tracks
  making S3 a required backend.

## Design

The storage/serving spine is unchanged. Only the two staged **capture** paths are
wired to upload the captured config to the object store and set `kernel_config_key`.

### Shared: config object key

Extract a pure key helper so publish, reconcile, and the new command compute the
same key without a `PublishRequest`:

```
config_object_key(provider, name, arch, visibility, owner) -> str
```

It returns `images/{provider}[__{owner}]/{name}/{arch}.config` (owner segment only
for a private image). `kernel_config_object_key(request)` becomes a thin wrapper.
Staged images are public (staged-path is public-only per `inventory/model.py`), so
the key is `images/{provider}/{name}/{arch}.config`.

### Capture path 1 — local-libvirt staged-path

1. **build-fs writes a DB-free sibling.** After a successful build, `run_build_fs`
   writes `output.kernel_config` (when present) to `<dest>.config` — raw bytes,
   beside the qcow2, parallel to the existing `<dest>.provenance.json` sidecar.
   Advisory: a sibling-write failure is logged and swallowed (the qcow2 is the
   primary artifact), exactly like the provenance sidecar. When
   `output.kernel_config` is `None` (no single baseline kernel / no config / probe
   failure) no sibling is written.
2. **A bounded sidecar reader.** `read_config_sibling(qcow2) -> bytes | None` reads
   `<qcow2>.config` with a byte cap (a config is ~250 KB; cap generously, e.g.
   4 MiB) and returns `None` on absent/oversize/unreadable — never raising. Mirrors
   `read_sidecar`'s defensive read.
3. **Reconcile uploads and sets the key.** In `inventory/reconcile/images.py`, the
   `staged-path` realization gains config handling under a single rule:

   > If the row's `kernel_config_key` is currently absent **and** a
   > `<path>.config` sibling is present, upload it to `config_object_key(...)` and
   > set the key. Otherwise **preserve** the row's existing `kernel_config_key`.

   The upload is **advisory**: any failure — no object store configured
   (`object_store_from_env` raises in a no-S3 deployment), a put failure, an
   unreadable sibling — degrades to no offer (`kernel_config_key` stays `NULL`) and
   never fails reconcile. Gating on "key currently absent" avoids re-uploading on
   every reconcile tick.

   `_RealizedImage` gains a `kernel_config_key: str | None` field; the reconcile
   INSERT/UPDATE column list includes `kernel_config_key`. For non-staged-path
   sources the realized value preserves the row's existing key (build/S3 rows own
   their key via `publish_image`; a `staged` volume row's key is owned by
   `stage-volume`, below), so reconcile never clobbers it — the same
   preserve-don't-wipe discipline already applied to `provenance`.

   Refresh caveat: because a staged-path row has no digest, a rebuild that produces
   a new config is not auto-refreshed while the key is set. To refresh, clear the
   row's `kernel_config_key` (documented in the operator note); the next reconcile
   re-uploads from the sibling.

### Capture path 2 — remote-libvirt staged volume: `kdive stage-volume`

A new operator command is the capture seam, since remote has no build hook and
kdive reaches the host only over `qemu+tls`:

```
kdive stage-volume --provider remote-libvirt --image <name> --from <built.qcow2> [--arch <arch>]
```

Steps, in order:

1. **Probe locally.** Run the existing guestfish boot-facts probe
   (`probe_boot_entries` + `probe_kernel_config`) against the local `built.qcow2`
   to extract `/boot/config-<ver>` before the image leaves the worker. Advisory: a
   probe failure yields no config (upload still proceeds, no offer).
2. **Upload the qcow2 to the remote pool.** `volUpload` the qcow2 into the remote
   host's storage pool over the existing mutual-TLS libvirt connection. **Fatal:**
   the volume must land, or the command fails.
3. **Attach the config.** When a config was captured, upload it to
   `config_object_key(...)` and `UPDATE image_catalog SET kernel_config_key = …
   WHERE (provider, name, arch)` on the **existing** `staged` row. Contract: the
   `[[image]]` must already be declared and reconciled (the row exists); otherwise
   fail fast with an actionable message — you cannot stage a volume for an image
   the catalog does not know. Config capture/attach is advisory: the upload having
   succeeded, a capture or attach failure leaves the volume staged with no offer.

Reconcile preserves the `stage-volume`-written key on subsequent passes (a `staged`
volume row has no sibling, so the preserve branch always applies).

### Serving — unchanged

`images.kernel_config` and the `has_kernel_config` derivation
(`kernel_config_key is not None`) are untouched. A staged image with a captured
config now presigns a URL identically to a published one.

## Error handling

- Capture is advisory everywhere (matches ADR-0317): a probe/upload/attach failure
  never fails a build, a reconcile, or the volume upload.
- `stage-volume` volume upload is fatal (`INFRASTRUCTURE_FAILURE` on a libvirt
  fault); a missing catalog row is `CONFIGURATION_ERROR` with a fix hint.
- Serving with no/absent config is unchanged: `configuration_error`,
  `reason=kernel_config_unavailable`.

## Testing

- **Unit** — `config_object_key` scoping (public vs private); `read_config_sibling`
  bounds (absent / oversize / unreadable → `None`, present → bytes); `build-fs`
  writes the sibling on capture and skips on `None`; reconcile uploads + sets the
  key when absent+sibling-present, preserves otherwise, and degrades to no-offer
  when the object store is absent or the put fails; `stage-volume`
  probe→upload→attach with a mocked libvirt connection and object store, including
  the fatal volume-upload path and the missing-row fail-fast; `has_kernel_config`
  derivation over a staged row that now carries a key.
- **Live functional proof** (this repo's standard that a live test forces real
  capability): rebuild `fedora-kdive-ready-44` locally → reconcile → verify
  `images.kernel_config` returns a presigned URL to the real `/boot/config-<ver>`
  and `has_kernel_config: true`. A `stage-volume` round-trip on the remote tier
  sets the offer on the staged volume row.

## Rejected alternatives

- **DB blob + inline/windowed serving.** Considered when the staged lane was
  assumed to have no egress. The deployment has S3 (MinIO in compose/Helm/
  live-stack), the config is a whole-file build input that should never enter model
  context, and windowing hurts the dominant full-file fetch. Rejected as
  over-engineering that would also tear down the working ADR-0317 serving path.
- **Reconcile probes the qcow2 directly (no build-fs sibling).** Reconcile has the
  staged path and could run guestfish itself. Rejected: it duplicates the probe
  build-fs already runs, couples reconcile to guestfish, and adds latency to every
  reconcile pass.
- **libvirt `volDownload` probe for the remote volume.** Extract `/boot/config` by
  streaming the volume on demand. Rejected: a multi-GB transfer per image, qcow2
  makes partial reads impractical, and feasibility over `qemu+tls` is unverified.
- **Operator supplies the config in `systems.toml`.** Rejected: an operator-declared
  artifact rather than a KDIVE-captured known-good config, and added operator burden.
