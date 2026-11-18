# 0825 — `faultinject.inspect` reads guest debugfs through an optional local-libvirt-only port

## Status

Accepted (2026-11-18)

Builds on [ADR-0822](0822-guest-exec-allowlist-adds-kdive-faultinject.md), which added the
reviewed in-guest program, and on [ADR-0219](0219-local-libvirt-live-drgn-introspection.md) for the
loopback SSH exec transport. Amends neither.

## Context

#3683 adds the read-only MCP tool `faultinject.inspect`. The only SSH-exec path today is
`LocalLibvirtLiveIntrospect`, which is hard-wired to `kdive-drgn` and keyed by a live drgn-live
DebugSession. Fault-injection state belongs to a System, not to a debug session, so the tool needs
a System-keyed path. Two further choices have viable alternatives: where the seam lives, and how
much of the helper's output reaches the agent.

## Decision

- **An optional `fault_injection` port on `ProviderRuntime`**, `FaultInjectionInspector`
  (`providers/ports/fault_injection.py`), with one synchronous method
  `read_state(*, transport_handle, key_path) -> dict[str, FaultKnobState]`. Only local-libvirt
  wires it. Every other provider leaves it `None` and the tool returns the existing
  `capability_unsupported` configuration error. No `ProviderSupport` flag is added, so the
  capability/port parity check is untouched (as for `domain_presence`).
- **The tool runs server-side, keyed by System.** It authorizes the System's project at viewer,
  requires state `ready`, resolves the loopback SSH endpoint exactly as `systems.ssh_info` does
  (including an authority-reported forward), loads the per-System bootstrap key, and runs the
  port off the event loop with the pooled connection already released.
- **The SSH argv builder takes the guest program as a parameter.** `_live_ssh_argv` keeps
  `kdive-drgn` as its default; the new module passes `/usr/local/sbin/kdive-faultinject` and the
  fixed argument `read`. Caller text never reaches argv.
- **Output is parsed, not forwarded.** The helper's stdout is capped at 4096 bytes, passed through
  the redactor, and accepted only if it is the exact `{"knobs":{...}}` shape with the two
  allowlisted knobs, the closed parameter names for each, and integer values. The tool re-emits
  the validated integers plus a derived `armed` flag per knob; guest stderr is never returned.
- **Error mapping.** The fixed argv is `read` with no knob, which the helper answers with exit 0
  and `{"present":false}` for a knob the kernel lacks (exit 4 is only reachable with a named
  knob). So a reply with both knobs absent is `configuration_error`
  `reason=fault_injection_unsupported`; one absent knob is a successful read. Helper exit 3 is
  `debugfs_not_mounted`, exit 127 is `helper_not_installed`, and exit 4 is also mapped to
  `fault_injection_unsupported` as a guard. Each carries a remediation sentence. SSH exit 255 is
  `transport_failure`; any other exit, an over-cap reply or an invalid reply is
  `infrastructure_failure`. Error `data` carries only a closed `reason` and the exit status, never
  ssh stderr or guest text.
- **Own timeout.** The read uses a 15 s per-attempt timeout and a 10 s retry deadline, not the
  drgn-live 60 s and 20 s, so a stuck guest holds a thread for at most about 25 s.

## Consequences

- #3684 extends the same port with mutating methods and the same result shape; the field names
  (`knobs.<knob>.present`, the helper's parameter names including `min-order`, and `armed`) are the
  readback contract it reuses.
- A caller at viewer can read root debugfs state through the System's bootstrap key. The read is
  one fixed argv with no caller text, and the state is not secret; set and reset stay out of
  scope.
- `read_state` is synchronous and blocks a thread for at most the retry deadline plus one attempt
  (about 25 s); concurrent calls are bounded by the thread pool, not by a lock. The 4096-byte
  cap is applied after `subprocess` has buffered the guest stdout, as the drgn-live path does;
  the guest is the operator-owned debug target.
- While a knob is armed the guest may fail the SSH round trip itself (`transport_failure` or
  `infrastructure_failure`); #3684's reset is the recovery, and the wrapper docstring says so.
- A guest image built before #3694 has no helper; the tool reports `helper_not_installed`
  rather than failing opaquely.

## Considered & rejected

- **Extend the `LiveIntrospector` port with a fault-injection method.** judgment: it is
  session-keyed and every provider adapter would have to implement or refuse a method that
  has nothing to do with drgn.
- **Add a `ProviderSupport.supports_fault_injection` flag with parity.** verified: the charter
  of #3683 excludes a new `ProviderSupport` flag (WORK:SCOPE exclusions, 2026-11-18), and
  `rg supports_ src/kdive/mcp` shows no reader a second flag would serve.
- **Enqueue a worker job like `systems.check_ssh_reachable`.** judgment: the read is one bounded
  round trip of a few integers; a job adds queueing, a poll and a dedup key for no benefit.
- **Run through a debug session (`introspect.script`).** verified: `introspect.script` is
  `_docmeta.mutating()` and needs an open drgn-live session and contributor
  (`mcp/tools/debug/introspection/registrar.py`), and ADR-0822 rejected the same route for
  writes; it executes caller Python.
- **Return the helper's JSON verbatim.** judgment: forwarding guest-controlled text makes the
  agent contract depend on the guest; a validated integer shape keeps the redactor and the byte
  bound meaningful.
