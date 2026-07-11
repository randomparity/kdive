# Decouple `migrate()` + deploy-time `systems.toml` validation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Helm "migrate" Job mean only SQL migration, re-home the build-config seed to its own command + hook, and add a deploy-time fail-fast `systems.toml` validator — while the reconciler stays fail-open at runtime.

**Architecture:** `admin/bootstrap.migrate()` drops to SQL-only; the build-config seed moves to a new `seed-build-configs` CLI command + `post-*` Helm hook; `reconcile-systems` gains a `--check` validate-only mode (no DB/S3) wired as a `pre-*` Helm hook weighted before migrate that aborts the upgrade on a malformed file. No schema change, migration, or new MCP tool.

**Tech Stack:** Python 3.13 (`uv`, `ruff`, `ty`, `pytest`), argparse CLI (`src/kdive/__main__.py`), Helm chart (`deploy/helm/kdive`), `pytest`-driven `helm template` render tests.

**Spec:** [`../../archive/design/decouple-migrate-validate-systems.md`](../../archive/design/decouple-migrate-validate-systems.md) · **ADR:** [`../../adr/0121-decouple-migrate-validate-systems.md`](../../adr/0121-decouple-migrate-validate-systems.md)

**Guardrails (run before every commit):** `just lint` · `just type` (whole tree) · the focused test for the task. Before the final push run the full `just test` plus `just docs-links docs-paths docs-check`. CI runs these recipes individually.

---

## File Structure

- `src/kdive/admin/bootstrap.py` — `migrate()` becomes SQL-only; remove `_reconcile_inventory_images`/`_reconcile_image_store`/`_NoS3HeadStore`; add public `seed_build_configs_step()`.
- `src/kdive/inventory/reconcile_cli.py` — add `validate_systems(path) -> int` (parse-only, no pool/store).
- `src/kdive/__main__.py` — add `--check` to `reconcile-systems`; add the `seed-build-configs` command.
- `deploy/helm/kdive/templates/job-migrate.yaml` — drop the systems ConfigMap mount/env.
- `deploy/helm/kdive/templates/job-validate-systems.yaml` — **new** pre-`*` fail-fast hook.
- `deploy/helm/kdive/templates/job-seed-build-configs.yaml` — **new** post-`*` seed hook.
- `tests/admin/test_bootstrap.py` — rewrite the four migrate tests; add seed-step tests.
- `tests/inventory/test_validate_systems.py` — **new** unit tests for `validate_systems`.
- `tests/helm/test_helm_render.py` — name-aware hook helper + render assertions for all three Jobs.
- `docs/operating/runbooks/kubernetes-deploy.md` — document the validator, fail-fast policy, seed recovery, AC#2 `kubectl logs`.

---

## Task 1: `migrate()` is SQL-only; expose `seed_build_configs_step()`

**Files:**
- Modify: `src/kdive/admin/bootstrap.py:42-169`
- Test: `tests/admin/test_bootstrap.py:103-194`

- [ ] **Step 1: Rewrite the four migrate tests to assert SQL-only**

In `tests/admin/test_bootstrap.py`, **delete** these four tests:
`test_migrate_reconciles_inventory_images_idempotently`,
`test_migrate_without_systems_toml_seeds_no_images`,
`test_migrate_without_s3_skips_build_config_seed`,
`test_migrate_with_s3_seeds_build_config`.
Replace them (keep the `_BASELINE_SYSTEMS_TOML`/`_write_baseline_systems_toml` helpers and the `_FakeStore` class — Task 3 reuses them) with:

