# Plan — local-libvirt live drgn introspection (`introspect.run`, #677 / B3)

- **Spec:** [`docs/specs/2026-06-23-local-libvirt-live-drgn-introspection.md`](../../specs/2026-06-23-local-libvirt-live-drgn-introspection.md)
- **ADR:** [ADR-0219](../../adr/0219-local-libvirt-live-drgn-introspection.md)
- **Execution mode:** direct, in-session. The change is tightly coupled to one module
  (`providers/local_libvirt/debug/introspect.py` + its test + one `composition.py` line); it is
  not parallelizable into independent subagent tasks.

## Guardrails (run before every commit)

- `just lint` (ruff check + format-check), `just type` (ty, whole tree).
- Focused tests: `uv run python -m pytest tests/providers/local_libvirt/test_introspect_drgn.py
  tests/providers/local_libvirt/test_composition.py tests/providers/test_composition.py
  tests/mcp/debug/test_introspect_tools.py -q`.
- Full `just ci` once before the first push (architecture/doc/snapshot tests live outside the
  touched dirs).
- Zero warnings. `# pragma: no cover - live_vm` only on the real `subprocess` SSH call.

## Tasks (TDD: failing test first, then minimal impl, then refactor green)

### T1 — Replace the live-introspect seam with the SSH-exec-helper model

**Where:** `src/kdive/providers/local_libvirt/debug/introspect.py`,
`tests/providers/local_libvirt/test_introspect_drgn.py`.

The stub `LocalLibvirtLiveIntrospect` carries `open_live_program(handle) -> _Program` +
`run_helper(program, name)`. Replace both `None` seams with one injected seam
`run_live_helper: Callable[[str, str], dict[str, object]] | None` (`(transport_handle, helper) ->
section`). `from_env()` wires the real `_real_run_live_helper`. Rewrite `introspect_live`:

1. `if run_live_helper is None: raise MISSING_DEPENDENCY` (off-gate guard).
2. `if helper not in {"tasks","modules","sysinfo"}: raise CONFIGURATION_ERROR` (before any seam call).
3. `section = run_live_helper(transport_handle, helper)`.
4. Route `section` into `tasks`/`modules`/`sysinfo` by `helper`; the other two stay `{}`.
5. `return assemble_report(tasks, modules, sysinfo, byte_cap=_REPORT_BYTE_CAP,
   secret_registry=...)`.

Wrap the seam call so a non-`CategorizedError` becomes `DEBUG_ATTACH_FAILURE` and a
`CategorizedError` (transport/attach/infrastructure) propagates typed — mirror the remote live
port and the existing `_normalize_attach_error`.

**Tests (rewrite the existing `LocalLibvirtLiveIntrospect` block — the old `open_live_program`
fakes are replaced):**

- off-gate: `run_live_helper=None` → `MISSING_DEPENDENCY`.
- unknown helper → `CONFIGURATION_ERROR`, and the fake seam is **not** called (assert no call).
- happy path: a fake seam returns a canned `tasks` section → routed to `out.tasks`, others `{}`.
- selected-helper routing for `modules` and `sysinfo` (parametrize): section lands in its field.
- the seam receives exactly `(transport_handle, helper)` (capture the args in the fake).
- redaction at the boundary: a planted `token=hunter2` in the section is masked in the report.
- byte-cap: a tiny `_report_byte_cap` trims `tasks` and sets `truncated`.
- a `CategorizedError(TRANSPORT_FAILURE)` from the seam propagates typed.
- a `RuntimeError` from the seam becomes `DEBUG_ATTACH_FAILURE`.
- `from_env()` wires a non-`None` `run_live_helper` that **is** `_real_run_live_helper` (identity
  assert), and calling it (off `live_vm`, no real SSH) raises a categorized error (not
  `ImportError`/`subprocess` escape) — drive the real seam with a fake/handle that fails before IO
  or via the managed-key-absent branch.

### T2 — The real `_real_run_live_helper` seam (live_vm-gated subprocess)

**Where:** same module.

`_real_run_live_helper(transport_handle, helper) -> dict[str, object]`:

1. `TransportHandleData.decode(handle)`; require `kind == "ssh"`, loopback-literal host, valid
   port → else `CONFIGURATION_ERROR` (before IO). Reuse the loopback check shape from
   `connect.py` (`_is_loopback_literal`).
