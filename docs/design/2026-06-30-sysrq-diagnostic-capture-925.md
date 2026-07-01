# SysRq diagnostic capture for local-libvirt Systems (#925)

- **Issue:** #925
- **ADR:** [0285](../adr/0285-sysrq-diagnostic-capture.md)
- **Status:** Draft
- **Date:** 2026-06-30

## Problem

A ready local-libvirt System exposes no way to ask the running kernel for a live
diagnostic dump — blocked tasks, held locks, per-CPU registers, memory usage, task
state. Today the only guest-injection path is `control.force_crash`, which panics the
guest via NMI (destructive, admin-gated). Investigators need the *non-destructive*
counterpart: trigger a magic-SysRq diagnostic and capture what the kernel prints to the
serial console, without crashing the guest.

## Requirements (issue acceptance criteria)

1. An authorized caller can trigger allowlisted diagnostic SysRq commands on a ready
   local-libvirt System.
2. Console output produced by the command is captured as redacted artifacts with bounded
   inline snippets.
3. Destructive SysRq commands are rejected unless already represented by an existing
   destructive tool (i.e. crash → `control.force_crash`).
4. Unsupported guest/profile state returns `configuration_error` with remediation.
5. Tests cover: allowed diagnostic command, disallowed destructive command, output
   capture, timeout/no-output, and authorization denial.

## Non-goals

- No interactive console (reaffirmed by ADR-0280); this is one-shot inject-and-capture.
- No new console *write*/reproducer path (ADR-0280 redirects that to in-guest exec).
- Remote-libvirt and other providers are out of scope; the tool is local-libvirt only.
- No loglevel/reboot/poweroff/sync/remount SysRq keys — those are destructive or
  non-diagnostic and are not in the allowlist.

## Design

### Shape: a synchronous tool that enqueues a worker job (mirrors `control.force_crash`)

Provider Control-plane ports are only ever called from **worker job handlers** (via
`asyncio.to_thread`, under the per-System advisory lock), and the capture step blocks for
a bounded settle window while the guest prints to the console. Both facts rule out a
synchronous server-side call. The tool therefore admits synchronously (validate → gate →
enqueue) and returns a `{job_id, status: queued}` envelope, exactly like `control.power`
and `control.force_crash`. Worker-owned execution injects the keystroke, captures the
console delta, and stores the artifact.

### Tool: `control.diagnostic_sysrq`

Placed in the `control.*` toolset next to `control.force_crash` (its destructive sibling)
for discoverability. Annotated `mutating` (not `read_only` — it injects a keystroke; not
`destructive` — it changes no state and the guest keeps running).

Parameters:

- `system_id` (str) — the ready local-libvirt System to inspect.
- `command` (str) — the diagnostic command, a friendly enum value (below). An unknown
  value is a `configuration_error` listing the allowed commands.
- `idempotency_key` (str | None) — replay-safe key; a repeated key returns the prior
  envelope. Absent, every call is a fresh capture (a diagnostic dump is inherently not
  idempotent).

On success the returned job's `refs.result` is the **redacted console artifact id**; the
caller reads the bounded inline snippet with `artifacts.get` (VIEWER, existing 24 KiB
token-safe window — no new snippet-bounding code).

### Allowlist (friendly name → SysRq trigger)

A single source of truth `SysRqCommand` StrEnum in
`src/kdive/domain/operations/sysrq.py`, each mapped to its magic-SysRq trigger character:

| `command` value           | SysRq | Kernel effect                          |
|---------------------------|-------|----------------------------------------|
| `show_task_states`        | `t`   | Dump state of all tasks                |
| `show_blocked_tasks`      | `w`   | Dump tasks in uninterruptible (D) sleep|
| `show_memory`             | `m`   | Dump memory info                       |
| `show_locks`              | `d`   | Dump all locks held                    |
| `show_registers`          | `p`   | Dump current-CPU registers             |
| `show_backtrace_all_cpus` | `l`   | Backtrace all active CPUs              |
| `show_timers`             | `q`   | Dump hrtimers / clock event devices    |

Every entry is read-only from the kernel's perspective (a printk dump). Destructive keys
(`c` crash, `b` reboot, `o` poweroff, `s` sync, `u` remount-ro, `e`/`i` signal-all,
`f` OOM-kill, `k` SAK) are simply absent from the enum, so they are **structurally
unexpressible** through this tool. Requirement 3 is met by construction: a caller who
wants the crash path uses `control.force_crash`; a caller who passes `crash`/`c`/`reboot`
gets a `configuration_error` whose remediation names `control.force_crash`.

