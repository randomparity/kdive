# Implementation plan — local external-boot recovery root

Spec: [2026-09-03-external-boot-recovery-root-design.md](../specs/2026-09-03-external-boot-recovery-root-design.md).
Issue: #2210. Governing decision: ADR-0586.

**Goal.** Supply the `recovery_root` that `RealLocalExternalBootIO` needs — one validated
`KDIVE_LIBVIRT_RECOVERY_ROOT` setting, and `live_vm_host` provisioning that creates and
health-gates a directory the module's own guards accept — without constructing anything that
consumes it.

**Architecture.** One new `Setting` in the local-libvirt settings module whose `parse`
restates the four conditions `_require_private_owned_directory` enforces, so a bad root is
rejected by `Registry.validate` at process start. One traverse-only parent plus one
`0700` per-slot child in the `live_vm_host` Ansible role, health-gated in that role's
`verify.yml`. No production code constructs `RealLocalExternalBootIO`, and a test asserts
`ProviderRuntime.external_boot` is still `None`.

**Tech stack.** Python 3.14 managed with `uv`; Ansible (`ansible-core==2.21.1`) for
provisioning; pytest; `just` recipes are the single source of truth for all checks.

Expected implementation size: 340–470 changed lines (M) — derived from the file map and task
list below: ~50 lines of settings code, ~70 lines of role YAML, ~85 lines of harness, and
~215 lines of tests.

## Global Constraints

Transcribed from `AGENTS.md` and the spec:

- Ruff line length **100**; lint set `E,F,I,UP,B,SIM`. `ty` runs with strict defaults over
  **src + tests**, not `src` alone.
- Doc-style guard is project-wide and covers code comments and commit messages: use
  **Milestone**, never "Sprint"; avoid "critical", "robust", "comprehensive", "elegant".
- Never pipe a gate recipe through `tail`/`head` — a pipeline returns the *last* command's
  status. Never append `; echo $?`. Use redirection: `just ci > <file> 2>&1 < /dev/null`.
  `just ci` runs `lint-ansible`, and ansible-core aborts with
  `ERROR: Ansible requires blocking IO on stdin/stdout/stderr` when any of the three is
  non-blocking, which is how an agent harness commonly supplies them — hence the redirects
  and `< /dev/null`.
- The host shell is **zsh**; its array is `pipestatus` and is 1-indexed. `${PIPESTATUS[0]}`
  is empty. Prefer running a gate as the last command and letting its status stand.
- `required_when` must be **omitted** from the `Setting(...)` call, never set to a local
  always-false predicate: `scripts/generate/gen_config_reference.py:32` compares
  `setting.required_when is never_required` by identity, and a local equivalent renders
  `Required: conditional` into `docs/guide/reference/config.md`. `just config-docs`
  regenerates that wrong value consistently, so `config-docs-check` would stay green.
- The setting carries **no** `default`. `Registry.validate` parses only settings whose name
  is in the environment (`registry.py:170-171`), so a default escapes the startup preflight
  as well as swallowing the absent-rejection.
- `deploy/ansible/tests/README.md` states the rule every harness follows: drive the **real**
  tasks, never a copy of the logic.
- `just test-ansible` enumerates its harnesses explicitly. A new `run-*.sh` that is not added
  to that recipe never runs in CI.
- **Never build a fixture directory with `Path.mkdir(mode=...)`.** That mode is masked by
  the process umask, so a `0o711` parent silently becomes `0o700` under umask `077` and a
  `0o700` root becomes `0o600` under umask `177` — the tests would then pass or fail by
  environment rather than by behaviour. Always `mkdir()` then `chmod()`, which is not
  masked. The Ansible `file` module sets the mode explicitly and is unaffected.
