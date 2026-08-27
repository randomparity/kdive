#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly script_dir

sanitize_failure() {
  local status="$1"
  local code
  case "$status" in
  2) code="target-argument" ;;
  3) code="python-unavailable" ;;
  4) code="target-malformed" ;;
  5) code="query-unavailable" ;;
  6) code="target-mismatch" ;;
  7) code="result-malformed" ;;
  *)
    status=8
    code="diagnostic-failed"
    ;;
  esac
  printf 'provision-evidence-error code=%s\n' "$code" >&2
  exit "$status"
}

# The sourced environment and interpreter are diagnostic dependencies too. Suppress their
# uncontrolled output and route every failure through the same fixed-line boundary.
# shellcheck source=scripts/live-stack/env.sh
if source "${script_dir}/env.sh" >/dev/null 2>&1; then
  :
else
  sanitize_failure "$?"
fi

if [[ $# -ne 1 ]]; then
  sanitize_failure 2
fi
export KDIVE_PROVISION_EVIDENCE_TARGET="$1"
readonly python="${KDIVE_PYTHON:-${repo_root}/.venv/bin/python}"
if [[ ! -x "$python" ]]; then
  sanitize_failure 3
fi

if "$python" - 3>&1 1>/dev/null 2>/dev/null <<'PY'; then
from __future__ import annotations

import os
import re
import stat
from datetime import datetime
from uuid import UUID


def fail(status: int) -> None:
    raise SystemExit(status)


_UUID_TEXT = rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TARGET_RECORD = re.compile(_UUID_TEXT + rb"\t" + _UUID_TEXT + rb"\n")
_TARGET_RECORD_BYTES = 74


def read_target(path: str) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError
        chunks: list[bytes] = []
        remaining = _TARGET_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


try:
    raw = read_target(os.environ["KDIVE_PROVISION_EVIDENCE_TARGET"])
    if len(raw) != _TARGET_RECORD_BYTES or _TARGET_RECORD.fullmatch(raw) is None:
        raise ValueError
    job_text, system_text = raw[:-1].split(b"\t")
    job_id = UUID(job_text.decode("ascii"))
    system_id = UUID(system_text.decode("ascii"))
except (KeyError, OSError, UnicodeError, ValueError):
    fail(4)


try:
    import psycopg
except Exception:
    fail(5)

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
    fail(5)

if len(rows) != 1:
    fail(6)

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
    fail(7)

output_bytes = output.encode("ascii")
if os.write(3, output_bytes) != len(output_bytes):
    fail(8)
PY
  exit 0
else
  sanitize_failure "$?"
fi
