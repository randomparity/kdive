# runs toolset

A run is one build → install → boot lifecycle of a kernel on a defined system. Reach for
these after you have an allocated, defined system (see the systems guide) and before
debugging (see the debug guide). For exact parameters, types, and return schema, read each
tool's own description.

## Starting a run

- `runs.create` — open a run bound to an investigation and a build profile. Build the kernel
  yourself and upload it (the external upload lane).
- `runs.bind` — bind an existing run to a system, when create did not.

## Building

- `runs.complete_build` — finalize an externally uploaded build once its artifacts are in.

## Install and boot

- `runs.install` — install the built kernel and modules onto the bound system.
- `runs.boot` — boot the system into the built kernel.
- `runs.release_external_boot` — release the external-boot activation restricting the run's
  system. Not served yet: it validates your role and the activation's state, then reports
  `recovery_executor_unavailable` and changes nothing (#2118 promotes it).

## Inspecting and stopping

- `runs.get` — read a run's status, build provenance, and console access.
- `runs.list` — list runs with filters and pagination.
- `runs.cancel` — cancel an in-flight run.
- `runs.set` — record a post-hoc `outcome_note` (a free-form verdict) on a run, editable at any
  time; readable back as `data.outcome_note`.
