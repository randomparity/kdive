# ADR 0194 — Name the staged remote-libvirt host in diagnostics and describe

- **Status:** Accepted
- **Date:** 2026-06-20
- **Deciders:** KDIVE maintainers

## Context

A black-box usability run (RUN_REVIEW.md D1) against a four-host remote-libvirt fleet — where the
operator-staged base image was present on only one of the four hosts — surfaced two diagnostics
defects that together make it impossible for an operator to learn *which* host is actually usable.

**(a) `ops.diagnostics` cannot name the staged host.** The fleet doctor fans the base-image-staging
check out over each declared `[[remote_libvirt]]` instance (ADR-0187): `diagnostics/contribution.py`
`_checks()` constructs one `BaseImageStagingCheck` per instance `name`. But the `CheckResult` that a
check returns carries only `provider: "remote-libvirt"` — never the host it probed
(`diagnostics/checks.py` `CheckResult`). With four hosts the verdict has four staging rows, all
labeled `remote-libvirt`, one `pass` and three `fail`/`error`, with nothing to tell them apart. The
worker-vantage `gdbstub_acl` check already names its host in the *detail string* (`config.gdb_addr`),
but that is prose, not a field a caller can key on, and the staging check's detail names no host at
all.

**(b) `resources.describe` reports `staged: "unknown"` for a host that is staged.**
`describe_resource` resolves the staged-volume probe through
`_runtime_staged_probe(resolver, resource.kind)`, which calls `resolver.resolve(kind)` — the
**unbound** provider runtime. For remote-libvirt the unbound runtime's `config_factory` is
`unbound_remote_config`, which *always* raises `CONFIGURATION_ERROR` ("port used without a bound
host"). `probe_staged_volumes` catches that and degrades every requested volume to `"unknown"`
(`staged_volumes.py`). So the field reads `"unknown"` even when the volume is demonstrably staged —
the probe never connected, because it was never bound to the host being described. The per-op
chokepoint that resolves the *allocated* host's config is `ProviderRuntime.for_resource(name)`
(ADR-0187), and `describe_resource` was the one staged-volume caller not going through it.

A secondary symptom of (b) is semantic: `probe_staged_volumes` returns `"unknown"` for two distinct
conditions — config could not be resolved (genuinely unprobeable) and a non-transport
`CategorizedError` after a successful open. Once the probe is host-bound, a reachable host should
surface a real per-volume verdict (`staged`/`absent`/`pool_absent`) and `"unknown"` should mean only
"the probe could not run".

## Decision

### (a) Carry the probed host on `CheckResult` and project it through `ops.diagnostics`

- Add an optional `resource_id: str | None = None` field to `CheckResult`
  (`diagnostics/checks.py`). It names the registered resource the result pertains to — for
  remote-libvirt the `[[remote_libvirt]]` instance `name`, which is the resource row's `name` under
  the `(kind, name)` identity (ADR-0112/0187). It is `None` for a fleet-aggregate or
  resource-independent check (`secret_ref`, `local_kernel_src`, the aggregated
  `ephemeral_libvirt_buildhost_agent`), exactly as `provider` is `None` for a provider-independent
  check.
- `provider` answers *which provider family*; `resource_id` answers *which host within it*. The two
  are orthogonal; neither replaces the other.
- Thread the per-host name into the fanned-out server-vantage checks at their construction site
  (`diagnostics/contribution.py` `_checks()` already iterates `remote_instance_names()`):
  `BaseImageStagingCheck` and `RemoteLibvirtReachabilityCheck` each take a `resource_id=name` and
  stamp it onto every `CheckResult` they emit (pass, fail, and error alike — an operator most needs
  the host name on a failing/erroring host).
- `ops.diagnostics` projects the new field in its per-check item
  (`mcp/tools/ops/diagnostics.py` `_item`): `data["resource_id"] = result.resource_id`. Additive
  `data` key, no schema/migration change (ADR-0113 flat outputSchema).
- The worker-vantage result codec (`diagnostics/result_codec.py`) round-trips the field for
  completeness so a serialized result survives the inline-JSON hop unchanged; the worker-vantage
  checks (`provider_tls`, `gdbstub_acl`) do not set it in this change, so it serializes as `null`.

### (b) Bind the `resources.describe` staged probe to the described host

- `_runtime_staged_probe` resolves the probe through
  `resolver.resolve(kind).for_resource(resource.name)` instead of `resolver.resolve(kind)`
  (`mcp/tools/catalog/resources.py`). For remote-libvirt this rebinds the runtime's
  `config_factory` to `remote_config_for_resource(resource.name)` (ADR-0187), so the probe opens a
  `qemu+tls://` connection to the host being described and returns its real per-volume verdict. For
  single-host providers `for_resource` is identity, so their behavior is unchanged. When
  `resource.name` is `None` the probe is skipped (degrades to `"unknown"`, the existing
  unresolvable contract), since an unnamed remote resource cannot be host-resolved.
- The probe's status vocabulary is kept but its meaning is tightened in the docstring and a test:
  `staged`/`absent`/`pool_absent` are real per-volume verdicts for a reachable host, `unreachable`
  is a transport failure or timeout, and `"unknown"` is reserved for "the probe could not run"
  (config unresolvable). No change to `probe_staged_volumes`' control flow is required for the
  primary fix — binding the host is what turns the all-`"unknown"` result into real verdicts — but
  the contract is documented so a future caller does not re-collapse the distinction.

## Consequences

- An operator running `ops.diagnostics` over an N-host fleet now sees `data.resource_id` on each
  per-host staging/reachability row and can name the one host whose base image is staged. A single
  declared host is unchanged except that its rows now also carry the (single) `resource_id`.
- `resources.describe` on a remote-libvirt resource now reports a real `staged` verdict for a
  reachable host, instead of `"unknown"` for every host. `"unknown"` now means what its name says.
- `CheckResult` gains one optional field; every existing producer that does not set it is unchanged
  (default `None`), and the codec round-trips it. No state machine, schema, or migration touched.
- The fix for (b) is a one-line binding change at the single mis-wired call site; the probe and the
  runtime rebind hook already existed (ADR-0187). The risk is limited to remote-libvirt describe.

## Considered & rejected

- **A separate `host` string field distinct from `resource_id`.** The resource row's `name` *is* the
  host identity under the `(kind, name)` reconcile (ADR-0112); a second host field would duplicate it
  and drift. `resource_id` is the caller-meaningful key (it matches `resources.describe`'s argument).
- **Putting the host only in the `detail` prose** (as `gdbstub_acl` does today). Prose is not
  machine-keyable; an agent triaging "which host is staged" needs a field, not a substring. The
  detail strings already name the host where natural; the field is additive.
- **Resolving the host inside `BaseImageStagingCheck.run()` from the probe.** The probe is an opaque
  `Callable[[], Awaitable[...]]`; the construction site (`contribution.py`) is where the name is
  known. Stamping it at construction keeps the check ignorant of inventory resolution, matching how
  `provider` is injected.
- **Changing `probe_staged_volumes` to return a real verdict for the unbound case.** It cannot — an
  unbound probe has no host to connect to. The bug is the *caller* not binding the host, so the fix
  belongs at the call site, not in the probe.
- **Reporting `staged` as a boolean instead of a string.** The probe already distinguishes
  `staged`/`absent`/`pool_absent`/`unreachable`/`unknown`; collapsing to a boolean would lose the
  `pool_absent`-vs-`absent` and reachable-vs-unprobeable distinctions the run review asked to
  preserve. The acceptance criterion ("a real true/false for reachable hosts") is met by the
  reachable host returning a concrete non-`unknown` status.