### Injection mechanism (local-libvirt)

A new Control-port method `diagnostic_sysrq(domain_name, trigger)` sends the magic-SysRq
key combination to the guest's emulated keyboard, exactly as `virsh send-key <dom>
--codeset linux KEY_LEFTALT KEY_SYSRQ KEY_<X>` does:

```
domain.sendKey(libvirt.VIR_KEYCODE_SET_LINUX, HOLDTIME_MS,
               [KEY_LEFTALT, KEY_SYSRQ, KEY_<trigger>], 3, 0)
```

The Linux input keycodes for the trigger characters live in the local-libvirt provider
(`KEY_T=20, KEY_W=17, KEY_M=50, KEY_D=32, KEY_P=25, KEY_L=38, KEY_Q=16`, plus
`KEY_LEFTALT=56, KEY_SYSRQ=99`). `sendKey` delivers to the QEMU PS/2 keyboard (the `i8042`
controller is part of the emulated `pc`/`q35` chipset, so it is present even though the
domain declares no `<graphics>`/`<input>` device — see `lifecycle/xml.py`). This mirrors
`force_crash`'s use of a single libvirt domain method and is unit-tested with a fake
connection; the real `sendKey` adapter is `live_vm`-only.

`remote_libvirt`'s `Controller` gains the method for Protocol conformance but raises
`CONTROL_FAILURE` (`not_supported`) — the tool never routes a non-local System to it
(gated below), so this is a defensive stub, not a second implementation.

### Guest prerequisites (a load-bearing, kdive-uncontrolled dependency)

The keyboard-SysRq path reaches the running kernel only if the **guest kernel** binds a
PS/2 keyboard driver (`CONFIG_SERIO_I8042` + `CONFIG_KEYBOARD_ATKBD`) *and* magic SysRq is
enabled for the requested command (`CONFIG_MAGIC_SYSRQ`, runtime `kernel.sysrq` bitmask).
kdive boots **user-supplied** kernels, and a minimal serial+virtio-only config can omit the
PS/2 keyboard stack; a hardened image can ship `kernel.sysrq=0` or a restrictive mask. When
any of these is unmet the keystroke is silently dropped and the capture yields no output.
This is treated as a first-class outcome, not swept under "should work":

- The **no-output** path (below) returns `configuration_error` with remediation naming
  **both** causes: enable `kernel.sysrq` for the command *and* build the guest kernel with a
  PS/2 keyboard driver.
- Acceptance is **gated on a `live_vm` proof** (this dev host runs KVM/libvirt) against a
  representative built kernel + a default catalog rootfs, confirming an allowlisted command
  produces a captured dump on the shipped configuration. A fake-connection unit test cannot
  falsify the end-to-end mechanism, so the live proof is required, not optional.
- The default catalog images' `kernel.sysrq` value is verified as part of that proof; if the
  defaults do not enable it, the supported-configuration constraint is documented in the
  operator docs and the tool's remediation, rather than left implicit.

The `/proc/sysrq-trigger`-over-SSH alternative avoids the guest-keyboard dependency but is
**not** a free substitute: it needs a prior `authorize_ssh_key` on the System and working
guest DHCP/networking (open #697; #782's live SSH e2e is deferred), whereas keyboard-sendKey
needs no credential and works on any ready System. ADR-0285 records this trade-off.

### Capture (worker handler)

The capture is a **redacted console tail from shortly before injection to the settle
point** — it is not a perfectly isolated command transcript. The guest is still running, so
the tail may interleave unrelated console lines; the artifact is labelled and documented as
"console tail after a SysRq injection," not "exactly the command's bytes."

**Lock scoping.** `advisory_xact_lock` is transaction-scoped, so the handler must **not**
hold it across the multi-second settle poll — that would keep a Postgres transaction
idle-in-transaction and serialize every other per-System op (teardown, `force_crash`,
`power`, `console_rotate`) behind the capture. The lock is taken in **two brief
transactions** with the lock-free poll between them; correctness allows this because the
local console log only grows while the System is READY (`append="off"` truncates only on
power-cycle), so the tail read needs no cross-op exclusion:

1. **Under the lock (tx 1):** verify the System is live, local-libvirt, and READY; resolve
   the domain name; read the console length before injection (`mark`) — the local serial log
   is the whole current-boot file (`read_console_log`, ADR-0258). Commit.
2. **Lock-free:** inject the SysRq via the Control port (`asyncio.to_thread`).
3. **Lock-free:** poll the console for growth with a bounded, count-driven loop (no
   wall-clock, so it is
   deterministic under test): up to `MAX_POLLS` iterations of `POLL_INTERVAL`, breaking
   early once the log has grown past `mark` and then stabilized for `SETTLE_POLLS`
   consecutive reads. The loop records whether it exited by **stabilization** or by hitting
   the **iteration bound** (a large bursty dump — `show_task_states`/`show_memory` on a busy
   guest — that is still growing at the bound is disclosed, below, so a truncated capture is
   detectable, never silently trusted). `MAX_POLLS`/`POLL_INTERVAL`/`SETTLE_POLLS` are named
   constants sized so the total bound comfortably exceeds a normal printk burst; the live
   proof validates them against a real dump.
4. `overlap_start = max(0, mark - SEAM_OVERLAP)`; capture `raw = console_bytes[overlap_start:]`.
   Redacting a small pre-injection overlap (mirroring `console_rotate`'s seam-overlap carry)
   keeps a secret that straddles the `mark` byte boundary contiguous, so it cannot be split
   and leak its tail. The artifact therefore begins a few KiB before injection — harmless,
   redacted context.
5. **No captured growth** (`len(console_bytes) <= mark`) → the job fails with
   `configuration_error`, `reason="no_console_output"`, remediation naming **both** causes:
   enable `kernel.sysrq` for the command, and build the guest kernel with a PS/2 keyboard
   driver (see Guest prerequisites). This is requirement 5's timeout/no-output path.
6. **Lock-free:** redact `raw` through `Redactor(registry=secret_registry)` (same pattern as
   `console_rotate` / `read_redacted_console`).
7. **Under the lock (tx 2):** re-verify the System is still live+READY. If it left that state
   during the poll (a concurrent `force_crash`/`teardown`, or a power-cycle that truncated the
   log), fail with `configuration_error` `reason="system_changed_state"` rather than mislabel
   it `no_console_output` — the outcome is not a sysrq/keyboard problem. Otherwise store the
   redacted bytes as a System-owned object `sysrq-diagnostic-<job_id>` (`owner_kind="systems"`,
   `tenant="local"`, `sensitivity=REDACTED`, `retention_class="console"`), register the row
   **insert-if-absent on the object key** (mirroring `console_rotate._existing_part_row` —
   jobs are at-least-once, so a worker retry re-runs the handler; the re-injection is a
   harmless second dump, but the row insert must not duplicate — `artifacts.object_key` has no
   unique constraint), stamp `run_id=None` (see below), and return `str(artifact.id)` as the
   job's `result_ref`.

The capture-poll core is a pure async function taking injected `read_console`, `inject`,
and `sleep` callables so tests drive it with scripted console reads and a no-op sleep — no
real time, no real libvirt. It returns the captured bytes and the exit reason
(stabilized / hit-bound) for the handler to store and record.

### Observability

The worker records a `diagnostic_sysrq` capture-outcome counter tagged by outcome
(`captured` / `no_output` / `control_failure`) and `provider_kind`, so a deployment where
the mechanism is silently failing (guest lacks the keyboard driver, `kernel.sysrq` disabled)
is visible as a rising `no_output` rate rather than an invisible dead feature. Because the
default `provider_kind` tagging already exists on worker capture paths, this reuses the
established pattern.

### Why `run_id = None`

Console-evidence artifacts are Run-correlated (ADR-0279) so `runs.get`'s
`console_artifacts` manifest can list them. A SysRq dump is not boot/console evidence; it
is reached only through the job's `refs.result`. Leaving `run_id` NULL keeps the
`console_artifacts` manifest meaning "console evidence" and avoids polluting it with
diagnostic dumps.

### Teardown reclaim (no artifact leak)

System-owned artifacts are reclaimed only at teardown (via an object-key `LIKE`); no gc
expiry sweep touches `owner_kind='systems'`. So `teardown_handler` is extended to also
delete `sysrq-diagnostic-*` objects+rows for the System, alongside the existing
`console-part-*` reclaim. Without this the dumps would leak past teardown.

### Authorization and preconditions (tool, fail-fast)

Minimum role **CONTRIBUTOR** — this is a non-destructive investigation action, and
CONTRIBUTOR already covers the debug/introspect/post-mortem loop. No destructive-op gate
(that is `force_crash`'s ADMIN + profile opt-in path). `require_role` denial is audited and
returned as `authorization_denied` by the existing denial middleware.

Ordered precondition checks (each a fail-fast `configuration_error`, cross-project details
suppressed exactly as `force_crash` does):

1. Malformed `system_id` → `configuration_error`.
2. System absent or not in caller's projects → `configuration_error`.
3. `require_role(ctx, project, CONTRIBUTOR)` (→ `authorization_denied` on under-reach).
4. Unknown/destructive `command` → `configuration_error` listing allowed commands (crash
   intent → remediation names `control.force_crash`).
5. Provider kind ≠ local-libvirt → `configuration_error`, remediation "diagnostic SysRq is
   supported only on local-libvirt Systems".
6. System state ≠ READY → `configuration_error` with `current_status`.

### Persistence: new job kind (migration 0055)

`JobKind.DIAGNOSTIC_SYSRQ = "diagnostic_sysrq"` and a `SysRqPayload(system_id, command)`.
`jobs.kind` is guarded by the `jobs_kind_check` CHECK constraint, so migration
`0055_diagnostic_sysrq_job_kind.sql` widens it (drop-and-recreate, constraint name stable,
forward-only per ADR-0015). `DIAGNOSTIC_SYSRQ` is **not** added to
`DESTRUCTIVE_JOB_KINDS`.

## Error taxonomy summary

| Condition                              | Category               | Where   |
|----------------------------------------|------------------------|---------|
| Malformed id / not visible             | `configuration_error`  | tool    |
| Under-privileged caller                | `authorization_denied` | tool    |
| Unknown or destructive command         | `configuration_error`  | tool    |
| Non-local-libvirt / not READY          | `configuration_error`  | tool    |
| No console output within the bound     | `configuration_error`  | worker  |
| System left live+READY during capture  | `configuration_error`  | worker (`reason=system_changed_state`) |
| Console log unreadable (non-root wall) | `configuration_error`  | worker (existing `read_console_log`) |
| Absent domain / libvirt fault          | `control_failure`      | worker (Control port) |

## Testing

- **Tool**: allowed command enqueues a `diagnostic_sysrq` job (assert dedup key + payload);
  unknown/destructive command → `configuration_error`; non-local / not-READY →
  `configuration_error`; under-privileged ctx → `authorization_denied`.
- **Capture core**: scripted `read_console` returning growth → returns the delta with exit
  reason `stabilized`; no growth → empty; growth-then-stable early-exit; still-growing at the
  iteration bound → returns exit reason `hit_bound` (truncation disclosed); a secret
  straddling the `mark` boundary is redacted because the `SEAM_OVERLAP` pre-injection region
  is included before redaction.
- **Worker handler**: fake Control records the injected trigger; fake console reader +
  no-op sleep; asserts a redacted System-owned artifact is written and `result_ref` is its
  id; no-output → job `configuration_error`; a registered secret in the delta is redacted; a
  **replayed handler run** (at-least-once) does not create a duplicate artifact row
  (insert-if-absent).
- **Provider**: fake libvirt domain records the `sendKey` codeset + keycodes for each
  command; absent domain → `control_failure`.
- **Teardown**: a System with a `sysrq-diagnostic-*` artifact is reclaimed (object + row)
  at teardown.
- **`live_vm` proof (required, not optional)**: on the KVM/libvirt dev host, provision a
  System on a built kernel + default catalog rootfs, call `control.diagnostic_sysrq` with an
  allowlisted command, and assert the job succeeds with a non-empty redacted artifact — this
  is the only test that falsifies the guest-keyboard/`kernel.sysrq` end-to-end mechanism.
  Records the default images' `kernel.sysrq` value; if unset, documents the supported-kernel
  constraint.
- Agent-facing surface guards (tool registry snapshot, agent-index/toolset docs) updated so
  the existing drift guards stay green.

## Rollback

Pure addition. Reverting removes the tool, handler, port method, payload, job kind, and the
migration's forward-only CHECK widening (a subsequent forward migration would re-narrow it).
No data backfill; existing Systems are unaffected.
