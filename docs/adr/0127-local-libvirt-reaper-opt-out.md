# ADR 0127 — Local-libvirt reconciler reaper opt-out

- **Status:** Proposed
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

The reconciler composes a provider-aware leaked-domain reaper. Since ADR-0111 the
local-libvirt-backed reaper has been composed **unconditionally**
(`src/kdive/providers/assembly/composition.py`, `_reconciler_reaper_factories`), on the stated
assumption that "local-libvirt is always-on (`KDIVE_LIBVIRT_URI` defaults to `qemu:///system`
and the local runtime is always registered)". That assumption is false for a remote-libvirt-only
deployment: a kdive pod in Kubernetes runs no `libvirtd`, so `repair_leaked_domains` opens a
connection to `/var/run/libvirt/libvirt-sock`, that socket does not exist, and the sweep raises
`libvirtError: Failed to connect socket` **every reconcile pass** (~every 30s). The failure is
caught per-pass (`reconcile_once` never raises), so it is not fatal, but it floods the reconciler
log with tracebacks and can mask a genuine repair failure. The sibling remote-provider reapers
(dump-volume, build-VM, transport-resetter) are already gated on `_remote_libvirt_enabled`; only
the local reaper has no gate.

## Decision

Introduce a `KDIVE_LOCAL_LIBVIRT_ENABLED` setting (default `true`, preserving today's behavior on
every existing deployment) and a `_local_libvirt_enabled` helper that mirrors the existing
enable-helper convention (an explicit flag wins, else the env, default on). Gate the local
reaper factory on it: when disabled, the local reaper is not composed. With neither the local
reaper nor the fault-inject reaper composed, `build_reconciler_reaper` returns a `NullReaper`
rather than assuming at least one reaper is present.

The kdive Helm chart — whose pods never expose a local libvirt socket — sets
`KDIVE_LOCAL_LIBVIRT_ENABLED: "false"` in its default `config`, so a k8s deployment stops the
flood out of the box. Compose/bare-metal deployments that do run `libvirtd` leave the default
on.

## Consequences

- A remote-libvirt-only deployment no longer logs a per-pass `leaked_domains` traceback; the
  reconciler log shows only real failures.
- Default behavior is unchanged: with the flag absent or `true`, the local reaper is composed
  exactly as before, so a stock workstation/compose deployment still reaps name-orphaned
  `kdive-<uuid>` domains (the ADR-0111 behavior).
- `build_reconciler_reaper` now has a third outcome (`NullReaper`) for the both-disabled case; the
  reconciler's other repair specs are unaffected.
- The setting is added to the generated config reference and is consumed by the migrate, server,
  worker, and reconciler processes (only the reconciler acts on it; the others snapshot the same
  config map).

## Alternatives considered

- **Make the local reaper tolerate a missing socket (return `[]` on connect failure)**: would
  silence the flood, but a deployment that *does* run local libvirt and suffers a transient
  outage would then silently skip reaping real leaked domains. Distinguishing "no local libvirt
  here" from "local libvirt transiently down" at the reaper layer is exactly what an explicit
  enable flag expresses cleanly. Rejected as masking a real failure mode.
- **Gate on libvirt-socket reachability at composition time**: probing the socket during
  assembly couples composition to host state and races a `libvirtd` that starts after the
  reconciler. Rejected for a declarative operator flag.
- **Leave it unconditional and filter the log**: the tracebacks are a symptom; a deployment with
  no local provider should not compose its reaper at all. Rejected as treating the symptom.
