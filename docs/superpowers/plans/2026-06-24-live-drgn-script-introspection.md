# Live in-guest drgn script introspection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `introspect.script(session_id, script, timeout_sec?)` — run a caller-supplied drgn script against the live guest kernel of an open drgn-live `DebugSession`, returning its stdout.

**Architecture:** A synchronous server-side MCP read (like `introspect.run`) that drives a new `LiveIntrospector.run_script` port, realized over the existing drgn-live SSH transport (local) and qemu-guest-agent (remote). The script rides **stdin** to a new `kdive-drgn run-script` mode so the remote single-program guest-agent allowlist is unchanged. Live-only; the offline half is #781.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`; FastMCP tools; drgn (operator-provided, `live_vm`-gated); bash in-guest helper.

**Spec:** `docs/superpowers/specs/2026-06-24-live-drgn-script-introspection-design.md` · **ADR:** `docs/adr/0240-live-drgn-script-introspection.md`

## Global Constraints

- Run guardrails before every commit: `just lint` (ruff check + format), `just type` (ty, **whole tree**), and the focused tests for the task. Zero warnings.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Absolute imports only (no `..`). Google-style docstrings on non-trivial public APIs.
- `ErrorCategory` taxonomy only — pick the most specific existing value; never invent strings.
- All guest-derived output passes the `Redactor` (secret-registry) before it leaves the port — but only masks **platform secrets** (the managed SSH key / registered secrets), not dump content.
- Conventional-commit subjects ≤72 chars, imperative; end each commit message with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Doc-style: plain prose; never "critical/crucial/essential/significant/comprehensive/robust/elegant"; "Milestone" not "Sprint".
- The introspect surface is **server-side** (a `_PLANE_REGISTRARS` tool via `asyncio.to_thread`), not a worker job.
- `timeout_sec` is clamped to `[1.0, KDIVE_LIVE_SCRIPT_MAX_TIMEOUT_SECONDS]` (default ceiling `600`) **before** it reaches the guest; `0`/negative/non-finite clamp up to the floor.
- New `IntrospectionMode` value is `"live-script"`; descriptor admission reuses `_require_introspection` (ADR-0209). fault-inject does **not** advertise it.
- No DB migration. No committed schema-snapshot change (free-form `data.output`).

---

## File structure

| File | Responsibility | Change |
|------|----------------|--------|
| `src/kdive/providers/ports/lifecycle.py` | `IntrospectionMode` literal + `INTROSPECTION_MODES` | add `"live-script"` |
| `src/kdive/providers/ports/retrieve.py` | introspect port contracts | add `LiveScriptOutput`, `LiveIntrospector.run_script` |
| `src/kdive/providers/ports/__init__.py` | port exports | export `LiveScriptOutput` |
| `src/kdive/providers/fault_inject/debug/introspect.py` | synthetic introspect ports | add synthetic `run_script` (satisfy Protocol) |
| `src/kdive/config/core_settings.py` | config registry | add `LIVE_SCRIPT_MAX_TIMEOUT_SECONDS` |
| `src/kdive/providers/shared/debug_common/introspect.py` | redact+byte-cap boundary | add `assemble_script_output` |
| `src/kdive/providers/remote_libvirt/guest/agent.py` | guest-agent exec | `run(..., input_data=None)` → `guest-exec` `input-data` |
| `deploy/remote-libvirt-guest-helpers/kdive-drgn` | in-guest helper (local + remote share it) | add `run-script` stdin mode |
| `src/kdive/providers/local_libvirt/debug/introspect.py` | local live introspect | add `run_script` + SSH-stdin seam |
| `src/kdive/providers/remote_libvirt/debug/introspect.py` | remote live introspect | add `run_script` (guest-agent input-data) |
| `src/kdive/providers/{local_libvirt,remote_libvirt}/composition.py` | descriptors | add `"live-script"` to `supported_introspection` |
| `src/kdive/mcp/tools/debug/introspect.py` | MCP tools | add `introspect.script` tool + handler + clamp |
| `src/kdive/mcp/exposure.py` | RBAC tool→role map | `introspect.script: _CONTRIBUTOR` |
| `tests/integration/test_live_drgn_script.py` | live_vm proof | new `live_vm` test |

---

### Task 1: Port surface — `LiveScriptOutput`, `run_script`, `live-script` mode

**Files:**
- Modify: `src/kdive/providers/ports/lifecycle.py:31-32`
- Modify: `src/kdive/providers/ports/retrieve.py:31-101`
- Modify: `src/kdive/providers/ports/__init__.py` (exports)
- Modify: `src/kdive/providers/fault_inject/debug/introspect.py`
- Test: `tests/providers/ports/test_introspection_modes.py` (create), `tests/providers/fault_inject/debug/test_introspect.py` (extend or create)

