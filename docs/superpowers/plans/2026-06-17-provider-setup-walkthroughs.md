# Provider Setup Walkthroughs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship two task-oriented provider walkthroughs (local-libvirt, remote-libvirt) plus reference helper scripts that run the #497 onboarding commands, so a first `allocations.request` never dead-ends on `quota_exceeded`.

**Architecture:** A small, unit-tested Python MCP-call helper (`scripts/kdive_set_accounting.py`) calls the audited `accounting.set_quota`/`set_budget`/`usage_project` tools over streamable HTTP. Two shell wrappers (`scripts/setup-*-libvirt.sh`) run the matching preflight, obtain a token, and onboard the demo project — local defaults to token-less `seed-demo`, remote always uses the audited MCP path. Two walkthrough docs stitch prepare→install→onboard→test into a linear path, linking canonical reference rather than restating it.

**Tech Stack:** Bash (`set -euo pipefail`, shellcheck/shfmt), Python 3.13 (fastmcp-slim 3.4.0 `Client` + `StreamableHttpTransport`), pytest, Markdown.

**Design spec:** `docs/superpowers/specs/2026-06-17-provider-setup-walkthroughs-design.md`

## Global Constraints

- Shell scripts start with `set -euo pipefail`; must be `shellcheck`- and `shfmt -i 2`-clean; ≤100-char lines.
- Python: absolute imports only (`from fastmcp import ...`, `import scripts.x`); `ruff check`/`ruff format`/`ty check` clean; ≤100-char lines; Google-style docstrings on public functions.
- Onboarding is idempotent — `set_quota`/`set_budget` and `seed-demo` are upserts; re-running is safe and `set_budget` preserves `spent_kcu`.
- Demo defaults (verbatim, matching `seed-demo`): project `demo`, `limit_kcu` `1000000`, `max_concurrent_allocations` `4`, `max_concurrent_systems` `4`, `max_pending_allocations` `0`. All overridable by flag/env.
- Every helper carries a demo-only warning: the bundled mock issuer mints a valid token for any caller; never point these scripts at a real deployment.
- MCP endpoint base URL **must end in `/mcp`** (FastMCP serves there; a bare host 307s).
- Docs: link-don't-restate; every relative link must resolve (`scripts/check-doc-links.sh`); any `docs/<path>` token in a script/doc must point at a real file (`scripts/check-doc-paths.sh`).
- Local-libvirt deployment = KDIVE app processes as **host services** (`deploy/systemd/`) where the worker has native `/dev/kvm` + libvirt; **not** the app-tier-only docker-compose worker. Remote-libvirt = Helm/k8s control plane driving a separate TLS target host.
- **Interpreter:** `kdive` and `fastmcp` live in the project venv (the systemd unit runs `/opt/kdive/.venv/bin/python`), not the system `python3`. Scripts select the interpreter via `PY="${KDIVE_PYTHON:-python3}"` and run the `scripts.*` helper from the repo root (`REPO_ROOT="$(dirname "${SCRIPT_DIR}")"`) so the package resolves. Run the setup scripts from the repo checkout with the venv active, or set `KDIVE_PYTHON=/opt/kdive/.venv/bin/python`.

---

### Task 1: MCP accounting-call helper (`scripts/kdive_set_accounting.py`)

A standalone fastmcp client that onboards a project through the audited admin tools. Both shell scripts (Tasks 2–3) call it; it is the only place that talks MCP.

**Files:**
- Create: `scripts/kdive_set_accounting.py`
- Test: `tests/scripts/test_kdive_set_accounting.py`

**Interfaces:**
- Consumes: a bearer token (env `KDIVE_TOKEN` or `--token`) and an MCP base URL (`--base`, ending in `/mcp`).
- Produces (later tasks rely on these exact names): module `scripts.kdive_set_accounting` with `build_calls(ns) -> list[tuple[str, dict]]`, `async def run(ns: argparse.Namespace) -> int`, and `parse(argv: list[str] | None = None) -> argparse.Namespace`. CLI: `python -m scripts.kdive_set_accounting --base URL [--project demo] [--limit-kcu 1000000] [--max-concurrent-allocations 4] [--max-concurrent-systems 4] [--max-pending-allocations 0] [--token T]`. Exit 0 on success, 2 on missing token, 1 if any tool result is an error.

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_kdive_set_accounting.py
"""Behavioral tests for scripts/kdive_set_accounting.py (no live server)."""

from __future__ import annotations

import asyncio
from typing import Any

import scripts.kdive_set_accounting as acct


class _FakeResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.is_error = False
        self.structured_content = payload


