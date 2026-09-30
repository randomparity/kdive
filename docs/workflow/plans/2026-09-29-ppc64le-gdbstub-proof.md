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

- Public text uses `sys-P1` for the host, `<REDACTED-HOME>` for home paths, `<REDACTED-USER>`
  for user names, and `<REDACTED-DSN>` for database URLs. It carries no host name, non-loopback
  IP address, user name, or credential. Versions and model numbers stay.
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
- Contract: doc links and referenced paths resolve. Covered by `just docs-links`,
  `just docs-paths`, and `just records`; expected exit 0.

Steps:

Every host script runs as `bash -s` with `set -euo pipefail`. Each command whose failure is a
result runs as `rc=0; timeout 120 <cmd> > <log> 2>&1 || rc=$?` and the script writes `rc` to
the proof directory, so an expected failure neither aborts the script nor hides its exit code.
A fired timeout (rc 124) is recorded as the failure boundary. Each direct QEMU guest is started
by one helper that also installs `trap 'kill "$QEMU_PID" 2>/dev/null || true' EXIT`.

1. Host setup. `git fetch origin` in the shared checkout's object store only, then
   `git worktree add --detach <REDACTED-HOME>/src/kdive-worktrees/verify-2739 <SHA>` and
   `uv sync` there. Create `<REDACTED-HOME>/kdive-2739-proof/`. Record `uname -r`,
   `/etc/os-release`, `gdb --version`, `qemu-system-ppc64 --version`, `virsh --version`, the
   `vmlinux` build ID, and a `virsh -c qemu:///session list --all --name` snapshot.
2. Guest helper. Each gdb session below gets a **fresh** guest halted at reset:
   `qemu-system-ppc64 -name kdive-2739-spike -machine pseries,accel=kvm -cpu host -m 2048
   -smp 2 -display none -monitor none -serial file:<proof-dir>/console-<n>.log
   -kernel <kernel-tree>/vmlinux -append "console=hvc0 nokaslr" -S
   -gdb tcp:127.0.0.1:<port> < /dev/null &`, then `QEMU_PID=$!`. After the session, kill the
   guest and wait for it. The record names the guest instance `<n>` for each result.
3. Transport and target selection (spec step 1), guests 1 and 2. Guest 1:
   `gdb -nx -batch -ex 'show architecture' -ex 'target remote 127.0.0.1:<port>'
   -ex 'show architecture' -ex 'info registers pc' <kernel-tree>/vmlinux`. Guest 2: the same
   with `-ex 'set architecture powerpc:common64'` first. Compare the two.
4. Register names (spec step 2), in the guest 1 session: add `-ex 'maint print registers'`
   and `-ex 'p $pc' -ex 'p $r1' -ex 'p $nip' -ex 'p $rip'`. Record the count and whether `pc`,
   `nip`, `rip`, `r1`, `lr`, `msr` are present.
5. Early boot (spec step 3), guest 3, one gdb session: `target remote`, `break start_kernel`,
   `continue`, `bt 1`, `stepi`, `x/i $pc`, `next`, `finish`, `hbreak <another function>`,
   `continue`. Record the stop location and each result or error text; quote the console log.
6. Real engine (spec step 4, part 1), guest 4: in the verify worktree, a Python driver calls
   `GdbMiEngine().attach(host="127.0.0.1", port=<port>, vmlinux_path=<vmlinux>,
   transcript_path=<proof-dir>/t.jsonl)` and then `read_registers(attachment, ["pc", "r1"])`,
   `["nip"]`, and `["rip"]`, each in its own `try`. Record results and errors.
7. Native proofs (spec step 4, part 2). In the verify worktree, widen
   `_GDBSTUB_PROVEN_ARCHES` to include `ppc64le` (uncommitted, host only; a trap runs
   `git restore tests/mcp/debug/session_support.py` on exit). Export: `source
   examples/local-libvirt/env.sh`; `KDIVE_DATABASE_URL=$KDIVE_SERVER_DATABASE_URL`;
   `KDIVE_LIBVIRT_URI=qemu:///session`; `KDIVE_LIVE_VM_BZIMAGE=<kernel-tree>/vmlinux` (pseries
   boots the ELF directly; there is no bzImage); `KDIVE_LIVE_VM_VMLINUX=<kernel-tree>/vmlinux`;
   `KDIVE_LIVE_VM_ROOTFS=<the ppc64le Fedora 44 fixture image>`. Run each proof alone with
   `timeout 900 uv run python -m pytest -m live_vm <node-id> -p no:randomly -q`. A skip or a
   `require_*` failure is an environment result, not a #2740 change. Where the first failure is
   an x86 register name, repeat once with a host-only `rip` -> `pc` edit and record how far
   the proof then gets; stop at the next failure.
8. Cleanup. Kill any `kdive-2739-spike` process. Compare `virsh -c qemu:///session list --all
   --name` with the step 1 snapshot; `virsh destroy` and `undefine` only new `kdive-x` domains
   the proofs left. Remove the verify worktree after the record is written.
9. Write the record: environment, method, per-step results with exact commands and exit
   codes, failure boundaries, and a findings table (change, file evidence, owner #2740). Mark
   for each proof what stays unobserved past its last recorded failure.
10. PII check: `rg -n -i '<host short name>|<operator user>|/home/|/tmp/pytest-of-|/run/user/|
    postgres(ql)?://|\b(?!127\.0\.0\.1)[0-9]{1,3}(\.[0-9]{1,3}){3}\b' --pcre2 <record>`;
    expect no match. Replace hits with `sys-P1`, `<REDACTED-HOME>`, `<REDACTED-USER>`, or
    `<REDACTED-DSN>`.
11. Run `just docs-links`, `just docs-paths`, and `git fetch origin main && just records`;
    expect exit 0 for each. Commit `docs: record native POWER pseries gdbstub proof (#2739)`.

Rollback: the record is one new file; `git revert` removes it. Host cleanup is step 8.
