# check-setup-deps.sh Auto-Fix + Arch Matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `scripts/check-setup-deps.sh` from a pure reporter into an opt-in fixer
(interactive prompt or `-y`) that installs missing distro packages and links the guestfs
binding into the venv, and make its cross-arch advisory show the host arch first with each
guest arch's acceleration.

**Architecture:** Additive changes to one bash script + its behavioral test. Report-only
behavior is preserved when stdin is not a TTY and `-y` is absent, so CI and every existing
test are unchanged. Three cohesive commits: (1) native-arch advisory, (2) package
auto-install, (3) guestfs symlink fix.

**Tech Stack:** Bash (`set -euo pipefail`, `local -n` namerefs), pytest behavioral tests
driving the script via `subprocess` with PATH stubs (and `pty` for the one interactive case).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-20-check-setup-deps-autofix-design.md`; ADR-0393.
- Guardrails (all must pass before each commit): `shellcheck scripts/check-setup-deps.sh`,
  `shfmt -i 2 -d scripts/check-setup-deps.sh`, `uv run ruff check tests/scripts/test_check_setup_deps.py`,
  `uv run ruff format --check …`, `uv run ty check`, `uv run python -m pytest tests/scripts/ -q`.
- Ruff line length 100. No banned prose in comments (critical/robust/comprehensive/…).
- Report-only contract preserved: **non-TTY + no `-y` ⇒ no prompt, no fix, no sudo** (an
  existing-tests invariant).
- Fixes are opt-in only: interactive `[y/N]` (default No) per tier + separate guestfs prompt,
  or `-y` to auto-accept. Manual-hint tooling (uv/rustup/just/prek) is never auto-installed.

---

### Task 1: Host-first native arch advisory

**Files:**
- Modify: `scripts/check-setup-deps.sh` (`print_cross_arch_advisory`, ~lines 237-255; add a
  `/dev/kvm` probe using `KDIVE_KVM_NODE`).
- Test: `tests/scripts/test_check_setup_deps.py`.

**Interfaces:**
- Produces: no new shell functions consumed by later tasks; the advisory gains a leading
  `Host architecture: <arch> (supported kdive provisioning arch)` line and a native
  `guest arch <host>: …` line. `KDIVE_KVM_NODE` (default `/dev/kvm`) overrides the KVM node
  for tests, matching `check-local-libvirt.sh`.

- [ ] **Step 1: Write failing tests** for the three native-line states + host-first ordering.

```python
def test_advisory_shows_host_arch_first(tmp_path: Path) -> None:
    # x86_64 host, native + foreign emulator present, /dev/kvm accessible.
    kvm = tmp_path / "kvm"; kvm.write_text("")
    result = _run_with_uname("debian", "x86_64", ("qemu-system-x86_64", "qemu-system-ppc64"),
                             tmp_path, extra_env={"KDIVE_KVM_NODE": str(kvm)})
    out = result.stdout
    assert "Host architecture: x86_64 (supported kdive provisioning arch)" in out
    # host/native line precedes the foreign line
    assert out.index("guest arch x86_64:") < out.index("guest arch ppc64le:")
    assert "guest arch x86_64: available natively via qemu-system-x86_64 (/dev/kvm accessible" in out
    assert "guest arch ppc64le: available via TCG only (qemu-system-ppc64)" in out

def test_advisory_native_line_when_kvm_absent(tmp_path: Path) -> None:
    result = _run_with_uname("debian", "x86_64", ("qemu-system-x86_64",), tmp_path,
                             extra_env={"KDIVE_KVM_NODE": str(tmp_path / "nokvm")})
    assert "guest arch x86_64: native emulator present, /dev/kvm not accessible" in result.stdout

def test_advisory_native_line_when_qemu_absent(tmp_path: Path) -> None:
    result = _run_with_uname("debian", "x86_64", (), tmp_path)  # no native qemu stub
    assert "guest arch x86_64: not available; install qemu-system-x86 for native guests" in result.stdout
