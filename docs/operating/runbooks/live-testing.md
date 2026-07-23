# Runbook: running the live test tiers

The canonical answer to "how do I run each live test tier, and what does it
need?" KDIVE has three live-test tiers — one per `just` recipe — at different
maturity and hardware levels, and the `live_vm` tier further spans four
families (below). Their environment quirks — session vs system libvirt, a short
session-mode socket path (`XDG_CONFIG_HOME`), modular daemons, per-mode guest
confinement — used to live only in one test file and in maintainer memory, so
every new live test re-derived them. This page is the single place that records
them.

**Design of record:** the spec
[`2026-07-18-live-test-framework.md`](../../design/2026-07-18-live-test-framework.md)
and ADR-0386 (runner topology), ADR-0353 (the `live_vm_tcg` tier), and ADR-0387
(self-hosted host codification). This runbook summarizes and points into that
design; it does not restate it.

## The three tiers at a glance

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

The native `live_vm` tier spans four families (below); `just test-live` runs all
of them (each gated), and one — the remote-libvirt family — additionally has a
focused `just test-live-remote` recipe (`-m live_vm_remote`) for driving it
against a `qemu+tls://` host on its own.

`live_vm_tcg` is deliberately **not** a throwaway-domain test: by ADR-0353 every
`live_vm_tcg` proof also carries the `live_stack` marker and runs the spine over
the stack. It needs no `/dev/kvm`, but it does need a full stack bring-up.

## The `live_vm` marker spans four families

`pytest -m live_vm` selects every family below. A run that exports only one
family's env would silently skip the others and still report green — the "green
run that is no coverage" failure this framework exists to kill. So each family
has its own `require_live_vm_*` gate that fails loud on a mis-set env. Three
additive sub-markers exist — `live_vm_throwaway`, `live_vm_provisioned`, and
`live_vm_remote` — and every test keeps the bare `live_vm` marker alongside its
sub-marker. The gdbstub-preserve debug tests are **not** a fourth sub-marker:
they reuse `live_vm_throwaway`, told apart from the ordinary throwaway tests by
their env (`KDIVE_LIVE_VM_BZIMAGE`) and gate (`require_live_vm_bzimage`), not by
marker.

| Family (sub-marker) | Required env | Default libvirt mode | Served by |
| --- | --- | --- | --- |
| Throwaway (`live_vm_throwaway`) | `KDIVE_LIVE_VM_ROOTFS` (a bootable rootfs qcow2) | `qemu:///system` (per-test; some tests force `qemu:///session`) | `boot_throwaway_domain` (`kdive.testing.live_vm`) |
| gdbstub-preserve debug (`live_vm_throwaway`, shared) | `KDIVE_LIVE_VM_BZIMAGE` (an early-panicking kernel) | `qemu:///session` | `boot_preserved_gdbstub_domain` (`kdive.testing.live_vm`); the caller renders the domain XML (ADR-0392) |
| Provisioned (`live_vm_provisioned`) | `KDIVE_LIVE_VM_SYSTEM_ID` + `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET` | `qemu:///system` | an externally provisioned System through the live stack |
| Remote (`live_vm_remote`) | `KDIVE_LIVE_VM_REMOTE_URI` (a `qemu+tls://` host) + `KDIVE_LIVE_VM_REMOTE_BASE_IMAGE` + `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET` + `KDIVE_LIVE_VM_REMOTE_RECONCILER` | `qemu+tls://` (operator-named; no default host) | direct provider ops against a genuinely remote libvirt host (ADR-0425) |

The env reads live in `tests/live_vm/__init__.py` (kept out of `src/` so the
ADR-0087 config-env guard is not tripped by test-only vars). That module also
exposes the `require_live_vm_throwaway` / `require_live_vm_bzimage` /
`require_live_vm_provisioned` / `require_live_vm_remote` gates — the `live_vm`
analogue of the `require_issuer` / `require_stack` / `require_guest_arch` gates
the stack tiers use.

