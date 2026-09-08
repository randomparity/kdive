# 0631 — No operator override for a `depmod` outside the four search directories

## Status

Accepted (2026-09-07)

## Context

`_resolve_depmod`
(`../../src/kdive/providers/local_libvirt/lifecycle/boot/guest_kernel_writer.py:113-137`)
resolves `depmod` against a fixed four-directory list — `/usr/sbin`, `/usr/bin`, `/sbin`,
`/bin` — and never through `PATH`. #2300 established that list: the live worker is exec'd by
`deploy/systemd/bin/kdive-live-worker-gate`, which builds the child environment wholesale from
`_WORKER_ENV_NAMES` and carries no `PATH`, so a bare-name lookup falls back to `os.defpath`
(`/bin:/usr/bin`), misses `/usr/sbin`, and told operators to install a package they already had.

An operator whose `depmod` sits outside those four directories has no supported way to say so.
The only remaining answer is to relocate or symlink the binary into one of them. #2300 proposed
a `KDIVE_DEPMOD` override for exactly that operator, an operator approved it on the evidence
available at the time, and it was withdrawn the same day (`b0cefbedb`, in PR #2313) when the
corrected evidence showed what shipping it would cost. This record settles the question the
withdrawal left open rather than leaving it as an omission (#2340).

What the override would cost is a boundary, not a parameter. The value has to reach the worker
process, and the gate exists to stop ambient values from doing that. ADR-0621 narrowed that
boundary deliberately: a worker receives only the names the gate hands it, and
`tests/deploy/test_live_worker_gate.py:209-261` pins the result as an exact dictionary with an
ambient control entry (`KDIVE_LIBVIRT_RECOVERY_ROOT`) proving nothing else crosses the exec.
Admitting a `depmod`-location name means widening `_WORKER_ENV_NAMES`, and the gated child would
then take an operator-supplied absolute path and execute it as the worker slot account
(`User=kdive-worker-N`), which holds authority over guest overlays.

The four directories are not an arbitrary list. Every one is root-owned, they are the set
`bootstrap_elf.py` already resolves its own host tools against (`_TOOL_PATH`, line 23), and
`/usr/local/{sbin,bin}` are excluded on purpose because `/usr/local` is group-writable by default
on part of the Debian family. The list encodes "root-owned host tool directories", so the shape
an override admits is not the shape the list was built to hold.

## Decision

We will not add an operator override for a `depmod` outside `/usr/sbin`, `/usr/bin`, `/sbin`,
`/bin`. Those four directories are the contract for where the worker host's `depmod` must live.
An operator whose `depmod` sits elsewhere relocates or symlinks it into one of them; that is the
supported answer, not a workaround pending a feature.

The worker gate's environment allowlist is not widened to carry a `depmod` location, and this
record is the reason to cite when the question is raised again. `_DEPMOD_SEARCH_DIRS` cites this
ADR so the list and its decision are one lookup apart.

## Consequences

- The failure surface stays the diagnosis. `_resolve_depmod` raises `MISSING_DEPENDENCY` naming
  all four directories in `failure_message` — the one field every failure surface forwards — so
  an operator in this position is told what the contract is and can act on it without host
  access. That message is what makes relocate-or-symlink actionable rather than a guess, and it
  is the part of this decision that must not regress.
- No new environment name reaches the gated worker, so ADR-0621's exact-environment invariant and
  the test that pins it are unchanged, and the control entry keeps meaning what it means.
- An operator running a distribution that puts `depmod` outside the four directories is blocked
  from local-libvirt module indexing until they relocate or symlink it. No such distribution is
  known to us; the failure message is what surfaces one if it exists, and a report of a real host
  that cannot satisfy the contract reopens this decision with the evidence it needs.
- The list stays the same shape as the one `bootstrap_elf.py` uses and the one #2333 is extending
  to `virsh`, `qemu-img`, and `virt-customize`. A per-tool override would have made this tool the
  exception to a convention the codebase is otherwise converging on.
- Nothing here preflights or diagnoses a missing host toolchain at worker startup (#2339), and
  nothing here declares `kmod` as a host package (#2331) — this record decides only the override
  question, and the install-time answer is owned elsewhere.

## Considered & rejected

- **A `KDIVE_DEPMOD` environment override.** verified: the value cannot reach the worker without
  adding a name to `_WORKER_ENV_NAMES` in `deploy/systemd/bin/kdive-live-worker-gate`, whose
  child environment `_worker_environment` builds from that set alone and `os.execve` installs
  wholesale. That widening is what ADR-0621 narrowed and what
  `tests/deploy/test_live_worker_gate.py:209-261` asserts against. The admitted value would be an
  absolute path the worker slot account then executes, so the gate would be carrying an operator's
  choice of binary rather than a setting. Built once and withdrawn in `b0cefbedb`.
- **A configuration-file override read by the worker rather than an environment variable.**
  judgment: it reaches the same place by a different door. The worker would still execute an
  operator-named binary under the slot account, and the trust question is the same one — with the
  gate's exact-environment assertion no longer positioned to answer it, because the value never
  passes through the gate at all.
- **Adding `/usr/local/sbin` and `/usr/local/bin` to the list.** verified: this is the cheapest
  thing that helps some of the affected operators, and #2300 rejected it on the ground that
  `/usr/local` is group-writable by default on part of the Debian family, so a planted binary
  would run with the worker slot's authority over guest overlays. Nothing about #2340 changes
  that; widening the list quietly is the same admission as the override, without the record.
- **Falling back to `PATH` when the four directories miss.** verified: under the gate there is no
  `PATH` to fall back to, so the fallback would be dead on the deployment it is meant to serve
  and live only on an ungated worker — where it would reintroduce the ambient-value path the gate
  exists to close, on the hosts least likely to notice.
- **Leaving the question open.** judgment: the withdrawal in #2300 left the constraint in the
  code with its reasoning spread across a comment, a test's control entry, and an abandoned
  branch. The next operator to hit it, or the next agent asked for an override, would re-derive
  the same argument. The cost of the record is one file.