```

Note: extend `_run_with_uname` to accept `extra_env` (forward to `_run`); `_run` already
gained `extra_env` in #1328.

- [ ] **Step 2: Run to verify FAIL** — `uv run python -m pytest tests/scripts/test_check_setup_deps.py -k advisory -q` → FAIL (no host/native line today; host arch is skipped).

- [ ] **Step 3: Implement.** In `print_cross_arch_advisory`, add a `KVM_NODE="${KDIVE_KVM_NODE:-/dev/kvm}"` read (top of script alongside other readonly vars), print the host-arch header for the supported case, and emit the native line for `host` before the foreign loop:

```bash
# native line for the host arch (KVM if the emulator + /dev/kvm are present)
native="$(qemu_binary_for_arch "${host}")"
if command_exists "${native}"; then
  if [[ -r "${KVM_NODE}" && -w "${KVM_NODE}" ]]; then
    printf "  guest arch %s: available natively via %s (/dev/kvm accessible — KVM)\n" "${host}" "${native}"
  else
    printf "  guest arch %s: native emulator present, /dev/kvm not accessible — runs under TCG until KVM is enabled\n" "${host}"
  fi
else
  printf "  guest arch %s: not available; install %s for native guests\n" "${host}" "$(package_for "${native}" "${distro}")"
fi
```

Keep the existing foreign loop wording byte-for-byte. Add the header line
`printf "\nHost architecture: %s (supported kdive provisioning arch)\n" "${host}"` at the
top of the supported branch (before the loop).

- [ ] **Step 4: Run to verify PASS** — the `-k advisory` tests pass; run the whole file to
  confirm existing advisory tests (foreign lines) are unchanged.

- [ ] **Step 5: Guardrails + commit** (`shellcheck`, `shfmt -i 2 -d`, ruff, ty, full scripts tests).

```bash
git add scripts/check-setup-deps.sh tests/scripts/test_check_setup_deps.py
git commit -m "feat(scripts): show host arch first + native acceleration in advisory"
```

---

### Task 2: Opt-in package auto-install (`-y` / interactive, per tier)

**Files:**
- Modify: `scripts/check-setup-deps.sh` — arg parse (`-y`/`--yes`), a `maybe_fix_tier` helper,
  distro→(refresh, install) command mapping, mode-scoped sudo, guarded run, re-probe+rebuild.
- Test: `tests/scripts/test_check_setup_deps.py`.

**Interfaces:**
- Consumes: existing per-tier accumulators (`required_commands`/`_packages`, etc.).
- Produces: `ASSUME_YES` (0/1 from `-y`), `is_interactive` (`[ -t 0 ]`), `install_cmd_for
  <distro>` (echoes refresh+install template), `run_tier_install <tier> <distro>`. Header
  comment updated to state the new opt-in-remediation contract (drop "never installs, never
  escalates"; cite ADR-0393). Reused by Task 3 for the post-fix re-probe/rebuild routine
  `reverify_and_rebuild`.

- [ ] **Step 1: Write failing tests** — the report-only contract, `-y` install with refresh +
  non-interactive flags + `sudo -n`, sudo-preflight-fail skip, install-failure handling,
  re-verify exit 0, manual-hint safety, and interactive plain-sudo (pty).

```python
def _bin(tmp_path):  # helper: bindir with uv+pkg-config present so Required is otherwise satisfiable
    b = tmp_path / "bin"; b.mkdir()
    _stub(b, "uv", "#!/bin/sh\nexit 0\n"); _stub(b, "pkg-config", "#!/bin/sh\nexit 0\n")
    return b

def test_non_tty_without_yes_stays_report_only(tmp_path):
    """No -y and piped stdin => no install command ever runs (report-only contract)."""
    b = _bin(tmp_path); log = tmp_path / "apt.log"
    _stub(b, "apt-get", f'echo "$@" >> "{log}"\nexit 0'); _stub(b, "sudo", f'echo "$@" >> "{log}"\nexit 0')
    _run("debian", str(b), tmp_path)  # missing recommended/future deps, but no fix offered
    assert not log.exists()

def test_yes_installs_with_refresh_and_noninteractive_flag_and_sudo_n(tmp_path):
    b = _bin(tmp_path); log = tmp_path / "cmd.log"
    _stub(b, "apt-get", f'echo "apt-get $@" >> "{log}"\nexit 0')
    _stub(b, "sudo", f'echo "sudo $@" >> "{log}"\nexec "$@"')  # sudo -n true preflight + passthrough
    _run("debian", str(b), tmp_path, extra_env={}, args=["-y"])  # _run must accept args
    logged = log.read_text()
    assert "apt-get update" in logged
    assert "apt-get install -y" in logged
    assert "sudo -n" in logged  # non-root path uses sudo -n under -y

