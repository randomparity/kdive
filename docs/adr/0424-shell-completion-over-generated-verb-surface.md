# ADR 0424 — Shell completion over the generated `kdivectl` verb surface (#1451)

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** kdive maintainers
- **Issue:** [#1451](https://github.com/randomparity/kdive/issues/1451) (epic #1442).
- **Builds on (does not supersede):** [ADR-0089](0089-operator-cli-mcp-client.md) (the CLI is a
  pure MCP client), [ADR-0421](0421-schema-generated-kdivectl-verbs.md) (the merged
  curated + generated verb tree this completes over), [ADR-0423](0423-generic-generated-verb-dispatch.md)
  (the `--<param>-json` and `--yes` flags on generated verbs that completion now surfaces).

## Context

`#1448` merged the curated and schema-generated verbs into one parser tree, so
`kdive.cli.__main__.build_parser` is the single constructor of the whole `kdivectl` surface:
the top-level flags, `login`, `tool call`, `doctor`, and — via
`commands.registry.add_subparsers` — every group, verb, and per-verb flag (including #1449's
`--<param>-json` escapes and #1450's `--yes` on destructive generated verbs). There is exactly
one tree a completion generator has to walk.

Two properties constrain the design. Completion runs once per keystroke, so it cannot afford a
`list_tools()` roundtrip; and it must work with **no bearer token** — completing a command line
is not an authenticated operation. The parser tree is fully static and constructed offline
(argparse construction touches no `Session`, no network, no token), which is the payoff of
generating verb shapes at build time (ADR-0421): completing 141 verbs and their flags is a
static walk of an in-process object, not a server query.

## Decision

Add a `kdivectl completion {bash,zsh}` subcommand that walks `build_parser()` once and prints a
**self-contained** shell completion script to stdout. No new dependency (no `argcomplete` /
`shtab`), no server call, no token.

1. **Static walk (`cli/completion.py`).** `build_completion_tree(parser)` recurses the argparse
   tree, recording for each subcommand path the tokens completable *at* that path: a group path
   yields its child verb names plus the inherited `--json`; a leaf verb path yields its `--`
   flags; the root yields the top-level subcommands (`login`, `tool`, `doctor`, `completion`,
   every group) plus `--json`. Only long (`--`) options are emitted. The walk deduplicates
   subparser aliases that point at the same parser object.
2. **Self-contained emission.** The tree is baked into the emitted script as a shell associative
   array keyed by the space-joined subcommand path (`""` for the root). A small fixed walker in
   the script reconstructs the current path from the words before the cursor — skipping any
   token that begins with `-` (flags) and any token that is not a known path key (positional
   argument values, which have no offline completion) — and completes against that path's token
   list. bash uses `complete -F` + `compgen`; zsh uses a `#compdef` function + `compadd`.
3. **Offline, unauthenticated.** The `completion` subcommand is dispatched before any handler
   that constructs a `Session`, so `kdivectl completion bash` runs with no `KDIVE_TOKEN` and no
   reachable server. Regenerate on demand (`kdivectl completion bash > …`) after upgrading;
   nothing is committed, so there is no generated-script drift gate to maintain.

## Consequences

- Tab-completion resolves groups, verbs, and per-verb flags entirely offline. Because the tree
  is walked from `build_parser()` — the same constructor dispatch uses — the completion surface
  cannot drift from the real parser: a new tool adds a generated verb, and the next
  `kdivectl completion bash` picks it up automatically.
- The emitted script embeds the tree, so completion at keystroke time is pure shell (no Python
  process, no import cost) once installed. The cost moves to install/regeneration time.
- Positional argument *values* (a `resource_id`, a tool `name`) are not completed — they are
  runtime/tenant data with no offline source. The walker degrades gracefully: an unrecognized
  positional leaves the path unchanged, so the verb's flags still complete after it.
- No schema change, no migration; CLI only.

## Alternatives considered

- **`argcomplete` / `shtab` dependency.** Rejected: each adds a runtime dependency (attack
  surface, maintenance) to produce a completion the repo can already derive from `build_parser`
  with a stdlib-only walk. Repo philosophy prefers no new dependency absent a strong reason, and
  a static walk of a committed-shape parser is exactly the case where there is none.
- **A tiny shell script that calls back into `kdivectl` per keystroke to resolve candidates.**
  Rejected: it keeps the tree in Python (no drift) but pays a Python import on every Tab, which
  is sluggish for an interactive completer and buys nothing a one-time static walk does not —
  the parser shape is static, so there is no live state to re-query.
- **Commit generated `bash`/`zsh` scripts with a CI drift guard.** Rejected as premature: it
  adds a guard and a committed artifact for a script the user can regenerate in one command. The
  on-demand emitter is the source of truth; a stale hand-copied script is the user's to refresh,
  not CI's to police.
