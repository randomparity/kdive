# 0598 — Group remote domain ownership metadata

## Status

Proposed

## Context

Remote-libvirt domain XML currently puts `system` and `storage` beside each other as two
top-level elements in the KDIVE namespace. Libvirt retains one top-level element per namespace,
so inactive-definition readback keeps the System id but loses storage ownership. The file-backed
overlay then cannot pass the external-boot ownership gate.

## Decision

Remote-libvirt domain metadata uses one namespaced `<kdive:domain>` root containing
`<kdive:system>` and `<kdive:storage>` children. Every remote-libvirt reader follows that shape.
The reaper also accepts the former standalone `<kdive:system>` element so domains created by an
older worker remain discoverable; storage-sensitive operations fail closed when the grouped
storage child is absent.

The System id stays element text. Storage continues to record exact `pool` and `volume`
attributes, while the disk source supplies the exact overlay path. These three storage identities
must agree with the request.

## Consequences

- Real libvirt retains both ownership records in inactive XML.
- Existing domains remain reapable, but they do not gain storage authority they never recorded.
- Local-libvirt keeps its existing standalone System tag; this decision changes only the remote
  representation that needs more than one ownership field.

## Considered & rejected

- **Put pool and volume attributes on the System element.** judgment: this overloads the shared
  System tag and makes remote-only storage data part of its identity contract.
- **Use two namespace URIs.** judgment: this manufactures a second ownership namespace solely to
  bypass libvirt's namespace-level retention rule.
- **Infer pool and volume from the file path.** judgment: host paths do not prove libvirt pool and
  volume identity, and accepting that inference would weaken the existing ownership gate.