def test_yes_sudo_preflight_failure_skips_with_message_no_hang(tmp_path):
    b = _bin(tmp_path); log = tmp_path / "cmd.log"
    _stub(b, "apt-get", f'echo installed >> "{log}"\nexit 0')
    _stub(b, "sudo", 'exit 1')  # sudo -n true fails (no NOPASSWD)
    r = _run("debian", str(b), tmp_path, args=["-y"])
    assert "passwordless sudo" in r.stderr
    assert not log.exists()  # install never attempted

def test_yes_install_failure_reported_not_fatal(tmp_path):
    b = _bin(tmp_path)
    _stub(b, "apt-get", 'exit 100'); _stub(b, "sudo", 'exec "$@"')
    r = _run("debian", str(b), tmp_path, args=["-y"])
    assert "failed to install" in r.stderr  # reported
    # script did not abort mid-run: the advisory (later output) still printed
    assert "guest arch" in r.stdout

def test_manual_hint_tools_not_auto_installed_under_yes(tmp_path):
    b = _bin(tmp_path); log = tmp_path / "curl.log"
    _stub(b, "curl", f'echo ran >> "{log}"\nexit 0'); _stub(b, "sudo", 'exec "$@"')
    _stub(b, "apt-get", 'exit 0')
    _run("debian", str(b), tmp_path, args=["-y"])
    assert not log.exists()  # uv/rustup/just/prek curl|sh never executed
```

Interactive (pty) test:

```python
import os, pty
def test_interactive_accept_uses_plain_sudo(tmp_path):
    """A TTY operator who answers 'y' gets plain sudo (password allowed), not sudo -n."""
    b = _bin(tmp_path); log = tmp_path / "cmd.log"
    _stub(b, "apt-get", f'echo installed >> "{log}"\nexit 0')
    _stub(b, "sudo", f'echo "sudo $@" >> "{log}"\nexec "$@"')
    os_release = tmp_path / "os-release"; os_release.write_text("ID=debian\n")
    env = {"PATH": str(b), "KDIVE_OS_RELEASE": str(os_release), "HOME": str(tmp_path)}
    mo, so = pty.openpty()
    proc = subprocess.Popen([BASH, str(SCRIPT)], stdin=so, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, text=True)
    os.write(mo, b"y\ny\ny\n")  # accept each tier prompt
    proc.wait(timeout=30); os.close(mo); os.close(so)
    logged = log.read_text()
    assert "sudo -n" not in logged  # interactive => plain sudo
    assert "installed" in logged
```

- [ ] **Step 2: Run to verify FAIL** (`-k "yes or interactive or report_only or manual_hint"`).
  Expected FAIL: no `-y` handling / install logic yet. Also confirm the pre-existing
  `test_all_missing_emits_required_hint_per_distro` etc. still pass unchanged (report-only path).

- [ ] **Step 3: Implement.** Add near the top:

```bash
ASSUME_YES=0
while (($#)); do
  case "$1" in
  -y | --yes) ASSUME_YES=1 ;;
  -h | --help) printf "usage: check-setup-deps.sh [-y|--yes]\n"; exit 0 ;;
  *) printf "unknown argument: %s\n" "$1" >&2; exit 2 ;;
  esac
  shift
done
readonly ASSUME_YES
```

Distro command mapping (refresh + install, from the spec table):

```bash
# echoes: "<refresh-or-:> ;; <install…>" — caller splits on the ';;' sentinel
install_plan_for() { # distro pkgs...
  local distro="$1"; shift
  case "${distro}" in
  debian)  printf 'apt-get update ;; apt-get install -y %s' "$*" ;;
  fedora)  printf ': ;; dnf install -y %s' "$*" ;;
  arch)    printf ': ;; pacman -S --noconfirm %s' "$*" ;;
  opensuse) printf ': ;; zypper --non-interactive install %s' "$*" ;;
  *) printf '' ;;  # unknown: no auto-install
  esac
}
```

Fix decision + escalation (per tier), honoring the spec's mode split and routable messages:

```bash
# returns 0 if the caller should run fixes for this tier
offer_accepted() { # prompt
  ((ASSUME_YES)) && return 0
  [[ -t 0 ]] || return 1            # non-tty + no -y => report-only
  local ans; printf "%s [y/N] " "$1" >&2; read -r ans; [[ "${ans}" == [yY]* ]]
}

