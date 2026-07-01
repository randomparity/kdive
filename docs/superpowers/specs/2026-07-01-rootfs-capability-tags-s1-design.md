# Rootfs capability tags (S1) — per-distro static capabilities

**Status:** Draft for review
**Date:** 2026-07-01
**ADR:** [0287](../../adr/0287-per-distro-capability-tags.md)
**Depends on:** #957 / ADR-0286 (the `Capability` enum and `capability_signals` framework)

## Problem

Every debug rootfs image is stamped with the same `(agent, kdump, drgn)` capability tuple,
regardless of distro, because `images/rootfs_specs.py` reads a fixed `_KIND_CAPABILITIES`
table rather than asking the family what it bakes. The table is wrong three ways:

- it **asserts a false tag**: `agent` is defined as the QEMU guest agent
  (`GUEST_CONTRACT_PATHS["agent"] = /usr/sbin/qemu-ga`, `images/validation.py`), but the
  local families (`RhelFamily`/`DebianFamily`) install **no** qemu-guest-agent — local-libvirt
  drives guests over SSH plus the `kdive-ready` serial unit, not qemu-ga. So `agent` is false
  for every local staged image today;
- it **omits** traits every debug image bakes (`ssh` — openssh-server is explicitly installed
  — and the MAC posture); and
- it **flattens** a real per-distro difference: rhel sets SELinux, debian uses AppArmor.

An agent reading `capabilities` sees a phantom `agent`, cannot tell a SELinux Fedora image
from an AppArmor Debian image, and cannot see that sshd is baked in.

## Scope

This is the first of four sub-projects (see ADR-0287 context). **S1 is boot-free and adds
no new build behavior** — it makes the capability *tags* describe, per distro, what today's
build already bakes. Runtime verification (does ssh actually answer, does sysrq trigger,
does it boot direct-kernel) is S2–S4 and out of scope here.

In scope:
- Add `ssh`, `selinux`, `apparmor` to the `Capability` enum.
- Add a `FamilyCustomizer.capabilities()` seam; each family declares its baked tags.
- Stop declaring `agent` for local families (it is false — no qemu-ga is installed); the
  `agent` member stays in the enum, reserved for the remote provider's guest contract.
- Route `catalog_rootfs_build` through the family; delete `_KIND_CAPABILITIES`.
- A structural guard tying declared tags to build evidence, iterated over the whole family
  registry.
- Converge the repo-tracked staged-path metadata to the family-accurate per-distro sets.

