# Implementation plan — container image version provenance (#1211)

Derived from the hardened spec `2026-07-16-container-image-version-provenance-1211.md` and
[ADR-0370](../adr/0370-container-image-version-provenance.md). Branch:
`fix/image-version-provenance-1211` off `main`.

**Guardrails (run the relevant ones per task; full set before push):**
`just lint` · `just type` · `just lint-shell` · `just lint-workflows` · `just test` ·
plus the CI-gated doc guards (`just adr-status-check`, `docs-links`, `docs-paths`, `docs-check`).

TDD order: Task 1 (script + unit test) → Task 2 (Dockerfile/.dockerignore) → Task 3
(release-image.yml) → Task 4 (ci.yml wiring + image-smoke assertion + structural guard). Tasks 2
and 3 have no unit-testable behavior on their own; Task 4 is what proves them end-to-end.

---

## Task 1 — `stamp-buildinfo.sh`: explicit-commit override + pinned SHA width

**Where it fits:** the container build (Task 2) has no `.git`, so the commit must be conveyed in.
The stamp script stays the single source of the `_buildinfo.py` format for both the wheel path
(git-derived) and the container path (arg-derived).

**Change** `scripts/stamp-buildinfo.sh`:
- Derive `commit` as `commit="${KDIVE_BUILDINFO_COMMIT:-$(git -C "$repo_root" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)}"`.
  Use the `${VAR:-default}` form so the `git` subprocess is **not** evaluated when the override is
  set — the builder stage's slim base has no `git` binary, and the override path must not depend
  on one.
- Pin the abbreviation to `--short=12` (was `--short`, auto length) so the git-derived width is
  deterministic across shallow and full clones.
- Leave the `RELEASE` handling (`$1`, the `true|false` validation, the file heredoc) unchanged.

**Test** — new `tests/scripts/test_stamp_buildinfo.py` (model on `tests/scripts/test_*.py`
subprocess pattern):
- Run the script in a `tmp_path` with a fixed sentinel commit (e.g. `KDIVE_BUILDINFO_COMMIT` set to
  the literal `deadb-eef-fake`, any non-empty token) and `"true"` → assert the written
  `_buildinfo.py` contains that exact `COMMIT` value and `RELEASE = True`; with `"false"` →
  `RELEASE = False`. Point the script at a throwaway target (copy it into
  `tmp_path/scripts/` with a `tmp_path/src/kdive/` tree, or assert against `git_root/src/kdive/`
  and restore) — the existing recipe removes the file via trap, so a test that writes the repo's
  real `_buildinfo.py` must clean it up. Prefer running the script from a copied minimal tree in
  `tmp_path` so the repo working tree is never touched.
