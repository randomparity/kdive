---
title: ruff format rewrites Python inside Markdown fences, and the pre-commit hook does not see it
date: 2026-09-04
tags: [ruff, markdown, pre-commit, ci-gap, pep-758, false-green]
components: [pyproject.toml, .pre-commit-config.yaml, justfile, docs/workflow/specs, docs/workflow/plans]
---

## Problem

A feature branch had `just lint` failing for hours across many commits, and nobody noticed. The
failure was introduced by a **documentation-only commit** and had nothing to do with the source
changes under review.

Two consequences, both worse than the failure itself:

- A `just ci` baseline was recorded as green against a commit it did not describe. The branch had
  been red since a much earlier commit; the recorded green was false when it was written.
- Later commits inherited the red without any signal, because the author's local
  `git commit` reported every hook passing.

The failing command is:

```
uv run ruff format --check .
```

and the offending file is a `.md` spec or plan containing a fenced Python block.

## Root cause

Two facts combine, and neither is obvious on its own.

**1. `ruff format` formats Python code inside Markdown fences.** This is not incidental — it edits
the code in the fence. Reproduced directly:

```
$ printf '# t\n\n```python\nx = [1,  2,3]\n```\n' > a.md
$ ruff format --check a.md
unformatted: File would be reformatted
 --> a.md:4:9
  |
3 | ```python
  - x = [1,  2,3]
4 + x = [1, 2, 3]
5 | ```
1 file would be reformatted
```

`pyproject.toml:61-66` sets `extend-exclude = ["docs/adr"]` — **only ADRs are exempt.** Everything
under `docs/workflow/specs/` and `docs/workflow/plans/` is formatted, and those are exactly the
documents that carry worked code examples.

**2. The pre-commit hook cannot catch it.** `.pre-commit-config.yaml:7` declares a bare
`ruff-format` hook with no `types_or` override, so it uses the upstream hook's default file
selection — Python only. A Markdown-only commit therefore prints:

```
ruff format..........................................(no files to check)Skipped
```

So the local gate is silent on precisely the file class that `just lint` and CI will fail on.
The author sees green, CI sees red, and the gap persists until someone runs `just lint` bare.

**The aggravating case: `target-version = "py314"` and PEP 758.** Ruff rewrites parenthesised
multi-exception handlers to the new unparenthesised form, *inside the fence*:

```
$ ruff format --target-version py314 b.md   # b.md contains except (ValueError, TypeError):
$ cat b.md
except ValueError, TypeError:
```

That silently changes the text of a documented example. It also breaks any tooling that anchors on
the old string — fault-injection harnesses that match on a code snippet from a spec will report
`FAULT SETUP FAILED` or, worse, silently miss.

## Solution

Run the gate bare after any documentation change that contains a Python fence:

```
just lint
```

No `| tail`, no `>/dev/null`. Check the exit code, not the output.

**Confirm the file was actually examined, not skipped.** `ruff format --check` reports a count, and
the count is the evidence:

```
uv run ruff format --check docs/workflow/plans/<file>.md
1 file already formatted        # examined and clean
0 files ...                     # NOT examined — the path or filter is wrong
```

"1 file", not "0 files", is what proves coverage. A `--check` that passes because it inspected
nothing is a false green of exactly the kind this whole trap produces.

To fix an offending file, run the formatter rather than hand-editing the fence:

```
uv run ruff format docs/workflow/plans/<file>.md
```

## Prevention

- **Treat a docs-only commit as lint-affecting.** The reflex "it's only Markdown, the hooks passed"
  is precisely wrong in this repo.
- **Never inherit a green.** A `just ci` result recorded by a previous session, or against an
  earlier commit, is a claim rather than evidence. Re-run it bare at the HEAD you intend to hand
  off, and report that exit code.
- **When writing a Python fence in a spec or plan, format it before committing** — the formatter is
  going to rewrite it anyway, and letting it do so at authoring time keeps the committed text equal
  to the formatted text.
- Closing the gap properly would mean giving the pre-commit `ruff-format` hook a `types_or` that
  includes Markdown, so the local gate matches `just lint`. That has not been done; until it is,
  the bare `just lint` run is the only thing standing between a docs commit and a red branch.
