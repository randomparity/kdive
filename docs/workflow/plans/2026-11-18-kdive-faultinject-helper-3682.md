# kdive-faultinject helper Implementation Plan

Goal: ship the `kdive-faultinject` guest helper and stage it wherever `kdive-drgn` is staged.
Architecture: a bash helper beside `kdive-drgn`; a staging function beside `drgn_helper_steps()`
called at its three call sites; one entry in the Ansible helper list. Spec:
`docs/workflow/specs/2026-11-18-kdive-faultinject-helper-3682-design.md`; decision: ADR-0822.
Tech stack: bash, Python 3.14, pytest, Ansible YAML.

## Global Constraints

- Branch `feat/kdive-faultinject-helper-3682` off `origin/main`; conventional commits; no ADR index.
- Knobs `failslab`, `fail_page_alloc`; bounds and exit codes exactly as ADR-0822.
- A `KDIVE_`-prefixed variable under `scripts/` or helpers fails `env-docs-check`; the helper's
  test override is `FAULTINJECT_DEBUGFS_ROOT`.
- Gates: `just lint`, `just type`, `just lint-shell`, `just lint-ansible`, `just test-ansible`,
  `just records`, then `just ci > FILE 2>&1 < /dev/null`.

Expected implementation size: 230-300 changed lines (M) - helper about 130 lines, tests about 130, staging and list about 30.

File map: create `deploy/remote-libvirt-guest-helpers/kdive-faultinject`,
`tests/scripts/test_kdive_faultinject_helper.py`; modify
`src/kdive/images/families/_fedora_customize.py`, `debian.py`, `suse.py`,
`deploy/ansible/inventory/group_vars/all.yml`, `deploy/remote-libvirt-guest-helpers/README.md`,
`tests/images/families/test_{debian,suse,fedora_customize}.py`,
`tests/providers/local_libvirt/test_rootfs_build.py`, `tests/deploy/test_guest_base_image_helpers.py`.
`guest_base_image` role tasks need no change: `build_one.yml` loops the helper list.

## Task 1: the helper

Files: create the helper (mode 0755) and its test file. Interfaces: argv `read|set|reset`; exit
codes 0/2/3/4/5; env `FAULTINJECT_DEBUGFS_ROOT`.

Verification:
- Mode: focused-test. Contract: read/set/reset, bounds, injection rejection, exit 3 vs 4.
  Test `tests/scripts/test_kdive_faultinject_helper.py` builds a tmp fake debugfs
  (`failslab/` and `fail_page_alloc/` with one file per parameter, plus a `tracing/` dir so the
  root is non-empty) and runs `bash <helper>` with the env override. Red: the helper file is
  absent (FileNotFoundError / exit 127). Green: `just test-verbose tests/scripts/test_kdive_faultinject_helper.py`.

Steps: (1) write the test cases: read all and one; set writes each parameter and prints the new
state; set with several pairs; reset writes defaults; bounds min/max accepted and min-1/max+1
rejected for every parameter (exit 2, files unchanged); unknown knob, unknown parameter, parameter
valid for the other knob (`min-order` on failslab), `../x`, `a/b`, `probability=1;id`, `=5`, empty
value, `+5`, `--5`, `-0`, `007`, a negative on a parameter whose minimum is 0, 11 digits, a repeated
parameter, `set failslab` with no pair (all exit 2, no file changed, also with a good pair before the bad
one); missing root and empty root exit 3; knob dir missing exits 4 for `read failslab`, and
`read` with none reports `present:false`; unreadable, missing or non-integer parameter file exits 5; stdout parses as
JSON on success, stderr parses as JSON with `error` on failure; no subcommand and an unknown
subcommand exit 2. (2) Run, see red. (3) Write the helper. (4) Run, see green; run
`just lint-shell`. (5) Commit `feat(guest): add the kdive-faultinject helper (#3682)`.

## Task 2: staging in the image families

Files: `_fedora_customize.py` adds, beside the drgn constants,

```python
FAULTINJECT_HELPER_GUEST_PATH = "/usr/local/sbin/kdive-faultinject"
FAULTINJECT_HELPER_REPO_RELPATH = ("deploy", "remote-libvirt-guest-helpers", "kdive-faultinject")

def faultinject_helper_source() -> Path: ...
def faultinject_helper_steps() -> list[Step]: ...
```

mirroring `drgn_helper_source`/`drgn_helper_steps` (same `CategorizedError` CONFIGURATION_ERROR
when the source is not a file); `debug_image_steps` returns `drgn_helper_steps() +
faultinject_helper_steps()`; `debian.py` (inside `ctx.kind == "debug"`) and `suse.py` (inside
`"drgn" in ctx.packages`) append `faultinject_helper_steps()` after `drgn_helper_steps()`.

Verification:
- Mode: focused-test. Contract: each family stages the helper 0755 and omits it for non-debug
  images; a missing source fails loud. Tests: extend `test_debian.py` (debug stages, build omits),
  `test_suse.py` (the existing drgn-gated Tumbleweed/Leap assertions gain the helper name),
  `test_fedora_customize.py` (fail loud on a missing source), `test_rootfs_build.py`
  (rhel debug stages with the repo source path and `chmod 0755`; non-debug omits). Red: no step
  is emitted. Green: `just test-verbose tests/images/families tests/providers/local_libvirt/test_rootfs_build.py`.

Steps: write tests, see red, implement, see green, `just lint`, `just type`, commit
`feat(images): stage kdive-faultinject in debug images (#3682)`.

## Task 3: Ansible helper list, README

Files: add `- kdive-faultinject` after `kdive-drgn` in the helper list of `all.yml`; add a README
table row (operations `read`, `set`, `reset`; consumer: none yet, ADR-0822) and a short paragraph.

Verification:
- Mode: focused-test. Contract: the role copies the new helper with chown/chmod. Test:
  `test_family_selection_preserves_installed_helper_path` gains `kdive-faultinject` in its helper
  parametrization; red because `_helper_args` raises for an item missing from the list if it is
  not listed (else the parametrization passes by construction and the catalog assertion below
  is the red); a test asserts `kdive-faultinject` is in `kdive_image_defaults.helpers` and no catalog row
  overrides `helpers` without it. Write-order: a test with a recording writer asserts `set` writes
  `probability` last and `reset` first. Green:
  `just test-verbose tests/deploy/test_guest_base_image_helpers.py`.
- Mode: task-test-not-applicable. Surface: README prose; no executable consumer.

Steps: write tests, see red, edit, green, `just lint-ansible`, `just test-ansible`; the
runner-task fixture does not change (no task added); commit
`feat(deploy): install kdive-faultinject in base images (#3682)`.