**Interfaces:**
- Produces: `IntrospectionMode` now includes `"live-script"`; `INTROSPECTION_MODES` includes it. `LiveScriptOutput(output: str, truncated: bool)` (NamedTuple). `LiveIntrospector.run_script(*, transport_handle: str, script: str, timeout_sec: float) -> LiveScriptOutput`.

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/ports/test_introspection_modes.py
from kdive.providers.ports import IntrospectionMode  # noqa: F401
from kdive.providers.ports.lifecycle import INTROSPECTION_MODES


def test_live_script_is_a_known_introspection_mode():
    assert "live-script" in INTROSPECTION_MODES
```

- [ ] **Step 2: Run it — expect FAIL**

Run: `uv run python -m pytest tests/providers/ports/test_introspection_modes.py -q`
Expected: FAIL (`'live-script' not in INTROSPECTION_MODES`).

- [ ] **Step 3: Extend the mode literal**

```python
# src/kdive/providers/ports/lifecycle.py
IntrospectionMode = Literal["offline-vmcore", "live", "live-script"]
INTROSPECTION_MODES: frozenset[IntrospectionMode] = frozenset(
    ("offline-vmcore", "live", "live-script")
)
```

- [ ] **Step 4: Add the port type + method**

```python
# src/kdive/providers/ports/retrieve.py  (near IntrospectOutput)
class LiveScriptOutput(NamedTuple):
    output: str
    truncated: bool
```

Extend the `LiveIntrospector` Protocol:

```python
class LiveIntrospector(Protocol):
    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        ...

    def run_script(
        self, *, transport_handle: str, script: str, timeout_sec: float
    ) -> LiveScriptOutput:
        """Run a caller-supplied drgn script in-guest; return its byte-capped stdout.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a malformed handle or an
                over-cap script, ``MISSING_DEPENDENCY`` off the ``live_vm`` gate,
                ``TRANSPORT_FAILURE`` for an unreachable transport/timeout, or
                ``DEBUG_ATTACH_FAILURE`` for a non-zero in-guest drgn exit.
        """
        ...
```

Export it from `src/kdive/providers/ports/__init__.py` (add `LiveScriptOutput` to the `from .retrieve import (...)` block and to `__all__`).

- [ ] **Step 5: Satisfy the Protocol in fault-inject (synthetic, never admitted)**

```python
# src/kdive/providers/fault_inject/debug/introspect.py
from kdive.providers.ports import IntrospectOutput, LiveScriptOutput

class FaultInjectIntrospect:
    ...
    def run_script(
        self, *, transport_handle: str, script: str, timeout_sec: float
    ) -> LiveScriptOutput:
        # fault-inject does not advertise the "live-script" mode, so the descriptor
        # gate rejects before this is reached; the synthetic body only satisfies the port.
        return LiveScriptOutput(output="", truncated=False)
```

Add a fault-inject test asserting the synthetic shape:

```python
# tests/providers/fault_inject/debug/test_introspect.py
from kdive.providers.fault_inject.debug.introspect import FaultInjectIntrospect


def test_run_script_returns_synthetic_output():
    out = FaultInjectIntrospect().run_script(transport_handle="x", script="print(1)", timeout_sec=5.0)
    assert out.output == "" and out.truncated is False
```

- [ ] **Step 6: Run tests + ty — expect PASS**

Run: `uv run python -m pytest tests/providers/ports/test_introspection_modes.py tests/providers/fault_inject/debug/test_introspect.py -q && just type`
Expected: PASS; ty clean (the new Protocol method is satisfied by all three realizations once Tasks 6–7 land — fault-inject satisfies it now; local/remote are added later, so run `just type` again at the end of Task 7).

- [ ] **Step 7: Commit**

```bash
git add src/kdive/providers/ports/lifecycle.py src/kdive/providers/ports/retrieve.py \
        src/kdive/providers/ports/__init__.py src/kdive/providers/fault_inject/debug/introspect.py \
        tests/providers/ports/test_introspection_modes.py tests/providers/fault_inject/debug/test_introspect.py
git commit -m "feat(introspect): add live-script mode + LiveIntrospector.run_script port"
```

---

### Task 2: Config setting `KDIVE_LIVE_SCRIPT_MAX_TIMEOUT_SECONDS`

**Files:**
- Modify: `src/kdive/config/core_settings.py` (add `Setting` + register in the settings tuple ~line 589)
- Test: `tests/config/test_core_settings.py` (extend; mirror an existing `_int` setting test)

**Interfaces:**
- Produces: `LIVE_SCRIPT_MAX_TIMEOUT_SECONDS` Setting; `config.require(LIVE_SCRIPT_MAX_TIMEOUT_SECONDS)` returns an int seconds (default 600), readable in the `server` process.

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_core_settings.py  (add)
import kdive.config as config
from kdive.config.core_settings import LIVE_SCRIPT_MAX_TIMEOUT_SECONDS


def test_live_script_max_timeout_default(monkeypatch):
    monkeypatch.delenv("KDIVE_LIVE_SCRIPT_MAX_TIMEOUT_SECONDS", raising=False)
    assert config.require(LIVE_SCRIPT_MAX_TIMEOUT_SECONDS) == 600
```

