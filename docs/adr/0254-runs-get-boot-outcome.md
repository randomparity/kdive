# ADR-0254: surface boot_outcome on the runs.get clean-boot success path (#837)

- Status: Accepted
- Date: 2026-06-26

## Context

`runs.get` on a verified Run reports `steps.boot: "succeeded"` but says nothing about *what*
defined that success. The failure path is rich — `expected_boot_failure_detail`,
`available_capture`, `inert_capture`, `inert_capture_reason`, `boot_readiness` — while the
success path is opaque. An agent that wants to trust the verdict has to scrape the console for
the readiness marker itself, which is exactly the asymmetry the disclosure work (ADR-0239) set
out to remove on the failure side.

The success signal already exists and is persisted. The boot succeeds when the `kdive-ready`
console marker line is reached with no preceding crash signature
(`classify_console` / `_READINESS_MARKER` in `providers/local_libvirt/lifecycle/install.py`,
ADR-0055), and the boot job records `boot_outcome: "ready"` in its step result
(`jobs/handlers/runs_boot.py`). `step_progress` reads it into `StepProgress.boot_outcome`, and
`envelope_for_run` already consumes that value to route `suggested_next_actions` — but never
writes it to `data`. There is no `data["boot_outcome"]` writer anywhere in `src/kdive/mcp/`.

## Decision

On the `runs.get` success read path, when `step_progress.boot_outcome == "ready"`,
`envelope_for_run` emits a structured `data["boot_outcome"]` mirroring the failure side:

```
{outcome: "ready", signal: "console_marker", marker: "kdive-ready",
 unit: "kdive-ready.service", rule: "marker line reached with no pre-marker crash signature"}
```

The descriptor is built by a single shared helper, `ready_boot_outcome()`, defined in
`services/runs/steps.py` — the provider-neutral home of `StepProgress.boot_outcome` that
`envelope_for_run` already reads. The `marker`/`unit` fields derive from the image layer's
single-source-of-truth `READINESS_MARKER` (`images/families/_fedora_customize.py`, the constant
the rootfs readiness unit echoes to the console) and the `rule` describes the console-verdict
logic (`classify_console`, ADR-0055), so the surfaced wording cannot drift. This mirrors how the
failure-side `inert_capture_reason` reuses the shared `CONSOLE_CRASH_GUIDANCE` constant (ADR-0239).
The descriptor interpolates no guest output — only build-time constants — so surfacing it on the
success path is redaction-safe.

The descriptor describes the libvirt console-marker readiness signal specifically, so the writer
is gated on `run.target_kind is ResourceKind.LOCAL_LIBVIRT` as well as the `"ready"` outcome. The
provider-neutral boot handler (`jobs/handlers/runs_boot.py`) records `boot_outcome: "ready"` for
*both* libvirt providers, but remote-libvirt confirms readiness by a boot-id change on a
console-less target (ADR-0082), not by the `kdive-ready` console marker — so the console-marker
descriptor would be a false description of a remote boot. For a remote-libvirt Run the descriptor
is omitted rather than misreported; a boot-id descriptor for remote is out of scope for this issue
(which is local-libvirt-only).

The helper lives in the `services` layer, not the local-libvirt provider, because the provider
boundary guard (`tests/providers/test_provider_boundaries.py`, ADR-0076) forbids importing
`kdive.providers.local_libvirt.*` outside the composition root. `services/runs/steps.py` sourcing
the marker from the `kdive.images` layer is an allowed dependency (the same direction
`services/images/upload.py` already takes), and `envelope_for_run` imports the helper from the
module it already depends on.

No new data model, schema, migration, or RBAC change. The boot job's `"ready"` outcome value and
its persistence are unchanged; this only reads what is already there.

## Consequences

- An agent reading `runs.get` on a clean boot gets the same grade of structured detail the
  failure path already provides, and need not scrape the console to know what success meant.
- The success descriptor's wording has one source (`ready_boot_outcome()`); the `marker`/`unit`
  derive from the image layer's `READINESS_MARKER`, so a change to the readiness marker changes the
  surfaced value with it.
- No new cross-layer provider dependency: the helper sits in `services/runs/steps.py` and depends
  only on the already-allowed `kdive.images` layer, so the provider boundary guard stays green.
- Remote-libvirt Runs reach `boot_outcome == "ready"` too but get no `data["boot_outcome"]`, so
  the success-path detail is local-libvirt-only for now. This is the honest scope: the remote
  readiness signal is a boot-id change, not the console marker, and characterizing it is separate
  work.

## Considered & rejected

- **Define the helper in `providers/local_libvirt/lifecycle/install.py`, beside `classify_console`
  / `_READINESS_MARKER`, and import it into the mcp layer.** This is the most natural home for the
  wording, but the provider boundary guard (ADR-0076) forbids importing
  `kdive.providers.local_libvirt.*` outside the composition root — the import fails
  `tests/providers/test_provider_boundaries.py`. The `services` layer is the next-closest neutral
  home that keeps the marker single-sourced.
- **Re-declare the marker/unit/rule wording inside the mcp layer with literals.** Avoids any new
  dependency but reintroduces exactly the drift the issue is about: a change to the readiness
  marker would silently diverge from the surfaced descriptor. Sourcing the marker from the image
  layer's `READINESS_MARKER` is the point.
- **Surface only the bare string `"ready"`.** Reproduces the opacity — it names the outcome but
  not the signal, marker, or rule, so the agent still cannot tell what defined success without
  scraping the console.
- **Add a typed model / DB column for the descriptor.** The value is fully derivable from existing
  constants at read time; persisting it would be redundant state and a needless migration.
- **Put the helper in the image-build module (`_fedora_customize.py`) next to `READINESS_MARKER`.**
  That module is rootfs-customization machinery, not a run-read presentation home, and editing it
  risks colliding with concurrent rootfs-catalog work; `services/runs/steps.py` imports the marker
  from it instead.
