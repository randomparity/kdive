# 0635 — One module-staging tool contract, surfaced as a diagnostics vantage

## Status

Accepted (2026-09-08)

- **Issue:** #2339

## Context

[ADR-0346](0346-ppc64le-kdump-crashkernel-defaults.md) §2 put module indexing on the host: the
worker runs `depmod -b <tmp> <version>` against an extracted module tree. #2300 (PR #2313) then
narrowed how that binary is found. Resolution never consults `PATH`, because the live-worker gate
execs the worker from an environment allowlist that omits it, so a bare `shutil.which` falls back
to `os.defpath` (`/bin:/usr/bin`) and misses `/usr/sbin`. The contract became four explicit
directories, `("/usr/sbin", "/usr/bin", "/sbin", "/bin")`.

Nothing reports on that contract until it is violated. `rg -n "depmod" src/kdive/diagnostics/`
matches nothing on `origin/main` at 14f9f462b, so a host laying `depmod` elsewhere first learns so
from the `MISSING_DEPENDENCY` error `_resolve_depmod` raises mid-install — after a System has been
allocated and a guest booted. #2339 is the deferred half of #2300, which left surfacing the
requirement to a follow-up.

The same four directories appear as the literal `"/usr/sbin:/usr/bin:/sbin:/bin"` in four other
places — `bootstrap_elf.py:23` (`_TOOL_PATH`, a `which` search path like this one) and three
subprocess-environment `PATH` assignments (`__main__.py:281`,
`jobs/capture_operations/launcher.py:292`, `scripts/generate/build-capture-bootstrap-manifest.py:28`).
A sixth copy written for the diagnostic is what this decision avoids; consolidating the other five
is out of #2339's surface.

## Decision

**1. The vantage is an `ops.diagnostics` check, not a worker-startup preflight.** One
worker-vantage `Check` with the stable id `depmod_toolchain`, dispatched through the existing
diagnostics job (ADR-0164) and aggregated into the same three-state verdict as every other check
(ADR-0091 §2). `ops.diagnostics` is already operator-gated, audited, and fronted by
`kdivectl doctor --json`; a startup preflight would need its own reporting surface and be reachable
only by restarting a worker. Module staging is one worker capability among several, so a missing
binary is one `fail` and never a reason to refuse to start.

**2. The search contract lives in `providers/shared/`, and both readers import it.** `DEPMOD`,
`DEPMOD_SEARCH_DIRS`, and the joined `DEPMOD_SEARCH_PATH` move to
`src/kdive/providers/shared/module_staging_tools.py`. `guest_kernel_writer._resolve_depmod` imports
them; so does the diagnostics contribution. That is where the existing neutral contributions
already read provider knowledge from — `pseries_fadump` ← `providers.shared.fadump_detect`,
`multiarch_gdb` ← `providers.shared.debug_common`. No behaviour moves: `_resolve_depmod` keeps its
`CategorizedError`, its category, and its message verbatim, because that message is tuned to a
mid-run job failure and a diagnostic's `detail`/`fix` pair is a different surface.

**3. The check joins the existing local-libvirt contribution.**
`service._worker_vantage_dispatch_mode` builds one `JobWorkerCheckDispatcher` per contribution, so
a second contribution naming `local-libvirt` would dispatch a second job for the same verdict. The
check and its `WorkerVantageDescriptor` are added to `contributions/multiarch_gdb.py`'s
`_worker_checks` and `_unavailable_worker_checks`.

**4. The searched directories go in `detail` and `fix`, not `data`.**
`result_codec.serialize_results` emits seven named fields and `CheckResult.data` is not among them,
so a worker-vantage check's structured fields are dropped in transport. The directory list is the
actionable half of the verdict, so it goes in the prose fields that survive — the same reason
`_resolve_depmod` names them in its message rather than only in `details`.

## Consequences

- An operator whose `depmod` sits outside the four directories gets a `fail` naming the binary,
  the directories searched, and the package to install, before any System is allocated.
- The verdict cannot drift from the run path: both readers import from one module, so a one-sided
  change to the search set is not expressible in source. `ops.diagnostics` gains one item —
  additive and same-shaped, but externally read, so a caller counting checks sees one more.
