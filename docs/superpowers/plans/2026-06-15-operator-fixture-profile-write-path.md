# Operator fixture-profile override Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local-libvirt fixture-profile policy operator-overridable without rebuilding the image, by formalizing the existing `KDIVE_FIXTURE_CATALOG_PATH` disk override, adding a read-only `fixtures.validate` MCP tool, and cloning the `systems.toml` Helm ConfigMap mount as a `fixtures` block.

**Architecture:** Profiles are read from disk by `load_fixture_catalog()` (ADR-0065), so there is no DB/object-store catalog to build (contrast the build-config write-path ADR-0119). The override seam already exists (`KDIVE_FIXTURE_CATALOG_PATH`); this work documents it, makes the server process a declared reader, adds an operator validation surface, and wires the k8s ConfigMap. No migration, no new DB table, no write-MCP-tool. See [`docs/design/operator-fixture-profile-write-path.md`](../../design/operator-fixture-profile-write-path.md) and [ADR-0120](../../adr/0120-operator-fixture-profile-write-path.md).

**Tech Stack:** Python 3.13, FastMCP, pydantic, PyYAML; Helm (Go templates); `uv`/`ruff`/`ty`/`pytest`; `just` recipes.

**Repo conventions (apply to every task):**
- Branch is `feat/fixtures-write-path-439` (already created). Never commit on `main`.
- Guardrails before every commit: `just lint`, `just type` (whole tree), and the focused tests for the task. Run `just docs-check` / `just config-docs-check` after any tool/config change. Zero warnings.
- Commit trailer required: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Conventional-commit subject ≤72 chars, imperative.
- Absolute imports only; line length 100; Google-style docstrings on public APIs.
- Doc-style: plain factual prose; never "critical/robust/comprehensive/elegant"; "Milestone" not "Sprint".

---

## File Structure

- `src/kdive/mcp/tools/catalog/fixtures.py` — **modify**: add `validate_fixtures_tool()` handler + register `fixtures.validate` alongside the existing `fixtures.list`.
- `tests/mcp/catalog/test_fixtures_validate.py` — **create**: behavior tests for the handler (valid / absent / malformed / empty-profiles), driven with a tmp catalog dir (no DB).
- `tests/mcp/core/test_tool_docs.py` — **modify**: add the `fixtures.validate` → test-module mapping.
- `docs/guide/reference/fixtures.md`, `docs/guide/reference/index.md` — **regenerated** by `just docs` (do not hand-edit).
- `src/kdive/config/core_settings.py` — **modify**: add a `_CATALOG_READERS` process set and point `FIXTURE_CATALOG_PATH` at it (adds `server`).
- `docs/guide/reference/config.md` — **regenerated** by `just config-docs`.
- `deploy/helm/kdive/values.yaml` — **modify**: add the `fixtures` block.
- `deploy/helm/kdive/templates/_helpers.tpl` — **modify**: add `fixturesEnv` / `fixturesVolumeMount` / `fixturesVolume` helpers (no per-key `items`).
- `deploy/helm/kdive/templates/deployment-server.yaml`, `deployment-worker.yaml`, `deployment-reconciler.yaml` — **modify**: include the fixtures helpers. **Not** `job-migrate.yaml`.
- `deploy/helm/kdive/Chart.yaml` — **modify**: bump chart `version` (feature change).
- `tests/helm/test_helm_render.py` — **modify**: add unset/set render assertions, including that migrate does not mount fixtures.
- `docs/operating/...` (the systems.toml operating doc) — **modify**: document the override workflow.

---

## Task 1: `fixtures.validate` MCP read tool

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/fixtures.py`
- Create: `tests/mcp/catalog/test_fixtures_validate.py`
- Modify: `tests/mcp/core/test_tool_docs.py`

**Where it fits:** Acceptance criterion 1 needs an operator-facing way to confirm an override took effect; this is the read/verify surface that satisfies AC#2's "read/verify" parity with `buildconfig.get`. The handler loads the catalog at the resolved disk path and returns the profile identity triples or a `configuration_error`. It is auth-only (mirrors `fixtures.list`), reads no DB, and re-categorizes the loader's internal `INFRASTRUCTURE_FAILURE` to `CONFIGURATION_ERROR` (the operator's supplied config is the wrong thing).

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/catalog/test_fixtures_validate.py`:

