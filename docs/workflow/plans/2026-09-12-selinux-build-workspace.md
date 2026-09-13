# SELinux build-workspace label — implementation plan (#2428)

**Goal.** Let the local example build a guest image through its session daemon on an
SELinux-enforcing RedHat-family host by labeling the selected build workspace with the established
`svirt_image_t` contract before customization starts, including when the operator chooses a
symlinked or regular-expression-metacharacter-containing workspace name.

**Architecture.** `build-image.sh` remains the one caller that owns its workspace. It canonicalizes
once, then passes that value to the sourceable `kdive_label_svirt_image` helper and `build-fs`.
The helper escapes the fcontext literal before adding its recursive suffix. No SELinux command is
duplicated. The documentation follows the executable behavior. No provider runtime, libvirt URI,
policy module, or provisioning/install label behavior changes.

**Tech stack.** Bash, pytest, Markdown.

**Spec:** `docs/workflow/specs/2026-09-12-selinux-build-workspace-design.md`

Expected implementation size: 45–80 changed lines (M) — derived from one ordered shell call, a
small static script-contract test, and two focused documentation updates.

## Global Constraints

- Use Bash and preserve `set -euo pipefail`; quote workspace paths.
- Reuse `examples/local-libvirt/selinux-label.sh`; do not add SELinux policy, package, or wrapper.
- Do not edit provisioning/install labels (#2424), Ubuntu/AppArmor, or remote-libvirt.
- Run focused pytest, `just lint-shell`, documentation guards, and relevant `just` guardrails before
  committing. The full parity command is `just ci > <log> 2>&1 < /dev/null`.
- ADR-0640 is accepted and remains unmodified; the ADR index is not coupled.

## File map

| File | Action | Responsibility |
|---|---|---|
| `examples/local-libvirt/build-image.sh` | modify | canonicalize and label the shared workspace before build-fs |
| `examples/local-libvirt/selinux-label.sh` | modify | escape fcontext literals before recursive matching |
| `tests/scripts/test_build_image_workspace.py` | create | verify canonical-path reuse and ordering |
| `tests/scripts/test_selinux_label.py` | modify | prove metacharacters are escaped in fcontext patterns |
| `examples/local-libvirt/README.md` | modify | describe workspace labeling accurately |
| `docs/operating/providers/local-libvirt.md` | modify | retire the stale unresolved-build warning |

## Task 1 — label the workspace before customization

**Files:** modify `examples/local-libvirt/build-image.sh` and
`examples/local-libvirt/selinux-label.sh`; create `tests/scripts/test_build_image_workspace.py`;
modify `tests/scripts/test_selinux_label.py`.

**Interfaces.** Consume `kdive_label_svirt_image <canonical-directory>` from
`examples/local-libvirt/selinux-label.sh`; no new interface is introduced.

### Verification

| Contract | Mode | Detail |
|---|---|---|
| The script calls the existing helper and `build-fs` with one canonical workspace after `mkdir -p` | focused-test | Add a source-contract test; it is red before the shared canonical value exists and green with `uv run python -m pytest tests/scripts/test_build_image_workspace.py -q` |
| The helper writes literal fcontext patterns and retains its failure paths | focused-test | `uv run python -m pytest tests/scripts/test_selinux_label.py -q` passes its metacharacter, argument, and failure-path contracts |

### Steps

1. Canonicalize the chosen value once, preserving `build-fs`'s `Path.resolve()` semantics:

   ```bash
   workspace="$(realpath -m -- "${workspace}")"
   mkdir -p "${workspace}" "$(dirname "${systems_toml}")"
   kdive_label_svirt_image "${workspace}"
   ```

   Keep the existing `--workspace "${workspace}"` argument. Do not add a second `semanage`,
   `restorecon`, SELinux probe, or helper wrapper.
2. Make the helper produce its fcontext base with a Bash character loop. Each of `\\ . ^ $ * + ?
   ( ) [ ] { } |` receives one literal backslash; all other characters pass unchanged. Append the
   existing recursive descendant suffix only after the literal base is complete. This preserves arbitrary valid workspace
   names while keeping the rule to that path and descendants.
3. Add a small Python source-contract test that reads `build-image.sh`, finds `realpath -m`, the
   workspace creation, helper call, the exact `--workspace "${workspace}"` operand, and
   `build-fs --image` text. Assert their byte offsets are increasing, with the workspace operand
   before the image operand. This proves the ordering and shared-value contract without attempting
   a privileged image build.

   ```python
   from pathlib import Path

   SCRIPT = Path(__file__).resolve().parents[2] / "examples/local-libvirt/build-image.sh"

   def test_workspace_is_canonicalized_and_labeled_before_build_fs() -> None:
       source = SCRIPT.read_text(encoding="utf-8")
       assert source.index('realpath -m -- "${workspace}"') < source.index(
           'mkdir -p "${workspace}"'
       ) < source.index(
           'kdive_label_svirt_image "${workspace}"'
       ) < source.index('--workspace "${workspace}"') < source.index(
           'build-fs --image "${name}"'
       )
   ```

4. Extend `test_selinux_label.py` with a parameterized directory name that contains each of
   `\\ . ^ $ * + ? ( ) [ ] { } |`, including a literal backslash. Assert the stub records each
   escaped character before the helper's existing descendant suffix. This is red for direct
   interpolation and proves the complete literal fcontext boundary without a real policy store.
5. Run both focused test modules. Expected result: all selected tests pass.

## Task 2 — align operator guidance

**Files:** modify `examples/local-libvirt/README.md` and
`docs/operating/providers/local-libvirt.md`.

**Interfaces.** Consume the Task 1 behavior: the selected workspace is labeled on enforcing hosts
before `build-fs`.

### Verification

| Contract | Mode | Detail |
|---|---|---|
| Documentation names the implemented workspace-label behavior | task-test-not-applicable | prose has no executable consumer; `just docs-links` and `just docs-paths` validate repository documentation structure |

### Steps

1. Replace the example walkthrough's warning that host labeling covers provisioning but not build
   with a note that `build-image.sh` labels its workspace on enforcing hosts and needs the existing
   `semanage` prerequisite.
2. Replace the provider guide's unresolved-build limitation with the same bounded behavior; retain
   the separate Enterprise Linux Python-binding limitation.
3. Run `just docs-links` and `just docs-paths`. Expected result: both exit zero.

## Task 3 — verify and commit

**Files:** all Task 1 and Task 2 files only.

### Verification

| Contract | Mode | Detail |
|---|---|---|
| Changed shell and test behavior remain repository-conformant | focused-test | `just lint-shell`, focused pytest modules, `just lint`, and `just type` exit zero |

### Steps

1. Re-read the diff for accidental provider, policy, or label-tree expansion.
2. Run the listed checks and commit one conventional, scoped change after pre-commit formatting.