```python
def test_migrate_is_sql_only(
    monkeypatch: pytest.MonkeyPatch, postgres_url: str, tmp_path: Path
) -> None:
    # migrate() applies the schema and nothing else: even with a systems.toml present and an
    # object store available, it creates no image_catalog config rows and no build_config rows.
    # Inventory reconcile is the reconciler's job (ADR-0112); the build-config seed is its own
    # command (ADR-0121). A failed "migrate" therefore always means SQL failed.
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(_write_baseline_systems_toml(tmp_path)))
    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", lambda: _FakeStore())

    applied = migrate(postgres_url)

    assert applied > 0  # the schema was migrated
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        images = conn.execute(
            "SELECT count(*) FROM image_catalog WHERE managed_by = 'config'"
        ).fetchone()
        configs = conn.execute("SELECT count(*) FROM build_config_catalog").fetchone()
    assert images is not None and images[0] == 0
    assert configs is not None and configs[0] == 0


def test_seed_build_configs_step_without_s3_returns_zero(
    monkeypatch: pytest.MonkeyPatch, postgres_url: str
) -> None:
    # No KDIVE_S3_* configured: the seed is a clean skip (ADR-0096), returns 0.
    from kdive.admin.bootstrap import migrate, seed_build_configs_step

    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for var in ("KDIVE_S3_ENDPOINT_URL", "KDIVE_S3_BUCKET", "KDIVE_S3_REGION"):
        monkeypatch.delenv(var, raising=False)
    migrate(postgres_url)

    assert seed_build_configs_step(postgres_url) == 0
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        configs = conn.execute("SELECT count(*) FROM build_config_catalog").fetchone()
    assert configs is not None and configs[0] == 0


def test_seed_build_configs_step_with_s3_seeds_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, postgres_url: str
) -> None:
    from kdive.admin.bootstrap import migrate, seed_build_configs_step

    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", lambda: _FakeStore())
    migrate(postgres_url)

    assert seed_build_configs_step(postgres_url) == 1
    assert seed_build_configs_step(postgres_url) == 0  # idempotent
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        row = conn.execute("SELECT name FROM build_config_catalog WHERE name = 'kdump'").fetchone()
    assert row is not None and row[0] == "kdump"
```

These three tests are the red→green cycle for both Task-1 functions (`migrate()` going SQL-only and the new `seed_build_configs_step` wrapper). The `_FakeStore` monkeypatch target (`kdive.store.objectstore.object_store_from_env`) matches the function-local `from kdive.store.objectstore import object_store_from_env` inside `_seed_build_configs_step`, which re-looks-up the name on the module at call time — the same pattern the pre-existing migrate tests used.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run python -m pytest tests/admin/test_bootstrap.py -k "is_sql_only or seed_build_configs_step" -q`
Expected: FAIL — `test_migrate_is_sql_only` because `migrate()` still seeds/reconciles (counts not 0); the two `seed_build_configs_step` tests with `ImportError: cannot import name 'seed_build_configs_step'`.

- [ ] **Step 3: Make `migrate()` SQL-only and remove the orphaned helpers**

In `src/kdive/admin/bootstrap.py`, replace `migrate()` (lines 42-54) with:

```python
def migrate(database_url: str | None = None) -> int:
    """Apply database migrations only (ADR-0121).

    Inventory reconcile is the reconciler loop's job (ADR-0112) and the build-config seed is the
    ``seed-build-configs`` command (ADR-0096) — both are deliberately *not* run here, so a failed
    "migrate" Job always means a SQL migration failed, never a config/bucket fault.

    Args:
        database_url: A psycopg connection string, or ``None`` to read ``KDIVE_DATABASE_URL``.

    Returns:
        The number of migrations applied.
    """
    url = database_url or config.require(DATABASE_URL)
    conn = psycopg.connect(url, autocommit=True)
    try:
        applied = apply_migrations(conn)
    finally:
        conn.close()
    print(f"applied {len(applied)} migration(s)")
    return len(applied)


def seed_build_configs_step(database_url: str | None = None) -> int:
    """Publish the packaged build-config fragments (the deploy ``seed-build-configs`` step).

    Re-homed out of ``migrate()`` (ADR-0121). S3-gated + idempotent: a wholly-unconfigured object
    store is a clean skip (returns 0); a configured-but-broken store (missing bucket, bad
    credentials) raises — a real object-store fault must surface, not be swallowed.

    Args:
        database_url: A psycopg connection string, or ``None`` to read ``KDIVE_DATABASE_URL``.

    Returns:
        The number of build-config fragments published (0 if already current or skipped).
    """
    url = database_url or config.require(DATABASE_URL)
    seeded = _seed_build_configs_step(url)
    print(f"seeded {seeded} build-config fragment(s)")
    return seeded
