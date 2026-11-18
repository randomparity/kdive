# 0822 — The guest exec allowlist adds `kdive-faultinject` for two debugfs fault-injection knobs

## Status

Accepted (2026-11-18)

Amends (does not supersede) [ADR-0079](0079-remote-live-debug-transport.md) and
[ADR-0085](0085-drgn-live-transport-generalization.md), which pin `kdive-drgn` as the one reviewed
in-guest program.

## Context

#918 adds MCP controls for kernel fault injection on local-libvirt Systems. Turning a knob means
writing debugfs as root inside the guest. ADR-0079 and ADR-0085 describe a single-program guest
exec surface (`/usr/local/sbin/kdive-drgn`, fixed argv, never a shell string). The allowlist is a
convention those records and the provider argv builders hold, not a code set. No reviewed helper
touches debugfs, and a free-form debugfs write would turn the surface into arbitrary root writes.

## Decision

Add one reviewed in-guest program, `/usr/local/sbin/kdive-faultinject`, to the allowlist. It is
staged wherever `kdive-drgn` is staged: the Debian, Fedora/RHEL and SUSE debug image families and
the Ansible `guest_base_image` helper list. Its contract:

- Fixed subcommands: `read [knob]`, `set <knob> <param>=<int>...`, `reset [knob]`.
- Knobs are `failslab` and `fail_page_alloc` only; a name is matched against that closed set and is
  never joined into a path from caller text.
- Parameters and bounds (min..max, `reset` default): `probability` 0..100 (0), `interval`
  1..1000000 (1), `times` -1..1000000 (1), `space` 0..1073741824 (0), `verbose` 0..2 (2), and for
  `fail_page_alloc` only `min-order` 0..10 (0). The defaults are the kernel's compiled-in
  `FAULT_ATTR_INITIALIZER` values (`include/linux/fault-inject.h`). A value matches
  `^(-1|0|[1-9][0-9]{0,9})$` and then its range; each parameter may appear once and `set` needs at
  least one pair. `task-filter` and the gfp flags are not exposed.
- Every pair is validated before any write, so a rejected `set` writes nothing. `set` writes
  `probability` last and `reset` writes it first, so a partial write never leaves injection armed
  with other parameters half-applied.
- Success prints one JSON object, `{"knobs":{"<knob>":{"present":true,<param>:<int>,...}}}`; `read`
  with no knob reports an absent knob as `{"present":false}`.
- Failures print `{"error":"<code>","detail":"..."}` on stderr and exit: 2 usage, unknown knob or
  parameter, or bad value; 3 debugfs not mounted; 4 knob directory absent from the kernel; 5 a
  parameter file is unreadable, unwritable or not an integer. A caller maps 3 and 4 to `configuration_error`.

The helper never mounts debugfs. Debugfs counts as mounted when its root is a non-empty directory.

This ADR does not change the remote-libvirt guest-agent `allowed_programs` set; no caller of the
new program ships here (#3683, #3684).

## Consequences

- The guest exec surface now includes a root debugfs writer. Its bounded argv is the control; a
  future knob needs a new amendment, a new bound table and tests.
- `reset` restores only the exposed parameters to the kernel defaults, not the values found
  before `set`; `task-filter` and the gfp flags are never touched.
- An image built earlier lacks the helper until rebuilt; callers treat a missing program as a
  configuration problem.
- `times` and `probability` can fail allocations for every task in the guest, including the SSH
  daemon; scoping to a task is the caller's concern and is outside this ADR.

## Considered & rejected

- **A `fault` subcommand on `kdive-drgn`.** judgment: it would give a read-only introspection
  contract a root write path and fold two review surfaces into one.
- **A generic debugfs read/write helper.** judgment: free-form paths and values are the arbitrary
  root write the allowlist exists to prevent, and the issue excludes them.
- **Writing debugfs through `kdive-drgn run-script`.** judgment: that mode runs caller-supplied
  Python under ADR-0240's separate gating; routing fixed writes through it hides them from the
  allowlist.
- **Fixed ssh one-liners per knob with no helper.** judgment: each caller would own its own
  validation, and the reviewed program the allowlist names would not exist.
- **A Python helper.** judgment: the guest carries bash for every sibling helper, and Python adds a
  guest dependency the debug images do not otherwise promise.
