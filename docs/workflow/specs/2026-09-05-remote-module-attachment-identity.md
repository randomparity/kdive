# Remote module attachment identity

Issue: #2167
Decision: [ADR-0603](../../adr/0603-remote-device-identity-port.md)

## Outcome

Before remote module preparation mutates storage, inspect active and inactive domain definitions
and prove that exactly one stopped System owns the root volume and no unrelated domain refers to
the root, source, or scratch storage through any supported libvirt storage spelling.

## Design

The existing bounded XML walk remains the source of disk paths and pool/volume references. A new
provider-local `RemoteDeviceIdentityPort` maps each validated absolute remote-host path to an opaque
device/inode pair. The inspector compares identities rather than path strings, so symlinks, hard
links, and bind mounts converge while unrelated unmanaged disks remain valid.

The port receives at most 4,096 encoded path bytes and returns only two bounded non-negative
integers. Missing or malformed identity is a closed conflict. Operational libvirt or identity-port
failures are `INFRASTRUCTURE_FAILURE`; ownership and malformed-input findings remain `CONFLICT`.
No path is included in a returned identity or error detail.

The server-preparation implementation supplies the remote-host adapter. This issue defines and
tests the provider contract and inspection policy; it does not add a generic provider capability,
persistence, migration, or agent-facing API.

## Verification

- Unit regressions prove unrelated unmanaged identities pass and unavailable identities fail
  closed.
- Controlled faults prove the new tests fail when identity comparison is bypassed.
- Real-host filesystem tests prove symlink, hard-link, and bind-mount aliases share identity.
- Existing active/inactive, nested backing/data/mirror, pool alias, and lexical alias cases remain
  green.
- Focused tests, lint, type checking, and the repository CI recipe pass before delivery.
