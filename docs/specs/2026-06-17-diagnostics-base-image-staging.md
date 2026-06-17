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
| pool exists, volume absent | `fail` | `configuration_error` | ADR-0080 staging text (`checks.py` `BASE_VOLUME_NOT_STAGED_FIX`) |
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
- `NOT_STAGED` → `fail`, `failure_category=configuration_error`, `fix` = the
  `BASE_VOLUME_NOT_STAGED_FIX` constant owned by `checks.py` (the operator-staging remediation;
  see §4 for why it lives in `checks.py`, not `storage.py`).
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

### 2. The (pool, volume) resolver — one public seam in `config.py`

The probe needs **two** facts from `systems.toml`: the storage pool (a config knob) and the
base-image **staged volume name** (the instance's `base_image` → `[[image]]` `.volume`).
`remote_config_from_inventory()` returns the pool but **not** `base_image` or the `[[image]]`
entries, and `_resolve_instance`/`_load_remote_instances` are private. To avoid loading the
inventory twice and re-implementing the zero/multi-instance guard a second time (a drift hazard),
add **one public resolver** in `config.py`:

```
def resolve_base_image_staged_volume() -> str:
    """Return the staged base-image volume name for the single [[remote_libvirt]] instance.

    Raises CONFIGURATION_ERROR for zero/multi instances (reusing _resolve_instance, the same
    guard remote_config_from_inventory uses), a base_image whose [[image]] is absent, or an
    [[image]] whose source is not `staged` (build/S3 images have no operator-staged volume).
    """
```

It reuses `_resolve_instance()` (so the zero/multi-instance guard has exactly one home) and the
loaded `InventoryDoc.image` list. The probe calls `remote_config_from_inventory()` for the pool +
TLS material and `resolve_base_image_staged_volume()` for the volume. Both raise only
`CategorizedError(CONFIGURATION_ERROR)`; both loads hit the same on-disk `systems.toml` and are
cheap, and keeping them as two narrow public calls is clearer than threading a shared doc through
the seam. (If the double parse ever matters, a later refactor can add a combined resolver; it is
not worth a wider seam now.)

### 3. The production probe adapter `diagnostics/base_image_staging.py`

`base_image_staging_probe(*, config_factory=remote_config_from_inventory,
volume_factory=resolve_base_image_staged_volume, open_connection=open_libvirt,
secret_backend_factory=..., pki_base_dir=None) -> BaseImageStagingProbe`, structured exactly like
`reachability.remote_libvirt_reachability_probe`:

1. Resolve config (`config_factory`) for the storage pool + TLS refs + URI, and the staged volume
   name (`volume_factory`). A `CategorizedError(CONFIGURATION_ERROR)` from either →
   `INDETERMINATE` (the opener is never called). `TRANSPORT_FAILURE` is not raised by either.
2. Open the connection through the shared `remote_connection(config, backend,
   open_connection=..., pki_base_dir=...)`, offloaded via `asyncio.to_thread`.
   `CategorizedError(TRANSPORT_FAILURE)` from connect → `UNREACHABLE`; `CONFIGURATION_ERROR`
   (unresolvable cert refs) → `INDETERMINATE`.
3. Call `lookup_volume_staged(conn, pool, volume)` (§4) and map `STAGED`→`STAGED`,
   `ABSENT`→`NOT_STAGED`, `POOL_ABSENT`→`INDETERMINATE` (a missing pool is a different
   misconfiguration than a missing volume — no staging fix).

**Storage-RPC libvirtError classification.** `remote_connection` wraps a failed *open* into
`TRANSPORT_FAILURE`, but a `libvirtError` raised by the storage RPC *after* a successful open
escapes raw (reachability.py:105-112 hits the same shape with `getInfo()`). `lookup_volume_staged`
maps only the two clean not-found codes (`VIR_ERR_NO_STORAGE_POOL`/`VIR_ERR_NO_STORAGE_VOL`) and
**re-raises** every other `libvirtError`. The probe catches that re-raised error and maps it to
`INDETERMINATE` (logged), **not** `NOT_STAGED`: at this layer a transport drop mid-RPC and a
malformed pool name are indistinguishable, and host-down reachability is already covered by the
sibling `remote_libvirt_reachability` check, so emitting a stage-the-volume `fail` (or a confident
`transport_failure`) here would be a wrong-fix. An `INDETERMINATE` "could not probe" verdict is the
honest floor.

**Connection slice typing.** `remote_connection[C: ClosableConn]` is generic over the slice, but
`StorageConn` (storage.py:33) carries only `storagePoolLookupByName`, not `close()`. The probe
defines a `_StorageProbeConn(Protocol)` that is `StorageConn` + `close()` and types the opener to
it; the production `open_libvirt`-style opener returns it via the same narrowing cast
`open_libvirt_protocol` already uses at the host seam, so `remote_connection[_StorageProbeConn]`
type-checks and `ty` stays green.

Config/volume resolution is deferred to probe time (factory-time resolution would collapse the
whole report on inventory drift), matching `reachability.py`. A fresh per-probe
`SecretRegistry`-backed backend (short-lived, read-only) is the default, mirroring reachability.

### 4. Shared `lookup_volume_staged` helper (`providers/remote_libvirt/lifecycle/storage.py`)

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
existing `StorageConn`/`Pool`/`Volume` protocols.

**Where the `fix` text lives (dependency direction).** `diagnostics → providers` is the only legal
import direction (a provider importing diagnostics would be an upward dependency). So the
diagnostic `fix` constant `BASE_VOLUME_NOT_STAGED_FIX` is owned by **`checks.py`** (it is
diagnostic-output policy). `storage.py` keeps its own provision-time `CategorizedError` message
(optionally extracted into a `storage.py`-local constant for its own reuse, with no observable
change). The two remediation sentences describe the same operator action but are independent
literals, each test-asserted — a small, low-risk duplication kept in exchange for a clean
dependency direction. There is no neutral shared-constant module.

### 5. Wiring (`service.py`)

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