- [ ] **Step 2: Run it — expect FAIL** (`ImportError: LIVE_SCRIPT_MAX_TIMEOUT_SECONDS`).

Run: `uv run python -m pytest tests/config/test_core_settings.py::test_live_script_max_timeout_default -q`

- [ ] **Step 3: Add the Setting**

```python
# src/kdive/config/core_settings.py  (near the other debug/_int settings)
LIVE_SCRIPT_MAX_TIMEOUT_SECONDS = Setting(
    name="KDIVE_LIVE_SCRIPT_MAX_TIMEOUT_SECONDS",
    parse=_int,
    default="600",
    group="debug",
    processes=_SERVER,
    help=(
        "Upper bound (seconds) the server clamps an agent-chosen `introspect.script` "
        "`timeout_sec` to before it drives the in-guest `timeout drgn -k` wrapper. A "
        "deployment policy bounding how long one live drgn script can hold a server "
        "thread-pool slot; single-tenant operators may set it high (ADR-0240)."
    ),
    suggest="an integer number of seconds, e.g. 600",
)
```

Add `LIVE_SCRIPT_MAX_TIMEOUT_SECONDS,` to the settings registry tuple (where `REPORT_INLINE_MAX_BYTES,` / `DEBUG_DIR,` are listed, ~line 589).

- [ ] **Step 4: Run test + env-docs guard — expect PASS**

Run: `uv run python -m pytest tests/config/test_core_settings.py::test_live_script_max_timeout_default -q && just env-docs-check`
Expected: PASS. If `env-docs-check` fails (token undocumented), regenerate the committed config reference: `just config-docs` and re-run `just config-docs-check`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/config/core_settings.py tests/config/test_core_settings.py docs/
git commit -m "feat(config): add KDIVE_LIVE_SCRIPT_MAX_TIMEOUT_SECONDS ceiling"
```

---

### Task 3: Shared `assemble_script_output` (redact + byte-cap boundary)

**Files:**
- Modify: `src/kdive/providers/shared/debug_common/introspect.py` (add function + `__all__`)
- Test: `tests/providers/debug_common/test_introspect.py` (extend; mirror the `assemble_report` redaction/byte-cap tests)

**Interfaces:**
- Consumes: `SecretRegistry`, `Redactor` (already imported in the module).
- Produces: `assemble_script_output(stdout: str, *, byte_cap: int, secret_registry: SecretRegistry) -> LiveScriptOutput` — redacts platform secrets first (single boundary), then truncates to `byte_cap` UTF-8 bytes, setting `truncated`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/providers/debug_common/test_introspect.py  (add)
from kdive.providers.ports import LiveScriptOutput
from kdive.providers.shared.debug_common.introspect import assemble_script_output
from kdive.security.secrets.secret_registry import SecretRegistry


def test_script_output_redacts_registered_secret():
    reg = SecretRegistry()
    reg.register("TOPSECRET", scope=None)
    out = assemble_script_output("value=TOPSECRET\n", byte_cap=1024, secret_registry=reg)
    assert "TOPSECRET" not in out.output
    assert out.truncated is False


def test_script_output_byte_caps_and_flags_truncated():
    reg = SecretRegistry()
    out = assemble_script_output("x" * 100, byte_cap=10, secret_registry=reg)
    assert len(out.output.encode("utf-8")) <= 10
    assert out.truncated is True
    assert isinstance(out, LiveScriptOutput)
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError: assemble_script_output`).

Run: `uv run python -m pytest tests/providers/debug_common/test_introspect.py -q -k script_output`

- [ ] **Step 3: Implement**

```python
# src/kdive/providers/shared/debug_common/introspect.py
from kdive.providers.ports import LiveScriptOutput  # add to imports

def assemble_script_output(
    stdout: str, *, byte_cap: int, secret_registry: SecretRegistry
) -> LiveScriptOutput:
    """Redact platform secrets (single boundary), then UTF-8 byte-cap the script stdout.

    Redaction precedes the cap so the cap bounds the returned (redacted) payload exactly,
    matching ``assemble_report``'s ordering. ``truncated`` is set when the cap trims bytes.
    """
    redactor = Redactor(registry=secret_registry)
    redacted = redactor.redact_value(stdout)
    text = redacted if isinstance(redacted, str) else str(redacted)
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_cap:
        return LiveScriptOutput(output=text, truncated=False)
    clipped = encoded[:byte_cap].decode("utf-8", "ignore")
    return LiveScriptOutput(output=clipped, truncated=True)
```

Add `"assemble_script_output"` to `__all__`.

- [ ] **Step 4: Run — expect PASS**

