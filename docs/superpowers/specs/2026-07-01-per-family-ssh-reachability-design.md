# Per-family live SSH reachability proof (#956)

- Status: Draft
- ADR: [ADR-0294](../../adr/0294-per-family-ssh-reachability-proof.md)
- Issue: [#956](https://github.com/randomparity/kdive/issues/956)

## Problem

Issue #956 was filed (from `BLACK_BOX_REVIEW.md` Finding 1) claiming
`debian-kdive-ready-*` guests are SSH-unreachable because the debian family disabled
cloud-init and staged no NIC-up config, while a follow-up comment reported the rhel
family failing identically (banner-exchange timeout) even with its NetworkManager
DHCP keyfile present.

**The debian defect was fixed by #962; the rhel/EL9 defect was not, and a new
per-family live test proved it still reproduces at HEAD.** PR#964 (ADR-0288, #962),
merged after both #956 comments, routed both families' first-boot through
`cloud_init_first_boot_args(ctx)` (baked NoCloud seed + `99-kdive.cfg` `dhcp4` drop-in;
`kdive-ready` ordered `After=network-online.target`) and was live-proven on **debian** —
but never live-proved rhel. Building the per-family test (below) and running it on a KVM
host showed `rocky-kdive-ready-9` still fails `transport_failure` on a from-scratch
current-HEAD image.

The guest serial console shows why: the rhel/EL9 guest never reaches userspace —
`ld.so` aborts PID 1 with `Fatal glibc error: CPU does not support x86-64-v2`, with the
CPU reported as `x86_64-v1 (QEMU Virtual CPU version 2.5+)`. Root cause:
`render_domain_xml` (`local_libvirt/lifecycle/xml.py`) emitted **no `<cpu>` element**, so
libvirt/QEMU defaulted to `qemu64` (x86-64-v1). EL9/RHEL-family glibc requires
x86-64-v2, so init is killed before any NIC or sshd exists — the always-rendered forward
TCP-connects but gets no banner (the #956 symptom). Debian 13's baseline is x86-64-v1, so
it booted regardless, masking the defect. Confirmed by a controlled boot of the same
image: default CPU → glibc panic; `-cpu host` → `systemd[1]` boots.

So there are two residuals: **(a) a real rhel/EL9 provider defect** (the guest CPU model
does not meet EL9's baseline), and **(b) no automated per-family test** proving the
forward reaches a guest sshd — the assumption that let the rhel regression ship unnoticed.

## Goal

Fix the rhel/EL9 defect by pinning a v2-capable guest CPU
(`<cpu mode='host-passthrough'/>`) in the local-libvirt domain XML, and add a gated
`live_stack` test that proves the reachability contract **per family** (debian and rhel)
so a re-breakage is caught. Also clear two documentation artifacts left stale by #962. Do
**not** promote the `ssh_reachable` capability signal (deferred — see Non-goals).

## Success criteria (falsifiable)

0. `render_domain_xml` emits `<cpu mode='host-passthrough'/>` (unit-asserted), and the
   per-family live test's **rhel** parameter drains `authorize_ssh_key` to *succeeded* on
   a current-recipe `rocky-kdive-ready-9` — i.e. the EL9 guest boots past its glibc
   x86-64-v2 check and answers sshd, where before it panicked at init.
1. A `live_stack`-marked test provisions a `*-kdive-ready` image per family, waits for
   `ready`, and asserts:
   - `systems.ssh_info` returns a `worker_loopback` endpoint (host+port, status `ok`),
     **and**
   - `systems.authorize_ssh_key` drains to a **succeeded** job (the worker SSHes into
     the guest over the managed key and appends the agent key — this only succeeds if
     the guest NIC leased and sshd answers).
   The test releases the allocation on exit (success or failure).
2. The test **skips cleanly** (never fails, never errors) when its per-family ready
   image, the kernel tree (`KDIVE_KERNEL_SRC`), the stack, the issuer, or the database
   URL is absent, mirroring the existing `_spine_preflight` idiom. It does not un-gate or
   widen any existing marker, and it does **not** require the drgn-live secret
   (`ssh_credential_ref` is unset).
3. The test is parametrized over `{debian, rhel}` so each family is proven
   independently; a family with no configured image skips only that parameter.
4. The debian family customizer comment near `debian.py:110` no longer claims
   "cloud-init's cloud-ifupdown-helper DHCPs the NIC" (false on two counts post-#962);
   it states the cloud.cfg `dhcp4`/NoCloud reality.
5. `PLANNED_SIGNALS`' `ssh_reachable` entry no longer claims "sshd/keygen liveness is
   broken"; its rationale reflects that reachability now works and the open question is
   static-signal-vs-runtime-probe, and its `tracking_issue` points at the new
   follow-up issue (not #956, which this change closes).
6. A follow-up GitHub issue exists capturing "surface `ssh_reachable` on
   `systems.get`/`ssh_info`", stating the static-vs-runtime design fork and referencing
   #956.
7. `just lint`, `just type`, `just test` are green (the new test is deselected by
   `just test`; the CI-runnable guards over the customizer comment and the signal
   rationale, if any, pass).

## Design

### The test lives in the local live-stack spine module

`tests/integration/test_live_stack.py` already drives allocate → provision → ready over
the live MCP HTTP transport with the shared spine helpers (`allocate`, `provision_to_ready`,
`scalar`, `ok`, `drain_job`, `await_system_state`). The reachability test reuses those
helpers rather than a new harness.

The provision profile is deliberately minimal: `boot_method: direct-kernel` with a
`kernel_source_ref` (the baseline kernel the System boots to `ready` on), and **no**
`ssh_credential_ref` and no `force_crash`. Post-ADR-0281 (#937) the loopback forward +
virtio NIC render on **every** domain (`render_domain_xml` always appends them;
`provisioning.py` allocates `ssh_port` unconditionally) — the forward is plumbing, no
longer gated on `ssh_credential_ref`, which now controls only the drgn-live
introspection credential. So the test must **not** set `ssh_credential_ref`: it buys
nothing for reachability and would import the drgn-live secret-seeding skip gate
(`_require_drgn_ssh_secret`), which would skip the test on hosts that have the per-family
images but have not seeded the drgn secret — the very hosts the test targets.

The test needs no build/install/boot: `systems.provision` alone brings the System to
`ready` on its baseline kernel (ADR-0272), and the forward renders at provision. So the
test is short: allocate → provision → ready → ssh_info → authorize_ssh_key(drain) →
release.

### Per-family image selection

The existing spine reads one `KDIVE_GUEST_IMAGE`. Proving reachability *per family*
needs a debian ready image and a rhel ready image, which are distinct artifacts. Add two
env vars read only by this test:

- `KDIVE_GUEST_IMAGE_DEBIAN` — path to a `debian-kdive-ready-*` qcow2.
- `KDIVE_GUEST_IMAGE_RHEL` — path to a rhel-family (`rocky`/`centos`/`fedora`)
  `*-kdive-ready-*` qcow2.

The test is `pytest.mark.parametrize`d over `("debian", <env>)` / `("rhel", <env>)`.
Each parameter's preflight skips only that parameter when its image env var is
unset/missing. It also reuses the existing spine skip conditions — `KDIVE_KERNEL_SRC`
(the direct-kernel `kernel_source_ref` the profile validator makes **required**), the
stack URL, the issuer, and the database URL — so a host that lacks the kernel tree skips
cleanly rather than erroring at provision-time with a `CONFIGURATION_ERROR`. This keeps a
single-family host able to prove the family it has without failing on the family it
lacks. The two image env vars are registered in the config/env reference (the repo has an
env-doc guard, `scripts/check_env_documented.py`).

### The authorize_ssh_key success is the reachability assertion

`systems.authorize_ssh_key` enqueues a worker job that SSHes into the guest over the
per-System managed key and appends the agent's public key. A drained **succeeded** job
is end-to-end proof that: the NIC leased, the loopback forward bridged, and sshd
answered — i.e. the exact contract #956 says is unproven. `ssh_info` returning an
endpoint alone is not sufficient (it reads recorded XML, ADR-0281), so the test asserts
both, and `authorize_ssh_key` is the load-bearing one.

The agent public key is a throwaway ed25519 public key generated in-test (only the
public half; KDIVE never needs the private half for the append). Validation
(`validate_authorized_public_key`) requires a well-formed key.

On a **non-succeeded** `authorize_ssh_key` drain the test raises with the family
parameter id and the job's `error_category` / `failure_detail` in the message (matching
the spine's phase-named `SpinePhaseError` pattern), so a per-family reachability
regression names which family failed and surfaces the guest-side detail (e.g.
`failure_detail_exit_status 255`) without a re-run. `ssh_info` returning no endpoint on a
`ready` System is likewise a named failure, not a skip.

### Housekeeping (docs only, no behavior change)

- `debian.py:110` comment: replace the cloud-ifupdown-helper claim with the cloud.cfg
  `dhcp4`/NoCloud reality (matches ADR-0288). No code change on that line.
- `capability_signals.py` `PLANNED_SIGNALS` `ssh_reachable`: repoint `tracking_issue`
  to the follow-up issue and rewrite the rationale.

### Follow-up issue (B, deferred)

File a fresh issue: "surface `ssh_reachable` on `systems.get`/`ssh_info`", stating the
fork — the issue text asks for a **runtime** probe on `ssh_info` (a live TCP/banner
check with its own failure modes), while the existing `PlannedSignal` is the **static**
image-capability layer (computed over build provenance, `images.describe`). Leave the
choice to the maintainer; reference #956.

## Non-goals

- Promoting `ssh_reachable` to a registered signal or adding any runtime probe (deferred
  to the follow-up issue).
- Any change to the cloud-init / family networking implementation (no current defect).
- A `live_vm`-only (non-stack) reachability test: reachability is a spine-level,
  over-the-wire property (provision + worker job), so it belongs in `live_stack`.

## Considered & rejected

- **One `KDIVE_GUEST_IMAGE` reused for both families.** Rejected: it cannot prove *per
  family*; a host would prove whichever single family its image happens to be and the
  issue's "per family, not assumed" ask would be unmet.
- **Assert reachability via a raw host-side `ssh` banner probe in-test.** Rejected:
  duplicates the worker's SSH path with a second, differently-configured client; the
  `authorize_ssh_key` job is the product's own reachability path and asserting it proves
  the contract agents actually use.
- **Add a build → install → boot before the reachability check.** Rejected as
  unnecessary: the forward renders at provision and the baseline kernel boots to `ready`
  (ADR-0272); adding the build spine only lengthens the test without touching the
  reachability property.
- **Promote `ssh_reachable` here.** Rejected per orchestrator scope: the static-vs-runtime
  design is unsettled and belongs in its own issue.
- **Set `ssh_credential_ref` on the profile (as the drgn-live spine test does).** Rejected:
  post-ADR-0281 the forward + NIC render on every provision regardless, so it buys nothing
  for reachability, and it would import the drgn-live secret-seeding skip gate
  (`_require_drgn_ssh_secret`), skipping the test on hosts that have the images but no
  seeded drgn secret — under-proving the very contract the test exists to prove.
