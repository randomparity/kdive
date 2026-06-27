# ADR-0266: named crash-signature presets for expected_boot_failure (#865)

- Status: Accepted
- Date: 2026-06-27

## Context

`expected_boot_failure` (ADR-0064) has exactly one `kind`, `console_crash`, whose `pattern` is a
case-sensitive literal substring with `|` alternation (not a regex), matched line-by-line
against the redacted boot-window console (`security/artifacts/artifact_search.py`;
worker-side use in `jobs/handlers/runs/boot_evidence.py`). Every caller hand-rolls the pattern
and defensively ORs several terms to be sure of catching, e.g., an oops. A slightly-off
wording — a string the kernel changed between versions (the x86 page-fault message changed at
v5.0), or an arch-specific variant — silently misses the crash, so a Run that *did* reproduce
the intended crash is scored as an unexpected failure. There are no shared presets; the term
lists are reinvented per call site. Surfaced by `BLACK_BOX_REVIEW.md` §7 (🟢).

## Decision

Add three curated named presets to `expected_boot_failure.kind` — `oops`, `panic`,
`hung_task` — alongside the existing `console_crash`. A preset expands to a canonical multi-term
literal pattern; `console_crash` keeps the custom-pattern lane.

- **Single source of truth.** `domain/lifecycle/crash_signatures.py` holds
  `CRASH_SIGNATURE_PRESETS: dict[str, str]` (preset → canonical `|`-pattern) and
  `CONSOLE_CRASH_KINDS = {"console_crash"} | presets`. The canonical patterns:
  - `panic` → `Kernel panic`
  - `oops` → `Oops:|BUG: unable to handle page fault for address|BUG: kernel NULL pointer
    dereference|BUG: unable to handle kernel|kernel BUG at`
  - `hung_task` → `INFO: task |blocked for more than|blocked in I/O wait|hung_task`

  Terms were chosen against current kernel sources (`kernel/panic.c`, `kernel/hung_task.c`,
  `arch/x86/mm/fault.c`) and the older EL kernels this project targets: EL8's 4.18 predates the
  v5.0 page-fault wording, so the pre- and post-v5.0 oops strings are both included, and the
  newer `blocked in I/O wait` hung-task variant is covered alongside the classic line.

- **Expand on validate; keep the preset name.** `ExpectedBootFailure.kind` widens to
  `Literal["console_crash", "oops", "panic", "hung_task"]`. A `model_validator(mode="before")`
  resolves a preset to its canonical pattern and **rejects** a preset that also carries a custom
  `pattern`. The persisted JSON keeps the preset name and the resolved pattern, e.g.
  `{kind: "panic", pattern: "Kernel panic"}`. The existing `pattern` field-validator
  (`parse_literal_terms` bounds) then runs over the resolved pattern, so a malformed preset entry
  fails its own validation. `console_crash` still requires a caller `pattern`.

- **Uniform matching.** `expected_crash_matched_line` (`boot_evidence.py`) guards on
  `kind in CONSOLE_CRASH_KINDS` instead of `== "console_crash"` and searches the doc's resolved
  `pattern`. Preset and custom expectations match identically; a matched preset crash is the
  Run's success outcome exactly as a matched `console_crash` is. `runs.get`/`runs.create` already
  echo `data.expected_boot_failure` (the kind, now possibly a preset name) and
  `data.expected_boot_failure_detail` (the full `{kind, pattern}`), so the agent sees the
  canonical terms with no new read field.

No DB migration (`expected_boot_failure` is a JSON column; presets are new accepted values), no
RBAC, config, or state-machine change.

## Consequences

- A caller declares `{kind: "oops"|"panic"|"hung_task"}` and gets a maintained, version- and
  arch-robust signature instead of reinventing the term list and risking a silent miss.
- The persisted record names the preset *and* the resolved pattern, so a Run immutably records
  which signature it was matched against; refining the preset map later does not rewrite old runs.
- Matching, redaction, and the read envelope are unchanged in shape; only the matcher's kind
  guard widens and the validator resolves presets. The one boot-matcher call site and its unit
  tests move from `== "console_crash"` to set membership.
- The custom-pattern escape hatch (`console_crash`) is retained for crashes outside the three
  presets; a caller needing extra terms uses it (and may copy a preset's terms).

## Considered & rejected

- **Normalize a preset to `kind: "console_crash"` (discard the preset name) so the matcher needs
  no change.** Loses the preset name from the record and the `runs.get` echo, so an agent that set
  `kind: "panic"` would read back `console_crash`. Keeping the name (one-line matcher widening, a
  set the matcher already needs) is more honest and is the value the issue asks for.
- **Resolve the preset → pattern at match time, persisting only `{kind: "panic"}`.** Makes the
  record non-self-describing and ties old runs to the current preset map; the agent could not see
  the terms in `expected_boot_failure_detail`. Expanding at validate keeps the run immutable and
  transparent.
- **Merge a preset's terms with a caller-supplied `pattern` (preset + custom OR-union).** Makes
  the matched signature ambiguous and the 16-term/256-char budget hard to reason about, for no
  gain over falling back to `console_crash` with the union written out. Rejected; preset and
  `pattern` are mutually exclusive.
- **Regex or per-term scoring.** Out of scope and a larger attack/parse surface; the literal
  `|`-substring lane (ADR-0064/0225 bounds) is sufficient for canonical kernel strings.
- **A DB column / enum for the kind.** The value lives in an existing JSON column validated at the
  app layer; an enum column would be a needless migration and a second source of truth for the
  preset set.
