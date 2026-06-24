# Live in-guest arbitrary drgn script introspection (#762)

- **Status:** Approved (design)
- **Date:** 2026-06-24
- **Issue:** [#762](https://github.com/randomparity/kdive/issues/762) (D4, part of epic #764)
- **ADR:** [ADR-0240](../../adr/0240-live-drgn-script-introspection.md)
- **Split from:** [#781](https://github.com/randomparity/kdive/issues/781) (the offline/captured-core half — make the raw vmcore + vmlinux fetchable and analyze locally)

## Problem

The drgn introspection surface offers only three fixed, parameterless helpers — `tasks`,
`modules`, `sysinfo` (`introspect.run` live, `introspect.from_vmcore` offline). There is no
way to read a named kernel global (`d_hash_shift`, `dentry_hashtable`), resolve a symbol, walk
a structure, or run any drgn expression an investigation needs. drgn's whole value is
programmatic Python introspection, and an agent is well-suited to author drgn scripts — the
fixed-helper enum kneecaps the capability.

ADR-0033 §2 dropped the v1 caller-supplied-`script` path explicitly as a **scope deferral, not
a security veto**: "The arbitrary-script path and its wrapper/cap/timeout machinery return with
the live introspection tier in M1, where that execution surface is designed as a whole." Every
subsequent live tier (ADR-0039, ADR-0085, ADR-0219) kept the fixed-helper enum and never
re-introduced the script path. #762 is where that deferred surface is finally designed.

## The online/offline distinction is fetchability, not security

The defining property of the two transports is not a trust boundary — it is whether the target
is a downloadable artifact:

- A **captured vmcore is a static file.** The principled answer for arbitrary offline analysis
  is to make the raw core + `vmlinux` **fetchable** (RBAC-gated by project membership) and let
  the agent run drgn/nm/gdb locally with unlimited power. That is a sensitivity/egress change,
  not a debug-surface change, and is tracked as **#781**. It is out of scope here.
- A **running kernel is not fetchable.** Its state is ephemeral and mutating, reachable only
  through the platform's live drgn-live transport into the guest. So live arbitrary-drgn
  introspection *must* be server-mediated. That is this spec.

Redaction-as-self-protection of a developer's own dump is not a control this spec relies on:
the moment an agent can run drgn against its own live kernel it can already read all of that
kernel's memory, so masking the same bytes would be theater. The controls this spec keeps are
the ones that are *not* about protecting an owner from their own data (see Safety envelope).

## Decision summary

Add an MCP tool `introspect.script(session_id, script, timeout_sec?)` that runs a
caller-supplied drgn script against the **live** guest kernel over the existing drgn-live
`DebugSession`, returning the script's stdout. The script executes **in the guest** (disposable
blast radius); the worker only opens the transport and relays bytes.

## Surface

```
introspect.script(
    session_id: str,           # an open `live` drgn-live DebugSession
    script: str,               # a drgn (Python) script; `prog` is the live drgn.Program
    timeout_sec: float = 30.0, # agent-chosen in-guest execution bound; see Timeouts
) -> ToolResponse
# data: { "output": <stdout, byte-capped>, "truncated": "true"|"false" }
```

- **Annotations:** `mutating`. drgn `-k` against a live kernel can *write* memory
  (`prog.write`, object assignment), so the tool is not advertised as read-only even though
  most scripts read.
- **RBAC:** `contributor` — the same role the rest of the live-debug surface (`debug.*`,
  `introspect.run`) requires. The capability is no more privileged than `debug.continue`, which
  already perturbs the live guest.
- **Capability admission:** a new `IntrospectionMode = "live-script"`. The tool is admitted only
  when the bound provider's descriptor advertises that mode (reuses the ADR-0209
  `_require_introspection` seam); a provider that omits it returns `capability_unsupported`
  (`configuration_error`). fault-inject omits it.