- **The shipped container fails this check today.** `Dockerfile`'s runtime stage is
  `python:3.14.6-slim-bookworm` (`:75`) and its package list (`:88-91`) omits `kmod`, so a worker
  from that image has no `depmod` in any of the four directories and `kdivectl doctor` exits
  nonzero. That is the check working — the image genuinely cannot stage modules, and the gap
  previously surfaced only mid-install. Adding the package is a `Dockerfile` decision outside
  #2339's surface, recorded as
  [debt 0012](../debt/0012-shipped-worker-image-has-no-depmod.md).
- A `pass` means `depmod` **resolves**, not that an install will succeed. The run path can still
  fail at exec (`_DEPMOD_EXEC_ERRNOS` in `guest_kernel_writer`) or on a non-zero `depmod` exit. The
  check deliberately does not exec the binary, so that residual stays.
- `guest_kernel_writer` loses its two module-level constants, which collides with two unmerged
  branches: #2340 inserts comment lines directly above `_DEPMOD_SEARCH_DIRS`, and #2333's
  `host_tool_search.py` docstring refers to `_DEPMOD_SEARCH_DIRS` by name twice. Whichever branch
  rebases second moves those comment lines into the new module and repoints those two references.

## Considered & rejected

- **A worker-startup preflight that refuses to start.** judgment: it would take down jobs that
  never stage modules, and the failure would be reachable only by restarting a worker.
- **Duplicating the name and directories in the contribution, with a test asserting equality.**
  judgment: the test links the copies but nothing stops a reader trusting the wrong one, and a
  diagnostic whose only guarantee of fidelity is a same-repository test is the divergence this
  check exists to prevent.
- **Importing `guest_kernel_writer` from `kdive.diagnostics`.** verified: both existing neutral
  contributions state in their own module docstrings that they depend on no local-libvirt
  internals (`src/kdive/diagnostics/contributions/multiarch_gdb.py` and `pseries_fadump.py`), and
  the import would pull `tarfile`, `kernel_bundle`, and the overlay writer in for two constants.
- **A second `DiagnosticProviderContribution`.** verified: `_worker_vantage_dispatch_mode` in
  `src/kdive/diagnostics/service.py` builds one `JobWorkerCheckDispatcher` per enabled
  contribution, so a second one naming `local-libvirt` dispatches a second diagnostics job.
- **Carrying the directories in `CheckResult.data`.** verified: `serialize_results` in
  `src/kdive/diagnostics/result_codec.py` writes `check_id`, `status`, `detail`, `fix`, `provider`,
  `failure_category`, `resource_id` — `data` is not among them, so it is dropped before the
  dispatcher reconstructs the result.
- **Covering `virsh`, `qemu-img`, and `virt-customize` too.** verified: #2333 resolves those
  against `("/usr/bin", "/bin")` in
  `src/kdive/providers/local_libvirt/lifecycle/host_tool_search.py` (branch
  `feat/host-tool-resolve-2333`) — a different search set for a different call site, and an
  operator-approved exclusion for #2339.
- **Sharing `_resolve_depmod` itself, not just the constants.** judgment: the resolver raises a
  `CategorizedError` whose message is written for a mid-run job failure, and a diagnostic needs a
  `detail`/`fix` pair, so the check would have to catch the error and re-split its message —
  coupling the verdict's wording to a job error string. The constants are the part that must not
  diverge; the one `shutil.which` call each side makes is one line.
- **One host-tool-search module carrying both this search set and #2333's.** verified: #2333's
  `PROVIDER_TOOL_SEARCH_DIRS` is `("/usr/bin", "/bin")` — a different set for a different call
  site — and lives on the unmerged branch `feat/host-tool-resolve-2333`. #2339 must not take a
  source dependency on an unmerged branch; merging the two sets is for whoever owns both after
  they land.
- **Adding `kmod` to `Dockerfile` here so the shipped image passes.** judgment: a runtime package
  changes what every container deployment installs, and `AGENTS.md` assigns that to the role owning
  the layer, not to a diagnostics change. Recorded as
  [debt 0012](../debt/0012-shipped-worker-image-has-no-depmod.md) instead.
- **Doing nothing.** verified: `rg -n "depmod" src/kdive/diagnostics/` returns nothing on
  `origin/main` at 14f9f462b, so the only report today is the mid-install `MISSING_DEPENDENCY`
  failure, after allocation and boot.
