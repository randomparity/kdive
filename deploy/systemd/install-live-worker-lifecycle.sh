#!/bin/bash
set -euo pipefail

_created_source_link=""

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

cleanup() {
  [[ -z $credential_temp || ! -e $credential_temp ]] || unlink "$credential_temp"
  [[ -z $config_temp || ! -e $config_temp ]] || unlink "$config_temp"
  [[ -z $revision_temp || ! -e $revision_temp ]] || unlink "$revision_temp"
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
install -d -o root -g root -m 0755 "$state_root" /opt/kdive-live-worker-lifecycle
install -d -o root -g root -m 0711 "$state_root/slots"
install -d -o root -g root -m 0755 /run/kdive
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
chmod 0755 /opt/kdive-live-worker-lifecycle

revision_temp="$(mktemp /opt/kdive-live-worker-lifecycle/.revision.XXXXXX)"
git -C "$source_root" rev-parse HEAD >"$revision_temp"
install -o root -g root -m 0444 "$revision_temp" \
  /opt/kdive-live-worker-lifecycle/revision
unlink "$revision_temp"
revision_temp=""

systemctl daemon-reload
if ! systemctl enable --now kdive-live-worker-lifecycle.socket; then
  echo "could not enable the live-worker lifecycle socket" >&2
  systemctl status --no-pager --full kdive-live-worker-lifecycle.socket >&2 || :
  exit 1
fi
