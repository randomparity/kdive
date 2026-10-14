# Local GDB debug tool cells over HTTP

## Problem and authority

Issue #2813 and epic #2803 require proof that the GDB command surface works on a
real matching kernel through the public HTTP surface, beyond a direct-driver
success or a session that merely attaches. Current main proves only
`debug.start_session` → `debug.read_registers` → `debug.end_session` over HTTP
(`tests/integration/test_live_stack.py`), and breakpoints, stepping and
watchpoints only in-process (`tests/mcp/debug/test_debug_gdbmi_live_smoke.py`).
Coverage group 2813 has 1760 generated cells, all unbound and all owned by
#2813.

The frozen charter is the issue's `WORK:SCOPE` with token `q2813-4031f349`
(campaign 73d4edacff-8cf13f64-8877-4610-beaf-496193501043), which reuses the
operator's 2026-10-07 approval of the exclusions unchanged. Operator decision
R2 (2026-10-13) landed in PR #3211: a capability cell follows the provider's
support verdict. Local-libvirt supports `gdbstub` and `drgn-live`, so the
group's x86_64 local cells have no capability cell. Operator decision R3
(2026-10-13) amends ADR-0756 for five filtering list tools; this PR lands it.
No product source or public contract changes here.

## Contract changes

- `contract.py` routes group 2813's ppc64le cells to #2818 and its
  remote-libvirt cells to #3113, beside the existing routing. The 440 x86_64
  local-libvirt cells stay owner 2813: 88 `functional` (21 tools,
  `debug.start_session` as `gdbstub` and `drgn-live`; four configuration ×
  exposure each) and 352 `rejection`.
