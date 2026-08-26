#!/bin/bash
set -euo pipefail

_created_source_link=""
_libvirt_family=""
_libvirt_daemon=""
_libvirt_executable=""
_libvirt_config=""
_libvirt_socket=""
_libvirt_pid=""
_libvirt_uri=""
_libvirt_tuple_action=""
_libvirt_tuple_pid=""
_libvirt_tuple_pid_live="false"
_libvirt_tuple_process_uid=""
_libvirt_tuple_process_name=""
_libvirt_tuple_socket_state="absent"
_libvirt_tuple_pid_identity=""
_libvirt_tuple_socket_identity=""
_libvirt_runtime_root=""
_libvirt_runtime_child=""
_libvirt_runtime_operator_uid=""
_libvirt_runtime_group_gid=""
_libvirt_runtime_root_locked="false"
_libvirt_runtime_child_locked="false"

_fixture_files=(
  manifest.yaml
  rootfs_catalog.toml
  profiles/console-ready_ppc64le.yaml
  profiles/console-ready_x86_64.yaml
)

_link_system_guestfs_binding() (
  local venv_python="$1" system_site venv_site source
  local -a native_modules sources
  system_site="$(
    /usr/bin/python3 -c \
      'import guestfs, pathlib; print(pathlib.Path(guestfs.__file__).resolve().parent)'
  )" || {
    echo "system Python cannot import the required guestfs binding" >&2
    return 1
  }
  venv_site="$(
    "$venv_python" -c 'import sysconfig; print(sysconfig.get_path("purelib"))'
  )"
  [[ -d $venv_site && ! -L $venv_site ]] || {
    echo "lifecycle worker venv site-packages is not a real directory: $venv_site" >&2
    return 1
  }
  shopt -s nullglob
  native_modules=("$system_site"/libguestfsmod*.so)
  ((${#native_modules[@]} > 0)) || {
    echo "system guestfs native module is absent from $system_site" >&2
    return 1
  }
  sources=("$system_site/guestfs.py" "${native_modules[@]}")
  for source in "${sources[@]}"; do
    [[ -f $source ]] || {
      echo "system guestfs binding file is absent: $source" >&2
      return 1
    }
    ln -sfnT -- "$source" "$venv_site/$(basename -- "$source")"
  done
  "$venv_python" -c 'import guestfs' || {
    echo "lifecycle worker venv cannot import the linked guestfs binding" >&2
    return 1
  }
)

_require_real_directory() {
  local path="$1" owner="$2" group="$3" mode="$4"
  if [[ -e $path || -L $path ]]; then
    [[ -d $path && ! -L $path ]] || {
      echo "fixture directory must be a real directory: $path" >&2
      return 1
    }
  else
    install -d -o "$owner" -g "$group" -m "$mode" -- "$path"
  fi
  chown -h "$owner:$group" -- "$path"
  chmod "$mode" -- "$path"
}

_fixture_inventory_is_exact() {
  local catalog="$1" entry relative
  [[ -d $catalog && ! -L $catalog ]] || return 1
  while IFS= read -r entry; do
    case "$entry" in
    d:profiles | f:manifest.yaml | f:rootfs_catalog.toml | \
      f:profiles/console-ready_ppc64le.yaml | f:profiles/console-ready_x86_64.yaml) ;;
    *) return 1 ;;
    esac
  done < <(find -P "$catalog" -mindepth 1 -printf '%y:%P\n')
  for relative in "${_fixture_files[@]}"; do
    [[ -f $catalog/$relative && ! -L $catalog/$relative ]] || return 1
  done
}

_fixture_source_is_safe() {
  local current="$1"
  while [[ $current != / ]]; do
    [[ ! -L $current && -d $current ]] || return 1
    current="$(dirname -- "$current")"
  done
}

_fixture_catalog_matches() {
  local source="$1" destination="$2" owner="$3" group="$4" relative metadata
  _fixture_inventory_is_exact "$destination" || return 1
  [[ "$(stat -c '%u:%g:%a' "$destination")" == "$owner:$group:750" ]] || return 1
  [[ "$(stat -c '%u:%g:%a' "$destination/profiles")" == "$owner:$group:750" ]] || return 1
  for relative in "${_fixture_files[@]}"; do
    cmp -s -- "$source/$relative" "$destination/$relative" || return 1
    metadata="$(stat -c '%u:%g:%a' "$destination/$relative")"
    [[ $metadata == "$owner:$group:640" ]] || return 1
  done
}

