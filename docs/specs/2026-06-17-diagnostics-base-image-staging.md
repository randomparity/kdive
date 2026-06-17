# Spec — Diagnostics remote-libvirt base-image-staging check (#513)

- **Issue:** [#513](https://github.com/randomparity/kdive/issues/513)
- **ADR:** [`0150`](../adr/0150-diagnostics-base-image-staging-check.md)
- **Companion:** #511 (volume-discoverability read) reuses the shared `lookup_volume_staged` helper.
- **Date:** 2026-06-17

## Problem

`ops.diagnostics(remote-libvirt)` reports healthy on `remote_libvirt_reachability`
(`src/kdive/diagnostics/checks.py:400-454`), whose pass detail itself disclaims usability
("libvirt-reachable only; config usability still surfaces at provision",
`checks.py:431-432`). The one operator prerequisite that actually blocks provisioning — the
base-image volume being staged on the host's storage pool — is unchecked. It surfaces only at
`systems.provision` time, deep in `ensure_named_overlay` (`storage.py:73-81`,
`CONFIGURATION_ERROR`), after a caller has already requested an allocation. Black-box defect #4.

## Decision (per ADR-0150)

Add a **server-vantage** check `remote_libvirt_base_image_staging` that looks up the configured
base-image volume on the host's storage pool and reports three-state:

| Outcome | Status | failure_category | fix |
|---|---|---|---|
| pool exists, volume staged | `pass` | — | — |
| pool exists, volume absent | `fail` | `configuration_error` | ADR-0080 staging text (from `storage.py`) |
| host unreachable | `error` | `transport_failure` | — |
| pool absent / inventory unresolvable / multi-instance / cert refs unresolvable / base image not `staged` | `error` | `configuration_error` | — |

The check is assembled in `_remote_libvirt_checks()` (`service.py:283-295`) directly after
`RemoteLibvirtReachabilityCheck`, only when `is_remote_libvirt_configured()` (the existing gate).
No new MCP tool, parameter, config setting, migration, DDL, or generated-doc change —
`ops.diagnostics` surfaces every assembled check generically (`diagnostics.py:177-202`).

## Components

### 1. `BaseImageStagingOutcome` enum + `BaseImageStagingCheck` (`checks.py`)

Mirror the reachability seam: a small outcome enum and a `Check` subclass holding the
three-state **policy**, consuming an injected `async () -> BaseImageStagingOutcome` probe so the
libvirt boundary is mocked in unit tests.

```
class BaseImageStagingOutcome(StrEnum):
    STAGED = "staged"            # -> pass
    NOT_STAGED = "not_staged"    # -> fail (configuration_error) + staging fix
    UNREACHABLE = "unreachable"  # -> error (transport_failure)
    INDETERMINATE = "indeterminate"  # -> error (configuration_error)
```

`BaseImageStagingCheck(provider, probe)` — `id = "remote_libvirt_base_image_staging"`
(a new `BASE_IMAGE_STAGING_ID` module constant), `vantage = Vantage.SERVER`. `run()` maps:

- `STAGED` → `pass`, detail "base image volume is staged on the remote host's storage pool".
- `NOT_STAGED` → `fail`, `failure_category=configuration_error`, `fix` = the verbatim
  `storage.py` operator-staging remediation (see §3 for the shared constant).
- `UNREACHABLE` → `error`, detail "remote-libvirt host unreachable; cannot verify base-image
  staging", `failure_category=transport_failure`.
- `INDETERMINATE` → `error`, detail "base-image staging could not be probed; check the
  [[remote_libvirt]] base_image/[[image]] staged volume, the storage pool, and the inventory",
  `failure_category=configuration_error`.

The `fail` detail must NOT interpolate the volume name into a no-leak-suppressed category; but
`configuration_error` is not suppressed (ADR-0123), and the volume name is operator-owned
inventory config (not tenant data), so naming it in `detail`/`fix` is safe and matches what
`storage.py` already surfaces at provision time. The existing `CheckResult.__post_init__`
invariants enforce fix-only-on-fail and no-category-on-pass.

### 2. The production probe adapter `diagnostics/base_image_staging.py`

`base_image_staging_probe(*, config_factory=remote_config_from_inventory,
inventory_factory=..., open_connection=open_libvirt, secret_backend_factory=...,
pki_base_dir=None) -> BaseImageStagingProbe`, structured exactly like
`reachability.remote_libvirt_reachability_probe`:

1. Resolve config (`remote_config_from_inventory`) for the storage pool + TLS refs + URI.
   A `CategorizedError(CONFIGURATION_ERROR)` → `INDETERMINATE`; `TRANSPORT_FAILURE` is not
   raised by config resolution.
2. Resolve the **base volume name**: load the inventory, take the single `[[remote_libvirt]]`
   instance's `base_image`, find the matching `[[image]]`, and require `source.kind == "staged"`
   to read `.volume`. Zero/many instances, a missing image cross-ref (the loader validates this,
   but probe-time drift is possible), or a non-staged source → `INDETERMINATE`. (Config
   resolution already rejects zero/multi-instance; this step reuses the loaded doc.)
3. Open the connection through the shared `remote_connection(config, backend,
   open_connection=..., pki_base_dir=...)` with a connection slice typed to the storage methods,
   offloaded via `asyncio.to_thread`. `CategorizedError(TRANSPORT_FAILURE)` from connect →
   `UNREACHABLE`; `CONFIGURATION_ERROR` (unresolvable cert refs) → `INDETERMINATE`.
4. Call `lookup_volume_staged(conn, pool, volume)` (§3) and map `STAGED`→`STAGED`,
   `ABSENT`→`NOT_STAGED`, `POOL_ABSENT`→`INDETERMINATE` (a missing pool is a different
   misconfiguration than a missing volume — no staging fix). A raw `libvirtError` escaping the
   helper (an infra fault, not a clean not-found) → `INDETERMINATE` (logged), never a confident
   `NOT_STAGED`.

Config resolution is deferred to probe time (factory-time resolution would collapse the whole
report on inventory drift), matching `reachability.py`. A fresh per-probe `SecretRegistry`-backed
backend (short-lived, read-only) is the default, mirroring the reachability default.

### 3. Shared `lookup_volume_staged` helper (`providers/remote_libvirt/lifecycle/storage.py`)

A new pure function over an already-open connection, the single "is volume X staged?" path #511
reuses:

```
class VolumeStaging(StrEnum):
    STAGED = "staged"
    ABSENT = "absent"
    POOL_ABSENT = "pool_absent"

def lookup_volume_staged(conn: StorageConn, pool_name: str, volume_name: str) -> VolumeStaging:
    ...
```

- `VIR_ERR_NO_STORAGE_POOL` on pool lookup → `POOL_ABSENT`.
- `VIR_ERR_NO_STORAGE_VOL` on volume lookup → `ABSENT`.
- volume found → `STAGED`.
- any other `libvirtError` → re-raised (the probe maps it to `INDETERMINATE`; #511 maps it to its
  own error envelope). The helper does not swallow infra faults into a clean state.

It does **not** open or close the connection; the caller owns the TLS lifecycle. It reuses the
existing `StorageConn`/`Pool`/`Volume` protocols. The verbatim ADR-0080 staging-remediation
string is lifted into a module constant `BASE_VOLUME_NOT_STAGED_FIX` so `ensure_named_overlay`'s
provision-time error and the diagnostic `fail` fix share one literal (no drift between doctor and
provision).

### 4. Wiring (`service.py`)

`_remote_libvirt_checks()` appends `BaseImageStagingCheck(provider=_REMOTE_PROVIDER,
probe=base_image_staging.base_image_staging_probe())` after the reachability check. The default
factory's `worker_available=False` is irrelevant (this is a server check). The function is
imported at module top the same way `reachability` is.

## Test plan (TDD)

`tests/diagnostics/test_base_image_staging.py` (mirrors `test_reachability.py`):

- **check logic** (probe injected, no libvirt): each outcome → expected status / failure_category
  / fix-presence / provider; `id`/`vantage` assertions; the `NOT_STAGED` fix equals the shared
  `BASE_VOLUME_NOT_STAGED_FIX` constant.
- **probe adapter** (libvirt boundary faked): a fake conn whose `storagePoolLookupByName` /
  `storageVolLookupByName` raise the two `VIR_ERR_*` codes (and a generic one), driving
  `STAGED`/`NOT_STAGED`/`UNREACHABLE`/`INDETERMINATE`; connect-error → `UNREACHABLE`; bad config →
  `INDETERMINATE` with the opener never called; non-staged base image → `INDETERMINATE`.

`tests/providers/.../test_storage.py` (or the existing storage test): `lookup_volume_staged`
returns each of the three states for the matching libvirt error codes and re-raises an unrelated
`libvirtError`.

`tests/diagnostics/test_default_factory.py`: extend the existing remote-configured assertions so
the assembled runnable set includes `BASE_IMAGE_STAGING_ID`, and a forced-`STAGED` /
forced-`NOT_STAGED` probe run yields the expected pass/fail in the aggregated report. The existing
`_IMAGE` fixture already declares a `staged` volume.

`tests/integration/test_doctor_exit_criterion.py` + `docs/operating/runbooks/doctor-exit-criterion.md`:
add the new check's row (fault = unstaged base volume → `fail` → doctor exit `1`) to the table so
the runbook stays in sync with the check set. (Whether the integration test seeds this fault
depends on how it injects faults; at minimum the runbook table is extended.)

## Non-goals

- Image **content** verification (kernel/debuginfo/guest-agent) — operator contract, not
  introspectable from a volume lookup (ADR-0080); the `guest_egress` opt-in is the boot path.
- A worker-vantage variant or any new dispatch surface.
- Any MCP tool / parameter / config / migration / DDL change.
