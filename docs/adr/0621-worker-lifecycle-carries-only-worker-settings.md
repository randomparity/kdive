# 0621 — Worker lifecycle carries only worker settings

## Status

Accepted (2026-09-06)

## Context

The native authority proof reached a running authority service, then worker startup failed before
dispatch. The lifecycle request required a private recovery root and artifact-capacity input,
but its launcher did not supply either. The only production consumers of those settings construct
the authority-host provider. Its recovery-root parser requires ownership by that process; sharing
that private root with workers would violate the installed authority boundary.

The lifecycle contract already contains a separate five-field worker authority route. The launcher
also omitted those fields, so a worker started without the configured sender binding.

## Decision

Remove authority-host recovery-root and artifact-capacity settings from the internal worker
lifecycle request, environment writer, and execution gate. Keep their authority-host configuration,
provisioning, bounds, and ownership checks unchanged. A worker receives only the existing typed
authority route: instance, fixed request socket, and three credential references. The launcher
forwards that complete optional route; incomplete routes remain invalid.

The lifecycle schema identity changes with its actual request schema. A mismatched installed
witness still requires reprovisioning before startup; no compatibility check is bypassed.

## Consequences

Ordinary worker startup does not require authority-private storage. Configured authority jobs use
the fixed sender and active worker-incarnation credential. A structural launcher test compares its
emitted settings with the actual lifecycle model, including every authority-route field.

## Considered & rejected

- judgment: Supply an authority-owned directory to every worker: its ownership validator rejects
  those processes, and granting access would violate the provider boundary.
- judgment: Add placeholder per-worker recovery stores: the worker does not implement the
  authority-owned mutations and does not need those stores.
- judgment: Omit the route from the launcher: configured jobs would still have no authority sender.
