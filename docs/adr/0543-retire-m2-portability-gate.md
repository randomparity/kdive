# 0543 — Retire the M2 portability diff gate; keep the capture-coverage drift guard

## Status

Accepted (2026-08-04)

Supersedes [ADR-0076](0076-remote-libvirt-provider-package.md) where it defined the
portability diff gate and its committed report. ADR-0076's package-boundary decision — an
independent `remote_libvirt` package with no shared `libvirt_common` layer — stands and is
not reopened here.

## Context

`docs/design/top-level-design.md` §Roadmap states the project's provider-seam bet as a
one-shot falsifiable hypothesis:

> Each milestone after M0 is intended to be "add a provider package + its provisioning
> profiles," with the core and tool surface unchanged … **This is a falsifiable hypothesis,
> not a guarantee**: the test is that adding the M2 remote provider touches zero lines in
> `core/*` and the MCP tool-surface modules, measured by diff scope.

ADR-0076 built `scripts/m2_portability_gate.py` as the instrument for that one test. It
measures cumulative touched lines over `pre-M2..HEAD` across
`domain/`/`db/`/`jobs/`/`reconciler/`/`services/`/`store/`/`security/`/`mcp/` and fails on
any file outside a named allowlist.

**The experiment reported, and the hypothesis held.** `AGENTS.md` records the verdict:
"The falsifiable design hypothesis held for remote-libvirt: adding that provider was mostly
a provider implementation plus `ProviderRuntime` wiring."

**The instrument stopped measuring that hypothesis three days after its baseline was cut.**
The `pre-M2` tag was cut 2026-06-09. The gate first reported violations on 2026-06-12 and
has reported them every day since, reaching 514 by 2026-08-04. The first 57 were M2.4
image-catalog work, artifact upload and read refactors, console hosting, and allocation
platform work — no remote-libvirt logic in any of them. The largest single entry, 390 lines
on `mcp/tools/ops/images/__init__.py`, is the split of a module the allowlist already named,
so it was allowlist path rot rather than a core touch at all (#1835 fixed that class; #1838
tracks a remaining variant).

**The flaw is structural, not a stale baseline.** The gate is a time-range measurement over
a trunk-based repository where every Milestone's sub-issues merge to `main` individually.
ADR-0076 saw half of this — it notes there is "no long-lived epic branch" and proposes
identifying the commit set "by milestone/label" — and then measured by time anyway. So
`pre-M2..HEAD` over core means "all core work since June", not "M2's core work".
Re-baselining per Milestone does not fix that: separating one Milestone's commits from
concurrent platform work needs scoping by label or pull request, which is a different
instrument with a different cost.

Nothing depends on the gate. `m2-gate` is in no workflow and is not one of the `ci`
recipe's members; it is reachable only by hand. Its one committed output,
`docs/<archive>/reports/m2-portability.md`, was last regenerated 2026-06-12 and still reads
"Verdict: gate passed — no core surface touched outside the ADR-0076 allowlist" over a
measurement that has contradicted it for eight weeks. It also describes a four-method
capture vocabulary, which ADR-0349 made five when it added `fadump`.

One piece of that machinery is worth more than the gate. The drift guard that lived in the
gate's test module imports the real `build_local_runtime` / `build_remote_runtime` builders
and fails when either provider advertises a capture-method set the pinned table does not
match. That guard is about provider capability truthfulness, not
about diff scope, and it is what epic #1814's exit criterion 8 and #1820 actually depend on.

## Decision

**Retire the gate.** Remove `scripts/m2_portability_gate.py`, the `m2-gate` and `m2-report`
recipes, and the committed `docs/<archive>/reports/m2-portability.md`. (The path is written
with the repository's angle-bracket placeholder idiom because the file no longer exists and
`just docs-paths` scans `docs/adr/`.)

The M2 verdict is recorded here and in `AGENTS.md` rather than in a generated file. A
committed report asserting a verdict that its own instrument contradicts is worse than no
report: it invites a reader to trust a measurement nobody has run since June.

