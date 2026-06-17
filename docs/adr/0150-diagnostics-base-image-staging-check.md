# ADR 0150 — Diagnostics: remote-libvirt base-image-staging check

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0091](0091-doctor-diagnostics-model.md) (the
  `Check`/three-state model + server-vs-worker vantage), [ADR-0125](0125-diagnostics-host-reachability.md)
  (the server-vantage `qemu+tls://` reachability probe whose connection lifecycle this reuses),
  [ADR-0080](0080-remote-provisioning-disk-image-profile.md) (the operator-staged
  `base_image_volume` prerequisite and its `CONFIGURATION_ERROR`-if-absent contract),
  [ADR-0139](0139-diagnostics-worker-vantage-substitution-honesty.md) (the
  feature-not-enabled substitution honesty this check is unaffected by).
- **Spec:** [`../specs/2026-06-17-diagnostics-base-image-staging.md`](../specs/2026-06-17-diagnostics-base-image-staging.md)

## Context

`ops.diagnostics(remote-libvirt)` reports a healthy environment whenever
`remote_libvirt_reachability` passes — the `qemu+tls://` connection opened and `getInfo()`
returned. But reachability **openly disclaims usability**
(`checks.py`: "libvirt-reachable only; config usability still surfaces at provision"), and the
one operator prerequisite that actually blocks provisioning goes unchecked: the
**base-image volume must be staged on the host's storage pool** before any System can be
provisioned (ADR-0080). When it is absent, `systems.provision` fails deep in
`ensure_named_overlay` (`storage.py`) with `CONFIGURATION_ERROR` —
*after* the caller has requested an allocation and burned the provisioning attempt on it.

Black-box defect #4: a run passed `remote_libvirt_reachability` and then failed
`systems.provision` on exactly this missing-volume condition, with the diagnostic admitting in
its own pass detail that it does not check usability. The class of failure (an operator
prerequisite a cheap server-vantage read could have caught) is the one a doctor exists to
surface before the user pays for it.

The data needed to know *which* volume to look up is already in `systems.toml`, resolvable
without a worker job or DB read:

- the declared `[[remote_libvirt]]` instance carries `base_image` (a cross-reference the
  inventory loader already validates names a declared `[[image]]`);
- that `[[image]]`, when `source.kind = "staged"`, carries `.volume` — the staged qcow2 volume
  name the provider looks up on the pool;
- the pool name is the operational `KDIVE_REMOTE_LIBVIRT_STORAGE_POOL` setting (default
  `default`), the same one provisioning resolves through `remote_config_from_inventory`.

So the check has the same vantage and inputs as reachability and needs no new dispatch surface.

## Decision

Add a **server-vantage** diagnostic check, `remote_libvirt_base_image_staging`, that looks up
the configured base-image volume on the host's storage pool and reports three-state:

- **`PASS`** — the pool exists and the volume is staged.
- **`FAIL`** — the pool exists but the volume is absent. `fix` is the ADR-0080 operator staging
  remediation, reusing the `storage.py` "base image volume … is not staged on the remote host's
  storage pool (an operator prerequisite, ADR-0080)" wording so doctor and provision speak with
  one voice. `failure_category = configuration_error`.
- **`ERROR`** — the check could not reach a verdict: the host is unreachable
  (`transport_failure`), the configured **pool** does not exist (a different operator
  misconfiguration — naming a missing-volume fix would be a confident-wrong-fix), the inventory
  is unresolvable/multi-instance, the cert refs do not resolve, or the resolved image is not a
  `staged` image (a build/S3 image has no operator-staged volume to look up). `detail` says what
  blocked it; it never carries a fix. `failure_category` is `transport_failure` for a host-down
  and `configuration_error` otherwise.

### Layering — mirror the reachability seam exactly

The check class lives in `diagnostics/checks.py` next to the other `Check`s; it consumes an
injected async probe returning a small `BaseImageStagingOutcome` enum, so the check holds the
three-state policy and the libvirt boundary is mocked in its unit tests (the reachability
pattern). The production probe is a new `diagnostics/base_image_staging.py` adapter that:

