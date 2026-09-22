# Probe the libvirt endpoint used by hosted TCG

Issue [#2648](https://github.com/randomparity/kdive/issues/2648), frozen scope
`q2648-de72c95f`. Full spec, M complexity, 250 changed-line design denominator from the
live issue and affected preflight/caller/record inspection. The operator approved the topology
rule and the additional native job on 2026-09-22.

## Problem and scope

The hosted `live_vm_tcg` job exports the published session URI and requires onboarding
preflight. The preflight nevertheless probes `qemu:///system`, its `default` network, and the
invoker's `libvirt` group. Run 35677000822 failed those three checks after its configured
session endpoint passed the earlier host preflight. The preflight script owns these checks;
`onboard.sh` and the hosted workflow retain their existing severity and call shape.

The scope includes the script, focused behavioral tests, the directly stale cross-platform
guide, Debt 0016, and an append-only amendment to ADR-0666. The remote-libvirt provider and
unrelated runner provisioning remain with their separate owners. No responsibility moves to
`onboard.sh`: it cannot know the next caller's provision needs, while the preflight already
owns endpoint checks. The obsolete hardcoded system probes leave the session path.

## Design

Use `LIBVIRT_URI`, already resolved from `KDIVE_LIBVIRT_URI` with the `qemu:///system`
default, in the `virsh list` connection probe. Classify only local `qemu:///system` and
`qemu+unix:///system` (with an optional query) as system-daemon URIs. On that topology,
retain the `libvirt` group check and probe its `default` network through the same URI.
Session and other configured endpoints skip those two system-only checks but still fail if
their `virsh list` connection fails. Keep the existing non-root system-mode advisory.

The connection failure names the configured setting and a topology-appropriate recovery
action without printing a possibly private URI. A failed network probe names the checked
URI class. A missing `virsh` remains diagnosed by the existing tool check rather than an
unhandled command failure.

Debt 0016 reopens because its hosted success criterion was not proved. ADR-0666 retains its
accepted caller-severity decision and gets an amendment to its consequences and resolution
notes: the required gate remains, but the system-only prerequisites did not describe the
hosted session topology. Neither record is closed again on a branch-only run. A normal
passing run on `main` is the remaining closure condition after merge.

## Success

- A healthy published session endpoint passes when the invoker has no `libvirt` group and
  the system `default` network is absent; it still checks the configured endpoint.
- An unreachable configured endpoint exits nonzero and identifies its failed preflight
  operation and recovery setting.
- The default system topology still requires the group, connection, and active `default`
  network; an explicit local system socket uses that same rule.
- The hosted `ONBOARD_PREFLIGHT=required` assignment remains intact. A controlled failure
  stops before migration and the later proof commands, with the preflight diagnosis visible.
- The normal hosted TCG job executes its proof selection on the candidate branch. The
  authorized workflow dispatch also runs the native job; report both outcomes and assess
  the hosted TCG job independently. The debt stays open until a normal `main` run passes.

## Failure model

- **Actors and deployments:** local-libvirt operators, the hosted TCG job, and the
  authorized self-hosted native VM job invoke the report-only script; focused tests
  run it with command stubs.
- **Invariants and assets:** a required preflight must reject an unusable configured
  connection, while a healthy session topology must not depend on another daemon.
- **Accepted failure classes:** other non-system libvirt transports have no local group or
  network assertion because the script cannot infer their daemon topology; connection
  reachability remains required. A later provider failure remains possible after preflight.
- **Covered elsewhere:** `preflight-env.sh host` checks KVM and configured reachability
  before hosted image staging; `onboard.sh` owns advisory/required severity (ADR-0666);
  the hosted workflow owns normal proof execution.

## Threat model

- **Boundary inventory:** no new source of URI data; the existing operator-provided
  `KDIVE_LIBVIRT_URI` now reaches two additional `virsh` argument positions.
- **Actor model:** the local operator and CI job control that environment variable.
  Untrusted tenants do not invoke this host preflight or set its environment.
- **Controls:** quote the URI as one `virsh -c` argument; do not evaluate or source it.
  Suppress command output in the connection probe and avoid echoing URI values in failures.
- **Outside scope:** validating the operator's entire URI syntax and remote transport
  authentication remain libvirt/client responsibilities; the remote provider is excluded.

## Validation

- `focused-test`: `tests/scripts/test_check_local_libvirt.py` must fail first for a
  healthy published session without the local group/network, an unreachable configured
  URI, and the default and explicit local-system cases; green command:
  `uv run python -m pytest tests/scripts/test_check_local_libvirt.py -q`.
- `focused-test`: `tests/scripts/test_onboard.py` must show a failed configured endpoint
  stops required onboarding before migrate while carrying the diagnosis; green command:
  `uv run python -m pytest tests/scripts/test_onboard.py -q`.
- `focused-test`: the existing workflow-shape regression keeps the hosted required gate;
  green command: `uv run python -m pytest tests/scripts/test_live_workflow_shape.py -q`.
- `task-test-not-applicable`: Debt 0016, ADR-0666, and cross-platform guide are prose
  status/explanation; no task-specific executable consumer establishes their truth.
  Review their claims against the hosted run and run `just docs-links` and `just records`.
- Run `just lint`, `just type`, focused tests, the pre-push `just ci` gate, and the normal
  live workflow on the candidate branch. Record both job verdicts and the hosted TCG
  proof count.