_clear_fixture_catalog() {
  local catalog="$1"
  _fixture_inventory_is_exact "$catalog" || {
    [[ -d $catalog && ! -L $catalog ]] || return 1
  }
  find -P "$catalog" -mindepth 1 -delete
}

install_fixed_fixture_catalog() (
  local source_catalog="$1" destination_catalog="$2" owner="$3" group="$4"
  local fixture_parent stage relative
  if ! _fixture_source_is_safe "$source_catalog" || ! _fixture_inventory_is_exact "$source_catalog"; then
    echo "fixed local-libvirt fixture catalog has an unsafe inventory" >&2
    return 1
  fi
  fixture_parent="$(dirname -- "$destination_catalog")"
  _require_real_directory "$fixture_parent" "$owner" "$group" 0750 || return 1
  if [[ -e $destination_catalog || -L $destination_catalog ]]; then
    [[ -d $destination_catalog && ! -L $destination_catalog ]] || {
      echo "fixture catalog destination must be a real directory" >&2
      return 1
    }
  fi
  _fixture_catalog_matches "$source_catalog" "$destination_catalog" "$owner" "$group" && exit 0
  stage="$(mktemp -d "$fixture_parent/.fixture-stage.XXXXXX")"
  trap 'find -P "$stage" -mindepth 1 -delete 2>/dev/null || :; rmdir "$stage" 2>/dev/null || :' EXIT
  install -d -m 0700 -- "$stage/profiles"
  for relative in "${_fixture_files[@]}"; do
    cp --no-dereference -- "$source_catalog/$relative" "$stage/$relative"
  done
  _fixture_inventory_is_exact "$stage" || return 1
  _require_real_directory "$destination_catalog" "$owner" "$group" 0750 || return 1
  _clear_fixture_catalog "$destination_catalog" || return 1
  install -d -o "$owner" -g "$group" -m 0750 -- "$destination_catalog/profiles"
  for relative in "${_fixture_files[@]}"; do
    install -o "$owner" -g "$group" -m 0640 -- "$stage/$relative" "$destination_catalog/$relative"
  done
)

_libvirt_tuple_error() {
  local reason="$1" socket_path="$2" pid_path="$3"
  echo "contradictory selected libvirt tuple: $reason; $socket_path and $pid_path left untouched." \
    "Recovery: inspect those exact paths and their owning process, correct the evidence, then rerun." >&2
  return 1
}

_libvirt_cleanup_error() {
  local reason="$1" socket_path="$2" pid_path="$3"
  echo "selected libvirt stale cleanup failed: $reason; start is blocked." \
    "Recovery: inspect $socket_path and $pid_path, correct the exact residue, then rerun." >&2
  return 1
}

_restore_libvirt_runtime() {
  local restore_status=0 authority expected_authority

  expected_authority="$_libvirt_runtime_operator_uid:$_libvirt_runtime_group_gid:750"

  if [[ $_libvirt_runtime_child_locked == true ]]; then
    if ! chown -h "$_libvirt_runtime_operator_uid:$_libvirt_runtime_group_gid" \
      "$_libvirt_runtime_child" || ! chmod 0750 "$_libvirt_runtime_child"; then
      echo "could not restore operator ownership on $_libvirt_runtime_child" >&2
      restore_status=1
    else
      authority="$(stat -c '%u:%g:%a' "$_libvirt_runtime_child" 2>/dev/null || :)"
      [[ $authority == "$expected_authority" ]] || restore_status=1
    fi
  fi
  if [[ $_libvirt_runtime_root_locked == true ]]; then
    if ! chown -h "$_libvirt_runtime_operator_uid:$_libvirt_runtime_group_gid" \
      "$_libvirt_runtime_root" || ! chmod 0750 "$_libvirt_runtime_root"; then
      echo "could not restore operator ownership on $_libvirt_runtime_root" >&2
      restore_status=1
    else
      authority="$(stat -c '%u:%g:%a' "$_libvirt_runtime_root" 2>/dev/null || :)"
      [[ $authority == "$expected_authority" ]] || restore_status=1
    fi
  fi
  if [[ $restore_status -eq 0 ]]; then
    _libvirt_runtime_child_locked="false"
    _libvirt_runtime_root_locked="false"
  fi
  return "$restore_status"
}

