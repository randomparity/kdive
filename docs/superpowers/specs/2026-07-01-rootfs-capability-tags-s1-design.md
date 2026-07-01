# Rootfs capability tags (S1) — per-distro static capabilities

**Status:** Draft for review
**Date:** 2026-07-01
**ADR:** [0287](../../adr/0287-per-distro-capability-tags.md)
**Depends on:** #957 / ADR-0286 (the `Capability` enum and `capability_signals` framework)

## Problem

Every debug rootfs image is stamped with the same `(agent, kdump, drgn)` capability tuple,
regardless of distro, because `images/rootfs_specs.py` reads a fixed `_KIND_CAPABILITIES`
table rather than asking the family what it bakes. The table both **omits** traits every
family bakes (`ssh`, the MAC posture) and **flattens** a real per-distro difference (rhel
sets SELinux, debian uses AppArmor). An agent reading `capabilities` cannot tell a
SELinux Fedora image from an AppArmor Debian image, and cannot see that sshd is baked in.

## Scope

This is the first of four sub-projects (see ADR-0287 context). **S1 is boot-free and adds
no new build behavior** — it makes the capability *tags* describe, per distro, what today's
build already bakes. Runtime verification (does ssh actually answer, does sysrq trigger,
does it boot direct-kernel) is S2–S4 and out of scope here.

In scope:
- Add `ssh`, `selinux`, `apparmor` to the `Capability` enum.
- Add a `FamilyCustomizer.capabilities()` seam; each family declares its baked tags.
- Route `catalog_rootfs_build` through the family; delete `_KIND_CAPABILITIES`.
- A structural guard tying declared tags to build evidence.
- Converge the repo-tracked staged-path metadata to the family-accurate per-distro sets.

Out of scope:
- Any boot, probe, or runtime verification (S2+).
- Any new `CapabilitySignal` or change to `images.describe` rendering.
- A `sysrq` tag or a `kernel.sysrq` sysctl stage (S3).
- The operator's real `~/.config/kdive/systems.toml` (corrected out-of-band after merge).

## Design

### Capability vocabulary

`domain/catalog/images.py` — add three members to the closed `Capability` StrEnum:

```python
class Capability(StrEnum):
    AGENT = "agent"
    KDUMP = "kdump"
    DRGN = "drgn"
    BUILD = "build"
    HELPERS = "helpers"
    SSH = "ssh"
    SELINUX = "selinux"
    APPARMOR = "apparmor"
```

The ADR-0286 vocabulary guard (`set(GUEST_CONTRACT_PATHS) <= {c.value for c in Capability}`)
still holds — new members only widen the enum. `helpers`/`drgn` guest-contract semantics in
`images/validation.py` are untouched.

### The family seam

`images/families/base.py` — add to the `FamilyCustomizer` protocol, next to `packages()`:

```python
def capabilities(self, kind: str, distro: str, version: str) -> tuple[Capability, ...]:
    """Return the capability tags this family bakes for `kind` on `distro`/`version`."""
    ...
```

Each family returns exactly what its `customize_argv`/`packages` produce. Because
`customize_argv` enables sshd, injects the key, uploads the readiness unit, and sets the MAC
posture for **both** kinds, `agent`/`ssh`/MAC are on every image; `kdump`/`drgn` are
debug-only (gated on the kdump/drgn packages), and `build` marks the build kind.

| family · kind | capabilities |
| --- | --- |
| rhel · debug | `agent, ssh, selinux, kdump, drgn` |
| rhel · build | `agent, ssh, selinux, build` |
| debian · debug | `agent, ssh, apparmor, kdump, drgn` |
| debian · build | `agent, ssh, apparmor, build` |

Tags are **EL-major-invariant**: EL 8/9 vs Fedora/EL 10 differ in `packages()` (drgn from
EPEL, makedumpfile bundled in `kexec-tools`) but reach the same trait set. `capabilities()`
therefore does not branch on `version` for the rhel family the way `packages()` does; the
`distro`/`version` parameters exist for parity and future use.

Implementation note — derive the MAC tag from the existing `guest_mac` attribute so the tag
and the recorded provenance cannot disagree:

```python
def _mac_tag(guest_mac: str) -> Capability:
    if guest_mac.startswith("selinux"):
        return Capability.SELINUX
    if guest_mac == "apparmor":
        return Capability.APPARMOR
    raise ValueError(f"unmapped guest_mac posture: {guest_mac!r}")
```

### Wiring

`images/rootfs_specs.py` — replace the constant lookup:

```python
spec = RootfsBuildSpec(
    ...
    capabilities=family.capabilities(entry.kind, entry.distro, entry.version),
    ...
)
```

Delete `_KIND_CAPABILITIES` entirely (replace, don't deprecate). `RootfsBuildSpec.capabilities`
stays `tuple[Capability, ...]` (already enum-typed, #957).

### Anti-drift guard

A test (`tests/images/families/test_capability_evidence.py`) asserts, for every
family × kind × representative distro, that each declared tag is backed by concrete build
evidence:

| tag | evidence |
| --- | --- |
| `agent` | the readiness unit is uploaded in `customize_argv` |
| `ssh` | an ssh-enable and/or `--ssh-inject` step appears in `customize_argv` |
| `selinux` / `apparmor` | equals `_mac_tag(family.guest_mac)` |
| `kdump` | a kdump package (`kexec-tools`/`kdump-tools`) in `packages()` |
| `drgn` | a drgn package (`drgn`/`python3-drgn`) in `packages()` |
| `build` | `kind == "build"` |

The guard drives `customize_argv` with a synthetic `CustomizeContext` and inspects the argv
tokens; it needs no libguestfs and no boot. If a future change bakes a trait without
declaring it (or declares one it stopped baking), the guard fails.

### Staged-path metadata convergence

Update the hand-authored `capabilities` in repo-tracked staged-path declarations to the
family-accurate per-distro sets:

- `systems.toml.example`, `docs/operating/providers/examples/systems-local-libvirt.toml`,
  `examples/local-libvirt/README.md`
- `fixtures/local-libvirt/*` staged-path entries
- `admin/default_fixtures.py`

Fedora/Rocky/CentOS-Stream debug entries → `["agent", "ssh", "selinux", "kdump", "drgn"]`;
Debian debug entries → `["agent", "ssh", "apparmor", "kdump", "drgn"]`. This makes a declared
local image match what `build-fs` would stamp, and is validated by the existing
fixture-load/validate tests.

## Data flow

```
resolve_rootfs_entry(name) ── entry(kind,distro,version,family) ──▶ family_for(entry.family)
                                                                          │
                                             family.capabilities(kind,distro,version)
                                                                          │
                                              RootfsBuildSpec.capabilities (tuple[Capability])
                                                                          │
   build-fs ──▶ (publish) image_catalog.capabilities text[]   OR   staged-path declared caps
                                                                          │
                                          images.describe data.capabilities  (per-distro)
```

## Error handling

- `family.capabilities()` is total: it returns a tuple for any `(kind, distro, version)` the
  family accepts, exactly as `packages()` does. An unknown `guest_mac` posture is a
  programming error (a new family added without a MAC mapping) and raises `ValueError` at
  construction — caught by the guard test, never reached at runtime for the two shipped
  families.
- No new failure path in `build-fs`: capability resolution is pure and precedes the build.

## Testing

- **Unit** (`tests/images/test_rootfs_specs.py` / family tests): `RhelFamily.capabilities`
  for `debug`/`build` across `fedora 44`, `rocky 8` (EL8), `rocky 10` (EL10) all return the
  same rhel tuples; `DebianFamily.capabilities` for `12`/`13`; assert exact membership.
- **Guard** (`tests/images/families/test_capability_evidence.py`): declared ⊆ evidenced, per
  family × kind (the anti-drift mechanism above).
- **Integration** (`tests/images/test_catalog_resolver.py` or `test_rootfs_specs.py`):
  `catalog_rootfs_build("local-libvirt", "fedora-kdive-ready-44")` carries `selinux`;
  `...("...","debian-kdive-ready-13")` carries `apparmor`; neither carries the other.
- **Fixture regression**: existing fixture-validate tests pass with the converged per-distro
  capability sets; any test asserting the old `(agent, kdump, drgn)` tuple or referencing
  `_KIND_CAPABILITIES` is updated.

## Rollback

Pure metadata/vocabulary change, no migration and no image-byte change. Reverting the commits
restores `_KIND_CAPABILITIES` and the prior tags; already-built images are unaffected (tags
are catalog metadata, not baked into the qcow2).

## Follow-ons

- **S2** — boot-probe harness in `build-fs` (reuse the local-libvirt lifecycle) + the first
  computed runtime signal `ssh_reachable`.
- **S3** — `sysrq` signal (bake `kernel.sysrq`, verify trigger under the probe).
- **S4** — `direct_kernel_bootable` signal (extract kernel/initrd, boot `-kernel/-initrd`).