```python
"""``fixtures.validate`` — read-only validation of the resolved fixture catalog (ADR-0120).

Drives the handler directly with an injected catalog path (no DB, no transport). Covers:
* a valid catalog (the packaged default written by ``install_fixtures``) → ``valid`` + its
  ``(provider, name, arch)`` profile triples;
* an absent path → ``configuration_error`` naming the resolved path;
* a malformed manifest → ``configuration_error`` (no raw file content in the reason);
* an empty profile list (valid manifest, ``profiles: []``) → ``valid`` with ``profiles == []``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from kdive.admin.bootstrap import install_fixtures
from kdive.mcp.tools.catalog import fixtures
from tests.mcp.json_data import data_sequence, data_str, json_mapping

_MIN_MANIFEST = """schema_version: 1
provider: local-libvirt
storage:
  allowed_component_roots:
    - /var/lib/kdive/rootfs
  cache_dir: /var/lib/kdive/rootfs/cache
  overlay_dir: /var/lib/kdive/rootfs/overlays
rootfs: []
profiles: []
"""


def test_valid_catalog_reports_profiles(tmp_path: Path) -> None:
    # install_fixtures refuses a pre-existing dest (force=False), and tmp_path already
    # exists; write into a fresh subdir it creates.
    dest = tmp_path / "catalog"
    install_fixtures(dest)
    resp = asyncio.run(fixtures.validate_fixtures_tool(dest))
    assert resp.status == "valid", resp
    assert resp.error_category is None
    rows = [json_mapping(r) for r in data_sequence(resp, "profiles")]
    triples = {(r["provider"], r["name"], r["arch"]) for r in rows}
    assert ("local-libvirt", "console-ready_x86_64", "x86_64") in triples
    assert data_str(resp, "path") == str(dest)


def test_absent_path_is_configuration_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    resp = asyncio.run(fixtures.validate_fixtures_tool(missing))
    assert resp.status != "valid"
    assert resp.error_category == "configuration_error"
    assert data_str(resp, "path") == str(missing)


def test_malformed_manifest_is_configuration_error_without_content(tmp_path: Path) -> None:
    (tmp_path / "manifest.yaml").write_text("schema_version: 2\nSEKRIT_TOKEN: leakme\n")
    resp = asyncio.run(fixtures.validate_fixtures_tool(tmp_path))
    assert resp.error_category == "configuration_error"
    assert "leakme" not in data_str(resp, "reason"), "bounded reason must not echo file content"


def test_empty_profile_list_is_valid(tmp_path: Path) -> None:
    (tmp_path / "manifest.yaml").write_text(_MIN_MANIFEST)
    resp = asyncio.run(fixtures.validate_fixtures_tool(tmp_path))
    assert resp.status == "valid", resp
    assert data_sequence(resp, "profiles") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/catalog/test_fixtures_validate.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'validate_fixtures_tool'`.

- [ ] **Step 3: Write the minimal implementation**

In `src/kdive/mcp/tools/catalog/fixtures.py`, add imports at the top (with the existing imports):

```python
import asyncio
from pathlib import Path

from kdive.components.catalog import fixture_catalog_path_from_env, load_fixture_catalog
from kdive.domain.errors import CategorizedError, ErrorCategory
```

Add the handler (above `register`):

