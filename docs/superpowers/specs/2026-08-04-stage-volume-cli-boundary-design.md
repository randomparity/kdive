# Stage-volume CLI boundary design

## Problem

`kdive.images.rootfs.stage_volume` owns provider-neutral orchestration but also registers the
operator CLI and imports provider assembly inside its runner. That creates a reverse dependency
from the image operation into provider composition:

```text
images.rootfs.stage_volume -> providers.assembly -> providers.remote_libvirt.stage_volume
                           -> images.rootfs.stage_volume
```

The cycle is hidden by a function-local import, but the ownership direction is still wrong. The
neutral operation should describe required capabilities; a CLI composition layer should choose the
provider implementation.

## Decision

Move `add_stage_volume_parser` and `run_stage_volume` into the existing
`kdive.images.rootfs.command` module beside the `build-fs` CLI assembly. That module already imports
provider composition and owns parser registration, argument-to-domain conversion, local file
validation, and one-shot operator-command execution.

Keep `kdive.images.rootfs.stage_volume` limited to `_TargetRow`, `StageVolumeDeps`,
`capture_kernel_config`, and `stage_volume`. It retains no `argparse`, `ErrorCategory`, or provider
assembly dependency. `kdive.__main__` imports both rootfs command families from
`kdive.images.rootfs.command` and keeps its existing thin `_handle_stage_volume` dispatcher.

The move preserves:

- `--provider=remote-libvirt`, `--arch=x86_64`, required `--image`, and required `--from`;
- source-path resolution and `CONFIGURATION_ERROR` details;
- operation-local dependency construction after source validation;
- row lookup, config capture, volume upload, and config attachment ordering;
- fatal upload and advisory capture/attachment semantics;
- the command's non-runnable configuration-validation and telemetry timing.

No compatibility re-export remains in the neutral module. Internal callers and tests move to the
new owner.

## Alternatives

### Put the functions directly in `kdive.__main__`

This is directionally valid but would add parser details and provider composition to an entrypoint
that already dispatches many unrelated processes and commands. The existing rootfs command module
is the narrower owner.

### Add a dedicated stage-volume command module

A new module would separate the two rootfs commands, but no behavior or dependency boundary needs
that extra surface. The established `command.py` module already owns the equivalent `build-fs`
composition seam.

### Inject a provider-dependency factory into the neutral module

This would hide the reverse dependency behind another callable while leaving CLI concerns in the
operation module. It does not fix ownership and is rejected.

## Testing

Tests first import the parser and runner from their intended CLI owner and verify:

- the top-level parser retains all stage-volume defaults and required arguments;
- a missing source fails before provider dependency construction with the same categorized details;
- a valid source forwards the exact provider, image, architecture, resolved path, and assembled
  dependencies to `stage_volume`;
- the neutral module no longer owns the parser or runner and imports no provider package.

Existing orchestration and provider-wiring suites continue to cover operation ordering and remote
implementation behavior. The integrated change runs focused tests, lint, whole-tree type checking,
and the full CI recipe.

## Scope

This is an ownership-preserving move. It does not change the remote provider factory, object-store
or secret construction, catalog schema, command name, public arguments, exit codes, logging,
configuration validation, or runtime behavior.