1. resolves the single `[[remote_libvirt]]` instance and the storage-pool setting (deferred to
   probe time, so a post-assembly inventory drift reports a legible `configuration_error` rather
   than collapsing the report — same rationale as `reachability.py`);
2. resolves that instance's `base_image` to its `[[image]]` entry and extracts the `staged`
   `.volume` (a non-staged source → an indeterminate `configuration_error` outcome);
3. opens the connection through the **shared** `remote_connection` lifecycle (mutual-TLS
   pkipath materialize → connect → cleanup) with a connection slice typed to the storage
   methods (`storagePoolLookupByName` → `storageVolLookupByName`), offloaded with
   `asyncio.to_thread`;
4. maps the libvirt result to the outcome via a **shared** `lookup_volume_staged(conn, pool,
   volume)` helper in `providers/remote_libvirt/lifecycle/storage.py` — the single
   "is volume X staged?" path the companion volume-discoverability read (#511) reuses.

### The shared lookup helper

`lookup_volume_staged` returns a three-state `VolumeStaging` enum (`STAGED` / `ABSENT` /
`POOL_ABSENT`), distinguishing "pool there, volume missing" (→ check `FAIL`) from "pool itself
missing" (→ check `ERROR`). It maps `VIR_ERR_NO_STORAGE_POOL`/`VIR_ERR_NO_STORAGE_VOL` to the
two absent states and re-raises any other `libvirtError` (an infra fault the probe maps to an
indeterminate outcome). It does **not** open or close the connection — the caller owns the
connection lifecycle — so both the diagnostic probe and #511's read can share it without
double-managing TLS.

### Wiring

`_remote_libvirt_checks()` appends the new check after `RemoteLibvirtReachabilityCheck`. It is
assembled only when `is_remote_libvirt_configured()` is true (the existing gate). No new MCP
tool, parameter, config setting, migration, DDL, or generated-doc change: `ops.diagnostics`
already surfaces every assembled check generically.

## Consequences

- A deployment with an unstaged base image gets a `FAIL` (with the staging fix) from
  `ops.diagnostics`/`doctor` **before** an allocation is requested, and the doctor gate exits
  nonzero on it — the acceptance criterion.
- The check is additive: reachability and `secret_ref` are untouched, and a deployment with the
  volume staged sees an extra `PASS`.
- The "is volume X staged?" logic has one home (`lookup_volume_staged`), shared with #511; a
  future change to the lookup (e.g. matching on volume path) changes one function.
- The pool-vs-volume distinction is load-bearing: collapsing "pool missing" into the
  volume-missing `FAIL` would emit a stage-the-volume fix for a missing-pool fault. Kept as an
  `ERROR` per ADR-0091's confident-wrong-fix prohibition.
- A non-`staged` base image (build/S3 source) yields an `ERROR`, not a `FAIL`: there is no
  operator-staged volume to look up, so a staging fix would be wrong. The reachability check
  still covers that deployment's host-reachability contract.

## Considered & rejected

- **Fold the staging verdict into `remote_libvirt_reachability`.** Reachability is deliberately
  scoped to libvirt-reachability (ADR-0125) and is a single-outcome probe; overloading it would
  blur its `transport_failure` semantics and make a host-up/volume-missing host read as a
  reachability fail. A distinct check keeps each verdict's failure category honest.
- **A worker-vantage check.** The volume lookup is a storage-pool read over the same
  server-opened `qemu+tls://` connection reachability already uses; it needs no worker job
  (which would surface as an unavailable substitution under ADR-0139). Server vantage matches the
  data and avoids the dispatch surface, exactly as the issue specifies.
- **Verify image *content* (kernel/debuginfo/guest-agent), not just volume existence.** Content
  obligations are the operator's contract and are not introspectable from a volume lookup
  (ADR-0080); the heavy `guest_egress` opt-in is the boot-and-exercise path. This check is the
  cheap existence preflight, matching what provisioning itself verifies.
- **Look the volume up via the DB `image_catalog` row.** The server-vantage probe already holds
  the inventory and the live connection; reading the catalog would add a DB dependency the
  reachability sibling does not have and would diverge from what provisioning resolves (the
  inventory `base_image` → `[[image]]` chain). Rejected for parity with the reachability seam.
