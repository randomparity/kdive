# ADR 0427 — Capability gates, not identity gates, in the control plane

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-23
- **Deciders:** kdive maintainers
- **Issue:** #1426 (part of #1423)

## Context

Three control-plane tools admit only local-libvirt Systems, but they decide this by asking the
bound provider's *identity* rather than its *capability*. Five sites gate on
`binding.kind is not ResourceKind.LOCAL_LIBVIRT` and reject with `reason="not_local_libvirt"`:

| Site | Tool | Layer |
|---|---|---|
| `jobs/handlers/control/capture_traffic.py` | `control.capture_traffic` | handler |
| `jobs/handlers/control/diagnostic_sysrq.py` | `control.diagnostic_sysrq` | handler |
| `jobs/handlers/control/watch_for_crash.py` | `control.watch_for_crash` | handler |
| `mcp/tools/lifecycle/control/registrar.py` (×2) | `diagnostic_sysrq`, `watch_for_crash` | tool |

An identity gate is the wrong shape: a future provider could implement the underlying port and
still be unreachable through the tool surface, because the gate never asks whether the port
exists. `capture_traffic` already shows the correct shape — its *tool* layer gates on
`runtime.support.supports_traffic_capture` and returns `capability_unsupported` (ADR-0209), while
its *handler* layer still carries a redundant identity check sitting two lines above an
already-correct `traffic_capturer is None` port-presence backstop. The contrasting good example is
`systems.snapshot`: the tool gates on `support.supports_snapshots`, the handler keeps a
defence-in-depth `runtime.snapshot is None` check, and a remote `Snapshotter` becomes reachable the
moment it is wired — no gate change (ADR-0378).

`diagnostic_sysrq` and `watch_for_crash` have no capability flag at all today, so the identity
check is their only gate. `ProviderSupport` (ADR-0208) carries `supports_snapshots` and
`supports_traffic_capture`; it lacks flags for these two planes.

This is a refactor: it changes no provider behavior on its own. Blocks the remote-provider entries
in #1423 — none of these tools can reach a second provider until the gates become capability-based.

## Decision

**Replace every identity gate in the control plane with a capability gate, and introduce the two
missing `ProviderSupport` flags the flagless tools need.**

1. **`ProviderSupport` gains two fail-closed flags** — `supports_diagnostic_sysrq: bool = False`
   and `supports_crash_watch: bool = False` — mirroring the existing `supports_snapshots` /
   `supports_traffic_capture` convention. An unconfigured or partially-wired provider advertises
   nothing. Local-libvirt sets both `True` in its composition, so its behavior is unchanged.

2. **The two tool-layer gates** (`diagnostic_sysrq_system`, `watch_for_crash_system`) check the
   new flag and return `capability_unsupported` (ADR-0209) with the capability token
   (`diagnostic_sysrq` / `crash_watch`), the bound provider name, and an empty `supported` set —
   exactly the `capture_traffic` tool-layer template.

3. **The two flagless handlers** raise a `configuration_error` with a capability-shaped
   machine-readable `reason` (`diagnostic_sysrq_unsupported` / `crash_watch_unsupported`, keeping
   `provider_kind` in `details`) when the flag is unset. Neither plane has a dedicated provider
   port to null-check — the Controller is always present and crash-watch reads the console log
   directly — so the capability flag is the sole handler-layer gate for these two.

4. **The `capture_traffic` handler's identity check is deleted outright.** The `traffic_capturer is
   None` port-presence check below it is the defence-in-depth backstop (mirroring
   `systems.snapshot`), and the tool layer already gates on `supports_traffic_capture`. The removed
   check was pure redundancy that would have rejected a second provider that wires a
   `TrafficCapturer`.

5. **Agent-facing wrapper docstrings, `Field` descriptions, and the served `toolsets-control`
   resource** state the capability requirement rather than naming local-libvirt (per the AGENTS.md
   rule that the wrapper docstring is the agent-facing contract), keeping "today local-libvirt" as
   descriptive context, not a hard requirement.

Every rejection keeps a machine-readable reason in `details`/`data` so an agent can act on it; the
new reasons are capability-shaped, not identity-shaped. A provider with a flag unset receives
`capability_unsupported` / a `configuration_error`, unchanged in category from today.

## Consequences

- **Remote providers become reachable by wiring, not by editing a gate.** The moment a provider
  advertises `supports_diagnostic_sysrq` / `supports_crash_watch` (and wires the underlying
  capability), the tool surface admits it — the property the three tools lacked and #1423 needs.
- **`ResourceKind` is no longer imported** by the three handlers or the registrar; the domain
  identity enum leaves the control-plane admission path entirely.
- **The wire contract's `reason` values change** on the refused path: `not_local_libvirt` becomes
  `capability_unsupported` (tool layer) or `diagnostic_sysrq_unsupported` /
  `crash_watch_unsupported` (handler layer). These are pre-enqueue/pre-execution refusals with no
  persisted state, so no migration and no compatibility shim — a caller keys on the new,
  capability-shaped reason. This is intentional: the epic's whole point is that the refusal reason
  should describe a missing capability, not a provider identity.
- **`ProviderSupport`'s two new flags default False**, so `remote-libvirt` and `fault-inject`
  advertise neither and keep refusing these tools exactly as before — the refactor is
  behavior-preserving across every provider.
- **No behavior change to `systems.get`.** The two new flags are read only at the admission gates;
  they are not (yet) surfaced on the system read envelope. Surfacing them for discovery, as
  `supports_snapshots` / `supports_traffic_capture` are, is a small follow-up left out of this
  refactor to keep it gate-only.

## Alternatives considered

- **Keep the identity gates.** Rejected — the epic's premise: an identity gate makes a
  port-implementing provider unreachable through the tool surface, and it forks the two layers of
  `capture_traffic` (tool = capability, handler = identity) so they can disagree.
- **Reuse one generic `supports_control_diagnostics` flag for both new planes.** Rejected — SysRq
  injection (a Controller call) and console crash-watch (a serial-log read) are independent
  capabilities a provider can support separately; one flag would couple them and force a provider
  that has only one to lie about the other.
- **Surface the two new flags on `systems.get` in the same change.** Deferred — it is additive read
  surface, not part of the gate refactor, and pulls in the `systems` view and its content docs. Out
  of scope here; a clean follow-up.