- **Output:** free-form stdout as `data["output"]`. The Run/tool envelope already advertises a
  free-form `data` outputSchema, so no committed schema snapshot changes. `truncated` is set
  when the byte cap trims the output.

## How the script reaches the guest (preserves both providers' security model)

`kdive-drgn` (the in-guest helper already baked into every base image / rootfs at image-build
time) gains exactly one new mode:

```
kdive-drgn run-script [timeout_sec]   # reads the drgn script from STDIN
```

- It reads the script from **stdin**, writes it to an in-guest `mktemp` file, and execs
  `timeout <timeout_sec> drgn -k -q <tmpfile>`. A `trap` removes the temp file on exit. The
  script is **never installed** and never persists between calls.
- Because the script travels over **stdin, never argv**, the remote path's single-program
  guest-agent allowlist (`GuestAgentExec(allowed_programs={"/usr/local/sbin/kdive-drgn"})`,
  `remote_libvirt/debug/introspect.py:271`) is **untouched**: argv stays the fixed, allowlisted
  `["/usr/local/sbin/kdive-drgn", "run-script", "<timeout>"]`. The untrusted bytes ride the
  agent's base64 `input-data` stdin channel.
- **Local** (drgn-live SSH, ADR-0218 §session-ssh / ADR-0219): SSH-exec `kdive-drgn run-script <timeout>`, script
  piped over SSH stdin.
- **Remote** (qemu-guest-agent, ADR-0083): the script is delivered over the guest-agent's
  `guest-exec` **`input-data`** (base64 stdin) field, over the same allowlisted program.

  **This is new scope, not an existing capability.** `GuestAgentExec.run(domain, argv)` today takes
  no stdin and `_spawn` builds `guest-exec` arguments as `{path, arg, capture-output}` with no
  `input-data` field (`remote_libvirt/guest/agent.py:208,242`). The plan must extend
  `GuestAgentExec` to pass `input-data` (and base64-encode the script). qemu-guest-agent caps both
  the inbound `input-data` and the captured `out-data` sizes; the script is therefore size-bounded
  before send (oversize script → `configuration_error`, not a silent agent rejection), and an agent
  `out-data` cap is treated as the same byte-cap `truncated` path as the worker-side cap — whichever
  binds first sets `truncated`.

## Execution model: stateless, repeatable within a boot

- The `DebugSession` persists across many calls in one boot cycle (exactly like `introspect.run`
  and the gdb-MI `debug.*` ops). An agent may call `introspect.script` any number of times.
- Each call is an **independent, fresh `drgn -k` process** against `/proc/kcore`. There is **no
  persistent drgn REPL and no Python state carried between calls.** An agent that needs
  continuity (variables, intermediate results) puts the whole computation in **one script**.
  This matches the existing one-shot-per-call helper model and avoids managing a long-lived
  in-guest interpreter.