2. Resolve `managed_private_key_path()`; if absent → `CONFIGURATION_ERROR` (before IO). Register
   the key value into the redaction registry (read bytes, register, pass the path to `ssh -i`).
3. Build fixed argv:
   `["ssh", "-i", str(key), "-o","BatchMode=yes","-o","StrictHostKeyChecking=no",
   "-o","UserKnownHostsFile=/dev/null","-o",f"ConnectTimeout={...}","-p",str(port),
   "root@127.0.0.1","--","/usr/local/sbin/kdive-drgn",helper]`. Helper already validated.
4. `subprocess.run(argv, timeout=_LIVE_INTROSPECT_SSH_TIMEOUT_S, capture_output=True, text=False)`.
   `subprocess.TimeoutExpired`/`OSError` → `TRANSPORT_FAILURE`; non-zero exit →
   `DEBUG_ATTACH_FAILURE`; `json.loads(stdout)` fail / non-dict → `INFRASTRUCTURE_FAILURE`.
   Decode stdout as bytes→utf-8 then JSON (mirror remote `_run_in_guest`).
5. Module constant `_LIVE_INTROSPECT_SSH_TIMEOUT_S` (pick a concrete value, e.g. 60s; document the
   `asyncio.to_thread` thread-pool exposure in a comment). The whole function carries
   `# pragma: no cover - live_vm` on the real subprocess line(s); the decode/loopback/key-absent
   branches must stay unit-testable (factor the pre-IO validation out so a test reaches it without
   spawning ssh, OR cover those branches via T1's `from_env` driving — choose the factoring that
   keeps the loopback/scheme/key-absent `CONFIGURATION_ERROR` branches covered without `live_vm`).

**Tests:** the pre-IO validation branches (bad scheme, non-loopback host, managed-key-absent) →
`CONFIGURATION_ERROR`, reached without spawning ssh (inject a fake `managed_private_key_path` /
drive the decode directly). The subprocess itself stays `live_vm`-gated.

### T3 — Flip `supported_introspection` += `"live"`

**Where:** `src/kdive/providers/local_libvirt/composition.py:124`,
`tests/providers/local_libvirt/test_composition.py:78` (the **local** assertion). Do **not** touch
`tests/providers/test_composition.py:947` — that is the *remote* runtime descriptor test, which
already asserts `{"offline-vmcore", "live"}` and stays unchanged.

Change `frozenset({"offline-vmcore"})` → `frozenset({"offline-vmcore", "live"})`. Update the
inline ADR comment (cite ADR-0219, drop "stays empty until that plane lands"). The local
composition test at line 78 flips to `frozenset({"offline-vmcore", "live"})`.

### T4 — `introspect.run` maturity stays `partial`; provider note updated

**Where:** `src/kdive/mcp/tools/debug/introspect.py` (the `maturity_meta` `providers=` line for
`introspect.run`).

The maturity stays `partial` (no change to the `"partial"` level). Update the `providers=` prose
from "local-libvirt: planned (M2.8 B3)" → "local-libvirt: wired (M2.8 B3), live proof pending B6".

**This prose is hard-gated by a generated doc.** `docs/guide/reference/introspect.md:23` carries
that exact "Provider support:" line, generated by `scripts/gen_tool_reference.py` and enforced by
the `docs-check` recipe (`justfile`: "tool reference is stale — run 'just docs' and commit"). After
the prose edit, **run `just docs`** (the mutating regenerator) and **commit the updated
`docs/guide/reference/introspect.md`** in the same change — this is unconditional, not optional, or
`just ci` fails on a stale doc. Confirm `tests/mcp/debug/test_introspect_tools.py` still passes
(it asserts the descriptor-gated `capability_unsupported` behavior, not the prose).

## Verification gaps / rollback

- No schema/migration/port change — rollback is reverting the branch.
- Full `just ci` before push catches the tool-docs/snapshot and architecture tests outside the
  touched dirs (the historical trap: a changed maturity-meta prose or descriptor breaks a
  generated `docs/.../reference` doc only under full CI).
- Live proof is explicitly **not** in scope (B6 #680); the two named live-gaps (guest DHCP,
  in-guest `kdive-drgn`/`drgn`) fail-fast with honest categories.