Run: `uv run python -m pytest tests/providers/debug_common/test_introspect.py -q -k script_output`

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/shared/debug_common/introspect.py tests/providers/debug_common/test_introspect.py
git commit -m "feat(introspect): add assemble_script_output redact+byte-cap helper"
```

---

### Task 4: `GuestAgentExec` stdin (`input-data`) support

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/guest/agent.py:208-252` (`run` + `_spawn`)
- Test: `tests/providers/remote_libvirt/guest/test_agent.py` (extend; the suite already drives the two-phase protocol with a fake `agent_command`)

**Interfaces:**
- Produces: `GuestAgentExec.run(domain, argv, *, input_data: str | None = None)`; when set, `_spawn` adds base64 `input-data` to the `guest-exec` arguments. Allowlist enforcement on `argv[0]` is unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/remote_libvirt/guest/test_agent.py  (add; reuse the suite's fake agent_command + domain)
import base64, json

def test_run_passes_input_data_as_base64(fake_agent):  # fake_agent: existing fixture pattern
    captured = {}
    def agent_command(domain, command, timeout, flags):
        msg = json.loads(command)
        if msg["execute"] == "guest-exec":
            captured["args"] = msg["arguments"]
            return {"return": {"pid": 7}}
        return {"return": {"exited": True, "exitcode": 0, "out-data": "", "err-data": ""}}
    exec_ = GuestAgentExec(agent_command=agent_command,
                           allowed_programs=frozenset({"/usr/local/sbin/kdive-drgn"}))
    exec_.run(_FakeDomain("dom"), ["/usr/local/sbin/kdive-drgn", "run-script", "30"],
              input_data="print(1)\n")
    assert captured["args"]["input-data"] == base64.b64encode(b"print(1)\n").decode("ascii")
```

(Adapt `_FakeDomain` / fixtures to the existing test module's helpers.)

- [ ] **Step 2: Run — expect FAIL** (`run() got an unexpected keyword argument 'input_data'`).

Run: `uv run python -m pytest tests/providers/remote_libvirt/guest/test_agent.py -q -k input_data`

- [ ] **Step 3: Implement**

```python
# src/kdive/providers/remote_libvirt/guest/agent.py
def run(
    self, domain: GuestDomain, argv: list[str], *, input_data: str | None = None
) -> AgentExecResult:
    ...  # existing empty-argv + allowlist guards unchanged
    pid = self._spawn(domain, program, args, input_data=input_data)
    return self._await_exit(domain, pid)

def _spawn(
    self, domain: GuestDomain, program: str, args: list[str], *, input_data: str | None = None
) -> int:
    arguments: dict[str, object] = {"path": program, "arg": args, "capture-output": True}
    if input_data is not None:
        arguments["input-data"] = base64.b64encode(input_data.encode("utf-8")).decode("ascii")
    command = json.dumps({"execute": "guest-exec", "arguments": arguments})
    reply = self._agent(domain, command)
    ...  # pid extraction unchanged
```

(`base64` is already imported at agent.py:19.)

- [ ] **Step 4: Run — expect PASS**; verify the existing no-input_data tests still pass.

Run: `uv run python -m pytest tests/providers/remote_libvirt/guest/test_agent.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/remote_libvirt/guest/agent.py tests/providers/remote_libvirt/guest/test_agent.py
git commit -m "feat(remote): pass guest-exec input-data stdin to GuestAgentExec.run"
```

---

### Task 5: `kdive-drgn run-script` in-guest mode

**Files:**
- Modify: `deploy/remote-libvirt-guest-helpers/kdive-drgn` (shared by local rootfs + remote base image)
- Test: `tests/scripts/test_kdive_drgn_helper.py` (create — a text/contract assertion since the real drgn run is `live_vm`)

**Interfaces:**
- Produces: `kdive-drgn run-script <timeout_sec>` reads a drgn script from **stdin**, writes it to a `mktemp`, runs `timeout <timeout_sec> drgn -k -q <tmpfile>`, removes the temp on a `trap`. The three existing helpers (`tasks|modules|sysinfo`) are unchanged.

- [ ] **Step 1: Write the failing contract test**

```python
# tests/scripts/test_kdive_drgn_helper.py
from pathlib import Path

HELPER = Path("deploy/remote-libvirt-guest-helpers/kdive-drgn")

def test_helper_has_run_script_stdin_mode():
    text = HELPER.read_text()
    assert "run-script" in text
    # script comes from stdin, never argv:
    assert "mktemp" in text and "timeout" in text and "drgn -k -q" in text
    # the fixed helpers still exist:
    assert "tasks | modules | sysinfo" in text
```

- [ ] **Step 2: Run — expect FAIL**.

Run: `uv run python -m pytest tests/scripts/test_kdive_drgn_helper.py -q`

- [ ] **Step 3: Add the mode to the helper.** Extend the `case "$helper"` dispatch:

```bash
# deploy/remote-libvirt-guest-helpers/kdive-drgn  (inside the case)
run-script)
  # Arbitrary caller drgn script over STDIN (ADR-0240). Never an argv string, so the remote
  # single-program guest-agent allowlist matches this fixed program path unchanged. Bounded
  # by the server-clamped timeout the caller passes as $2.
  timeout_sec="${2:-30}"
  user_script="$(mktemp)"
  trap 'rm -f "$user_script"' EXIT
  cat >"$user_script"    # read the drgn script from stdin
  exec timeout "$timeout_sec" drgn -k -q "$user_script"
  ;;
