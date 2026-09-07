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

Resolve `depmod` in `_run_host_depmod` and pass an absolute path to `subprocess.run`: a
`KDIVE_DEPMOD` override, then the four in `src/kdive/jobs/.../bootstrap/bootstrap_elf.py`, then
`/usr/local/{sbin,bin}` — trailing, so an unmanaged local binary cannot shadow a packaged depmod.
The override is a worker-only `Setting` with no default in `providers/local_libvirt/settings.py`,
because `config_env_guard.py` forbids `KDIVE_*` reads outside `kdive.config`; its `parse` takes
only an absolute path to an executable file, and that module's `KDIVE_LIBVIRT_*` docstring widens
for it. `deploy/systemd/bin/kdive-live-worker-gate` rebuilds the worker environment strictly from
`_WORKER_ENV_NAMES` before `execve`, so this adds that one name; else the override is stripped on
the gated slot and `Registry.validate` never sees it. That slot's `worker.env` is generated, so
the override goes in a drop-in there (`systemctl edit kdive-live-worker@N`), not a role template.

Excluded, with owners: any other `_WORKER_ENV_NAMES` addition, `PATH` included (this change);
guest-side depmod (ADR-0346); gate credential invariants (gate-hardening). Deferred to a
follow-up issue: the `ops.diagnostics` startup preflight, because `src/kdive/diagnostics/` is
owned by a concurrent run. No ADR — this applies ADR-0087 to one knob.

## Success

1. `subprocess.run` gets an absolute `depmod` path, never a bare name.
2. Unresolvable depmod raises `MISSING_DEPENDENCY` reporting that none was found in the
   `details["searched"]` directories, offering both remedies: install kmod or set `KDIVE_DEPMOD`.
3. A `KDIVE_DEPMOD` that is not an absolute path to an executable file raises
   `CONFIGURATION_ERROR` naming the variable — misconfiguration, not a missing package.
4. A resolved depmod that cannot be executed (vanished, unreadable, wrong format) raises
   `MISSING_DEPENDENCY` — non-retryable, as today — with `details["depmod"]`; a spawn-side
   `OSError` (spawn pressure) stays retryable `INFRASTRUCTURE_FAILURE`. Neither escapes.
5. `KDIVE_DEPMOD` survives the gate's `execve`, so the override and its startup `validate` are
   reachable on the fixed live-worker slot rather than silently inert.

## Validation

Cases are in `tests/providers/local_libvirt/lifecycle/boot/test_module_indexing.py` unless named,
red first then green under `uv run python -m pytest <that file> -q`.

- Absolute argv (1). focused-test — `test_run_host_depmod_runs_the_resolved_absolute_path`; red
  against today's bare-name argv assertion.
- Unresolvable envelope (2). focused-test —
  `test_run_host_depmod_unresolvable_names_searched_directories`; red, no `searched` detail.
- Override honoured, and rejected when not absolute and executable (1, 3). focused-test —
  `test_run_host_depmod_uses_the_override`, `test_override_must_be_absolute_executable`; red.
- Exec-failure class (4). focused-test — `test_unexecutable_depmod_is_missing_dependency` and
  `test_spawn_pressure_stays_retryable_infrastructure_failure`; red, `FileNotFoundError` only.
- Gate crossing and startup validate (5); red, the frozenset omits the name. focused-test —
  `test_live_worker_gate.py`: the gate assertion, plus a `Registry.validate("worker")` case there.
- Config reference row. focused-test — `just config-docs-check`; red until `just config-docs` runs.
