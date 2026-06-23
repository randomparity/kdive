# Spec — local-libvirt live drgn introspection (`introspect.run`)

- **Issue:** #677 (M2.8 Epic B, B3)
- **ADR:** [ADR-0219](../adr/0219-local-libvirt-live-drgn-introspection.md) (anchor; do not
  re-decide). Refines [ADR-0210](../adr/0210-local-libvirt-live-debug-introspection.md) (the
  local live-debug/introspection plane), [ADR-0039](../adr/0039-ssh-transport-live-introspection.md)
  and [ADR-0218](../adr/0218-local-libvirt-session-ssh-transport.md) (the drgn-live SSH transport
  this consumes), and [ADR-0033](../adr/0033-drgn-introspection-from-vmcore.md) /
  [ADR-0085](../adr/0085-drgn-live-transport-generalization.md) (the fixed-helper drgn contract).
- **Design doc:** [m2.8-local-libvirt-service-parity](../design/m2.8-local-libvirt-service-parity.md)
- **Status:** Accepted

## Problem

`LocalLibvirtLiveIntrospect.from_env()` builds the live-introspection port with its drgn seam
left `None`, so `introspect.run` against a local-libvirt session raises `MISSING_DEPENDENCY` off
the `live_vm` gate — before any transport IO. The local provider's runtime descriptor leaves
`supported_introspection = {"offline-vmcore"}` (no `live`), so capability-aware admission
(ADR-0209) rejects live introspection on local with `capability_unsupported` even on a System
that *was* provisioned for drgn-live.

The pieces this builds on already exist on `main`:

- **The drgn-live SSH transport** (#697, ADR-0218): `open_transport(system, "drgn-live")`
  resolves the recorded loopback-forwarded guest SSH port from the live domain XML, enforces
  loopback-before-IO, and returns an `ssh://127.0.0.1:<port>` `TransportHandle` the session row
  persists. `supported_debug_transports` already includes `drgn-live`.
- **The credential plumbing** (ADR-0039 §2): `debug.start_session` resolves the profile's
  `ssh_credential_ref` through the bound secret backend and registers the value into the
  redaction registry *before* `open_transport`, gated on `drgn_live_requires_credential` (local
  returns `True`). The build injects the kdive-managed public key to **`root`** (ADR-0052), so
  the live transport connects as `root@127.0.0.1` with the managed private key as identity.
- **The shared fixed-helper + redaction machinery** (ADR-0033): the three in-tree helpers
  (`tasks`, `modules`, `sysinfo`) and `assemble_report` (redact first — the single redaction
  boundary — then byte-cap), shared by every provider's introspectors.
- **The handler path** (`mcp/tools/debug/introspect.py`): `introspect.run(session_id, helper)`
  resolves a `live` drgn-live DebugSession, validates `helper` against the fixed set, and calls
  `LiveIntrospector.introspect_live(transport_handle=..., helper=...)` off the event loop.

The **only** missing piece is wiring `from_env()` to a real live seam that attaches drgn to the
running guest kernel over that SSH transport and runs one helper.

## Decision

Realize local live drgn introspection by **SSH-exec'ing an in-guest `kdive-drgn <helper>` helper
over the drgn-live SSH transport** and parsing its one-JSON-object section output host-side, then
redacting + byte-capping it through the shared `assemble_report`. This mirrors the proven
`RemoteLibvirtLiveIntrospect` path (which runs the identical in-guest helper over the
qemu-guest-agent) exactly, differing only in the channel (SSH vs guest agent). drgn never runs on
the worker for the live path; the worker only opens an SSH connection and parses JSON.

See [ADR-0219](../adr/0219-local-libvirt-live-drgn-introspection.md) for the mechanism rationale
(remote-exec vs tunnelling `/proc/kcore`), the descriptor-flip / maturity-`partial` split, and
the two named live-proof gaps.

### 1. Replace the `open_live_program` Program-model seam with an SSH-exec helper seam

The stub `LocalLibvirtLiveIntrospect` is built around an `open_live_program(handle) -> _Program`
seam plus `run_helper(program, name)` — the drgn-`Program`-on-worker model the offline port uses.
That model **cannot be honestly realized over SSH**: drgn has no native remote-`/proc/kcore`
reader, and tunnelling the live kernel's memory to the worker is rejected (ADR-0219). The
realization runs drgn *in the guest*, so the seam shape that matches reality is the remote
provider's: a single seam that runs one helper end-to-end over the transport and returns the
section dict.

Replace (not deprecate — CLAUDE.md "replace, don't deprecate") the two `None` seams
(`open_live_program`, `run_helper`) with one injected seam:

```python
type _RunLiveHelper = Callable[[str, str], dict[str, object]]  # (transport_handle, helper) -> section
```

`from_env()` wires the real `_real_run_live_helper`; a unit test injects a fake that returns a
canned section without touching SSH or drgn.

### 2. `introspect_live` orchestration

```
introspect_live(*, transport_handle, helper):
    if run_live_helper is None: raise MISSING_DEPENDENCY        # off-gate guard (both-seam parity)
    if helper not in {"tasks","modules","sysinfo"}: raise CONFIGURATION_ERROR   # before any IO
    section = run_live_helper(transport_handle, helper)         # SSH-exec kdive-drgn <helper>
    route section into tasks|modules|sysinfo by helper
    return assemble_report(tasks, modules, sysinfo, byte_cap, secret_registry)   # redact + cap
```

Helper validation happens **before** the seam runs (no SSH round-trip for a bad helper), matching
remote. The section is routed to its matching report field and the other two fields stay `{}`,
exactly as the offline port and remote live port do.

### 3. The real seam: SSH-exec `kdive-drgn <helper>`

`_real_run_live_helper(transport_handle, helper)` (a `# pragma: no cover - live_vm` seam):

1. Decode the handle (`TransportHandleData.decode`) and require `kind == "ssh"`, host loopback,
   port in range — a non-loopback or non-ssh handle is a `CONFIGURATION_ERROR` *before* any IO
   (defense-in-depth: the connect plane already enforced loopback, this re-checks at use).
2. Resolve the managed SSH identity for `root@127.0.0.1` via the existing
   `materialized_ssh_identity(ssh_credential_ref, secret_registry)` (0600 temp file, deleted on
   every exit; key value registered for redaction — the machinery `ssh_transport.py` already
   ships). The `ssh_credential_ref` is read from env/config the same way the connect plane reads
   it; the value never enters the handle, a state row, or a response.
3. Run `ssh -i <identity> <BatchMode/StrictHostKeyChecking=accept-new/ConnectTimeout> -p <port>
   root@127.0.0.1 -- /usr/local/sbin/kdive-drgn <helper>` with **fixed argv** (the helper name is
   validated against the fixed set before this runs — never a shell string, no caller interpolation
   into the remote command) and a bounded timeout.
4. A non-zero exit → `DEBUG_ATTACH_FAILURE` (drgn could not attach in-guest, e.g. no debuginfo);
   undecodable / non-object stdout → `INFRASTRUCTURE_FAILURE`; an SSH launch/connect fault →
   `TRANSPORT_FAILURE`; a timeout → `TRANSPORT_FAILURE`. These map onto the same categories the
   remote live path and the offline path already use.

The connectable surface (decode, loopback re-check, helper-validation, error mapping) is
unit-tested with an injected fake; only the real `subprocess` SSH call is `live_vm`-gated.

### 4. Flip `supported_introspection` to add `live`

`composition.py` adds `"live"` to `supported_introspection` (now
`frozenset({"offline-vmcore", "live"})`), so capability-aware admission (ADR-0209) admits an
`introspect.run` on a local drgn-live session and the seam runs end-to-end.

### 5. Tool maturity stays `partial`

`introspect.run` tool maturity stays `partial`. The descriptor advertises the *wired* capability;
the milestone live-verifier (B6 #680) promotes maturity only after a live KVM
`introspect.run` → drgn-attach → helper round-trip. This is the same descriptor-vs-maturity split
B2 (#676), B4 (#678), and the consumed transport ADR-0218 §6 used, and the conservative deviation
from ADR-0210 §1's literal "flip and promote in one PR" — see ADR-0219.

## Acceptance criteria

- **CI (fakes):**
  - `introspect.run` with a `None` live seam raises `MISSING_DEPENDENCY` before any IO (off-gate).
  - An unknown `helper` is a `CONFIGURATION_ERROR` raised before the seam runs (no SSH round-trip).
  - The selected helper's section is routed to its matching report field; the other two are `{}`.
  - The seam receives the resolved `transport_handle` (the SSH handle the session persisted) and
    the helper name; a fake seam proves the contract.
  - The assembled report is redacted at the port boundary (a planted guest secret is masked) and
    byte-capped (a tiny cap trims `tasks` and sets `truncated`).
  - A non-zero in-guest exit → `DEBUG_ATTACH_FAILURE`; undecodable output →
    `INFRASTRUCTURE_FAILURE`; an SSH transport fault → `TRANSPORT_FAILURE` (all via injected fakes).
  - `from_env()` wires a non-`None` real seam (without importing drgn or opening SSH); calling it
    on a host without the live prerequisites raises a categorized error, not an `ImportError`.
  - The local provider descriptor advertises `live` introspection (`composition.py` test).
  - `introspect.run` tool maturity is `partial` (descriptor/maturity drift-guard).
- **Live (KVM host, B6 #680, not this PR):** `introspect.run` on a live drgn-live local session
  attaches drgn in-guest over SSH and returns a redacted report; maturity promotes only then.

## Failure modes & edges

| Condition | Category | Where |
|---|---|---|
| live seam not configured (off-gate) | `MISSING_DEPENDENCY` | `introspect_live` guard |
| unknown helper | `CONFIGURATION_ERROR` | before seam runs |
| handle is not `ssh://` / non-loopback host / bad port | `CONFIGURATION_ERROR` | `_real_run_live_helper`, before IO |
| `ssh_credential_ref` unresolvable | `CONFIGURATION_ERROR` | `materialized_ssh_identity` |
| SSH connect/launch fault or timeout | `TRANSPORT_FAILURE` | seam |
| in-guest helper exits non-zero (drgn cannot attach) | `DEBUG_ATTACH_FAILURE` | seam |
| undecodable / non-object helper stdout | `INFRASTRUCTURE_FAILURE` | seam |
| guest SSH unreachable (the #697 DHCP gap) | `TRANSPORT_FAILURE` (honest fail, never false success) | seam |
| guest lacks `kdive-drgn` / `drgn` (known live-gap) | `DEBUG_ATTACH_FAILURE` (non-zero exit) | seam |

## Out of scope (named, not solved here)

- **Guest networking under direct-kernel boot** (#697 / ADR-0218): the guest may not auto-DHCP
  the SLIRP NIC, so `root@127.0.0.1:<port>` may not reach guest sshd until a one-line rootfs
  network-enable lands. This PR's code fails-fast with `TRANSPORT_FAILURE` (never a false
  success); B6 surfaces the real state.
- **In-guest `kdive-drgn` + `drgn` in the local rootfs.** The local rootfs build installs sshd +
  the managed key but not the `kdive-drgn` helper or `drgn` (remote's base image carries both).
  Until they land in `rootfs_build`, the live path returns `DEBUG_ATTACH_FAILURE` on a real guest.
  This is a named follow-up the B6 live drive surfaces, recorded in ADR-0219; not solved here
  because it is a rootfs-build change orthogonal to the worker-side wiring this issue scopes.
- **Maturity promotion** — owned by B6 (#680) after the live proof.
