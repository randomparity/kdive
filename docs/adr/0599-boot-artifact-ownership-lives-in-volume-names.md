# 0599 — Boot-artifact ownership lives in volume names

## Status

Accepted (2026-09-05)

## Context

Remote external boot materializes kernel and initrd bytes as libvirt dir-pool volumes. The
producer submits System, Run, artifact kind, content digest, and staging-attempt identity in a
storage-volume `<metadata>` element. The reaper reads that element before deciding ownership.

Libvirt storage volumes do not persist arbitrary metadata. ADR-0588 records the reproduced
libvirt 12.0.0 behavior and establishes the volume name as the durable ownership channel for the
sibling remote-module path. The boot-artifact reaper therefore recognizes nothing on a real host,
even though the artifact bytes and deterministic legacy name survive.

Cleanup runs after the creating worker may have gone away and deletes remote storage. It must
recover a complete owner and content identity from the object itself, without accepting a legacy,
malformed, or merely prefix-matching name as proof.

## Decision

Final and partial boot-artifact volumes use one versioned, anchored name grammar:

```text
kdive-boot-v1-<kind>-<system-uuid>-<run-uuid>-<sha256-hex>-final
kdive-boot-v1-<kind>-<system-uuid>-<run-uuid>-<sha256-hex>-partial-<attempt-uuid>
```

`kind` is exactly `kernel` or `initrd`. UUIDs are canonical lowercase hyphenated strings.
`sha256-hex` is exactly 64 lowercase hexadecimal characters. The longest name is 204 ASCII bytes,
within the 255-byte dir-pool limit measured by ADR-0588. No field is truncated or encoded through
a lossy abbreviation.

One shared parser performs a full-string match and returns the kind, System, Run, digest, final or
partial state, and optional attempt. Producer lookup, retry cleanup, inventory, and reaping use the
same renderer/parser contract. Legacy names have no version marker or digest and are foreign: they
remain untouched rather than acquiring reconstructed ownership.

The digest in the name is an integrity claim, not sufficient deletion proof. Inventory and reaping
stream the complete volume and require its SHA-256 to equal the name before returning or deleting
it. The reaper also retains every parsed owner present in its durable live-owner set. Malformed,
foreign, content-mismatched, unreadable, and live-owner volumes receive no mutation.

Volume creation XML carries no ownership metadata. The name and bytes are the complete remote
object proof. The shared dir-pool test double implements the storage operations exercised by this
path while continuing to render readback from retained libvirt fields and discard metadata and
unknown elements.

## Consequences

- A final volume is deterministic for one exact `(kind, System, Run, digest)` identity. A retry
  with changed bytes uses a different name instead of treating the old bytes as that identity.
- Old boot-artifact volumes are intentionally stranded for operator inspection; deleting them
  automatically would require ownership evidence they do not carry.
- The fixed prefix and field order are a persisted compatibility surface. A future format needs a
  new version and must retain this parser while version-1 objects may exist.
- Names expose UUID ownership and content digests to operators who can list the configured pool,
  matching the visibility accepted by ADR-0588.

## Considered & rejected

- **Keep or duplicate storage-volume metadata.** verified: ADR-0588 records that libvirt 12.0.0
  silently discards the element and exposes no volume metadata API. Retaining it would preserve a
  second, inert source of truth and keep test doubles capable of masking the defect.
- **Keep the legacy final name and put only a short digest tag in metadata or content.** verified:
  the metadata does not survive, while content is written after volume creation. Either channel
  leaves a crash window in which a present object has no durable integrity identity.
- **Put only a truncated digest in the name.** judgment: the full digest fits with 51 bytes of
  headroom, so accepting collisions buys no needed capacity.
- **Delete legacy names after rehashing their bytes.** judgment: bytes can prove content but cannot
  recover the absent digest expectation or distinguish KDIVE ownership from a foreign object that
  resembles the old shape. Fail-closed retention avoids operator data loss.
- **Use a durable database row as the only inventory.** judgment: it cannot identify an object
  after the row or creating worker is lost, which is when orphan reaping needs the embedded owner.
