# ADR 0341 — TCG deadline scaling in the local-libvirt provider

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-13
- **Issue:** #1143
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0338 (`guest_arches` discovery), ADR-0339 (admission arch-validation +
  `systems.accel` persist), ADR-0340 (accel-derived domain XML), ADR-0055 §7 (boot window)

## Context

A TCG-emulated guest (a foreign-arch domain, `<domain type="qemu">`) boots an order of
magnitude slower than a KVM-accelerated one. The local-libvirt boot-readiness deadline is
tuned for KVM — `_DEFAULT_BOOT_WINDOW_POLLS = 60` polls × `_POLL_INTERVAL_SECONDS = 5s` =
a 300s ceiling (`lifecycle/install.py`, ADR-0055 §7). Under TCG that ceiling times a boot
out spuriously before the guest ever reaches its `kdive-ready` marker.

ADR-0339 persists the resolved accelerator on the System row (`systems.accel`, migration
`0067`): `kvm` / `tcg`, or `NULL` when the bound Resource advertised no `guest_arches`
(remote-libvirt, fault-inject, a local-libvirt host not re-discovered since ADR-0338). The
migration's own header names "downstream timeout scaling" as the consumer this ADR
implements.

The #1140 review (carried onto this issue) adds a load-bearing caveat: the `accel=kvm`
classification is a **hint**, not a proof. It is derived from libvirt advertising a
`<domain type="kvm">` for the arch, verified live on only two hosts, and the KVM-accelerated
path itself was validated only synthetically (the POWER10 host advertised no KVM domain).
Native bare-metal KVM-HV POWER validation is #1156. So this ADR must not treat `accel=kvm`
as infallible: an over-optimistic `kvm` must degrade to a slow-but-correct boot, never a
spurious timeout.

## Decision

We add **one** operator-tunable accelerator multiplier to the local-libvirt provider
settings and apply it, keyed off the persisted `System.accel`, at the boot-readiness
deadline — the single provision/boot/install deadline that waits on **guest execution**.

**The setting.** `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` (float, default `10.0`) joins the
co-located `KDIVE_LIBVIRT_*` settings (`providers/local_libvirt/settings.py`, ADR-0087). Its
parser rejects a value `< 1.0` with `CONFIGURATION_ERROR` — a multiplier below 1 would make
a TCG deadline *tighter* than the KVM baseline, which is never intended (`1.0` is the
operator opt-out: "do not scale even under TCG"). Being a registry `Setting` it flows into
`docs/guide/reference/config.md` automatically (`scripts/gen_config_reference.py`) and
satisfies `scripts/check_env_documented.py` with no manual doc edit.

**The multiplier (TCG-safe fallback).** A single shared policy function
`tcg_deadline_multiplier(accel: str | None) -> float` in
`providers/local_libvirt/lifecycle/deadlines.py`:

- `accel == "kvm"` → `1.0` (native speed, unscaled).
- **everything else** — `"tcg"` **and** `None`/unknown — → the configured multiplier.

Making `None` scale is the deliberate TCG-safe fallback the #1140 caveat demands: an
absent or over-optimistic accel classification yields the generous deadline, so the worst
case is a slow-but-correct boot, not a spurious timeout. It is *the one* multiplier the
issue mandates — sites that need to scale a guest-execution deadline call this function
rather than each re-deriving a constant.

**Application site — the boot-readiness window only.** `LocalLibvirtBooter` scales its
effective window to `math.ceil(base_polls × tcg_deadline_multiplier(accel))` before the
`_await_ready` poll loop. The window is a *ceiling*, not a fixed wait — `_await_ready`
returns the instant the readiness marker appears — so the wider TCG ceiling costs a KVM
boot nothing and never slows a fast TCG boot; it only defers the timeout verdict.

**Threading `accel`.** The persisted fact reaches the booter through the `Booter.boot`
port, which gains a keyword-only `accel: str | None = None`. The provider-agnostic
`boot_handler` reads `system.accel` from the System row it already has a connection to and
passes it down; `install.py` stays DB-free (it never reads Postgres). The default `None`
means any caller that omits `accel` gets the TCG-safe (generous) window — a safe default
for tests and for the non-scaling providers.

**Audit — sites that do NOT scale.** The acceptance criterion requires auditing every
deadline in the provision/boot/install paths. TCG slows only **guest code execution**; it
does not slow host-native tools. The following are therefore left unscaled, by design:

| Site | Deadline | Why it does not scale |
|------|----------|-----------------------|
| `lifecycle/boot/readiness.py` `_POLL_INTERVAL_SECONDS` | per-poll cadence (5s) | Polling *frequency*, not the total window. Scaling the poll count (above) widens the ceiling; the interval is unchanged. |
| `lifecycle/boot/readiness.py` `_DOMSTATE_PROBE_TIMEOUT` | `virsh domstate` (10s) | A host libvirt query; fast regardless of guest accel. |
| `lifecycle/connect.py` `_SSH_PROBE_TIMEOUT_S` | per-attempt TCP connect (2s) | Host-network-bound once the guest's sshd listens; slow guest start is absorbed by the boot-window retries, not this per-attempt budget. Not on the boot-readiness path (readiness tails the console, it does not SSH). |
| `lifecycle/storage.py` `_QEMU_IMG_TIMEOUT_S` | qemu-img overlay create (5m) | Host-native tool; no guest execution. |
| `lifecycle/rootfs/overlay_customize.py` `_VIRT_CUSTOMIZE_TIMEOUT_S`; `rootfs_build.py` (`SLOW_BUILD_TOOL_TIMEOUT_S`) | virt-customize / build tools (5m / 30m) | Native-arch host tooling (the design routes foreign-arch customization through a firstboot boot, not virt-customize). Its boot, when added (#1152, epic issue 8), reuses `tcg_deadline_multiplier`. |

Scaling a host-native timeout by 10× would not cause spurious timeouts, but it would mask a
genuinely hung host tool behind a 10×-wider window — so leaving them unscaled is the
correct audit outcome, not an omission.

**Scope of scaling providers.** Only the local-libvirt booter scales. Remote-libvirt and
fault-inject booters accept the new `accel` kwarg to satisfy the port and ignore it: their
Systems carry `accel=NULL`, remote-libvirt is KVM, and the multiplier is a local-libvirt
setting.

## Consequences

- A foreign-arch (TCG) System gets a `10×`-wider boot ceiling (3000s by default) and no
  longer times out before its slow boot completes; a native KVM System is byte-for-behavior
  unchanged (`1.0`, 60 polls).
- A local-libvirt System with `accel=NULL` (host not re-discovered since ADR-0338, a
  transient pre-migration state) gets the generous TCG window. This is over-generous for a
  host that is really KVM, but it is the intended safe fallback: the cost is a wider ceiling
  on a boot that still returns the instant it is ready.
- `boot_handler` does one extra System read per boot (the write-time boot path, not a hot
  read). A missing System row yields `accel=None` → the safe window; no crash.
- The default scaled ceiling (3000s) is 10× the job `DEFAULT_LEASE` (5 min,
  `jobs/queue.py`). A boot that runs past 300s therefore relies on the worker's concurrent
  heartbeat renewing the lease (`jobs/worker.py`) — the same mechanism every long provision
  or build job already depends on, so no new risk, but the dependency is now explicit: a
  TCG boot is not wrapped in an `asyncio.wait_for`, and only a genuinely lapsed lease (a
  stalled heartbeat) would let the reconciler reclaim it mid-boot.
- Readiness is console-tail + `virsh domstate` (`lifecycle/boot/readiness.py` `_real_readiness`),
  not an SSH command probe, so the boot window is the only guest-execution deadline on the
  boot path — the audit table's "does not scale" rows carry no hidden guest-execution wait.
- The `Booter` port carries a System-model fact (`accel`), not a provider-specific one, so
  every implementation's signature widens by one ignorable kwarg.
- The multiplier function is the single seam #1152's customization boot reuses, so the
  "one multiplier, not scattered constants" property holds as the epic adds TCG boot sites.

## Alternatives considered

- **Re-derive `accel` from live libvirt capabilities at boot time (as ADR-0340 does at
  provision).** Rejected: the issue explicitly requires keying off the *persisted*
  `System.accel`, not re-derived host state — the persisted fact is exactly what ADR-0339
  recorded so repeated reads (this one included) need not re-probe the host.
- **Let the booter read `System.accel` itself.** Rejected: `lifecycle/install.py` is
  deliberately DB-free so its unit tests exercise the boot orchestration without Postgres.
  The handler already holds the connection; it reads the fact and passes it.
- **Do not touch the `Booter` port; have the handler compute the scaled window and pass a
  poll count.** Rejected: the multiplier is a local-libvirt *setting* and the base window is
  a local-libvirt constant — computing the scaled window in the provider-agnostic handler
  leaks provider policy upward. This differs from ADR-0340's rejection of port-threading:
  there the threaded value (`emulator`) was local-libvirt-only, forcing other providers to
  carry a foreign concept; here the threaded value (`accel`) is a System-model fact every
  provider legitimately has, and the *policy* (multiplier, base window) stays in the
  provider.
- **Scale every provision/boot/install timeout uniformly.** Rejected: host-native tool
  timeouts (qemu-img, virt-customize, build) do not slow under TCG; a 10×-wider window there
  masks a hung host tool. See the audit table.
- **A non-nullable multiplier with no `< 1.0` guard, or an int multiplier.** Rejected: a
  float allows fractional tuning; the `< 1.0` guard stops a misconfiguration that would make
  TCG deadlines tighter than KVM's; `1.0` is the explicit opt-out.
- **Make `NULL` accel unscaled (treat unknown as KVM).** Rejected outright — it is the exact
  over-optimistic classification the #1140 caveat warns against, and would reintroduce
  spurious TCG timeouts whenever discovery lags.
