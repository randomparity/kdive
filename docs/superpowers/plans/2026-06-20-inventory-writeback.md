# Plan: inventory writeback seam (M2.7 sub-issue D, #641)

Derived from [the spec](../../specs/2026-06-20-inventory-writeback.md) and
[ADR-0199](../../adr/0199-seed-once-runtime-authoritative-inventory.md). The serializer
(`src/kdive/inventory/serialize.py`, #640) is merged into `main` and is the input to this work.

**Repo conventions (apply to every task):**
- Python 3.14, `uv`. Absolute imports only. ≤100 lines/function, complexity ≤8, ≤100-char lines,
  Google-style docstrings on non-trivial public APIs.
- Every tool returns a `ToolResponse` (`src/kdive/mcp/responses.py`) with the most specific
  `ErrorCategory` (`src/kdive/domain/errors.py`); never invent error strings.
- TDD: write the failing test first, confirm it fails for the right reason, minimal impl, refocus.
- Guardrails before each commit: `just lint`, `just type` (whole tree), focused `just test`. For
  doc/config/tool-signature changes also `just docs`, `just config-docs`, `just env-docs-check`,
  `just check-mermaid`, `just docs-paths`. Run the FULL `just ci` before the first push.
- Doc-style word ban (no "critical/comprehensive/robust/significant/…") in code, comments, commits,
  docs.
- Conventional commits, imperative ≤72-char subject, one logical change per commit, end every commit
  with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Never squash.
- No new ADR: ADR-0199 (Accepted) already records the writeback-opt-in decision and the rejected
  auto-write alternative.

**No new dependency.** The ConfigMap patch uses `httpx` (already a dependency); the idiom is
`async with httpx.AsyncClient(...) as client` (see `src/kdive/process_health/server.py:51`). Do NOT
add a Kubernetes client library.

---

## Task 1 — Config settings for the writeback selector

**Where it fits:** the opt-in switch. The factory (Task 3) reads these; the tool (Task 5) gates on
them. Off by default → zero behavior change.

**Files:** `src/kdive/config/core_settings.py` (add settings + append to `SETTINGS`);
`docs/guide/reference/config.md` (regenerated); `tests/config/` (a focused settings test if the
existing pattern has one — otherwise the registry's own tests + `env-docs-check` cover it).

**Steps (TDD):**
1. Add `INVENTORY_WRITEBACK = Setting(name="KDIVE_INVENTORY_WRITEBACK", parse=_str, default=None,
   group="inventory", processes=frozenset({"server"}), help=..., suggest="one of: off, configmap,
   file")`. Default `None` (= off). Help text: names the three accepted values and that it is the
   opt-in for `ops.export_systems_toml(persist=true)`.
2. Add `INVENTORY_WRITEBACK_CONFIGMAP = Setting(name="KDIVE_INVENTORY_WRITEBACK_CONFIGMAP",
   parse=_str, default="kdive-systems", group="inventory", processes=frozenset({"server"}),
   help=..., suggest="the ConfigMap name, e.g. kdive-systems")`. The ConfigMap **key** reuses the
   file name; default it to `systems.toml` via a module constant (no separate setting — the chart's
   `fileName` default is `systems.toml`).
3. Append both to `SETTINGS`.
4. Regenerate config docs: `just config-docs`; confirm `just config-docs-check` and
   `just env-docs-check` pass.

**Acceptance:** `config.get(INVENTORY_WRITEBACK)` resolves `None` unset, the raw string when set;
`just config-docs-check`, `just env-docs-check`, `just config-guard`, `just type` green. No reader
of `KDIVE_*` outside `kdive.config` (config-guard).

**Rollback:** remove the two settings + the `SETTINGS` entries + regenerate docs.

---

## Task 2 — The `writeback.py` port, the fake, and the skeleton guard

**Where it fits:** the seam every other task depends on. Pure-ish module; no I/O in the port, the
fake, or the guard, so it is fully unit-testable without a cluster.

**Files:** `src/kdive/inventory/writeback.py` (new); `tests/inventory/test_writeback.py` (new).

**Public API:**
```python
WRITEBACK_PLACEHOLDER_MARKER = "REPLACE_ME_"  # the shared skeleton marker

class WritebackError(...)  # not needed — raise CategorizedError directly

class WritebackTarget(Protocol):
    target_kind: str                         # "configmap" | "file"
    async def write(self, toml_text: str) -> None: ...

class FakeWriteback:
    target_kind = "fake"
    def __init__(self, *, fail: CategorizedError | None = None) -> None: ...
    written: str | None                      # last text written
    async def write(self, toml_text: str) -> None: ...   # records, or raises self._fail

def assert_persistable(toml_text: str) -> None:
    """Raise CONFIGURATION_ERROR if toml_text still contains a REPLACE_ME_* placeholder."""
```

**Steps (TDD):**
1. Test `assert_persistable` passes for text with no marker and raises `CategorizedError`
   (`CONFIGURATION_ERROR`, detail names the placeholder + that the operator must complete it) when
   `WRITEBACK_PLACEHOLDER_MARKER` is present. Implement.
2. **Drift test:** import `serialize._REMOTE_PLACEHOLDERS` (or its public re-export) and assert every
   value starts with `WRITEBACK_PLACEHOLDER_MARKER`, so the serializer's placeholders and the guard
   marker cannot diverge. If `_REMOTE_PLACEHOLDERS` is private, add a minimal public re-export in
   `serialize.py` (`REMOTE_PLACEHOLDER_PREFIX = "REPLACE_ME_"`) and have BOTH the serializer dict and
   the guard reference it — this is the only allowed touch of `serialize.py` (a constant, not the
   serializer logic; note it in the commit).
3. Test `FakeWriteback.write` records `toml_text`; with `fail=` set, it raises that error and does
   not record.
4. Define `WritebackTarget` Protocol (`runtime_checkable` not required). `target_kind` is a plain
   attribute.

**Acceptance:** `tests/inventory/test_writeback.py` green; `just type` green; the drift test fails
if someone changes the serializer marker without the guard. ≤100-line functions.

**Rollback:** delete `writeback.py` + its test (+ the re-export constant in `serialize.py`).

---

## Task 3 — The two real adapters + the factory

**Where it fits:** the deployment-shape implementations behind the port. Both raise typed
`CategorizedError`; neither is exercised against a real backend in CI (the HTTP boundary is mocked;
the file adapter writes a tmp path).

**Files:** `src/kdive/inventory/writeback.py` (extend); `tests/inventory/test_writeback.py` (extend).

**`MountedFileWriteback`:**
- `__init__(self, path: Path)`; `target_kind = "file"`.
- `write`: `await asyncio.to_thread(self._write_atomic, toml_text)`. `_write_atomic` writes to a
  `NamedTemporaryFile(dir=path.parent, delete=False)`, flush+fsync, `os.replace(tmp, path)`. A
  missing parent dir / `OSError` → `CategorizedError(CONFIGURATION_ERROR, detail names the path)`;
  clean up the temp file on failure.

**`ConfigMapWriteback`:**
- `__init__(self, *, namespace, name, key, token, ca_cert_path, api_base)`; `target_kind =
  "configmap"`. A classmethod `from_in_cluster(name, key)` reads
  `/var/run/secrets/kubernetes.io/serviceaccount/{token,namespace}` + `ca.crt` and
  `KUBERNETES_SERVICE_HOST/PORT`; any missing piece → `CONFIGURATION_ERROR` ("not running in a pod
  / service account not mounted"). Keep this constructor side-effecting on the filesystem **lazy**:
  resolve in `from_in_cluster`, so the unit test can build the object directly with injected values
  and never touch `/var/run`.
- `write`: `PATCH {api_base}/api/v1/namespaces/{ns}/configmaps/{name}`, headers
  `Authorization: Bearer <token>`, `Content-Type: application/strategic-merge-patch+json`, body
  `json={"data": {key: toml_text}}`, `verify=ca_cert_path`, bounded `timeout`. Map the response:
  2xx → ok; 403 → `CONFIGURATION_ERROR` (RBAC grant missing/insufficient, names `kdive-systems`);
  other non-2xx → `INFRASTRUCTURE_FAILURE` (status code only, **body redacted**); `httpx`
  transport/timeout exception → `INFRASTRUCTURE_FAILURE` (exception class name only).

**`resolve_writeback_target(config_module) -> WritebackTarget | None`:**
- Read `INVENTORY_WRITEBACK`. `None`/empty/`"off"` → `None`. `"configmap"` →
  `ConfigMapWriteback.from_in_cluster(name=config.get(INVENTORY_WRITEBACK_CONFIGMAP), key=...)`.
  `"file"` → `MountedFileWriteback(Path(config.get(SYSTEMS_TOML)))`. Any other value →
  `CategorizedError(CONFIGURATION_ERROR, accepted_values=["off","configmap","file"])`.

**Steps (TDD):** one failing test per behavior first:
1. File adapter: writes content equal to the toml; temp file gone after; atomic (mock or assert no
   partial); non-writable/missing-parent dir → `CONFIGURATION_ERROR`.
2. ConfigMap adapter with a mocked `httpx.AsyncClient` (patch the client or inject a transport):
   200 → issues the exact `PATCH` (assert URL, headers, strategic-merge body, key); 403 →
   `CONFIGURATION_ERROR`; 500 → `INFRASTRUCTURE_FAILURE` with **no** body text in the detail; a
   raised `httpx.ConnectError` → `INFRASTRUCTURE_FAILURE` naming the exception class, not the URL.
3. `from_in_cluster` with a tmp fake `/var/run` (inject the base dir, or test the missing-mount →
   `CONFIGURATION_ERROR` branch by pointing at an empty tmp dir).
4. `resolve_writeback_target`: returns `None` off; the right type per value; `CONFIGURATION_ERROR`
   on an unknown value. Drive it with `monkeypatch.setenv` + `config.load(...)` per the registry's
   snapshot model (`tests` set `KDIVE_*` then `load`).

**Mock only the boundary:** the `httpx` HTTP call (external service) and, where unavoidable, the
in-cluster file mount. The adapter logic and the factory run for real.

**Acceptance:** all adapter/factory tests green; `just type`, `just lint` green; secret material
(token, CA, response body) never appears in any `CategorizedError` detail or log (assert in tests).

**Rollback:** delete the two adapter classes + the factory + their tests.

---

## Task 4 — Audit event for the persist (write) path

**Where it fits:** the persist is a state-changing operator action, distinct from the read-only
export; the audit trail must distinguish them.

**Files:** `src/kdive/mcp/tools/ops/tuning.py` (add a write-audit helper + the new object/scope
consts); `tests/` for tuning (extend whatever asserts the export audit).

**Steps (TDD):**
1. Add `_PERSIST_SYSTEMS_TOOL = "ops.export_systems_toml"` reuse + a distinct scope const, e.g.
   `_PERSIST_SYSTEMS_SCOPE = "all-inventory-writeback"`, and an audit helper
   `_audit_inventory_write(conn, ctx, target_kind)` mirroring `_audit_inventory_read` but with the
   write scope and `args={"persist": "true", "target": target_kind}`.
2. Test: a `persist=True` success writes a `platform_audit_log` row with the write scope and the
   target kind in args (assert via the same harness the read-audit test uses).

**Acceptance:** the persist audit row is distinguishable from the export-read row; existing
export-read audit test still green.

**Rollback:** remove the helper + consts; the read audit is untouched.

---

## Task 5 — Wire `persist` into `export_systems_toml`

**Where it fits:** the operator-facing surface. Adds the opt-in param, the off/guard/write/audit
sequence, and the response fields. Not a new tool (no new registration set).

**Files:** `src/kdive/mcp/tools/ops/tuning.py` (the handler + the `@app.tool` wrapper);
`tests/` for tuning; regenerated `docs/guide/reference/` (tool reference).

**Steps (TDD), in order, each a failing test first:**
1. `export_systems_toml(pool, ctx, *, persist=False)` — default path unchanged (existing test still
   green; assert no write, no persist fields in `data`).
2. `persist=True`, writeback **off** → `CONFIGURATION_ERROR` naming `KDIVE_INVENTORY_WRITEBACK` and
   accepted values; writes nothing. (Resolve the target via `resolve_writeback_target`; `None` →
   failure.)
3. `persist=True`, adapter present, snapshot has **no** placeholders → serialize, `assert_persistable`
   passes, `await target.write(toml)`, audit the write, success with `data["persisted"]=True`,
   `data["target"]=target.target_kind`, `data["toml"]=toml`. Use `FakeWriteback` injected via a
   monkeypatched `resolve_writeback_target` (or a seam param) and assert it captured the toml.
4. `persist=True`, snapshot **with** a `remote_libvirt` host (placeholders present) → the skeleton
   guard fires → `CONFIGURATION_ERROR`, **no** write (assert the fake recorded nothing).
5. `persist=True`, adapter `.write` raises `INFRASTRUCTURE_FAILURE` (use `FakeWriteback(fail=...)`)
   → the tool returns that category via `failure_from_error`; partial-failure honesty (no
   `persisted=True`).
6. Auth: a non-operator caller with `persist=True` gets `authorization_denied` (the role gate runs
   first, before any writeback resolution — assert the fake saw nothing).
7. Update the `@app.tool` wrapper `ops_export_systems_toml` to accept
   `persist: Annotated[bool, Field(description="When true, also persist the serialized inventory to
   the configured writeback target (KDIVE_INVENTORY_WRITEBACK). Default false.")] = False` and pass
   it through. Keep the maturity/read_only annotation honest: the tool is now conditionally mutating
   — change `_docmeta.read_only()` to `_docmeta.mutating()` IF the repo's annotation contract treats
   a conditional write as mutating (check how other conditionally-writing tools annotate; default to
   `mutating()` since `persist=True` writes). Confirm `test_tool_docs` still passes (no new tool, but
   the annotation/param changed).
8. Regenerate the tool reference: `just docs`; confirm `just docs-check` green.

**Seam for testability:** prefer passing the resolved target (or the resolver) so the test injects
`FakeWriteback` without monkeypatching a module global. E.g. `export_systems_toml(pool, ctx, *,
persist=False, resolve_target=resolve_writeback_target)` with the default wired to the real factory.
Keep the public tool wrapper calling the default.

**Acceptance:** all seven behavior tests green; `just docs-check`, `just type`, `just lint`,
`just test` (focused) green; the existing `export_systems_toml` read test unchanged and green.

**Rollback:** revert the handler to the read-only signature; remove the `persist` param from the
wrapper; regenerate docs.

---

## Task 6 — Operator runbook + RBAC manifest

**Where it fits:** the real ConfigMap path is operator-verified, not CI. This documents the enable
steps, the RBAC, and the skeleton/file-path caveats the spec raised.

**Files:** `docs/operating/runbooks/kubernetes-deploy.md` (a new section, e.g.
"## N. Persist runtime inventory back to the ConfigMap (opt-in)"); optionally a manifest snippet
under the runbook or `deploy/helm/kdive/` — **inline in the runbook** as a fenced YAML block keeps
it a documented operator step rather than a chart default (the chart stays opt-in-free).

**Content:**
1. The opt-in: set `KDIVE_INVENTORY_WRITEBACK=configmap` on the **server** (where `ops.*` runs) via
   the chart's `config.*` ConfigMap; note `KDIVE_INVENTORY_WRITEBACK_CONFIGMAP` defaults to
   `kdive-systems`.
2. The RBAC manifest: a `ServiceAccount`, a `Role` granting `get`+`patch` on the **named**
   `kdive-systems` ConfigMap only (`resourceNames: [kdive-systems]`), a `RoleBinding`, and binding
   the server Deployment's pod to that ServiceAccount. Use placeholder names; add
   `# pragma: allowlist secret` only if detect-secrets flags a token/cert ref.
3. The skeleton caveat: an export containing a `remote_libvirt` host emits `REPLACE_ME_*`
   placeholders and is **refused** by `persist=true`; the operator completes the placeholders in the
   returned text, then either re-applies the file by hand (existing flow) or persists a completed
   document. State that persisting a fleet of images/build_hosts/cost_classes (no remote_libvirt)
   works directly.
4. The propagation caveat: after a successful patch, a running reconciler re-reads on the next
   kubelet ConfigMap sync or a pod restart; the verification step is a pod restart reproducing the
   live inventory.
5. The `file` path caveat: only for a deployment whose `KDIVE_SYSTEMS_TOML` is a writable volume
   shared by the server and the reconciler (RWX PVC or single-host); the default ConfigMap-mounted
   chart does **not** support it.
6. Verification (operator, real cluster): apply RBAC, set the opt-in, call
   `ops.export_systems_toml(persist=true)`, `kubectl get configmap kdive-systems -o yaml` shows the
   updated `systems.toml`, restart the reconciler pod, confirm the live inventory reproduces.

**Steps:** write the section; `just docs-links`, `just docs-paths`, `just check-mermaid` green;
`detect-secrets` (the prek hook) passes.

**Acceptance:** the runbook names every operator step, the RBAC is least-privilege (one named
ConfigMap, `get`+`patch` only), the three caveats (skeleton, propagation, file-path) are stated;
doc guardrails green.

**Rollback:** remove the runbook section.

---

## Sequencing

```
1 (settings) ─┐
2 (port+fake+guard) ─┼─> 3 (adapters+factory) ─> 5 (tool wiring) ─> 6 (runbook)
              4 (write-audit) ───────────────────┘
```

Tasks 1, 2, 4 are independent and can be done in any order. 3 depends on 1 (the factory reads the
settings) and 2 (the port). 5 depends on 3, 4, and 2 (guard). 6 is last (documents the shipped
behavior). All tasks are sequential on this one branch — no parallel subagents in this worktree.

## Self-review

- **Spec coverage:** port+fake → Task 2; two adapters + factory → Task 3; opt-in wiring + off/guard
  paths → Task 5; settings → Task 1; write-audit → Task 4; runbook+RBAC → Task 6. The spec's skeleton
  guard, file-path cross-pod honesty, and propagation caveat each map to a named step (Task 2 guard,
  Task 6 caveats).
- **No new dependency** (httpx). **No new ADR** (0199 covers it). **No migration** (config-only).
- **CI-covered vs runbook-only** is explicit: every adapter/factory/tool test is CI; the real
  ConfigMap patch is Task 6 runbook-only.
- **No placeholder/TODO** left; the only `serialize.py` touch is a shared marker constant, called
  out in Task 2.
