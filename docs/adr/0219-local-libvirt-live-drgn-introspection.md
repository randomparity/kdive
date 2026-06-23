# ADR 0219 — Local-libvirt live drgn introspection over the drgn-live SSH transport

- **Status:** Accepted
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers
- **Issue:** [#677](https://github.com/randomparity/kdive/issues/677) (M2.8 Epic B, B3)
- **Refines:** [ADR-0210](0210-local-libvirt-live-debug-introspection.md) (the local live-debug /
  introspection plane; this is its B3 realization, the live half ADR-0210 §2 deferred),
  [ADR-0039](0039-ssh-transport-live-introspection.md) and
  [ADR-0218](0218-local-libvirt-session-ssh-transport.md) (the drgn-live SSH transport this
  consumes), [ADR-0033](0033-drgn-introspection-from-vmcore.md) and
  [ADR-0085](0085-drgn-live-transport-generalization.md) (the fixed-helper drgn contract and the
  `drgn-live` agent token).
- **Builds on:** [ADR-0079](0079-remote-live-debug-transport.md) (the in-guest `kdive-drgn`
  fixed-argv helper contract the remote provider already runs; local reuses it over SSH),
  [ADR-0052](0052-bootable-rootfs-image-builder.md) (the in-guest sshd + managed authorized key
  the local rootfs build installs), [ADR-0208](0208-provider-capability-descriptor.md) /
  [ADR-0209](0209-capability-aware-mcp-admission.md) (the descriptor flip + capability-aware
  admission), [ADR-0027](0027-safety-modules-secret-backend-impl.md) (secret-by-reference +
  redaction).
- **Spec:** [`docs/specs/2026-06-23-local-libvirt-live-drgn-introspection.md`](../specs/2026-06-23-local-libvirt-live-drgn-introspection.md).

## Context

B1 (#675, ADR-0210 §1) and #697 (ADR-0218) wired local-libvirt's **drgn-live SSH transport**:
`open_transport(system, "drgn-live")` resolves the recorded loopback-forwarded guest SSH port
from the live domain XML, enforces loopback-before-IO, and returns an `ssh://127.0.0.1:<port>`
`TransportHandle` the session row persists. `supported_debug_transports` already includes
`drgn-live`. B2 (#676, ADR-0210 §2) wired the offline introspection port and flipped
`supported_introspection` to `{"offline-vmcore"}`.

The live introspection port — `LocalLibvirtLiveIntrospect` — is still a stub: `from_env()` leaves
its drgn seam `None`, so `introspect.run` against a local session raises `MISSING_DEPENDENCY` off
the `live_vm` gate, and `supported_introspection` lacks `live`, so capability-aware admission
(ADR-0209) rejects live introspection on local even for a System provisioned for drgn-live.

Everything around the seam already exists and is unit-tested: the handler resolves a `live`
drgn-live DebugSession to its persisted `transport_handle`, validates the `helper` against the
fixed three (`tasks`/`modules`/`sysinfo`), calls `introspect_live` off the event loop, and returns
the **already-redacted** report; the shared `assemble_report` is the single redaction boundary; the
credential plumbing resolves and redaction-registers `ssh_credential_ref` before `open_transport`;
the managed key is injected to `root`, so the transport target is `root@127.0.0.1`. The remote
provider already runs the identical in-guest `kdive-drgn <helper>` fixed-argv helper over its
guest-agent channel and feeds the section JSON straight into `assemble_report`
(ADR-0079/0085, `RemoteLibvirtLiveIntrospect`).

## Decision

Realize local live drgn introspection by **SSH-exec'ing the in-guest `kdive-drgn <helper>` helper
over the drgn-live SSH transport** and parsing its one-JSON-object section output host-side, then
redacting + byte-capping it through the shared `assemble_report`. drgn runs **in the guest**
against its own live `/proc/kcore`; the worker only opens an SSH connection and parses JSON. This
mirrors `RemoteLibvirtLiveIntrospect` exactly, differing only in the channel (SSH vs guest agent),
so there is one in-guest helper contract and one report-assembly boundary, not two.

### 1. SSH-exec the in-guest helper (not a worker-side kcore tunnel)

The realization runs `kdive-drgn <helper>` in the guest over SSH with **fixed argv** — the helper
name is validated against the fixed set worker-side *before* the SSH round-trip, so no
caller-controlled string is interpolated into the remote command. The helper opens drgn against
the live kernel (`/proc/kcore` + the running kernel's debuginfo) in-guest and prints the section
JSON the shared `helper_tasks`/`helper_modules`/`helper_sysinfo` producers define; the worker
passes that section straight to `assemble_report`.

### 2. Replace the `open_live_program` Program-model seam with one SSH-exec helper seam

The stub `LocalLibvirtLiveIntrospect` carried an `open_live_program(handle) -> _Program` seam plus
`run_helper(program, name)` — the drgn-`Program`-on-worker model the *offline* port uses (it stages
a core file and opens it locally). That model cannot be honestly realized over SSH (see rejected
alternatives), so per CLAUDE.md "replace, don't deprecate" the two `None` seams are **replaced**
by one injected seam `run_live_helper(transport_handle, helper) -> dict[str, object]`. `from_env()`
wires the real `_real_run_live_helper`; unit tests inject a fake returning a canned section. The
off-gate guard, helper validation, section routing, redaction, and byte-cap orchestration are
unchanged in shape from the remote live port.

### 3. The real seam decodes the handle, materializes the managed identity, runs ssh

`_real_run_live_helper` (`# pragma: no cover - live_vm`):

1. `TransportHandleData.decode(handle)`; require `kind == "ssh"`, a loopback-literal host, and a
   valid port — a non-ssh/non-loopback handle is a `CONFIGURATION_ERROR` **before any IO**
   (defense-in-depth: the connect plane already enforced loopback at open time; the seam
   re-enforces at use time so a tampered/forged handle cannot redirect the SSH connection off
   loopback).
2. Materialize the managed SSH identity via the existing `materialized_ssh_identity(
   ssh_credential_ref, secret_registry)` (0600 temp file deleted on every exit; key value
   registered for redaction). The `ssh_credential_ref` resolves from config exactly as the connect
   plane resolves it; the key value never enters the handle, a row, or a response.
3. Run `ssh -i <identity> -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o
   ConnectTimeout=... -p <port> root@127.0.0.1 -- /usr/local/sbin/kdive-drgn <helper>` with fixed
   argv and a bounded timeout.
4. Error mapping: SSH launch/connect fault or timeout → `TRANSPORT_FAILURE`; non-zero helper exit
   → `DEBUG_ATTACH_FAILURE`; undecodable / non-object stdout → `INFRASTRUCTURE_FAILURE`. Identical
   categories to the remote live path and the offline path.

### 4. Descriptor flip is live-gated; tool maturity stays `partial`

`composition.py` adds `"live"` to `supported_introspection` so capability-aware admission admits
an `introspect.run` on a local drgn-live session and the seam runs end-to-end. **`introspect.run`
tool maturity stays `partial`** (per ADR-0208 invariant 5: maturity asserts the plane works *on
hardware*; CI proves only the fake-seam contract — no KVM, no booted guest, no real sshd, no
in-guest drgn). The milestone live-verifier (B6 #680) promotes maturity only after a live
`introspect.run` → drgn-attach → helper round-trip on the KVM host. This is the same
descriptor-vs-maturity split B2 (#676), B4 (#678), and ADR-0218 §6 used.

This deviates from ADR-0210 §1's literal "flip the descriptor **and** promote maturity in the same
PR" wording in the conservative direction only: ADR-0210 assumed the wiring PR would carry the live
drive, but this milestone separated the live proof into B6, so the wiring PR flips the descriptor
(the capability is genuinely wired) while B6 owns the maturity promotion. The honesty invariant
ADR-0208 protects — never advertise a stubbed plane as working — holds: `partial` is the honest
signal that the live proof is outstanding.

## Consequences

- `introspect.run` on a `drgn-live`-provisioned local session runs in production: the handler
  resolves the session's SSH handle, the seam materializes the managed identity, SSH-execs the
  in-guest helper, and returns a redacted, byte-bounded report. This is the last wiring piece of
  M2.8 Epic B.
- drgn never runs on the worker for the live path; the in-guest helper carries the
  kernel-version/debuginfo coupling, exactly as remote. A worker without drgn is fine.
- The single in-guest `kdive-drgn` helper contract is now shared by both libvirt providers; the
  fixed-argv + section-JSON shape is one thing to keep correct, not two.
- Credential material never touches the handle, a state row, or a response; the identity file is
  0600 and deleted on every exit, and the key value is redaction-registered (ADR-0027/0039 §2).
- No port, schema, or migration change — the seam satisfies the existing `LiveIntrospector` port;
  the change is the seam realization + the descriptor flip.

### Named live-proof gaps (surfaced by B6 #680, not solved here)

- **Guest networking under direct-kernel boot** (#697 / ADR-0218): the guest may not auto-DHCP the
  SLIRP NIC, so `root@127.0.0.1:<port>` may not reach guest sshd until a one-line rootfs
  network-enable lands. The seam fails-fast with `TRANSPORT_FAILURE` (never a false success); B6
  surfaces the real state.
- **In-guest `kdive-drgn` + `drgn` in the local rootfs.** The local `rootfs_build` installs sshd +
  the managed key (ADR-0052) but **not** the `kdive-drgn` helper or the `drgn` package the helper
  needs (remote's ansible-built base image carries both, ADR-0079 §5). Until they land in
  `rootfs_build`, the live path returns `DEBUG_ATTACH_FAILURE` on a real guest (the SSH connects,
  the helper is absent / drgn cannot attach). This is a rootfs-build follow-up orthogonal to the
  worker-side wiring this issue scopes; B6 surfaces it on the live drive. Recorded here so the gap
  is not silent.

## Considered & rejected

- **Tunnel the guest's `/proc/kcore` to the worker and open drgn locally.** Rejected: drgn has no
  native remote-`/proc/kcore` reader; tunnelling a live kernel's memory over SSH (e.g. SSHFS /
  block-streaming `/proc/kcore`) is gigabytes of fragile, slow, racy live-memory IO for a read drgn
  is purpose-built to do in-process against the local kernel. Running drgn in the guest (where the
  kernel and its debuginfo already are) is what the remote provider already does and is the honest,
  proven shape.
- **Keep the `open_live_program(handle) -> _Program` Program-model seam and "open drgn over SSH".**
  Rejected: there is no honest implementation — see above. Retaining the seam shape would force a
  fake-only contract that can never be realized, exactly the phantom-plane dishonesty ADR-0208
  forbids. Replacing it with the SSH-exec-helper seam makes the unit-tested contract match the
  live realization.
- **A new in-guest channel / helper distinct from remote's `kdive-drgn`.** Rejected: the remote
  provider already pins the fixed-argv `kdive-drgn <helper>` → section-JSON contract (ADR-0079/0085);
  reusing it keeps one in-guest helper and one report shape. The only provider difference is the
  transport channel.
- **Promote `introspect.run` maturity to `implemented` in this PR.** Rejected: CI proves only the
  fake-seam contract; no KVM, no booted guest, no real in-guest drgn. Promoting now would be a
  phantom claim. B6 owns the promotion after the live proof (ADR-0208 invariant 5).
- **Solve the guest-DHCP / in-guest-helper gaps in this PR.** Rejected as out of scope: both are
  rootfs-build / live-environment concerns the B6 live drive must surface against real hardware;
  this issue scopes the worker-side wiring. The seam fails-fast honestly when either gap bites.