_lock_libvirt_runtime() {
  local runtime_parent="$1" runtime_root="$2" operator_uid="$3" group_gid="$4"
  local lock_uid="$5" lock_gid="${6:-$5}"

  _libvirt_runtime_root="$runtime_root"
  _libvirt_runtime_child="$runtime_root/libvirt"
  _libvirt_runtime_operator_uid="$operator_uid"
  _libvirt_runtime_group_gid="$group_gid"
  _libvirt_runtime_root_locked="false"
  _libvirt_runtime_child_locked="false"
  if [[ -L $runtime_parent || (-e $runtime_parent && ! -d $runtime_parent) ]]; then
    echo "libvirt runtime parent must be a real directory: $runtime_parent" >&2
    return 1
  fi
  [[ -d $runtime_parent ]] || mkdir -- "$runtime_parent"
  chown -h "$lock_uid:$lock_gid" "$runtime_parent"
  chmod 0755 "$runtime_parent"
  if [[ -L $runtime_root || (-e $runtime_root && ! -d $runtime_root) ]]; then
    echo "libvirt runtime root must be a real directory: $runtime_root" >&2
    return 1
  fi
  [[ -d $runtime_root ]] || mkdir -- "$runtime_root"
  chown -h "$lock_uid:$group_gid" "$runtime_root"
  chmod 0750 "$runtime_root"
  _libvirt_runtime_root_locked="true"
  if [[ -L $_libvirt_runtime_child ||
    (-e $_libvirt_runtime_child && ! -d $_libvirt_runtime_child) ]]; then
    echo "libvirt runtime child must be a real directory: $_libvirt_runtime_child" >&2
    _restore_libvirt_runtime || :
    return 1
  fi
  [[ -d $_libvirt_runtime_child ]] || mkdir -- "$_libvirt_runtime_child"
  chown -h "$lock_uid:$group_gid" "$_libvirt_runtime_child"
  chmod 0750 "$_libvirt_runtime_child"
  _libvirt_runtime_child_locked="true"
}

