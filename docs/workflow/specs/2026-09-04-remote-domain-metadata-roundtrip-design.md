# Remote domain metadata round-trip design

## Goal and scope

Issue #2238 corrects the remote-libvirt ownership representation that blocks #2121. Production
rendering and every direct remote reader move to the grouped contract in ADR-0598. Focused tests
and the existing native Ubuntu/libvirt carrier prove the result. Local-libvirt and native ppc64le
testing are excluded.

## Contract

Under `<domain><metadata>`, KDIVE emits exactly one top-level element in
`https://kdive.dev/libvirt/1`:

```xml
<kdive:domain>
  <kdive:system>system-uuid</kdive:system>
  <kdive:storage pool="pool-name" volume="volume-name" />
</kdive:domain>
```

The renderer builds this with ElementTree. Remote readers use shared helpers for the grouped
System and storage fields rather than repeating paths. System parsing accepts the old standalone
`<kdive:system>` only at the reaper metadata API boundary, where retaining discovery of existing
domains is necessary. Full-domain XML admission requires the grouped contract.

The external-boot ownership gate accepts a file-backed overlay only when the grouped System id,
pool, volume, and disk source path all match the requested System. A changed or missing value is a
conflict. Teardown pool lookup uses the grouped storage field and otherwise keeps its existing
configured-pool fallback.

## Failure handling

Malformed XML keeps each caller's current failure contract. Missing or divergent ownership never
becomes inferred authority: external boot rejects it, while teardown/reaping may use their
existing idempotent fallback paths.

## Verification

- Unit rendering asserts one namespaced root containing both children.
- Admission tests independently change pool, volume, and overlay path and observe `boot-disk`.
- Parser/reaper tests retain legacy standalone System discovery without granting storage identity.
- The native Ubuntu/libvirt carrier defines production XML, reads inactive XML back, proves both
  children, accepts the unchanged definition, and rejects each changed storage identity.

## Threat model

The trust boundary is libvirt XML read from the remote daemon. Defused XML parsing remains the
control against hostile structure. Parsed metadata grants ownership only after exact System,
pool, volume, and path comparison. No new actor, network endpoint, credential, command builder, or
permission is introduced. Deliberately unsupported legacy storage authority is out of scope and
fails closed.
