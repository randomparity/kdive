# Pytest terminal-summary proof gate

## Problem

Five live-tier gates infer that a tier ran from captured pytest output. Four search the entire
stream for a positive passed count and one remote recipe only rejects exit 5. With pytest's
`-ra` enabled, a skip reason can contain `1 passed`; an all-skip tier can therefore appear
proved. The proof predicate is duplicated between `justfile` and `live.yml`.

## Scope

Add one executable checker under `scripts/` that reads a captured stream, considers only its final
non-empty line, removes terminal ANSI escapes, recognizes pytest's count-and-duration
terminal-summary shape, and succeeds only when that line contains a nonzero `passed` count. Route
the native, TCG, and remote recipes plus the hosted TCG and native workflow spines through it.
The remote recipe gains the same `mktemp`, `tee`, cleanup, and preserved-`rc` capture shape as its
siblings while retaining its existing exit-5 message. Other recipes retain their commands, marker
expressions, exit handling, and no-proof text.

### Failure model

- Actors and deployments: local operators running the three recipes; GitHub-hosted TCG and
  self-hosted native workflow jobs.
- Invariants and assets at stake: a green live tier represents at least one executed proof; pytest
  exit status remains the command's verdict after proof has been established.
- Accepted failure classes: malformed or non-pytest final output fails closed; output before the
  final non-empty line is deliberately ignored because it is not pytest's terminal summary.
- Covered elsewhere: marker selection is owned by live-tier maintainers; native environment
  contract is #2518; allocation sizing is #2560; agent-smoke exit-5 policy is #2540 / PR #2580.

## Success

For the five named gates, all-skip, all-fail with zero passed, and zero-collection streams fail
the checker. A terminal pytest summary with one or more passed tests succeeds, including
passed/skipped and failed/passed mixtures; the owning command may then propagate pytest's own exit
status. Controlled-fault tests prove a `-ra` skip reason containing `1 passed` does not satisfy
the gate and prove a genuine terminal pass does.

## Validation

- Focused tests: add checker tests for false-positive skip text, zero-pass summaries, mixed
  summaries, ANSI color, and real pytest output with `-p no:cacheprovider` under varied widths.
- Focused tests: drive all three recipes with the existing stub `uv`, including an all-skip stream
  whose reason contains `1 passed`; execute extracted workflow proof blocks with both deceptive
  and genuine streams so the two spines are behavioral rather than text-only coverage.
- Integration: run `just lint`, `just type`, and the final `just ci` gate. Run applicable live
  verification on the provided development/test systems without publishing their identifiers.
