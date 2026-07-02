# ADR 0290 — Guest-contract probes must point at markers the build actually bakes

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** kdive maintainers

## Context

ADR-0286 reconciled the `GUEST_CONTRACT_PATHS` **keys** into the closed `Capability`
vocabulary so a guest-contract element can never fail the capability read-back. It did not
verify the **paths** those keys map to. Auditing exactly what each distro bakes (the follow-up
to ADR-0287, PR#960) found the paths had drifted from reality:

- `drgn` → `/usr/lib/kdive/drgn-ready` and `helpers` → `/usr/lib/kdive/allowlisted-helpers`
  are **phantom**: no `customize_argv` on any family writes them (the drgn contract is proven by
  the staged `/usr/local/sbin/kdive-drgn` helper, ADR-0220). The #830 spec merely *assumed* the
  markers existed.
- `kdump` → `/usr/lib/systemd/system/kdump.service` is **rhel-only**: Debian's unit is
  `kdump-tools.service` (ADR-0287), so a Debian debug image bakes kdump correctly yet would fail
  the probe.
- `agent` → `/usr/sbin/qemu-ga` is baked by no local family (ADR-0287 dropped the local `agent`
  tag; qemu-ga is the *remote* provider's seam), yet it stayed in the MCP upload default
  `DEFAULT_REQUIRED_CONTRACT = ("agent", "kdump", "drgn")`.

The only reason this never fired a live failure is that local images are built through
`build-fs` (the plane runs `verify_cloud_init`, not `validate_guest_contract`); the worker
`IMAGE_BUILD` handler — the only caller that validates the contract, and the only provider it
supports is local-libvirt — would reject *every* debug image on `drgn`. The existing guard
(`GUEST_CONTRACT_PATHS` keys ⊆ `Capability`) checks vocabulary membership, never that a path is
written, so the drift was invisible. Separately, both families ran `systemctl enable
sshd/ssh.service` **unconditionally**, so a build-host image enabled a service its own package
set never installed (working only because the cloud base ships openssh).

## Decision

Make the guest contract probe only markers the build actually writes, using the same constants
the families bake so it cannot re-drift.

- **`GUEST_CONTRACT_PATHS` = `{kdump: KDUMP_SYSCTL_PATH, drgn: DRGN_HELPER_GUEST_PATH}`**,
  imported from the family primitives. `drgn` points at the staged `kdive-drgn` helper. `kdump`
  points at the family-neutral NMI-panic sysctl `/etc/sysctl.d/99-kdive-kdump.conf`, which both
  families write on every kdump image gated identically to kdump enablement — chosen over a
  systemd unit path because it is the artifact **kdive itself** writes (no dependency on
  per-distro on-disk unit locations, the same guessing that produced the phantom markers).
- **Drop `agent` and `helpers`** from the vocabulary and from `DEFAULT_REQUIRED_CONTRACT`
  (now `("kdump", "drgn")`): no local family bakes either, and requiring one is now a
  `CONFIGURATION_ERROR` rather than a silent pass against a phantom path.
- **A new anti-drift guard** (`test_guest_contract_markers_are_baked_by_declaring_families`)
  asserts every `GUEST_CONTRACT_PATHS` value appears in a declaring family's `customize_argv`,
  tying the file probe to the real recipe. It fails on a phantom or distro-wrong marker at unit
  time, not at a live build.
- **Couple the SSH-enable to the debug kind.** `capabilities()` ties the `ssh` tag to `kind`
  (every debug image, never a build-host image), so the `systemctl enable sshd/ssh.service` step
  now gates on `ctx.kind == "debug"` — the same predicate — rather than running unconditionally.
  Gating on `kind` rather than package membership keeps declaration and enablement from
  diverging: a build-host image never enables an SSH service it does not need, and a debug image
  that somehow lacks openssh-server fails the build **loudly** (enable of a missing unit) instead
  of silently shipping an `ssh`-tagged image with no sshd.

## Consequences

- The worker `IMAGE_BUILD`/upload guest-contract validation now checks evidence that exists on
  both rhel and debian images; it can no longer reject a correctly-built debug image.
- `agent`/`helpers` remain `Capability` enum members (reserved; the remote guest contract still
  speaks of qemu-ga) but are not file-verifiable elements. No stored row is affected: local
  families never emitted them and the seed fixtures carry `[]`.
- No migration, no schema change, no new tool, no RBAC change.

## Alternatives considered

- **Multi-candidate kdump unit paths (rhel + debian unit files).** Rejected: it hardcodes
  per-distro on-disk systemd locations across usr-merge that cannot be verified without a live
  image — exactly the assumption that created the phantom `drgn-ready`. The sysctl kdive writes
  is deterministic and family-neutral.
- **Remove `agent`/`helpers` from the `Capability` enum too.** Deferred: removing enum members
  risks deserializing any legacy row that stored them; `agent` is still the remote guest
  contract's word. Pruning is a separate change once no stored row uses them.
- **Write the missing `drgn-ready`/`allowlisted-helpers` markers to satisfy the old paths.**
  Rejected: it invents build steps to match stale validation instead of validating what the
  build already proves (the `kdive-drgn` helper).
- **Leave the unconditional SSH-enable.** Rejected: enabling an uninstalled service is latent
  build fragility and an under-claimed build fact for build-host images.
- **Gate the SSH-enable on `openssh-server` package membership (like kdump).** Rejected: the
  `ssh` tag is `kind`-determined, not package-determined, so a debug build whose packages are
  overridden to drop openssh-server would then be tagged `ssh` yet skip the enable — a silent
  capability lie. Gating on `kind` makes that case fail the build loudly instead.