**Keep the capture-coverage drift guard.** It survives as a test with its pinned
`CAPTURE_COVERAGE` table rehomed into the test module, whose only other consumer was the
deleted report renderer. Its contract is unchanged, and narrower than its name suggests:
changing an existing provider's advertised set reddens `just test`, because the guard
asserts two hardcoded keys against `build_local_runtime` and `build_remote_runtime`. It
enumerates nothing, so a newly registered kind with no row stays green and undetected.
Closing that is the registered-kinds completeness assertion #1820 owns; this decision
neither creates nor worsens that gap.

**Do not build a replacement.** Provider-specific logic reaching core is a design smell that
review catches by reading the change, not a quantity a line-count instrument measures. The
seam that made the hypothesis hold — typed `ProviderRuntime` ports (ADR-0063) behind the
per-kind resolver (ADR-0071) — is what keeps it holding, and adding a provider still starts
by satisfying those ports. A future Milestone that wants the measurement back should design
one scoped by label or pull request and accept the cost, under its own ADR.

## Consequences

- **The M2 portability claim becomes a record rather than a check.** Anyone asking whether
  the provider seam held reads this ADR or `AGENTS.md`; there is no command that recomputes
  it. That is the honest state — there has not been a trustworthy recomputation since June.
- **The 514-violation number disappears rather than being resolved.** It was never evidence
  of provider leakage, so nothing is being suppressed. What ends is a measurement of the
  wrong window.
- **#1820 shrinks.** It planned a BYO baseline tag alongside `BASELINE_TAG` plus a set of
  R9 allowlist entries; with the gate gone it needs only the `byo-host` capture-coverage row
  against the surviving drift guard. Epic #1814's criterion 8 loses its automated check and
  is satisfied by review plus that guard.
- **#1838 is moot.** It tracks a dead-entry shape in an allowlist that no longer exists.
- **A later provider could leak into core without an automated signal.** This is the real
  cost, accepted deliberately: the gate has not provided that signal since 2026-06-12 in any
  case, so nothing operative is lost — only the appearance of one.
- **Three recent records keep citing the deleted script**, and this decision is what makes
  those citations historical: [ADR-0538](0538-byo-host-provider-package.md) §BYO portability,
  [ADR-0540](0540-adopt-only-provisioning.md) and
  [ADR-0542](0542-kgdb-over-leased-serial-channel.md) each reason from `CORE_PREFIXES` or
  `CAPTURE_COVERAGE` living in `scripts/m2_portability_gate.py`, and ADR-0538 assigns entry-17
  work on that basis. They are not amended: three amendments would add more text than the
  risk removes, and a reader arriving from any of them lands here.
- **`docs/design/m2-remote-libvirt.md` gets a pointer, not a rewrite.** M2's design doc
  describes the gate as a per-PR CI check in six places. It is the record of a completed
  Milestone, so this change adds one note naming ADR-0543 rather than restating the document
  around an instrument that no longer exists.

## Considered & rejected

- **Re-baseline per Milestone** (cut `pre-M3`, `pre-M4`, parameterize `BASELINE_TAG`).
  Rejected because it fixes the window without fixing the measurement: a time range on a
  trunk-based repository still cannot separate a Milestone's provider work from the platform
  work landing beside it. It would produce a smaller wrong number on the same footing, and
  it is the option that most looks like a fix.
- **Wire the existing gate into CI.** Rejected: it is red by 514 violations, so wiring it
  blocks every pull request immediately. Making it green first means either re-baselining
  (above) or allowlisting hundreds of files, which turns the allowlist into a record of
  everything rather than of deliberate decisions.
- **Keep the gate, delete only the false report.** Rejected: the report is the gate's only
  output. A gate nobody runs and whose result nobody records is not a check, and leaving the
  script implies an enforcement that does not exist — the misreading this ADR removes.
- **Scope a replacement by label or pull request now.** Rejected as unrequested scope. The
  hypothesis it would test has already been answered for the providers built so far, and a
  Milestone that wants the measurement can justify the instrument then, with the cost in
  view.
- **Keep the whole test module and let it exercise a deleted script.** Not viable: most of
  its assertions target the gate's numstat parsing, allowlist, and report rendering, all of
  which go. Only the capture-coverage guard has a subject that outlives the gate.