```python
_VALIDATE_TOOL = "fixtures.validate"


async def validate_fixtures_tool(path: Path | None = None) -> ToolResponse:
    """Load the fixture catalog at the resolved path and report its profiles or an error.

    Args:
        path: An explicit catalog directory; ``None`` resolves ``KDIVE_FIXTURE_CATALOG_PATH``
            (or the packaged source-tree default).

    Returns:
        ``valid`` with ``{path, profiles:[{provider,name,arch}]}`` when the catalog loads, else
        a ``CONFIGURATION_ERROR`` failure carrying the resolved ``path`` and a bounded ``reason``
        (the underlying exception type name — never the raw exception text or file body).
    """
    resolved = path or fixture_catalog_path_from_env()
    try:
        catalog = await asyncio.to_thread(load_fixture_catalog, resolved)
    except CategorizedError as exc:
        cause = exc.__cause__
        reason = type(cause).__name__ if cause is not None else type(exc).__name__
        return ToolResponse.failure(
            _OBJECT_ID,
            ErrorCategory.CONFIGURATION_ERROR,
            suggested_next_actions=[_VALIDATE_TOOL],
            data={"path": str(resolved), "reason": reason},
        )
    profiles: list[JsonValue] = sorted(
        (
            {"provider": p.provider, "name": p.name, "arch": p.arch}
            for p in catalog.profiles
        ),
        key=lambda r: (r["provider"], r["name"], r["arch"]),
    )
    return ToolResponse.success(
        _OBJECT_ID,
        "valid",
        suggested_next_actions=[_OBJECT_ID + ".list"],
        data={"path": str(resolved), "profiles": profiles},
    )
```

In the existing `register` function, add the second tool after the `fixtures.list` registration:

```python
    @app.tool(
        name=_VALIDATE_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def fixtures_validate() -> ToolResponse:
        """Validate the resolved fixture catalog and list its profiles. Requires a valid token."""
        current_context()
        return await validate_fixtures_tool()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/catalog/test_fixtures_validate.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Map the tool in the documentation guard**

In `tests/mcp/core/test_tool_docs.py`, add to `_BEHAVIOR_TESTS_BY_TOOL` (keep alphabetical near `fixtures.list`):

```python
    "fixtures.validate": ("tests/mcp/catalog/test_fixtures_validate.py",),
```

- [ ] **Step 6: Regenerate the tool reference and run the doc guard**

Run: `just docs && just docs-check`
Expected: `just docs` updates `docs/guide/reference/fixtures.md` (adds a `fixtures.validate` section) and `index.md`; `docs-check` passes.

- [ ] **Step 7: Run the tool-docs guard + lint + type**

Run: `uv run python -m pytest tests/mcp/core/test_tool_docs.py -q && just lint && just type`
Expected: PASS, zero warnings. `test_tool_docs` asserts `active == mapped` (the registry-vs-map bijection), so if the `fixtures.validate` wrapper registration was omitted, the new map entry shows as "stale" and this fails — i.e. this step also verifies the tool is actually registered, not just the handler.

- [ ] **Step 8: Commit**

```bash
git add src/kdive/mcp/tools/catalog/fixtures.py tests/mcp/catalog/test_fixtures_validate.py \
        tests/mcp/core/test_tool_docs.py docs/guide/reference/fixtures.md docs/guide/reference/index.md
git commit -m "feat(fixtures): add read-only fixtures.validate tool (#439)"
```

---

## Task 2: declare the server a `FIXTURE_CATALOG_PATH` reader

**Files:**
- Modify: `src/kdive/config/core_settings.py`
- Regenerated: `docs/guide/reference/config.md`

**Where it fits:** `fixtures.validate` reads `KDIVE_FIXTURE_CATALOG_PATH` in the **server** process. `config.get` does not gate on process at read time, so this is a config-reference/manifest accuracy change, not a functional unlock — but the declared reader set should be honest. Do **not** mutate the shared `_DISCOVERY` literal (a second setting references it); introduce a new named set.

- [ ] **Step 1: Add the process set and point the setting at it**

In `src/kdive/config/core_settings.py`, add near the other process-group constants (after `_DISCOVERY`):

```python
# Processes that read the on-disk provider fixture catalog: the worker/reconciler build paths
# and the server's fixtures.validate read (ADR-0120).
_CATALOG_READERS = frozenset({"server", "worker", "reconciler"})
```

Change the `FIXTURE_CATALOG_PATH` setting's `processes=_DISCOVERY` to `processes=_CATALOG_READERS`:

```python
FIXTURE_CATALOG_PATH = Setting(
    name="KDIVE_FIXTURE_CATALOG_PATH",
    parse=_str,
    group="catalog",
    processes=_CATALOG_READERS,
    help="Override path to the provider fixture catalog (operator override, ADR-0120).",
)
```

- [ ] **Step 2: Regenerate the config reference**

Run: `just config-docs`
Expected: `docs/guide/reference/config.md`'s `KDIVE_FIXTURE_CATALOG_PATH` row now lists the server process.

- [ ] **Step 3: Run the config guards + type**

Run: `just config-docs-check && just config-guard && just type`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/kdive/config/core_settings.py docs/guide/reference/config.md
git commit -m "feat(config): declare server a fixture-catalog reader (#439)"
```