class _FakeClient:
    """Records call_tool invocations; satisfies the async-context-manager protocol."""

    calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, transport: Any) -> None:
        self.transport = transport

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any], *, raise_on_error: bool):
        type(self).calls.append((name, arguments))
        return _FakeResult({"object": "ok", "data": dict(arguments)})


def test_build_calls_uses_flat_quota_params_and_defaults() -> None:
    ns = acct.parse(["--base", "http://h/mcp"])
    calls = acct.build_calls(ns)
    names = [n for n, _ in calls]
    assert names == ["accounting.set_quota", "accounting.set_budget", "accounting.usage_project"]
    quota = dict(calls)["accounting.set_quota"]
    assert quota == {
        "project": "demo",
        "max_concurrent_allocations": 4,
        "max_concurrent_systems": 4,
        "max_pending_allocations": 0,
    }
    assert dict(calls)["accounting.set_budget"] == {"project": "demo", "limit_kcu": "1000000"}


def test_run_invokes_three_tools_with_bearer(monkeypatch) -> None:
    _FakeClient.calls = []
    monkeypatch.setattr(acct, "Client", _FakeClient)
    ns = acct.parse(["--base", "http://h/mcp", "--token", "T", "--project", "acme"])
    rc = asyncio.run(acct.run(ns))
    assert rc == 0
    assert [n for n, _ in _FakeClient.calls] == [
        "accounting.set_quota",
        "accounting.set_budget",
        "accounting.usage_project",
    ]


def test_run_without_token_exits_2(monkeypatch) -> None:
    monkeypatch.delenv("KDIVE_TOKEN", raising=False)
    ns = acct.parse(["--base", "http://h/mcp"])
    assert asyncio.run(acct.run(ns)) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_kdive_set_accounting.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.kdive_set_accounting'`.

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/kdive_set_accounting.py
"""Onboard a project through the audited accounting admin tools over MCP.

Calls ``accounting.set_quota`` then ``accounting.set_budget`` (and reads back
``accounting.usage_project``) against a running KDIVE server's MCP endpoint, using a
bearer token that carries the project ``admin`` role. This is the production-style,
audited alternative to ``seed-demo``'s raw INSERTs (see
``docs/operating/project-onboarding.md``).

DEMO/operator helper. The bundled mock OIDC issuer mints a valid token for any caller, so
never point this at a real deployment; production supplies its own token via ``KDIVE_TOKEN``.
The ``--base`` URL must end in ``/mcp``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the accounting onboarding call."""
    p = argparse.ArgumentParser(prog="kdive_set_accounting.py")
    p.add_argument("--base", required=True, help="server MCP endpoint, must end in /mcp")
    p.add_argument("--project", default="demo")
    p.add_argument("--limit-kcu", dest="limit_kcu", default="1000000")
    p.add_argument("--max-concurrent-allocations", dest="max_alloc", type=int, default=4)
    p.add_argument("--max-concurrent-systems", dest="max_sys", type=int, default=4)
    p.add_argument("--max-pending-allocations", dest="max_pending", type=int, default=0)
    p.add_argument("--token", default=os.environ.get("KDIVE_TOKEN"))
    return p.parse_args(argv)


def build_calls(ns: argparse.Namespace) -> list[tuple[str, dict[str, object]]]:
    """Return the ordered (tool, arguments) pairs for onboarding ``ns.project``."""
    return [
        (
            "accounting.set_quota",
            {
                "project": ns.project,
                "max_concurrent_allocations": ns.max_alloc,
                "max_concurrent_systems": ns.max_sys,
                "max_pending_allocations": ns.max_pending,
            },
        ),
        ("accounting.set_budget", {"project": ns.project, "limit_kcu": ns.limit_kcu}),
        ("accounting.usage_project", {"project": ns.project}),
    ]


async def run(ns: argparse.Namespace) -> int:
    """Execute the onboarding calls; return a process exit code."""
    if not ns.token:
        print("error: no token (set KDIVE_TOKEN or pass --token)", file=sys.stderr)
        return 2
    transport = StreamableHttpTransport(
        url=ns.base, headers={"Authorization": f"Bearer {ns.token}"}
    )
    rc = 0
    async with Client(transport) as client:
        for name, arguments in build_calls(ns):
            result = await client.call_tool(name, arguments, raise_on_error=False)
            if getattr(result, "is_error", False):
                print(f"error: tool {name} failed", file=sys.stderr)
                rc = 1
            print(json.dumps(result.structured_content, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse())))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_kdive_set_accounting.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Lint, type-check, format**

Run: `uv run ruff check scripts/kdive_set_accounting.py tests/scripts/test_kdive_set_accounting.py && uv run ruff format --check scripts/kdive_set_accounting.py && uv run ty check scripts/kdive_set_accounting.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add scripts/kdive_set_accounting.py tests/scripts/test_kdive_set_accounting.py
git commit -m "feat(scripts): MCP accounting onboarding helper for #497"
```

