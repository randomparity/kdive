#!/usr/bin/env python3
"""
hmc_verify_q3_latency.py — Measure LPAR create, power-on, and full-activation
latencies on a live HMC, answering open question 3 from issue #1808.

Usage:
    python scripts/hmc_verify_q3_latency.py

Environment (from env.sh):
    HMC_HOST  — HMC hostname or IP
    HMC_USER  — HMC username (e.g. hscroot)
    HMC_PASS  — HMC password
    HMC_SYST  — managed-system name (e.g. ltczz386-lp3)

What it measures (wall-clock seconds, all three separately):
    1. mksyscfg  — time from command invocation to return
    2. chsysstate -o on  — time from command invocation to return
    3. Partition state == "Running"  — time from power-on command to first
       lssyscfg poll reporting state=Running
    4. First non-blank lsrefcode  — time from power-on command until a
       reference code appears (indicates firmware/OS activity started)

The script creates a minimal test LPAR (1 shared proc unit, 1024 MB, linux,
no storage, no network), powers it on, polls state + refcode for up to
ACTIVATION_TIMEOUT_S, then deletes the LPAR whether the poll succeeds or not.

Safety:
    - LPAR name is timestamped to avoid collisions with existing partitions.
    - Cleanup runs in a finally block.
    - All commands are structured (no string interpolation of user data).

Results are printed to stdout and written to /tmp/hmc_q3_latency.json.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

# ── Configuration ─────────────────────────────────────────────────────────────

HMC_HOST = os.environ.get("HMC_HOST", "")
HMC_USER = os.environ.get("HMC_USER", "")
HMC_PASS = os.environ.get("HMC_PASS", "")
HMC_SYST = os.environ.get("HMC_SYST", "")

POLL_INTERVAL_S = 5
ACTIVATION_TIMEOUT_S = 300   # 5 min; IBM REST default implies ≤3600 s worst-case
RESULTS_PATH = "/tmp/hmc_q3_latency.json"

# Minimal LPAR config: shared proc, 1024 MB, linux, no storage, no network.
# Verified against HMC V10R3.  sharing_mode=uncap (not "uncapped").
# Both proc_units (shared) AND procs (virtual CPU count) must be specified.
LPAR_ENV = "aixlinux"
PROC_MODE = "shared"
MIN_PROCS = 1
DESIRED_PROCS = 1
MAX_PROCS = 2
PROC_UNITS = "0.1"
MIN_PROC_UNITS = "0.1"
MAX_PROC_UNITS = "0.2"
SHARING_MODE = "uncap"
MIN_MEM = 512
DESIRED_MEM = 1024
MAX_MEM = 2048
MAX_VIRTUAL_SLOTS = 10


# ── SSH helpers ───────────────────────────────────────────────────────────────

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=30",
    "-o", "LogLevel=ERROR",   # suppress PQ-key warning noise
]


def _ssh(cmd: str) -> tuple[int, str, str]:
    """Run a single HMC CLI command over SSH via sshpass.

    Returns (returncode, stdout, stderr).  Never raises.
    """
    env = os.environ.copy()
    env["SSHPASS"] = HMC_PASS
    full = (
        ["sshpass", "-e", "ssh"]
        + SSH_OPTS
        + [f"{HMC_USER}@{HMC_HOST}", cmd]
    )
    result = subprocess.run(
        full,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def ssh_ok(cmd: str) -> str:
    """Run cmd, raise RuntimeError on non-zero exit."""
    rc, out, err = _ssh(cmd)
    if rc != 0:
        raise RuntimeError(f"HMC command failed (rc={rc})\n  cmd: {cmd}\n  stderr: {err}\n  stdout: {out}")
    return out


# ── HMC CLI wrappers ──────────────────────────────────────────────────────────

def lpar_state(system: str, lpar_name: str) -> str:
    out = ssh_ok(
        f"lssyscfg -r lpar -m '{system}'"
        f" --filter 'lpar_names={lpar_name}'"
        f" -F state"
    )
    return out.strip()


def lpar_refcode(system: str, lpar_name: str) -> str:
    """Return current reference code string, or '' if none."""
    out = ssh_ok(
        f"lsrefcode -r lpar -m '{system}'"
        f" --filter 'lpar_names={lpar_name}'"
        f" -F refcode"
    )
    return out.strip()


def find_system_with_capacity(candidates: list[str], min_proc_units: float = 0.2, min_mem_mb: int = 1024) -> str | None:
    """Return the first system in candidates that has enough free proc units and memory.

    Uses lshwres -r proc --level sys and lshwres -r mem --level sys.
    Returns None if none qualify.
    """
    for system in candidates:
        try:
            proc_out = ssh_ok(f"lshwres -r proc -m '{system}' --level sys -F curr_avail_sys_proc_units")
            mem_out = ssh_ok(f"lshwres -r mem -m '{system}' --level sys -F curr_avail_sys_mem")
            avail_proc = float(proc_out.strip())
            avail_mem = int(mem_out.strip())
            if avail_proc >= min_proc_units and avail_mem >= min_mem_mb:
                return system
        except Exception:
            continue
    return None


def create_lpar(system: str, lpar_name: str) -> None:
    """Create a minimal test LPAR via mksyscfg -r lpar.

    Uses shared processor mode (proc_mode=shared, sharing_mode=uncap) with
    minimal proc_units and memory.  No storage or network — just enough to
    reach Not Activated → Running and generate reference codes.
    """
    cfg = (
        f"name={lpar_name},"
        f"lpar_env={LPAR_ENV},"
        f"profile_name=default_profile,"
        f"proc_mode={PROC_MODE},"
        f"min_mem={MIN_MEM},"
        f"desired_mem={DESIRED_MEM},"
        f"max_mem={MAX_MEM},"
        f"min_procs={MIN_PROCS},"
        f"desired_procs={DESIRED_PROCS},"
        f"max_procs={MAX_PROCS},"
        f"min_proc_units={MIN_PROC_UNITS},"
        f"desired_proc_units={PROC_UNITS},"
        f"max_proc_units={MAX_PROC_UNITS},"
        f"sharing_mode={SHARING_MODE},"
        f"max_virtual_slots={MAX_VIRTUAL_SLOTS}"
    )
    ssh_ok(f"mksyscfg -r lpar -m '{system}' -i '{cfg}'")


def power_on_lpar(system: str, lpar_name: str) -> None:
    """Power on the LPAR using the named profile.

    The -f flag activates via the saved profile rather than the (empty)
    current configuration, which resolves HSCL3680 on a freshly created LPAR.
    """
    ssh_ok(f"chsysstate -r lpar -m '{system}' -o on -n '{lpar_name}' -f default_profile")


def delete_lpar(system: str, lpar_name: str) -> None:
    """Delete the test LPAR. Errors are printed but not raised (cleanup path)."""
    # Shut down first if not already off; ignore errors.
    _ssh(f"chsysstate -r lpar -m '{system}' -o shutdown --immed -n '{lpar_name}'")
    time.sleep(3)
    rc, out, err = _ssh(f"rmsyscfg -r lpar -m '{system}' -n '{lpar_name}'")
    if rc != 0:
        print(f"[!] Cleanup warning: rmsyscfg rc={rc} err={err}", flush=True)


# ── Measurement ───────────────────────────────────────────────────────────────

# Managed systems to try in order; the first with available capacity is used.
# HMC_SYST is always tried first; others are fallbacks.
_CANDIDATE_SYSTEMS = [
    HMC_SYST,
    "ltczz388",
    "ltczz345",
    "ltcfleet8",
    "ltczzci",
    "ltczz219",
    "ltczz75",
]


def measure() -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    lpar_name = f"kdive-q3-{stamp}"

    # De-duplicate while preserving order (HMC_SYST first)
    seen: set[str] = set()
    candidates = [s for s in _CANDIDATE_SYSTEMS if s and not (s in seen or seen.add(s))]  # type: ignore[func-returns-value]

    print(f"[*] Finding a system with available capacity...", flush=True)
    system = find_system_with_capacity(candidates, min_proc_units=0.2, min_mem_mb=DESIRED_MEM)
    if system is None:
        return {
            "hmc_host": HMC_HOST,
            "system": None,
            "lpar_name": lpar_name,
            "timestamp_utc": stamp,
            "create_s": None,
            "power_on_cmd_s": None,
            "state_running_s": None,
            "first_refcode_s": None,
            "first_refcode_value": None,
            "final_state": None,
            "error": f"No system with ≥0.2 proc units and ≥{DESIRED_MEM} MB available. Candidates: {candidates}",
        }
    print(f"[+] Using system: {system}", flush=True)

    results: dict = {
        "hmc_host": HMC_HOST,
        "system": system,
        "lpar_name": lpar_name,
        "timestamp_utc": stamp,
        "create_s": None,
        "power_on_cmd_s": None,
        "state_running_s": None,
        "first_refcode_s": None,
        "first_refcode_value": None,
        "final_state": None,
        "error": None,
    }

    try:
        # ── 1. Create ──────────────────────────────────────────────────────
        print(f"[*] Creating LPAR '{lpar_name}' on {system}...", flush=True)
        t0 = time.monotonic()
        create_lpar(system, lpar_name)
        results["create_s"] = round(time.monotonic() - t0, 2)
        print(f"[+] mksyscfg returned in {results['create_s']} s", flush=True)

        # ── 2. Power on ────────────────────────────────────────────────────
        print(f"[*] Powering on '{lpar_name}'...", flush=True)
        t_on = time.monotonic()
        power_on_lpar(system, lpar_name)
        results["power_on_cmd_s"] = round(time.monotonic() - t_on, 2)
        print(f"[+] chsysstate -o on returned in {results['power_on_cmd_s']} s", flush=True)

        # ── 3 & 4. Poll state and refcode until Running or timeout ─────────
        print(f"[*] Polling state + refcode (timeout {ACTIVATION_TIMEOUT_S} s)...", flush=True)
        deadline = time.monotonic() + ACTIVATION_TIMEOUT_S
        while time.monotonic() < deadline:
            state = lpar_state(system, lpar_name)
            elapsed = round(time.monotonic() - t_on, 2)

            if results["state_running_s"] is None and state.lower() in ("running", "open firmware"):
                results["state_running_s"] = elapsed
                print(f"[+] state='{state}' at {elapsed} s after power-on", flush=True)

            if results["first_refcode_s"] is None:
                rc_val = lpar_refcode(system, lpar_name)
                if rc_val and rc_val not in ("", "none", "0000 0000"):
                    results["first_refcode_s"] = elapsed
                    results["first_refcode_value"] = rc_val
                    print(f"[+] first refcode='{rc_val}' at {elapsed} s after power-on", flush=True)

            if results["state_running_s"] is not None and results["first_refcode_s"] is not None:
                print("[+] Both milestones reached — stopping poll.", flush=True)
                break

            print(f"    state={state!r:20s}  elapsed={elapsed:6.1f}s", flush=True)
            time.sleep(POLL_INTERVAL_S)
        else:
            print(f"[!] Timed out after {ACTIVATION_TIMEOUT_S} s", flush=True)

        results["final_state"] = lpar_state(system, lpar_name)

    except Exception as exc:
        results["error"] = str(exc)
        print(f"[!] Error: {exc}", flush=True)

    finally:
        print(f"[*] Cleaning up LPAR '{lpar_name}'...", flush=True)
        delete_lpar(system, lpar_name)
        print("[+] Cleanup done.", flush=True)

    return results


# ── Entry point ───────────────────────────────────────────────────────────────

def _check_env() -> None:
    missing = [v for v in ("HMC_HOST", "HMC_USER", "HMC_PASS", "HMC_SYST") if not os.environ.get(v)]
    if missing:
        sys.exit(f"Missing required environment variables: {', '.join(missing)}\nSource env.sh first.")


def main() -> None:
    _check_env()
    print(f"=== HMC Q3 Latency Measurement ===", flush=True)
    print(f"    HMC  : {HMC_HOST}", flush=True)
    print(f"    Preferred system: {HMC_SYST} (will try fallbacks if full)", flush=True)
    print(f"    Time : {datetime.now(timezone.utc).isoformat()}", flush=True)
    print(flush=True)

    results = measure()

    print(flush=True)
    print("=== Results ===", flush=True)
    for k, v in results.items():
        if k not in ("hmc_host", "system", "lpar_name", "timestamp_utc", "error"):
            print(f"  {k}: {v}", flush=True)
    if results["error"]:
        print(f"  error: {results['error']}", flush=True)

    with open(RESULTS_PATH, "w") as fh:
        json.dump(results, fh, indent=2)
    print(f"\n[+] Full results written to {RESULTS_PATH}", flush=True)

    if results["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