- `FILTERED_LIST_BOUNDARIES` gains the `authorization` and `project-isolation`
  pairs of `runs.list`, `systems.list`, `jobs.list`, `allocations.list` and
  `debug.list_sessions`: exactly the pairs the ADR-0756 amendment names, since
  the ADR makes the contract set the single owner of the exception. Eight of
  the new pairs belong to unbound cells (#3097, #3098), so only their
  observation text moves.
- The 440 cells are bound in `[implementations]` to the new carrier's one
  test function. A local scenario id names no architecture, so the 440 local
  ppc64le cells (owner #2818) share the node, as #2814's and #2815's do; on a
  ppc64le host the carrier stops each as `blocked` ("#2818's lane").

The matrix digest moves; earlier qualifications against it fail
`matrix-mismatch`, which is the intended behaviour.

## ADR-0756 amendment (R3)

A dated amendment names ten more pairs: `authorization` and
`project-isolation` of the five list tools. Each answers a denied caller with
an empty page rather than a failure: the SQL scopes rows to the caller's
viewer projects (`viewer_projects`, `projects_with_role`). Four of them filter a
`project` argument naming another project rather than rejecting it; `jobs.list` takes no
`project` argument. The decision's four-step evidence
sequence applies unchanged. Two precise differences from the original pairs
are recorded:

- The canonical empty page differs per tool: object id `runs`, `systems`,
  `jobs`, `allocations` or `debug_sessions`, its fixed next actions, and
  `data` `{truncated: false, next_cursor: null, count: 0}`.
- `allocations.list` filters its next actions by the caller's roles, and with
  a `project` argument echoes it in `data.project`; its canonical page is the
  one its denied caller's grants produce, which its carrier states.

The harness's `_empty_page` gains only the `debug.list_sessions` page here;
#3097 and #3098 add theirs when they bind their cells. The named-pair check
in `_observe` already fails closed for a named pair without a control.

## Approach and alternatives

Chosen: one scenario module, `tests/integration/live_stack/debug_cells.py`,
and one carrier, `tests/integration/test_debug_tool_cells_live.py`, on the
merged tool-cell frame, following #2814's `capture_cells.py`. The functional
frame is `on_lane_system` with a `provision` wrapper that adds
`debug.gdbstub = true` (and nothing else, so the System's capture method
resolves to `gdbstub` and the boot carries `nokaslr`); `deep_body` then boots
the verified `longterm` fixture kernel, uploads its `vmlinux` (the Run's
debuginfo) and loads `loop`.

Rejected:

- Extend `test_live_stack.py`'s spine. One shared flow with a stack-wide
  token cannot record per-cell evidence.
- Share one booted System and session across cells. Each record would depend
  on the order and failure of others, and a halted guest from one cell is the
  next cell's precondition.
- Compare against the debugger's own output only (`resolve_symbol` then
  `read_registers`). The obligations need independent ground truth, so every
  address comes from the guest (`/proc/kallsyms`, `/sys/module`) captured
  before the attach halts it, cross-checked with the fixture's `System.map`.

## Functional preparation

Each functional cell owns one System in a fresh project:

1. The frame provisions the lane image (`fedora-kdive-ready-44`) with
   `debug.gdbstub = true`.
2. `deep_body` uploads, installs and boots the fixture kernel, proves its
   build identity and a loaded `loop` module, and returns the Run id.
3. Before any attach, the body reads ground truth over SSH as root:
   `/proc/version`, the `T`/`t` lines of `/proc/kallsyms` for the named
   symbols and their successors, every `[loop]` text symbol, and
   `/sys/module/loop/sections/.text`. A zero address (pointer restriction)
   fails the cell. `vfs_read`'s guest address must equal its fixture
   `System.map` address, proving the matching unrandomised kernel.
4. The supporting calls (session start and end, breakpoints and continues the
   tool under test needs around it) use the frame's operator client. Only the
   tool under test goes through the cell's exposure and token, with the tool's
   gate role: contributor, or viewer for `debug.get_session` and
   `debug.list_sessions`.
5. A stop at `vfs_read` is the controlled workload: the body starts the
   resume call, then opens the guest's SSH forward and writes a client
   banner, which makes guest `sshd` call `read(2)`. A hit is expected well
   inside a 30 s resume wait.
6. Every session the body starts is ended before the body returns, also on
   failure; the frame then proves domain, disk and capacity cleanup.

`ksym(name)` below is the guest's `/proc/kallsyms` address; `next(name)` is the
next higher text symbol's address. A stop "in `vfs_read`" means
`ksym(vfs_read) ≤ rip < next(vfs_read)`.

| Tool (variant) | Exposed call and observed effect |
|---|---|
| `debug.start_session` (gdbstub) | Start on the Run. `debug.get_session` reads `live`, transport `gdbstub`, the Run and System; `debug.read_registers(["rip"])` answers; a second gdbstub start on the same Run answers `transport_conflict`. |
| `debug.start_session` (drgn-live) | Start on the Run, no `missing_debuginfo`. `introspect.run(helper="sysinfo")` names the fixture release; a second drgn-live start answers `transport_conflict`. |
| `debug.end_session` | After an attach (the guest is halted: no banner reply within 5 s), end. `debug.get_session` reads `detached`; the guest answers SSH again; a new gdbstub session on the Run starts and reads registers, then ends. |
| `debug.get_session` | `live`, `project`, `run_id`, `system_id` and `transport` equal the started session's. |
| `debug.list_sessions` | One session started and ended, a second left live. `request={run_id}` lists exactly those two ids, newest first; `state="live"` lists the second only, `state="detached"` the first only, each with the record's state. |
| `debug.continue` | Breakpoint at `vfs_read` (operator); continue with the banner: `stopped`, `reason="breakpoint-hit"`, not `timed_out`; `rip` is in `vfs_read`. |
| `debug.interrupt` | After the session starts, the cell records whether the guest still answers SSH; the interrupt answers a clean `stopped`; the guest no longer answers a banner within 5 s. It passes only if the guest was running before the interrupt and the stop carries `reason="signal-received"`. |
| `debug.advance` | Stopped in `vfs_read`. `mode="instruction"` answers `reason="end-stepping-range"` and `rip` is where the fixture binary says one step lands: `objdump` of the fixture `vmlinux` gives the next instruction, or a jump or call's target. `mode="out"` then answers `reason="function-finished"` with `rip` after one of the fixture's `call vfs_read` sites in a read(2)-family caller, which frame 1's address must also be, or, after a step into a callee, at the instruction after the call. |
| `debug.set_breakpoint` | Set at `vfs_read`: `number` returned; `debug.list_breakpoints` counts one; continue with the banner stops `breakpoint-hit` in `vfs_read`; frame 0 is `vfs_read`. |
| `debug.clear_breakpoint` | Breakpoint at `vfs_read` set, then cleared: `cleared`; the count is zero; a 5 s continue with the banner times out (`timed_out`), which a live `vfs_read` breakpoint would not. |
| `debug.list_breakpoints` | Breakpoints at `vfs_read` and `vfs_write`: count two; after clearing one, count one. The tool returns no identifiers or locations, so the cell then fails naming that gap. |
| `debug.set_watchpoint` | Watch `jiffies_64` (8 bytes): `watching`, a number; continue answers `reason="watchpoint-trigger"`; `jiffies_64` read before the set and after the stop increased. |
| `debug.clear_watchpoint` | Watchpoint on `jiffies_64` set, then cleared: `cleared`; `debug.list_watchpoints` lists none; a 5 s continue times out, which a live `jiffies_64` watchpoint would not. |
| `debug.list_watchpoints` | One watchpoint on `jiffies_64`: exactly one entry with the set call's number and the expression the tool sets, `*(char(*)[8])` at `ksym(jiffies_64)`. |
| `debug.read_memory` | At `ksym(linux_banner)`, the length of `/proc/version`: the bytes equal `/proc/version` and begin `Linux version <fixture release> `. |
| `debug.read_registers` | Stopped in `vfs_read`: `rip` in `vfs_read`; `rsp` a kernel address. |
| `debug.resolve_symbol` | `vfs_read` and `linux_banner` resolve to their `ksym` addresses. |
| `debug.disassemble` | `symbol="vfs_read"`, 16 instructions: their addresses are the first 16 instruction boundaries `objdump` decodes from the fixture `vmlinux` at `ksym(vfs_read)`, every `func_name` `vfs_read`. Mnemonics are recorded, not required: the kernel patches some sites at boot (the ftrace call, static branches). |
| `debug.backtrace` | Stopped in `vfs_read`: frame 0 is `vfs_read` at the stop's `rip`; frame 1 is a read(2)-family caller (`ksys_read`, `ksys_pread64` or their syscall wrappers); the walk reaches the syscall entry. |
| `debug.read_frame` | Stopped in `vfs_read`: level 0 is `vfs_read` with the stop's `rip`. The tool returns no arguments or locals, so the cell then fails naming that gap. |
| `debug.list_modules` | `loop` is listed with `base_address` equal to `/sys/module/loop/sections/.text`. |
| `debug.load_module_symbols` | A `[loop]` text symbol does not resolve before the load; `loop` with that base loads (`symbols_loaded`); the symbol then resolves to its `ksym` address. |

Every expected value comes from the guest before the attach, the fixture
binary or the session rows; the exposed call never supplies its own expected
value. The operator's own debugger calls set up a state (a stop, a breakpoint)
or read one back, and a value they return is checked against that ground truth
rather than trusted.

## Known gaps the cells report

Three obligations ask for effects the tool does not expose. The cell proves
what is observable, then fails with a message naming the gap; no obligation
text is weakened, and the live run's result is reported:

- `debug.list_breakpoints` returns only `count`.
- `debug.read_frame` returns the frame's `level`, `func`, `addr`, `file` and
  `line`, no arguments or locals.
- `debug.interrupt` never meets a running guest. `debug.start_session`'s
  gdbstub reachability probe connects and closes without a detach, which
  leaves the guest paused (ADR-0184); the interrupt's own first engine
  operation attaches gdb, which halts a running guest before
  `-exec-interrupt` is sent; every resume operation waits for a stop
  and halts again on timeout; and the per-session lock serialises a concurrent
  interrupt behind it. The cell records whether the started session's guest
  still answers, interrupts, requires a clean stop, and fails naming the gap
  while the guest was already halted.

## Rejection preparation

`debug_target` prepares one target per stack and caches it like
`capture_target`: a lane System booted with the fixture kernel, its Run, and
a gdbstub session started, attached and ended (`detached`), after which the
frame tears the System down. Every role and project check of the 21 tools
runs before any state check, so the torn-down System, its Run and the
detached session are valid targets. A failed preparation is remembered and
replayed for every later cell. A rejection record carries the lane context
and no kernel field, as #2814's do.

| Boundary | Call and grants | Expected |
|---|---|---|
| authentication | the tool's arguments; a viewer token re-signed by an untrusted key | HTTP 401; the issued token's call is not 401 |
| authorization | the gate role minus one: viewer for contributor, member with no role for viewer | `authorization_denied` |
| authorization, `debug.list_sessions` | member with no role; `request={run_id}` | the canonical empty page after a viewer's call lists the target session (ADR-0756) |
| project-isolation | an admin of another project, with an absent-id twin | `not_found` (`debug.get_session`) or `configuration_error`, identical to the twin's masked answer |
| project-isolation, `debug.list_sessions` | an admin of another project; `request={run_id}` | the canonical empty page after a viewer's call lists the target session |
| validation | the id argument (or `request.run_id`) given as an integer | the frame's validation rule |

Each tool's other arguments are fixed valid values (`location="vfs_read"`,
`mode="out"`, `registers=["rip"]`, `module="loop"`, …). Protected state is the
target project's snapshot (which includes its `debug_sessions` rows), the jobs
naming the target's System or Run, and whether its domain is defined.

