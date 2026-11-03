# Production remote materializer capacity (#3133)

## Scope and ownership

Scope6051507783 requires valid bounded plans to pass production capacity admission
and overcapacity plans to retain the existing refusal. [ADR-0747](../../adr/0747-derive-remote-materializer-capacity.md)
selects a derived ceiling; no public schema, setting or artifact validity limit changes.
The shared bounds owner computes the maximum of its existing reservation terms
using existing build-validation limits and module bounds. The remote materializer
adds its existing metadata allowance and exports the production ceiling. Host
assembly replaces only its fixed10GiB argument. No caller or authority migrates.

## Success

The actual production assembly admits a maximum schema-validated plan to staging.
The sum includes decoded kernel, optional initrd and debuginfo, module bytes and
entry overhead, both worst-case module archives and compressed bundle bytes.
A ceiling one byte below the computed reservation refuses before external effects.
Existing ownership, deadline, artifact validation and reservation accounting persist.
Remote DWARF3131 and nativePOWER2818 remain excluded; no unrelated authority work.

## Failure model

- Actors: authenticated closed plans and trusted production assembly; artifact
  staging and libvirt remain external I/O boundaries.
- Prevent: production/test ceiling skew, undercounted reservation terms and
  overcapacity materialization; malformed artifact limits remain rejected as before.
- Accept: finite disk exhaustion after admission, as this ceiling is not a disk
  allocation or aggregate-concurrency guarantee; existing I/O recovery owns that path.

## Validation

Use the existing host assembly test with real materializer and reservation logic,
stubbing only object-store/libvirt/staging I/O. Derive maximum declarations from
schema maxima and validate the complete plan before invoking the assembled object.
Observe the existing capacity error on current source, then staging entry after fix.
Test exact-fit and one-byte-too-small capacities, preserving early refusal without
large allocations. Run owning host/remote/shared-bound modules, lint/type/docs/records.
These execute the production capacity contract on x86_64 without a remote host;
they do not claim a real remote activation, DWARF staging or native POWER proof.