- On a fresh worktree `just check-mermaid` aborts CI with `ERR_MODULE_NOT_FOUND: jsdom`
  before the test recipe runs. Run `npm ci` in `.github/scripts/mermaid-check/` first. Known
  (#2156), unrelated to this change.

## File map

| File | Created / changed | Answerable for |
|---|---|---|
| `src/kdive/providers/local_libvirt/settings.py` | changed | the setting declaration and its directory validation |
| `docs/guide/reference/config.md` | changed (generated) | the published operator reference row |
| `deploy/ansible/roles/live_vm_host/defaults/main.yml` | changed | the root path and its owning account |
| `deploy/ansible/roles/live_vm_host/tasks/main.yml` | changed | creating the parent and the per-slot roots |
| `deploy/ansible/roles/live_vm_host/tasks/verify.yml` | changed | the health gate over both |
| `deploy/ansible/tests/external_boot_recovery_root.yml` | created | the isolation play for the harness |
| `deploy/ansible/tests/run-external-boot-recovery-root.sh` | created | the clean-host regression harness |
| `deploy/ansible/tests/README.md` | changed | the harness table row |
| `justfile` | changed | running the new harness in `test-ansible` |
| `tests/providers/local_libvirt/test_settings.py` | changed | setting field pins and rejection cases |
| `tests/providers/local_libvirt/test_recovery_root_guard.py` | created | equivalence with the real guard, and resolution-time validation |
| `tests/providers/local_libvirt/test_composition.py` | changed | the closed-gate assertion |
| `tests/deploy/test_live_worker_provisioning.py` | changed | the role's parsed provisioning contract |

## Task 1 — the setting and its validation

**Interfaces.** Consumes `Setting` from `kdive.config.registry` and the module's existing
`_RT` (`frozenset({"worker", "reconciler"})`, `settings.py:13`). Provides
`settings.LIBVIRT_RECOVERY_ROOT` and the module-private `_private_owned_directory(raw: str)
-> Path`, both relied on by Tasks 3 and 4. Appends `LIBVIRT_RECOVERY_ROOT` to the existing
`SETTINGS` list.

**Where it fits.** First task: Tasks 2 and 4 both reference the setting name, and Task 3
proves this task's restated guard matches the real one.

### Step 1.1 — write the failing tests

Append to `tests/providers/local_libvirt/test_settings.py`:

```python
def test_recovery_root_setting_fields() -> None:
    s = settings.LIBVIRT_RECOVERY_ROOT
    assert s.name == "KDIVE_LIBVIRT_RECOVERY_ROOT"
    # No default: absence must be rejectable by name, and Registry.validate parses only
    # settings present in the environment, so a default would also escape the preflight.
    assert s.default is None
    assert s.group == "local-libvirt"
    assert s.processes == _RT
    assert s.secret is False
    # Identity, not equality: gen_config_reference.py:32 tests `is never_required`, and a
    # local always-false predicate would publish "Required: conditional".
    assert s.required_when is never_required


def test_recovery_root_rejects_a_relative_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        settings.LIBVIRT_RECOVERY_ROOT.parse("relative/recovery")


def test_recovery_root_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing directory"):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(tmp_path / "absent"))


def test_recovery_root_rejects_a_non_directory(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("", encoding="utf-8")
    target.chmod(0o700)
    with pytest.raises(ValueError, match="must be a directory"):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(target))


def test_recovery_root_rejects_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    real.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    # lstat, not stat: the link is judged as itself, matching the stores' O_NOFOLLOW open.
    with pytest.raises(ValueError, match="symlink"):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(link))


@pytest.mark.parametrize("mode", [0o750, 0o755, 0o600, 0o770])
def test_recovery_root_rejects_a_mode_other_than_0700(tmp_path: Path, mode: int) -> None:
    root = tmp_path / "recovery"
    root.mkdir()
    root.chmod(mode)
    with pytest.raises(ValueError, match="0o700"):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(root))


def test_recovery_root_rejects_a_foreign_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "recovery"
    root.mkdir()
    root.chmod(0o700)
    # geteuid is the comparison the store makes; move it rather than the directory's owner,
    # which a test cannot change without privilege.
    monkeypatch.setattr(settings.os, "geteuid", lambda: os.stat(root).st_uid + 1)
    with pytest.raises(ValueError, match="owned by the running user"):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(root))


def test_recovery_root_accepts_a_provisioned_shape_directory(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir()
    root.chmod(0o700)
    assert settings.LIBVIRT_RECOVERY_ROOT.parse(str(root)) == root
```

Extend the existing ordering test in the same file to include the new setting last:

```python
def test_settings_list_is_the_declared_settings_in_order() -> None:
    assert settings.SETTINGS == [
        settings.LIBVIRT_URI,
        settings.LIBVIRT_ALLOCATION_CAP,
        settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER,
        settings.LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S,
        settings.LIBVIRT_BOOT_WINDOW_S,
        settings.LIBVIRT_RECOVERY_ROOT,
    ]
```

Add these imports at the top of the test file: `import os`, `from pathlib import Path`, and
`from kdive.config.registry import never_required`.

### Step 1.2 — run them and confirm they fail

```
uv run python -m pytest tests/providers/local_libvirt/test_settings.py -q
```

Expect failures, all of the form
`AttributeError: module 'kdive.providers.local_libvirt.settings' has no attribute
'LIBVIRT_RECOVERY_ROOT'`. An `AttributeError` here is the expected red — the parse tests
cannot fail on their own behaviour until the setting exists.

### Step 1.3 — implement

In `src/kdive/providers/local_libvirt/settings.py`, add `import os`, `import stat`, and
`from pathlib import Path` to the imports (stdlib only — the module docstring's
dependency-light rule is about the `libvirt` C-extension, and
`providers/external_boot_authority/settings.py:5` already imports `Path` the same way).

Add after `_parse_positive_int`:

```python
def _private_owned_directory(raw: str) -> Path:
    """Resolve an absolute path the local recovery stores will accept as their root.

    Restates the conditions ``_require_private_owned_directory`` enforces on every open
    (``lifecycle/boot/external_boot.py``, ADR-0586): a real directory, mode exactly 0700,
    owned by the running euid. It is restated rather than imported because this module stays
    free of provider imports (see the module docstring) and #2210 excludes editing that
    guard; ``tests/providers/local_libvirt/test_recovery_root_guard.py`` holds the two in
    step by opening a directory this accepts through the real guard.

    Uses ``os.lstat`` so a symlink is judged as itself, matching the stores' ``O_NOFOLLOW``.
    Raises ``ValueError`` so the registry surfaces a ``CONFIGURATION_ERROR``.
    """
    value = Path(raw)
    if not value.is_absolute():
        raise ValueError(f"must be an absolute path (got {raw!r})")
    try:
        entry = os.lstat(value)
    except OSError as exc:
        raise ValueError(f"must be an existing directory ({exc.strerror})") from exc
    if stat.S_ISLNK(entry.st_mode):
        raise ValueError("must be a real directory, not a symlink")
    if not stat.S_ISDIR(entry.st_mode):
        raise ValueError("must be a directory")
    mode = stat.S_IMODE(entry.st_mode)
    if mode != 0o700:
        raise ValueError(f"must be mode 0o700 (owner-only); got {mode:#o}")
    euid = os.geteuid()
    if entry.st_uid != euid:
        raise ValueError(
            f"must be owned by the running user; owned by {entry.st_uid}, running as {euid}"
        )
    return value
```

Keep every line at or under 100 characters — the last `raise` needs wrapping:

```python
raise ValueError(
    f"must be owned by the running user; owned by uid {entry.st_uid}, running as uid {euid}"
)
```

Then declare the setting after `LIBVIRT_BOOT_WINDOW_S`:

```python
LIBVIRT_RECOVERY_ROOT = Setting(
    name="KDIVE_LIBVIRT_RECOVERY_ROOT",
    parse=_private_owned_directory,
    group="local-libvirt",
    processes=_RT,
    help=(
        "Provider-owned root holding one local external-boot recovery point per activation "
        "(ADR-0586). Must be an existing owner-only directory — mode 0700, owned by the "
        "running worker account — which the recovery stores re-check on every open. It has "
        "no default: an unset root is rejected by name rather than silently assumed, and "
        "leaving it unset keeps the dormant external-boot path off."
    ),
    suggest=(
        "set an absolute path to an existing mode-0700 directory owned by the worker "
        "account; provisioning creates one per slot under "
        "/var/lib/kdive/live-workers/external-boot-recovery"
    ),
)
```

Note there is deliberately no `default=` and no `required_when=` argument.

Append `LIBVIRT_RECOVERY_ROOT` to the `SETTINGS` list.

### Step 1.4 — confirm green, and regenerate the reference

```
uv run python -m pytest tests/providers/local_libvirt/test_settings.py -q
```

Expect all tests passing.

```
just config-docs
git diff --stat docs/guide/reference/config.md
```

Expect exactly one added row in the `local-libvirt` group for
`KDIVE_LIBVIRT_RECOVERY_ROOT`, with `Required: no`. If the row reads
`Required: conditional`, `required_when` was passed rather than omitted — see Global
Constraints.

```
just lint && just type
```

Expect both clean.

### Step 1.5 — commit

`feat(config): add the local external-boot recovery root setting`

**Acceptance criteria.** `KDIVE_LIBVIRT_RECOVERY_ROOT` exists, is registered in `SETTINGS`,
appears in the generated reference as not-required, has no default, and rejects each of the
seven conditions with a message naming the condition.

## Task 2 — provisioning and the health gate

**Interfaces.** Consumes the existing `live_vm_host_worker_accounts` list
(`defaults/main.yml:48-56`, the eight `kdive-worker-N` names). Provides two new role
variables relied on by Task 5's contract test and by the harness:
`live_vm_host_worker_recovery_root` (string, absolute path) and
`live_vm_host_worker_recovery_root_owner` (string, account name owning the traverse-only
parent). Provides four task tags: `external_boot_recovery_root` (parent),
`external_boot_recovery_root_slots` (children), `external_boot_recovery_root_verify` (gate).

**Where it fits.** Independent of Task 1 at the code level — nothing yet passes the
provisioned path to the setting — but it is the other half of what #2210 requires, and Task 3
proves the shape it creates is the shape Task 1 accepts.

### Step 2.1 — defaults

In `deploy/ansible/roles/live_vm_host/defaults/main.yml`, after the
`live_vm_host_worker_fixture_files` block:

```yaml
# Local external-boot recovery roots (ADR-0586, #2210). The recovery stores open their root
# O_NOFOLLOW and require mode exactly 0700 owned by the running euid, so there is one root
# per fixed worker account rather than one shared root: a single directory cannot be
# uid-owned by eight slots. The parent is traverse-only (0711) so a slot reaches its own
# child by name without being able to list its siblings. The owner is a variable because the
# clean-host harness drives these same tasks unprivileged.
live_vm_host_worker_recovery_root: /var/lib/kdive/live-workers/external-boot-recovery
live_vm_host_worker_recovery_root_owner: root
```

### Step 2.1a — correct two ADR citations in the role (incidental)

Both comments name the fixed host-worker contract as ADR-0555. That number belongs to
`0555-reap-orphaned-pcap-capture-volumes.md`. The contract they mean is ADR-0574
(`0574-systemd-supervises-host-worker-incarnations.md:27`, "Each instance runs as its own
no-login account"). Change `(ADR-0555)` to `(ADR-0574)` in exactly these two places and
nothing else:

- `deploy/ansible/roles/live_vm_host/defaults/main.yml:46` — "Fixed host-worker authority
  contract (ADR-0555)"
- `deploy/ansible/roles/live_vm_host/tasks/verify.yml:53` — "Part 3: the fixed live-worker
  authority contract (ADR-0555)"

Same verified root cause, same role, both files already edited by this task. Carried here on
the campaign's found-here-fixed-here rule; record it in the PR body as an incidental
correction naming both paths, so a reviewer is not surprised by the extra hunks.

### Step 2.2 — creation tasks

In `deploy/ansible/roles/live_vm_host/tasks/main.yml`, immediately after the
"Create root-owned fixed slot directories" task (which ends at the `loop:` on line 404):

```yaml
- name: Inspect the external-boot recovery parent without following links
  ansible.builtin.stat:
    path: "{{ live_vm_host_worker_recovery_root }}"
    follow: false
  register: live_vm_host_recovery_root_before
  tags: [external_boot_recovery_root]

- name: Require a real external-boot recovery parent
  ansible.builtin.assert:
    that:
      - >-
        not live_vm_host_recovery_root_before.stat.exists
        or (
        live_vm_host_recovery_root_before.stat.isdir
        and not live_vm_host_recovery_root_before.stat.islnk
        )
    fail_msg: >-
      {{ live_vm_host_worker_recovery_root }} must be a real directory; the existing entry
      was left untouched. Correct it and rerun provisioning.
  tags: [external_boot_recovery_root]

- name: Create the traverse-only external-boot recovery parent
  ansible.builtin.file:
    path: "{{ live_vm_host_worker_recovery_root }}"
    state: directory
    owner: "{{ live_vm_host_worker_recovery_root_owner }}"
    group: "{{ live_vm_host_worker_recovery_root_owner }}"
    mode: "0711"
  tags: [external_boot_recovery_root]

- name: Create the owner-only external-boot recovery root for each worker slot
  ansible.builtin.file:
    path: >-
      {{ live_vm_host_worker_recovery_root }}/{{
      item | regex_replace('^kdive-worker-', '') }}
    state: directory
    owner: "{{ item }}"
    group: "{{ item }}"
    mode: "0700"
  loop: "{{ live_vm_host_worker_accounts }}"
  tags: [external_boot_recovery_root_slots]
```

### Step 2.3 — health gate

In `deploy/ansible/roles/live_vm_host/tasks/verify.yml`, after the "Assert authority
protected path ownership" task (which ends on line 365):

```yaml
- name: Inspect the external-boot recovery parent
  ansible.builtin.stat:
    path: "{{ live_vm_host_worker_recovery_root }}"
    follow: false
  register: live_vm_host_recovery_root_check
  tags: [external_boot_recovery_root_verify]

- name: Assert the external-boot recovery parent is traverse-only
  ansible.builtin.assert:
    that:
      - live_vm_host_recovery_root_check.stat.exists
      - live_vm_host_recovery_root_check.stat.isdir
      - not live_vm_host_recovery_root_check.stat.islnk
      - >-
        live_vm_host_recovery_root_check.stat.pw_name
        == live_vm_host_worker_recovery_root_owner
      - live_vm_host_recovery_root_check.stat.mode == '0711'
    fail_msg: >-
      {{ live_vm_host_worker_recovery_root }} must be a real mode-0711 directory owned by
      {{ live_vm_host_worker_recovery_root_owner }}, so a worker slot reaches its own
      recovery root without being able to list its siblings.
  tags: [external_boot_recovery_root_verify]

- name: Inspect the per-slot external-boot recovery roots
  ansible.builtin.stat:
    path: >-
      {{ live_vm_host_worker_recovery_root }}/{{
      item | regex_replace('^kdive-worker-', '') }}
    follow: false
  loop: "{{ live_vm_host_worker_accounts }}"
  register: live_vm_host_recovery_slot_checks
  tags: [external_boot_recovery_root_verify]

- name: Assert each per-slot external-boot recovery root is owner-only
  ansible.builtin.assert:
    that:
      - item.stat.exists
      - item.stat.isdir
      - not item.stat.islnk
      - item.stat.pw_name == item.item
      - item.stat.gr_name == item.item
      - item.stat.mode == '0700'
    fail_msg: >-
      The external-boot recovery root for {{ item.item }} under
      {{ live_vm_host_worker_recovery_root }} must be a mode-0700 directory owned by
      {{ item.item }}; the local recovery stores refuse any other shape at open time.
  loop: "{{ live_vm_host_recovery_slot_checks.results }}"
  loop_control:
    label: "{{ item.item }}"
  tags: [external_boot_recovery_root_verify]
```

`stat.exists` is asserted first on purpose: without it, an absent root fails on an undefined
`isdir` attribute rather than reporting what is wrong.

### Step 2.4 — verify

```
just lint-ansible > /tmp/lint-ansible.log 2>&1 < /dev/null
```

Expect exit 0. Redirect rather than pipe — `ansible-lint` refuses non-blocking stdout, and a
pipeline would hide the recipe's own status.

### Step 2.5 — commit

`feat(provisioning): create and gate the external-boot recovery roots`

**Acceptance criteria.** The role creates a `0711` parent and one `0700` per-account child,
`verify.yml` fails when either is absent, wrongly owned, or wrongly permissioned, and
`just lint-ansible` is clean.

## Task 3 — prove the restated guard matches the real one

**Interfaces.** Consumes `settings.LIBVIRT_RECOVERY_ROOT` from Task 1 and the real
`_open_private_directory` / `RecoveryMetadataStore` from
`kdive.providers.local_libvirt.lifecycle.boot.external_boot`. Adds no production code.

**Where it fits.** This is the test that makes Task 1's restatement safe. Without it, the
two copies of the guard can drift and the drift surfaces during a live recovery.

### Step 3.1 — write the test

Create `tests/providers/local_libvirt/test_recovery_root_guard.py`:

```python
"""Hold the recovery-root setting's restated guard in step with the real one (#2210)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kdive.config.registry import Registry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt import settings
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    RecoveryMetadataStore,
    _open_private_directory,
)


def _provisioned_root(tmp_path: Path) -> Path:
    """A directory of exactly the shape Task 2's Ansible creates: mode 0700, euid-owned."""
    root = tmp_path / "external-boot-recovery" / "1"
    root.parent.mkdir()
    root.parent.chmod(0o711)
    root.mkdir()
    root.chmod(0o700)
    return root


def test_provisioned_shape_is_accepted_by_the_real_guard(tmp_path: Path) -> None:
    root = _provisioned_root(tmp_path)
    parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # The real guard, unchanged -- not a re-assertion of the mode in isolation.
        fd = _open_private_directory(parent_fd, root.name)
        os.close(fd)
    finally:
        os.close(parent_fd)
    with RecoveryMetadataStore(root):
        pass


def test_the_setting_accepts_exactly_what_the_real_store_accepts(tmp_path: Path) -> None:
    root = _provisioned_root(tmp_path)
    assert settings.LIBVIRT_RECOVERY_ROOT.parse(str(root)) == root


@pytest.mark.parametrize("mode", [0o750, 0o755, 0o770, 0o600])
def test_the_setting_and_the_real_store_reject_the_same_modes(tmp_path: Path, mode: int) -> None:
    root = _provisioned_root(tmp_path)
    root.chmod(mode)
    with pytest.raises(ValueError):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(root))
    with pytest.raises(ValueError):
        RecoveryMetadataStore(root)


def test_the_setting_and_the_real_store_both_reject_a_symlinked_root(
    tmp_path: Path,
) -> None:
    # Both reject, but NOT with the same exception type, so this asserts rejection rather
    # than a shared type: the setting raises ValueError from its own lstat check, while the
    # store's O_NOFOLLOW open raises NotADirectoryError (ENOTDIR) before the guard is
    # reached. Verified against the real store on this branch.
    root = _provisioned_root(tmp_path)
    link = root.parent / "2"
    link.symlink_to(root)
    with pytest.raises(ValueError):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(link))
    with pytest.raises(OSError):
        RecoveryMetadataStore(link)


def test_a_bad_root_fails_at_configuration_resolution(tmp_path: Path) -> None:
    """validate() is the startup preflight: a bad root fails there, not at first recovery."""
    root = _provisioned_root(tmp_path)
    root.chmod(0o755)
    registry = Registry([settings.LIBVIRT_RECOVERY_ROOT])
    registry.load({"KDIVE_LIBVIRT_RECOVERY_ROOT": str(root)})
    with pytest.raises(CategorizedError) as caught:
        registry.validate("worker")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "KDIVE_LIBVIRT_RECOVERY_ROOT" in str(caught.value)


def test_an_absent_root_is_rejected_by_name(tmp_path: Path) -> None:
    registry = Registry([settings.LIBVIRT_RECOVERY_ROOT])
    registry.load({})
    with pytest.raises(CategorizedError) as caught:
        registry.require(settings.LIBVIRT_RECOVERY_ROOT)
    assert "KDIVE_LIBVIRT_RECOVERY_ROOT is not set" in str(caught.value)
    assert caught.value.details["suggest"]
```

### Step 3.2 — run it

```
uv run python -m pytest tests/providers/local_libvirt/test_recovery_root_guard.py -q
```

Expect all passing once Task 1 is in. If
`test_the_setting_and_the_real_store_reject_the_same_modes` fails on one side only, the
restatement has drifted from the real guard — fix `_private_owned_directory`, not the test.

### Step 3.3 — commit

`test(local-libvirt): pin the recovery-root guard to the real store`

**Acceptance criteria.** A provisioned-shape directory opens through the unmodified
`_open_private_directory` and `RecoveryMetadataStore`; a misconfigured root raises
`CONFIGURATION_ERROR` from `Registry.validate`; an absent root raises by name from
`require`.

## Task 4 — assert the composition gate stays closed

**Interfaces.** Consumes `composition.build_runtime` and `settings.LIBVIRT_RECOVERY_ROOT`.
Adds no production code.

**Where it fits.** #2212 is the single point at which `ProviderRuntime.external_boot`
becomes non-`None`. This test makes reordering the chain fail here rather than silently.

### Step 4.1 — write the test

Append to `tests/providers/local_libvirt/test_composition.py`:

```python
def test_configuring_a_recovery_root_does_not_advertise_external_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #2210 provisions the recovery root; #2212 alone binds RealLocalExternalBootIO. A
    # configured root is the one input that could plausibly open the gate, so configure it
    # and require composition to stay unadvertised anyway.
    root = tmp_path / "external-boot-recovery" / "1"
    root.parent.mkdir()
    root.parent.chmod(0o711)
    root.mkdir()
    root.chmod(0o700)
    monkeypatch.setenv("KDIVE_LIBVIRT_RECOVERY_ROOT", str(root))
    config.reset()

    runtime = composition.build_runtime(secret_registry=SecretRegistry())

    # Presence first: without this the value assertion below would pass vacuously if the
    # attribute were ever renamed away, and a gate test that passes on nothing is worse
    # than no gate test.
    assert hasattr(runtime, "external_boot")
    assert runtime.external_boot is None
```

Add `from pathlib import Path` and `from kdive import config` to that file's imports if not
already present.

### Step 4.2 — run it, then prove it bites

```
uv run python -m pytest tests/providers/local_libvirt/test_composition.py -q
```

Expect passing. Then verify the test can fail, twice, reverting after each:

1. **Stub-binding fault** — in `composition.build_runtime`, set the runtime's
   `external_boot` to a non-`None` sentinel. Re-run; expect
   `assert None is None` to become a clean `AssertionError` on the
   `runtime.external_boot is None` line, **not** a collection or import error.
2. **Vacuity fault** — change the assertion to
   `assert getattr(runtime, "external_boot", None) is None` and delete the attribute from
   the runtime. Re-run; expect it to **pass**, demonstrating that the `hasattr` line is
   what stops a vacuous pass. Restore the original assertion and confirm it now fails.

After each, revert and confirm byte-identity:

```
git stash list && git diff --stat
sha256sum src/kdive/providers/local_libvirt/composition.py tests/providers/local_libvirt/test_composition.py
```

Expect an empty `git diff --stat` for the reverted files and unchanged digests against the
values recorded before the fault.

### Step 4.3 — commit

`test(local-libvirt): assert the external-boot gate stays closed`

**Acceptance criteria.** `ProviderRuntime.external_boot` is `None` with the recovery root
configured; both faults were injected and the results are as described above.

## Task 5 — the clean-host harness and the deploy contract test

**Interfaces.** Consumes the four tags and two variables Task 2 defined. Provides
`deploy/ansible/tests/run-external-boot-recovery-root.sh`, added to `just test-ansible`.

**Where it fits.** Last: it exercises Task 2's real tasks end to end and is what makes the
provisioning claim checkable in CI.

### Step 5.1 — the isolation play

Create `deploy/ansible/tests/external_boot_recovery_root.yml`, matching the shape of
`remote-module-appliance.yml`:

```yaml
---
- name: Exercise the live_vm_host external-boot recovery roots in isolation
  hosts: localhost
  connection: local
  gather_facts: false
  become: false
  roles:
    - role: live_vm_host
```

Task selection is by `--tags` on the command line, so every untagged task in the role's
1500-line `main.yml` is skipped. `verify.yml` is reached because `main.yml:1524` imports it
with `import_tasks` (static), so its tags are selectable from the same run.

### Step 5.2 — the harness

Create `deploy/ansible/tests/run-external-boot-recovery-root.sh`, mode `0755`:

```sh
#!/bin/sh
# Clean-host regression harness for the local external-boot recovery roots (#2210, ADR-0586).
#
# Drives the REAL live_vm_host tasks against localhost, unprivileged, by overriding the two
# role variables naming the root and its owner plus the account list -- the same technique
# run-remote-module-appliance.sh uses for its install dir and owner. It proves the created
# shape is exactly what RecoveryMetadataStore's guard accepts, that creation is idempotent,
# and that the verify gate rejects each way the shape can be wrong.
#
# Why the idempotence check greps the PLAY RECAP rather than counting tasks: a --tags value
# that matches nothing produces an EMPTY recap -- no "localhost : ok=..." line at all -- so a
# mistyped tag makes the grep FAIL rather than pass on zero changes. That is the property
# that stops this harness degrading into a silent no-op, which is the usual failure of
# tag-scoped Ansible tests. Verified: a non-matching tag against the real role prints a play
# header and an empty recap. Do not "simplify" the grep to a task count or a bare exit-status
# check -- both pass vacuously when the tag stops matching.
set -eu

test_root=$(mktemp -d)
trap 'chmod -R u+w "$test_root" 2>/dev/null || :; rm -rf "$test_root"' EXIT HUP INT TERM

recovery_root="$test_root/external-boot-recovery"
me=$(id -un)

cat >"$test_root/vars.json" <<JSON
{
  "live_vm_host_worker_recovery_root": "$recovery_root",
  "live_vm_host_worker_recovery_root_owner": "$me",
  "live_vm_host_worker_accounts": ["$me"]
}
JSON

export ANSIBLE_CONFIG=deploy/ansible/ansible.cfg
export ANSIBLE_ROLES_PATH=deploy/ansible/roles
playbook=deploy/ansible/tests/external_boot_recovery_root.yml
create_tags=external_boot_recovery_root,external_boot_recovery_root_slots
verify_tags=external_boot_recovery_root_verify

run() {
  ansible-playbook "$playbook" -i localhost, --tags "$1" \
    -e "@$test_root/vars.json" >"$2" 2>&1
}

fail() {
  cat "$2" 2>/dev/null || :
  echo "FAIL: $1" >&2
  exit 1
}

# 1. Creation produces exactly the shape the recovery stores accept.
run "$create_tags" "$test_root/create.log" || fail "creation run failed" "$test_root/create.log"
[ "$(stat -c '%a' "$recovery_root")" = "711" ] ||
  fail "parent mode is $(stat -c '%a' "$recovery_root"), not 711" "$test_root/create.log"
[ "$(stat -c '%a' "$recovery_root/$me")" = "700" ] ||
  fail "slot mode is $(stat -c '%a' "$recovery_root/$me"), not 700" "$test_root/create.log"
[ "$(stat -c '%U' "$recovery_root/$me")" = "$me" ] ||
  fail "slot owner is $(stat -c '%U' "$recovery_root/$me"), not $me" "$test_root/create.log"
echo "ok create: parent 0711, per-slot root 0700 owned by the slot account"

# 2. Idempotence across re-runs.
run "$create_tags" "$test_root/second.log" || fail "second run failed" "$test_root/second.log"
grep -Eq 'changed=0([[:space:]].*)?failed=0([[:space:]]|$)' "$test_root/second.log" ||
  fail "re-run was not idempotent" "$test_root/second.log"
echo "ok idempotent: the second run reported changed=0"

# 3. The health gate accepts a provisioned tree.
run "$verify_tags" "$test_root/verify.log" ||
  fail "health gate rejected a provisioned tree" "$test_root/verify.log"
echo "ok verify: health gate accepts the provisioned tree"

# 4. It rejects a widened slot mode -- what the store refuses at open time.
chmod 0750 "$recovery_root/$me"
if run "$verify_tags" "$test_root/widened.log"; then
  fail "health gate accepted a mode-0750 recovery root" "$test_root/widened.log"
fi
chmod 0700 "$recovery_root/$me"
echo "ok verify: health gate rejects a widened slot mode"

# 5. It rejects a listable parent -- siblings must stay unenumerable.
chmod 0755 "$recovery_root"
if run "$verify_tags" "$test_root/parent.log"; then
  fail "health gate accepted a mode-0755 recovery parent" "$test_root/parent.log"
fi
chmod 0711 "$recovery_root"
echo "ok verify: health gate rejects a listable recovery parent"

# 6. It rejects an absent root, and says so rather than erroring on a missing attribute.
rm -rf "$recovery_root/$me"
if run "$verify_tags" "$test_root/absent.log"; then
  fail "health gate accepted an absent recovery root" "$test_root/absent.log"
fi
grep -q 'must be a mode-0700 directory owned by' "$test_root/absent.log" ||
  fail "absent root did not produce the actionable message" "$test_root/absent.log"
echo "ok verify: health gate rejects an absent recovery root by name"

# 7. It refuses a symlinked root rather than following it.
mkdir -p "$test_root/elsewhere"
chmod 0700 "$test_root/elsewhere"
ln -s "$test_root/elsewhere" "$recovery_root/$me"
if run "$verify_tags" "$test_root/symlink.log"; then
  fail "health gate followed a symlinked recovery root" "$test_root/symlink.log"
fi
echo "ok verify: health gate refuses a symlinked recovery root"
```

### Step 5.3 — register the harness

In `justfile`, add to the `test-ansible` recipe, after the existing five lines:

```
    uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-external-boot-recovery-root.sh
```

In `deploy/ansible/tests/README.md`, add a table row matching the tone of its neighbours:

```
| `run-external-boot-recovery-root.sh` | the `live_vm_host` external-boot recovery-root shape, idempotence, and health gate (#2210) |
```

### Step 5.4 — the deploy contract test

Extend `tests/deploy/test_live_worker_provisioning.py` with assertions parsed from the role
YAML, mirroring however that file already reads the role (read it first and follow its
existing helper, rather than introducing a second parsing style). Assert: the parent task
sets mode `0711`; the per-slot task sets mode `0700`, loops
`live_vm_host_worker_accounts`, and derives its path with the
`regex_replace('^kdive-worker-', '')` strip; the defaults declare
`live_vm_host_worker_recovery_root` as an absolute path; and the verify tasks assert
`stat.exists` before `stat.isdir`.

### Step 5.5 — run both

```
uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-external-boot-recovery-root.sh
```

Expect the seven `ok ...` lines and exit 0.

```
uv run python -m pytest tests/deploy/test_live_worker_provisioning.py -q
```

Expect all passing.

### Step 5.6 — prove the harness bites

Change the per-slot task's mode from `0700` to `0750` in `tasks/main.yml`, re-run the
harness, and expect `FAIL: slot mode is 750, not 700` with exit 1. Revert and confirm
`git diff --stat` is empty for that file.

### Step 5.7 — commit

`test(provisioning): add the external-boot recovery-root harness`

**Acceptance criteria.** The harness drives the real tasks, covers creation shape,
idempotence, and four health-gate rejections; it is registered in `just test-ansible` and in
the tests README; the deploy contract test extends the existing file.

## Final verification

```
cd .github/scripts/mermaid-check && npm ci
```

Then, from the worktree root, run the gate bare — a redirect, not a pipe, so the exit status
survives and `ansible-lint` gets blocking streams:

```
just ci > /tmp/ci-2210.log 2>&1 < /dev/null
```

Expect exit 0. On a non-zero exit, read the log; do not re-run under a pipeline.

## Rollback

Every task is additive: a new setting with no consumer, new role tasks behind new tags, new
tests, and a new harness. Reverting the branch removes all of it with no migration, no
persisted data, and no change to any existing code path. The provisioned directories, if a
play has already created them, are inert once the role no longer references them and can be
removed by hand.

## Deferrals carried from review

None yet. Any deferral a `$trial-loop` run disposes of during review is recorded here with
its owning record path or tracker issue.
