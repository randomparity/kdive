# Markdown anchor check for `docs-links` (#3048)

Issue: [#3048](https://github.com/randomparity/kdive/issues/3048). Follow-up from #3022.

## Problem

`scripts/check-doc-links.sh` (the `just docs-links` gate, part of `just ci` and a separate CI
step) checks only that a relative link's file exists. It strips `#fragment` before the
existence test (`:80`) and skips pure `#fragment` links (`:75-77`). A link to a heading that
does not exist, or that a later rename broke, passes.

Audit on `main` at `31a524bd0`, using the slug rules below over the files the gate scans:
239 fragment links into tracked `.md` targets (18 same-file), **0 broken**. The gate starts
green with no link fixes.

## Design

The shell script keeps enumeration, exclusions, link extraction and the existence check. A new
stdlib-only helper, `scripts/check_doc_anchors.py`, owns heading anchors and fragment matching.

**Which links are anchor-checked.** After the existing `://` and `mailto:` skips and title
strip, a link with a non-empty fragment is checked when:

- it is a pure fragment (`#x`): the target is the source file itself; or
- its path part resolves (from the source's directory) to an existing regular file whose name
  ends in `.md`.

A fragment on any other target (a directory, a `.py` file, a missing file) is not
anchor-checked; a missing file is still reported by the existing `broken link:` line.

**Hand-off.** The script collects one `source`, `target path`, `link` triple per checked link
in a bash array (bash 3.2 compatible, no `mapfile`) and, when the array is non-empty, pipes it
NUL-separated to `python3 <script dir>/check_doc_anchors.py` once. NUL separation keeps source
names containing tabs, quotes or backslashes literal, as the existing tests require.

**Anchor set of a target file** (read as UTF-8):

- Fenced blocks are skipped: an opening fence is 0-3 spaces then three or more backticks or
  tildes; it closes on a line of 0-3 spaces and at least as many of the same character.
- A leading YAML front-matter block (`---` on line 1 through the next `---`) is skipped.
- ATX headings: 0-3 spaces, 1-6 `#`, then a space/tab or end of line; a closing `#` run
  preceded by whitespace is dropped.
- Heading text is reduced to rendered text: inline links and images become their link text;
  outside code spans, HTML tags are removed and HTML entities decoded; code-span contents
  stay literal.
- Slug, following GitHub's `github-slugger`: lowercase; keep characters whose Unicode category
  is a letter (`L*`), mark (`M*`), decimal or letter number (`Nd`, `Nl`) or connector
  punctuation (`Pc`, which holds `_`), plus space and `-`; drop the rest (`No` such as `①` too);
  each space becomes `-`. Duplicates take `-1`, `-2`, ... using the slugger's occurrence loop.
- Explicit anchors: `id` or `name` attributes of `<a ...>` tags, outside fences.

**Matching.** The fragment is percent-decoded, then compared case-insensitively with the anchor
set (GitHub lowercases heading slugs; anchors are compared lowercased too).

**Output and exit.** The helper prints `broken anchor: <source> -> <link>` to stderr for each
miss and exits 3, or exits 0. Any other exit (missing `python3`, an unreadable target, bad
input, or 1 from an uncaught Python exception) makes the script print
`cannot check markdown anchors; check python3 diagnostics above` and exit 1, so a failed anchor stage cannot certify the links. The script's final status and
messages are otherwise unchanged; the header comment drops the anchor exemption.

**`check-served-doc-links.sh` does not share the check** and is not changed. It flags every
relative link from one served doc to another, with or without a fragment, so a fragment never
decides its verdict; a `resource://` link it asks for has no file-relative anchor to verify.
Served sources live under `docs/` and are already scanned by `docs-links`, so their anchors are
covered there.

## Alternatives

- **Slug in awk inside the shell script.** judgment: mawk and BWK awk have no Unicode
  categories, and headings here carry `—`, `§` and `→`, which GitHub drops; a byte-range
  approximation would produce false `just ci` failures.
- **Rewrite the whole gate in Python.** judgment: discards the fail-closed enumeration and the
  tests that pin it, which the issue asks to keep.
- **Exact-case fragment match.** judgment: GitHub's slugs are lowercase and every fragment in
  the tree is lowercase; case-insensitive matching avoids a false failure for no lost coverage
  of renamed headings.

## Failure model

1. Actors and deployments: a contributor running `just docs-links` / `just ci` locally
   (Linux, macOS); the CI `Doc links resolve` step on GitHub-hosted Ubuntu.
2. Invariants at stake: `just ci` and CI stay green on a correct tree (no false failures);
   a tool failure in the anchor stage never prints `markdown links resolve`.
3. Accepted failure classes:
   - Setext headings (`===`/`---` underlines) are not anchors; none exist in the scanned
     tree, and a link to one fails loudly naming the link (fix: use an ATX heading).
   - Underscore emphasis in a heading (`_x_`) keeps its underscores in the slug; a link to
     such a heading fails loudly. None exist today.
   - Headings inside raw HTML blocks or indented code blocks are treated as text lines by the
     rules above; bounded to extra or missing anchors on such pages.
   - Reference links (`[t][ref]`) and autolinks in a heading are not rendered, and Python's
     Unicode database is newer than github-slugger's; a link to an affected heading fails
     loudly. None exist in the scanned tree's links today.
   - Link extraction keeps its column-0 backtick fence rule, while anchor collection also
     honours indented and tilde fences; a link inside an indented or tilde fence is still
     checked, as its existence already is.
4. Covered elsewhere: external URLs (operator exclusion); served-doc `resource://` routing
   (`check-served-doc-links.sh`); excluded trees (`docs/archive/**` and the rest) keep their
   exemption as link sources.

## Success

- A `file.md#missing` link and a `#missing` same-file link each make `just docs-links` exit 1
  with `broken anchor: <source> -> <link>` on stderr.
- Links to existing headings (including duplicates, inline code, punctuation and `—`), to
  `<a id>`/`<a name>` anchors, and fragments on non-`.md` targets pass.
- A failing, crashing or missing `python3` makes the gate exit non-zero without `markdown links resolve`.
- `just docs-links` passes on the tree, this design set included. Every existing test in
  `tests/scripts/test_check_doc_links.py` still passes, except
  `test_external_and_anchor_only_links_ignored` and the `#here` case of `test_no_matches_pass`,
  whose pure-anchor links now need a matching heading.