_inspect_libvirt_pid() {
  local pid_path="$1" operator_uid="$2" socket_path="$3"
  local authority pid_mode

  [[ -e $pid_path || -L $pid_path ]] || return 0
  if [[ ! -f $pid_path || -L $pid_path ]]; then
    _libvirt_tuple_error "pid residue has wrong type" "$socket_path" "$pid_path"
    return
  fi
  authority="$(stat -c '%u:%a' "$pid_path")"
  pid_mode="${authority#*:}"
  if [[ ${authority%%:*} != "$operator_uid" ]] ||
    ((!(8#$pid_mode & 8#400) || (8#$pid_mode & 8#022))); then
    _libvirt_tuple_error "pid residue has wrong authority" "$socket_path" "$pid_path"
    return
  fi
  _libvirt_tuple_pid="$(<"$pid_path")"
  if [[ ! $_libvirt_tuple_pid =~ ^[1-9][0-9]*$ ]]; then
    _libvirt_tuple_error "pid residue is not one numeric pid" "$socket_path" "$pid_path"
    return
  fi
  _libvirt_tuple_pid_identity="$(stat -c '%d:%i:%u:%a' "$pid_path")"
  if ! kill -0 "$_libvirt_tuple_pid" 2>/dev/null; then
    return 0
  fi
  _libvirt_tuple_pid_live="true"
  if ! _libvirt_tuple_process_uid="$(stat -c '%u' "/proc/$_libvirt_tuple_pid")" ||
    ! IFS= read -r _libvirt_tuple_process_name <"/proc/$_libvirt_tuple_pid/comm"; then
    _libvirt_tuple_error "pid process changed during inspection" "$socket_path" "$pid_path"
  fi
}

_probe_libvirt_socket() {
  /usr/bin/python3 - "$1" <<'PY'
import socket
import sys

client = socket.socket(socket.AF_UNIX)
client.settimeout(0.5)
try:
    client.connect(sys.argv[1])
except ConnectionRefusedError:
    raise SystemExit(3)
except OSError:
    raise SystemExit(4)
finally:
    client.close()
PY
}

_inspect_libvirt_socket() {
  local socket_path="$1" operator_uid="$2" group_gid="$3" pid_path="$4"
  local authority probe_status

  [[ -e $socket_path || -L $socket_path ]] || return 0
  if [[ ! -S $socket_path || -L $socket_path ]]; then
    _libvirt_tuple_error "socket residue has wrong type" "$socket_path" "$pid_path"
    return
  fi
  authority="$(stat -c '%u:%g:%a' "$socket_path")"
  if [[ $authority != "$operator_uid:$group_gid:770" ]]; then
    _libvirt_tuple_error "socket residue has wrong authority" "$socket_path" "$pid_path"
    return
  fi
  _libvirt_tuple_socket_identity="$(stat -c '%d:%i:%u:%g:%a' "$socket_path")"
  if _probe_libvirt_socket "$socket_path"; then
    _libvirt_tuple_socket_state="live"
    return 0
  else
    probe_status=$?
  fi
  if [[ $probe_status -eq 3 ]]; then
    _libvirt_tuple_socket_state="stale"
    return 0
  fi
  _libvirt_tuple_error "socket listener state is indeterminate" "$socket_path" "$pid_path"
}

_verify_stale_libvirt_tuple_unchanged() {
  local socket_path="$1" pid_path="$2"
  local current_identity probe_status

  if [[ -n $_libvirt_tuple_pid_identity ]]; then
    current_identity="$(stat -c '%d:%i:%u:%a' "$pid_path" 2>/dev/null || :)"
    if kill -0 "$_libvirt_tuple_pid" 2>/dev/null ||
      [[ $current_identity != "$_libvirt_tuple_pid_identity" ]]; then
      _libvirt_tuple_error "pid residue changed during inspection" "$socket_path" "$pid_path"
      return
    fi
  fi
  if [[ -n $_libvirt_tuple_socket_identity ]]; then
    current_identity="$(stat -c '%d:%i:%u:%g:%a' "$socket_path" 2>/dev/null || :)"
    if [[ $current_identity != "$_libvirt_tuple_socket_identity" ]]; then
      _libvirt_tuple_error "socket residue changed during inspection" "$socket_path" "$pid_path"
      return
    fi
    if _probe_libvirt_socket "$socket_path"; then
      _libvirt_tuple_error "socket gained a listener during inspection" "$socket_path" "$pid_path"
      return
    else
      probe_status=$?
    fi
    if [[ $probe_status -ne 3 ]]; then
      _libvirt_tuple_error "socket state changed during inspection" "$socket_path" "$pid_path"
      return
    fi
  fi
}

_remove_stale_libvirt_tuple() {
  local socket_path="$1" pid_path="$2"

  _verify_stale_libvirt_tuple_unchanged "$socket_path" "$pid_path" || return
  if [[ -n $_libvirt_tuple_pid_identity ]] && ! unlink "$pid_path"; then
    _libvirt_cleanup_error "could not remove stale pid residue" "$socket_path" "$pid_path"
    return
  fi
  if [[ -n $_libvirt_tuple_socket_identity ]] && ! unlink "$socket_path"; then
    _libvirt_cleanup_error "could not remove stale socket residue" "$socket_path" "$pid_path"
    return
  fi
  if [[ -e $pid_path || -L $pid_path || -e $socket_path || -L $socket_path ]]; then
    _libvirt_cleanup_error "stale residue survived exact removal" "$socket_path" "$pid_path"
    return
  fi
}

_reconcile_libvirt_tuple() {
  local socket_path="$1" pid_path="$2" daemon="$3" operator_uid="$4" group_gid="$5"

  _libvirt_tuple_action=""
  _libvirt_tuple_pid=""
  _libvirt_tuple_pid_live="false"
  _libvirt_tuple_process_uid=""
  _libvirt_tuple_process_name=""
  _libvirt_tuple_socket_state="absent"
  _libvirt_tuple_pid_identity=""
  _libvirt_tuple_socket_identity=""
  _inspect_libvirt_pid "$pid_path" "$operator_uid" "$socket_path" || return
  _inspect_libvirt_socket "$socket_path" "$operator_uid" "$group_gid" "$pid_path" || return

  if [[ $_libvirt_tuple_pid_live == true ]]; then
    if [[ $_libvirt_tuple_process_uid != "$operator_uid" ||
      $_libvirt_tuple_process_name != "$daemon" || $_libvirt_tuple_socket_state != live ]]; then
      _libvirt_tuple_error "live pid, daemon identity, and socket do not agree" \
        "$socket_path" "$pid_path"
      return
    fi
    _libvirt_tuple_action="adopt"
    return 0
  fi
  if [[ $_libvirt_tuple_socket_state == live ]]; then
    _libvirt_tuple_error "socket has a listener without the exact selected pid" \
      "$socket_path" "$pid_path"
    return
  fi
  _remove_stale_libvirt_tuple "$socket_path" "$pid_path" || return
  _libvirt_tuple_action="start"
}

_select_libvirt_tuple() {
  local os_release="$1"
  local ID="" ID_LIKE="" distro_tokens

  if [[ ! -r $os_release ]]; then
    echo "cannot read distro identity from $os_release" >&2
    return 1
  fi
  # shellcheck disable=SC1090 # The caller supplies the os-release path for isolated tests.
  source "$os_release"
  distro_tokens=" ${ID:-} ${ID_LIKE:-} "
  case "$distro_tokens" in
  *" debian "* | *" ubuntu "*)
    _libvirt_family="Debian-family"
    _libvirt_daemon="libvirtd"
    _libvirt_config="libvirtd-live.conf"
    _libvirt_socket="libvirt-sock"
    _libvirt_pid="libvirtd.pid"
    _libvirt_uri="qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock"
    ;;
  *" rhel "* | *" fedora "* | *" centos "* | *" rocky "* | *" almalinux "*)
    _libvirt_family="Red Hat-family"
    _libvirt_daemon="virtqemud"
    _libvirt_config="virtqemud-live.conf"
    _libvirt_socket="virtqemud-sock"
    _libvirt_pid="virtqemud.pid"
    _libvirt_uri="qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/virtqemud-sock"
    ;;
  *)
    echo "unsupported distro family in $os_release (ID=${ID:-unset}, ID_LIKE=${ID_LIKE:-unset})" >&2
    return 1
    ;;
  esac

  if ! _libvirt_executable="$(command -v "$_libvirt_daemon")"; then
    echo "selected $_libvirt_family $_libvirt_daemon executable is missing" >&2
    return 1
  fi
}

