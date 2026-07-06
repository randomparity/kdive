# Spec: operator-gated guest egress on local-libvirt (`restrict=on` opt-out) (#1031)

- Issue: #1031 (parent epic #998; relates #985 / ADR-0312)
- ADR: [ADR-0313](../adr/0313-local-libvirt-operator-gated-egress.md)
- Status: Draft

## Problem

On `local-libvirt`, the guest has **no outbound network egress**, so an agent cannot install tools
at runtime — `dnf`/`apt install` cannot resolve any host. The local domain's SSH-forward NIC renders
with QEMU user-mode (SLIRP) networking and `restrict=on`
(`providers/local_libvirt/lifecycle/xml.py::_append_ssh_forward`):

```
user,id=kdivessh,restrict=on,hostfwd=tcp:127.0.0.1:<ssh_port>-:22
```

`restrict=on` blocks all guest-initiated outbound traffic (NAT'd internet and the SLIRP DNS resolver
at `10.0.2.3`), a deliberate defense-in-depth block (ADR-0218 §1). This contradicts #998 ("the guest
is yours as root — install tools at runtime") and undercuts #985/ADR-0312 (the larger guest disk is
headroom for a runtime-installed tracer toolchain — the space is there, the network to fill it is
not). Verified live during the #985 gate: a booted `debug` System could not resolve
`mirrors.fedoraproject.org`, and no `resolv.conf` change helped — with `restrict=on` there is no
route off the guest at all.

**local-libvirt only.** remote-libvirt uses an operator-staged base image (toolchain pre-installed)
and the real host network via the guest-agent seam.

## Goal

1. **Operator opt-in egress.** An operator can enable guest egress for a local-libvirt Resource via a
   new `guest_egress` field on the `[[local_libvirt]]` `systems.toml` block. When enabled, the guest
   NIC renders `restrict=off` (normal SLIRP NAT + DNS) so the guest reaches its distro mirrors.
2. **Secure default preserved.** Absent or `false`, the NIC renders `restrict=on` — byte-for-byte the
   current behavior (ADR-0218 default-deny). Regression-guarded.
3. **Operator-owned, not request-settable.** The knob is resolved from operator-owned inventory at
   the worker boundary; no allocation/provision tool or `LibvirtProfile` field can set it.

## Non-goals

- **No per-request egress.** Deliberately excluded (confused-deputy avoidance, ADR-0313 §4). No
  `LibvirtProfile.guest_egress`, no tool parameter.
- **No DB schema/migration.** `guest_egress` is resolved op-time from `systems.toml`, not persisted
  to `resources` (ADR-0313 §1). No reconcile change.
- **No runtime retrofit of a running domain.** `guest_egress` renders into the domain XML at
  provision; flipping it takes effect on the next fresh provision, not on a live guest (same property
  as ADR-0281 for the forward). A destructive `systems.reprovision` is the supported re-render path.
- **No curated proxy / no image pre-baking in this slice.** Both are ADR-0313 rejected/deferred
  alternatives.
- **No capability-descriptor / `profile_examples` advertisement.** Out of scope; docs carry the
  "when do runtime installs work" guidance.
- **No new RBAC role, `ErrorCategory`, or config-env setting.**

## Design

### Inventory model (`inventory/model.py`)

- `LocalLibvirtInstance` gains `guest_egress: bool = False` (pydantic default; a pre-existing file
  with no `guest_egress` key parses unchanged as `False`).

### Op-time resolver (new `providers/local_libvirt/config.py`)

