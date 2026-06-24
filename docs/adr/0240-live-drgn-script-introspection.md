# ADR 0240 — Live in-guest arbitrary drgn script introspection

- **Status:** Accepted
- **Date:** 2026-06-24
- **Issue:** [#762](https://github.com/randomparity/kdive/issues/762) (D4, epic #764)
- **Spec:** [`../superpowers/specs/2026-06-24-live-drgn-script-introspection-design.md`](../superpowers/specs/2026-06-24-live-drgn-script-introspection-design.md)
- **Depends on:** [ADR-0033](0033-drgn-introspection-from-vmcore.md) (the fixed-helper
  introspect surface this extends, and the deferral it discharges),
  [ADR-0218](0218-local-libvirt-session-ssh-transport.md) / [ADR-0219](0219-local-libvirt-live-drgn-introspection.md)
  (the local drgn-live SSH transport + in-guest `kdive-drgn`), [ADR-0083](0083-remote-connect-debug-plane.md)
  (the remote guest-agent exec + single-program allowlist), [ADR-0209](0209-capability-aware-mcp-admission.md)
  (the descriptor-mode admission seam this reuses).

## Context

The drgn introspection surface (`introspect.run` live, `introspect.from_vmcore` offline) runs
only three fixed, parameterless helpers — `tasks`, `modules`, `sysinfo`. There is no way to read
a named kernel global, resolve a symbol, or evaluate any drgn expression an investigation needs
(#762 / black-box Part 3 D4). drgn's value is programmatic Python introspection, which agents are
well-suited to author; the fixed enum kneecaps it.

ADR-0033 §2 dropped the v1 caller-supplied-`script` path as an explicit **scope deferral, not a
security veto**: the arbitrary-script surface "return[s] with the live introspection tier in M1,
where that execution surface is designed as a whole." Every live tier since kept the fixed enum
and never discharged that deferral. This ADR discharges it.

The defining property separating the two introspect transports is **fetchability, not security**:
a captured vmcore is a static file (the principled path for arbitrary offline analysis is to make
it fetchable and run drgn locally — tracked as **#781**), whereas a running kernel is ephemeral
and reachable only through the live transport, so live arbitrary-drgn introspection must be
server-mediated. This ADR covers only the live path.

Redaction of a developer's own dump from its owner is not a control relied on here: an agent able
to run drgn against its own live kernel can already read all of that kernel's memory. The controls
that matter are blast-radius containment, platform-secret redaction, and resource bounds — not
self-protection.

## Decision

We will add an MCP tool `introspect.script(session_id, script, timeout_sec?)` that runs a
caller-supplied drgn (Python) script against the **live** guest kernel of an open drgn-live
`DebugSession` and returns the script's stdout.

1. **In-guest execution, server-relayed.** The script runs inside the disposable guest VM. The
   in-guest `kdive-drgn` helper (already baked into every base image) gains one mode,
   `kdive-drgn run-script [timeout_sec]`, that reads the script from **stdin**, writes it to a
   `mktemp` file, and execs `timeout <timeout_sec> drgn -k -q <tmpfile>` (temp file removed on a
   `trap`). The script never reaches argv, so the remote single-program guest-agent allowlist
   (`{/usr/local/sbin/kdive-drgn}`) is unchanged — untrusted bytes ride the agent's `input-data`
   stdin / the SSH stdin. The script is never installed and never persists between calls.
   `GuestAgentExec` gains a `guest-exec` `input-data` path (it has no stdin today); the script is
   size-bounded before send to stay within the guest-agent's base64 input/output caps.
2. **Stateless, repeatable per boot.** The `DebugSession` persists across many calls; each call
   is a fresh `drgn -k` process with no carried-over Python state. Continuity lives in a single
   script.
3. **Agent-chosen timeout, clamped both ends.** `timeout_sec` defaults to `30.0` and is clamped to
   `[1.0, ceiling]` before reaching the guest — a floor of `1.0` (a `0`/negative/non-finite value
   is clamped up, never passed through, since coreutils `timeout 0` means *no* timeout and would
   delete the in-guest bound) and a deployment-config maximum
   (`KDIVE_LIVE_SCRIPT_MAX_TIMEOUT_SECONDS`, default `600`) as a multi-tenant DoS policy
   (single-tenant operators set it effectively unbounded). It drives the in-guest `timeout`; the
   server transport timeout is `clamped_timeout + slack`.
4. **`mutating`, `contributor`, descriptor-gated.** drgn `-k` can write live memory, so the tool
   is `mutating`. It requires `contributor` (the rest of the live-debug surface's role) and a new
   `IntrospectionMode = "live-script"` the provider descriptor must advertise (ADR-0209 admission;
   fault-inject omits it).
5. **Safety controls that are not self-protection.** Blast radius is the disposable guest. The
   output passes the secret-registry `Redactor` to mask **platform secrets only** (managed SSH
   key / registered secrets), and is bounded by an output byte cap (`truncated`) plus the
   timeouts — DoS/usability bounds, not dump-content redaction.
6. **Port + wiring.** `LiveIntrospector` gains `run_script(*, transport_handle, script,
   timeout_sec) -> LiveScriptOutput(output, truncated)`, realized in local-libvirt (SSH) and
   remote-libvirt (guest-agent). No migration; the free-form `data.output` invalidates no
   committed schema snapshot.

## Consequences

- An agent can run any drgn script against a live kernel — symbol resolution, struct walks,
  expression reads — without downloading vmlinux or shelling out, satisfying #762 for the live
  half. Symbol lookup becomes a one-line script.
- The arbitrary-script execution surface ADR-0033 deferred is finally specified, for the live
  transport only.
- The remote guest-agent allowlist is preserved byte-for-byte (stdin, not argv); no new
  allowlisted program, no widened exec surface.
- New obligations: the `kdive-drgn` reference helper and the baked base images must carry the
  `run-script` mode (a guest-image change operators re-bake, like any `kdive-drgn` update); a new
  config key; a new descriptor mode the two real providers advertise.
- The tool ships `implemented`, matching its `introspect.run` sibling. The `live_vm` proof ran on
  a local-libvirt KVM host (2026-06-24): a booted guest, a real `drgn -k` caller script over the
  drgn-live SSH transport returned `release=7.0.0-dirty init_comm=swapper/0 ntasks=137` (the
  guest's own warm-tree kernel, distinct from the host), and an over-cap script was rejected
  before send. Encoded as `test_spine_live_script_over_the_wire` (`live_vm`/`live_stack`-gated).
- The offline/captured-core half is **not** delivered here; it is #781 (fetchable raw
  vmcore/vmlinux). An operator wanting arbitrary offline analysis before #781 lands has no path —
  an accepted, documented gap.

## Alternatives considered

- **Symbol-resolution-only / bounded reads.** Loses the programmatic power that is drgn's point
  and that agents exploit well; a bounded API re-kneecaps the deferred capability. A symbol lookup
  is a trivial script under this surface.
- **Arbitrary scripts on the offline/server path too.** The server is shared, credentialed
  infrastructure; arbitrary caller code there is a cross-tenant RCE surface and would breach the
  platform-secret boundary. Offline arbitrary analysis is served by #781 (fetch the static core,
  run drgn locally) with no server-side execution.
- **A new allowlisted in-guest program.** Widens the remote single-program allowlist. Routing the
  script over stdin to the existing `kdive-drgn` keeps the allowlist unchanged.
- **A persistent in-guest drgn REPL for cross-call state.** Long-lived interpreter
  lifecycle/affinity/cleanup is complexity the one-shot model avoids; continuity lives in one
  script.
- **A fixed platform timeout.** The agent sizes its own work; the platform supplies only a default
  and an operator ceiling.
- **Extending `introspect.run` with an optional `script` arg.** It is `read_only`, structured
  output; the script tool is `mutating`, free-form output, a distinct admission mode. A separate
  tool keeps annotations, schema, and admission honest.
