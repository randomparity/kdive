#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly script_dir
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
import re
from datetime import datetime
import sys
from pathlib import Path
from uuid import UUID


def fail(code: str, status: int) -> None:
    print(f"provision-evidence-error code={code}", file=sys.stderr)
    raise SystemExit(status)


try:
    import psycopg
except (ImportError, OSError):
    fail("query-unavailable", 5)


try:
    raw = Path(os.environ["KDIVE_PROVISION_EVIDENCE_TARGET"]).read_text(encoding="utf-8")
    if not raw.endswith("\n"):
        raise ValueError
    record = raw.removesuffix("\n")
    fields = record.split("\t")
    if len(fields) != 2 or "\n" in record:
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

_SYSTEM_STATES = frozenset(
    {
        "provisioning",
        "ready",
        "reprovisioning",
        "restoring",
        "paused",
        "crashing",
        "crashed",
        "torn_down",
        "failed",
    }
)
_JOB_STATES = frozenset({"queued", "running", "succeeded", "failed", "canceled"})
_LANE = re.compile(r"[a-z][a-z0-9-]{0,62}")
_WORKER = re.compile(
    r"local-systemd:kdive-live-worker@[1-8]\.service:[0-9a-f]{32}"
)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?[+-][0-9]{2}:[0-9]{2}"
)
_HEADER = (
    "system_id\tsystem_state\tjob_id\tdispatch_lane\tjob_state\tattempt\tworker_id\t"
    "enqueued_at\tlast_heartbeat_at\tlease_expires_at"
)
_MAX_OUTPUT_BYTES = 1024


def exact_text(value: object, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError
    return value


def matched_text(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError
    return value


def timestamp(value: object | None) -> str:
    if value is None:
        return "NONE"
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    rendered = value.isoformat()
    if _TIMESTAMP.fullmatch(rendered) is None:
        raise ValueError
    return rendered


try:
    row = rows[0]
    if len(row) != 10 or row[0] != str(system_id) or row[2] != str(job_id):
        raise ValueError
    attempt = row[5]
    if type(attempt) is not int or not 0 <= attempt <= 2_147_483_647:
        raise ValueError
    worker = "NONE" if row[6] is None else matched_text(row[6], _WORKER)
    values = [
        str(system_id),
        exact_text(row[1], _SYSTEM_STATES),
        str(job_id),
        matched_text(row[3], _LANE),
        exact_text(row[4], _JOB_STATES),
        str(attempt),
        worker,
        timestamp(row[7]),
        timestamp(row[8]),
        timestamp(row[9]),
    ]
    output = f"{_HEADER}\n" + "\t".join(values) + "\n"
    if len(output.encode("ascii")) > _MAX_OUTPUT_BYTES:
        raise ValueError
except (TypeError, UnicodeError, ValueError):
    fail("result-malformed", 7)

sys.stdout.write(output)
PY