---

## Task 3: Helm `fixtures` ConfigMap mount (server/worker/reconciler only)

**Files:**
- Modify: `deploy/helm/kdive/values.yaml`
- Modify: `deploy/helm/kdive/templates/_helpers.tpl`
- Modify: `deploy/helm/kdive/templates/deployment-server.yaml`, `deployment-worker.yaml`, `deployment-reconciler.yaml`
- Modify: `deploy/helm/kdive/Chart.yaml`
- Modify: `tests/helm/test_helm_render.py`

**Where it fits:** the k8s parallel of the venv-on-host override. A flat ConfigMap (each profile YAML as a top-level key; manifest referencing bare filenames) mounts at `mountPath` and sets `KDIVE_FIXTURE_CATALOG_PATH`. The `fixturesVolume` helper uses a plain `configMap:` (no per-key `items` — the chart cannot enumerate an operator-authored ConfigMap's keys, and a plain mount writes every key as a flat file, which is the required flat layout). The migrate job is **excluded** because `migrate()` never reads the fixture catalog.

- [ ] **Step 1: Write the failing render tests**

In `tests/helm/test_helm_render.py`, add the tests below. They reuse the file's **existing** helpers — `_deployments_with(*set_args)` (renders with the DB URL injected and indexes Deployments by process name) and `_container(deploy)` — and find the migrate Job inline, exactly as `test_systems_inventory_*` and `test_secrets_unset_mounts_nothing` already do. Do not invent new helpers.

```python
def test_fixtures_unset_mounts_nothing() -> None:
    for proc, deploy in _deployments_with().items():
        container = _container(deploy)
        env_names = {e["name"] for e in container["env"]}
        assert "KDIVE_FIXTURE_CATALOG_PATH" not in env_names, proc
        mounts = {m["name"] for m in container.get("volumeMounts", [])}
        assert "kdive-fixtures" not in mounts, proc
        volumes = {v["name"] for v in deploy["spec"]["template"]["spec"].get("volumes", [])}
        assert "kdive-fixtures" not in volumes, proc


def test_fixtures_configmap_mounts_on_components_not_migrate() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y", "fixtures.configMapName=fx")
    assert res.returncode == 0, res.stderr
    docs = [doc for doc in yaml.safe_load_all(res.stdout) if isinstance(doc, dict)]
    deployments = {
        doc["metadata"]["name"].removeprefix("kdive-kdive-"): doc
        for doc in docs
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].startswith("kdive-kdive-")
    }
    for proc in ("server", "worker", "reconciler"):
        deploy = deployments[proc]
        container = _container(deploy)
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env["KDIVE_FIXTURE_CATALOG_PATH"] == "/etc/kdive/fixtures", proc
        mount = next(m for m in container["volumeMounts"] if m["name"] == "kdive-fixtures")
        assert mount["mountPath"] == "/etc/kdive/fixtures"
        assert mount["readOnly"] is True
        volumes = deploy["spec"]["template"]["spec"]["volumes"]
        volume = next(v for v in volumes if v["name"] == "kdive-fixtures")
        assert volume["configMap"]["name"] == "fx"
        assert "items" not in volume["configMap"], "fixtures mount must be flat (no items)"
    migrate = next(doc for doc in docs if doc.get("kind") == "Job")
    mmounts = {m["name"] for m in _container(migrate).get("volumeMounts", [])}
    assert "kdive-fixtures" not in mmounts, "migrate does not read the fixture catalog"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/helm/test_helm_render.py -q -k fixtures`
Expected: FAIL — `KeyError: 'KDIVE_FIXTURE_CATALOG_PATH'` / no `kdive-fixtures` volume.

- [ ] **Step 3: Add the values block**

In `deploy/helm/kdive/values.yaml`, after the `systems:` block, add:

```yaml
# Optional operator override for the local-libvirt fixture-profile catalog (ADR-0120).
# Create a ConfigMap whose keys are manifest.yaml plus each profile YAML as a FLAT top-level
# key (a ConfigMap key cannot contain '/'; the manifest must list bare filenames). Set
# configMapName to mount it on server/worker/reconciler and point KDIVE_FIXTURE_CATALOG_PATH
# at it. Not mounted on the migrate job (migrate does not read the fixture catalog).
fixtures:
  configMapName: ""
  mountPath: /etc/kdive/fixtures
```

- [ ] **Step 4: Add the helpers**

In `deploy/helm/kdive/templates/_helpers.tpl`, after the `kdive.systemsVolume` define block, add:

```yaml
{{- define "kdive.fixturesEnv" -}}
{{- if .Values.fixtures.configMapName -}}
- name: KDIVE_FIXTURE_CATALOG_PATH
  value: {{ .Values.fixtures.mountPath | quote }}
{{- end -}}
{{- end -}}

{{- define "kdive.fixturesVolumeMount" -}}
{{- if .Values.fixtures.configMapName -}}
- name: kdive-fixtures
  mountPath: {{ .Values.fixtures.mountPath | quote }}
  readOnly: true
{{- end -}}
{{- end -}}

{{- define "kdive.fixturesVolume" -}}
{{- if .Values.fixtures.configMapName -}}
- name: kdive-fixtures
  configMap:
    name: {{ .Values.fixtures.configMapName | quote }}
{{- end -}}
{{- end -}}
```

(No `items:` — a plain ConfigMap volume writes every key as a flat file at `mountPath`, the required flat layout.)

- [ ] **Step 5: Wire the helpers into the three component deployments**

In each of `deployment-server.yaml`, `deployment-worker.yaml`, `deployment-reconciler.yaml`, add the fixtures includes immediately after the matching `systems*` includes (same `nindent` as the adjacent systems line):

- After `{{- include "kdive.systemsEnv" . | nindent 12 }}` add `{{- include "kdive.fixturesEnv" . | nindent 12 }}`
- After `{{- include "kdive.systemsVolumeMount" . | nindent 12 }}` add `{{- include "kdive.fixturesVolumeMount" . | nindent 12 }}`
- After `{{- include "kdive.systemsVolume" . | nindent 8 }}` add `{{- include "kdive.fixturesVolume" . | nindent 8 }}`

Do **not** edit `job-migrate.yaml`.

- [ ] **Step 6: Bump the chart version**

In `deploy/helm/kdive/Chart.yaml`, bump `version: 0.2.0` → `version: 0.3.0` (a chart feature). Leave `appVersion` unchanged (`chart-version-check` requires `appVersion == pyproject version`, which is not changing).

- [ ] **Step 7: Run the render tests + chart guards**

Run: `uv run python -m pytest tests/helm/test_helm_render.py -q && just chart-version-check`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add deploy/helm/kdive/values.yaml deploy/helm/kdive/templates/_helpers.tpl \
        deploy/helm/kdive/templates/deployment-server.yaml \
        deploy/helm/kdive/templates/deployment-worker.yaml \
        deploy/helm/kdive/templates/deployment-reconciler.yaml \
        deploy/helm/kdive/Chart.yaml tests/helm/test_helm_render.py
git commit -m "feat(helm): mount operator fixtures ConfigMap on components (#439)"
```

---

## Task 4: operator override documentation

**Files:**
- Modify: the operating doc that documents `systems.toml` (find it: `rg -l "KDIVE_SYSTEMS_TOML" docs/operating docs/guide`), or add a short section to the closest operating runbook.

**Where it fits:** AC#1's usability — an operator must be able to find and follow the override workflow. Document both deployments and the flat-ConfigMap constraint, and point at `fixtures.validate`.

- [ ] **Step 1: Locate the systems.toml operating doc**

Run: `rg -ln "KDIVE_SYSTEMS_TOML" docs/`
Pick the operating doc (not an ADR/spec/archive) that explains operator config; that is where the fixtures section belongs.

- [ ] **Step 2: Add the override section**

Add a "Fixture-profile override" subsection containing (plain prose, no banned words):

- What a fixture profile is (build-time kernel-config/cmdline validation policy, provider-scoped, non-secret) and that the packaged default is `console-ready_x86_64`.
- **venv-on-host:** `python -m kdive install-fixtures --dest <dir>`, edit the profile YAML, then export `KDIVE_FIXTURE_CATALOG_PATH=<dir>` for **every** process that loads it (server + worker + reconciler).
- **k8s:** create a **flat-layout** ConfigMap — `manifest.yaml` plus each profile YAML as a top-level key, with the manifest listing **bare filenames** (a ConfigMap key cannot contain `/`); set `fixtures.configMapName` and (optionally) `fixtures.mountPath`. Include a minimal flat-layout example:

```yaml
# kubectl create configmap kdive-fixtures \
#   --from-file=manifest.yaml --from-file=console-ready_x86_64.yaml
# manifest.yaml must reference the profile by bare filename:
#   profiles: ["console-ready_x86_64.yaml"]
```

- **Verify:** call `fixtures.validate` after overriding/mounting; it reports the resolved path and the profiles the catalog advertises, or a `configuration_error` if the catalog is absent/malformed. Note it attests the **server** process's view only — in venv-on-host the operator must set the env identically across processes; in k8s the shared ConfigMap mounts on every component pod.

- [ ] **Step 3: Run the doc guards**

Run: `just docs-links && just check-mermaid`
Expected: PASS. Also grep the new prose for banned words: `rg -ni "critical|crucial|essential|significant|comprehensive|robust|elegant|sprint" <edited-doc>` → no hits.

- [ ] **Step 4: Commit**

```bash
git add <edited-doc>
git commit -m "docs(fixtures): document the operator fixture-profile override (#439)"
```

---

## Self-Review (run before handing off)

**Spec coverage:**
- §1 (formalize env override) → Task 2 (server reader) + Task 4 (docs).
- §2 (`fixtures.validate`) → Task 1.
- §3 (Helm fixtures block, migrate excluded, flat layout) → Task 3.
- §4 (documentation) → Task 4.
- AC#1 (override without rebuild) → Tasks 2–4. AC#2 (read/verify parity, operator-owned, survives redeploy) → Task 1 + the file-seam design (no code needed for no-clobber).

**Type/name consistency:** `validate_fixtures_tool(path: Path | None)` is defined in Task 1 and called by the wrapper in Task 1; `_CATALOG_READERS` defined and used in Task 2; `kdive.fixtures*` helpers defined in Task 3 Step 4 and included in Step 5; `fixtures.configMapName` / `fixtures.mountPath` consistent across Steps 1, 3, 4. Status string `"valid"` and `error_category == "configuration_error"` consistent between the handler and its tests.

**No-placeholder check:** every code step shows the actual code; every run step shows the command and expected result.

**Out of scope (do not implement):** domain XML / provisioning profile override; any DB/object-store profile catalog or `profile.set` write tool; baking a default catalog into the container image (ADR-0120 rejected alternatives).
