# depmod explicit resolution design

Issue: #2300
Status: Accepted

## Problem

`_run_host_depmod` in `src/kdive/providers/local_libvirt/lifecycle/boot/guest_kernel_writer.py`
runs the bare name `["depmod", ...]`, so resolution depends on an inherited `PATH`. The systemd
worker gate execs the worker with an allowlist that omits `PATH`, so Python falls back to the
POSIX `confstr(_CS_PATH)` default `/bin:/usr/bin`, which lacks `/usr/sbin` where `depmod` lives.
`runs.install` then fails `MISSING_DEPENDENCY` telling the operator to install a package they
already have, and the envelope never says where it looked.

## Scope

Resolve `depmod` in `_run_host_depmod` and pass the absolute path to `subprocess.run`. Order: a
`KDIVE_DEPMOD` absolute-path override, then `shutil.which` restricted to `/usr/sbin`, `/usr/bin`,
`/sbin`, `/bin` — the host-tool list `jobs/capture_operations/bootstrap/bootstrap_elf.py` already
uses. The override is a `Setting` in `src/kdive/providers/local_libvirt/settings.py`, because
`scripts/guards/config_env_guard.py` forbids reading a `KDIVE_*` key outside `kdive.config`. Its
`parse` rejects anything but an absolute path to an executable file, so a stale override fails
the worker's startup `validate` instead of a first install. The generated
`docs/guide/reference/config.md` row follows from that declaration.

Excluded, with owners: adding `PATH` to `_WORKER_ENV_NAMES` (this change — the issue rejects
widening the gate's minimal exec environment); guest-side depmod invocation (none — ADR-0346
settled host-side indexing); worker-gate credential binding (gate-hardening work); the
`ops.diagnostics` worker-startup preflight (a follow-up issue — it touches
`src/kdive/diagnostics/`, owned by a concurrent change). No deferral records are open, and no ADR
is warranted: this applies ADR-0087 to one knob and leaves ADR-0346 untouched.

## Success

1. `subprocess.run` receives an absolute `depmod` path, never a bare name, so an absent or
   restricted `PATH` cannot change the outcome.
2. Unresolvable `depmod` raises `MISSING_DEPENDENCY` keeping the "install kmod" remedy, naming
   `KDIVE_DEPMOD` as the alternative, with `details["searched"]` listing the directories tried.
3. A `KDIVE_DEPMOD` that is not an absolute path to an executable file raises
   `CONFIGURATION_ERROR` naming the variable — a misconfiguration, not a missing package.
4. A binary that resolves then disappears before exec raises `MISSING_DEPENDENCY` carrying the
   resolved path, so no uncategorized `FileNotFoundError` escapes the envelope.

## Validation

Cases live in `tests/providers/local_libvirt/lifecycle/boot/test_module_indexing.py`, each red
first then green under `uv run python -m pytest <that file> -q`.

- Absolute argv (1). Mode: focused-test — `test_run_host_depmod_runs_the_resolved_absolute_path`;
  red against today's bare-name argv assertion.
- Unresolvable envelope (2). Mode: focused-test —
  `test_run_host_depmod_unresolvable_names_searched_directories`; red because no `searched`
  detail exists.
- Override honoured, and rejected when not an absolute executable (1, 3). Mode: focused-test —
  `test_run_host_depmod_uses_the_override` and `test_override_must_be_an_absolute_executable`;
  red because the setting does not exist.
- Resolved-then-vanished (4). Mode: focused-test —
  `test_run_host_depmod_resolved_binary_vanished_is_missing_dependency`.
- Generated config reference row. Mode: focused-test — `just config-docs-check`; red until
  `just config-docs` regenerates `docs/guide/reference/config.md`.