## Failure model

1. Actors and deployments:
   - A maintainer running the carrier on a KVM x86_64 host against a stack
     brought up by `examples/local-libvirt/demo-up.sh`, one configuration per run; the MCP
     server shares the host with QEMU and has `gdb` on its PATH.
   - CI runs only the unit tests and the contract test.
2. Invariants and assets at stake:
   - Evidence honesty: a record passes only on the observed effect and the
     deployed candidate's identity; a known gap fails, never passes.
   - Host resources: every domain, disk, allocation and session a cell creates
     is released, including after a failure.
   - The coverage contract shared with #3111, #3115, #2817, #3097, #3098.
3. Accepted failure classes:
   - A guest halted under the debugger misses its own deadlines (watchdogs,
     clocks); the cell ends its sessions before the frame's cleanup probes.
   - An ambient `read(2)` may hit the `vfs_read` breakpoint before the
     banner's; any hit in `vfs_read` is the same stop the workload produces.
   - A test host without `objdump` (binutils) cannot decode the fixture binary;
     the `debug.advance` and `debug.disassemble` cells record `blocked`.
   - A missing server `gdb`, Run debuginfo or module `.ko` answers
     `debug_attach_failure`, `no_debuginfo` or `no_module_debuginfo`; the cell
     records `blocked` for a missing dependency, `failure` otherwise.
   - Each functional cell boots the fixture: several hours per configuration.
