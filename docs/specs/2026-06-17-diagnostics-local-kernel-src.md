# Spec — Diagnostics local build-host warm-tree source check (#533, #532)

- **Issue:** [#533](https://github.com/randomparity/kdive/issues/533)
- **ADR:** [`0163`](../adr/0163-diagnostics-local-kernel-src-check.md)
- **Covers:** #532 (the `KDIVE_KERNEL_SRC` blind spot). #531 (the ephemeral-libvirt
  guest-agent reachability probe) is split into a follow-up — it provisions a throwaway
  VM (the cost-bearing, mutating shape of `guest_egress`) and needs an async DB pool the
  synchronous factory does not have.
- **Date:** 2026-06-17

## Problem

`ops.diagnostics` reports the environment healthy while every build deterministically
fails. The check registry has no check that touches any build host: it validates the
remote-libvirt runtime provider (`remote_libvirt_reachability`,
`remote_libvirt_base_image_staging`) and the secret backend (`secret_ref`), but nothing
probes the warm-tree kernel source the seeded local build host needs. In the black-box
session that motivated #533, `doctor` passed every check while build → boot → debug was
gated by a `LOCAL` build host whose `KDIVE_KERNEL_SRC` was unusable (#532).

The seeded `worker-local` build host (`db/build_hosts.py` `WORKER_LOCAL_ID`) always exists
in a migrated database (`list_all_hosts` docstring: "always contains at least the seeded
`worker-local` row"). Its warm-tree lane is admitted only when `KDIVE_KERNEL_SRC` is usable
(ADR-0161, `check_warm_tree_source_admission`). When it is unset or invalid, every local
warm-tree build fails deterministically — the exact class of failure a doctor exists to
surface before a caller pays for it.

## Decision (per ADR-0163)

Add a **server-vantage** check `local_kernel_src` that resolves `KDIVE_KERNEL_SRC` and
reports three-state over the **single shared** warm-tree predicate
`warm_tree_source_error` (`providers/shared/build_host/workspace.py`), the same rule
`sync_tree` (build-time) and `check_warm_tree_source_admission` (admission-time, ADR-0161)
enforce:

| Resolved `KDIVE_KERNEL_SRC` | Status | failure_category | fix |
|---|---|---|---|
| existing absolute directory | `pass` | — | — |
| unset / empty / whitespace | `fail` | `configuration_error` | `LOCAL_KERNEL_SRC_FIX` |
| set but not an existing absolute tree | `fail` | `configuration_error` | `LOCAL_KERNEL_SRC_FIX` |

The check carries `provider=None` (it is provider-independent, like `secret_ref` — it is a
property of the build worker, not a runtime provider). It is assembled **unconditionally**
in the default factory: the seeded `worker-local` `LOCAL` host is a database invariant, so
the local warm-tree lane always exists, and a server-vantage config read needs no DB. No new
MCP tool, parameter, config setting, migration, DDL, or generated-doc change — `ops.diagnostics`
surfaces every assembled check generically.

## Components

### 1. `WarmTreeSourceOutcome` enum + `LocalKernelSrcCheck` (`checks.py`)

Mirror the reachability/base-image seam: a small outcome enum and a `Check` subclass holding
the three-state **policy**, consuming an injected `async () -> WarmTreeSourceOutcome` probe so
the check is unit-tested without touching the filesystem or config.

```
class WarmTreeSourceOutcome(StrEnum):
    USABLE = "usable"     # -> pass
    UNSET = "unset"       # -> fail (configuration_error)
    INVALID = "invalid"   # -> fail (configuration_error)
```

`LocalKernelSrcCheck(probe)` — `id = "local_kernel_src"` (a new `LOCAL_KERNEL_SRC_ID`
constant), `vantage = Vantage.SERVER`. `run()` maps:

- `USABLE` → `pass`, detail "warm-tree kernel source is set on the build worker
  (KDIVE_KERNEL_SRC points at an existing absolute tree)". This asserts source-path usability,
  not that the directory is a valid/buildable kernel tree (the ADR-0161 predicate does not
  inspect tree contents).
- `UNSET` → `fail`, `failure_category=configuration_error`, fix = `LOCAL_KERNEL_SRC_FIX`,
  detail "the local build worker has no warm-tree kernel source: KDIVE_KERNEL_SRC is unset,
  so every local warm-tree build fails".
- `INVALID` → `fail`, `failure_category=configuration_error`, fix = `LOCAL_KERNEL_SRC_FIX`,
  detail "KDIVE_KERNEL_SRC is set on the build worker but is not an absolute path to an
  existing kernel source tree, so every local warm-tree build fails".

The `fix` constant is **owned by `checks.py`** (diagnostic-output policy): `diagnostics →
providers` is the only legal import direction, so the remediation that names build-host
registration lives in diagnostics, mirroring `BASE_VOLUME_NOT_STAGED_FIX`. It describes the
same two lanes as `workspace.py`'s `_BUILD_LANE_GUIDANCE` (stage a tree + set
`KDIVE_KERNEL_SRC`, or register a git build host) but is an independent, test-asserted literal.
`checks.py` stays free of any provider/transport import — the predicate import lives only in
the probe adapter below.

### 2. The production probe adapter `diagnostics/kernel_src.py`

`warm_tree_source_probe(*, source=_kernel_src_from_config) -> WarmTreeSourceProbe`, the one
module that imports the provider-owned predicate:

```
from kdive.providers.shared.build_host.workspace import (
    KERNEL_SRC_UNSET_DETAIL,
    warm_tree_source_error,
)

def _kernel_src_from_config() -> str:
    return config.get(KERNEL_SRC) or ""

def warm_tree_source_probe(*, source=_kernel_src_from_config):
    async def probe() -> WarmTreeSourceOutcome:
        kernel_src = source()
        error = warm_tree_source_error(kernel_src)
        if error is None:
            return WarmTreeSourceOutcome.USABLE
        if error == KERNEL_SRC_UNSET_DETAIL:
            return WarmTreeSourceOutcome.UNSET
        return WarmTreeSourceOutcome.INVALID
    return probe
```

`KDIVE_KERNEL_SRC` resolution is deferred to probe time (mirroring `reachability.py`): the
`config.get` snapshot read happens when the check runs, so a value that drifts after assembly
is reflected in the verdict, not frozen at factory time. `config.get(KERNEL_SRC)` reads the
snapshot regardless of the setting's `processes=_WORKER` tag (`processes` only gates startup
`validate()`), so the server process can read it. The unset-vs-invalid split reuses
`warm_tree_source_error`'s own return values — the single predicate — rather than
re-deriving the rule.