---

### Task 2: Local-libvirt setup script (`scripts/setup-local-libvirt.sh`)

Preflight, then onboard. Default path is token-less `seed-demo` (works against the stock compose issuer); the audited MCP path (Task 1 helper) runs only when `KDIVE_SETUP_AUDITED=1` and `KDIVE_MCP_BASE` are set.

**Files:**
- Create: `scripts/setup-local-libvirt.sh`
- Modify: `justfile` (add the `setup-local-libvirt` recipe after `check-local-libvirt`, around line 21)
- Test: `tests/scripts/test_setup_local_libvirt.py`

**Interfaces:**
- Consumes: `scripts/check-local-libvirt.sh` (preflight, resolved via `SCRIPT_DIR`); `python3 -m kdive seed-demo`; `scripts.kdive_set_accounting` (audited path). Env: `KDIVE_SETUP_AUDITED`, `KDIVE_MCP_BASE`, `KDIVE_TOKEN`, plus the demo-default overrides (`KDIVE_PROJECT`, `KDIVE_LIMIT_KCU`, `KDIVE_MAX_ALLOC`, `KDIVE_MAX_SYS`).
- Produces: an onboarded `demo` project (budget + quota rows) on the local host deployment.

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_setup_local_libvirt.py
"""Behavioral tests for scripts/setup-local-libvirt.sh via PATH stubs."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-local-libvirt.sh"
BASH = shutil.which("bash")


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _healthy_local(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Stubs that make the REAL check-local-libvirt.sh pass under this PATH.
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    calllog = tmp_path / "python.log"
    _stub(bindir, "python3", f'echo "$@" >> "{calllog}"\nexit 0')
    # Stub bin first so it shadows real python3/virsh/etc.; system bins follow so the
    # scripts' `dirname` (and other coreutils) resolve.
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "KDIVE_KVM_NODE": str(kvm)}
    return bindir, env, calllog


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, check=False)


def test_default_path_runs_seed_demo(tmp_path: Path) -> None:
    _bindir, env, calllog = _healthy_local(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    logged = calllog.read_text()
    assert "-m kdive seed-demo" in logged
    assert "--project demo" in logged


def test_audited_path_runs_mcp_helper_not_seed_demo(tmp_path: Path) -> None:
    _bindir, env, calllog = _healthy_local(tmp_path)
    env |= {"KDIVE_SETUP_AUDITED": "1", "KDIVE_MCP_BASE": "http://localhost:8000/mcp",
            "KDIVE_TOKEN": "T"}
    result = _run(env)
    assert result.returncode == 0, result.stderr
    logged = calllog.read_text()
    assert "kdive_set_accounting" in logged
    assert "seed-demo" not in logged


def test_preflight_failure_aborts(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", "exit 0")
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    calllog = tmp_path / "python.log"
    _stub(bindir, "python3", f'echo "$@" >> "{calllog}"\nexit 0')
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path),
           "KDIVE_KVM_NODE": str(tmp_path / "absent")}  # unreadable -> preflight fails
    result = _run(env)
    assert result.returncode != 0
    # Onboarding must not run when the preflight aborts.
    assert not calllog.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_setup_local_libvirt.py -q`
Expected: FAIL — script does not exist (bash exits non-zero / no such file).

- [ ] **Step 3: Write the script**

```bash
#!/usr/bin/env bash
# Onboard the local-libvirt demo project so the first allocations.request is granted
# instead of dead-ending on quota_exceeded (#497).
#
# Runs the local-libvirt preflight, then seeds the demo project's budget + quota:
#   default       : python -m kdive seed-demo  (token-less; the local host path)
#   audited (opt) : the role-gated accounting.set_quota / set_budget MCP tools, when
#                   KDIVE_SETUP_AUDITED=1 and KDIVE_MCP_BASE are set (needs an OIDC issuer
#                   configured to assert the project-admin claims and a KDIVE_TOKEN).
#
# DEMO ONLY: the bundled mock issuer mints a valid token for any caller. Never run the
# audited path against a real deployment; production onboards via the audited admin tools
# with a real token (see the project-onboarding guide).
#
# Env overrides: KDIVE_PROJECT (demo), KDIVE_LIMIT_KCU (1000000), KDIVE_MAX_ALLOC (4),
#   KDIVE_MAX_SYS (4); KDIVE_SETUP_AUDITED, KDIVE_MCP_BASE, KDIVE_TOKEN for the audited path.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"

# kdive/fastmcp live in the project venv, not the system python3. Override with KDIVE_PYTHON
# (e.g. /opt/kdive/.venv/bin/python) on a host-services deployment.
readonly PY="${KDIVE_PYTHON:-python3}"
readonly PROJECT="${KDIVE_PROJECT:-demo}"
readonly LIMIT_KCU="${KDIVE_LIMIT_KCU:-1000000}"
readonly MAX_ALLOC="${KDIVE_MAX_ALLOC:-4}"
readonly MAX_SYS="${KDIVE_MAX_SYS:-4}"

main() {
  "${SCRIPT_DIR}/check-local-libvirt.sh"

  if [[ "${KDIVE_SETUP_AUDITED:-0}" == "1" ]]; then
    : "${KDIVE_MCP_BASE:?set KDIVE_MCP_BASE (…/mcp) for the audited path}"
    (cd "${REPO_ROOT}" && "${PY}" -m scripts.kdive_set_accounting \
      --base "${KDIVE_MCP_BASE}" \
      --project "${PROJECT}" \
      --limit-kcu "${LIMIT_KCU}" \
      --max-concurrent-allocations "${MAX_ALLOC}" \
      --max-concurrent-systems "${MAX_SYS}")
    printf "onboarded project %s via audited admin tools\n" "${PROJECT}"
    return 0
  fi

  "${PY}" -m kdive seed-demo \
    --project "${PROJECT}" \
    --limit-kcu "${LIMIT_KCU}" \
    --max-concurrent-allocations "${MAX_ALLOC}" \
    --max-concurrent-systems "${MAX_SYS}"
  printf "onboarded project %s via seed-demo\n" "${PROJECT}"
}

main "$@"
```

Make it executable:

```bash
chmod +x scripts/setup-local-libvirt.sh
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_setup_local_libvirt.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Add the justfile recipe**

Insert after the `check-local-libvirt` recipe (after line 21):

```
# Onboard the local-libvirt demo project (preflight + seed budget/quota). See #497.
setup-local-libvirt:
    ./scripts/setup-local-libvirt.sh
```

- [ ] **Step 6: Shell lint/format**

Run: `shellcheck scripts/setup-local-libvirt.sh && shfmt -i 2 -d scripts/setup-local-libvirt.sh`
Expected: no findings, no diff.

- [ ] **Step 7: Commit**

```bash
git add scripts/setup-local-libvirt.sh tests/scripts/test_setup_local_libvirt.py justfile
git commit -m "feat(scripts): local-libvirt onboarding wrapper for #497"
```

---

### Task 3: Remote-libvirt setup script (`scripts/setup-remote-libvirt.sh`)

Preflight against the target host, obtain a project-`admin` token (reuse `scripts/demo-token.sh` in-cluster, or honor a supplied `KDIVE_TOKEN`), then onboard via the audited MCP helper.

**Files:**
- Create: `scripts/setup-remote-libvirt.sh`
- Modify: `justfile` (add `setup-remote-libvirt` recipe after `check-remote-libvirt`, around line 25)
- Test: `tests/scripts/test_setup_remote_libvirt.py`

**Interfaces:**
- Consumes: `scripts/check-remote-libvirt.sh HOST USER URI` (preflight via `SCRIPT_DIR`); a token (`KDIVE_TOKEN` if set, else `scripts/demo-token.sh`); `scripts.kdive_set_accounting`. Required args/env: positional `HOST [USER] [URI]`; `KDIVE_MCP_BASE` (the port-forwarded `…/mcp`); demo-default overrides as in Task 2.
- Produces: an onboarded `demo` project on the remote-libvirt deployment.

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_setup_remote_libvirt.py
"""Behavioral tests for scripts/setup-remote-libvirt.sh via PATH stubs."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-remote-libvirt.sh"
BASH = shutil.which("bash")


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _healthy_remote(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "ssh", "exit 0")
    _stub(bindir, "virsh", "exit 0")
    calllog = tmp_path / "python.log"
    _stub(bindir, "python3", f'echo "$@" >> "{calllog}"\nexit 0')
    pki = tmp_path / "pki"
    pki.mkdir()
    (pki / "clientcert.pem").write_text("x")
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "kdive-agent").write_text("x")
    env = {
        # Stub bin first (shadows ssh/virsh/python3); system bins follow so `dirname` resolves.
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "KDIVE_REMOTE_PKI_DIR": str(pki),
        "KDIVE_GUEST_HELPERS_DIR": str(helpers),
        "KDIVE_TOKEN": "T",
        "KDIVE_MCP_BASE": "http://127.0.0.1:8000/mcp",
    }
    return env, calllog


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


