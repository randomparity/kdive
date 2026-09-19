# `--wipe` reap privilege follows the endpoint (#2516)

**Goal.** Make `stack-down.sh --wipe` reap at the privilege that owns the endpoint it was
published, instead of escalating to root against an operator-owned session daemon.

**Architecture.** `scripts/live-stack/stack-down.sh` gains one integer, `reap_as_root`,
classified from `KDIVE_LIBVIRT_URI` inside the `--wipe` branch, and one wrapper, `reap_run`, that
the enumeration, `destroy`, `undefine`, and overlay `rm` all route through — so the list grading
the reap and the calls performing it cannot carry different identities.

**Tech stack.** Bash (the script uses nothing newer than bash 4.0's `mapfile`); pytest arms in
`tests/scripts/test_live_stack_scripts.py`.

Expected implementation size: 60–90 changed lines (S) — from the file map below: one classifier,
one wrapper, four call sites, two pytest arms plus a one-parameter helper change, one README
paragraph.

**Overtaken in review — read the spec and ADR-0662 instead.** Three remedies were added to this
change after the plan was written: an overlay-directory writability precondition, de-escalating the
up-front gate's liveness probe, and stripping the fragment before classifying the endpoint. The
plan has not been rewritten to track them. Plans are transient here and specs and ADRs are the
durable record, so `docs/workflow/specs/2026-09-15-wipe-reap-privilege-2516-design.md` and
`docs/adr/0662-the-wipe-reap-privilege-follows-the-endpoint.md` are what describe what this change
does and why. The steps, file map, acceptance criteria and size estimate below all predate those
remedies and none of them is authoritative.

## Global Constraints

- The decision is ADR-0662: path component `/session` (query stripped) → invoking account, else
  `sudo`. Not an exact-URI allowlist — `resolve_libvirt_uri` honours a caller-supplied
  `KDIVE_LIBVIRT_URI` without consulting `LIBVIRT_SOCKET_URIS`.
- Do not change the reap's exit-code or reporting behaviour (#2515, merged): `destroy` keeps its
  `|| true`, both halves stay graded on a re-read end state, every existing `test_wipe_*` arm
  passes untouched.
- `KDIVE_LIBVIRT_URI` may be **unset** (the `LIBVIRT_OPTIONAL` degraded state), so the classifier
  lives inside the `--wipe` branch after `require_libvirt_uri` succeeds. A top-level `case` would
  kill a plain teardown on a broken contract under `set -u`.
- Leave `reap_as_root` unset elsewhere on purpose: `((reap_as_root))` under `set -u` aborts with
  `reap_as_root: unbound variable`, so a future call site outside the gate fails loudly.
- Shell lines ≤ 100 chars; `just lint-shell` green. No new dependency and no new env var — a
  `KDIVE_`-prefixed shell name under `scripts/` would fail `env-docs-check`, so both new names
  are unprefixed, matching `libvirt-uri.sh`'s own internals.

## File map

| File | Now | After |
| --- | --- | --- |
| `scripts/live-stack/stack-down.sh` | reap escalates unconditionally, enumeration bare | one URI-derived privilege for the whole reap |
| `tests/scripts/test_live_stack_scripts.py` | reap arms cover reporting only | one arm per branch; `_wipe_reap` takes a `uri` override |
| `deploy/systemd/README.md` | socket ownership and modes | plus the operator-credential expectation for the `--wipe` reap |

No caller migration and no obsolete path: `stack-down.sh` already owns the reap and keeps it.

## Task 1 — one privilege decision for the whole `--wipe` reap

One task: no reviewer could accept the classifier while rejecting the call sites consuming it.

**Interfaces.** Defines, for this file only — `reap_as_root`: integer `0`/`1`, set once inside
the `--wipe` branch; `reap_run <command> [args...]`: runs `<command>` under `sudo` when
`reap_as_root` is 1 and directly otherwise, passing through exit status and both streams.
Consumes unchanged from `scripts/live-stack/libvirt-uri.sh`: `require_libvirt_uri <operation>`
and the exported `KDIVE_LIBVIRT_URI`. Both confirmed present in that file at this branch's base.

### Verification

- **Session endpoint escalates nowhere.** `focused-test`:
  `test_wipe_reaps_a_session_endpoint_without_sudo`. Red before the change: the recording stub's
  log exists and names `virsh`, because today's reap calls `sudo virsh` unconditionally. Green:
  `just test-verbose tests/scripts/test_live_stack_scripts.py::test_wipe_reaps_a_session_endpoint_without_sudo`
- **Non-session endpoint still escalates.** `focused-test`:
  `test_wipe_reaps_a_system_endpoint_under_sudo`. Red before the change: the `list` line is
  absent from the log, because the enumeration runs bare today. Green:
  `just test-verbose tests/scripts/test_live_stack_scripts.py::test_wipe_reaps_a_system_endpoint_under_sudo`
- **#2515's reporting preserved.** `focused-test`: the existing `test_wipe_*` arms, unmodified.
  Green: `just test-verbose tests/scripts/test_live_stack_scripts.py -k wipe`
- **README paragraph.** `task-test-not-applicable`: operator prose with no executable consumer;
  no task-specific observation could fail meaningfully, and a wording search asserts nothing.

### Steps

1. In `enumerate_kdive_domains()`, route the enumeration through the wrapper, and rewrite the
   header paragraph naming #2516 to record that the split is now closed rather than open. While
   rewriting that block, replace its `libvirt-uri.sh:119-122` citation — and the identical one in
   the overlay-warning comment — with a citation by name rather than by line: the base merge
   already moved that passage, so a line number there is stale the next time the file is touched:

   ```bash
   out="$(reap_run virsh -c "$KDIVE_LIBVIRT_URI" list --all --name 2>&1)" || {
   ```

2. Below that function, add the wrapper, with a comment citing ADR-0662 and noting that
   `reap_as_root` is set in the `--wipe` branch only, so a call from elsewhere dies on `set -u`:

   ```bash
   reap_run() {
     if ((reap_as_root)); then
       sudo "$@"
     else
       "$@"
     fi
   }
   ```

3. Inside the `--wipe` gate, between the `require_libvirt_uri` block and the `gate_out=` probe,
   add the classifier. Its comment carries ADR-0662's reasoning in short: two daemon scopes
   reach one variable; `sudo` against the session socket bypasses the `kdive-live-libvirt` gate
   instead of satisfying it, and against a plain `qemu:///session` reaches root's own per-uid
   daemon; the query is stripped because the published URIs carry the socket path there; an
   unclassifiable value falls to the escalating branch and so keeps today's behaviour.

   ```bash
   case "${KDIVE_LIBVIRT_URI%%\?*}" in
   */session) reap_as_root=0 ;;
   *) reap_as_root=1 ;;
   esac
   ```

4. Route the three remaining escalating calls through the wrapper, leaving their redirections,
   `|| true` suppressions, and surrounding comments exactly as they are:

   ```bash
   reap_run virsh -c "$KDIVE_LIBVIRT_URI" destroy "$dom" >/dev/null 2>&1 || true
   undefine_err["$dom"]="$(reap_run virsh -c "$KDIVE_LIBVIRT_URI" undefine "$dom" 2>&1 >/dev/null || true)"
   rm_err="$(reap_run rm -f "$overlay" 2>&1 >/dev/null || true)"
   ```

5. In `tests/scripts/test_live_stack_scripts.py`, give `_wipe_reap` a keyword-only
   `uri: str | None = None`; when set, assign `staged["KDIVE_LIBVIRT_URI"] = uri` after
   `_published_contract` returns, and say in the docstring that an explicit value wins over the
   staged contract because `resolve_libvirt_uri` honours a caller value by design.

6. Add the two arms. Each installs a recording `sudo` through `lib_extra` — appended after
   `_REAP_STUBS`, so it overrides that module's pass-through stub — and still calls `"$@"`, so
   the reap runs to completion and the exit code stays an assertion.

7. In `deploy/systemd/README.md`, after the paragraph ending "start a new login session after
   installation before using the installed socket." and before the `## Lifecycle retry actions`
   heading, add a paragraph scoped to the reap: `stack-down.sh --wipe` reaches the published
   session endpoint and the overlay directory as the operator, which owns both (socket
   `operator_uid:group_gid:770`, `/var/lib/kdive/rootfs` `operator:kdive-live-libvirt` `2770`);
   worker accounts reach the same paths through `kdive-live-libvirt`. `sudo` is not that access
   path, because root satisfies neither ACL — it bypasses both — and a per-uid session daemon is
   not reachable by changing uid at all. Cite ADR-0662.

   Scoped to the reap deliberately, and NOT widened to tooling in general: `stack-services.sh`
   uses `sudo install -d` to create the provider data directories and `sudo systemctl enable` to
   socket-activate the system daemon on a bare host, so a blanket "`sudo` is not the access path"
   would be false in-tree. `scripts/live-stack/README.md` already scopes the same distinction
   correctly and is the wording to match.

8. Prove the arms bite: `git stash push -- scripts/live-stack/stack-down.sh`, run both new arms,
   record the two failures, `git stash pop`, re-run. Expected: red without the change, green
   with it.

9. Run the guardrails bare: `just lint`, `just lint-shell`, `just type`, `just test-changed`.
   Expected exit 0 from each.

### Acceptance criteria

- Both new arms pass, and each was observed failing with the script change absent.
- Every pre-existing `test_wipe_*` arm passes unmodified.
- `deploy/systemd/README.md` states the reap's operator-credential expectation, does not widen
  it to tooling generally, and cites ADR-0662.
- `just ci` exits 0.

### Rollback

Confined to one script, one test module, one README paragraph; reverting the commit restores the
prior behaviour with no state to undo.
