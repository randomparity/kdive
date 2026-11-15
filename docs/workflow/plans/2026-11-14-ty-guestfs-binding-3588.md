# Type-check against the libguestfs binding in CI (#3588) — plan

Goal: CI's gating `just type` sees the libguestfs binding's annotations, so a binding-only type
error fails CI. Spec: [design](../specs/2026-11-14-ty-guestfs-binding-3588-design.md).

Architecture: one new step in the `checks` job of `.github/workflows/ci.yml`, between
`Sync dependencies` and `Type check`, installs `python3-guestfs` through
`scripts/apt-install.sh` and symlinks `/usr/lib/python3/dist-packages/guestfs.py` into the
locked `.venv`'s purelib. `just type` is unchanged.

Tech stack: GitHub Actions YAML, Bash, pytest + PyYAML guard tests.

Expected implementation size: 45–70 changed lines (M) — one workflow step plus a job timeout
change (~25), one guard test (~20), two comment edits (~15).

## Global Constraints

- Python `==3.14.*`; ty `0.0.62`; uv `0.11.18` (unchanged pins).
- Every apt install in a workflow goes through `./scripts/apt-install.sh` (ADR-0566,
  `tests/guards/test_apt_install_is_bounded.py`); every job declares `timeout-minutes` (ADR-0571).
- `just type` stays whole-tree and stays the only gating type step (AGENTS.md).
- Prose: plain and factual; no "critical", "robust", "comprehensive", "elegant".
- Do not edit `.github/workflows/live.yml`, the ty override `include` list or rules, or drgn
  handling (excluded by WORK:SCOPE).

## File map

| File | Change |
|---|---|
| `.github/workflows/ci.yml` | `checks`: new step before `Type check`; `timeout-minutes` 20 → 30 |
| `tests/guards/test_ci_job_graph.py` | new test pinning the step |
| `pyproject.toml` | ty override comment: libguestfs is visible in CI's type check; drgn is not |
| `AGENTS.md` | ty bullet: same correction |

## Task 1 — expose the binding to the gating type check

Interfaces: consumes the existing `checks` steps `Sync dependencies` (creates `.venv`) and
`Type check` (`run: just type`). Produces step name
`Expose the libguestfs binding to the type check`, which the guard test looks up.

Verification:

- Contract: the `checks` job makes the binding wrapper resolvable before `Type check`, fail
  closed. `Mode: focused-test` — `tests/guards/test_ci_job_graph.py::
  test_type_check_sees_the_libguestfs_binding`. Red: before the workflow edit it fails with
  `StopIteration` (no step of that name). Green: `just test-verbose
  tests/guards/test_ci_job_graph.py` passes, and `just test-verbose
  tests/guards/test_apt_install_is_bounded.py` still passes.
- Contract: the comments in `pyproject.toml` and `AGENTS.md`. `Mode: task-test-not-applicable` —
  prose with no executable consumer.

Steps:

1. Add to `tests/guards/test_ci_job_graph.py`:

   ```python
   def test_type_check_sees_the_libguestfs_binding() -> None:
       """`just type` must resolve the guestfs annotations, or binding-only errors pass CI (#3588)."""
       steps = _jobs()["checks"]["steps"]
       names = [step["name"] for step in steps]
       expose = next(
           s for s in steps if s["name"] == "Expose the libguestfs binding to the type check"
       )
       position = names.index(expose["name"])
       assert names.index("Sync dependencies") < position < names.index("Type check")
       run = expose["run"]
       assert "./scripts/apt-install.sh python3-guestfs" in run
       assert "wrapper=/usr/lib/python3/dist-packages/guestfs.py" in run
       assert 'if [ ! -f "$wrapper" ]' in run
       assert 'ln -sfn "$wrapper" "$site/guestfs.py"' in run
       assert 'find_spec("guestfs") is None' in run
   ```

2. Run `just test-verbose tests/guards/test_ci_job_graph.py`; expect the new test to fail
   with `StopIteration`.
3. In `.github/workflows/ci.yml`, `checks` job: set `timeout-minutes: 30` and update its
   comment to name both bounded apt installs; insert immediately before `- name: Type check`:

   ```yaml
         - name: Expose the libguestfs binding to the type check
           # <why: ty needs the wrapper's annotations; wrapper only; fail closed; budget>
           env:
             KDIVE_APT_TIMEOUT_S: "180"
           run: |
             ./scripts/apt-install.sh python3-guestfs
             wrapper=/usr/lib/python3/dist-packages/guestfs.py
             if [ ! -f "$wrapper" ]; then
               echo "::error::python3-guestfs installed no $wrapper; ty cannot see the binding" >&2
               exit 1
             fi
             site="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
             ln -sfn "$wrapper" "$site/guestfs.py"
             .venv/bin/python -c 'import importlib.util as u, sys; sys.exit(u.find_spec("guestfs") is None)'
   ```

4. Run `just test-verbose tests/guards/test_ci_job_graph.py
   tests/guards/test_apt_install_is_bounded.py`; expect all pass. Run `just lint-workflows`
   (actionlint); expect exit 0.
5. Edit the `pyproject.toml` override comment and the `AGENTS.md` ty bullet to say the CI type
   check installs the libguestfs wrapper, so its ignores are unused there too, while drgn stays
   absent. Run `just lint` and `just type`; expect exit 0.
6. Commit: `ci(type): resolve the libguestfs binding in the gating ty check`.

## Task 2 — prove it on CI

Verification: `Mode: task-test-not-applicable` locally — the wrapper path exists only on the
runner; the proof is the CI run itself (spec Success 1–2).

1. Record the green `lint · type · guards` run on the PR head.
2. Create throwaway branch `proof/ty-guestfs-3588` from the PR head. In
   `src/kdive/providers/local_libvirt/lifecycle/rootfs/baseline_kernel.py` replace
   `mountpoints = cast(dict[str, str], guest.inspect_get_mountpoints(roots[0]))` with
   `mountpoints = guest.inspect_get_mountpoints(roots[0])`, commit
   `test: inject a binding-only type error (proof, do not merge)`. Confirm `just type` passes
   locally (this host's venv has no binding, which is the defect being fixed).
3. Push the branch (pre-push gate runs normally) and start `gh workflow run ci.yml --ref
   proof/ty-guestfs-3588`. Expect `lint · type · guards` red at `Type check` with a ty
   attribute diagnostic on `mountpoints.get` (the `list[tuple[str, str]]` arm has no `.get`).
4. Record the run URL, then delete the remote and local proof branch.
5. Put both run URLs in the PR body.