def test_onboards_via_mcp_helper(tmp_path: Path) -> None:
    env, calllog = _healthy_remote(tmp_path)
    result = _run(env, "target.example", "root")
    assert result.returncode == 0, result.stderr
    logged = calllog.read_text()
    assert "kdive_set_accounting" in logged
    assert "--base http://127.0.0.1:8000/mcp" in logged


def test_missing_host_arg_fails(tmp_path: Path) -> None:
    env, _calllog = _healthy_remote(tmp_path)
    result = _run(env)  # no HOST
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_setup_remote_libvirt.py -q`
Expected: FAIL — script does not exist.

- [ ] **Step 3: Write the script**

```bash
#!/usr/bin/env bash
# Onboard the remote-libvirt demo project so the first allocations.request is granted
# instead of dead-ending on quota_exceeded (#497).
#
# Runs the remote-libvirt preflight against the target host, obtains a project-admin token
# (KDIVE_TOKEN if set, else scripts/demo-token.sh in-cluster), then seeds the demo project's
# budget + quota via the role-gated accounting.set_quota / set_budget MCP tools.
#
# KDIVE_MCP_BASE must point at the server's MCP endpoint and end in /mcp. The in-cluster
# server is ClusterIP-only, so port-forward first, e.g.:
#   kubectl port-forward svc/<release>-server 8000:8000
#   export KDIVE_MCP_BASE=http://127.0.0.1:8000/mcp
#
# DEMO ONLY: the bundled mock issuer mints a valid token for any caller. Never run against a
# real deployment; production supplies its own token via KDIVE_TOKEN.
#
# Usage: setup-remote-libvirt.sh HOST [USER] [URI]
# Env: KDIVE_MCP_BASE (required), KDIVE_TOKEN (optional; else demo-token.sh),
#   KDIVE_PROJECT (demo), KDIVE_LIMIT_KCU (1000000), KDIVE_MAX_ALLOC (4), KDIVE_MAX_SYS (4).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"

