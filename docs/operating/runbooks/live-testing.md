# Runbook: running the live test tiers

The canonical answer to "how do I run each live test tier, and what does it
need?" KDIVE has four live-test surfaces at different maturity and hardware
levels. Their environment quirks — session vs system libvirt, a short
`XDG_RUNTIME_DIR`, modular daemons, per-mode guest confinement — used to live
only in one test file and in maintainer memory, so every new live test
re-derived them. This page is the single place that records them.

**Design of record:** the spec
[`2026-07-18-live-test-framework.md`](../../design/2026-07-18-live-test-framework.md)
and ADR-0386 (runner topology), ADR-0353 (the `live_vm_tcg` tier), and ADR-0387
(self-hosted host codification). This runbook summarizes and points into that
design; it does not restate it.

## The four tiers at a glance

The pytest markers are declared in `pyproject.toml`
(`[tool.pytest.ini_options].markers`) and gate two distinct test *vehicles* — a
tier is not a single harness.

| Marker | `just` recipe | Vehicle | What it drives | Accel | Where it runs |
| --- | --- | --- | --- | --- | --- |
| `live_stack` | `just test-live-stack` | live-stack spine over MCP/HTTP | the running stack, end to end | n/a | any host with the stack + compose backends |
| `live_vm` (native) | `just test-live` | direct provider ops / a provisioned System | boot a real kernel, crash it, introspect | KVM | self-hosted, arch-labeled KVM host |
| `live_vm_tcg` | `just test-live-tcg` | live-stack spine (ADR-0353) | the ppc64le provision→boot→crash→retrieve proofs, emulated | TCG | hosted `ubuntu-latest` (compose + S3, no `/dev/kvm`) |

