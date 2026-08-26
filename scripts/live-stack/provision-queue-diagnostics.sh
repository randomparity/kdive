#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly script_dir
repo_root="$(cd "${script_dir}/../.." && pwd)"
readonly repo_root
# shellcheck source=scripts/live-stack/env.sh
source "${script_dir}/env.sh"

if [[ $# -ne 1 ]]; then
  echo "provision-evidence-error code=target-argument" >&2
  exit 2
fi
export KDIVE_PROVISION_EVIDENCE_TARGET="$1"
readonly python="${KDIVE_PYTHON:-${repo_root}/.venv/bin/python}"
if [[ ! -x "$python" ]]; then
  echo "provision-evidence-error code=python-unavailable" >&2
  exit 3
fi

exec "$python" - <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import UUID

import psycopg


def fail(code: str, status: int) -> None:
    print(f"provision-evidence-error code={code}", file=sys.stderr)
    raise SystemExit(status)


try:
    raw = Path(os.environ["KDIVE_PROVISION_EVIDENCE_TARGET"]).read_text(encoding="utf-8")
    fields = raw.removesuffix("\n").split("\t")
    if len(fields) != 2 or "\n" in raw.removesuffix("\n"):
        raise ValueError
    job_id, system_id = (UUID(value) for value in fields)
except (KeyError, OSError, UnicodeError, ValueError):
    fail("target-malformed", 4)

query = """
SELECT s.id::text, s.state::text, j.id::text, j.dispatch_lane, j.state::text,
       j.attempt, j.worker_id, j.created_at, j.heartbeat_at, j.lease_expires_at
FROM jobs AS j
JOIN systems AS s ON s.id = (j.payload->>'system_id')::uuid
WHERE j.id = %s AND j.kind = 'provision' AND s.id = %s
"""
try:
    with psycopg.connect(
        os.environ["KDIVE_SERVER_DATABASE_URL"], connect_timeout=5
    ) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '5s'")
            rows = conn.execute(query, (job_id, system_id)).fetchall()
except (KeyError, psycopg.Error):
    fail("query-unavailable", 5)

if len(rows) != 1:
    fail("target-mismatch", 6)


def render(value: object | None) -> str:
    if value is None:
        return "NONE"
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)


print(
    "system_id\tsystem_state\tjob_id\tdispatch_lane\tjob_state\tattempt\tworker_id\t"
    "enqueued_at\tlast_heartbeat_at\tlease_expires_at"
)
print("\t".join(render(value) for value in rows[0]))
PY
