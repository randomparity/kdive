# runs toolset

A run is one build → install → boot lifecycle of a kernel on a defined system. Reach for
these after you have an allocated, defined system (see the systems guide) and before
debugging (see the debug guide). For exact parameters, types, and return schema, read each
tool's own description.

## Starting a run

- `runs.create` — open a run bound to an investigation and a build profile. The external
  upload lane (build the kernel yourself and upload it) is the default build path.
- `runs.bind` — bind an existing run to a system, when create did not.
- `runs.validate_profile` — check a build profile for problems without creating a run.
- `runs.profile_examples` — fetch ready-made build-profile templates to start from.

## Building

- `runs.build` — enqueue a warm-tree server build for a run (the single-host server-build
  lane, an alternative to uploading a prebuilt kernel).
- `runs.complete_build` — finalize an externally uploaded build once its artifacts are in.
- `runs.build_install_boot` — run the single-host server-build lane as one pollable job;
  prefer it over calling build, install, and boot separately when you build on a host.

## Install and boot

- `runs.install` — install the built kernel and modules onto the bound system.
- `runs.boot` — boot the system into the built kernel.

## Inspecting and stopping

- `runs.get` — read a run's status, build provenance, and console access.
- `runs.list` — list runs with filters and pagination.
- `runs.cancel` — cancel an in-flight run.
