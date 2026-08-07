#!/usr/bin/env python3
"""
hmc_verify_q4_ssh_sessions.py — Probe the practical concurrent SSH session
limit on a live HMC, answering open question 4 from issue #1808.

Usage:
    python scripts/hmc_verify_q4_ssh_sessions.py

Environment (from env.sh):
    HMC_HOST  — HMC hostname or IP
    HMC_USER  — HMC username
    HMC_PASS  — HMC password

Strategy:
    Open N concurrent SSH connections in parallel, each running a trivial
    command (lshmc -V).  Ramp from 1 up to MAX_SESSIONS in steps of STEP,
    collecting (concurrent_count, success_count, failure_count, wall_time_s)
    at each level.

    The "limit" is where failures first appear or where success_count drops
    below concurrent_count.  An explicit "session limit exceeded" error string
    from the HMC is also captured if present.

    This is *read-only* — it issues only lshmc -V, which has no side effects.

Results are printed to stdout and written to /tmp/hmc_q4_sessions.json.
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ── Configuration ─────────────────────────────────────────────────────────────

HMC_HOST = os.environ.get("HMC_HOST", "")
HMC_USER = os.environ.get("HMC_USER", "")
HMC_PASS = os.environ.get("HMC_PASS", "")

MAX_SESSIONS = 50   # ceiling — stop well below WebUI limit of 1000
STEP = 5            # sessions to add per ramp level
RESULTS_PATH = "/tmp/hmc_q4_sessions.json"

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=10",
    "-o", "LogLevel=ERROR",   # suppress PQ-key warning noise
]

PROBE_CMD = "lshmc -V"   # fast, read-only, always available


# ── SSH helpers ───────────────────────────────────────────────────────────────

def _one_session(session_id: int) -> dict:
    """Open one SSH connection, run PROBE_CMD, return a result dict."""
    env = os.environ.copy()
    env["SSHPASS"] = HMC_PASS
    t0 = time.monotonic()
    result = subprocess.run(
        ["sshpass", "-e", "ssh"] + SSH_OPTS + [f"{HMC_USER}@{HMC_HOST}", PROBE_CMD],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    elapsed = round(time.monotonic() - t0, 3)
    ok = result.returncode == 0
    # Capture any session-limit message from stderr or stdout
    combined = (result.stdout + result.stderr).lower()
    session_limit_hit = any(
        phrase in combined
        for phrase in (
            "session limit",
            "too many sessions",
            "max_sessions",
            "connection refused",
            "connection reset",
            "too many logins",
        )
    )
    return {
        "session_id": session_id,
        "ok": ok,
        "rc": result.returncode,
        "elapsed_s": elapsed,
        "session_limit_hit": session_limit_hit,
        "stderr_snippet": result.stderr.strip()[:200] if not ok else "",
    }


# ── Ramp logic ────────────────────────────────────────────────────────────────

def probe_level(n: int) -> dict:
    """Fire n sessions in parallel, wait for all, return aggregated result."""
    t0 = time.monotonic()
    individual: list[dict] = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {pool.submit(_one_session, i): i for i in range(n)}
        for future in as_completed(futures):
            try:
                individual.append(future.result())
            except Exception as exc:
                individual.append({
                    "session_id": futures[future],
                    "ok": False,
                    "rc": -1,
                    "elapsed_s": 0,
                    "session_limit_hit": False,
                    "stderr_snippet": str(exc),
                })
    wall = round(time.monotonic() - t0, 2)
    successes = sum(1 for r in individual if r["ok"])
    failures = n - successes
    limit_signals = sum(1 for r in individual if r["session_limit_hit"])
    avg_elapsed = round(
        sum(r["elapsed_s"] for r in individual) / max(len(individual), 1), 3
    )
    return {
        "concurrent": n,
        "success": successes,
        "failure": failures,
        "session_limit_signals": limit_signals,
        "wall_s": wall,
        "avg_session_s": avg_elapsed,
        "failures_detail": [r for r in individual if not r["ok"]],
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def _check_env() -> None:
    missing = [v for v in ("HMC_HOST", "HMC_USER", "HMC_PASS") if not os.environ.get(v)]
    if missing:
        sys.exit(f"Missing required environment variables: {', '.join(missing)}\nSource env.sh first.")


def main() -> None:
    _check_env()

    print("=== HMC Q4 Concurrent SSH Session Probe ===", flush=True)
    print(f"    HMC  : {HMC_HOST}", flush=True)
    print(f"    User : {HMC_USER}", flush=True)
    print(f"    Time : {datetime.now(timezone.utc).isoformat()}", flush=True)
    print(f"    Range: 1..{MAX_SESSIONS} in steps of {STEP}", flush=True)
    print(flush=True)

    # Baseline: single session must succeed before we ramp
    print("[*] Baseline check (1 session)...", flush=True)
    baseline = probe_level(1)
    if baseline["failure"] > 0:
        sys.exit(
            f"[!] Baseline single-session failed — check HMC connectivity.\n"
            f"    Detail: {baseline['failures_detail']}"
        )
    print(f"[+] Baseline OK ({baseline['avg_session_s']} s)\n", flush=True)

    levels: list[dict] = [baseline]
    first_failure_at: int | None = None

    for n in range(STEP, MAX_SESSIONS + 1, STEP):
        print(f"[*] Probing {n} concurrent sessions...", flush=True)
        result = probe_level(n)
        levels.append(result)
        status = "OK" if result["failure"] == 0 else f"FAIL({result['failure']} failures)"
        limit_note = "  ← SESSION LIMIT SIGNALLED" if result["session_limit_signals"] else ""
        print(
            f"    concurrent={n:3d}  success={result['success']:3d}  failure={result['failure']:3d}"
            f"  wall={result['wall_s']:6.2f}s  avg_session={result['avg_session_s']:.3f}s"
            f"  {status}{limit_note}",
            flush=True,
        )
        if result["failure"] > 0 and first_failure_at is None:
            first_failure_at = n
            print(f"    First failures at n={n}. Sample errors:", flush=True)
            for d in result["failures_detail"][:3]:
                print(f"      [{d['session_id']}] rc={d['rc']} {d['stderr_snippet']}", flush=True)

        # Stop if all sessions failed (hard wall hit)
        if result["success"] == 0:
            print(f"[!] All sessions failed at n={n}. Stopping ramp.", flush=True)
            break

    # ── Summary ────────────────────────────────────────────────────────────
    print(flush=True)
    print("=== Summary ===", flush=True)
    if first_failure_at is None:
        print(f"  No failures observed up to {MAX_SESSIONS} concurrent sessions.", flush=True)
        print(f"  The practical SSH ceiling is > {MAX_SESSIONS} (or limit is session-type-gated).", flush=True)
    else:
        prev = first_failure_at - STEP
        print(f"  First failure: {first_failure_at} concurrent sessions", flush=True)
        print(f"  Last clean level: {prev} concurrent sessions", flush=True)
        print(f"  Practical SSH concurrency window: {prev}–{first_failure_at}", flush=True)

    output = {
        "hmc_host": HMC_HOST,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "max_probed": MAX_SESSIONS,
        "step": STEP,
        "first_failure_at": first_failure_at,
        "levels": levels,
    }
    with open(RESULTS_PATH, "w") as fh:
        json.dump(output, fh, indent=2)
    print(f"\n[+] Full results written to {RESULTS_PATH}", flush=True)


if __name__ == "__main__":
    main()
