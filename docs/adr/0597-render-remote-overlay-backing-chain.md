# 0597 — Render the remote overlay backing chain

## Status

Accepted (2026-09-04)

## Context

ADR-0080 attaches a remote-libvirt overlay as a `volume` disk. On Ubuntu, libvirt translates the
top volume path after its first AppArmor profile generation. The translated overlay is admitted,
but the operator-staged base named only inside the volume metadata is absent from the generated
per-domain profile. QEMU therefore cannot open the qcow2 backing file.

The base volumes accepted by this provider are standalone qcow2 images. The one supported chain is
the per-System overlay followed by its base. Libvirt's domain schema can represent that chain even
while the top disk remains a pool volume, and `virt-aa-helper` walks an explicit domain backing
chain when producing the domain's exact file rules.

## Decision

`ensure_overlay` returns the base path obtained from libvirt together with the overlay name. The
remote domain renderer keeps the top disk as `type="volume"` and adds one explicit file-backed
`backingStore` node for that path, terminated by an empty `backingStore` node.

The path is never assembled from configuration or a volume name: it is the path returned by the
already-resolved base volume. A reused overlay reads its recorded backing path from volume XML and
must agree with the requested base path; missing, malformed, or divergent metadata fails before a
domain is defined. This preserves retry identity and prevents a changed request from granting a
path unrelated to the overlay.

## Consequences

- Ubuntu's ordinary libvirt AppArmor helper sees both layers and grants the base read access in the
  per-domain profile; the security driver remains enabled.
- The domain continues to record its storage pool for teardown and keeps ADR-0080's volume-backed
  lifecycle.
- Provider fakes must retain the volume path and backing metadata that production now consumes.
- A base image with its own backing file remains unsupported; the terminal node states that closed
  chain explicitly.

## Considered & rejected

- **Add the pool directory to the shared AppArmor abstraction.** judgment: every guest would gain
  access to every image in the directory, violating the issue's no-unrelated-path criterion.
- **Change the domain disk from a volume to a file.** judgment: this would discard ADR-0080's
  pool identity and widen teardown/reconciliation changes when the schema already represents the
  required chain.
- **Rely on libvirt to infer the base from storage-volume metadata.** verified: a clean Ubuntu host
  running libvirt 12.0.0 admitted the translated overlay but denied the staged base, while
  `/usr/lib/libvirt/virt-aa-helper -c -d` over an explicit domain backing node emitted one exact
  read rule for that base.