# fastmcp lives in the project venv, not the system python3. Override with KDIVE_PYTHON
# (e.g. /opt/kdive/.venv/bin/python) if you are not running inside the venv.
readonly PY="${KDIVE_PYTHON:-python3}"
readonly PROJECT="${KDIVE_PROJECT:-demo}"
readonly LIMIT_KCU="${KDIVE_LIMIT_KCU:-1000000}"
readonly MAX_ALLOC="${KDIVE_MAX_ALLOC:-4}"
readonly MAX_SYS="${KDIVE_MAX_SYS:-4}"

usage() {
  echo "usage: setup-remote-libvirt.sh HOST [USER] [URI]" >&2
}

main() {
  if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    return 1
  fi
  : "${KDIVE_MCP_BASE:?set KDIVE_MCP_BASE (…/mcp); port-forward the ClusterIP server first}"

  "${SCRIPT_DIR}/check-remote-libvirt.sh" "$@"

  local token="${KDIVE_TOKEN:-}"
  if [[ -z "${token}" ]]; then
    token="$("${SCRIPT_DIR}/demo-token.sh")"
  fi

  (cd "${REPO_ROOT}" && KDIVE_TOKEN="${token}" "${PY}" -m scripts.kdive_set_accounting \
    --base "${KDIVE_MCP_BASE}" \
    --project "${PROJECT}" \
    --limit-kcu "${LIMIT_KCU}" \
    --max-concurrent-allocations "${MAX_ALLOC}" \
    --max-concurrent-systems "${MAX_SYS}")
  printf "onboarded project %s via audited admin tools\n" "${PROJECT}"
}

main "$@"
```

Make it executable:

```bash
chmod +x scripts/setup-remote-libvirt.sh
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_setup_remote_libvirt.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Add the justfile recipe**

Insert after the `check-remote-libvirt` recipe (after line 25):

```
# Onboard the remote-libvirt demo project (preflight + token + audited budget/quota). See #497.
setup-remote-libvirt host user="root" uri="":
    ./scripts/setup-remote-libvirt.sh {{host}} {{user}} {{uri}}
```

- [ ] **Step 6: Shell lint/format**

Run: `shellcheck scripts/setup-remote-libvirt.sh && shfmt -i 2 -d scripts/setup-remote-libvirt.sh`
Expected: no findings, no diff.

- [ ] **Step 7: Commit**

```bash
git add scripts/setup-remote-libvirt.sh tests/scripts/test_setup_remote_libvirt.py justfile
git commit -m "feat(scripts): remote-libvirt onboarding wrapper for #497"
```

---

### Task 3b: Catalogue the new env vars and regenerate the config reference

The setup scripts introduce seven new `KDIVE_*` tokens. The repo guard `scripts/check_env_documented.py` sweeps `src/ tests/ scripts/ deploy/` and fails on any `KDIVE_*` that is neither a registry setting nor catalogued in `kdive.config.external_env`; that guard runs in the full suite (`tests/scripts/test_check_env_documented.py`). The catalogue also feeds `scripts/gen_config_reference.py`, and `just config-docs-check` fails if `docs/guide/reference/config.md` is stale. This task closes both gates.