# runs "$@" with the right sudo flavor; returns nonzero on escalation-preflight failure (77)
run_privileged() { # cmd...
  if ((EUID == 0)); then "$@"; return; fi
  command_exists sudo || { printf "  need root: sudo not found — run as root to: %s\n" "$*" >&2; return 77; }
  if ((ASSUME_YES)); then
    sudo -n true 2>/dev/null || { printf "  re-run as root or with passwordless sudo to install: %s\n" "$*" >&2; return 77; }
    sudo -n "$@"
  else
    sudo -v || { printf "  sudo authentication failed; re-run as root to install: %s\n" "$*" >&2; return 77; }
    sudo "$@"
  fi
}
```

Then, after each `report_tier`, run the install if accepted (guarded so `set -e` never aborts):

```bash
maybe_install_tier() { # tier distro
  local tier="$1" distro="$2"
  local -n pkgs="${tier}_packages"
  ((${#pkgs[@]})) || return 0
  offer_accepted "Install ${tier} packages (${pkgs[*]})?" || return 0
  local plan refresh install
  plan="$(install_plan_for "${distro}" "${pkgs[@]}")"
  [[ -n "${plan}" ]] || { printf "  no auto-install for this distro; install manually: %s\n" "${pkgs[*]}" >&2; return 0; }
  refresh="${plan%% ;; *}"; install="${plan#* ;; }"
  if ! run_privileged bash -c "${refresh}"; then return 0; fi   # refresh guarded, non-fatal
  if ! run_privileged bash -c "${install}"; then
    printf "  package set failed to install: %s\n" "${pkgs[*]}" >&2
  fi
}
```

Wire calls right after the three `report_tier` lines (`maybe_install_tier required "${distro}"`
etc.). Update the header comment to the ADR-0393 contract. Then add `reverify_and_rebuild`
(see Task 3 interface) that re-runs the tier probes after `hash -r`, rebuilds the accumulators,
and recomputes the exit condition; call it before the terminal summary block so the trailer /
`manual_hints` / "present" line and exit code all reflect post-fix state.

> The re-probe reruns the same probe calls the top-of-script setup does. Factor the probe
> sequence into a `probe_all` function (called once at startup and again by
> `reverify_and_rebuild` after `hash -r`), resetting the accumulator arrays first.

- [ ] **Step 4: Run to verify PASS** — the new tests pass; all pre-existing tests pass (the
  report-only path is unchanged because `offer_accepted` returns 1 for non-tty + no `-y`).

- [ ] **Step 5: Guardrails + commit.**

```bash
git add scripts/check-setup-deps.sh tests/scripts/test_check_setup_deps.py
git commit -m "feat(scripts): opt-in package install (-y/interactive, sudo, per tier)"
```

---

### Task 3: Opt-in guestfs venv symlink (separate prompt, three-state)

**Files:**
- Modify: `scripts/check-setup-deps.sh` — replace the guestfs report block (added in #1328,
  `if ! { command_exists "${PY}" && … import guestfs …; }`) with a three-state
  `probe_guestfs` (package-present vs venv-import) + a `maybe_link_guestfs` fix; ensure the
  post-symlink re-probe feeds `reverify_and_rebuild`.
- Test: `tests/scripts/test_check_setup_deps.py`.

**Interfaces:**
- Consumes: `${PY}` (venv interpreter, resolved in #1328), `offer_accepted`, `probe_all` /
  `reverify_and_rebuild` (Task 2).
- Produces: `guestfs_state` = `absent` | `unlinked` | `ok` keyed on package presence +
  venv import; `link_guestfs_into_venv` (idempotent `ln -sf`, ABI-checked).

- [ ] **Step 1: Write failing tests** — three-state keying, venv identity, ABI mismatch,
  idempotency, ordering, glob-miss.

```python
def _sys_site(tmp_path, *, present):  # a fake system dist-packages dir
    d = tmp_path / "dist-packages"; d.mkdir()
    if present:
        (d / "guestfs.py").write_text(""); (d / "libguestfsmod.cpython-314.so").write_text("")
    return d

def test_installed_but_unlinked_shows_symlink_not_install_hint(tmp_path):
    """Package present system-wide, venv can't import => symlink remedy, NOT an install hint."""
    b = _bin(tmp_path)
    venv_py = _stub_python(b, "venv-python", imports_ok=False)   # venv can't import guestfs
    # KDIVE_GUESTFS_SYS_SITE points probe at a dir that HAS the binding (package present)
    sys_site = _sys_site(tmp_path, present=True)
    r = _run("debian", str(b), tmp_path,
             extra_env={"KDIVE_PYTHON": str(venv_py), "KDIVE_GUESTFS_SYS_SITE": str(sys_site)})
    assert "python3-guestfs" in r.stderr        # still surfaced
    assert "apt install python3-guestfs" not in r.stderr  # NOT as a missing-package install
    assert "symlink" in r.stderr                # the real remedy

def test_guestfs_skips_when_py_is_not_a_venv(tmp_path):
    """A system (non-venv) ${PY} must never be symlinked into."""
    b = _bin(tmp_path)
    # a python stub reporting sys.prefix == base_prefix (not a venv) and failing import
    sysython = b / "sys-python"
    sysython.write_text('#!/bin/sh\ncase "$*" in *base_prefix*) exit 1;; *) exit 1;; esac\n')
    sysython.chmod(0o755)
    sys_site = _sys_site(tmp_path, present=True); link_target = tmp_path / "should-not-appear"
    r = _run("debian", str(b), tmp_path, args=["-y"],
             extra_env={"KDIVE_PYTHON": str(sysython), "KDIVE_GUESTFS_SYS_SITE": str(sys_site)})
    assert "skip" in r.stderr.lower()  # Fix 2 skipped; no symlink attempted

def test_yes_links_guestfs_when_package_present_and_venv_ok_abi(tmp_path):
    """-y with matching ABI creates the symlink into the venv site-packages."""
    # venv-python: reports it IS a venv, matching minor, and site path under tmp
    site = tmp_path / "venv-site"; site.mkdir()
    venv_py = _venv_python_stub(tmp_path, site=site, minor="3.14", is_venv=True)
    sys_site = _sys_site(tmp_path, present=True)
    b = _bin(tmp_path)
    r = _run("debian", str(b), tmp_path, args=["-y"],
             extra_env={"KDIVE_PYTHON": str(venv_py), "KDIVE_GUESTFS_SYS_SITE": str(sys_site),
                        "KDIVE_SYSTEM_PY_MINOR": "3.14"})
    assert (site / "guestfs.py").is_symlink()
    assert list(site.glob("libguestfsmod*.so"))

def test_abi_mismatch_fails_loud_no_symlink(tmp_path):
    site = tmp_path / "venv-site"; site.mkdir()
    venv_py = _venv_python_stub(tmp_path, site=site, minor="3.14", is_venv=True)
    sys_site = _sys_site(tmp_path, present=True); b = _bin(tmp_path)
    r = _run("debian", str(b), tmp_path, args=["-y"],
             extra_env={"KDIVE_PYTHON": str(venv_py), "KDIVE_GUESTFS_SYS_SITE": str(sys_site),
                        "KDIVE_SYSTEM_PY_MINOR": "3.12"})
    assert "3.12" in r.stderr and "3.14" in r.stderr  # both versions reported
    assert not (site / "guestfs.py").exists()          # no broken link

def test_link_is_idempotent(tmp_path):
    # pre-create a correct link, run -y again => no abort, still linked
    ...  # ln -sf / skip-if-correct; assert returncode 0 and link present
```

> `_venv_python_stub` writes a `#!/bin/sh` that answers the script's probes: the venv-identity
> check (`sys.prefix != base_prefix` → exit 0), the site path (`sysconfig.get_path purelib` →
> echo `${site}`), and the minor version (`echo 3.14`). Introduce `KDIVE_GUESTFS_SYS_SITE` and
> `KDIVE_SYSTEM_PY_MINOR` test overrides mirroring `KDIVE_KVM_NODE` (documented as test seams).

- [ ] **Step 2: Run to verify FAIL** (`-k guestfs or abi or link or unlinked`).

- [ ] **Step 3: Implement.** Replace the #1328 guestfs block with:

```bash
# system binding dir: dist-packages (Debian) then the owning interpreter's purelib (runbook §4b)
guestfs_sys_site() {
  local d="${KDIVE_GUESTFS_SYS_SITE:-/usr/lib/python3/dist-packages}"
  [[ -e "${d}/guestfs.py" ]] || d="$(/usr/bin/python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))' 2>/dev/null || true)"
  printf "%s" "${d}"
}
probe_guestfs() {  # sets GUESTFS_STATE=absent|unlinked|ok
  local site; site="$(guestfs_sys_site)"
  if [[ ! -e "${site}/guestfs.py" ]]; then GUESTFS_STATE=absent; return; fi
  if command_exists "${PY}" && "${PY}" -c "import guestfs" 2>/dev/null; then GUESTFS_STATE=ok
  else GUESTFS_STATE=unlinked; fi
}
```

Future-tier reporting keys on `GUESTFS_STATE`: `absent` → `note_package future python3-guestfs …`
(the install hint); `unlinked` → the symlink `manual_hints` line (no package install hint);
`ok` → nothing. Fix (separate prompt, after Task 2's Future install re-probe):

```bash
maybe_link_guestfs() { # distro
  [[ "${GUESTFS_STATE}" == unlinked ]] || return 0
  # ${PY} must be a real venv, else skip (never symlink into system python3)
  "${PY}" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)' 2>/dev/null \
    || { printf "  guestfs: %s is not a venv — skip (symlink only into an isolated venv)\n" "${PY}" >&2; return 0; }
  offer_accepted "Symlink the libguestfs binding into the venv?" || return 0
  local site sys_site vmin smin
  site="$("${PY}" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
  sys_site="$(guestfs_sys_site)"
  vmin="$("${PY}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  smin="${KDIVE_SYSTEM_PY_MINOR:-$(/usr/bin/python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')}"
  if [[ "${vmin}" != "${smin}" ]]; then
    printf "  guestfs ABI mismatch: system python %s vs venv %s — not linking\n" "${smin}" "${vmin}" >&2; return 0
  fi
  ln -sf "${sys_site}/guestfs.py" "${site}/" 2>/dev/null || true
  local so; for so in "${sys_site}"/libguestfsmod*.so; do [[ -e "${so}" ]] && ln -sf "${so}" "${site}/"; done
}
```

Call `probe_guestfs` inside `probe_all` (so the re-probe after the symlink refreshes
`GUESTFS_STATE`), and `maybe_link_guestfs "${distro}"` after the Future-tier install.

- [ ] **Step 4: Run to verify PASS** — new tests pass; full scripts test file green.

- [ ] **Step 5: Guardrails + commit.**

```bash
git add scripts/check-setup-deps.sh tests/scripts/test_check_setup_deps.py
git commit -m "feat(scripts): opt-in guestfs venv symlink (three-state, ABI-checked)"
```

---

## Rollback / cleanup

Each task is an independent commit; revert a task's commit to back it out. No migrations, no
persistent state. The script remains report-only for all existing non-interactive callers, so
a partial landing (Task 1 only, or Tasks 1-2) is safe to ship.

## Self-review notes

- Spec coverage: Task 1 = arch matrix; Task 2 = `-y`/interactive install, sudo modes, refresh,
  guard, manual-hint safety, re-verify/rebuild; Task 3 = three-state guestfs, venv identity,
  ABI, idempotency, ordering. Non-TTY report-only contract enforced by `offer_accepted`.
- Test seams introduced (documented in the script header): `KDIVE_KVM_NODE`,
  `KDIVE_GUESTFS_SYS_SITE`, `KDIVE_SYSTEM_PY_MINOR` — env overrides mirroring the existing
  `KDIVE_OS_RELEASE`/`KDIVE_PYTHON` pattern (not production-only branches).
- Interactive path covered via `pty`; automation path via `-y`; report-only via non-tty default.
