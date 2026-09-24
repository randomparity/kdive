# Failed-boot crash signature on `runs.get`

Issue #2691. Builds on [ADR-0230](../../adr/0230-runs-get-failed-boot-evidence.md),
[ADR-0413](../../adr/0413-restore-boot-readiness-failure-read-surfaces.md), and
[ADR-0594](../../adr/0594-readiness-probe-failures-leave-the-probe-as-a-closed-vocabulary.md).

## Problem

A failed boot's `runs.get` `data.boot_readiness` is `{job_id, status, error_category}` plus
`expected_crash_matched: false` when the Run declared an `expected_boot_failure`. The local-libvirt
readiness scan already finds a pre-marker crash signature (`first_crash_signature`), but
`ReadinessResult` has no field for it and the booter raises a generic `READINESS_FAILURE`. An
agent that declared `panic` and got a `UBSAN:` splat cannot tell "a crash happened, but not the
declared one" from "no crash; the guest never became ready" without reading the console.

## Requirements

1. `data.boot_readiness` carries `observed_crash_signature`: the literal the readiness scanner
   matched, or `null` when the failed boot job recorded none.
2. `data.boot_readiness` carries `detail`, a one-line string that separates the crash-observed
   case from the no-signature cases and names the declared kind when one was declared.
3. The signature travels readiness result → `CategorizedError.details` → the failed boot job's
   `failure_context` → `runs.get`.
4. `expected_crash_matched` keeps its ADR-0413 derivation (`false` whenever an expectation is
   declared on this path). No new crash patterns.
5. The `runs.get` wrapper docstring and the generated tool reference name both fields.

## Design

**Provider (local-libvirt readiness).** `ReadinessResult` gains a trailing defaulted field
`crash_signature: str | None = None`, so every existing positional or keyword construction keeps
its meaning, and `ReadinessResult(True, True, None)` (the external-boot success check) still
compares equal to a success result. A private `_scan_console(data, marker)` returns the verdict
and `match.group(0)` of `first_crash_signature` over the pre-marker region; `classify_console`
returns only its verdict (signature unchanged for its callers). `_verdict_to_result` gains a
`crash_signature` keyword it sets on the `CRASHED` result, and a private `_scan_result(data,
exited)` joins the two; the polled `LocalExternalBootReadiness` and `_real_readiness` call it.
The external-boot session checks only for exact success, so the new field does not reach it.

**Booter.** `LocalLibvirtBooter._await_ready` passes the answered result's `crash_signature` to
`_boot_failure_details`, which adds `details["crash_signature"]` when it is not `None`. The
message and category are unchanged. The worker's existing `_failure_context` copies the scalar to
`failure_context["failure_detail_crash_signature"]` (redacted, truncated) — the same channel
ADR-0594 uses for `probe_error`. No schema change. Because `ToolResponse.from_job` merges
`failure_context` into a failed job's envelope, the key is also visible on `jobs.get` /
`jobs.wait` for that job; its value is a closed-vocabulary literal, never console text.

**Service.** `BootAttempt` gains `observed_crash_signature: str | None = None`.
`failed_boot_attempt` reads `failure_detail_crash_signature` from the job's `failure_context` and
keeps it only when `is_crash_signature(value)` holds — a new `crash_signatures` helper that
full-matches the existing `_CRASH_SIGNATURE` regex. Any other value reads as `None`.
`as_data()` adds the `observed_crash_signature` key.

**MCP read model.** `_boot_readiness_data` adds `detail` from `(signature, error_category,
declared expectation)`:

| Signature | Category | `detail` |
|---|---|---|
| `S` | any | ``crash signature `S` observed before the readiness marker`` |
| none | `boot_timeout` | `no crash signature was recorded before the readiness deadline; the guest did not become ready in the boot window` |
| none | `readiness_failure` | `no crash signature was recorded; the guest stopped or failed a run-readiness check before becoming ready` |
| none | other or `null` | `no crash signature was recorded for this boot failure` |

When the Run declared an expectation, every row gains ``; the declared `K` crash was not
recorded as matched``, where `K` is the preset `kind` (`panic`, `oops`, `hung_task`, `ubsan`) or,
for `console_crash`, the stored `pattern` literal; a declaration with neither a string `kind`
nor, for `console_crash`, a string `pattern` adds no clause. The clause states only what the
failed job records: it makes no claim that a console was captured or searched. The null cases
say "recorded", not "observed": a provider that does not scan (remote-libvirt) stores nothing
and must not read as a clean scan.

Persisting the signature (rather than re-scanning the console at read time) is the only option
that keeps `runs.get` from reading an object-store artifact per call; the scanner already ran,
and ADR-0594 set the precedent for carrying a closed scalar through `details`.

## Failure model

1. **Actors and deployments** — an agent calling `runs.get` over MCP; the worker running the
   local-libvirt boot handler; remote-libvirt boots through the same read path.
2. **Invariants and assets at stake** — the published `runs.get` `data.boot_readiness` shape
   (additive only; existing keys and values unchanged); ADR-0413's `expected_crash_matched`
   derivation; the no-leak rule for console text (only a closed-vocabulary literal leaves the
   provider).
3. **Accepted failure classes**
   - Remote-libvirt and window-continuity failures (`_ConsoleWindowFailure`) record no signature;
     they report `null` and a "not recorded" detail, which is true.
   - The external-boot session and the system-authority readiness probe compute the signature
     but consume only success/`ok`, so their failures record no signature and read as "not
     recorded".
   - When console capture fails, the declared clause still reads "not recorded as matched",
     which is what the job records.
   - A failed boot job written before this change has no `failure_detail_crash_signature`; it
     reads as `null`.
   - The signature reflects the pre-marker readiness scan, not the full captured console.
4. **Covered elsewhere** — matching the declared pattern stays in `boot_evidence`
   (ADR-0266/0383); secret redaction of `failure_context` stays in `jobs/worker.py`.

## Testing

- Readiness: `_scan_result` on a `CRASHED` console yields `crash_signature == "UBSAN:"`;
  `READY`/`PENDING` yield `None`; `_real_readiness` (the Run booter's probe) returns the
  signature; `classify_console` verdicts unchanged.
- Booter: an answered-failed readiness with a signature puts `crash_signature` in `details`; a
  timeout does not.
- Service: `failed_boot_attempt` reads a valid signature from `failure_context` and drops an
  unknown value.
- `runs.get`: declared `panic` + `UBSAN:` job → signature and the crash detail; declared `panic`
  + `boot_timeout` with no signature → `null` and the timeout detail, both ending in the declared
  `panic` clause; no expectation → no clause; the existing
  `expected_crash_observed` success path shows no `boot_readiness` (a `runs.get` test with that
  outcome beside a stale failed boot job), and the boot handler's `expected_crash` tests stay
  green.