```

Then **delete** `_reconcile_inventory_images` (lines 102-142), `_reconcile_image_store` (lines 145-161), and `_NoS3HeadStore` (lines 164-169). Keep `_seed_build_configs_step` and `_run_async_db_step`.

Remove now-unused imports: in the `from kdive.config.core_settings import ...` line drop `SYSTEMS_TOML` (keep `DATABASE_URL`); remove `from typing import Any` if `Any` is now unused (it was only used by `_reconcile_image_store`'s return annotation). Leave `from pathlib import Path` (used by `install_fixtures`/`_refuse_existing`) and `from collections.abc import Awaitable, Callable, Mapping, Sequence` (still used).

- [ ] **Step 4: Run the test + guardrails**

Run: `uv run python -m pytest tests/admin/test_bootstrap.py -q && just lint && just type`
Expected: PASS; ruff reports no unused-import warnings; `ty` clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/admin/bootstrap.py tests/admin/test_bootstrap.py
git commit -m "refactor(bootstrap): make migrate() SQL-only, expose seed_build_configs_step (#440)"
```

---

## Task 2: `validate_systems()` + `reconcile-systems --check`

**Files:**
- Modify: `src/kdive/inventory/reconcile_cli.py:33-78`
- Modify: `src/kdive/__main__.py:192-225`
- Test: `tests/inventory/test_validate_systems.py` (create)

- [ ] **Step 1: Write the failing unit tests**

Create `tests/inventory/test_validate_systems.py`:

```python
"""Unit tests for the `reconcile-systems --check` validate-only path (#440, ADR-0121)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.inventory.reconcile_cli import validate_systems

_VALID = """schema_version = 2

[[image]]
provider = "local-libvirt"
name = "fedora-kdive-ready-43"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
capabilities = ["kdive-ready-console", "ssh", "drgn"]
[image.source]
kind = "s3"
object_key = "rootfs/local/fedora-kdive-ready-43.qcow2"
"""


def test_validate_valid_file_returns_zero(tmp_path: Path) -> None:
    path = tmp_path / "systems.toml"
    path.write_text(_VALID, encoding="utf-8")
    assert validate_systems(path) == 0


def test_validate_malformed_file_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "systems.toml"
    path.write_text("this is = not valid toml [[", encoding="utf-8")
    assert validate_systems(path) == 1
    assert "error:" in capsys.readouterr().err

def test_validate_missing_explicit_path_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "absent.toml"
    assert validate_systems(missing) == 1
    # The InventoryError "cannot read" message names the path (actionable for the ConfigMap
    # key-mismatch case the validate hook hits, too).
    assert str(missing) in capsys.readouterr().err


def test_validate_absent_default_path_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # path=None resolves the default KDIVE_SYSTEMS_TOML; an absent default is the gitignored
    # pre-config state -> no-op success (mirrors reconcile_systems / the reconciler loop).
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "absent.toml"))
    import kdive.config as config

    config.load()
    assert validate_systems(None) == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/inventory/test_validate_systems.py -q`
Expected: FAIL — `ImportError: cannot import name 'validate_systems'`.

- [ ] **Step 3: Implement `validate_systems` in `reconcile_cli.py`**

Add to `src/kdive/inventory/reconcile_cli.py` (after the `reconcile_systems` function, reusing the existing `_load_doc` and the `_EXIT_OK`/`_EXIT_INVENTORY_ERROR` constants):

```python
def validate_systems(path: Path | None) -> int:
    """Parse + schema-validate ``systems.toml`` with no DB/S3 access; return the exit code.

    The deploy-time fail-fast validator (ADR-0121): it touches neither Postgres nor the object
    store. An absent **default** path is a quiet no-op (exit 0, the gitignored pre-config state);
    a malformed/invalid file, or an explicit ``path`` to a missing file, is exit 1 with the
    :class:`~kdive.inventory.InventoryError` message (``entry.field: msg``, which names the path)
    on stderr.

    Args:
        path: An explicit ``systems.toml`` path, or ``None`` to use the default
            ``KDIVE_SYSTEMS_TOML`` path.

    Returns:
        ``0`` on a valid file (or an absent default), ``1`` on an ``InventoryError``.
    """
    try:
        _load_doc(path)
    except InventoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_INVENTORY_ERROR
    return _EXIT_OK
```

