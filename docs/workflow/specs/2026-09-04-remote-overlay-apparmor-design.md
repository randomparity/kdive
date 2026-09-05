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

Remote base volumes are required to be standalone qcow2 images, but neither the upload-time volume
definition nor a mutable worker-local source proves the bytes of an existing remote volume. A
directory-pool refresh makes libvirt reconstruct volume metadata from the actual remote files.
Admission will use that observation to enforce the terminal-base contract on both lanes. The
supported chain is therefore provably overlay then base. This change must retain the volume source,
storage pool identity, security driver, and cleanup behavior.

## Components and flow

1. `ensure_named_overlay` refreshes the pool before looking up either the base or overlay. The
   refresh is the authority-bearing observation: libvirt reads the actual remote qcow2 headers and
   reconstructs their volume XML. Failure aborts before overlay/domain mutation.
2. It resolves the requested base volume from that refreshed view, reads its XML, requires no
   `backingStore`, and obtains its path. This covers operator-staged, newly uploaded supplied,
   legacy supplied, retry, and replaced-worker-source lanes. On reuse, it reads the refreshed
   overlay XML and verifies that its immediate backing path equals the observed base path.
3. `PreparedOverlay` carries `name`, `backing_path`, and `created`. A missing or malformed backing
   record, or a mismatch on reuse, is a configuration failure before domain definition.
4. Provisioning passes `backing_path` to `render_domain_xml`.
5. The disk remains `type="volume"` with the existing pool/volume source. The renderer adds
   `<backingStore type="file"><format type="qcow2"/><source file="..."/><backingStore/>
   </backingStore>`.
6. Libvirt's existing `virt-aa-helper` processes the definition and emits an exact read grant for
   that base. No host-wide AppArmor rule is installed.

The path is libvirt-produced, not constructed from a request or inventory text. XML construction
uses ElementTree attribute encoding. Provider errors expose the volume name and category but not
the host path.

## Failure behavior

- A missing requested base retains the current configuration error.
- A supplied or operator-staged base whose refreshed metadata carries any backing store is rejected
  as a configuration error before overlay/domain mutation. A pool-refresh or XML-read failure is
  an infrastructure failure without raw path leakage.
- An unexpected libvirt lookup, path, or XML-read failure is an infrastructure failure.
- A reused overlay with absent, malformed, or different backing metadata is a configuration error;
  it is neither rewritten nor deleted because another running or recoverable System may own it.
- The volume returned by overlay creation is read back immediately. Missing, malformed, or
  divergent backing metadata causes that new overlay to be deleted before provisioning fails.
- Domain-start retry and port allocation behavior is unchanged.

## Verification

- Storage tests prove refresh precedes every lookup; refreshed standalone bases are admitted while
  nested operator, newly uploaded supplied, legacy/retry supplied, and replaced-local-source cases
  are rejected from the remote metadata. Refresh failure precedes mutation. New and reused overlays
  return the exact libvirt base path; both created/reused overlays fail closed on absent or
  divergent backing metadata, and created-overlay failures prove deletion.
- XML tests prove a volume disk renders one file-backed base and an explicit terminal node, with
  XML metacharacters encoded rather than becoming structure.
- Provisioning tests capture the exact definition passed before `domain.create()` and prove it
  contains the overlay volume and matching base path.
- A native clean Ubuntu proof first creates two test-owned chained base volumes outside libvirt's
  metadata path: one operator-style catalog name and one deterministic pre-existing supplied-style
  name. Each case calls production `ensure_overlay`, proves its internal refresh reconstructs the
  embedded backing record, receives `CONFIGURATION_ERROR`, and observes no overlay or domain
  mutation. The supplied case deliberately changes the worker-side source fixture before retry so
  the verdict can only come from remote bytes. Cleanup removes both negative fixtures.
- The same proof then uses a standalone catalog base with production `ensure_overlay` and
  `render_domain_xml`, starts the domain under an enforcing generated AppArmor profile, and verifies
  the generated `.files` entry names the base without disabling the security driver. A test-owned
  decoy file in the same pool must be absent from the profile, as must any pool-wide wildcard rule.
  Cleanup removes the test domain, overlay, base, and decoy.
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

- Pool refresh plus exact volume lookup binds the grant to the detected remote bytes of the selected
  base; reuse equality binds it to the existing overlay. Failure is closed before define/start.
- ElementTree encodes the path in XML.
- The terminal backing node bounds the represented chain to the provider's admission-checked
  standalone-base contract.
- Error details and public proof output omit host paths.
- AppArmor access remains per-domain; no shared abstraction or security-driver default changes.

### Out of scope

Nested base-image chains are rejected rather than recursively granted. Malicious
libvirt daemon output is outside the model because the provider already entrusts that daemon with
domain and storage control. Hot-plug and block-commit chains are unrelated to this provision-time
definition.
