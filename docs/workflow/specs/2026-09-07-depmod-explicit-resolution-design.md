# depmod explicit resolution design

Issue: #2300
Status: Accepted

## Problem

`_run_host_depmod` in `providers/local_libvirt/lifecycle/boot/guest_kernel_writer.py` runs the
bare name `["depmod", ...]`, so resolution rides an inherited `PATH`. The systemd worker gate's
allowlist omits `PATH`, so CPython falls back to `os.defpath` — the hardcoded `/bin:/usr/bin`,
not the `confstr(_CS_PATH)` the issue names — which lacks `/usr/sbin`, where the Ubuntu target's
kmod puts it. `runs.install` then fails `MISSING_DEPENDENCY` naming a package the host has.

## Scope

Resolve `depmod` in `_run_host_depmod` with `shutil.which` restricted to the four root-owned
directories `src/kdive/jobs/.../bootstrap_elf.py` already uses — `/usr/sbin`, `/usr/bin`, `/sbin`,
`/bin` — and pass the resolved absolute path to `subprocess.run`. `PATH` is never consulted.
`/usr/local/{sbin,bin}` stay out: `/usr/local` is group-writable by default on part of the Debian
family, and a binary planted there would run as the worker slot account.

A `KDIVE_DEPMOD` override was in the original scope and the operator **released** that clause
rather than leaving it unbuilt. It could only reach the gated live-worker slot by widening
`_WORKER_ENV_NAMES`, which the gate's exec environment excludes; the repository already declined
that widening once, provisioning `KDIVE_LIBVIRT_RECOVERY_ROOT` to the provider-authority host
instead, with a negative assertion in the gate tests as the durable record. No host has been seen
with `depmod` outside the four directories, and the failure names them, so the gap is diagnosable
without a knob. Reintroducing one is a follow-up, not a silent omission.

Excluded, with owners: widening `_WORKER_ENV_NAMES` at all, `PATH` and any `KDIVE_*` name alike
(this change); guest-side depmod (ADR-0346); gate credential invariants (gate-hardening).
Deferred to a follow-up issue: the `ops.diagnostics` startup preflight, because
`src/kdive/diagnostics/` is owned by a concurrent run. No ADR — no new decision is made here.

## Success

1. `subprocess.run` gets an absolute `depmod` path, never a bare name, so an absent or restricted
   `PATH` cannot change the outcome.
2. Unresolvable depmod raises `MISSING_DEPENDENCY`, keeping the "install kmod" remedy, with the
   searched directories in `details["searched"]` as a scalar — the worker's failure context keeps
   only scalar details, so a list never reaches the operator.
3. A resolved depmod that cannot be executed (vanished, unreadable, wrong format) raises
   `MISSING_DEPENDENCY` — non-retryable, as today — with `details["depmod"]` and `errno`; a
   spawn-side `OSError` stays retryable `INFRASTRUCTURE_FAILURE`. Neither escapes uncategorized.

## Validation

Cases are in `tests/providers/local_libvirt/lifecycle/boot/test_module_indexing.py`, red first
then green under `uv run python -m pytest <that file> -q`.

- Absolute argv and fixed search path (1). focused-test —
  `test_run_host_depmod_runs_the_resolved_absolute_path`; red against today's bare-name argv
  assertion.
- Unresolvable envelope (2). focused-test —
  `test_run_host_depmod_unresolvable_names_searched_directories`; red, no `searched` detail.
- Exec-failure class (3). focused-test — `test_unexecutable_depmod_is_missing_dependency` over the
  vanished and wrong-format cases; red, only `FileNotFoundError` is caught.
- Spawn pressure stays retryable (3). focused-test —
  `test_spawn_pressure_stays_retryable_infrastructure_failure` over EMFILE, EAGAIN and ENOMEM;
  red, the blanket catch made them non-retryable.
