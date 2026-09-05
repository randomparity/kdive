# Remote overlay AppArmor backing-chain design

Issue: #2236
Decision: [ADR-0597](../../adr/0597-render-remote-overlay-backing-chain.md)

## Outcome

A production remote-libvirt System backed by a pool overlay starts on Ubuntu with libvirt's
default AppArmor security driver enabled. The domain definition gives libvirt the complete
supported chain—the writable overlay volume and its standalone base file—so the generated
per-domain profile admits those files and no unrelated image path.

## Existing failure and invariant

`ensure_overlay` creates a qcow2 storage volume whose volume XML points to the selected base. The
domain renderer currently emits only `source pool=... volume=...`. On the affected Ubuntu host,
libvirt's generated profile includes the resolved top volume but omits the base, and QEMU fails at
`domain.create()` with permission denied. DAC access as the QEMU account succeeds.

Remote base volumes are standalone qcow2 images: operator images are staged as full copies and
supplied images are uploaded into standalone libvirt volumes. The supported chain is therefore
exactly overlay then base. This change must retain the volume source, storage pool identity,
security driver, and cleanup behavior.

## Components and flow

1. `ensure_named_overlay` resolves the requested base volume and obtains its libvirt path before
   creating an overlay. On reuse, it reads the existing overlay's volume XML and verifies that its
   immediate backing path equals the resolved requested base path.
2. `PreparedOverlay` carries `name`, `backing_path`, and `created`. A missing or malformed backing
   record, or a mismatch on reuse, is a configuration failure before domain definition.
3. Provisioning passes `backing_path` to `render_domain_xml`.
4. The disk remains `type="volume"` with the existing pool/volume source. The renderer adds
   `<backingStore type="file"><format type="qcow2"/><source file="..."/><backingStore/>
   </backingStore>`.
5. Libvirt's existing `virt-aa-helper` processes the definition and emits an exact read grant for
   that base. No host-wide AppArmor rule is installed.

The path is libvirt-produced, not constructed from a request or inventory text. XML construction
uses ElementTree attribute encoding. Provider errors expose the volume name and category but not
the host path.

## Failure behavior

- A missing requested base retains the current configuration error.
- An unexpected libvirt lookup, path, or XML-read failure is an infrastructure failure.
- A reused overlay with absent, malformed, or different backing metadata is a configuration error;
  it is neither rewritten nor deleted because another running or recoverable System may own it.
- A newly created overlay whose readback lacks the expected backing path is reclaimed by the
  existing failed-provision cleanup path and provisioning fails.
- Domain-start retry and port allocation behavior is unchanged.

## Verification

- Storage tests prove new and reused overlays return the exact libvirt base path, and prove reuse
  fails closed on absent or divergent backing metadata.
- XML tests prove a volume disk renders one file-backed base and an explicit terminal node, with
  XML metacharacters encoded rather than becoming structure.
- Provisioning tests capture the exact definition passed before `domain.create()` and prove it
  contains the overlay volume and matching base path.
- A native clean Ubuntu proof uses production `ensure_overlay` and `render_domain_xml`, starts the
  domain under an enforcing generated AppArmor profile, and verifies the generated `.files` entry
  names the base without disabling the security driver. Cleanup removes the test domain/overlay.
- `just lint`, `just type`, focused provider tests, `just test-ansible`, and `just ci` remain green.

Native ppc64le execution is excluded by the campaign. No Ansible policy change is expected: the
host's standard helper is the mechanism under test.

## Threat model

### Boundary inventory

- Existing widened boundary: libvirt-provided storage paths flow from remote volume metadata into
  domain XML and then into a per-domain AppArmor grant.
- No new external entry point, credential, network listener, or tenant-visible input is added.

### Actors and trust

The authenticated operator controls the remote-libvirt pool and staged catalog. Tenants may select
an admitted image but cannot supply its host path. The provider trusts libvirt's resolved path for
the volume it has already looked up, and does not trust a requested name to stand in for that path.

### Controls

- Exact volume lookup binds the grant to the selected base; reuse equality binds it to the existing
  overlay. Failure is closed before define/start.
- ElementTree encodes the path in XML.
- The terminal backing node bounds the represented chain to the provider's standalone-base
  contract.
- Error details and public proof output omit host paths.
- AppArmor access remains per-domain; no shared abstraction or security-driver default changes.

### Out of scope

Nested operator base-image chains are unsupported rather than recursively granted. Malicious
libvirt daemon output is outside the model because the provider already entrusts that daemon with
domain and storage control. Hot-plug and block-commit chains are unrelated to this provision-time
definition.