4. Covered elsewhere:
   - Remote debug cells: #3113. ppc64le cells and native POWER: #2818.
   - Capture and introspection tools: #2814. List-tool carriers: #3097, #3098.

## Threat model

The change adds test code and one ADR amendment. It reads guest output (probe
text, kallsyms lines, memory bytes) produced by a guest the test provisioned;
parsing is bounded to named symbols and fields, and evidence records carry
addresses, digests, booleans and counts, never memory contents or guest text.
Tokens are minted for synthetic projects. Out of scope: hostile guests, which
the deployment does not run.

## Verification and completion

- Unit tests (`tests/integration/live_stack/test_debug_cells.py`): the gdbstub
  provision options; kallsyms parsing (symbol, successor, `[loop]` symbols,
  zero addresses); the in-function check; the rejection arguments, categories
  and grants per tool and boundary; the `debug.list_sessions` empty page; and
  the cached target keeping only lane keys.
- Contract tests (`tests/scripts/test_coverage_contract.py`): the filtered
  set equals the ADR's thirteen pairs; group 2813's ppc64le cells are #2818's,
  its remote cells #3113's and unbound, its 440 x86_64 local cells #2813's;
  all 880 local cells name the carrier node.
- Live: both configurations, each exposure, on a lab x86_64 host with the
  deployed head equal to the committed candidate; results, including blocked
  or failing cells, recorded in `docs/operating/runbooks/live-testing.md`.