The single `Path.is_dir()` stat the predicate performs is cheap and synchronous; unlike the
libvirt probes there is no blocking RPC to offload, so the probe does not use
`asyncio.to_thread`.

### 3. Wiring (`service.py`)

A new `_build_host_checks() -> list[Check]` returns
`[LocalKernelSrcCheck(probe=kernel_src.warm_tree_source_probe())]`. `default_service_factory`
calls `checks.extend(_build_host_checks())` unconditionally (after `_secret_ref_check`,
before the remote-libvirt gate). `kernel_src` is imported at module top the same way
`reachability` / `base_image_staging` are. The factory stays synchronous and pool-free.

## Test plan (TDD)

`tests/diagnostics/test_local_kernel_src.py` (mirrors `test_base_image_staging.py`):

- **check logic** (probe injected, no config/filesystem): each `WarmTreeSourceOutcome` →
  expected status / failure_category / fix-presence / `provider is None`; `id`/`vantage`
  assertions; the two `fail` fixes both equal `LOCAL_KERNEL_SRC_FIX`; `__post_init__`
  invariants hold (no fix on pass, fix present on fail).
- **probe adapter** (`source` injected): unset/empty/whitespace → `UNSET`; a `tmp_path`
  directory → `USABLE`; a relative path, a non-existent absolute path, and a file (not a
  dir) → `INVALID`. One test drives the default `_kernel_src_from_config` source via
  `monkeypatch.setenv("KDIVE_KERNEL_SRC", ...)` + `config.load()` to prove the config read
  and the deferred resolution.

`tests/diagnostics/test_default_factory.py`: the always-on check changes the assembled set —
update `test_factory_omits_remote_checks_when_not_configured` and
`test_multiple_instances_are_not_configured_so_no_reachability_check` (which assert
`ids == {SECRET_REF_ID}`) to `{SECRET_REF_ID, LOCAL_KERNEL_SRC_ID}`. Add: the factory always
includes `local_kernel_src`; a run with `KDIVE_KERNEL_SRC` unset yields `local_kernel_src`
`fail` (`has_failure` True) and a run with it pointed at a `tmp_path` tree yields `pass`.

## Non-goals

- The #531 ephemeral-libvirt guest-agent reachability probe (provisions a throwaway VM;
  needs `guest_egress`-style reaper/single-flight/TTL guards, an async DB pool in the factory,
  and an operator-staged build image). Split to a follow-up.
- A worker-vantage variant. The check reads the server process's `KDIVE_KERNEL_SRC`; this is
  correct when server and worker share an environment (the default single-host / compose
  deployment, the one #532 reproduced on). A split deployment whose worker has a distinct
  environment is the worker-vantage refinement, deferred with the existing worker-vantage
  dispatch backlog (#514, ADR-0139). See ADR-0163 "Considered & rejected".
- Gating the check on the `LOCAL` host's DB `enabled` flag (would require an async pool in the
  synchronous factory). Deferred with the #531 DB-plumbing work; see ADR-0163.
- Any MCP tool / parameter / config / migration / DDL change.