- **Concurrency stance: unserialized, agent's responsibility.** Like the existing `introspect.run`
  live path (and unlike the gdb-MI `debug.*` ops, which take a per-session `asyncio.Lock` in
  `run_engine_op`), `introspect.script` calls are **not serialized per session** — each is an
  independent transport round-trip spawning its own in-guest `drgn -k`. Two concurrent calls on
  one session therefore run two drgn processes against `/proc/kcore` at once. Concurrent *reads*
  are safe; two concurrent *writing* scripts race against live kernel state with no ordering
  guarantee. The spec does not add per-session serialization (it would not bound cross-session
  concurrency anyway, and the blast radius is the agent's own guest); an agent that needs ordering
  serializes its own calls or combines them into one script.

## Timeouts

- `timeout_sec` is **agent-chosen**, defaulting to `30.0` when omitted. The platform does not
  impose an arbitrary fixed bound; the agent sizes the bound to its work.
- **Bounded both ends.** The effective value is clamped to `[floor, ceiling]` *before* it reaches
  the guest: a **floor of `1.0`** (a non-positive, blank, or non-finite `timeout_sec` is clamped
  up to the floor, never passed through) and the operator ceiling below. The floor matters because
  the in-guest wrapper is `timeout <n> drgn -k`, and GNU coreutils treats `timeout 0` as **no
  timeout at all** — so a `0`/negative value would silently delete the in-guest DoS guard, the one
  control this section exists to provide. The wrapper therefore always receives a positive bound.
- The clamped value drives the in-guest `timeout … drgn -k` wrapper (kills a runaway drgn in the
  guest).
- The worker-side transport timeout (SSH / guest-agent round-trip) is derived from the **clamped**
  value as `clamped_timeout_sec + transport_slack` (never the raw request) so it tracks the same
  bound the guest receives — an over-ceiling request cannot inflate the worker thread-pool wait
  past the ceiling. A legitimately long script is not severed by the channel timeout, while a
  *wedged* sshd/agent still releases the worker thread-pool slot.
- A wedged or runaway script never traps the agent: it can `debug.end_session` and, if needed,
  tear down / force the VM to recover. The disposable guest is the backstop.
- **Operator-configurable ceiling.** Because an unbounded agent-chosen timeout can squat a
  shared-worker thread-pool slot in a multi-tenant deployment, a deployment-config maximum
  (`KDIVE_LIVE_SCRIPT_MAX_TIMEOUT_SECONDS`, generous default `600`) clamps `timeout_sec`. This
  is a deployment *policy*, not a per-call limit; a single-tenant operator can set it
  effectively unbounded. A request over the ceiling is clamped to it (not rejected), with the
  effective value reflected in the in-guest bound.

## Safety envelope (no self-protection theater)

- **Real control — blast radius.** Arbitrary code runs in the **disposable guest VM**. Worst
  case it harms only a throwaway kernel-under-test the agent already controls.
- **Kept, not theater — platform-secret redaction.** The script output passes the
  secret-registry `Redactor` only to mask **platform secrets** (the kdive-managed SSH key, any
  registered secret), never to redact dump contents from their owner. The managed key is
  registered for redaction exactly as the fixed-helper path does today.
- **DoS / usability guards, not confidentiality.** The in-guest `timeout`, the worker-side
  transport timeout, the operator ceiling, and an **output byte cap** (`truncated` flag,
  reusing the 1 MiB `_REPORT_BYTE_CAP` order of magnitude) bound resource use and response size.
- The capability is **not** offered on the offline/worker path — running an arbitrary
  caller script on the shared, credentialed worker process is out of scope (that path is served
  by #781's fetch-and-analyze-locally model).

## Port + wiring

- `LiveIntrospector` (`providers/ports/retrieve.py`) gains:
  ```python
  class LiveScriptOutput(NamedTuple):
      output: str
      truncated: bool
  class LiveIntrospector(Protocol):
      def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput: ...
      def run_script(self, *, transport_handle: str, script: str,
                     timeout_sec: float) -> LiveScriptOutput: ...
  ```
- Realized in `LocalLibvirtLiveIntrospect` (SSH, ADR-0219) and `RemoteLibvirtLiveIntrospect`
  (guest-agent, ADR-0083). Both validate the handle (loopback-ssh / non-blank domain) exactly as
  `introspect_live` does, then exec `kdive-drgn run-script` with the script on stdin, byte-cap +
  platform-secret-redact the stdout, and return `LiveScriptOutput`.
- fault-inject does **not** advertise `live-script` in its descriptor and need not implement a
  real `run_script` (the descriptor gate rejects before the port is touched).
- `ProviderRuntime.supported_introspection` adds `"live-script"` for the two real providers.
- Tool registered in `mcp/tools/debug/introspect.py` alongside `introspect.run`; one descriptor
  mode addition; **no migration; no schema change.**

## Error contracts

The port and tool reuse the existing live-introspect taxonomy:

- malformed `session_id` / not a `live` drgn-live session → `configuration_error`.
- provider does not advertise `live-script` → `capability_unsupported` (`configuration_error`).
- transport unreachable / SSH launch fault / round-trip timeout → `transport_failure`.
- in-guest `drgn` exits non-zero (script error, drgn could not attach, in-guest `timeout` kill)
  → `debug_attach_failure`, with the in-guest stderr tail (platform-secret-redacted, byte-capped)
  in `data` so the agent can see *why* its script failed.
- undecodable / oversize agent reply → `infrastructure_failure`.
- off the `live_vm` gate (no real seam) → `missing_dependency` mapped to `debug_attach_failure`
  at the tool boundary, mirroring `introspect.run`.
- **a script that wedges or crashes the live guest mid-call** → surfaces as `transport_failure`
  (the channel drops / the agent stops answering) or `debug_attach_failure` (the in-guest `timeout`
  kills drgn), bounded by `timeout_sec + transport_slack`; it never hangs the worker thread past
  that bound. The call does **not** mutate Run or boot-outcome state — a `DebugSession` left
  unusable by the agent's own script is recovered by `debug.end_session` (and, if the guest is
  dead, teardown), exactly as a wedged gdb session is.

## Testing

- **CI-real (no host):** session gating (role, project, `live` state, drgn-live transport),
  descriptor admission for `live-script`, `timeout_sec` defaulting + ceiling clamp, the stdin
  argv construction, byte-cap + `truncated`, platform-secret redaction of output, handle
  validation (loopback-ssh / non-blank domain), and every error mapping above — all driven
  through injected fake seams, mirroring the existing `introspect.run` / `introspect_live`
  tests for both providers.
- **`live_vm`-gated:** the real SSH/agent + `drgn -k` exec stays behind `# pragma: no cover -
  live_vm`. A `live_vm` test boots a drgn-live-capable guest and runs a real script
  (e.g. read `init_uts_ns` release, resolve `&dentry_hashtable`) end-to-end through
  `introspect.script`, plus a timeout test (a `while True: pass` script returns
  `debug_attach_failure` within `timeout_sec + slack`).
- **Live proof:** executed on this KVM host (it runs live_vm directly), promoting the tool
  maturity from `partial` to `implemented`.

## Maturity

The tool ships `partial` (descriptor-gated, CI-real orchestration) and is promoted to
`implemented` once the `live_vm` proof passes on a prepared host, mirroring the ADR-0219
descriptor-vs-maturity split for the live introspect surface.

## Considered & rejected

- **Symbol-resolution-only / bounded reads.** Rejected per the issue's own intent and the user's
  direction: drgn's value is programmatic, agents author drgn well, and a bounded API kneecaps
  the capability ADR-0033 deferred whole. A symbol lookup is a one-line drgn script under this
  surface.
- **Arbitrary scripts on the offline/worker path too.** Rejected: the worker is shared,
  credentialed infrastructure; arbitrary caller code there is a cross-tenant RCE surface, and it
  would also have to punch a hole in the platform-secret boundary. Offline arbitrary analysis is
  served by #781 (fetch the static core and run drgn locally), which needs no worker execution.
- **A new program in the guest-agent allowlist.** Rejected: it would widen the remote
  single-program allowlist. Routing the script over stdin to the *existing* allowlisted
  `kdive-drgn` keeps the allowlist byte-for-byte unchanged.
- **A persistent in-guest drgn REPL for cross-call state.** Rejected: long-lived interpreter
  lifecycle/affinity/cleanup is complexity the one-shot model avoids; continuity lives in a
  single script.
- **A fixed platform timeout.** Rejected: the agent sizes its own work; the platform only
  supplies a default and an operator ceiling.
- **Extending `introspect.run` with an optional `script` arg.** Rejected: it is `read_only`,
  contributor, structured-`report` output; the script tool is `mutating`, free-form output, and a
  distinct capability mode. A separate tool keeps the annotations, schema, and admission honest.
