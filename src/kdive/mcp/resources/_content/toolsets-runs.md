# runs toolset

A Run records a kernel build and its install/boot steps within an Investigation. It can
start without a System, so you can build and upload before consuming a provisioned target.
Read each tool's schema for exact parameters, roles, and returned fields.

## Create or reuse a build

1. `runs.create` creates a Run bound to a System, or unbound with a `target_kind` and build
   profile. For a new build, build externally, upload through `artifacts.create_run_upload`,
   then call `runs.complete_build`. Follow resource://kdive/docs/operating/external-build-upload.md.
2. Alternatively, pass a compatible `build_ref` from the same Investigation to `runs.create`
   to reuse validated build artifacts. Reuse does not extend their retention deadline; follow
   the returned expiry/recovery guidance if the build has expired.
3. If the Run is unbound, use `runs.bind` to attach a READY System before installation.

## Install and boot

`runs.install` installs the built kernel and modules; `runs.boot` boots the installed Run.
Poll their returned jobs with `jobs.wait`. A Run's top-level `succeeded` means its **build**
completed, not that the guest booted. Inspect `runs.get` for `data.steps`, `data.boot_readiness`,
and console references. A failed boot can reset its step to `pending`; inspect the failed
job and readiness result instead of waiting forever for a successful step.

Change boot arguments through `runs.install` (`cmdline`/`crashkernel`), then boot again;
`runs.boot` does not take a command line. Inspect `data.replayed` to distinguish an existing
boot job from a fresh attempt. Follow the schema's restage/force rules before requesting one.

## Inspect and finish

- `runs.get` reads one Run's build provenance, step results, and console references.
- `runs.list` lists visible Runs with filters and cursor pagination.
- `runs.set` records or clears an editable `outcome_note`, including on terminal Runs.
- `runs.cancel` cancels a non-terminal Run; it does not tear down the System. Restricting
  external-boot activations must be resolved through the returned recovery action first.
- `runs.release_external_boot` enqueues release of an active external boot owned by this Run.
  It refuses while a job or debug session holds the System. Poll the returned job; repeated
  identical requests recover that job. Recovery-conflict/failed states need the recovery
  guidance in `runs.get` and resource://kdive/docs/guide/toolsets/systems.md.

System teardown and allocation release are separate steps; see
resource://kdive/docs/guide/toolsets/systems.md.
