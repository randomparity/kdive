# Spec — wire a real `crash(8)` runner into the Retrieve postmortem (#816)

- **Issue:** [#816](https://github.com/randomparity/kdive/issues/816)
- **ADR:** [ADR-0249](../../adr/0249-real-crash-postmortem-runner.md)
- **Status:** Draft

## Problem

`postmortem.crash` and `postmortem.triage` return `missing_dependency` over the deployed
(live HTTP) server even when a real captured vmcore exists. The production provider
assembly wires the crash subprocess seam to a no-op stub:

- `src/kdive/providers/local_libvirt/retrieve.py` builds `LocalLibvirtRetrieve.from_env`
  with `run_crash=default_run_crash`.
- `src/kdive/providers/remote_libvirt/retrieve/facade.py` defaults
  `run_crash: RunCrash = default_run_crash`.
- `default_run_crash` (`providers/shared/debug_common/crash_postmortem.py:96`) raises
  `CategorizedError(category=MISSING_DEPENDENCY)` unconditionally.

Every other host-bound seam in the Retrieve plane (`_real_wait_for_vmcore`,
`_real_read_build_id`, `_real_host_dump_capture` in local; the kdump/host-dump capturers
in remote) has a real `# pragma: no cover - live_vm` implementation wired into production.
The crash seam is the only one missing its real counterpart, so the deployed
postmortem feature does not function. The two tools' `partial` promotion text — "a
recorded live_stack run runs crash commands over a real captured core" — is unsatisfiable
as written, because the live_stack server wires the stub.

Separately, `CrashResult.exit_status` is dead: `run_crash_postmortem` reads only
`crash.stdout`, so a `crash(8)` run that fails (non-zero exit, empty stdout) would be
reported as a successful postmortem with an empty transcript — a silent failure.

## Goals

1. The deployed worker runs the real `crash(8)` over a captured core when the binary is
   present on the worker host, returning the redacted transcript.
2. When `crash(8)` is absent, the tool returns `missing_dependency` with an actionable
   message naming the missing binary (not the misleading "runs only under the live_vm
   gate" stub message).
3. A non-zero `crash(8)` exit surfaces as a typed failure carrying redacted stderr, not a
   silently-empty transcript.
4. The new runner's command construction and failure mapping are unit-tested off the gate
   (no `/usr/bin/crash` required); only the `subprocess.run` itself is `live_vm`-gated.
5. A `live_vm` test drives the real `/usr/bin/crash` over a real captured core, so the
   real path has at least one executable proof.
6. The maturity metadata for `postmortem.crash`/`triage` reflects the new reality.

## Non-goals

- Changing the crash-command allowlist or the validator (`security/artifacts/crash_commands.py`).
- Changing the build-id provenance check or the `read_build_id` seam.
- Changing the tool surface, parameters, RBAC, schema, or any persistence — no migration.
- Streaming a multi-GB core differently; the existing tempfile spool is unchanged.

## Design

### The real runner

Add `_real_run_crash(vmlinux: Path, vmcore: Path, script: str) -> CrashResult` to
`providers/shared/debug_common/crash_postmortem.py`, replacing `default_run_crash` as the
production default (the stub is deleted — replace, don't deprecate).

```
crash_path = shutil.which("crash")
if crash_path is None:
    raise CategorizedError(
        "the crash(8) utility is not installed on this worker host",
        category=MISSING_DEPENDENCY,
    )
argv = [crash_path, "-s", str(vmlinux), str(vmcore)]
# subprocess.run(argv, input=script.encode(), timeout=_CRASH_TIMEOUT_S,
#                check=False, capture_output=True)
```

- `crash -s` (silent) suppresses the banner and the `crash>` prompt echo so the transcript
  is the command output only. `vmlinux` and `vmcore` are the worker-owned temp paths the
  shared helper already spools; the command batch (validated upstream, terminated with
  `quit`) is fed on **stdin only**, never argv — so the argv is fixed (`S603`/`S607`
  justified inline like `introspect.py`).
- `shutil.which` is injected as `crash_path_finder: Callable[[str], str | None] = shutil.which`
  so the "binary absent → missing_dependency" branch is unit-testable, mirroring
  `PygdbmiController`'s `gdb_path_finder`.
- The `subprocess.run` call is a thin `_exec_crash(...)` helper marked
  `# pragma: no cover - live_vm`, mapping:
  - `subprocess.TimeoutExpired` → `INFRASTRUCTURE_FAILURE` (local subprocess, not transport),
    `details={"timeout_s": …}`.
  - `OSError` (launch failure after `which` succeeded) → `INFRASTRUCTURE_FAILURE`.
  - otherwise → `CrashResult(exit_status=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)`.
- `crash(8)` may write session scratch state under its working directory, which a
  constrained worker may not allow under the process CWD. `_exec_crash` runs with `cwd`
  set to a per-call temp dir (the vmlinux/vmcore spool's parent, already worker-owned), so
  crash never depends on a writable process CWD.

Bound the run with `_CRASH_TIMEOUT_S` (default 300 s; a batch of allowlisted read verbs
over a multi-GB core can take minutes) so a wedged `crash` never pins a worker thread.

The exact invocation (`-s`, positional order, stdin batch delivery) is **not** falsifiable
by CI — the unit tests cover argv *construction* against an injected path-finder but never
spawn `crash`. Its correctness is proven only by the `live_vm` test and the recorded live
proof (the chosen scope drives one); the proof records the `crash(8)` version it ran
against so a future version skew is traceable.

### Exit-status check moves into the shared helper

`run_crash_postmortem` gains an exit-status guard so the `CrashResult.exit_status` field is
load-bearing and the check is provider-neutral and unit-testable. The guard is
**conservative**: `crash(8)` continues a batch past a per-command error and (verified on
the live proof, below) exits non-zero mainly when it cannot *initialize* over the
core/namelist — but a version that returns the last command's status could exit non-zero
with a full, useful transcript already on stdout. So the helper fails only when the exit is
non-zero **and** stdout is empty/whitespace (the init-failure shape); a non-zero exit with
real stdout returns the transcript rather than discarding it:

```
crash = run_crash(...)
redactor = Redactor(registry=secret_registry)
transcript = redactor.redact_text(crash.stdout.decode("utf-8", "replace"))
if crash.exit_status != 0 and not transcript.strip():
    raise CategorizedError(
        "the crash(8) subprocess exited non-zero with no output; the core could not be analyzed",
        category=INFRASTRUCTURE_FAILURE,
        details={"exit_status": crash.exit_status,
                 "stderr": redactor.redact_text(crash.stderr.decode("utf-8", "replace"))[:_STDERR_CAP]},
    )
```

stderr is redacted (it can echo paths/values) and capped (`_STDERR_CAP = 2048`) before it
enters the response. The redactor is constructed once and reused for stderr + the
transcript.

### Wiring

- Local: `LocalLibvirtRetrieve.from_env` passes `run_crash=_real_run_crash`.
- Remote: `RemoteLibvirtRetrieve.__init__` default becomes `run_crash: RunCrash = _real_run_crash`.
- `default_run_crash` is removed from the module and its `__all__`; remote's `facade.py`
  import is updated. `default_read_vmcore_build_id` is **out of scope** (still wired in
  remote; the issue's claim about it is noted but not changed here — local already wires a
  real build-id reader, and remote's real build-id reader is a separate gap).

### Maturity

The stub is gone, so a live run over a real core exercises the real path. The live proof
(below) drove the production worker path to a real `sys`+`log` transcript over a real
captured core, so `postmortem.crash` and `postmortem.triage` are promoted to `implemented`
(`maturity_detail` removed) and the maturity guard in `tests/mcp/core/test_tool_docs.py`
adds a promotion check. The worker's `crash(8)` must support the kernel under test — a host
prerequisite, documented alongside `drgn`/`libguestfs`.

## Acceptance criteria

1. With `crash(8)` present and a real core, `postmortem.crash(commands=["sys"])` returns a
   redacted transcript (live_vm proof + recorded live run).
2. With `crash(8)` absent, the tool returns `missing_dependency` naming the missing binary.
3. A non-zero `crash(8)` exit **with empty stdout** returns `infrastructure_failure` with
   redacted, capped stderr; a non-zero exit that still produced a transcript returns it.
4. `default_run_crash` no longer exists in the tree (`rg` finds no references).
5. Unit tests cover: argv construction (fixed argv, stdin script), `which`→missing_dependency,
   timeout→infrastructure_failure, non-zero-exit→infrastructure_failure, success→CrashResult.
6. `just ci` is green; the live_vm test runs the real `/usr/bin/crash`.

## Failure modes / edge cases

- **crash absent on worker** → `missing_dependency` (covered).
- **crash present, debuginfo/core mismatch** → already guarded by the build-id check before
  crash runs; if crash still fails, non-zero exit → `infrastructure_failure`.
- **crash hangs** → `_CRASH_TIMEOUT_S` bound → `infrastructure_failure`.
- **crash present but cannot write scratch state** (read-only/constrained worker CWD) → the
  runner sets `cwd` to a worker-owned temp dir, so this does not occur for CWD; a deeper
  sandbox failure still surfaces as a non-zero exit with empty stdout → `infrastructure_failure`.
- **crash exits non-zero but produced a full transcript** (last batch verb errored, or a
  version that returns the last command's status) → the conservative guard returns the
  transcript instead of discarding it.
- **stderr contains a secret/path** → redacted + capped before it reaches the response.
- **invalid UTF-8 in stdout/stderr** → `decode(errors="replace")`, unchanged for stdout.

## Live proof (2026-06-25)

Run on this dev host (`crash 9.0.1-2.fc43`, GCC 15.2.1, KVM).

**Step 1 — invocation correctness (kdive's default kernel 7.0.0).** Against a real captured
`host_dump` core + matching `vmlinux` from a prior live run (run `d26709b2-…`, vmlinux
build-id `c8be067b…` == the core's VMCOREINFO `BUILD-ID`), `_real_run_crash` **invoked
`/usr/bin/crash` correctly** — it launched, loaded the build-id-matched 2 GB ELF core + 457 MB
`vmlinux`, resolved symbols, and reached crash's memory init. crash 9.0.1 then **could not
analyze the kernel-7.0.0 core** (`invalid structure member offset: kmem_cache_s_num` in
`kmem_cache_init()`, then a libc abort) — a `crash(8)` forward-compat limitation with kernel
7.0, **not** a kdive defect (the same core walks fully under `drgn`: 137 tasks). The real
failure (exit 1, empty stdout) is exactly the `infrastructure_failure` shape the conservative
exit-status guard surfaces — validating that design.

**Step 2 — green end-to-end transcript (crash-supported kernel 6.19).** Built kernel `v6.19`
(`make defconfig` + `DEBUG_INFO`/`DWARF5`, `RANDOMIZE_BASE=n`) in an external worktree, booted
it under QEMU/KVM with a freestanding init, dumped guest memory via QMP `dump-guest-memory`
(2.1 GB ELF core), and ran the production worker path over it:

- The `live_vm` test (`_real_run_crash` → real `/usr/bin/crash -s`) returned `exit_status=0`
  with a `sys` banner (`RELEASE: 6.19.0`, `CPUS: 2`, `TASKS: 59`).
- The full shared helper `run_crash_postmortem` (validate → fetch → build-id → real crash →
  redact → exit guard) returned a redacted transcript for `["sys", "log"]`
  (`results={'sys': {'ran': True}, 'log': {'ran': True}}`), the full dmesg included.

**Outcome:** the production worker-side postmortem produces a real crash transcript
end-to-end. `postmortem.crash`/`triage` are promoted to `implemented`. The usable-kernel range
is bounded by the worker's `crash(8)` version, not by kdive: `crash(8)` must support the
kernel under test — a host prerequisite alongside `drgn`/`libguestfs` (and the reason kdive's
current default 7.0 build needs a newer `crash` than 9.0.1).

## Considered & rejected

- **Option 2 — declare the tools `live_vm`-only and keep the production stub.** Rejected:
  the deployed MCP server is the product; a postmortem that only works inside pytest is a
  phantom feature. The crash subprocess runs on the worker host exactly like `drgn`/`gdb`/
  `libguestfs`, all of which run for real in production.
- **Keep the exit-status check in the real runner only.** Rejected: putting it in the
  shared helper makes it provider-neutral and unit-testable without `/usr/bin/crash`, and
  makes the existing `CrashResult.exit_status` field load-bearing instead of dead.
- **Feed the command batch via `crash -i <cmdfile>` or argv.** Rejected: stdin keeps the
  argv fixed (no per-command argv injection surface) and matches the existing
  `_exec_live_script` pattern; the upstream validator already sanitizes the batch.
- **Fix remote's `default_read_vmcore_build_id` here too.** Deferred: out of this issue's
  scope (a separate remote-only gap); this change is the crash-runner wiring.
