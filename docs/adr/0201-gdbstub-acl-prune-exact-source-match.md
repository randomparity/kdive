# ADR 0201 — gdbstub_acl ufw prune excludes the current worker by exact source match

- **Status:** Accepted
- **Date:** 2026-06-21
- **Deciders:** KDIVE maintainers

## Context

`deploy/ansible/roles/gdbstub_acl/tasks/main.yml` enforces the worker-CIDR ACL on the
raw-TCP gdbstub tier. On the ufw (Debian) path this ACL is the **only** authorization for
those ports — the gdbstub range carries no TLS, so a wrong rule means unauthenticated
full-VM memory access.

The role's **prune** task (ADR-0200 describes its regression harness) deletes `ALLOW IN`
rules on the protected ports whose source is not the current `worker_cidr`, cleaning up stale
allows after a `worker_cidr` change. It selects line numbers from `ufw status numbered` and
excludes the current source with a fixed-**substring** filter:

```
| grep -vF "{{ worker_cidr }}"
```

`grep -vF` keeps a line only if the whole line does **not** contain the worker CIDR as a
substring. The security question is narrower: is the rule's *source field* equal to the
worker CIDR? A stale allow whose source string merely *contains* the worker CIDR as a
substring answers "yes" to the substring test and is wrongly treated as current, so it
**survives** the prune (#648). Concretely, with `worker_cidr=10.0.0.0/24`, a stale allow from
the real, routable range `110.0.0.0/24` contains `10.0.0.0/24` and is not pruned — leaving an
over-permission on the only auth tier for those ports, silently (the prune reports success).

This is the "under-match → over-permission persists" failure #616 named. ADR-0200's harness
**pinned** it as the `substring_collision` case (expected deletions: none) rather than fixing
it, because an address-aware change to this audited pipeline warranted its own review and live
re-verification. This ADR is that fix.

## Decision

Replace the substring exclusion with an **exact equality** comparison on the ufw `From`
(source) column. After the existing port/action grep has reduced the stream to `ALLOW IN`
rows on exactly the TLS port or gdbstub range, select a row for deletion iff its source field
is **not** string-equal to `worker_cidr`:

```sh
| awk -v cidr="{{ worker_cidr }}" \
    '{ for (i = 1; i <= NF; i++) if ($i == "IN") { if ($(i + 1) != "" && $(i + 1) != cidr) print; break } }'
```

The source is read as **the field immediately after the `IN` direction token**, not as the
last field on the line (`$NF`) and not by a substring test. This reads the actual `From`
column regardless of a trailing ufw `# comment` or an IPv6 `(v6)` marker, both of which the
existing grep already lets through and which `$NF` would misread (deleting the current allow
when it carries a comment). `awk` exits `0` with no output, so — unlike the `grep` it replaces
— it needs no `|| true` to stay `set -euo pipefail`-safe; the port grep keeps its `|| true`.

The `$(i + 1) != ""` guard makes the matcher **fail toward not deleting**: a malformed row
with no source after `IN` is skipped rather than selected for deletion by line number. Today
no such row can reach `awk` — the upstream port grep anchors a trailing space after `IN `, so
a sourceless `ALLOW IN` line never matches — but the guard removes that coupling, so a future
grep edit cannot turn an unparseable row into a blind delete (an over-prune that would drop
the worker).

The comparison stays a string equality, not a subnet/`ipaddress` comparison: the role writes
exactly one canonical `worker_cidr` allow per port, so the current rule's source is
byte-identical to the templated value. Any other source string — a different CIDR, a
different mask, a substring collision, `Anywhere`, or a `(v6)` source — is a non-current
source and is correctly pruned.

**Load-bearing assumption — canonical `worker_cidr`.** Equality is correct only if ufw renders
the `From` column byte-identically to the templated `worker_cidr` string. The role applies
the allow with `from_ip: "{{ worker_cidr }}"` and does not canonicalize, so an operator who
supplies a non-canonical form (a host-bit address like `10.0.0.5/24`, a netmask form, or an
IPv6 source ufw re-renders) gets a stored source that differs from the template — and exact
equality then fails to match the *current* allow and prunes it, dropping the worker. The
substring filter tolerated trailing differences and partly masked this; exact equality does
not, so it is a sharper edge. The mitigation is operational, not code: supply `worker_cidr` as
the canonical network CIDR (as ufw renders it), and the live re-verification below asserts the
current allow **survives** the prune, not only that the stale collision is gone. A
subnet-aware comparison would paper over a non-canonical `worker_cidr` but at the cost of
under-pruning a stale sub/supernet (see Considered & rejected), so it is not the fix here.

When fixed, the `substring_collision` harness case flips from "no deletion" to deleting the
stale lines, and two regression fixtures lock the matcher: a prefix-collision source
(`10.0.0.0/2`, a substring *of* the worker CIDR) is pruned, and a row carrying a trailing
ufw comment is matched by its source column (current survives, stale pruned) rather than by
its last token.

## Consequences

- A stale allow whose source string collides with `worker_cidr` as a substring is now pruned,
  closing the silent over-permission on the gdbstub tier's only auth path.
- The matcher reads the source column positionally, so a future ufw output that appends a
  `# comment` or `(v6)` column to a protected-port `ALLOW IN` row does not cause the current
  allow to be misread and deleted. The harness pins this with the `comment_column` fixture.
- This changes the audited security pipeline. The hermetic harness covers the parse/selection
  logic but not live netfilter application, so the change must be re-verified on real hardware
  before a host is registered, following the off-CIDR ACL refusal check in
  `deploy/ansible/README.md`: change `worker_cidr` on a host that already carries a stale
  substring-colliding allow (e.g. move from `10.0.0.0/24` while a `110.0.0.0/24` allow
  lingers), re-run the role, then assert with `ufw status numbered` both that the stale allow
  is **gone** and that the new `worker_cidr` allow is **present** (the latter catches a
  non-canonical-CIDR over-prune).
- Behavior is unchanged for every previously-correct case: the current allow, the SSH allow,
  the deny rows, and non-protected-port allows are still untouched; broader-mask and
  distinct-CIDR stale allows are still pruned highest-first.

## Considered & rejected

- **Keep `grep -vF` (substring exclusion).** The status quo and the bug itself: a stale
  source containing the worker CIDR as a substring survives. Rejected — it is the defect.
- **`awk '$NF != cidr'` (compare the last field).** The literal suggestion in #648 and the
  smallest diff. Rejected: `$NF` is the last whitespace token, which a trailing ufw
  `# comment` (or a `(v6)` suffix) shifts off the source — so a *current* allow carrying a
  comment would compare unequal and be deleted, dropping the worker mid-run. Reading the
  field after the `IN` token is the same size and reads the real source column.
- **Subnet-aware comparison (`python` / `ipaddress`, "is this source within/equal to the
  worker network").** Over-broad: the role grants exactly one canonical `worker_cidr` string,
  so equality is sufficient and a subnet test would risk *under*-pruning a legitimately stale
  sub/supernet that an operator intends to remove. It also adds an interpreter dependency to a
  pure text-selection step. Rejected as unnecessary scope on this audited auth path.
- **Anchored `grep -E` on the source column.** Equivalent in effect but harder to read and to
  keep correct across the optional `(v6)`/comment columns than an explicit "field after `IN`"
  in `awk`. Rejected for clarity.
- **Extract the pipeline into a `files/` script to unit-test the matcher directly.** Rejected
  for the same reason ADR-0200 rejected it: it changes the audited inline role implementation
  rather than only the one selection filter; the harness already drives the real task.