**Files:**
- Modify: `src/kdive/config/external_env.py` (append to `EXTERNAL_ENV_VARS`)
- Modify: `docs/guide/reference/config.md` (regenerated, not hand-edited)

**Interfaces:**
- Consumes: the `ExternalEnvVar(name, category, default, help)` dataclass already defined in `external_env.py`.
- Produces: catalogue coverage for `KDIVE_PYTHON`, `KDIVE_SETUP_AUDITED`, `KDIVE_MCP_BASE`, `KDIVE_PROJECT`, `KDIVE_LIMIT_KCU`, `KDIVE_MAX_ALLOC`, `KDIVE_MAX_SYS`.

- [ ] **Step 1: Show the guard fails first**

Run: `uv run python scripts/check_env_documented.py`
Expected: FAIL — lists the seven new tokens as undocumented (run this after Tasks 2–3 have added the scripts).

- [ ] **Step 2: Append the catalogue entries**

In `src/kdive/config/external_env.py`, add to the `EXTERNAL_ENV_VARS` tuple, in the `# --- operator shell scripts ---` block:

```python
    ExternalEnvVar(
        "KDIVE_PYTHON",
        "script",
        "python3",
        "Python interpreter the setup-*-libvirt.sh scripts invoke (set to the project venv, "
        "e.g. /opt/kdive/.venv/bin/python, when not running inside the venv).",
    ),
    ExternalEnvVar(
        "KDIVE_SETUP_AUDITED",
        "script",
        "0",
        "When 1, setup-local-libvirt.sh onboards via the audited MCP admin tools instead of "
        "seed-demo (requires KDIVE_MCP_BASE and a project-admin KDIVE_TOKEN).",
    ),
    ExternalEnvVar(
        "KDIVE_MCP_BASE",
        "script",
        None,
        "Server MCP endpoint (must end in /mcp) the setup-*-libvirt.sh onboarding calls target.",
    ),
    ExternalEnvVar(
        "KDIVE_PROJECT",
        "script",
        "demo",
        "Project the setup-*-libvirt.sh scripts onboard.",
    ),
    ExternalEnvVar(
        "KDIVE_LIMIT_KCU",
        "script",
        "1000000",
        "Budget ceiling (KCU) the setup-*-libvirt.sh scripts set for the project.",
    ),
    ExternalEnvVar(
        "KDIVE_MAX_ALLOC",
        "script",
        "4",
        "max_concurrent_allocations quota the setup-*-libvirt.sh scripts set.",
    ),
    ExternalEnvVar(
        "KDIVE_MAX_SYS",
        "script",
        "4",
        "max_concurrent_systems quota the setup-*-libvirt.sh scripts set.",
    ),
```

- [ ] **Step 3: Verify the env guard passes**

Run: `uv run python scripts/check_env_documented.py`
Expected: exit 0, no undocumented tokens.

- [ ] **Step 4: Regenerate the config reference and confirm it is current**

Run: `uv run python scripts/gen_config_reference.py && just config-docs-check`
Expected: `config.md` updated with the seven new variables; `config-docs-check` passes (no diff).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/config/external_env.py docs/guide/reference/config.md
git commit -m "feat(config): catalogue setup-script env vars for #497"
```

---

### Task 4: Local-libvirt walkthrough doc

**Files:**
- Create: `docs/operating/providers/local-libvirt-walkthrough.md`
- Modify: `docs/operating/providers/local-libvirt.md` (add a pointer near the top, after the opening paragraph)
- Modify: `docs/operating/index.md` (add a row to the Providers table)

**Interfaces:**
- Consumes: `scripts/setup-local-libvirt.sh` (Task 2), `just check-local-libvirt`, `python -m kdive seed-demo`.
- Produces: a linear local-libvirt prepare→install→onboard→test page.

- [ ] **Step 1: Write the walkthrough**

Create `docs/operating/providers/local-libvirt-walkthrough.md` with these sections and exact content. Keep prose tight; link rather than restate.

````markdown
# local-libvirt walkthrough

End-to-end setup for the local-libvirt provider, where the KDIVE worker drives QEMU/KVM
guests on its own host. For the provider's prerequisites and config see
[the local-libvirt provider reference](local-libvirt.md); this page is the linear path from a
prepared host to a verified run.

> **Deployment:** the KDIVE app processes run as **host services** on the libvirt host (see
> [systemd](../systemd.md)), so the worker has native `/dev/kvm` and libvirt access. The
> app-tier-only [docker-compose](../docker-compose.md) worker has no KVM/libvirt access and
> cannot drive this provider — use compose only for the backends (Postgres, MinIO, OIDC).

## 1. Prepare

Run the read-only preflight; fix anything it reports before continuing:

```bash
just check-local-libvirt
```

## 2. Install

Bring up the backends, then run the three KDIVE processes as host services:

```bash
docker compose up -d --wait postgres minio oidc
docker compose run --rm minio-init
```

Install and start the host services as described in [systemd](../systemd.md), then apply the
schema with `python -m kdive migrate`. See [Local stack administration](../local-stack.md)
for the package-on-host layout.

## 3. Onboard the project

A fresh database has no quota or budget, so the first `allocations.request` would dead-end on
`quota_exceeded`. Seed the demo project's budget and quota. Run this from the repo checkout
with the project venv active (or set `KDIVE_PYTHON=/opt/kdive/.venv/bin/python`), so `kdive`
resolves:

```bash
just setup-local-libvirt
```

By default this runs `python -m kdive seed-demo`, which writes the budget/quota rows with no
token. To onboard through the audited, role-gated admin tools instead (the production-style
path), set `KDIVE_SETUP_AUDITED=1` and supply a project-`admin` token in `KDIVE_TOKEN` — this
path needs an OIDC issuer configured to assert the project-`admin` claims, and the local
script does **not** mint a token for you (unlike the remote one):

```bash
KDIVE_SETUP_AUDITED=1 \
  KDIVE_MCP_BASE=http://localhost:8000/mcp \
  KDIVE_TOKEN="$(your-issuer-mint-command)" \
  just setup-local-libvirt