(No new imports needed — `sys`, `Path`, `InventoryError`, `_load_doc`, and the exit constants already exist in the module.)

- [ ] **Step 4: Run the unit tests**

Run: `uv run python -m pytest tests/inventory/test_validate_systems.py -q`
Expected: PASS (all four).

- [ ] **Step 5: Wire `--check` into the CLI**

In `src/kdive/__main__.py`, change `_add_reconcile_systems_arguments` (lines 192-200) to add the flag:

```python
def _add_reconcile_systems_arguments(parser: argparse.ArgumentParser) -> None:
    from pathlib import Path

    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="path to systems.toml (default: KDIVE_SYSTEMS_TOML, then ./systems.toml)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate systems.toml only (no DB/S3 writes); exit non-zero on a schema error",
    )
```

Change `_handle_reconcile_systems` (lines 203-225) to branch on `--check` **before** acquiring any store/pool:

```python
def _handle_reconcile_systems(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del secret_registry, telemetry
    from kdive.inventory.reconcile_cli import reconcile_systems, validate_systems

    if args.check:
        raise SystemExit(validate_systems(args.path))

    from kdive.store.objectstore import object_store_from_env

    store = _optional_reconciler_object_store(object_store_from_env)
    if store is None:
        raise SystemExit(
            "reconcile-systems requires an object store; set KDIVE_S3_ENDPOINT_URL / "
            "KDIVE_S3_BUCKET / KDIVE_S3_REGION (the pass HEADs s3 image objects)."
        )
    pool = create_pool(min_size=1)

    async def _run() -> int:
        await pool.open()
        try:
            return await reconcile_systems(args.path, pool=pool, store=store)
        finally:
            await pool.close()

    raise SystemExit(asyncio.run(_run()))
```

- [ ] **Step 6: Run focused tests + guardrails**