`just test` (the default PR suite) selects `-m "not live_vm and not
live_stack"`, so none of the tiers below run in the ordinary gate. Each tier
**skips cleanly** when its environment is absent — but a tier whose env is set
*wrong* fails loud rather than skipping (see [Skip vs. fail](#skip-vs-fail-a-skip-must-not-look-like-a-pass)).

`live_vm_tcg` is deliberately **not** a throwaway-domain test: by ADR-0353 every
`live_vm_tcg` proof also carries the `live_stack` marker and runs the spine over
the stack. It needs no `/dev/kvm`, but it does need a full stack bring-up.

## The `live_vm` marker spans three families

`pytest -m live_vm` selects every family below. A run that exports only one
family's env would silently skip the others and still report green — the "green
run that is no coverage" failure this framework exists to kill. So each family
carries an **additive sub-marker** (the test keeps the bare `live_vm` marker and
adds one) and its own `require_live_vm_*` gate.

| Sub-marker | Required env | Default libvirt mode | Served by |
| --- | --- | --- | --- |
| `live_vm_throwaway` | `KDIVE_LIVE_VM_ROOTFS` (a bootable rootfs qcow2) | `qemu:///system` (per-test; some tests force `qemu:///session`) | `boot_throwaway_domain` (`kdive.testing.live_vm`) |
| *(gdbstub-preserve debug)* | `KDIVE_LIVE_VM_BZIMAGE` (an early-panicking kernel) | `qemu:///session` | its own production XML (a `gdb_port` harness extension is pending) |
| `live_vm_provisioned` | `KDIVE_LIVE_VM_SYSTEM_ID` + `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET` | `qemu:///system` | an externally provisioned System through the live stack |

The env reads live in `tests/live_vm/__init__.py` (kept out of `src/` so the
ADR-0087 config-env guard is not tripped by test-only vars). That module also
exposes the `require_live_vm_throwaway` / `require_live_vm_bzimage` /
`require_live_vm_provisioned` gates — the `live_vm` analogue of the
`require_issuer` / `require_stack` / `require_guest_arch` gates the stack tiers
use.

## The environment contract

The contract is the seam between "the runner host" and "the tests" — the thing
whose absence forces relearning. `KDIVE_LIBVIRT_URI` is the operator escape
hatch across every family; the resolved URI is the single value a test threads
into `boot_throwaway_domain(mode=…)`.

- **libvirt URI / mode is a per-test contract variable, not one global pin.**
  The throwaway family itself splits: a capture-traffic test needs
  `qemu:///session` (unprivileged, dodges the `qemu:///system` root-readback
  wall) while a snapshot test uses `qemu:///system` (the product default). The
  harness carries each test's mode rather than forcing one.
- **Environment variables** are read in one place per family (the table above),
  never per module. S3 *credentials* for the provisioned family are **not** env
  vars — they are file-based under `KDIVE_SECRETS_ROOT`; the resolver checks
  only that the endpoint + bucket env is present.
- **`XDG_RUNTIME_DIR` must stay short** (e.g. `/run/user/<uid>`) under session
  mode — the QMP socket path has a length limit that a long temp path overruns.
- **libvirt runs as modular daemons** (`virtqemud` / `virtnetworkd`), not the
  monolithic `libvirtd`.
- **Guest confinement is named per environment.** Under **system mode** on the
  RHEL-family self-hosted runner, staged images must be relabeled SELinux
  `virt_image_t`; under system mode on an Ubuntu host, AppArmor's `libvirt-qemu`
  profile applies. **Session mode engages neither** — qemu runs as the invoking
  user with no sVirt relabel — so a session-mode tier sidesteps both.
- **Guest image and matching debuginfo** are staged at a known location and kept
  warm between runs on the self-hosted host.

### Skip vs. fail: a skip must not look like a pass

The `require_live_vm_*` gates distinguish three states, so a mis-provisioned
runner cannot masquerade as "no environment":

- **required env unset** → the gate **skips** (this host simply isn't set up for
  the tier);
- **env set but wrong** (rootfs file missing, staging dir not writable, partial
  `KDIVE_S3_*`) → the gate **fails loud**;
- **env present and valid** → the gate returns the resolved contract.

## Running each tier

### `live_stack` — drive the running stack over HTTP

```
just stack-up          # backends healthy + schema migrated + host-process env
just test-live-stack   # runs -m live_stack; skips cleanly if the stack is absent
```

`just stack-up` reuses the compose backends (Postgres + MinIO + mock-OIDC) and
keeps the host `server`/`worker`/`reconciler` outside compose. Full bring-up,
including the host-process env block, is in the
[live-stack runbook](live-stack.md). To drive a genuinely remote
`qemu+tls://` libvirt host instead, use the
[remote live-stack runbook](remote-live-stack.md).

### `live_vm` (native) — a real kernel on real silicon

```
just test-live         # -m "live_vm and not live_vm_tcg"
```

This needs a KVM/nested-virt host with libvirt, `drgn`, and a kdump-enabled
guest image, plus the per-family env above. Standing up the host reproducibly —
including `XDG_RUNTIME_DIR`, the warm image store, and both boot families under
`qemu:///session` — is the
[self-hosted KVM runner runbook](self-hosted-kvm-runner.md); the ppc64le
(POWER) north-star host is the [POWER host bring-up runbook](power-host-bringup.md).
To validate all four crash-capture methods against such a host, see the
[four-method live run](four-method-live-run.md). Never hand-install a host
dependency for one of these: declare it in the owning Ansible role in the same
change, or the next clean runner reprovision breaks (see the cross-platform and
provisioning-parity notes in [AGENTS.md](../../../AGENTS.md)).

### `live_vm_tcg` — the emulated foreign-arch spine

```
just stack-up          # the tier runs over the live-stack vehicle
just test-live-tcg     # -m live_vm_tcg; skips cleanly without the foreign emulator
```

This runs the four ppc64le provision→boot→crash→retrieve proofs under TCG. It
needs the foreign qemu emulator (e.g. `qemu-system-ppc64`) **and** a running
stack; it skips cleanly (pytest exit 5 tolerated) without either. Because TCG
needs no `/dev/kvm`, this is the tier that runs on a hosted `ubuntu-latest`
runner. The ppc64le prerequisites and container images are in the
[cross-platform guide](../../development/cross-platform.md).

## The shared harness

Throwaway-domain tests boot through one context manager instead of
copy-pasting the boot/teardown dance:

```python
from kdive.testing.live_vm import boot_throwaway_domain

with boot_throwaway_domain(rootfs, arch=arch, name=unique, mode=uri,
                           wait_for="panic") as domain:
    ...  # run one provider op against a live domain; teardown is guaranteed
```

`boot_throwaway_domain` stages a qcow2 overlay beside the rootfs, resolves the
arch-specific machine type / console / kernel format, waits for
`active` / `panic` / `ssh`, yields the live domain, and tears it down (deleting
its overlay) on exit. `mode` (session/system) is per-test. The
gdbstub-preserve debug tests still render their own production XML pending a
harness `gdb_port` extension.

## Hard-won quirks

- **`qemu:///session` dodges the root-readback wall.** A `qemu:///system`
  domain writes a root-owned console log a non-root runner cannot read back;
  session mode runs qemu as the invoking user. This is why the self-hosted
  runner exports `KDIVE_LIBVIRT_URI=qemu:///session` for both boot families.
- **A long `XDG_RUNTIME_DIR` breaks the QMP socket** under session mode — keep
  it short.
- **Staged images need the right label under system mode** — `virt_image_t`
  (SELinux) or the `libvirt-qemu` AppArmor profile — and the rootfs's parent
  dir must be writable, because the boot stages an overlay beside it.
- **`pytest -m live_vm` selects all three families.** If you run a nightly for
  only one, declare which families you intend to run and let the fail-loud gate
  catch a missing declared family, rather than skipping to green.
- **Fakes are blind to what these tiers prove.** `FakeLibvirtConn` / `FakeDomain`
  cannot surface libvirt rejecting domain XML, `filter-dump` emitting no
  packets, a real panic going undetected, snapshot-revert corruption, or
  arch/accel resolving wrong on real silicon. That is the whole reason the live
  tiers exist.

## See also

- Spec: [`2026-07-18-live-test-framework.md`](../../design/2026-07-18-live-test-framework.md)
- [ADR-0386 — live-test framework and arch-additive runner topology](../../adr/0386-live-test-framework-runner-topology.md)
- [ADR-0353 — the `live_vm_tcg` tier](../../adr/0353-live-vm-tcg-tier.md)
- [ADR-0387 — self-hosted KVM runner host codification](../../adr/0387-selfhosted-kvm-runner-host-codification.md)
- [live-stack runbook](live-stack.md) · [self-hosted KVM runner](self-hosted-kvm-runner.md) · [POWER host bring-up](power-host-bringup.md) · [four-method live run](four-method-live-run.md)
