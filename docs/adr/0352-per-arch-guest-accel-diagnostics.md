# ADR 0352 — Per-arch guest-accelerator diagnostics: a `guest_arch_accel` doctor check + cross-arch dep/preflight advisories

- **Status:** Accepted
- **Date:** 2026-07-14
- **Issue:** #1153
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0338 (#1140 `guest_arches` discovery — the accel/emulator facts
  this check mirrors), ADR-0340 (#1142 accel-derived domain XML — the KVM-vs-TCG rule
  this reuses), ADR-0341 (#1143 `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER`), ADR-0347
  (#1149 the `multiarch_gdb` worker-vantage check — the PATH-probe pattern), ADR-0349
  (#1151 the `pseries_fadump` worker-vantage check — the co-located contribution), and
  ADR-0091 (the doctor diagnostics model, three-state checks, "doctor cannot be its own
  oracle").

## Context

Cross-arch support (a ppc64le guest under TCG on an x86_64 host, or the reverse) is now
functional: discovery advertises `guest_arches` with a per-arch accelerator, admission
validates a profile against it, and the domain XML plus deadline multiplier act on the
resolved accel. But "which qemu emulators does this host have, and which guest arch runs
native-KVM vs TCG-only" is invisible to an operator until a guest boots slowly (TCG) or a
provision is rejected. The three surfaces an operator consults — the service `doctor`,
`check-local-libvirt.sh` (pre-deploy preflight), and `check-setup-deps.sh` (dep-checker)
— each only know a single host-arch qemu hint (`check-setup-deps.sh:167`, and
`check-local-libvirt.sh` hardcodes `qemu-system-x86_64`), so a POWER host is mis-served
and the TCG-only story is unstated.

Three facts constrain the decision:

1. **The accel facts already exist.** Discovery derives them from libvirt capabilities
   (`parse_guest_arches` reads `<domain type='kvm'>`, the qemu/libvirtd view), persisted
   on the System at provision. A diagnostic that re-observes the host must not diverge
   from that for a benign reason. The host-KVM signal libvirt gates `<domain
   type='kvm'>` on is the **presence** of `/dev/kvm`; a guest arch is KVM when it is the
   host's native arch and `/dev/kvm` is present, else TCG. Presence (not a *worker*-uid
   R+W test) is the right probe: a non-root worker under `qemu:///system` — an
   explicitly-supported deployment — cannot R+W `/dev/kvm`, yet qemu (the qemu uid) still
   KVM-accelerates the domain, so a R+W probe would falsely report a real KVM arch as
   TCG. `os.path.exists('/dev/kvm')` succeeds regardless of the worker's uid.
2. **The doctor model forbids self-oracle checks.** ADR-0091 says a check observes the
   host, not kdive's own recorded state. The two prior local worker-vantage checks
   (`multiarch_gdb`, `pseries_fadump`) both probe the worker's PATH with no DB/libvirt
   handle for exactly this reason.
3. **The qemu binary name is arch-asymmetric.** `x86_64 → qemu-system-x86_64` (matches
   `uname -m`), but `ppc64le → qemu-system-ppc64` (POWER has no `-ppc64le` binary). Any
   "derive binary from arch" logic that assumes `qemu-system-$(uname -m)` breaks on
   POWER.

## Decision

Add one worker-vantage `guest_arch_accel` check to the single local-libvirt diagnostic
contribution, and generalize the two shell scripts and the install docs to the same
per-arch model.

- **`guest_arch_accel` check** reports, per schedulable guest arch (its qemu emulator is
  on the worker's PATH), whether it runs under KVM or is TCG-only. It probes PATH +
  `/dev/kvm` **presence** directly (`host_arch`, `supported`, `which`, `kvm_present` are
  injected), so it needs no DB or libvirt call and cannot diverge from a stale
  inventory. It carries the accel map in `CheckResult.data` (serialized into `doctor
  --json`) and names the distinction in `detail`.
- **The check FAILs on exactly one condition — the host cannot schedule its own native
  arch.** When the host's own arch is a supported arch and its qemu emulator is absent,
  the host cannot run even native guests; `doctor` is the only post-deploy surface, so
  it FAILs with a `MISSING_DEPENDENCY` fix naming the native qemu package. Otherwise the
  check PASSes, carrying the accel map. A **foreign** arch's absence never fails
  (cross-arch is optional), and a host whose own arch kdive does not support PASSes with
  no native expectation. The accel map is the *data*; native schedulability is the
  *pass/fail* — one coherent check, not two responsibilities.
- **`check-local-libvirt.sh`** probes the **host-native** qemu binary (arch-derived,
  fixing the x86 hardcode) as its required check, and prints an informational
  "guest arch X available via TCG only" line when a foreign emulator is present.
- **`check-setup-deps.sh`** keeps the host-native qemu in the future tier and adds a
  cross-arch advisory: foreign qemu present → "available via TCG only"; absent → an
  install hint naming the exact per-distro package (`package_for`).
- **The Python arch→qemu-binary map is co-located with the check** (a
  `qemu_system_binary(arch)` helper over `SUPPORTED_ARCHES`), mirroring how
  `multiarch_gdb` keeps gdb binary selection near the tool rather than in `arch_traits`.
- **Docs** (`install.md`, `image-lifecycle.md`) name the same per-distro packages and
  the `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` expectation.

No migration, no new dependency, no schema or state change.

## Consequences

- An operator sees the KVM-vs-TCG accel map at `doctor` time (and a FAIL if the host
  lost its native emulator post-deploy — a gap the pre-deploy scripts cannot cover once
  the service is running), and the exact foreign-arch package at dep-check/install time,
  on either host arch.
- The accel rule is re-observed by this check independently of discovery, but both key
  off the same host fact (`/dev/kvm` presence = what libvirt gates `<domain type='kvm'>`
  on), so they agree for the supported deployments — including a non-root worker under
  `qemu:///system`. The residual case where libvirt could still refuse KVM despite
  `/dev/kvm` being present (e.g. a per-domain restriction) is rare and is not treated as
  a defect by this informational map. Both paths are pinned by tests.
- The arch→qemu-binary asymmetry now has a Python home and a regression test, closing a
  latent `qemu-system-$(uname -m)` trap for a future arch.
- Adding a future arch is one `SUPPORTED_ARCHES` row plus one qemu-binary-map entry (and
  the shell scripts' `SUPPORTED_ARCHES` array); the diagnostic and advisories extend
  automatically.

## Considered & rejected

- **Keep the check purely informational (never fail, even on an absent native
  emulator).** Rejected: `doctor` is the only *post-deploy* surface, and the
  `check-local-libvirt.sh` / dep-checker gates run only *pre-deploy* — so a native
  emulator removed after deploy (a package upgrade) would leave `doctor` green on a host
  that cannot provision its own arch. The pre-deploy scripts do not "gate" a post-deploy
  fault they never run for. Failing only on the native-arch floor (never on optional
  foreign arches) closes that blind spot without turning the accel map into a gate.
- **Probe worker-uid `/dev/kvm` read/write instead of presence.** Rejected: a non-root
  worker under `qemu:///system` cannot R+W `/dev/kvm`, yet qemu KVM-accelerates the
  domain — a R+W probe would falsely report a real KVM arch as TCG at the exact surface
  meant to make accel legible. Presence matches libvirt's own KVM gating.
- **Fail on an absent *foreign* emulator.** Rejected: cross-arch (TCG) guests are
  optional; their absence is a normal single-arch host, not a defect.
- **Read the accel map from the persisted `guest_arches` inventory instead of probing.**
  Rejected: violates ADR-0091 (a doctor check must not be its own oracle) — a stale row
  would mask a real host change (a removed emulator, a lost `/dev/kvm`).
- **Put the arch→qemu-binary map in `arch_traits`.** Rejected for now: `arch_traits`
  holds domain-XML facts; tool-binary selection lives near its tool
  (`multiarch_gdb`/gdbmi precedent). A future consolidation is cheap if a third consumer
  appears.
- **A `note_warn` for a TCG-only arch in the shell scripts.** Rejected: an available TCG
  arch is not a defect; `WARN`/`fix` framing would mis-signal. It is an informational
  line.
- **Emit the advisory only from the dep-checker (skip the doctor check).** Rejected:
  Acceptance-1 requires the distinction in `doctor` output specifically; the dep-checker
  runs pre-`uv sync`, not post-deploy, and cannot probe `/dev/kvm` the way the worker
  host does.