_prepare_attested_runtime_root() {
  local runtime_root="$1" owner="$2" group="$3" parent
  parent="$(dirname -- "$runtime_root")"
  if [[ ! -d $parent || -L $parent ]]; then
    echo "lifecycle runtime parent must be a real directory: $parent" >&2
    return 1
  fi
  install -d -o "$owner" -g "$group" -m 0755 -- "$parent"
  if [[ -L $runtime_root || (-e $runtime_root && ! -d $runtime_root) ]]; then
    echo "lifecycle runtime root must be a real directory: $runtime_root" >&2
    return 1
  fi
  install -d -o "$owner" -g "$group" -m 0755 -- "$runtime_root"
}

_prepare_source_link() {
  local source_root="$1" install_link="$2"
  if [[ -L $install_link ]] &&
    [[ $(readlink -f "$install_link") == "$(readlink -f "$source_root")" ]]; then
    return
  fi
  if [[ -e $install_link || -L $install_link ]]; then
    echo "$install_link already exists and does not name --source" >&2
    return 1
  fi
  ln -s "$source_root" "$install_link"
  _created_source_link="$install_link"
}

_cleanup_source_link() {
  if [[ -n $_created_source_link && -L $_created_source_link ]]; then
    unlink "$_created_source_link"
  fi
  _created_source_link=""
}

if [[ ${BASH_SOURCE[0]} != "$0" ]]; then
  return 0
fi

usage() {
  echo "usage: $0 --operator USER --source PATH" >&2
  exit 2
}

