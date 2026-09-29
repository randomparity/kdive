# Native POWER pseries gdbstub proof — plan

Goal: produce the proof record that
`docs/workflow/specs/2026-09-29-ppc64le-gdbstub-proof-design.md` (#2739) specifies.
Architecture: a live spike on a native ppc64le KVM-HV host; the only committed output is one
Markdown proof record under `docs/design/`. No runtime, test, or configuration file changes.
Tech stack: QEMU `qemu-system-ppc64` (pseries, KVM-HV), GNU gdb, libvirt `qemu:///session`,
the kdive `GdbMiEngine`, pytest `live_vm` tier.

Expected implementation size: 150–230 changed lines (M) — one proof record with four step
sections, a findings table, and quoted command output.

## Global Constraints

- Public text uses `sys-P1` for the host and `<REDACTED-HOME>` for home paths. It never carries
  a host name, IP address, or user name. Versions and model numbers stay.
- On the host: do not change the shared kdive checkout or its branch; use a detached worktree
  at the tested SHA and a private proof directory for this issue. Destroy every guest and
  domain the spike creates. Do not source the external-boot authority environment.
- Fixes the spike finds are findings for #2740, not changes in this PR.
- Guardrails: `just docs-links`, `just docs-paths`, `just records` (after
  `git fetch origin main`). The pre-push hook runs `just ci`.

## Task 1 — run the spike and write the proof record

Files: create `docs/design/2026-09-29-ppc64le-gdbstub-proof-record-2739.md`.
Interfaces: consumes nothing from other tasks; #2740 consumes the findings table.

Verification:

- Contract: the proof record. `Mode: task-test-not-applicable`. Surface: one human-readable
  evidence document. Reason: no executable consumer reads it, and its content is observed
  host output that no repository test can reproduce off the host.
- Contract: doc links and referenced paths resolve. Covered by `just docs-links` and
  `just docs-paths`; expected exit 0.

Steps:

1. Host setup. Fetch `origin` in the shared checkout's object store only, then
   `git worktree add --detach <REDACTED-HOME>/src/kdive-worktrees/verify-2739 <SHA>` and
   `uv sync` there. Create `<REDACTED-HOME>/kdive-2739-proof/`. Record `uname -r`,
   `/etc/os-release`, `gdb --version`, `qemu-system-ppc64 --version`, `virsh --version`, and
   the `vmlinux` build ID.
2. Transport (spec step 1). Start
   `qemu-system-ppc64 -machine pseries,accel=kvm -cpu host -m 2048 -smp 2 -nographic
   -kernel <kernel-tree>/vmlinux -append "console=hvc0 nokaslr" -S -gdb tcp:127.0.0.1:<port>`
   in the background. Run
   `gdb -nx -batch -ex 'show architecture' -ex 'target remote 127.0.0.1:<port>'
   -ex 'show architecture' -ex 'info registers pc' <kernel-tree>/vmlinux`.
   Expect: connect succeeds; record the architecture gdb chose. Repeat with
   `-ex 'set architecture powerpc:common64'` first and compare.
3. Register names (spec step 2). `-ex 'maint print registers'` (or
   `-data-list-register-names` over MI); record the count and whether `pc`, `nip`, `rip`, `r1`,
   `lr`, `msr` are present; `-ex 'p $pc' -ex 'p $r1'`.
4. Early boot (spec step 3). From the `-S` halt: `break start_kernel`, `continue`; record the
   stop location. Then `stepi`, `next`, `finish`, and a separate `hbreak` on another function;
   record each result or error text. Kill the guest.
5. Real engine (spec step 4, part 1). In the verify worktree, run a short Python driver that
   calls `GdbMiEngine().attach(host="127.0.0.1", port=<port>, vmlinux_path=<vmlinux>,
   transcript_path=<proof-dir>/t.jsonl)` and `read_registers(attachment, ["pc", "r1"])`, then
   `["nip"]` and `["rip"]`. Record results and errors.
6. Native proofs (spec step 4, part 2). In the verify worktree, widen
   `_GDBSTUB_PROVEN_ARCHES` to include `ppc64le` (uncommitted, host only). Export the
   live-tier environment (`examples/local-libvirt/env.sh`, `KDIVE_DATABASE_URL`, the ppc64le
   fixture variables, and `KDIVE_LIVE_VM_ROOTFS`, `KDIVE_LIVE_VM_BZIMAGE`,
   `KDIVE_LIVE_VM_VMLINUX`, `KDIVE_LIBVIRT_URI` from `tests/live_vm/__init__.py`).
   Run the three proofs one at a time with
   `uv run python -m pytest -m live_vm <node-id> -p no:randomly -x -q`. Record the first failure
   of each and its cause. Restore the file with `git restore`.
7. Cleanup. `virsh -c qemu:///session list --all` shows no spike domain; no spike `qemu`
   process remains; remove the verify worktree only after the record is written.
8. Write the record: environment, method, per-step results with exact commands and exit
   codes, failure boundaries, and a findings table (change, file evidence, owner #2740).
   Scan it for host identifiers before commit.
9. Run `just docs-links` and `just docs-paths`; expect exit 0. Commit
   `docs: record native POWER pseries gdbstub proof (#2739)`.

Rollback: the record is one new file; `git revert` removes it. Host cleanup is step 7.
