#!/usr/bin/env bash
# Mint one provisioned System for the self-hosted live_vm provisioned family (#1293, ADR-0389).
# Order: fund/onboard a project (onboard.sh) -> allocate -> provision from the warm rootfs -> poll
# ready -> print the System id (the SOLE stdout line, captured into KDIVE_LIVE_VM_SYSTEM_ID).
#
# Preconditions are CI-gated (tests/scripts/test_mint_system.py); the live allocate->provision->ready
# path needs a running stack and is proven by the local native smoke / operator nightly (plan Task 7),
# not ordinary CI.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-vm/lib.sh
source "${here}/lib.sh"

[ -n "${KDIVE_LIVE_VM_ROOTFS:-}" ] || die "KDIVE_LIVE_VM_ROOTFS unset (the warm rootfs to provision)"
[ -e "${KDIVE_LIVE_VM_ROOTFS}" ] || die "KDIVE_LIVE_VM_ROOTFS=${KDIVE_LIVE_VM_ROOTFS} does not exist"
[ -n "${KDIVE_STACK_BASE_URL:-}" ] || die "KDIVE_STACK_BASE_URL unset (bring up the stack first)"

# KDIVE_PYTHON overrides the interpreter (the self-hosted job points it at /opt/kdive's libguestfs
# venv); unset, fall back to `uv run python` (the operator dev-loop default).
if [ -n "${KDIVE_PYTHON:-}" ]; then
  py=("$KDIVE_PYTHON")
else
  py=(uv run python)
fi

# 1. Fund the project + mint a token. onboard.sh prints banners + a token-contract heredoc to stdout
#    alongside its one `export KDIVE_TOKEN=...` line, so eval ONLY that line (eval-ing the whole
#    capture hits `(advisory)` unbalanced parens under set -e and aborts before the token).
eval "$("${here}/../live-stack/onboard.sh" | grep '^export KDIVE_TOKEN=')"
[ -n "${KDIVE_TOKEN:-}" ] || die "onboard.sh did not mint a token"

# 2. allocate -> provision (from the warm rootfs) -> poll systems.get until ready -> print the id.
#    Uses the shipped kdive.mcp.dev_harness LiveStackClient (the client scripts/live-debug.py drives),
#    following the spine sequence (tests/integration/live_stack/spine.py): the System id is
#    data["system_id"] on systems.provision (object_id there is the provisioning JOB id). All progress
#    goes to stderr; stdout is the id alone.
KDIVE_LIVE_VM_ROOTFS="$KDIVE_LIVE_VM_ROOTFS" \
  KDIVE_STACK_BASE_URL="$KDIVE_STACK_BASE_URL" \
  KDIVE_TOKEN="$KDIVE_TOKEN" \
  KDIVE_MINT_PROJECT="${KDIVE_PROJECT:-demo}" \
  "${py[@]}" - <<'PY'
import asyncio
import os
import sys

from kdive.mcp.dev_harness import LiveStackClient


def _scalar(resp):
    """Unwrap a possibly-listed ToolResponse (call_tool may return one or a list)."""
    return resp[-1] if isinstance(resp, list) else resp


async def main() -> int:
    base = os.environ["KDIVE_STACK_BASE_URL"]
    token = os.environ["KDIVE_TOKEN"]
    project = os.environ["KDIVE_MINT_PROJECT"]
    rootfs = os.environ["KDIVE_LIVE_VM_ROOTFS"]
    client = LiveStackClient.over_http(base, token)

    await _scalar(await client.call_tool("investigations.open", project=project, title="live-vm-mint"))
    alloc = _scalar(
        await client.call_tool(
            "allocations.request",
            project=project,
            vcpus=2,
            memory_gb=2,
            disk_gb=10,
            resource={"mode": "kind", "kind": "local-libvirt"},
        )
    )
    if alloc.status in {"error", "failed"}:
        print(f"allocations.request {alloc.status}: {alloc.error_category}", file=sys.stderr)
        return 1

    profile = {
        "schema_version": 1,
        "arch": os.uname().machine,
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 10,
        "boot_method": "direct-kernel",
        "provider": {"local-libvirt": {"rootfs": {"kind": "local", "path": rootfs}}},
    }
    prov = _scalar(await client.call_tool("systems.provision", allocation_id=alloc.object_id, profile=profile))
    if prov.status in {"error", "failed"}:
        print(f"systems.provision {prov.status}: {prov.error_category}", file=sys.stderr)
        return 1
    system_id = prov.data.get("system_id")  # in data, NOT object_id (the provisioning job id)
    if not system_id:
        print("systems.provision returned no data.system_id", file=sys.stderr)
        return 1

    for _ in range(180):  # poll up to ~15 min (native KVM boot); the tcg deadline is generous
        env = _scalar(await client.call_tool("systems.get", system_id=system_id))
        if env.status in {"error", "failed"}:
            print(f"systems.get {env.status}: {env.error_category}", file=sys.stderr)
            return 1
        state = env.data.get("status")
        if state == "ready":
            print(system_id)  # the sole stdout line
            return 0
        if state in {"failed", "error"}:
            print(f"System {system_id} reached {state}", file=sys.stderr)
            return 1
        await asyncio.sleep(5)
    print(f"System {system_id} did not reach ready in time", file=sys.stderr)
    return 1


raise SystemExit(asyncio.run(main()))
PY