```

Keep the existing `tasks | modules | sysinfo)` branch and its embedded-script path as-is; only add the new `run-script)` branch (and the original `*)` unknown-helper branch still rejects anything else).

- [ ] **Step 4: Run test + shell guardrails — expect PASS / clean**

Run: `uv run python -m pytest tests/scripts/test_kdive_drgn_helper.py -q && shellcheck deploy/remote-libvirt-guest-helpers/kdive-drgn && shfmt -d deploy/remote-libvirt-guest-helpers/kdive-drgn`
Expected: PASS; shellcheck clean; shfmt no diff (run `shfmt -w` if it reports one, then re-commit).

- [ ] **Step 5: Commit**

```bash
git add deploy/remote-libvirt-guest-helpers/kdive-drgn tests/scripts/test_kdive_drgn_helper.py
git commit -m "feat(guest): add kdive-drgn run-script stdin mode"
```

---

### Task 6: `LocalLibvirtLiveIntrospect.run_script` (SSH stdin)

**Files:**
- Modify: `src/kdive/providers/local_libvirt/debug/introspect.py`
- Test: `tests/providers/local_libvirt/test_introspect_drgn.py` (extend; mirror the `introspect_live` fake-seam tests)

**Interfaces:**
- Consumes: `assemble_script_output` (Task 3), `_validate_ssh_target` (existing), the managed key seam.
- Produces: `LocalLibvirtLiveIntrospect.run_script(*, transport_handle, script, timeout_sec) -> LiveScriptOutput`. A new injected `run_live_script` seam (`(transport_handle, script, timeout_sec) -> str` stdout) defaulting to `_real_run_live_script` (SSH-exec with the script on stdin); `None` off-gate → `MISSING_DEPENDENCY`.

- [ ] **Step 1: Write the failing tests** (fake seam — no SSH):

```python
# tests/providers/local_libvirt/test_introspect_drgn.py  (add)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.debug.introspect import LocalLibvirtLiveIntrospect
from kdive.security.secrets.secret_registry import SecretRegistry


def test_run_script_returns_capped_redacted_stdout():
    reg = SecretRegistry()
    port = LocalLibvirtLiveIntrospect(
        secret_registry=reg,
        run_live_script=lambda handle, script, timeout_sec: "d_hash_shift = 0x14\n",
    )
    out = port.run_script(transport_handle="x", script="print(prog['d_hash_shift'])", timeout_sec=5.0)
    assert "0x14" in out.output and out.truncated is False


def test_run_script_off_gate_is_missing_dependency():
    port = LocalLibvirtLiveIntrospect(secret_registry=SecretRegistry(), run_live_script=None)
    try:
        port.run_script(transport_handle="x", script="print(1)", timeout_sec=5.0)
    except CategorizedError as exc:
        assert exc.category is ErrorCategory.MISSING_DEPENDENCY
    else:
        raise AssertionError("expected MISSING_DEPENDENCY")
```

- [ ] **Step 2: Run — expect FAIL**.

Run: `uv run python -m pytest tests/providers/local_libvirt/test_introspect_drgn.py -q -k run_script`

- [ ] **Step 3: Implement.** Add a `run_live_script` seam to `__init__`/`from_env`, the `run_script` method, and the `_real_run_live_script` SSH-stdin seam:

```python
# constructor: add  run_live_script: _RunLiveScript | None = None  (type alias near _RunLiveHelper)
type _RunLiveScript = Callable[[str, str, float], str]
_LIVE_SCRIPT_OUTPUT_BYTE_CAP = _REPORT_BYTE_CAP  # 1 MiB, reuse the report cap order