- `local_guest_egress_for_resource(resource_name: str) -> bool`:
  - Loads `[[local_libvirt]]` instances via the shared inventory loader
    (`inventory.loader.load_inventory_optional(systems_toml_path())`), mirroring
    `remote_libvirt/config.py::_load_inventory_doc` / `_load_remote_instances`.
  - Returns the named instance's `guest_egress`; returns **`False`** when the file is absent
    (`load_inventory_optional` → `None`) **or** no `[[local_libvirt]]` block names that Resource.
  - A *malformed* file raises `CONFIGURATION_ERROR` from the shared loader and propagates (fail-fast;
    identical to the reconciler's all-or-nothing). This is the one path that fails provisioning — an
    operator with a corrupt inventory, not a legitimate absence.
- Rationale for no-raise on a missing block: unlike remote (host config mandatory, missing instance =
  hard error), the `[[local_libvirt]]` block is optional — discovery creates the Resource with or
  without it. Defaulting to egress-off is both the secure default and the back-compatible one.

### Provisioner (`providers/local_libvirt/lifecycle/provisioning.py`)

- `LocalLibvirtProvisioning` carries a `guest_egress: bool = False` (constructor field; `from_env`
  keeps the `False` default for host-agnostic callers). `provision()` passes
  `guest_egress=self._guest_egress` into `render_domain_xml`.

### Composition + rebind seam (`providers/local_libvirt/composition.py`)

- `build_runtime` gains `resource_name: str | None = None`. When set, it resolves
  `local_guest_egress_for_resource(resource_name)` and builds
  `LocalLibvirtProvisioning.from_env(guest_egress=...)`; when `None` (host-agnostic construction) it
  keeps egress-off.
- `build_runtime` sets `rebind_for_resource = lambda name: build_runtime(secret_registry=secret_registry, resource_name=name)`,
  activating the seam the resolver already invokes (`ProviderRuntime.for_resource` →
  `rebind_for_resource`, ADR-0187), which local previously left unset (no-op). This mirrors
  remote-libvirt's `_rebind_for_resource`.
- Recursion note: `build_runtime` sets a hook that calls `build_runtime` again with a `resource_name`;
  `for_resource` is called once per op by the resolver, not recursively — identical to remote, no
  loop.

### Renderer (`providers/local_libvirt/lifecycle/xml.py`)

- `render_domain_xml` gains keyword-only `guest_egress: bool = False`, forwarded to
  `_append_ssh_forward(domain, ssh_port, guest_egress=guest_egress)`.
- `_append_ssh_forward` computes `restrict = "off" if guest_egress else "on"` and renders
  `user,id=kdivessh,restrict={restrict},hostfwd=tcp:127.0.0.1:<port>-:22`. Everything else (the
  pinned `addr=0x10` PCI slot, `virtio-net-pci`, loopback `hostfwd`) is unchanged. Update the
  function + `render_domain_xml` docstrings: `restrict=on` is now the *default* of an operator
  policy, not unconditional.

## Test plan

Renderer (`tests/providers/local_libvirt/test_provisioning.py`):

- The existing SSH-forward assertions hardcode `restrict=on`; keep them as the **default** (no
  `guest_egress` → `restrict=on`).
- Add `test_render_ssh_forward_egress_enabled`: `render_domain_xml(..., guest_egress=True)` emits
  `restrict=off` in the `-netdev` arg and the `hostfwd`/`addr=0x10`/`virtio-net-pci` args are
  otherwise unchanged (assert the full arg list to pin that only `restrict` flips).
- Add `test_render_ssh_forward_egress_default_is_restricted`: an explicit `guest_egress=False` (and
  the omitted-kwarg default) emit `restrict=on`.

Resolver (`tests/providers/local_libvirt/test_egress_config.py`, new):

- `guest_egress=True` block for the named Resource → `True`.
- `guest_egress` omitted / `false` → `False`.
- No `[[local_libvirt]]` block naming the Resource → `False` (not an error).
- Absent `systems.toml` (loader returns `None`) → `False`.
- Malformed `systems.toml` → `CONFIGURATION_ERROR` propagates.
- (Loader is exercised against a `tmp_path` `systems.toml` with `KDIVE_SYSTEMS_TOML` pointed at it —
  the boundary is the filesystem read, mocked via the real loader + a temp file, not a stub.)

Composition/rebind (`tests/providers/local_libvirt/test_composition.py` or the provisioning test):

- `build_runtime(...).rebind_for_resource` is set; `for_resource("name-with-egress")` yields a
  runtime whose provisioner renders `restrict=off`, and `for_resource("name-without")` renders
  `restrict=on`. This is the end-to-end proof that the operator field reaches the rendered NIC.

Model (`tests/inventory/test_model.py` or the inventory parse tests):

- A `[[local_libvirt]]` block with `guest_egress = true` parses; a block omitting it defaults to
  `False`; a pre-existing file without the key is unaffected.

## Risks

- **Security posture.** With egress on, an agent-supplied kernel can egress; the operator accepts
  this and the network-zone firewall becomes the enforcement boundary (ADR-0313 Consequences). The
  default is unchanged and secure; the risk is opt-in only.
- **New op-time dependency on `systems.toml` readability for local provisioning.** Today local
  provisioning reads only `KDIVE_LIBVIRT_URI`; it will now also read `systems.toml` per op. Bounded:
  a missing file/block is the secure default (no error); only a *malformed* file fails provisioning,
  which is the same operator-corruption signal the reconciler already raises — and an operator with a
  malformed inventory has a broken fleet regardless.
- **Reprovision to take effect.** Flipping `guest_egress` requires a fresh provision to re-render the
  NIC; a define-only retry against a running domain will not change live QEMU (ADR-0281 §Non-goals).
  Documented in the runbook.
