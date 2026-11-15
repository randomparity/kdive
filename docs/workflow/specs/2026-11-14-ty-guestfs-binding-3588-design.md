# Type-check against the libguestfs binding in CI (#3588) — design

## Problem

`just type` (`uv run ty check`) runs in the CI `checks` job (`lint · type · guards`) after
`uv sync --locked`, with no libguestfs Python binding. The binding is a distro package
(`python3-guestfs`), not a PyPI wheel, so ty treats `import guestfs` as unresolved, the
per-site `# ty: ignore[unresolved-import]` directives absorb it, and every value derived from
the binding is `Unknown`. A type error that exists only when the binding's annotations are
visible passes CI and fails the pre-push gate on a host that has the binding. That happened at
`f7143cadc`: `GuestFS.inspect_get_mountpoints` is annotated
`Union[List[Tuple[str, str]], dict]`, and `baseline_kernel.py` needed a `cast` (PR #3576).

## Scope

One change to the `checks` job in `.github/workflows/ci.yml`, immediately before its
`Type check` step: a new step installs `python3-guestfs` through the bounded
`scripts/apt-install.sh` and symlinks only the binding's pure-Python wrapper,
`/usr/lib/python3/dist-packages/guestfs.py`, into the job's locked `.venv` site-packages.
`just type` and the `justfile` stay unchanged; the type gate stays a single step in the same
job, checking the same locked venv.

Why the wrapper alone is enough: ty reads annotations from source and does not import
modules. The wrapper carries every `GuestFS` method annotation; its C module
(`libguestfsmod`) is built for the distro interpreter and is irrelevant to static checking.
Verified on Fedora 44 (python3-libguestfs 1.60.1, ty 0.0.62): with only `guestfs.py` on the
search path, a probe assigning `inspect_get_mountpoints(...)` to `dict[str, str]` fails with
`invalid-assignment`, and passes without the wrapper.

The step sets `KDIVE_APT_TIMEOUT_S=180`: a healthy install of the package's 252-package
closure took about 60 s in `ubuntu:26.04`, equal to the script's default budget, and `live.yml`
already uses 180 s for a superset. The `checks` job's `timeout-minutes` rises from 20 to 35 to
hold the worst case of both bounded installs (three attempts each: about 17 min here plus about
8 min for `libvirt-dev`).

The step fails closed: if the package installs no wrapper at that path, or the venv
interpreter cannot then locate the `guestfs` module, the step exits non-zero, so the job
cannot report a type check that ran without the binding.

Supporting edits: the `pyproject.toml` ty override comment and the `AGENTS.md` ty bullet
state that libguestfs is absent from CI; both are corrected (drgn stays absent). The override
itself is unchanged: the guestfs ignores are now unused in CI too, which
`unused-ignore-comment = "ignore"` already tolerates, and they stay used on hosts without the
binding. A guard test in `tests/guards/test_ci_job_graph.py` pins the step, its order before
`Type check`, its fail-closed checks, its apt budget, and the job timeout.

Considered and rejected:

- **Typed stub for the used surface** (`.pyi` checked into the repo). judgment: a hand-written
  stub is a second copy of the binding's API that drifts from the real one; the defect in
  #3576 was exactly a signature the project had not modelled.
- **Separate type job syncing against `/usr/bin/python3`** (live.yml's pattern).
  judgment: the system-interpreter sync exists so the C module can be *imported*, which static
  checking never does; it would add a second runner and a second venv shape for the one gating
  type check.
- **Do nothing.** judgment: the failure reaches main and surfaces only on some hosts, late.

### Failure model

1. Actors and deployments
   - GitHub Actions `ci.yml` on GitHub-hosted `ubuntu-latest`, for pull requests and pushes to
     main. `ubuntu-latest` resolved to `ubuntu-26.04` on the main run of 2026-11-14 (job log
     `Image: ubuntu-26.04`); the wrapper path is the same on 24.04.
2. Invariants and assets at stake
   - The required `lint · type · test` check: it must not go green when the binding was not
     visible to ty (fail closed), and must not go red for reasons unrelated to the change
     beyond the existing apt retry budget.
   - `just type` remains the one gating type check, over the locked venv.
3. Accepted failure classes
   - Annotation skew between the runner's libguestfs (1.58.1 on `ubuntu:26.04`) and a
     worker's (Fedora 44 ships 1.60.1): bounded — the wrapper is generated from one API
     description and signature changes are rare; a host-only difference still fails that host's
     pre-push gate, as today.
   - About 93 MB / 252 packages of appliance dependencies (`apt-get install -s
     --no-install-recommends python3-guestfs` in `ubuntu:26.04`) installed in `checks`: accepted
     runner cost; `checks` (~1.5 min) is off the critical path (`pytest (other)`, ~17 min).
4. Covered elsewhere
   - apt stalls and mirror failures: `scripts/apt-install.sh` bound and retry (ADR-0566, ADR-0571).
   - Runtime import of the binding: `live.yml` (unchanged, excluded).
   - drgn typing: excluded. Python 3.15 lane: #3600.

### Threat model

- Boundary: the job installs a distro package from the runner's configured Ubuntu archive
  (apt signature verification); no new credentials, permissions, or network hosts.
- Actor: a pull-request author controls the workflow only through the PR itself; the job keeps
  `permissions: contents: read` and `persist-credentials: false`.
- Out of scope: a compromised Ubuntu archive — held by apt's signed metadata, as for the
  existing `libvirt-dev` install.

## Success

1. On a CI run of the PR head, the `checks` job's new step succeeds and `Type check` passes.
2. On a CI run with a deliberately injected binding-only type error (the `cast` removed from
   `mountpoints = cast(dict[str, str], guest.inspect_get_mountpoints(...))` in
   `baseline_kernel.py`, together with the then-unused `from typing import cast`), every step
   before `Type check` succeeds and `Type check` fails with `unresolved-attribute` on
   `mountpoints.get` (`list[tuple[str, str]]` has no `get`). The proof branch is not merged; its
   run URL is reported.
3. `just type` is still the only gating type step, and the guard test passes.

## Validation

- Guard test: `Mode: focused-test` — `tests/guards/test_ci_job_graph.py` asserts the step
  exists in `checks`, runs `./scripts/apt-install.sh python3-guestfs`, links `guestfs.py` into
  the venv purelib, checks `find_spec("guestfs")`, sets the 180 s apt budget, precedes
  `Type check`, and that `checks` has `timeout-minutes` of at least 35. Red: removing the
  step fails the test. Green: `just test-verbose tests/guards/test_ci_job_graph.py`.
- CI behaviour: `Mode: task-test-not-applicable` locally — the binding path is the runner's;
  proven by Success 1 and 2 on real CI runs.
- Doc comments: `Mode: task-test-not-applicable` — prose with no executable consumer.

Plan: [2026-11-14-ty-guestfs-binding-3588](../plans/2026-11-14-ty-guestfs-binding-3588.md).
