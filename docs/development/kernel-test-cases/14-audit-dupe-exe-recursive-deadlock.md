# audit executable rule recursive lock on rename

## Summary

- **Subsystem**: audit, `kernel/audit_watch.c`
- **Fix reference**: [81905b5acbe7](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=81905b5acbe77284734438df3fbec1158e6429a3) "audit: fix recursive locking deadlock in audit_dupe_exe()"
- **Introduced by**: 34d99af52ad4 "audit: implement audit by executable" (v4.3-rc1)
- **Fixed release**: Linux 7.2-rc1
- **Architecture**: generic
- **Primary symptom**: `mv` hangs in D state; lockdep reports recursive locking
- **VM suitability**: Easy. Five-line shell script, deterministic.
- **KDIVE tools exercised**: `control.diagnostic_sysrq`, `debug.set_breakpoint`, `debug.backtrace`, `debug.read_frame`, `introspect.run`

## Bug description

A rename locks the parent directory and sends an `fsnotify_move` event. When an executable
audit rule matches, `audit_dupe_exe()` calls `audit_alloc_mark()`, which resolves the path
with `kern_path_locked()` and tries to take the same parent directory lock again.

## Suggested starting prompt

> On a system with audit rules, a plain `mv` of one file hangs forever. Explain which lock the
> `mv` task waits for and who holds it.

## Reproduction sketch

1. `auditctl -D; mkdir -p /tmp/foo; touch /tmp/file`
2. `auditctl -a always,exit -F exe=/tmp/file -F path=/tmp/file -S all -k dr`
3. `mv /tmp/file /tmp/foo/file`
4. Collect task stacks with SysRq `w`. With `CONFIG_PROVE_LOCKING`, read the lockdep report.

## Expected signal on vulnerable kernel

- `mv` blocks in `down_write_nested()` under `audit_alloc_mark()`.
- Lockdep: "possible recursive locking detected" on `i_mutex_dir_key`.

## Fixed-kernel expectation

The rename completes; the audit rule follows the moved file.

## A/B scoring hints

- Award credit for tracing the path from `vfs_rename()` through fsnotify into audit.
- Award extra credit for naming the second path lookup as the cause.
- Penalize an answer that blames the VFS rename locking.

## Caveats

Needs `CONFIG_AUDIT`, `CONFIG_AUDITSYSCALL`, and `auditctl` in the guest image.