def run_script(self, *, transport_handle: str, script: str, timeout_sec: float) -> LiveScriptOutput:
    if self._run_live_script is None:
        raise CategorizedError(
            "live drgn script introspection runs only under the live_vm gate",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    try:
        stdout = self._run_live_script(transport_handle, script, timeout_sec)
    except Exception as exc:  # noqa: BLE001 - any seam fault becomes a typed failure
        raise _normalize_attach_error(exc, "drgn could not run the script in the live guest") from exc
    return assemble_script_output(
        stdout, byte_cap=_LIVE_SCRIPT_OUTPUT_BYTE_CAP, secret_registry=self._secret_registry
    )
```

`_real_run_live_script` mirrors `_real_run_live_helper` but execs `kdive-drgn run-script <timeout>` and writes `script` to the subprocess **stdin** (`subprocess.run(argv, input=script.encode(), timeout=timeout_sec + _SSH_SLACK_S, ...)`), with `_SSH_SLACK_S` (e.g. `10`) as the transport slack over the in-guest bound; reuse `_validate_ssh_target` + the managed-key resolution + secret registration. A non-zero exit → `DEBUG_ATTACH_FAILURE`; `TimeoutExpired`/`OSError` → `TRANSPORT_FAILURE`. Wire `from_env` to pass the real seam. `# pragma: no cover - live_vm` on the real subprocess line.

- [ ] **Step 4: Run — expect PASS** + ty.

Run: `uv run python -m pytest tests/providers/local_libvirt/test_introspect_drgn.py -q -k run_script && just type`

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/debug/introspect.py tests/providers/local_libvirt/test_introspect_drgn.py
git commit -m "feat(local): realize LiveIntrospector.run_script over drgn-live SSH"
```

---

### Task 7: `RemoteLibvirtLiveIntrospect.run_script` (guest-agent input-data)

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/debug/introspect.py`
- Test: `tests/providers/remote_libvirt/debug/test_introspect.py` (extend; the suite injects fake `agent_command` + `open_connection`)

**Interfaces:**
- Consumes: `GuestAgentExec.run(..., input_data=...)` (Task 4), `assemble_script_output` (Task 3).
- Produces: `RemoteLibvirtLiveIntrospect.run_script(*, transport_handle, script, timeout_sec) -> LiveScriptOutput` — validates the domain handle, execs `["/usr/local/sbin/kdive-drgn", "run-script", str(int(timeout_sec))]` with `input_data=script`, maps a non-zero exit to `DEBUG_ATTACH_FAILURE`, redacts + byte-caps stdout.

- [ ] **Step 1: Write the failing tests** (fake agent — no libvirt):

```python
# tests/providers/remote_libvirt/debug/test_introspect.py  (add; reuse the module's fakes)
def test_run_script_caps_and_returns_stdout(make_remote_live_introspect):  # existing helper pattern
    port = make_remote_live_introspect(exit_status=0, stdout=b"hash_shift=20\n")
    out = port.run_script(transport_handle="dom", script="print(1)", timeout_sec=5.0)
    assert "hash_shift=20" in out.output and out.truncated is False


def test_run_script_nonzero_exit_is_debug_attach_failure(make_remote_live_introspect):
    port = make_remote_live_introspect(exit_status=3, stdout=b"")
    with pytest.raises(CategorizedError) as ei:
        port.run_script(transport_handle="dom", script="boom", timeout_sec=5.0)
    assert ei.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
```

(Adapt to the existing fixtures used by the `introspect_live` tests in this file.)

- [ ] **Step 2: Run — expect FAIL**.

Run: `uv run python -m pytest tests/providers/remote_libvirt/debug/test_introspect.py -q -k run_script`

- [ ] **Step 3: Implement** — add `run_script` next to `introspect_live`, reusing `_exec` with the new `input_data`:

```python
def run_script(self, *, transport_handle: str, script: str, timeout_sec: float) -> LiveScriptOutput:
    domain_name = transport_handle.strip()
    if not domain_name:
        raise CategorizedError(
            "remote live introspection handle must carry a domain name",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    argv = [_DRGN_HELPER, "run-script", str(int(timeout_sec))]
    result = self._exec(domain_name, argv, input_data=script)
    if result.exit_status != 0:
        raise CategorizedError(
            "in-guest drgn script exited non-zero (could not attach or script error)",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details={"domain": domain_name, "exit_status": result.exit_status},
        )
    return assemble_script_output(
        result.stdout.decode("utf-8", "replace"),
        byte_cap=_REPORT_BYTE_CAP,
        secret_registry=self._secret_registry,
    )
```

Thread `input_data` through `_exec` to `agent.run(domain, argv, input_data=input_data)`.

- [ ] **Step 4: Run — expect PASS** + ty (now all three `LiveIntrospector` realizations have `run_script`).

Run: `uv run python -m pytest tests/providers/remote_libvirt/debug/test_introspect.py -q -k run_script && just type`

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/remote_libvirt/debug/introspect.py tests/providers/remote_libvirt/debug/test_introspect.py
git commit -m "feat(remote): realize LiveIntrospector.run_script over guest-agent stdin"
```

---

### Task 8: Advertise `live-script` in the real-provider descriptors

**Files:**
- Modify: `src/kdive/providers/local_libvirt/composition.py:124`, `src/kdive/providers/remote_libvirt/composition.py:305`
- Leave `src/kdive/providers/fault_inject/composition.py:114` unchanged (no `live-script`).
- Test: `tests/providers/local_libvirt/test_composition.py` / remote equivalent (extend; assert `supported_introspection`)

**Interfaces:**
- Produces: local + remote `ProviderRuntime.supported_introspection == {"offline-vmcore", "live", "live-script"}`; fault-inject stays `{"offline-vmcore", "live"}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/providers/local_libvirt/test_composition.py  (add)
def test_local_descriptor_advertises_live_script(...):  # reuse the composition-build helper
    runtime = build_local_runtime(...)
    assert "live-script" in runtime.supported_introspection
```

Add the mirror remote test and a fault-inject negative test:

```python
# tests/providers/fault_inject/test_composition.py  (add)
def test_fault_inject_does_not_advertise_live_script(...):
    assert "live-script" not in build_fault_inject_runtime(...).supported_introspection
```

- [ ] **Step 2: Run — expect FAIL**.

- [ ] **Step 3: Implement** — in local + remote composition:

```python
supported_introspection=frozenset({"offline-vmcore", "live", "live-script"}),
```

- [ ] **Step 4: Run — expect PASS**.

Run: `uv run python -m pytest tests/providers/local_libvirt/test_composition.py tests/providers/remote_libvirt/test_composition.py tests/providers/fault_inject/test_composition.py -q -k introspection`

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/composition.py src/kdive/providers/remote_libvirt/composition.py tests/
git commit -m "feat(providers): advertise live-script introspection on local + remote"
```

---

### Task 9: `introspect.script` MCP tool + clamp + RBAC exposure + docs

**Files:**
- Modify: `src/kdive/mcp/tools/debug/introspect.py` (handler, clamp, registration)
- Modify: `src/kdive/mcp/exposure.py:140` (add `"introspect.script": _CONTRIBUTOR`)
- Test: `tests/mcp/test_introspect_tools.py` (extend; mirror the `introspect_run` handler tests — gate, descriptor admission, clamp, error mapping with a fake `LiveIntrospector`)

**Interfaces:**
- Consumes: `resolve_live_drgn_session` (existing), `_require_introspection` (existing), `runtime.live_introspector.run_script`, `config.require(LIVE_SCRIPT_MAX_TIMEOUT_SECONDS)`.
- Produces: tool `introspect.script(session_id, script, timeout_sec=30.0)` → `ToolResponse` with `data["output"]`, `data["truncated"]`. Module constants `_LIVE_SCRIPT: IntrospectionMode = "live-script"`, `_TIMEOUT_FLOOR = 1.0`, `_DEFAULT_TIMEOUT = 30.0`.

- [ ] **Step 1: Write the failing handler tests** (fake introspector + fake runtime; mirror existing introspect_run tests):

```python
# tests/mcp/test_introspect_tools.py  (add)
async def test_introspect_script_clamps_timeout_to_floor(...):
    captured = {}
    class _Fake:
        def run_script(self, *, transport_handle, script, timeout_sec):
            captured["timeout"] = timeout_sec
            return LiveScriptOutput(output="ok", truncated=False)
    resp = await introspect_script(pool, ctx, session_id=sid, script="print(1)",
                                   timeout_sec=0.0, introspector=_Fake())
    assert captured["timeout"] == 1.0           # floor, never 0 (coreutils timeout 0 = no bound)
    assert resp.status == "succeeded"
    assert resp.structured_content["data"]["output"] == "ok"


async def test_introspect_script_clamps_timeout_to_ceiling(monkeypatch, ...):
    monkeypatch.setenv("KDIVE_LIVE_SCRIPT_MAX_TIMEOUT_SECONDS", "600")
    captured = {}
    ...  # run_script records timeout
    await introspect_script(..., timeout_sec=99999.0, introspector=_Fake())
    assert captured["timeout"] == 600.0


async def test_introspect_script_unsupported_mode_is_capability_unsupported(...):
    # runtime descriptor lacks "live-script" -> configuration_error / capability_unsupported
    ...
```

- [ ] **Step 2: Run — expect FAIL**.

Run: `uv run python -m pytest tests/mcp/test_introspect_tools.py -q -k introspect_script`

- [ ] **Step 3: Implement the handler + clamp + registration.**

```python
# src/kdive/mcp/tools/debug/introspect.py
import math
import kdive.config as config
from kdive.config.core_settings import LIVE_SCRIPT_MAX_TIMEOUT_SECONDS
from kdive.mcp.tools._docmeta import MaturityReason  # for partial maturity
from kdive.providers.ports import LiveScriptOutput

_LIVE_SCRIPT: IntrospectionMode = "live-script"
_TIMEOUT_FLOOR = 1.0
_DEFAULT_SCRIPT_TIMEOUT = 30.0

def _clamp_timeout(requested: float) -> float:
    ceiling = float(config.require(LIVE_SCRIPT_MAX_TIMEOUT_SECONDS))
    if not math.isfinite(requested) or requested < _TIMEOUT_FLOOR:
        requested = _TIMEOUT_FLOOR
    return min(requested, ceiling)

async def introspect_script(
    pool: AsyncConnectionPool, ctx: RequestContext, *, session_id: str, script: str,
    timeout_sec: float, introspector: LiveIntrospector,
) -> ToolResponse:
    """Run a caller drgn script over a live drgn-live DebugSession; return capped stdout."""
    clamped = _clamp_timeout(timeout_sec)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            try:
                resolved = await resolve_live_drgn_session(conn, ctx, session_id)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(session_id, exc)
        try:
            output = await asyncio.to_thread(
                introspector.run_script,
                transport_handle=resolved.transport_handle,
                script=script,
                timeout_sec=clamped,
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(session_id, exc)
        return ToolResponse.success(
            session_id, "succeeded",
            suggested_next_actions=["introspect.script", "debug.end_session"],
            data=cast(ResponseData, {
                "output": output.output,
                "truncated": str(output.truncated).lower(),
                "transcript_sensitivity": "sensitive",
            }),
        )
```

Register the tool in `register(...)` mirroring `introspect_run_tool` but: `annotations=_docmeta.mutating()`, `meta=_docmeta.maturity_meta("partial", reason=MaturityReason.LIVE_DEPENDENCY, detail="needs an operator drgn-live host; CI exercises only the fake seam", promotion="a live_vm proof runs a real script end-to-end")`, a `script: Annotated[str, Field(...)]` param and `timeout_sec: Annotated[float, Field(...)] = 30.0`, gating with `_require_introspection(session_id, runtime, _LIVE_SCRIPT)` before calling `introspect_script(..., introspector=runtime.live_introspector)`.

Add to `src/kdive/mcp/exposure.py` after the `introspect.run` line:

```python
    "introspect.script": _CONTRIBUTOR,
```

- [ ] **Step 4: Run handler tests + exposure guard + regenerate tool docs**

Run: `uv run python -m pytest tests/mcp/test_introspect_tools.py -q -k introspect_script`
Then: `just docs` (regenerate the agent-facing tool reference — a new tool changes it) and `just docs-check`.
Expected: tests PASS; `docs-check` green after regen. If an exposure-completeness test fails, the `exposure.py` entry is missing — add it.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/debug/introspect.py src/kdive/mcp/exposure.py tests/mcp/test_introspect_tools.py docs/
git commit -m "feat(introspect): add introspect.script tool with timeout clamp + RBAC"
```

---

### Task 10: Live proof + promote maturity

**Files:**
- Create: `tests/integration/test_live_drgn_script.py` (marker `live_vm`)
- Modify: `src/kdive/mcp/tools/debug/introspect.py` (promote maturity to `implemented` once proven)

**Interfaces:**
- Consumes: the full stack on a prepared KVM/libvirt host with a drgn-live-capable guest image.

- [ ] **Step 1: Write the `live_vm` test** — boot a drgn-live guest, open a drgn-live `DebugSession`, call `introspect.script` with a real script (e.g. `print(prog["init_uts_ns"].name.release.string_().decode())`) and assert the release string appears in `data.output`; a second case runs `import time; time.sleep(999)` with `timeout_sec=2.0` and asserts a `debug_attach_failure` within ~`2 + slack` seconds (the in-guest `timeout` floor/kill works).

```python
# tests/integration/test_live_drgn_script.py
import pytest

pytestmark = pytest.mark.live_vm

# ... mirror the existing live introspect integration test bring-up (drgn-live session),
# then drive introspect.script over the real MCP path.
```

- [ ] **Step 2: Run the live proof on the prepared host**

Run: `just test-live -k live_drgn_script` (this host runs `live_vm` directly — do not defer the proof).
Expected: PASS (real drgn reads the live kernel; the timeout case fails fast).

- [ ] **Step 3: Promote maturity** — once the proof passes, change the tool `meta` from `maturity_meta("partial", ...)` to `{"maturity": "implemented"}` (matching `introspect.run`), and regenerate docs.

Run: `just docs && just docs-check`

- [ ] **Step 4: Full guardrail sweep**

Run: `just lint && just type && just test`
Expected: all green (the gated `live_vm` suite is excluded from `just test`).

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_live_drgn_script.py src/kdive/mcp/tools/debug/introspect.py docs/
git commit -m "test(introspect): live_vm proof for introspect.script; promote to implemented"
```

---

## Self-review

- **Spec coverage:** surface (Task 9), in-guest stdin mode + allowlist preservation (Tasks 4, 5, 7), stateless/one-shot (Task 5–7 seams), timeout clamp floor+ceiling (Tasks 2, 9), unserialized concurrency (no lock added — Task 9 mirrors `introspect_run`), redaction+byte-cap of output (Task 3), descriptor admission `live-script` (Tasks 1, 8, 9), error contracts (Tasks 6, 7, 9), `mutating`/`contributor` (Task 9 + exposure), no migration/schema change (free-form `data.output`, Task 9), live proof + maturity (Task 10). #781 (offline fetchability) is explicitly out of scope.
- **Placeholder scan:** none — every code step shows the code; adapt-to-existing-fixtures notes point at named existing tests.
- **Type consistency:** `LiveScriptOutput(output, truncated)`, `run_script(*, transport_handle, script, timeout_sec)`, `assemble_script_output(stdout, *, byte_cap, secret_registry)`, `_clamp_timeout(requested) -> float`, mode literal `"live-script"` are used identically across Tasks 1–10.