```

See [Project onboarding](../project-onboarding.md) for the audited-onboarding rationale and
why `kdivectl` cannot perform these writes.

## 4. Test the lifecycle

With the project onboarded, request an allocation and drive a System through its lifecycle:

```bash
# allocations.request → provision → build → boot → verify → teardown → release
```

Issue these as MCP tool calls from an agent session or a scripted client. For the deep
build→boot→debug steps and the canonical dcache `dhash_entries` verification, follow the
[four-method live run](../runbooks/four-method-live-run.md) and
[live stack](../runbooks/live-stack.md) runbooks. A successful run reaches a ready System via
`provision` (minimum) and ideally completes teardown and release.
````

- [ ] **Step 2: Add the pointer to the reference doc**

In `docs/operating/providers/local-libvirt.md`, insert the following as its own paragraph between the opening paragraph and `## What it needs`, with a blank line both before and after it:

```markdown
> Setting up from scratch? See the [local-libvirt walkthrough](local-libvirt-walkthrough.md).
```

- [ ] **Step 3: Add the index row**

In `docs/operating/index.md`, in the Providers table, add this as a new table row directly below the existing `Local libvirt` row — no blank line between rows (a blank line splits the table):

```markdown
| [Local libvirt walkthrough](providers/local-libvirt-walkthrough.md) | End-to-end local-libvirt setup: prepare, install, onboard, test |
```

- [ ] **Step 4: Verify doc links/paths resolve**

Run: `./scripts/check-doc-links.sh && ./scripts/check-doc-paths.sh`
Expected: exit 0, no missing targets.

- [ ] **Step 5: Commit**

```bash
git add docs/operating/providers/local-libvirt-walkthrough.md docs/operating/providers/local-libvirt.md docs/operating/index.md
git commit -m "docs: local-libvirt setup walkthrough for #497"
```

---

### Task 5: Remote-libvirt walkthrough doc

**Files:**
- Create: `docs/operating/providers/remote-libvirt-walkthrough.md`
- Modify: `docs/operating/providers/remote-libvirt.md` (add a pointer near the top)
- Modify: `docs/operating/index.md` (add a row to the Providers table)

**Interfaces:**
- Consumes: `scripts/setup-remote-libvirt.sh` (Task 3), `just check-remote-libvirt`, the remote-libvirt host-setup runbook, `kubectl port-forward`.
- Produces: a linear remote-libvirt prepare→install→onboard→test page.

- [ ] **Step 1: Write the walkthrough**

Create `docs/operating/providers/remote-libvirt-walkthrough.md`:

````markdown
# remote-libvirt walkthrough

End-to-end setup for the remote-libvirt provider, where the KDIVE worker drives QEMU/KVM
guests on a separate TLS target host. For the provider's prerequisites and config see
[the remote-libvirt provider reference](remote-libvirt.md); this page is the linear path from
a prepared target host to a verified run.

> **Deployment:** a Helm/k8s control plane drives a separate TLS target host. The worker needs
> no local KVM — the guest runs on the target host. Provisioning that target (PKI,
> `virtproxyd`, firewall ACL, guest image) is a prerequisite, covered by the
> [remote-libvirt host setup runbook](../runbooks/remote-libvirt-host-setup.md).

## 1. Prepare

