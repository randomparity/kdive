# Named crash-signature presets for expected_boot_failure (#865)

- Issue: #865
- ADR: [ADR-0266](../adr/0266-crash-signature-presets.md)
- Status: Accepted

## Problem

`expected_boot_failure` has exactly one `kind` (`console_crash`) and forces every caller to
hand-roll the crash-signature `pattern` — a case-sensitive literal substring with `|`
alternation (not a regex), matched line-by-line against the redacted boot-window console.
Callers defensively OR several terms to be sure of catching, say, an oops; a slightly-off
wording (a string the kernel changed between versions, or an arch-specific variant) silently
misses the crash and the Run is scored as an unexpected failure instead of the intended
reproduce. There are no shared presets — the term lists are reinvented at every call site.

## Requirements

1. `expected_boot_failure.kind` accepts three curated presets — `oops`, `panic`, `hung_task` —
   in addition to the existing `console_crash`. A preset expands to a canonical, multi-term
   literal pattern maintained in one place.
2. A preset kind takes **no** `pattern`: supplying both `kind: <preset>` and a `pattern` is a
   `configuration_error` (`reason=bad_expected_boot_failure`), the same failure the malformed
   `console_crash` already returns. The `console_crash` lane is unchanged: it still **requires**
   a custom `pattern`.
3. The validated/persisted doc keeps the preset name verbatim and carries the resolved canonical
   pattern, e.g. `{kind: "panic", pattern: "Kernel panic", description?}`. The record is
   self-describing and immutable: it states which signature the Run was matched against, even if
   the preset map is later refined.
4. Boot-time matching treats preset and custom kinds uniformly: it searches the redacted console
   for the doc's resolved `pattern`. A matched preset crash is the Run's success outcome exactly
   as a matched `console_crash` is today.
5. `runs.get` / `runs.create` echo the preset name as `data.expected_boot_failure` and the full
   `{kind, pattern}` as `data.expected_boot_failure_detail` (existing read path, no new field),
   so an agent can see the canonical terms a preset expanded to.
6. The `runs.create` tool description documents the three presets and the
   "preset OR custom pattern, not both" rule.

## Canonical preset → pattern mapping

Each preset is a `|`-OR of literal substrings (the existing `parse_literal_terms` lane: ≤16
terms, ≤256 chars, case-sensitive). Terms were chosen against current kernel sources and the
older EL kernels this project targets (RHEL 8 = 4.18 predates the v5.0 page-fault wording
change), so a preset matches across kernel versions and the common arches.

| preset | resolved `pattern` | rationale |
|--------|--------------------|-----------|
| `panic` | `Kernel panic` | `panic()` prints `Kernel panic - not syncing: …` (`kernel/panic.c`); the `Kernel panic` substring catches every panic wording. |
| `oops` | `Oops:\|BUG: unable to handle page fault for address\|BUG: kernel NULL pointer dereference\|BUG: unable to handle kernel\|kernel BUG at` | `__die("Oops", …)` header (`Oops: 0000 [#1]`, x86 + arm64 `Internal error: Oops:`); modern x86 page-fault/NULL-deref wording (v5.0+); the pre-v5.0 `BUG: unable to handle kernel …` form (EL8); `BUG()`/`BUG_ON` → `kernel BUG at file:line`. |
| `hung_task` | `INFO: task \|blocked for more than\|blocked in I/O wait\|hung_task` | khungtaskd prints `INFO: task <c>:<p> blocked for more than <n> seconds.` plus the `hung_task_timeout_secs` help line; `INFO: task ` is the stable prefix across the standard, mutex-blocker, and (newer) `blocked in I/O wait` variants. |

`console_crash` resolves to the caller's literal `pattern` unchanged.

## Composition: preset OR custom pattern, never both

A preset is self-contained. A caller who needs custom terms uses the `console_crash` lane with
their own `pattern` (which may copy a preset's terms and add to them). Merging a preset's terms
with a caller pattern was rejected (ADR): it makes the matched signature ambiguous and the
16-term/256-char budget hard to reason about, for no real gain over "fall back to
`console_crash`."

## Success criteria (falsifiable)

- `runs.create` with `expected_boot_failure={kind:"panic"}` persists
  `{kind:"panic", pattern:"Kernel panic"}`; a boot whose console contains
  `Kernel panic - not syncing: Attempted to kill init!` scores `expected_crash_observed`
  with that line as `matched_line`.
- `{kind:"oops"}` matches a console containing `BUG: unable to handle page fault for address:`
  (v5.0+) **and** one containing `BUG: unable to handle kernel paging request at` (EL8) **and**
  one containing `Oops: 0000 [#1] SMP`.
- `{kind:"hung_task"}` matches `INFO: task khungtaskd:42 blocked for more than 120 seconds.`
  and `INFO: task dd:456 blocked in I/O wait for more than 122 seconds.`.
- `{kind:"console_crash"}` with no `pattern` → `configuration_error`
  (`reason=bad_expected_boot_failure`), unchanged from today.
- `{kind:"panic", pattern:"foo"}` → `configuration_error` (`reason=bad_expected_boot_failure`).
- `{kind:"console_crash", pattern:"…"}` round-trips exactly as before (no preset expansion).
- Every preset's canonical pattern itself passes `parse_literal_terms` (≤16 terms, ≤256 chars,
  no empty term, no NUL) — guards against a malformed preset entry.

## Edges & failure modes

- An unknown/legacy `kind` not in `{console_crash, oops, panic, hung_task}` fails validation at
  create (`bad_expected_boot_failure`); a persisted row carrying an unrecognized kind fails the
  boot matcher closed to "no match" (no exception), the existing defensive behavior.
- A persisted preset doc missing its `pattern` (corruption) matches nothing: the matcher guards
  `isinstance(pattern, str)`, and an empty/whitespace pattern that slips past that is rejected one
  layer down by `parse_literal_terms` inside `search_text`, which the matcher catches and fails
  closed to `None`.
- Redaction and length-bounding are unchanged: matching runs over the already-redacted console
  and the matched line is clipped by `search_text` (ADR-0260).

## Out of scope

- New `kind` families beyond console-text matching (e.g. exit-code, kdump-based outcomes).
- Regex matching, per-term scoring, or merging presets with custom patterns.
- Schema/DB migration: `expected_boot_failure` is a JSON column validated at the app layer; the
  presets are new accepted values, not a column change. RBAC, config, and the run state machine
  are untouched.

## Affected code

- `src/kdive/domain/lifecycle/crash_signatures.py` — **new**: `CRASH_SIGNATURE_PRESETS` map and
  the `CONSOLE_CRASH_KINDS` set (single source of truth).
- `src/kdive/domain/lifecycle/records.py` — `ExpectedBootFailure.kind` Literal widened; a
  `model_validator(mode="before")` resolves a preset to its canonical pattern and rejects
  preset+pattern.
- `src/kdive/jobs/handlers/runs/boot_evidence.py` — `expected_crash_matched_line` guards on
  `kind in CONSOLE_CRASH_KINDS` instead of `== "console_crash"`.
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — `expected_boot_failure` field description.
- `docs/guide/reference/runs.md` — regenerated via `just docs`.
