# Remote module preparation package

## Problem

Remote rootfs currently owns appliance supervision, preparation execution,
documents and result extraction alongside boot artifact transfer. The concrete
preparation authority host is separated from the workflow it assembles.

## Scope

Move all nine remaining `remote_module_*.py` rootfs modules and the provider-root
`remote_module_authority_host.py` into `remote_libvirt/module_preparation/`.
Keep filenames and symbols unchanged; add only an inert package initializer.
Keep boot artifact naming/transfer and generic volume upload under rootfs.

Migrate direct, dynamic and string imports in source, tests and the provider
Ansible installation task. Tests retain their existing paths and helper ownership.
Historical specifications and decision records retain their point-in-time paths.
Remove old implementation files; add no forwarding modules. Public tool/provider
contracts, packaged appliance assets and deployed data paths remain unchanged.
No policy, concurrency, schema or dependency changes are needed.

### Failure model

- Deployment: existing remote libvirt on Linux x86_64 and ppc64le.
- Obsolete imports or patch strings can break assembly and tests; migrate all
  tracked runtime references, including the Ansible appliance validation import.
- Preserve implementation bodies, volume names, appliance loading, deadlines,
  completion ownership and error handling.
- Accepted: this package move does not redesign the preparation workflow.

## Success

1. Preparation implementation lives together in its dedicated package.
2. Production assembly and provisioning import the same implementation directly.
3. Rootfs contains only rootfs/boot artifact helpers; no compatibility shims.

## Validation

- `Mode: task-test-not-applicable`: pure module relocation changes no executable
  behavior or new dependency boundary; do not invent a prose/ownership snapshot.
  Compare normalized ASTs for every moved module and inspect all import updates.
- `Mode: focused-test`: existing remote-provider and authority service/composition
  tests verify assembly and behavior. Run `just test-verbose` for those paths and
  local external-artifact validation, plus applicable Ansible role tests.
- Run `just lint`, whole-tree `just type`, documentation checks and configured
  hooks. Verify zero obsolete runtime references before committing locally.