operator=""
source_root=""
while (($#)); do
  case "$1" in
  --operator)
    [[ $# -ge 2 ]] || usage
    operator="$2"
    shift 2
    ;;
  --source)
    [[ $# -ge 2 ]] || usage
    source_root="$2"
    shift 2
    ;;
  *) usage ;;
  esac
done

[[ $EUID -eq 0 ]] || {
  echo "install-live-worker-lifecycle.sh must run as root" >&2
  exit 1
}
[[ -n $operator && -d $source_root ]] || usage
[[ -d $source_root/fixtures/local-libvirt ]] || {
  echo "fixed local-libvirt fixture catalog is missing from the installation source" >&2
  exit 1
}
operator_uid="$(id -u "$operator")"
_select_libvirt_tuple /etc/os-release
IFS= read -r witness_dsn || [[ -n $witness_dsn ]]
[[ -n $witness_dsn ]] || {
  echo "witness DSN on standard input must be non-empty" >&2
  exit 1
}

control_group="kdive-live-control"
libvirt_group="kdive-live-libvirt"
state_root="/var/lib/kdive/live-workers"
credential_temp=""
config_temp=""
revision_temp=""
uri_temp=""

cleanup() {
  if ! _restore_libvirt_runtime; then
    echo "live-libvirt runtime ownership restoration failed; inspect before retrying" >&2
  fi
  [[ -z $credential_temp || ! -e $credential_temp ]] || unlink "$credential_temp"
  [[ -z $config_temp || ! -e $config_temp ]] || unlink "$config_temp"
  [[ -z $revision_temp || ! -e $revision_temp ]] || unlink "$revision_temp"
  [[ -z $uri_temp || ! -e $uri_temp ]] || unlink "$uri_temp"
  _cleanup_source_link
}
trap cleanup EXIT

getent group "$control_group" >/dev/null || groupadd --system "$control_group"
getent group "$libvirt_group" >/dev/null || groupadd --system "$libvirt_group"
getent group kvm >/dev/null || {
  echo "the required host KVM group is missing" >&2
  exit 1
}
IFS=: read -r _ _ libvirt_group_gid _ < <(getent group "$libvirt_group")
usermod -a -G "$control_group,$libvirt_group" "$operator"

for slot in {1..8}; do
  worker="kdive-worker-${slot}"
  getent group "$worker" >/dev/null || groupadd --system "$worker"
  if ! getent passwd "$worker" >/dev/null; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
      --gid "$worker" --groups "$libvirt_group,kvm" "$worker"
  else
    usermod -G "$libvirt_group,kvm" "$worker"
  fi
done

install -d -o root -g root -m 0755 /usr/local/libexec /etc/kdive
install -d -o root -g root -m 0700 /etc/kdive/credentials
install -d -o root -g root -m 0755 "$state_root"
_prepare_attested_runtime_root /opt/kdive-live-worker-lifecycle root root
install -d -o root -g root -m 0711 "$state_root/slots"
_lock_libvirt_runtime /run/kdive /run/kdive/live-libvirt \
  "$operator_uid" "$libvirt_group_gid" 0 0
_restore_libvirt_runtime
install -d -o "$operator" -g "$libvirt_group" -m 2770 \
  /var/lib/kdive/rootfs /var/lib/kdive/console \
  /var/lib/kdive/pcap /var/lib/kdive/build /var/lib/kdive/install
_require_real_directory /var/lib/kdive 0 0 0755
_require_real_directory /var/lib/kdive/fixtures 0 "$libvirt_group_gid" 0750
install_fixed_fixture_catalog "$source_root/fixtures/local-libvirt" \
  /var/lib/kdive/fixtures/local-libvirt 0 "$libvirt_group_gid"

for slot in {1..8}; do
  install -d -o root -g "kdive-worker-${slot}" -m 0750 "$state_root/slots/${slot}"
done

install -o root -g root -m 0755 \
  "$source_root/deploy/systemd/bin/kdive-live-worker-gate" \
  /usr/local/libexec/kdive-live-worker-gate
install -o root -g root -m 0755 \
  "$source_root/deploy/systemd/bin/kdive-live-worker-lifecycle" \
  /usr/local/libexec/kdive-live-worker-lifecycle
for unit in kdive-live-worker@.service kdive-live-worker-lifecycle.socket \
  kdive-live-worker-lifecycle@.service; do
  install -o root -g root -m 0644 "$source_root/deploy/systemd/system/$unit" \
    "/etc/systemd/system/$unit"
done

install -o root -g root -m 0644 "$source_root/deploy/systemd/$_libvirt_config" \
  "/etc/kdive/$_libvirt_config"

uri_temp="$(mktemp /etc/kdive/.live-worker-libvirt.env.XXXXXX)"
printf 'KDIVE_LIBVIRT_URI=%s\n' "$_libvirt_uri" >"$uri_temp"
install -o root -g root -m 0644 "$uri_temp" /etc/kdive/live-worker-libvirt.env
unlink "$uri_temp"
uri_temp=""

credential_temp="$(mktemp /etc/kdive/credentials/.live-worker-witness.dsn.XXXXXX)"
printf '%s\n' "$witness_dsn" >"$credential_temp"
install -o root -g root -m 0600 "$credential_temp" \
  /etc/kdive/credentials/live-worker-witness.dsn
unlink "$credential_temp"
credential_temp=""
config_temp="$(mktemp /etc/kdive/.live-worker-lifecycle.conf.XXXXXX)"
printf 'KDIVE_LIVE_WORKER_OPERATOR_UID=%s\n' "$operator_uid" >"$config_temp"
printf 'KDIVE_LIVE_WORKER_STATE_ROOT=%s\n' "$state_root" >>"$config_temp"
install -o root -g root -m 0600 "$config_temp" /etc/kdive/live-worker-lifecycle.conf
unlink "$config_temp"
config_temp=""

if [[ $source_root != /opt/kdive ]]; then
  _prepare_source_link "$source_root" /opt/kdive
fi
uv venv --python /usr/bin/python3 /opt/kdive-live-worker-lifecycle/.venv
uv pip install --python /opt/kdive-live-worker-lifecycle/.venv/bin/python /opt/kdive
_link_system_guestfs_binding /opt/kdive-live-worker-lifecycle/.venv/bin/python
chown -R root:root /opt/kdive-live-worker-lifecycle
# The readiness attestation rejects any replaceable ancestor, independent of the invoking umask.
if [[ -L /opt/kdive-live-worker-lifecycle ]]; then
  echo "refusing symlinked lifecycle runtime root" >&2
  exit 1
fi
chmod -R -P go-w /opt/kdive-live-worker-lifecycle
chmod 0755 /opt/kdive-live-worker-lifecycle

revision_temp="$(mktemp /opt/kdive-live-worker-lifecycle/.revision.XXXXXX)"
git -C "$source_root" rev-parse HEAD >"$revision_temp"
install -o root -g root -m 0444 "$revision_temp" \
  /opt/kdive-live-worker-lifecycle/revision
unlink "$revision_temp"
revision_temp=""

libvirt_socket_path="/run/kdive/live-libvirt/libvirt/$_libvirt_socket"
libvirt_pid_path="/run/kdive/live-libvirt/libvirt/$_libvirt_pid"
_lock_libvirt_runtime /run/kdive /run/kdive/live-libvirt \
  "$operator_uid" "$libvirt_group_gid" 0 0
_reconcile_libvirt_tuple "$libvirt_socket_path" "$libvirt_pid_path" "$_libvirt_daemon" \
  "$operator_uid" "$libvirt_group_gid"
libvirt_tuple_action="$_libvirt_tuple_action"
_restore_libvirt_runtime
if [[ $libvirt_tuple_action == start ]]; then
  runuser -u "$operator" -- env XDG_RUNTIME_DIR=/run/kdive/live-libvirt \
    "$_libvirt_executable" --daemon --config "/etc/kdive/$_libvirt_config" \
    --pid-file "$libvirt_pid_path"
  _lock_libvirt_runtime /run/kdive /run/kdive/live-libvirt \
    "$operator_uid" "$libvirt_group_gid" 0 0
  _reconcile_libvirt_tuple "$libvirt_socket_path" "$libvirt_pid_path" "$_libvirt_daemon" \
    "$operator_uid" "$libvirt_group_gid"
  libvirt_tuple_action="$_libvirt_tuple_action"
  _restore_libvirt_runtime
  if [[ $libvirt_tuple_action != adopt ]]; then
    echo "selected $_libvirt_family $_libvirt_daemon did not produce a complete live tuple;" \
      "inspect $libvirt_pid_path and $libvirt_socket_path, then rerun" >&2
    exit 1
  fi
fi

systemctl daemon-reload
if ! systemctl enable --now kdive-live-worker-lifecycle.socket; then
  echo "could not enable the live-worker lifecycle socket" >&2
  systemctl status --no-pager --full kdive-live-worker-lifecycle.socket >&2 || :
  exit 1
fi
