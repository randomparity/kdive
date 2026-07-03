# ADR 0303 — Layer the `check_ssh_reachable` verdict so `reachable=false` names the failing layer

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** KDIVE maintainers

## Context

`systems.check_ssh_reachable` (ADR-0298, #972) returns a compact JSON verdict whose
top-level `detail` is a fixed three-value vocabulary produced by the probe in
`src/kdive/jobs/handlers/ssh_reachable.py`:

- `reachable` — a `SSH-` banner arrived;
- `unreachable` — no TCP connection was ever accepted before the deadline;
- `no SSH banner` — a connection was accepted but no `SSH-` line arrived.

That already usefully splits "nothing accepted the connection" from "connected but sshd
was silent". But #1011 observes that a `reachable=false` verdict does not name **which
layer** failed in a machine-readable field: an agent reading `{"reachable":false,…,
"detail":"no SSH banner"}` has to know that `no SSH banner` implies "TCP connected, banner
did not" rather than seeing that stated structurally. Live evidence in the issue: stopping
sshd inside an otherwise-healthy `ready` guest produced `"detail":"no SSH banner"` — correct
but coarse.

The probe is deliberately a bounded, no-auth banner read (ADR-0298 §6): it opens one TCP
connection and reads at most one banner line, sending nothing. The only two facts it observes
at probe time are therefore **did a TCP connection get accepted** and **did an `SSH-` banner
arrive** — and those two observations are exactly what the existing `detail` value already
encodes. `detail` maps one-to-one onto the lowest failing layer.

## Decision

Add two **additive** fields to the verdict, derived from the existing `detail` at
serialization time — no new probe I/O, no change to what the probe measures, no change to the
`detail` vocabulary or the `refs.result` inline-JSON shape.

1. **Two ordered probe layers, lowest → highest.** `tcp_connect` (a TCP connection to the
   recorded loopback SSH forward was accepted) then `ssh_banner` (the server sent an `SSH-`
   identification string). These are the only two layers the banner-only probe can observe;
   see "Considered & rejected" for why "forward bound" is not a separately-nameable layer.

2. **`layer` — the lowest failing layer, or `null` when reachable.** Derived from `detail`
   via a fixed, explicit map: `reachable → null`, `unreachable → "tcp_connect"`,
   `no SSH banner → "ssh_banner"`. `detail` remains the single source of truth; `layer` is a
   structured projection of it.

3. **`checks` — the ordered pass/fail breakdown up to and including the first failure.** A
   list of `{"layer": name, "ok": bool}` in layer order. When reachable, every layer is
   `ok:true`. When a layer fails, that layer is `ok:false` and no higher (un-evaluated) layer
   is listed — the probe short-circuits, so reporting an un-reached layer would be a claim the
   probe never tested.

The verdict keys are appended after `detail`, so the existing prefix
(`{"reachable",…,"detail"}`) is byte-for-byte unchanged and any reader keyed on the existing
fields is unaffected.

Example verdicts:

```json
{"reachable":true,…,"detail":"reachable","layer":null,
 "checks":[{"layer":"tcp_connect","ok":true},{"layer":"ssh_banner","ok":true}]}

{"reachable":false,…,"detail":"no SSH banner","layer":"ssh_banner",
 "checks":[{"layer":"tcp_connect","ok":true},{"layer":"ssh_banner","ok":false}]}

{"reachable":false,…,"detail":"unreachable","layer":"tcp_connect",
 "checks":[{"layer":"tcp_connect","ok":false}]}
```

A drift guard test asserts every `detail` value the probe can emit is present in the map, so a
future fourth `detail` value cannot ship without also naming its layer.

No schema, migration, RBAC, `ErrorCategory`, config, or provider change. The wrapper docstring
(the agent-visible surface) gains the two field names; `just docs` regenerates the reference.

## Consequences

- A `reachable=false` verdict now names the lowest failing layer structurally: an agent can
  branch on `layer == "tcp_connect"` (forward/NIC path down — nothing accepted the connection)
  vs `layer == "ssh_banner"` (connection accepted, sshd not speaking SSH) without parsing the
  human-readable `detail` string.
- `detail` stays the single source of truth; `layer`/`checks` are a pure function of it, so the
  three cannot disagree and there is nothing new for the probe to measure or get wrong.
- Because the fields are appended and `detail` is untouched, the ADR-0298 `refs.result` contract
  is preserved; existing callers and the serialization golden-string test continue to hold with
  only the additive keys added.

## Considered & rejected

- **Make `layer` the source of truth on `ReachResult` and derive `detail` from it.** The
  "more correct" direction (probe owns the layer enum; `detail` is presentation). Rejected as a
  larger, back-compat-riskier refactor for no observable gain: `detail` is already a closed,
  probe-set vocabulary that encodes the layer exactly, so a second field would be redundant
  state that must be kept in agreement. Deriving the projection from the existing single field
  is smaller and cannot drift out of sync. The drift-guard test covers the one failure mode
  (a new `detail` value with no mapped layer).

- **Add a separate `forward` layer ("is the loopback SSH forward bound?").** The issue lists
  "forward bound?" as a candidate. Rejected because it is not *cheaply and separately*
  observable from `tcp_connect` in the banner-only probe: with QEMU user-mode hostfwd the
  host-side port is bound whenever the VM is running, so a pure TCP connect cannot distinguish
  "forward not bound" from "forward bound, guest refused" — both surface as a failed connect.
  Separating them would require querying libvirt/QEMU hostfwd state, which is not a probe-time
  read. The honest, probe-observable layering is the two layers above; `tcp_connect` failing is
  the composite "forward + guest-accept" path being down.

- **Complete the SSH handshake to distinguish "sshd up but rejecting auth" from "sshd up".**
  Rejected as out of scope and contrary to ADR-0298 §6: the probe sends nothing and does no
  auth. A healthy sshd always emits its banner before any auth exchange, so `ssh_banner` passing
  already means "sshd is up and speaking SSH"; authentication outcome is a different question
  (`authorize_ssh_key`'s), not reachability.

- **A dedicated non-JSON reader tool for the layer breakdown.** Rejected for the same reason
  ADR-0298 rejected a dedicated `reachability_result` reader: the `jobs.wait → refs.result`
  inline-verdict contract already carries the whole verdict; adding fields to it is the minimal
  surface.
