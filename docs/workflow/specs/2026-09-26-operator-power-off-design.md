# Operator power off clean shutdown (#2801)

## Problem

Local-libvirt operator `power off` still destroys a running guest and can lose page-cache writes.
ADR-0679 provides the bounded clean-stop helper, but the Control plane does not use it.

## Scope

Amend [ADR-0028](../../adr/0028-control-plane-power-force-crash.md) through
[ADR-0685](../../adr/0685-operator-power-off-clean-shutdown.md). Only local-libvirt
`PowerAction.OFF` changes. The existing `Controller` port and other power actions remain stable.
The provider accepts an already-shut-off domain, then reads the looked-up domain XML type to
select KVM or TCG scaling and calls the existing helper. Invalid or unsupported XML for an active
domain fails as `CONTROL_FAILURE` before power mutation.
The helper owns the clean request, bounded wait, and destroy fallback. For OFF only, the control
job handler holds the existing System advisory key as a session lock from READY precheck through
provider IO, so a force-crash marker cannot land before the fallback. Short transactions commit
within that fence. If cancellation arrives while the provider thread runs, the handler waits for
the fenced operation, including lock release and connection-mode restoration, to complete before
propagating cancellation; provider failure also restores the connection mode. No new
shutdown policy or `Controller` interface is introduced here.

The `control.power` MCP wrapper states the local-libvirt OFF wait in seconds, its worker monotonic
reference clock, its per-job scope, hard-destroy consequence, and `jobs.wait` polling action. The
generated tool reference and CLI verb description follow the wrapper.

Excluded: other power actions (control plane), install and external-boot shutdown
(ADR-0679/0681 owners), and a configurable wait (future decision).

Global constraints: Python 3.14; x86_64 and ppc64le targets; reuse installed `defusedxml`; no new
dependencies or changes to the `Controller` port.

## Failure model

- **Actors and deployments:** authenticated operators request power off through the local-libvirt
  worker; libvirtd supplies domain state and XML.
- **Invariants and assets:** a cooperative guest's page-cache writes should survive; an
  uncooperative guest must stop after a bounded wait; an already-off domain stays a success;
  force-crash's kdump guest cannot be destroyed by an OFF fallback after its CRASHING marker.
- **Accepted failure classes:** an uncooperative guest pays the configured bound; one blocking
  libvirt shutdown call can overrun it, as ADR-0679 already accepts. Other control actions retain
  their existing interleaving behavior outside this OFF fence; this change does not promise
  job-wide serialization for them. Cancellation may wait for bounded provider completion before
  propagating. No live VM proof is available in the ordinary unit-test
  checkout; the live tier owns host integration evidence.
- **Covered elsewhere:** job retry behavior belongs to the worker; install/external-boot stop
  policy belongs to ADR-0679/0681.

### Threat model

- **Boundary inventory:** no new external request field. The provider newly parses domain XML
  returned by libvirtd; an authenticated operator can select the existing System but does not
  directly submit XML in this action.
- **Actor model:** an authenticated operator calls the existing control tool; the provider trusts
  libvirtd for the selected domain identity but treats its XML as untrusted parser input.
- **Control per boundary:** parse with the installed `defusedxml` parser, accept only `kvm` or
  `qemu` as the root domain type, map malformed/unknown XML to `CONTROL_FAILURE`, and do not log
  XML contents. The existing control handler keeps authorization; the OFF path's session fence
  shares the System key with the force-crash marker. The pool's per-lane floor reserves a separate
  heartbeat connection while the OFF dispatch connection is held.
- **Out of scope:** domain XML creation and mutation remain owned by provisioning; this action
  changes only OFF stop semantics and its force-crash ordering fence.

## Success

- `PowerAction.OFF` requests clean shutdown of a running local-libvirt guest and returns when
  libvirt reports `SHUTOFF`.
- KVM uses 60 seconds and TCG uses 60 seconds times its configured multiplier (600 seconds by
  default); timeout falls back to `destroy()`.
- Already shut-off guests succeed, and helper/libvirt failures retain `CONTROL_FAILURE`.
- An OFF job that starts before force-crash finishes before the CRASHING marker; an OFF job after
  that marker is refused before provider IO. Provider failure and cancellation release the fence
  only after the provider thread has finished.
- Other power actions keep their current behavior.

## Validation

- `focused-test`: `tests/providers/local_libvirt/test_control.py` covers clean shutdown, KVM and
  TCG bounds, timeout fallback, already-off, malformed XML, and error mapping.
- `focused-test`: `tests/providers/local_libvirt/lifecycle/test_power.py` protects the shared
  helper's existing state-race behavior.
- `focused-test`: `tests/adversarial/test_provider_state_races.py` races the OFF wait with a
  force-crash marker and verifies fence release and connection-mode restoration on exception and
  cancellation.
- `focused-test`: `tests/mcp/lifecycle/test_control_registrar.py` checks that the agent-facing
  wrapper and `action` Field expose the new wait/fallback; `just docs-check` and
  `just cli-verbs-check` check generated consumers.
- `task-test-not-applicable`: no live VM proof in this worktree because the native live tier
  requires a provisioned operator VM; unit tests exercise the provider with fake libvirt domains.