The **remote** family is the fourth (#1424, epic #1423): the only `live_vm`
family that drives a genuinely remote `qemu+tls://` host the worker shares no
filesystem with, so remote-provider capabilities get a direct provider-op proof
instead of being asserted only through the operator-run `live_stack` spine
(`test_remote_live_stack.py`). Its contract is wider than a URI because two
dependents are otherwise unprovable: the two-phase vmcore retrieve flows through
a **guest-routable** object store (`KDIVE_S3_*`, ADR-0084/ADR-0110), and remote's
console collector is **reconciler-resident** (ADR-0095/ADR-0235), so a
console-dependent proof needs one alive — hence the `KDIVE_LIVE_VM_REMOTE_RECONCILER`
presence marker. The trigger is the `qemu+tls://` URI itself (there is no default
remote host, so `KDIVE_LIBVIRT_URI` is not the lever here); a non-TLS URI or one
carrying `no_verify` **fails loud**, because remote mandates verified mutual TLS
(ADR-0076; the [remote live-stack runbook](remote-live-stack.md) forbids `no_verify`).

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
- **The session-mode QMP socket path is length-limited (108 bytes), and the
  lever is `XDG_CONFIG_HOME`.** Session-mode libvirt derives each domain's QMP
  monitor socket under `$XDG_CONFIG_HOME`, so a deep pytest tmp path overflows
  it. The harness redirects `XDG_CONFIG_HOME` to a short `/tmp/kdive-cl-<hex>`
  path for the duration of a session-mode boot and restores it in teardown
  (`prepare_session_runtime`) — a test that boots through the harness need not
  manage it. (Separately, the self-hosted runner keeps `XDG_RUNTIME_DIR` short
  for the session libvirt daemon's own socket; see its runbook.)
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
including the short session-runtime paths, the warm image store, and both boot families under
`qemu:///session` — is the
[self-hosted KVM runner runbook](self-hosted-kvm-runner.md); the ppc64le
(POWER) north-star host is the [POWER host bring-up runbook](power-host-bringup.md).
To validate all four crash-capture methods against such a host, see the
[four-method live run](four-method-live-run.md). Never hand-install a host
dependency for one of these: declare it in the owning Ansible role in the same
change, or the next clean runner reprovision breaks (see the cross-platform and
provisioning-parity notes in [AGENTS.md](../../../AGENTS.md)).

### `live_vm_remote` — direct provider ops against a remote `qemu+tls://` host

```
just test-live-remote  # -m live_vm_remote; skips cleanly with no remote env
```

This drives the remote-libvirt family (a sub-selection of `live_vm`, also run by
`just test-live`) directly against an operator-provided `qemu+tls://` host. Set
`KDIVE_LIVE_VM_REMOTE_URI` to the host's control URI, `KDIVE_LIVE_VM_REMOTE_BASE_IMAGE`
to the staged base-image volume name, `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET`
to the **guest-routable** object store, and `KDIVE_LIVE_VM_REMOTE_RECONCILER` to a
presence marker for a running reconciler (its metrics endpoint, or `1`). Standing
up the host — mutual TLS, the staged base volume, the gdbstub-port ACL, and
object-store reachability — is the [remote live-stack runbook](remote-live-stack.md).
The URI must be `qemu+tls://` and must not carry `no_verify` (remote mandates
verified mutual TLS, ADR-0076); a wrong scheme or a missing companion fails loud
rather than skipping.

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
                           wait_for="panic", console_log=log_path) as domain:
    ...  # run one provider op against a live domain; teardown is guaranteed
```

`boot_throwaway_domain` stages a qcow2 overlay beside the rootfs, resolves the
arch-specific machine type / console / kernel format, waits for
`active` / `panic` / `ssh`, yields the live domain, and tears it down (deleting
its overlay) on exit. `mode` (session/system) is per-test. Two waits carry a
required companion argument, enforced up front: `wait_for="panic"` needs
`console_log` (the panic-wait reads the serial console) and `wait_for="ssh"`
needs `ssh_hostfwd_port` — pass them or the call raises before any domain
boots. The gdbstub-preserve debug tests boot through a sibling harness in the
same module, `boot_preserved_gdbstub_domain(xml, *, uri, console_log)`, which
takes the caller's already-rendered production domain XML: the debug rendering
(`render_domain_xml(..., gdb_port=…, debug=…)`) is their subject under test, so
by ADR-0392 the caller keeps rendering it rather than the harness hiding it.

## Hard-won quirks

- **`qemu:///session` dodges the root-readback wall.** A `qemu:///system`
  domain writes a root-owned console log a non-root runner cannot read back;
  session mode runs qemu as the invoking user. This is why the self-hosted
  runner exports `KDIVE_LIBVIRT_URI=qemu:///session` for both boot families.
- **A long `XDG_CONFIG_HOME` breaks the session-mode QMP socket** (the per-domain
  monitor socket lives under it and hits a 108-byte path limit) — `XDG_RUNTIME_DIR`
  is *not* the lever. The harness redirects it to a short path automatically; the
  quirk bites only code that boots a session-mode domain without the harness.
- **Staged images need the right label under system mode** — `virt_image_t`
  (SELinux) or the `libvirt-qemu` AppArmor profile — and the rootfs's parent
  dir must be writable, because the boot stages an overlay beside it.
- **`pytest -m live_vm` selects all four families.** If you run a nightly for
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
- [ADR-0425 — the remote-libvirt `live_vm` family](../../adr/0425-remote-live-vm-tier.md)
- [ADR-0387 — self-hosted KVM runner host codification](../../adr/0387-selfhosted-kvm-runner-host-codification.md)
- [live-stack runbook](live-stack.md) · [self-hosted KVM runner](self-hosted-kvm-runner.md) · [POWER host bring-up](power-host-bringup.md) · [four-method live run](four-method-live-run.md)
