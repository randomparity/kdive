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

The renderer builds this with ElementTree. Full-domain readers use remote helpers for the grouped
System and storage fields rather than repeating paths. The reaper's namespace-specific metadata
API returns a namespace-stripped `<domain><system>...</system><storage ... /></domain>` fragment;
its existing shared System parser accepts that exact form and the old standalone System element.
Full-domain XML admission requires the grouped namespaced contract.

The external-boot ownership gate accepts a file-backed overlay only when the grouped System id,
pool, volume, and disk source path all match the requested System. A changed or missing value is a
conflict. Teardown and reaping are not authority-granting admission: when a convention-owned
legacy domain has no recorded pool, they retain the deterministic overlay-name/configured-pool
cleanup fallback.

## Failure handling

Malformed XML keeps each caller's current failure contract. Missing or divergent ownership never
becomes inferred boot authority. Cleanup fallback can address only the deterministic overlay name
in the configured pool.

## Verification

- Unit rendering asserts one namespaced root containing both children.
- Admission tests independently change pool, volume, and overlay path and observe `boot-disk`.
- Parser/reaper tests feed the exact namespace-stripped grouped fragment with a metadata id that
  differs from the domain name and prove the metadata id wins. A separate legacy standalone case
  remains discoverable without granting storage identity; missing-pool cleanup stays bounded.
- `tests/live_vm/test_remote_metadata_roundtrip.py` defines production XML through the configured
  remote URI, reads inactive XML back, proves both children, accepts the unchanged definition, and
  rejects each changed storage identity. Its `require_live_vm_remote` gate must resolve available,
  and cleanup must remove its domain and overlay.

## Threat model

The trust boundary is libvirt XML read from the remote daemon. Defused XML parsing remains the
control against hostile structure. Parsed metadata grants ownership only after exact System,
pool, volume, and path comparison. No new actor, network endpoint, credential, command builder, or
permission is introduced. Deliberately unsupported legacy storage authority is out of scope and
fails closed.