- Assert the override path does **not** require `git`: run with `PATH` scrubbed of `git`
  (`env={"PATH": "/nonexistent", "KDIVE_BUILDINFO_COMMIT": "<sentinel>"}`, plus a shell) and
  confirm it still writes that sentinel `COMMIT` and exits 0. (If scrubbing PATH breaks the
  shell interpreter, instead assert the generated COMMIT equals the override regardless of the
  repo's real HEAD — proving the override wins over git.)
- Optionally: feed the generated file into `full_version()` by importing it, to prove the round
  trip renders `X.Y.Z+g<sha>` / `-dev` — but `tests/test_version.py` already covers the
  render given a `VersionInfo`, so this is optional belt-and-suspenders, not required.

**Acceptance:** `just lint-shell` clean (shellcheck + `shfmt -i 2`); new test green under
`just test`; all existing `tests/test_version.py` cases still green.

**Rollback:** revert the one-line `commit=` change; the `${VAR:-...}` form and `--short=12` are
self-contained.

---

## Task 2 — Dockerfile: bake `_buildinfo.py` from build args; ignore the leftover

**Where it fits:** produces the baked module inside the image so the runtime
`from kdive import _buildinfo` resolves through `PYTHONPATH=/app/src`.

**Change** `Dockerfile` (builder stage, after the project sync `RUN … uv sync … --group live`,
line ~60 — placed here so a changing commit never busts the `uv sync` cache):
```dockerfile
ARG KDIVE_COMMIT=""
ARG KDIVE_RELEASE="false"
# Bake version provenance (ADR-0370): a hermetic build has no .git, so the commit + release
# flag come in as build args and stamp _buildinfo.py, which rides into the final image on the
# existing `COPY --from=builder /app/src`. Skipped when no arg is passed (local/ci PR build),
# leaving today's live-git-less `X.Y.Z-dev` behaviour.
RUN if [ -n "$KDIVE_COMMIT" ]; then \
      KDIVE_BUILDINFO_COMMIT="$KDIVE_COMMIT" ./scripts/stamp-buildinfo.sh "$KDIVE_RELEASE"; \
    fi
```
The builder stage already has `scripts/` and `src/` from `COPY . .`, and `repo_root` resolves to
`/app`, so the file lands at `/app/src/kdive/_buildinfo.py`. No change to the final stage — its
existing `COPY --from=builder /app/src /app/src` carries the file.

**Change** `.dockerignore`: add `src/kdive/_buildinfo.py` (a developer's leftover local stamp must
never be copied by `COPY . .` and baked stale; the build-arg path is the only authoritative
source).

**Acceptance:** the image builds (proven by Task 4's ci.yml build); with a build-arg, a
`docker run <img> python -m kdive --version` reports `X.Y.Z-dev+g<sha>` (RELEASE=false) — asserted
by Task 4's smoke test. No unit-test surface of its own.

**Rollback:** remove the ARG/RUN block and the `.dockerignore` line; the image reverts to
reporting `X.Y.Z-dev`.

---

## Task 3 — `release-image.yml`: pass the build args

**Where it fits:** the release/edge publish path is where the commit + release flag are known.

**Change** `.github/workflows/release-image.yml`:
- Before the `Build and push` step, add a step that resolves the short SHA to an output:
  ```yaml
  - name: Resolve build provenance
    id: prov
    run: echo "sha=$(git rev-parse --short=12 HEAD)" >> "$GITHUB_OUTPUT"
  ```
- In the `Build and push` step (`docker/build-push-action`), add:
  ```yaml
  build-args: |
    KDIVE_COMMIT=${{ steps.prov.outputs.sha }}
    KDIVE_RELEASE=${{ startsWith(github.ref, 'refs/tags/v') }}
  ```
This covers both existing triggers: a `main` push → `RELEASE=false` → `-dev+g<sha>`; a `vX.Y.Z`
tag → `RELEASE=true` → `+g<sha>`. The checkout is shallow (depth 1) but HEAD exists, so
`rev-parse` works; `--short=12` makes the width match the wheel's.

**Acceptance:** `just lint-workflows` (actionlint) clean; `zizmor .github/workflows/release-image.yml`
raises no new finding (a plain `git rev-parse` run step and static build-args add no injectable
surface — `github.ref` is used only inside a GitHub expression, not interpolated into the run
script). No new secret or permission is needed.

**Rollback:** remove the `prov` step and the `build-args` block.

---

## Task 4 — CI end-to-end smoke + structural wiring guard

**Where it fits:** closes the silent-failure surface (Finding 1 of the spec review) — nothing
else proves the baked file survives the multi-stage copy and is read by the running image.

**Change** `.github/workflows/ci.yml` `Build image (no push)` step: add the same
`Resolve build provenance` step as Task 3 (id `prov`, `git rev-parse --short=12 HEAD`) before it,
then add to the build step:
```yaml
build-args: |
  KDIVE_COMMIT=${{ steps.prov.outputs.sha }}
  KDIVE_RELEASE=false
```
The stamp writes the arg **verbatim**, so pass the 12-char value from `prov`, not the 40-char
`github.sha`. A PR build is never a release, so `KDIVE_RELEASE` is always `false` here — which is
exactly what makes the smoke test able to assert the `-dev` shape deterministically.

**Change** `tests/image/test_image_smoke.py`: add
```python
import re

def test_version_reports_baked_provenance() -> None:
    res = _run(_image(), "python", "-m", "kdive", "--version")
    assert res.returncode == 0, res.stderr
    # CI builds this image with KDIVE_COMMIT + KDIVE_RELEASE=false, so the running
    # binary must self-report a dev build with a 12-hex baked commit (ADR-0370) —
    # proving _buildinfo.py survived the multi-stage COPY and PYTHONPATH import.
    assert re.match(r"^kdive \d+\.\d+\.\d+-dev\+g[0-9a-f]{12}$", res.stdout.strip()), res.stdout
```
This runs only when `KDIVE_IMAGE` + `docker` are present (the module's `pytestmark` skipif), i.e.
in the CI `image-build` job and any local build — it skips cleanly in the plain unit run.

**Change** new `tests/test_image_provenance_wiring.py` — a structural guard that runs in the unit
gate (no docker):
- Read `Dockerfile`; assert it contains a `KDIVE_BUILDINFO_COMMIT=` stamp invocation of
  `scripts/stamp-buildinfo.sh` guarded by a `KDIVE_COMMIT` non-empty check, and that `.dockerignore`
  lists `src/kdive/_buildinfo.py`.
- Read `.github/workflows/release-image.yml` and `.github/workflows/ci.yml`; assert each passes
  `KDIVE_COMMIT=` and `KDIVE_RELEASE=` build-args to the build step.
- Keep the assertions token-level and documented so an intentional refactor updates the guard
  deliberately (the guard's failure message should say "ADR-0370 wiring").

**Acceptance:** `just lint-workflows` clean; `just test` green (the wiring guard runs and passes;
the image-smoke `--version` case skips locally, runs in CI); on a PR, the CI `image-build` job's
smoke step passes, asserting the built image reports `X.Y.Z-dev+g<12 hex>`.

**Rollback:** remove the two test additions and the ci.yml `build-args`; the structural guard and
smoke assertion are self-contained.

---

## Cross-task verification (before PR)

- `just lint && just type && just lint-shell && just lint-workflows && just test` all green.
- `just adr-status-check`, `just docs-links`, `just docs-paths`, `just docs-check` green.
- `zizmor .github/workflows/` raises no new finding for the two edited workflows.
- Manual (optional, local, since this host builds images): `docker build --build-arg
  KDIVE_COMMIT=$(git rev-parse --short=12 HEAD) --build-arg KDIVE_RELEASE=false -t kdive:prov . &&
  docker run --rm kdive:prov python -m kdive --version` → `kdive X.Y.Z-dev+g<12 hex>`; repeat with
  `KDIVE_RELEASE=true` → `kdive X.Y.Z+g<12 hex>`.

## Out of scope (restated)

The operator `v0.3.0` tag event; the post-release `begin <next>-dev` bump; any change to
`full_version()` rendering or the wheel/sdist path.