What S1's honesty claim does and does not cover: the guard proves **declaration ↔ recipe
consistency** — that a family declares exactly the tags its own `packages()`/`customize_argv`
install. It does **not** prove **recipe ↔ image efficacy** — that the installed tooling
actually works at runtime (sshd answers, the relabel didn't deny the key). Runtime truth is
verified by the S2 boot-probe; S1 tags mean "the build installs this", not "this works".

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

Each family returns exactly what its `packages()`/`customize_argv` install. The MAC posture
is set for **both** kinds (so `selinux`/`apparmor` is on every image); `ssh` is declared only
where **openssh-server is explicitly in `packages()`** — the debug sets — so the tag never
rests on the base image happening to ship sshd; `kdump`/`drgn` are debug-only (gated on the
kdump/drgn packages); `build` marks the build kind. `agent` is **not** declared — the local
families install no qemu-guest-agent.

| family · kind | capabilities |
| --- | --- |
| rhel · debug | `ssh, selinux, kdump, drgn` |
| rhel · build | `selinux, build` |
| debian · debug | `ssh, apparmor, kdump, drgn` |
| debian · build | `apparmor, build` |

Tags are **EL-major-invariant**: EL 8/9 vs Fedora/EL 10 differ in `packages()` (drgn from
EPEL, makedumpfile bundled in `kexec-tools`, openssh-server present in each debug set) but
reach the same trait set. `capabilities()` therefore does not branch on `version` for the
rhel family the way `packages()` does; the `distro`/`version` parameters exist for parity
with `packages()` and future use.

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

A test (`tests/images/families/test_capability_evidence.py`) iterates the **live family
registry** (`kdive.images.families._FAMILIES`, not a hand-listed set) × both kinds, and
asserts each declared tag is backed by concrete build evidence:

| tag | evidence |
| --- | --- |
| `ssh` | `openssh-server` is in `packages()` (an explicit install, not a bare enable step) |
| `selinux` / `apparmor` | equals `_mac_tag(family.guest_mac)` |
| `kdump` | a kdump package (`kexec-tools`/`kdump-tools`) in `packages()` |
| `drgn` | a drgn package (`drgn`/`python3-drgn`) in `packages()` |
| `build` | `kind == "build"` |
| `agent` | not declared by any local family (reserved for the remote guest contract) |

The guard reads `packages()` and drives `customize_argv` with a synthetic `CustomizeContext`,
inspecting the resulting tokens; it needs no libguestfs and no boot. Iterating `_FAMILIES`
means a newly-registered family is covered automatically — including that its `guest_mac`
maps to a MAC tag (so an unmapped posture fails the guard here, not at a later `build-fs`).

What the guard proves and does not prove: it enforces **declaration ↔ recipe** consistency
(the family declares exactly what its `packages()`/`customize_argv` install). It does not
prove **recipe ↔ efficacy** (that a `systemctl enable` was not a no-op, that sshd actually
answers). That is the S2 boot-probe's job; S1 deliberately stops at "the build installs
this".

### Staged-path metadata convergence

Update the hand-authored `capabilities` in repo-tracked staged-path declarations to the
family-accurate per-distro sets:

- `systems.toml.example`, `docs/operating/providers/examples/systems-local-libvirt.toml`,
  `examples/local-libvirt/README.md`
- `fixtures/local-libvirt/*` staged-path entries
- `admin/default_fixtures.py`

Fedora/Rocky/CentOS-Stream debug entries → `["ssh", "selinux", "kdump", "drgn"]`;
Debian debug entries → `["ssh", "apparmor", "kdump", "drgn"]` (no `agent` — see the Problem
section). All repo-tracked staged-path entries are debug-kind rootfs images; there are no
build-kind staged entries to converge. This makes a declared local image match what
`build-fs` would stamp; the existing fixture-load tests confirm the tokens are valid
vocabulary, and any test asserting the prior literal set is updated in the same change.

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
  family accepts, exactly as `packages()` does.
- An unknown `guest_mac` posture (a new family added without a MAC mapping) surfaces from
  `_mac_tag` at **`capabilities()` call time** — i.e. during `catalog_rootfs_build`/`build-fs`,
  not at family construction. Because the anti-drift guard iterates the live `_FAMILIES`
  registry, any registered family with an unmapped posture fails the guard in CI before it can
  reach a real build. To make the runtime surface actionable rather than a bare traceback,
  `catalog_rootfs_build` wraps an unmapped-posture `ValueError` as a `CONFIGURATION_ERROR`
  naming the family and posture.
- No new failure path for the two shipped families: their postures map, so capability
  resolution is pure and precedes the build.

## Testing

- **Unit** (`tests/images/test_rootfs_specs.py` / family tests): `RhelFamily.capabilities`
  for `debug`/`build` across `fedora 44`, `rocky 8` (EL8), `rocky 10` (EL10) all return the
  same rhel tuples; `DebianFamily.capabilities` for `12`/`13`; assert exact membership,
  including that no set contains `agent`.
- **Guard** (`tests/images/families/test_capability_evidence.py`): iterates `_FAMILIES` ×
  `{debug, build}`; asserts declared ⊆ evidenced per the evidence table (the anti-drift
  mechanism above), and that every family's `guest_mac` maps to a declared MAC tag.
- **Integration** (`tests/images/test_catalog_resolver.py` or `test_rootfs_specs.py`):
  `catalog_rootfs_build("local-libvirt", "fedora-kdive-ready-44")` carries `selinux` and not
  `apparmor`/`agent`; `...("...","debian-kdive-ready-13")` carries `apparmor` and not
  `selinux`/`agent`.
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
