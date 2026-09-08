# Module-staging host toolchain: a diagnostics vantage over the `depmod` search contract

- **Issue:** #2339
- **ADR:** [ADR-0635](../../adr/0635-module-staging-toolchain-vantage.md)

## Problem

Module staging runs `depmod -b <workdir> <version>` on the worker host (ADR-0346 §2). #2300
(PR #2313) narrowed how that binary is found: resolution never consults `PATH`, because the
live-worker gate execs the worker from an environment allowlist that omits it, so a bare
`shutil.which` falls back to `os.defpath` (`/bin:/usr/bin`) and misses `/usr/sbin`. The contract
became four explicit directories — `("/usr/sbin", "/usr/bin", "/sbin", "/bin")` in
`src/kdive/providers/local_libvirt/lifecycle/boot/guest_kernel_writer.py`.

Nothing reports on that contract until it is violated. `rg -n "depmod" src/kdive/diagnostics/`
matches nothing on `origin/main` at 14f9f462b, so the first signal is the `MISSING_DEPENDENCY`
error `_resolve_depmod` raises during an install — after a System has been allocated and a guest
booted. The trap the narrowing introduced is that a host *can* have `depmod` and still fail, so the
directory list, not just the binary name, is the actionable part of any report.

## Scope

One worker-vantage diagnostics check, `depmod_toolchain`, reporting whether the module-staging
toolchain resolves under the contract the run path uses.

1. **One contract, two readers** (ADR-0635 §2). `DEPMOD`, `DEPMOD_SEARCH_DIRS`, and
   `DEPMOD_SEARCH_PATH` move to a new `src/kdive/providers/shared/module_staging_tools.py`;
   `_resolve_depmod` and the new contribution both import them. `_resolve_depmod`'s behaviour,
   error, category, and message text are unchanged.
2. **The check.** `DEPMOD_TOOLCHAIN_ID = "depmod_toolchain"` in `diagnostics/checks.py`;
   `DepmodToolchainCheck` in `diagnostics/provider_checks.py` beside the seven existing classes.
   Vantage `WORKER`, provider `local-libvirt`. The probe returns the resolved absolute path or
   `None`: resolved → `pass` naming the path; `None` → `fail` naming `depmod` and the four
   directories, `fix` naming the package and the alternative,
   `failure_category=MISSING_DEPENDENCY`. `checks.run_check` already converts a timeout or an
   unexpected exception into `error`, so the check declares no third branch.
3. **The contribution.** `diagnostics/contributions/depmod_toolchain.py` builds the default probe
   (`shutil.which` injected for tests) and exposes `depmod_toolchain_worker_check()` and
   `depmod_toolchain_worker_descriptor()`, matching `pseries_fadump.py`'s shape. Both are appended
   to the single `local-libvirt` contribution in `contributions/multiarch_gdb.py` — not a second
   contribution (ADR-0635 §3).
4. **The allowlist.** One additive entry in `result_codec._ALLOWED_IDS` plus its import. No
   restructuring — #2344 owns that.
5. **One operator-doc line.** `docs/operating/install.md` already carries a `guest_arch_accel`
   note; the new check's remedy is "install kmod", so it gets the same treatment — one sentence
   naming the check, the package, and the fact that the search is the four directories, not
   `PATH`. This settles the charter's conditional install.md item: a line is owed.

**Deployments this changes.** The shipped container fails the new check — `Dockerfile`'s runtime
stage is `python:3.14.6-slim-bookworm` and installs no `kmod`. That is the honest verdict, and the
`Dockerfile` fix is deferred to
[debt 0012](../../debt/0012-shipped-worker-image-has-no-depmod.md); the check is not softened to
`not_applicable` to make it green.

**Out of scope** (operator-approved 2026-09-07, `WORK:SCOPE` token `q2339-a036f228`): guest-side
in-VM `depmod` (ADR-0346); an operator override for a `depmod` outside the four directories
(#2340); `virsh`/`qemu-img`/`virt-customize` resolution (#2333); widening the worker gate's
environment allowlist (#2340); restructuring the allowlist (#2344); folding
`bootstrap_elf._TOOL_PATH` into the shared module (follow-up candidate).

## Threat model

The change moves a security-relevant constant and adds one read-only probe. It adds no boundary.

1. **Boundaries.** None added. Two existing ones move. (a) The worker host's filesystem is read
   from in a new place, via `shutil.which` over a fixed directory list. (b) The worker→dispatcher
   inline-result boundary is **widened**: `result_codec._ALLOWED_IDS` is the validation allowlist
   the dispatcher applies to a payload the worker produced, and it gains one member. The
   `ops.diagnostics` entry point itself is unchanged — no new parameter, caller, or gate.
2. **Actors.** The caller holds `platform_operator` (`require_platform_role` in
   `mcp/tools/ops/diagnostics.py`); the denial path is unchanged. The untrusted party that matters
   is anyone who can write to a directory on the search path, since `depmod` is exec'd by the
   worker slot account with authority over guest overlays. The design trusts the four root-owned
   directories and nothing else.
3. **Controls.** The search path is a module constant with no operator input and no environment
   read, so nothing untrusted reaches it; moving it preserves that byte-for-byte, including the
   exclusion of `/usr/local/{sbin,bin}` (group-writable by default on part of the Debian family)
   and the comment recording why. The probe resolves a path and never execs it, and the verdict
   discloses only fixed directory names and a resolved path under one of them. For boundary (b),
   the allowlist gains one module constant — no operator, config, or network input reaches it —
   `_reconstruct` still re-runs every `CheckResult` invariant on the admitted item, an unrecognised
   id still degrades to a per-item `error` rather than poisoning the batch, and
   `test_allowed_ids_matches_registered_worker_vantage_descriptors` fails if the allowlist and the
   registered descriptors ever disagree.
4. **Out of scope.** A hostile `depmod` in a root-owned directory (that actor already has root);
   widening or overriding the search set (#2340); whether the resolved binary is genuinely
   `depmod` — the check reports resolution, not provenance, as the run path does.

## Success

1. `ops.diagnostics` returns a `depmod_toolchain` item, provider `local-libvirt`, where the local
   contribution is enabled.
2. Where `depmod` resolves, the item is `pass` and its `detail` names the resolved path.
3. Where it does not, the item is `fail`, `failure_category` is `missing_dependency`, `detail`
   names `depmod` and all four directories, and `fix` names `kmod` and the alternative.
4. The directories reported are the ones the run path searches — both sides import from one
   module, so no test or convention is needed to keep them equal. A `pass` means `depmod`
   resolves, not that an install will succeed; the check does not exec the binary.
5. A `depmod_toolchain` result survives the worker→dispatcher inline codec; an unregistered id
   still degrades to a per-item `error`.
6. `_resolve_depmod` is behaviourally unchanged: same resolution, message, category, and
   `details={"searched": ...}`.
7. `docs/operating/install.md` names the check, the `kmod` package, and the four-directory search.
8. `just ci` is green.

## Validation

The per-contract inventory — each entry's mode, test case, expected red observation, and exact
green command — lives in the plan's two **Verification** sections, which is where the implementer
meets it. The map from Success criteria to that inventory:

- Success 1 (the item exists, one dispatcher) — Task 2, `::test_check_id_and_vantage` plus the
  extended `test_service.py` and `test_default_factory.py` id sets. Success 2 and 3 (the two
  verdicts) — `::test_resolved_depmod_passes` and
  `::test_missing_depmod_fails_naming_the_searched_dirs`.
- Success 4 — Task 2's `::test_default_probe_searches_the_shared_dirs` for the probe. The
  structural half (both modules importing from one source rather than holding copies) is Task 1's
  single `task-test-not-applicable` entry: divergence is not expressible, so no assertion can fail
  on it.
- Success 5 (codec) — Task 2, `::test_depmod_toolchain_id_survives_roundtrip` and the existing
  `::test_allowed_ids_matches_registered_worker_vantage_descriptors`, which goes red the moment
  either half of the registration lands alone.
- Success 6 (`_resolve_depmod` unchanged) — Task 1, the existing
  `test_module_indexing.py::test_run_host_depmod_unresolvable_names_searched_directories`, kept
  green unmodified. Success 7 (operator doc) is `task-test-not-applicable` — prose whose only
  consumers are `just docs-links` / `docs-paths` — and Success 8 is `just ci`, Task 2's gate step.
- ADR-0635 and this spec. Mode: task-test-not-applicable — a decision record and a design document
  have no executable consumer beyond `scripts/guards/check_adr_status.py`, which the guardrail
  suite runs over every ADR; a test over their prose would assert wording, not a contract.