Provision the target host per the
[remote-libvirt host setup runbook](../runbooks/remote-libvirt-host-setup.md), then run the
read-only preflight from where the worker will connect:

```bash
just check-remote-libvirt HOST USER qemu+tls://HOST/system
```

## 2. Install

Deploy the control plane with the chart and attach the provider (see
[Kubernetes (Helm)](../kubernetes.md) and the
[Kubernetes deploy runbook](../runbooks/kubernetes-deploy.md)):

```bash
helm install kdive deploy/helm/kdive -n kdive-demo -f deploy/helm/kdive/values-demo.yaml --wait
```

## 3. Onboard the project

The chart seeds build-configs but **not** quota or budget, so the first `allocations.request`
dead-ends on `quota_exceeded` (this is issue #497). Onboard the demo project through the
audited admin tools. The in-cluster server is ClusterIP-only, so port-forward its MCP
endpoint first:

Run this from the repo checkout with the project venv active (or set
`KDIVE_PYTHON=/opt/kdive/.venv/bin/python`), so `fastmcp` resolves:

```bash
kubectl port-forward -n kdive-demo svc/kdive-kdive-server 8000:8000 &
export KDIVE_MCP_BASE=http://127.0.0.1:8000/mcp
just setup-remote-libvirt HOST root qemu+tls://HOST/system
```

The script mints a project-`admin` token in-cluster (`scripts/demo-token.sh`) and calls
`accounting.set_quota` + `accounting.set_budget`. Supply your own token with `KDIVE_TOKEN` to
skip the demo mint. See [Project onboarding](../project-onboarding.md) for the production
path.

## 4. Test the lifecycle

With the project onboarded, request an allocation and drive a System through its lifecycle:

```bash
# allocations.request → provision → build → boot → verify → teardown → release
```

Issue these as MCP tool calls against the port-forwarded endpoint. For the deep
build→boot→debug steps and the canonical dcache `dhash_entries` verification, follow the
[remote live stack](../runbooks/remote-live-stack.md) and
[four-method live run](../runbooks/four-method-live-run.md) runbooks. The full
build→boot→verify needs the real target hardware; a successful run reaches at least a ready
System via `provision`.
````

- [ ] **Step 2: Add the pointer to the reference doc**

In `docs/operating/providers/remote-libvirt.md`, insert the following as its own paragraph between the opening paragraph and `## What it needs`, with a blank line both before and after it:

```markdown
> Setting up from scratch? See the [remote-libvirt walkthrough](remote-libvirt-walkthrough.md).
```

- [ ] **Step 3: Add the index row**

In `docs/operating/index.md`, in the Providers table, add this as a new table row directly below the existing `Remote libvirt` row — no blank line between rows (a blank line splits the table):

```markdown
| [Remote libvirt walkthrough](providers/remote-libvirt-walkthrough.md) | End-to-end remote-libvirt setup: prepare, install, onboard, test |
```

- [ ] **Step 4: Verify doc links/paths resolve**

Run: `./scripts/check-doc-links.sh && ./scripts/check-doc-paths.sh`
Expected: exit 0, no missing targets.

- [ ] **Step 5: Commit**

```bash
git add docs/operating/providers/remote-libvirt-walkthrough.md docs/operating/providers/remote-libvirt.md docs/operating/index.md
git commit -m "docs: remote-libvirt setup walkthrough for #497"
```

---

## Final verification

- [ ] Run the full new-file test set: `uv run pytest tests/scripts/test_kdive_set_accounting.py tests/scripts/test_setup_local_libvirt.py tests/scripts/test_setup_remote_libvirt.py -q` — all pass.
- [ ] Run `shellcheck scripts/setup-local-libvirt.sh scripts/setup-remote-libvirt.sh` and `shfmt -i 2 -d scripts/setup-*-libvirt.sh` — clean.
- [ ] Run `uv run ruff check scripts/ tests/scripts/ && uv run ty check scripts/kdive_set_accounting.py` — clean.
- [ ] Run `uv run python scripts/check_env_documented.py` — exit 0 (the seven new `KDIVE_*` tokens are catalogued, Task 3b).
- [ ] Run `just config-docs-check` — passes (the regenerated `config.md` matches the committed copy).
- [ ] Run `./scripts/check-doc-links.sh && ./scripts/check-doc-paths.sh` — exit 0.
- [ ] Run the full suite once before pushing (boundary/arch tests live outside touched dirs): `just test` (or `uv run pytest -q`).
- [ ] **Manual acceptance (per the spec's Verification):** on a live local host deployment, `just setup-local-libvirt` followed by an `allocations.request` returns granted (not `quota_exceeded`) and reaches a ready System via `provision`. Record what was executed; the remote full build→boot→verify needs real target hardware.