Run: `uv run python -m pytest tests/inventory/test_validate_systems.py tests/mcp/ops/test_reconcile_systems.py -q && just lint && just type`
Expected: PASS; clean.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/inventory/reconcile_cli.py src/kdive/__main__.py tests/inventory/test_validate_systems.py
git commit -m "feat(cli): add reconcile-systems --check validate-only mode (#440)"
```

---

## Task 3: `seed-build-configs` CLI command

**Files:**
- Modify: `src/kdive/__main__.py:147-259`
- Test: `tests/admin/test_bootstrap.py` (append)

> The `seed_build_configs_step` **behavior** tests (no-S3 skip, with-S3 seed + idempotent) live in Task 1, where the function is introduced and can fail-first. This task only adds the test for the **CLI command wiring** (the new failing-first surface here).

- [ ] **Step 1: Write the failing command-wiring test**

Append to `tests/admin/test_bootstrap.py`:

```python
def test_seed_build_configs_command_invokes_step(monkeypatch: pytest.MonkeyPatch) -> None:
    from kdive import __main__ as main_mod

    called: list[str] = []
    monkeypatch.setattr(
        "kdive.admin.bootstrap.seed_build_configs_step",
        lambda: called.append("seeded"),
    )
    main_mod.main(["seed-build-configs"])
    assert called == ["seeded"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/admin/test_bootstrap.py::test_seed_build_configs_command_invokes_step -q`
Expected: FAIL — `SystemExit: argument command: invalid choice: 'seed-build-configs'` (the subcommand is not registered yet).

- [ ] **Step 3: Add the `seed-build-configs` command handler**

In `src/kdive/__main__.py`, add a handler next to `_handle_migrate` (after line 153):

```python
def _handle_seed_build_configs(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del args, secret_registry, telemetry
    from kdive.admin.bootstrap import seed_build_configs_step

    seed_build_configs_step()
```

Add the command to the `_COMMANDS` tuple (after the `migrate` entry, line 234). It is **not** `runnable=True` — like `seed-demo`/`reconcile-systems` it reads config lazily (`seed_build_configs_step` calls `config.require(DATABASE_URL)`):

```python
    _Command(
        "seed-build-configs",
        "publish packaged build-config fragments to the object store",
        _handle_seed_build_configs,
    ),
```

- [ ] **Step 4: Run the wiring test + guardrails**

Run: `uv run python -m pytest tests/admin/test_bootstrap.py -q && just lint && just type`
Expected: PASS; clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/__main__.py tests/admin/test_bootstrap.py
git commit -m "feat(cli): add seed-build-configs command (#440)"
```

---

## Task 4: Helm — migrate Job drops the systems mount; name-aware render helper

**Files:**
- Modify: `deploy/helm/kdive/templates/job-migrate.yaml:56-68`
- Modify: `tests/helm/test_helm_render.py:122-168`

- [ ] **Step 1: Add a name-aware Job helper + the migrate-no-systems-volume test**

In `tests/helm/test_helm_render.py`, add a helper that indexes Jobs by name (the existing `_hooks_by_kind` collapses all three Jobs into one Kind="Job" entry, which is now ambiguous) and a test:

```python
def _jobs_by_name(*set_args: str) -> dict[str, dict[str, Any]]:
    """Index every rendered Job by its metadata.name suffix.

    Returns ``{name_suffix: {"phase", "weight", "volumes", "args"}}``. Name-keyed because the
    chart now renders three Jobs (migrate, validate-systems, seed-build-configs) and the
    Kind-keyed `_hooks_by_kind` cannot tell them apart.
    """
    res = _template(*set_args)
    assert res.returncode == 0, res.stderr
    jobs: dict[str, dict[str, Any]] = {}
    for doc in yaml.safe_load_all(res.stdout):
        if not (isinstance(doc, dict) and doc.get("kind") == "Job"):
            continue
        name = str(doc.get("metadata", {}).get("name", ""))
        ann = doc.get("metadata", {}).get("annotations", {}) or {}
        spec = doc["spec"]["template"]["spec"]
        container = spec["containers"][0]
        suffix = name.split("-kdive-", 1)[-1] if "-kdive-" in name else name
        jobs[suffix] = {
            "phase": ann.get("helm.sh/hook"),
            "weight": int(ann.get("helm.sh/hook-weight", "0")),
            "volumes": [v["name"] for v in spec.get("volumes", [])],
            "args": container.get("args", []),
        }
    return jobs


def test_migrate_job_has_no_systems_volume() -> None:
    # migrate() no longer reads systems.toml (ADR-0121), so the migrate Job must not mount the
    # systems ConfigMap even when one is configured.
    jobs = _jobs_by_name(
        "config.KDIVE_DATABASE_URL=postgresql://x/y", "systems.configMapName=my-systems"
    )
    assert "migrate" in jobs
    assert "kdive-systems" not in jobs["migrate"]["volumes"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/helm/test_helm_render.py::test_migrate_job_has_no_systems_volume -q`
Expected: FAIL — the migrate Job currently mounts `kdive-systems` when `systems.configMapName` is set. (If `helm` is not installed locally, the test SKIPs; note it and rely on CI which provides helm. State the limitation in the PR body.)

- [ ] **Step 3: Remove the systems mount from `job-migrate.yaml`**

In `deploy/helm/kdive/templates/job-migrate.yaml`, delete the `kdive.systemsEnv` include and the systems volume/mount blocks. The container `env`/`envFrom` and `volumes` become:

```yaml
      containers:
        - name: migrate
          image: {{ include "kdive.image" . }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          args: ["migrate"]
          envFrom:
            - configMapRef:
                name: {{ include "kdive.fullname" . }}-config
```

Delete the trailing `{{- if .Values.systems.configMapName }} volumes: ... {{- end }}` block (old lines 61-68) entirely. (The `env:` key with `kdive.systemsEnv` and the `volumeMounts:` block on the container also go.)

- [ ] **Step 4: Run the render test**

Run: `uv run python -m pytest tests/helm/test_helm_render.py::test_migrate_job_has_no_systems_volume -q`
Expected: PASS (or SKIP if helm absent).

- [ ] **Step 5: Commit**

```bash
git add deploy/helm/kdive/templates/job-migrate.yaml tests/helm/test_helm_render.py
git commit -m "feat(helm): drop systems ConfigMap mount from the migrate Job (#440)"
```

---

## Task 5: Helm — fail-fast `job-validate-systems.yaml` pre-upgrade hook

**Files:**
- Create: `deploy/helm/kdive/templates/job-validate-systems.yaml`
- Modify: `tests/helm/test_helm_render.py`

- [ ] **Step 1: Write the render tests**

Append to `tests/helm/test_helm_render.py`:

```python
def test_validate_hook_rendered_only_with_systems_configmap() -> None:
    without = _jobs_by_name("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert "validate-systems" not in without
    with_cm = _jobs_by_name(
        "config.KDIVE_DATABASE_URL=postgresql://x/y", "systems.configMapName=my-systems"
    )
    assert "validate-systems" in with_cm


def test_validate_hook_is_pre_upgrade_weighted_before_migrate() -> None:
    jobs = _jobs_by_name(
        "config.KDIVE_DATABASE_URL=postgresql://x/y", "systems.configMapName=my-systems"
    )
    v = jobs["validate-systems"]
    assert "pre-install" in v["phase"] and "pre-upgrade" in v["phase"]
    assert v["weight"] < jobs["migrate"]["weight"]  # runs before migrate
    assert v["args"][:2] == ["reconcile-systems", "--check"]
    assert "kdive-systems" in v["volumes"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/helm/test_helm_render.py -k validate_hook -q`
Expected: FAIL (`KeyError: 'validate-systems'`) — the template does not exist yet. (SKIP if helm absent.)

- [ ] **Step 3: Create `job-validate-systems.yaml`**

```yaml
{{- if .Values.systems.configMapName }}
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "kdive.fullname" . }}-validate-systems
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
  annotations:
    # Fail-fast deploy-time validation (ADR-0121): parse + schema-check the mounted systems.toml
    # with no DB/S3 access, BEFORE migrate (weight -10 < migrate 0) and before app rollout. A
    # malformed file aborts the upgrade with the precise field error in this pod's logs. The
    # reconciler stays fail-open (keep-last-good) at runtime — this gates only the deploy.
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "-10"
    "helm.sh/hook-delete-policy": before-hook-creation
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        {{- include "kdive.labels" . | nindent 8 }}
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
      containers:
        - name: validate-systems
          image: {{ include "kdive.image" . }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          args:
            - reconcile-systems
            - --check
            - --path
            - {{ printf "%s/%s" .Values.systems.mountPath .Values.systems.fileName | quote }}
          env:
            {{- include "kdive.systemsEnv" . | nindent 12 }}
          volumeMounts:
            {{- include "kdive.systemsVolumeMount" . | nindent 12 }}
      volumes:
        {{- include "kdive.systemsVolume" . | nindent 8 }}
{{- end }}
```

`backoffLimit: 0` — a validation failure must abort immediately, not retry three times against an unchanging file.

- [ ] **Step 4: Run the render tests**

Run: `uv run python -m pytest tests/helm/test_helm_render.py -k validate_hook -q`
Expected: PASS (or SKIP if helm absent).

- [ ] **Step 5: Confirm the existing pre-existing ConfigMap-ordering test still passes**

Run: `uv run python -m pytest tests/helm/test_helm_render.py -q`
Expected: PASS. The existing `test_external_configmap_is_a_pre_install_hook_before_migrate` uses `_hooks_by_kind`, which keys by Kind and now aliases three Jobs. If it fails (because it reads the wrong Job's weight), update that test to use `_jobs_by_name(...)["migrate"]["weight"]` for the migrate Job's weight instead of `_hooks_by_kind(...)["Job"]["weight"]`. Make that edit in the same commit.

- [ ] **Step 6: Commit**

```bash
git add deploy/helm/kdive/templates/job-validate-systems.yaml tests/helm/test_helm_render.py
git commit -m "feat(helm): add fail-fast systems.toml validate pre-upgrade hook (#440)"
```

---

## Task 6: Helm — `job-seed-build-configs.yaml` post-deploy hook

**Files:**
- Create: `deploy/helm/kdive/templates/job-seed-build-configs.yaml`
- Modify: `tests/helm/test_helm_render.py`

- [ ] **Step 1: Write the render tests**

Append to `tests/helm/test_helm_render.py`:

```python
def test_seed_build_configs_is_post_hook_after_migrate() -> None:
    jobs = _jobs_by_name("config.KDIVE_DATABASE_URL=postgresql://x/y")
    s = jobs["seed-build-configs"]
    assert "post-install" in s["phase"] and "post-upgrade" in s["phase"]
    assert s["weight"] > jobs["migrate"]["weight"]  # runs after migrate
    assert s["args"] == ["seed-build-configs"]
    assert "kdive-systems" not in s["volumes"]  # seed does not read systems.toml
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/helm/test_helm_render.py::test_seed_build_configs_is_post_hook_after_migrate -q`
Expected: FAIL (`KeyError: 'seed-build-configs'`). (SKIP if helm absent.)

- [ ] **Step 3: Create `job-seed-build-configs.yaml`** (mirrors `job-migrate.yaml`'s bundled `wait-for-db` initContainer, no systems volume)

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "kdive.fullname" . }}-seed-build-configs
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
  annotations:
    # Re-homed out of migrate (ADR-0121): seed the packaged build-config fragments AFTER the
    # schema migration. Its own Job name keeps a seed/object-store fault from masquerading as a
    # "migrate" failure. Runs post-* (after the DB exists and migrate ran), weighted after migrate.
    "helm.sh/hook": post-install,post-upgrade
    "helm.sh/hook-weight": "10"
    "helm.sh/hook-delete-policy": before-hook-creation
spec:
  backoffLimit: 3
  template:
    metadata:
      labels:
        {{- include "kdive.labels" . | nindent 8 }}
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
      {{- if .Values.bundledBackends }}
      initContainers:
        - name: wait-for-db
          image: {{ include "kdive.image" . }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command:
            - python
            - -c
            - |
              import socket, time, sys
              host = "{{ include "kdive.fullname" . }}-postgres"
              for _ in range(60):
                  try:
                      socket.create_connection((host, 5432), timeout=2).close()
                      sys.exit(0)
                  except OSError:
                      time.sleep(2)
              sys.exit("timed out waiting for %s:5432" % host)
      {{- end }}
      containers:
        - name: seed-build-configs
          image: {{ include "kdive.image" . }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          args: ["seed-build-configs"]
          envFrom:
            - configMapRef:
                name: {{ include "kdive.fullname" . }}-config
```

- [ ] **Step 4: Fix the existing external-render hook test (the seed hook adds post-install)**

The new seed hook is `post-install,post-upgrade` on **both** paths, so the external render now
contains the string "post-install". The existing `test_external_render_omits_post_install_migrate_hook`
(tests/helm/test_helm_render.py:94-98) asserts `"post-install" not in res.stdout` — a blanket
output scan that this seed hook legitimately violates. Its real intent is "the **migrate** Job is
not post-install on the external path." Rewrite it to scope the assertion to the migrate Job:

```python
def test_external_render_omits_post_install_migrate_hook() -> None:
    # The migrate Job must stay pre-* on the external path (the bundled path runs it post-install
    # after the in-chart DB). The seed-build-configs hook is legitimately post-* on both paths, so
    # assert on the migrate Job's phase, not a blanket output scan.
    jobs = _jobs_by_name("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert "post-install" not in (jobs["migrate"]["phase"] or "")
    assert "pre-install" in jobs["migrate"]["phase"]
```

- [ ] **Step 5: Run the FULL helm-render test file**

Run: `uv run python -m pytest tests/helm/test_helm_render.py -q`
Expected: PASS (or SKIP if helm absent) — runs every render test, catching any cross-Job
interaction (e.g. the post-install scan above) at its source rather than at Task 8.

- [ ] **Step 6: Commit**

```bash
git add deploy/helm/kdive/templates/job-seed-build-configs.yaml tests/helm/test_helm_render.py
git commit -m "feat(helm): seed build-configs in a post-deploy hook, not migrate (#440)"
```

---

## Task 7: Runbook documentation

**Files:**
- Modify: `docs/operating/runbooks/kubernetes-deploy.md` (the migrate-hook section near lines 111-130, and the troubleshooting/teardown section)

- [ ] **Step 1: Document the three Jobs, the validator, the fail-fast policy, and recovery**

Add a subsection after the existing migrate-Job description (around line 130) covering:

- The three deploy Jobs and what each failure means: `*-validate-systems` (pre-upgrade, fail-fast on a malformed `systems.toml`), `*-migrate` (SQL only — a failure here is a real schema failure), `*-seed-build-configs` (post-upgrade; a failure means an object-store fault, with the app already rolled out).
- **AC#2 — validate `systems.toml` with only the image + kubectl:**
  ```
  kubectl run kdive-validate --rm -i --restart=Never \
    --image=<your kdive image> \
    --overrides='{"spec":{"volumes":[{"name":"s","configMap":{"name":"<your systems CM>"}}],
      "containers":[{"name":"v","image":"<your kdive image>","args":["reconcile-systems","--check","--path","/s/systems.toml"],
      "volumeMounts":[{"name":"s","mountPath":"/s"}]}]}}'
  ```
  and note that on a deploy, the precise field error is in the hook pod's logs:
  `kubectl logs job/<release>-kdive-validate-systems` — read it **before** retrying `helm upgrade` (the `before-hook-creation` policy reaps the failed pod on the next attempt).
- **Failure policy:** a malformed `systems.toml` aborts the upgrade (fail-fast at deploy); the reconciler degrades (keep-last-good) at runtime — different moments, by design (ADR-0121).
- **ConfigMap preconditions:** `systems.configMapName` must name an existing ConfigMap whose key equals `systems.fileName` (default `systems.toml`); a missing ConfigMap leaves the hook pod in `CreateContainerConfigError`.
- **Seed recovery:** if `*-seed-build-configs` fails, fix the object store, then re-run `helm upgrade` (re-fires the hook) or `kubectl exec` a pod to run `python -m kdive seed-build-configs`.

Keep prose plain (no "robust"/"comprehensive"/"critical"). Update the teardown section (around line 271) to also delete the two new hook Jobs if listing leftover hook resources.

- [ ] **Step 2: Run the doc guardrails**

Run: `just docs-links && just docs-paths && just check-mermaid`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add docs/operating/runbooks/kubernetes-deploy.md
git commit -m "docs(runbook): document validate hook, fail-fast policy, seed recovery (#440)"
```

---

## Task 8: Full-suite sweep

**No generated-doc regen is needed.** `just docs-check` regenerates only the **MCP tool
reference** (`scripts/gen_tool_reference.py` → `docs/guide/reference/`); there is no generated CLI
subcommand reference (CLI commands like `reconcile-systems`/`seed-demo` appear only in
hand-written runbooks, updated by hand in Task 7). This change adds no MCP tool, so `docs-check`
is unaffected.

- [ ] **Step 1: Run the full guardrail suite**

Run: `just lint && just type && just test`
Expected: all pass. (`just type` is whole-tree — it type-checks `tests/` too.)

- [ ] **Step 2: Run the doc gates**

Run: `just docs-check && just docs-links && just docs-paths && just config-docs-check && just config-guard && just env-docs-check && just chart-version-check`
Expected: all pass (no drift — confirms the no-regen claim above). `config-docs`/`config-guard`
matter because Task 3 touches the CLI command set, not the config registry, so they should be
clean; running them proves it.

---

## Self-Review notes (author)

- **Spec coverage:** A→Task 1 (incl. the `seed_build_configs_step` wrapper + its behavior tests); B→Tasks 3 (CLI command wiring) + 6 (hook); C→Task 2; D→Task 5; migrate-mount cleanup→Task 4; runbook/AC#3→Task 7; baseline-reconcile precondition is a behavior the tests in Task 1 assert (migrate creates no config rows) and Task 7 documents.
- **AC mapping:** AC#1→Task 1 (`test_migrate_is_sql_only`); AC#2→Task 2 + Task 7 (`--check` + kubectl recipe/logs); AC#3→Tasks 5 + 7 (pre-upgrade hook + runbook).
- **Type consistency:** `seed_build_configs_step(database_url=None)` and `validate_systems(path)` names are used identically across Tasks 1/3 and 2/5. `_jobs_by_name` suffix keys (`migrate`, `validate-systems`, `seed-build-configs`) match the Job `metadata.name` suffixes in Tasks 4/5/6.
- **Guardrail hazard flagged:** Task 5 Step 5 explicitly handles the existing `_hooks_by_kind` Job-aliasing breakage.
- **Helm-absent caveat:** the render tests SKIP without a local `helm`; CI provides it. State this in the PR body.
