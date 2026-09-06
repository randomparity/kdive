"""Closed configuration and cleanup scope for the #2151 native carrier."""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import re
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
import pytest
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kdive.mcp.dev_harness import LiveStackClient
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.skew import _fetch_version, _resolve, readyz_urls
from tests.integration.live_stack.spine import (
    build_and_upload_kernel,
    drain_job,
    mint_role_token,
    ok,
    scalar,
)

CONFIG_ENV = "KDIVE_LIVE_VM_LOCAL_AUTHORITY_CONFIG"
OPERATIONS = ("activate", "recover", "resolve-conflict", "release", "cleanup", "teardown")
_PREFIX = re.compile(r"kdive-2151-[0-9a-f]{12}-[0-9a-f]{8}")
_FIXED_WORKER_UNIT = re.compile(r"kdive-live-worker@([1-8])\.service")
_AUTHORITY_ACCOUNT = "kdive-provider-authority"
_AUTHORITY_ARTIFACT_ROOTS = (
    Path("/var/lib/kdive/provider-authority/rootfs"),
    Path("/var/lib/kdive/provider-authority/console"),
)
_IDENTITY_PYTHON = "/usr/bin/python3"
_FAULT_BARRIER_MAX_BYTES = 1024
_FAULT_BARRIER_SOCKET = Path("/run/kdive/provider-authority/proof-control/control.sock")
_JOURNAL_ROOT = Path("/var/lib/kdive/provider-authority/journal")
_JOURNAL_PROOF_MAX_BYTES = 512
_TAKEOVER_WAIT_SECONDS = 8 * 60.0

_STALE_PROVIDER_CLIENT = r"""
import asyncio
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import kdive.config as config_registry
from kdive.domain.errors import CategorizedError
from kdive.jobs.authority_sender import local_authority_sender_factory
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.local_client import local_authority_binding
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1, JournalPhase,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import secret_backend_from_env
from kdive.worker_lifecycle.worker_incarnation import worker_incarnation_credential

if len(sys.argv) != 8:
    raise SystemExit("invalid stale-provider proof subject")
(
    system_id,
    run_id,
    original_worker,
    invocation_id,
    authority_id,
    generation,
    job_id,
) = sys.argv[1:]
if not generation.isdigit():
    raise SystemExit("invalid stale-provider generation binding")
match = re.fullmatch(
    r"local-systemd:kdive-live-worker@([1-8])\.service:([0-9a-f]{32})", original_worker
)
if match is None:
    raise SystemExit("invalid stale-provider worker binding")
slot = match.group(1)

def env_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > 32768):
            raise SystemExit("unsafe retained worker environment")
        document = os.read(descriptor, 32769).decode("utf-8", "strict")
    finally:
        os.close(descriptor)
    values = {}
    allowed = {
        "KDIVE_DATABASE_URL", "KDIVE_SECRETS_ROOT",
        "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE",
        "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET",
        "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF",
        "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_CERT_REF",
        "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF",
    }
    for line in document.splitlines():
        name, separator, raw = line.partition("=")
        if name not in allowed:
            continue
        if not separator:
            raise SystemExit("invalid retained worker environment")
        parsed = shlex.split(raw, posix=True)
        if len(parsed) != 1 or name in values:
            raise SystemExit("invalid retained worker environment")
        values[name] = parsed[0]
    return values

worker_env = env_file(f"/var/lib/kdive/live-workers/slots/{slot}/worker.env")
os.environ.update(worker_env)
config_registry.load(os.environ)
async def main():
    owner = pwd.getpwnam("kdive-provider-authority").pw_uid
    journal = FileAuthorityJournal(
        Path("/var/lib/kdive/provider-authority/journal"), f"{system_id}.jsonl", owner_uid=owner
    )
    try:
        records = journal.load()
    finally:
        journal.close()
    matches = [record for record in records if (
        record.phase is JournalPhase.MUTATION_STARTED
        and str(record.authority_id) == authority_id and record.generation == int(generation)
        and str(record.system_id) == system_id and str(record.run_id) == run_id
    )]
    if len(matches) != 1:
        raise SystemExit("stale-provider proof has no exact g1 mutation-started record")
    record = matches[0]
    request = AuthorityMutationRequestV1.model_validate({
        "authority_id": record.authority_id, "generation": record.generation,
        "system_id": record.system_id, "activation_id": record.activation_id,
        "run_id": record.run_id, "plan_identity": record.plan_identity,
        "purpose": record.purpose, "provider_kind": record.provider_kind,
        "authority_instance": record.authority_instance,
        "operation_identity": record.operation_identity,
        "operation_digest": record.operation_digest, "operation": record.operation,
        "attempt_id": record.attempt_id,
        "expected_source_identity": record.expected_source_identity,
        "intended_target_identity": record.intended_target_identity,
        "recovery_objects": record.recovery_objects,
    })
    unit = f"kdive-live-worker@{slot}.service"
    status = subprocess.run(
        ["systemctl", "show", unit, "--property=MainPID", "--property=InvocationID"],
        capture_output=True, check=False, text=True, timeout=10,
    )
    fields = dict(line.split("=", 1) for line in status.stdout.splitlines() if "=" in line)
    if (status.returncode != 0 or fields.get("InvocationID") != invocation_id
            or not fields.get("MainPID", "").isdigit()):
        raise SystemExit("stale-provider worker projection is no longer active")
    projected = (Path("/proc") / fields["MainPID"] / "environ").read_bytes().split(b"\0")
    directories = [item.split(b"=", 1)[1].decode("utf-8", "strict") for item in projected
                   if item.startswith(b"CREDENTIALS_DIRECTORY=")]
    if len(directories) != 1:
        raise SystemExit("stale-provider worker credential projection is ambiguous")
    credential_path = Path(directories[0]) / "worker-incarnation"
    credential = worker_incarnation_credential(credential_path)
    registry = SecretRegistry()
    backend = secret_backend_from_env(registry=registry)
    binding = local_authority_binding()
    sender = local_authority_sender_factory(backend, lambda: credential, binding=binding)
    if sender is None:
        raise SystemExit("stale-provider proof has no installed local sender")
    try:
        await sender.execute_mutation(request, deadline=asyncio.get_running_loop().time() + 10.0)
    except CategorizedError as exc:
        if str(exc) != "authority: superseded":
            raise
    else:
        raise SystemExit("stale provider request was accepted")
    print("superseded")

asyncio.run(main())
"""

_WORKER_HOLD_CLIENT = """
import json
import os
import re
import signal
import stat
import subprocess
import sys

if len(sys.argv) != 3 or sys.argv[2] not in {"stop", "continue"}:
    raise SystemExit("invalid native worker hold request")
incarnation, action = sys.argv[1:]
if re.fullmatch(
    r"local-systemd:kdive-live-worker@[1-8]\\.service:[0-9a-f]{32}", incarnation
) is None:
    raise SystemExit("invalid native worker incarnation")
matches = []
for slot in range(1, 9):
    path = f"/var/lib/kdive/live-workers/slots/{slot}/state.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_size > 4096):
                raise SystemExit("unsafe native worker state")
            data = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        state = json.loads(data)
    except FileNotFoundError:
        continue
    if not isinstance(state, dict) or state.get("incarnation") != incarnation:
        continue
    unit = f"kdive-live-worker@{slot}.service"
    if (state.get("unit") != unit or state.get("phase") != "started"
            or not isinstance(state.get("invocation_id"), str)
            or not isinstance(state.get("boot_id"), str)):
        raise SystemExit("native worker state does not bind its live slot")
    matches.append((unit, state))
if len(matches) != 1:
    raise SystemExit("native worker incarnation is not uniquely retained")
unit, state = matches[0]
status = subprocess.run(
    ["systemctl", "show", unit, "--property=MainPID", "--property=InvocationID"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=10,
)
fields = {}
for line in status.stdout.decode("ascii", "strict").splitlines():
    name, separator, value = line.partition("=")
    if not separator or name in fields:
        raise SystemExit("native worker unit status is malformed")
    fields[name] = value
boot_id = open("/proc/sys/kernel/random/boot_id", encoding="ascii").read().strip()
if (status.returncode != 0 or set(fields) != {"MainPID", "InvocationID"}
        or not fields["MainPID"].isdigit() or int(fields["MainPID"]) <= 1
        or fields["InvocationID"] != state["invocation_id"] or boot_id != state["boot_id"]):
    raise SystemExit("native worker is no longer the retained invocation")
pidfd = os.pidfd_open(int(fields["MainPID"]), 0)
try:
    recheck = subprocess.run(
        ["systemctl", "show", unit, "--property=MainPID", "--property=InvocationID"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=10,
    )
    rechecked = {}
    for line in recheck.stdout.decode("ascii", "strict").splitlines():
        name, separator, value = line.partition("=")
        if not separator or name in rechecked:
            raise SystemExit("native worker invocation recheck is malformed")
        rechecked[name] = value
    if recheck.returncode != 0 or rechecked != fields:
        raise SystemExit("native worker invocation changed after pidfd acquisition")
    signal.pidfd_send_signal(pidfd, signal.SIGSTOP if action == "stop" else signal.SIGCONT)
finally:
    os.close(pidfd)
if not os.path.exists("/proc/" + fields["MainPID"]):
    raise SystemExit("native worker has no main process")
print(json.dumps({"state": action + "ped", "invocation_id": state["invocation_id"]}))
"""

_INSTALLED_ROUTE_PREFLIGHT = """
import os
import shlex
import stat
import sys
from pathlib import Path

if len(sys.argv) < 2:
    raise SystemExit("invalid installed route preflight")
source_root = Path("/opt/kdive")
python = source_root / ".venv/bin/python"
slots = [int(slot) for slot in sys.argv[1:]]
if not slots or sorted(set(slots)) != slots or any(slot not in range(1, 9) for slot in slots):
    raise SystemExit("invalid installed worker slots")
source = source_root.stat()
if (not stat.S_ISDIR(source.st_mode) or source.st_uid <= 0 or source.st_mode & 0o022
        or not (source_root / "pyproject.toml").is_file()
        or not os.access(python, os.X_OK)):
    raise SystemExit("unsafe installed route preflight target")
source_root = source_root.resolve(strict=True)
resolved_python = python.resolve(strict=True)
owner_uid = source.st_uid

def read_regular(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_size > maximum):
            raise SystemExit("unsafe installed route input")
        data = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if len(data) > maximum:
        raise SystemExit("oversized installed route input")
    return data

def env_file(path, names):
    values = {}
    expected = {name.encode("ascii") for name in names}
    for line in read_regular(path, 32768).splitlines():
        name, separator, raw = line.partition(b"=")
        if name not in expected:
            continue
        parsed = shlex.split(raw.decode("utf-8", "strict"), posix=True) if separator else []
        decoded = name.decode("ascii")
        if decoded in values or len(parsed) != 1:
            raise SystemExit("unsafe installed route environment")
        values[decoded] = parsed[0]
    return values

def proc_environment(pid, names):
    values = {}
    expected = {name.encode("ascii") for name in names}
    for entry in read_regular("/proc/" + pid + "/environ", 131072).split(b"\\0"):
        if not entry:
            continue
        name, separator, value = entry.partition(b"=")
        if name not in expected:
            continue
        if not separator:
            raise SystemExit("unsafe server environment")
        decoded = name.decode("ascii")
        if decoded in values:
            raise SystemExit("unsafe server environment")
        values[decoded] = value.decode("utf-8", "strict")
    return values

candidates = []
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    try:
        if entry.stat().st_uid != owner_uid:
            continue
        command = read_regular(str(entry / "cmdline"), 4096).split(b"\\0")[:-1]
        if command != [str(python).encode(), b"-m", b"kdive", b"server"]:
            continue
        if (entry / "cwd").resolve() != source_root or (entry / "exe").resolve() != resolved_python:
            continue
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        continue
    candidates.append(entry.name)
if len(candidates) != 1:
    raise SystemExit("installed stack must have one exact server process")
required_server = {
    "KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE",
    "KDIVE_EXTERNAL_BOOT_AUTHORITY_STORE_IDENTITY",
    "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_RESERVE_BYTES",
    "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_MAX_BYTES",
    "KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES",
}
server = proc_environment(candidates[0], required_server)
if any(not server.get(name) for name in required_server):
    raise SystemExit("installed server authority route is incomplete")
try:
    reserve = int(server["KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_RESERVE_BYTES"])
    maximum = int(server["KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_MAX_BYTES"])
    server_capacity = int(server["KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES"])
except ValueError as exc:
    raise SystemExit("installed server authority geometry is invalid") from exc
if (min(reserve, maximum, server_capacity) <= 0 or reserve > maximum
        or not all(str(value) == server[name] for value, name in (
            (reserve, "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_RESERVE_BYTES"),
            (maximum, "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_MAX_BYTES"),
            (server_capacity, "KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES"),
        ))):
    raise SystemExit("installed server authority geometry is invalid")
authority = env_file(
    "/etc/kdive/provider-authority.env",
    {"KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE", "KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES"},
)
if (authority.get("KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE")
        != server["KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE"]):
    raise SystemExit("installed authority instance does not match server")
try:
    authority_capacity = int(authority["KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES"])
except (KeyError, ValueError) as exc:
    raise SystemExit("installed authority materialization capacity is invalid") from exc
if authority_capacity != server_capacity or reserve != authority_capacity:
    raise SystemExit("installed authority materialization capacity does not match server")
route_names = (
    "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE",
    "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET",
    "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF",
    "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_CERT_REF",
    "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF",
)
expected = None
for slot in slots:
    worker = env_file(f"/var/lib/kdive/live-workers/slots/{slot}/worker.env", route_names)
    route = tuple(worker.get(name) for name in route_names)
    if any(not value for value in route):
        raise SystemExit("installed worker authority route is incomplete")
    if route[0] != server["KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE"]:
        raise SystemExit("installed worker authority instance does not match server")
    if route[1] != "/run/kdive/provider-authority/request/authority.sock":
        raise SystemExit("installed worker authority socket is not fixed")
    if expected is None:
        expected = route
    elif route != expected:
        raise SystemExit("installed worker authority references do not match active fleet")
print("route-ok")
"""

_JOURNAL_INVENTORY_FAILURE = """
import subprocess

unit = "kdive-external-boot-authority.service"
state = subprocess.run(
    ["systemctl", "show", unit, "--property=InvocationID", "--property=Result"],
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    check=False,
    timeout=10,
)
values = {}
for line in state.stdout.decode("ascii", "strict").splitlines():
    name, separator, value = line.partition("=")
    if not separator or name in values:
        raise SystemExit("authority startup has malformed failed invocation evidence")
    values[name] = value
if (state.returncode != 0 or set(values) != {"InvocationID", "Result"}
        or not values["InvocationID"] or values["Result"] != "exit-code"):
    raise SystemExit("authority startup has no failed invocation evidence")
result = subprocess.run(
    ["journalctl", "-b", "--no-pager", "-o", "cat",
     "_SYSTEMD_INVOCATION_ID=" + values["InvocationID"]],
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    check=False,
    timeout=10,
)
if b"journal: inventory-mismatch" not in result.stdout:
    raise SystemExit("authority startup did not reject the missing lane as inventory-mismatch")
print("inventory-mismatch")
"""

_JOURNAL_LANE_CLIENT = """
import hashlib
import json
import os
import pwd
import re
import stat
import sys

root = "/var/lib/kdive/provider-authority/journal"
hold_root = "/var/lib/kdive/provider-authority"
request = json.loads(sys.stdin.buffer.read(512))
if not isinstance(request, dict) or request.get("action") not in {"hide", "restore"}:
    raise SystemExit("invalid journal proof request")
system_id = request.get("system_id")
if not isinstance(system_id, str) or re.fullmatch(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", system_id
) is None:
    raise SystemExit("invalid journal proof system")
uid = pwd.getpwnam("kdive-provider-authority").pw_uid
root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
hold_fd = os.open(hold_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
try:
    root_status = os.fstat(root_fd)
    if (not stat.S_ISDIR(root_status.st_mode) or root_status.st_uid != uid
            or stat.S_IMODE(root_status.st_mode) != 0o700):
        raise SystemExit("unsafe journal root")
    hold_status = os.fstat(hold_fd)
    if (not stat.S_ISDIR(hold_status.st_mode) or hold_status.st_uid != uid
            or stat.S_IMODE(hold_status.st_mode) != 0o700
            or hold_status.st_dev != root_status.st_dev):
        raise SystemExit("unsafe journal proof root")
    name = system_id + ".jsonl"
    held = "." + system_id + ".native-proof-held"
    if request["action"] == "hide":
        status = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != uid
                or stat.S_IMODE(status.st_mode) != 0o600 or status.st_nlink != 1):
            raise SystemExit("unsafe journal lane")
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            content = os.read(descriptor, 64 * 1024 * 1024 + 1)
        finally:
            os.close(descriptor)
        if len(content) != status.st_size or len(content) > 64 * 1024 * 1024:
            raise SystemExit("journal lane exceeds bound")
        try:
            os.stat(held, dir_fd=hold_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise SystemExit("journal proof hold already exists")
        proof = {
            "device": str(status.st_dev), "inode": str(status.st_ino),
            "size": str(status.st_size),
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        }
        os.rename(name, held, src_dir_fd=root_fd, dst_dir_fd=hold_fd)
        os.fsync(root_fd)
        os.fsync(hold_fd)
        print(json.dumps(proof, sort_keys=True, separators=(",", ":")))
    else:
        required = {"action", "system_id", "device", "inode", "size", "digest"}
        if set(request) != required or any(not isinstance(request[key], str) for key in required):
            raise SystemExit("invalid journal restoration proof")
        if (not re.fullmatch(r"[0-9]+", request["device"])
                or not re.fullmatch(r"[0-9]+", request["inode"])
                or not re.fullmatch(r"[0-9]+", request["size"])
                or re.fullmatch(r"sha256:[0-9a-f]{64}", request["digest"]) is None):
            raise SystemExit("invalid journal restoration proof")
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise SystemExit("journal lane unexpectedly exists")
        status = os.stat(held, dir_fd=hold_fd, follow_symlinks=False)
        identity = str(status.st_dev), str(status.st_ino), str(status.st_size)
        expected = request["device"], request["inode"], request["size"]
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != uid
                or stat.S_IMODE(status.st_mode) != 0o600 or status.st_nlink != 1
                or identity != expected):
            raise SystemExit("journal lane identity changed")
        descriptor = os.open(held, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=hold_fd)
        try:
            content = os.read(descriptor, 64 * 1024 * 1024 + 1)
        finally:
            os.close(descriptor)
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if len(content) != status.st_size or digest != request["digest"]:
            raise SystemExit("journal lane content changed")
        os.rename(held, name, src_dir_fd=hold_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
        os.fsync(hold_fd)
        print('{"state":"restored"}')
finally:
    os.close(hold_fd)
    os.close(root_fd)
"""

_FAULT_BARRIER_METADATA = """
import os
import pwd
import stat

path = "/run/kdive/provider-authority/proof-control/control.sock"
metadata = os.stat(path, follow_symlinks=False)
if (not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != pwd.getpwnam("kdive-provider-authority").pw_uid
        or stat.S_IMODE(metadata.st_mode) != 0o600):
    raise SystemExit("fault barrier socket metadata is unsafe")
"""

_FAULT_BARRIER_CLIENT = """
import json
import socket
import sys

path = sys.argv[1]
request = sys.stdin.buffer.read()
if not request or len(request) > 1024:
    raise SystemExit("fault barrier request is unsafe")
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    connection.settimeout(10)
    connection.connect(path)
    connection.sendall(len(request).to_bytes(4, "big") + request)
    header = connection.recv(4)
    if len(header) != 4:
        raise SystemExit("fault barrier response is incomplete")
    size = int.from_bytes(header, "big")
    if size > 1024:
        raise SystemExit("fault barrier response is oversized")
    response = bytearray()
    while len(response) < size:
        chunk = connection.recv(size - len(response))
        if not chunk:
            raise SystemExit("fault barrier response is incomplete")
        response.extend(chunk)
finally:
    connection.close()
sys.stdout.buffer.write(response)
"""

_CREATE_SENTINELS = """
import json
import os
import stat
import sys

for entry in json.load(sys.stdin):
    metadata = os.stat(entry["artifact"], follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"authority fixture artifact is not a regular file: {entry['artifact']}")
    for name in (entry["sentinel"], entry["replacement"]):
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        os.close(descriptor)
"""

_REMOVE_SENTINELS = """
import json
import os
import sys

for entry in json.load(sys.stdin):
    for name in (entry["sentinel"], entry["replacement"]):
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
"""

_IDENTITY_BYPASS_PROBE = """
import json
import os
import sys

failures = []
for entry in json.load(sys.stdin):
    try:
        descriptor = os.open(entry["artifact"], os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        pass
    else:
        os.close(descriptor)
        failures.append(f"opened private artifact {entry['artifact']}")
    try:
        os.unlink(entry["sentinel"])
    except OSError:
        pass
    else:
        failures.append(f"unlinked authority sentinel {entry['sentinel']}")
    try:
        os.replace(entry["replacement"], entry["sentinel"])
    except OSError:
        pass
    else:
        failures.append(f"replaced authority sentinel {entry['sentinel']}")
if failures:
    raise SystemExit("; ".join(failures))
"""


class NativeAuthorityConfig(BaseModel):
    """Non-secret operator inputs for one installed-authority invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    installed_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    system_id: UUID
    project: Annotated[str, Field(min_length=1, max_length=63)]
    ownership_prefix: str
    authority_service: Literal["kdive-external-boot-authority.service"]
    barrier_socket: Path | None = None
    fixture_mode: Literal["create", "verify-existing"] = "create"

    @field_validator("ownership_prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        if _PREFIX.fullmatch(value) is None:
            raise ValueError("ownership prefix must be unique and bounded")
        return value

    @field_validator("barrier_socket")
    @classmethod
    def validate_barrier(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        if value != _FAULT_BARRIER_SOCKET:
            raise ValueError("barrier socket must be the fixed authority proof socket")
        return value


@dataclass(frozen=True, slots=True)
class NormalOperationJobs:
    """The public operations whose exact durable completion the carrier verifies."""

    investigation_id: str
    run_id: str
    activate_job_id: str
    release_job_id: str


@dataclass(frozen=True, slots=True)
class ActivationJob:
    """One publicly admitted activation whose job has not yet been drained."""

    investigation_id: str
    run_id: str
    activate_job_id: str


@dataclass(frozen=True, slots=True)
class JournalLaneProof:
    """Exact immutable lane facts required to restore a temporarily hidden journal."""

    device: str
    inode: str
    size: str
    digest: str


@dataclass(frozen=True, slots=True)
class RunningJobClaim:
    """The durable live claim used to fence one paused worker and observe its replacement."""

    worker_id: str
    attempt: int
    lease_expires_at: datetime
    server_time: datetime


def _output(*argv: str) -> str:
    result = subprocess.run(argv, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def run_installed_local_authority_normal_operations() -> None:
    """Run the configured carrier's public activate and root-release proof.

    The collected native-test entrypoint remains deliberately thin; this support function also
    keeps pre-mutation, confinement, and cleanup behavior directly unit-testable without importing
    a collected test module.
    """
    config = load_config()
    if config is None:
        pytest.skip("installed local-authority carrier is not configured")

    installed = _output("sudo", "-n", "cat", "/opt/kdive-provider-authority/revision")
    assert installed == config.installed_revision, (
        f"installed authority revision {installed!r} does not match configured coherent revision"
    )
    assert _output("systemctl", "is-active", config.authority_service) == "active"
    running_workers = _output(
        "systemctl",
        "list-units",
        "kdive-live-worker@*.service",
        "--state=running",
        "--no-legend",
    )

    issuer = require_issuer()
    base_url = require_stack()
    require_deployed_revision(config, base_url, running_workers)
    require_installed_authority_routes(config, running_workers)
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    assert db_url, "native authority carrier requires KDIVE_DATABASE_URL"
    token = mint_role_token(
        issuer,
        project=config.project,
        agent_session=config.ownership_prefix,
        role="admin",
    )
    ledger = ResourceLedger(config.ownership_prefix)

    async def run() -> None:
        await provision_authority_fixture(db_url, config)
        require_authority_artifact_confinement(config, running_workers)
        client = LiveStackClient.over_http(base_url, token)
        async with client:
            primary: Exception | None = None
            try:
                operations = await drive_normal_operations(client, config, ledger)
                await assert_root_release_completion(db_url, operations)
            except Exception as exc:  # preserve the native failure while still attempting cleanup
                primary = exc
            cleanup_failures: list[Exception] = []
            investigations = [r for r in ledger.resources if r.kind == "investigation"]
            for resource in reversed(investigations):
                try:
                    closed = await client.call_tool(
                        "investigations.close",
                        investigation_id=resource.identity,
                        summary="Native authority proof cleanup",
                    )
                    assert not isinstance(closed, list)
                    assert closed.status not in {"error", "failed"}
                except Exception as exc:
                    cleanup_failures.append(exc)
            if primary is not None:
                cleanup_failures.insert(0, primary)
            if len(cleanup_failures) == 1:
                raise cleanup_failures[0]
            if cleanup_failures:
                raise ExceptionGroup("native carrier and cleanup failures", cleanup_failures)

    asyncio.run(run())


def run_installed_local_authority_restart_recovery() -> None:
    """Prove a real authority restart after provider effect, before its journal return record."""
    config = load_config()
    if config is None:
        pytest.skip("installed local-authority carrier is not configured")
    require_fault_barrier(config)

    installed = _output("sudo", "-n", "cat", "/opt/kdive-provider-authority/revision")
    assert installed == config.installed_revision, (
        f"installed authority revision {installed!r} does not match configured coherent revision"
    )
    assert _output("systemctl", "is-active", config.authority_service) == "active"
    running_workers = _output(
        "systemctl",
        "list-units",
        "kdive-live-worker@*.service",
        "--state=running",
        "--no-legend",
    )

    issuer = require_issuer()
    base_url = require_stack()
    require_deployed_revision(config, base_url, running_workers)
    require_installed_authority_routes(config, running_workers)
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    assert db_url, "native authority carrier requires KDIVE_DATABASE_URL"
    token = mint_role_token(
        issuer,
        project=config.project,
        agent_session=config.ownership_prefix,
        role="admin",
    )
    ledger = ResourceLedger(config.ownership_prefix)

    async def run() -> None:
        await provision_authority_fixture(db_url, config)
        require_authority_artifact_confinement(config, running_workers)
        client = LiveStackClient.over_http(base_url, token)
        async with client:
            primary: Exception | None = None
            try:
                activation = await start_external_boot_activation(
                    client,
                    config,
                    ledger,
                    before_activate=lambda run_id: arm_fault_barrier(
                        config, run_id, "activate", "after-provider"
                    ),
                )
                wait_for_fault_barrier(config)
                restart_authority_after_fault(config)
                await drain_job(client, "activate-restart-recovery", activation.activate_job_id)
                release = ok(
                    await scalar(client, "runs.release_external_boot", run_id=activation.run_id),
                    "release",
                )
                await drain_job(client, "release", release.object_id)
                await assert_root_release_completion(
                    db_url,
                    NormalOperationJobs(
                        investigation_id=activation.investigation_id,
                        run_id=activation.run_id,
                        activate_job_id=activation.activate_job_id,
                        release_job_id=release.object_id,
                    ),
                )
            except Exception as exc:  # preserve the native failure while still attempting cleanup
                primary = exc
            cleanup_failures: list[Exception] = []
            investigations = [
                resource for resource in ledger.resources if resource.kind == "investigation"
            ]
            for resource in reversed(investigations):
                try:
                    closed = await client.call_tool(
                        "investigations.close",
                        investigation_id=resource.identity,
                        summary="Native authority proof cleanup",
                    )
                    assert not isinstance(closed, list)
                    assert closed.status not in {"error", "failed"}
                except Exception as exc:
                    cleanup_failures.append(exc)
            if primary is not None:
                cleanup_failures.insert(0, primary)
            if len(cleanup_failures) == 1:
                raise cleanup_failures[0]
            if cleanup_failures:
                raise ExceptionGroup("native carrier and cleanup failures", cleanup_failures)

    asyncio.run(run())


def run_installed_local_authority_unresolved_call_takeover() -> None:
    """Prove a paused genuine holder is reclaimed and its late completion remains fenced."""
    config = load_config()
    if config is None:
        pytest.skip("installed local-authority carrier is not configured")
    require_fault_barrier(config)
    installed = _output("sudo", "-n", "cat", "/opt/kdive-provider-authority/revision")
    assert installed == config.installed_revision
    running_workers = _output(
        "systemctl", "list-units", "kdive-live-worker@*.service", "--state=running", "--no-legend"
    )
    require_deployed_revision(config, require_stack(), running_workers)
    require_installed_authority_routes(config, running_workers)
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    assert db_url, "native authority carrier requires KDIVE_DATABASE_URL"
    token = mint_role_token(
        require_issuer(),
        project=config.project,
        agent_session=config.ownership_prefix,
        role="admin",
    )
    ledger = ResourceLedger(config.ownership_prefix)

    async def run() -> None:
        await provision_authority_fixture(db_url, config)
        require_authority_artifact_confinement(config, running_workers)
        client = LiveStackClient.over_http(require_stack(), token)
        async with client:
            held: RunningJobClaim | None = None
            try:
                activation = await start_external_boot_activation(
                    client,
                    config,
                    ledger,
                    before_activate=lambda run_id: arm_fault_barrier(
                        config, run_id, "activate", "before-provider"
                    ),
                )
                wait_for_fault_barrier(config)
                held = await running_job_claim(db_url, activation.activate_job_id)
                held_invocation = set_exact_worker_hold(held, "stop")
                replacement = await wait_for_reclaimed_job(db_url, activation.activate_job_id, held)
                release_fault_barrier(config)
                await drain_job(client, "activate-takeover", activation.activate_job_id)
                await assert_stale_provider_request_denied(
                    db_url, config, activation, held, replacement, held_invocation
                )
                resumed_invocation = set_exact_worker_hold(held, "continue")
                if resumed_invocation != held_invocation:
                    raise AssertionError("native worker invocation changed across retained hold")
                await wait_for_stale_worker_commit_observed(
                    activation.activate_job_id, held_invocation
                )
                held = None
                if replacement.attempt < 2:
                    raise AssertionError("native takeover did not charge a reclaimed attempt")
                async with (
                    await psycopg.AsyncConnection.connect(db_url) as conn,
                    conn.cursor() as cur,
                ):
                    await cur.execute(
                        "SELECT state, attempt, worker_id FROM jobs WHERE id = %s",
                        (activation.activate_job_id,),
                    )
                    row = await cur.fetchone()
                if row != ("succeeded", replacement.attempt, replacement.worker_id):
                    raise AssertionError("stale paused worker completion changed the reclaimed job")
                release = ok(
                    await scalar(client, "runs.release_external_boot", run_id=activation.run_id),
                    "release",
                )
                await drain_job(client, "release-takeover", release.object_id)
                await assert_root_release_completion(
                    db_url,
                    NormalOperationJobs(
                        activation.investigation_id,
                        activation.run_id,
                        activation.activate_job_id,
                        release.object_id,
                    ),
                )
                closed = await client.call_tool(
                    "investigations.close",
                    investigation_id=activation.investigation_id,
                    summary="Native stale-provider and reclaimed-commit proof completed",
                )
                assert not isinstance(closed, list)
                assert closed.status not in {"error", "failed"}
            finally:
                if held is not None:
                    set_exact_worker_hold(held, "continue")

    asyncio.run(run())


def require_deployed_revision(
    config: NativeAuthorityConfig,
    base_url: str,
    running_workers: str,
    *,
    fetch: Callable[[str], dict[str, object] | None] = _fetch_version,
    resolve: Callable[[str], str | None] = _resolve,
) -> None:
    """Refuse carrier mutation unless reports resolve exactly to the configured full build."""
    default_urls = readyz_urls(base_url, {})
    server_url = default_urls["server"]
    worker_urls = _active_worker_readyz_urls(base_url, running_workers)
    for process, url in (("server", server_url), *worker_urls):
        version = fetch(url)
        commit = version.get("commit") if version is not None else None
        resolved = resolve(commit) if isinstance(commit, str) else None
        if resolved != config.installed_revision:
            reported = commit if isinstance(commit, str) else "unknown"
            raise AssertionError(
                f"deployed {process} revision {reported!r} does not match configured "
                f"installed revision {config.installed_revision}"
            )


def _active_worker_readyz_urls(base_url: str, running_workers: str) -> tuple[tuple[str, str], ...]:
    """Derive fixed-slot aux URLs from the lifecycle provisioner's assigned bind map."""
    slots = sorted({int(slot) for slot in _FIXED_WORKER_UNIT.findall(running_workers)})
    if not slots:
        raise AssertionError("native authority carrier requires an active fixed worker incarnation")
    parsed = urlsplit(base_url)
    host = parsed.hostname
    if host is None:
        raise AssertionError("native authority carrier stack URL has no host")
    authority = f"[{host}]" if ":" in host else host
    scheme = parsed.scheme or "http"
    return tuple(
        (
            f"worker slot {slot}",
            f"{scheme}://{authority}:{9465 if slot == 1 else 9468 + slot}/readyz",
        )
        for slot in slots
    )


def _active_worker_slots(running_workers: str) -> tuple[int, ...]:
    """Return the exact fixed-worker slots systemd reports as active for this invocation."""
    slots = tuple(sorted({int(slot) for slot in _FIXED_WORKER_UNIT.findall(running_workers)}))
    if not slots:
        raise AssertionError("native authority carrier requires an active fixed worker incarnation")
    return slots


def require_installed_authority_routes(
    _config: NativeAuthorityConfig, running_workers: str
) -> None:
    """Check the exact live server, authority, and active-worker route before mutation."""
    slots = _active_worker_slots(running_workers)
    result = _output(
        "sudo",
        "-n",
        _IDENTITY_PYTHON,
        "-c",
        _INSTALLED_ROUTE_PREFLIGHT,
        *(str(slot) for slot in slots),
    )
    if result != "route-ok":
        raise AssertionError("installed local-authority route preflight returned an invalid result")


def _authority_artifacts(config: NativeAuthorityConfig) -> tuple[tuple[str, Path], ...]:
    """Construct only the selected fixture's two authority-private artifacts."""
    return (
        ("overlay", _AUTHORITY_ARTIFACT_ROOTS[0] / f"{config.system_id}-overlay.qcow2"),
        ("console", _AUTHORITY_ARTIFACT_ROOTS[1] / f"{config.system_id}.log"),
    )


def _sentinel_entries(config: NativeAuthorityConfig) -> tuple[dict[str, str], ...]:
    """Name the only new files this invocation may create beneath private artifact parents."""
    return tuple(
        {
            "artifact": str(artifact),
            "sentinel": str(artifact.parent / f".{config.ownership_prefix}-{kind}-sentinel"),
            "replacement": str(artifact.parent / f".{config.ownership_prefix}-{kind}-replacement"),
        }
        for kind, artifact in _authority_artifacts(config)
    )


def _run_identity_program(
    identity: str,
    program: str,
    entries: tuple[dict[str, str], ...],
    *,
    sudo: bool,
) -> None:
    argv = [_IDENTITY_PYTHON, "-c", program]
    if sudo:
        argv = ["sudo", "-n", "-u", identity, *argv]
    result = subprocess.run(
        argv,
        input=json.dumps(entries),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise AssertionError(f"identity {identity} bypass probe failed: {detail}")


def require_authority_artifact_confinement(
    config: NativeAuthorityConfig,
    running_workers: str,
) -> None:
    """Require every active worker and this control uid to be unable to alter private artifacts.

    This is native-only evidence: the subprocesses run under installed identities. Unit tests mock
    only that subprocess boundary and do not establish host permission enforcement.
    """
    entries = _sentinel_entries(config)
    _run_identity_program(_AUTHORITY_ACCOUNT, _CREATE_SENTINELS, entries, sudo=True)
    try:
        for slot in _active_worker_slots(running_workers):
            _run_identity_program(
                f"kdive-worker-{slot}", _IDENTITY_BYPASS_PROBE, entries, sudo=True
            )
        control_identity = pwd.getpwuid(os.geteuid()).pw_name
        _run_identity_program(control_identity, _IDENTITY_BYPASS_PROBE, entries, sudo=False)
    finally:
        _run_identity_program(_AUTHORITY_ACCOUNT, _REMOVE_SENTINELS, entries, sudo=True)


def load_config(environment: dict[str, str] | None = None) -> NativeAuthorityConfig | None:
    """Return ``None`` only when the native carrier trigger is absent."""
    env = os.environ if environment is None else environment
    raw = env.get(CONFIG_ENV)
    if raw is None:
        return None
    path = Path(raw)
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) not in (0o400, 0o600)
            or not 0 < metadata.st_size <= 16_384
        ):
            raise ValueError("local authority proof config has unsafe metadata")
        data = os.read(descriptor, 16_385)
    finally:
        os.close(descriptor)
    if len(data) > 16_384:
        raise ValueError("local authority proof config exceeds 16384 bytes")
    return NativeAuthorityConfig.model_validate_json(data)


class OwnedResource(BaseModel):
    """One exact carrier-created resource eligible for cleanup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal[
        "domain", "volume", "object", "journal", "recovery", "investigation", "run", "activation"
    ]
    identity: Annotated[str, Field(min_length=1, max_length=1024)]


class ResourceLedger:
    """Attempt exact cleanup in reverse creation order without inferred targets."""

    def __init__(self, prefix: str) -> None:
        if _PREFIX.fullmatch(prefix) is None:
            raise ValueError("invalid ownership prefix")
        self.prefix = prefix
        self._resources: list[OwnedResource] = []

    @property
    def resources(self) -> tuple[OwnedResource, ...]:
        return tuple(self._resources)

    def record(self, resource: OwnedResource) -> None:
        if resource in self._resources:
            raise ValueError("owned resource was recorded twice")
        if self.prefix not in resource.identity and resource.kind not in {
            "investigation",
            "run",
            "activation",
        }:
            raise ValueError("resource identity is outside the invocation ownership scope")
        self._resources.append(resource)

    def cleanup(self, remove: Callable[[OwnedResource], None]) -> None:
        failures: list[Exception] = []
        for resource in reversed(self._resources):
            try:
                remove(resource)
            except Exception as exc:
                try:
                    raise RuntimeError(f"cleanup failed for {resource.kind}") from exc
                except RuntimeError as wrapped:
                    failures.append(wrapped)
        if failures:
            raise ExceptionGroup("owned-resource cleanup failed", failures)


def require_fault_barrier(config: NativeAuthorityConfig) -> Path:
    """Fail loud rather than representing unavailable deterministic arms as acceptance."""
    if config.barrier_socket is None:
        raise RuntimeError("installed authority exposes no deterministic provider-effect barrier")
    try:
        _output("sudo", "-n", _IDENTITY_PYTHON, "-c", _FAULT_BARRIER_METADATA)
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "installed authority exposes no deterministic provider-effect barrier"
        ) from None
    return config.barrier_socket


def fault_barrier_request(config: NativeAuthorityConfig, request: dict[str, str]) -> dict[str, str]:
    """Send one bounded closed proof request through the root-only installed socket."""
    socket_path = require_fault_barrier(config)
    payload = json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if not payload or len(payload) > _FAULT_BARRIER_MAX_BYTES:
        raise ValueError("fault barrier request exceeds its closed byte limit")
    result = subprocess.run(
        ["sudo", "-n", _IDENTITY_PYTHON, "-c", _FAULT_BARRIER_CLIENT, str(socket_path)],
        input=payload,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()[-1000:]
        raise RuntimeError(f"installed authority fault barrier request failed: {detail}")
    if len(result.stdout) > _FAULT_BARRIER_MAX_BYTES:
        raise AssertionError("installed authority fault barrier response is oversized")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError("installed authority fault barrier response is malformed") from None
    if not isinstance(response, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in response.items()
    ):
        raise AssertionError("installed authority fault barrier response is malformed")
    return response


def _journal_lane_request(config: NativeAuthorityConfig, request: dict[str, str]) -> dict[str, str]:
    """Run the fixed root-only lane mover; it never accepts a filesystem destination."""
    payload = json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if not payload or len(payload) > _JOURNAL_PROOF_MAX_BYTES:
        raise ValueError("journal lane proof request exceeds its closed byte limit")
    result = subprocess.run(
        ["sudo", "-n", _IDENTITY_PYTHON, "-c", _JOURNAL_LANE_CLIENT],
        input=payload,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()[-1000:]
        raise RuntimeError(f"installed authority journal lane proof failed: {detail}")
    if len(result.stdout) > _JOURNAL_PROOF_MAX_BYTES:
        raise AssertionError("journal lane proof response is oversized")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError("journal lane proof is malformed") from None
    if not isinstance(response, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in response.items()
    ):
        raise AssertionError("journal lane proof is malformed")
    return response


def hide_authority_journal_lane(config: NativeAuthorityConfig) -> JournalLaneProof:
    """Hide exactly this System's lane and retain its inode/content proof for restoration."""
    response = _journal_lane_request(config, {"action": "hide", "system_id": str(config.system_id)})
    required = {"device", "inode", "size", "digest"}
    if set(response) != required or any(
        not response[name].isdigit() for name in required - {"digest"}
    ):
        raise AssertionError("journal lane proof is malformed")
    digest = response["digest"]
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise AssertionError("journal lane proof is malformed")
    return JournalLaneProof(
        device=response["device"], inode=response["inode"], size=response["size"], digest=digest
    )


def restore_authority_journal_lane(config: NativeAuthorityConfig, proof: JournalLaneProof) -> None:
    """Restore only the exact previously hidden lane; replacement is a hard failure."""
    response = _journal_lane_request(
        config,
        {
            "action": "restore",
            "system_id": str(config.system_id),
            "device": proof.device,
            "inode": proof.inode,
            "size": proof.size,
            "digest": proof.digest,
        },
    )
    if response != {"state": "restored"}:
        raise AssertionError(f"journal lane proof did not restore its exact lane: {response!r}")


def arm_fault_barrier(
    config: NativeAuthorityConfig,
    run_id: str,
    operation: Literal["activate", "recover", "cleanup"],
    checkpoint: Literal["before-provider", "after-provider"],
) -> None:
    """Arm one exact configured-System provider checkpoint or fail loud."""
    response = fault_barrier_request(
        config,
        {
            "action": "arm",
            "system_id": str(config.system_id),
            "run_id": run_id,
            "operation": operation,
            "checkpoint": checkpoint,
        },
    )
    if response != {"state": "armed"}:
        raise RuntimeError(f"installed authority fault barrier refused arm: {response!r}")


def release_fault_barrier(config: NativeAuthorityConfig) -> None:
    """Release the sole configured-System fault checkpoint or fail loud."""
    response = fault_barrier_request(config, {"action": "release"})
    if response != {"state": "released"}:
        raise RuntimeError(f"installed authority fault barrier refused release: {response!r}")


def wait_for_fault_barrier(config: NativeAuthorityConfig, *, timeout_seconds: float = 60.0) -> None:
    """Wait for the configured checkpoint by its observable state, never elapsed-time luck."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        response = fault_barrier_request(config, {"action": "status"})
        if response == {"state": "reached"}:
            return
        if response != {"state": "armed"}:
            raise RuntimeError(f"installed authority fault barrier lost its arm: {response!r}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "installed authority provider effect did not reach its armed barrier"
            )
        time.sleep(min(0.1, remaining))


def set_exact_worker_hold(claim: RunningJobClaim, action: Literal["stop", "continue"]) -> str:
    """Signal only the retained unit whose immutable state names the active job holder."""
    result = _output(
        "sudo", "-n", _IDENTITY_PYTHON, "-c", _WORKER_HOLD_CLIENT, claim.worker_id, action
    )
    try:
        response = json.loads(result)
    except json.JSONDecodeError:
        raise AssertionError("native worker hold returned an invalid result") from None
    if (
        not isinstance(response, dict)
        or set(response) != {"state", "invocation_id"}
        or response["state"] != f"{action}ped"
        or not isinstance(response["invocation_id"], str)
        or re.fullmatch(r"[0-9a-f]{32}", response["invocation_id"]) is None
    ):
        raise AssertionError("native worker hold returned an invalid result")
    return response["invocation_id"]


async def assert_stale_provider_request_denied(
    db_url: str,
    config: NativeAuthorityConfig,
    activation: ActivationJob,
    original: RunningJobClaim,
    replacement: RunningJobClaim,
    invocation_id: str,
) -> None:
    """Submit one exact g1 mutation through the installed typed sender and require fencing."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, generation, worker_incarnation, state, activation_id, run_id, "
            "plan_identity, purpose, provider_kind, authority_instance, operation, "
            "operation_identity, operation_digest, job_id, job_attempt "
            "FROM external_boot_authorities WHERE system_id=%s AND job_id=%s ORDER BY generation",
            (config.system_id, activation.activate_job_id),
        )
        authorities = await cur.fetchall()
        await cur.execute(
            "SELECT state, attempt, worker_id FROM jobs WHERE id=%s",
            (activation.activate_job_id,),
        )
        job_before = await cur.fetchone()
        await cur.execute(
            "SELECT authority_id, generation, sequence, digest, head_record "
            "FROM external_boot_authority_journal_heads WHERE system_id=%s",
            (config.system_id,),
        )
        head_before = await cur.fetchone()
    if len(authorities) != 2:
        raise AssertionError("stale-provider proof requires exact g1/g2 authorities")
    g1, g2 = authorities
    if (
        str(g1[2]) != original.worker_id
        or g1[3] != "superseded"
        or str(g2[2]) != replacement.worker_id
        or g2[3] != "current"
        or str(g1[4]) != str(g2[4])
        or str(g1[5]) != activation.run_id
        or str(g2[5]) != activation.run_id
        or g1[6:12] != g2[6:12]
        or g1[12] == g2[12]
        or str(g1[13]) != activation.activate_job_id
        or str(g2[13]) != activation.activate_job_id
        or g1[14] != original.attempt
        or g2[14] != replacement.attempt
        or g1[1] >= g2[1]
        or job_before != ("succeeded", replacement.attempt, replacement.worker_id)
        or head_before is None
        or head_before[0] != g2[0]
        or head_before[1] != g2[1]
    ):
        raise AssertionError("stale-provider proof DB binding is not exact")
    result = _output(
        "sudo",
        "-n",
        "/opt/kdive/.venv/bin/python",
        "-c",
        _STALE_PROVIDER_CLIENT,
        str(config.system_id),
        activation.run_id,
        original.worker_id,
        invocation_id,
        str(g1[0]),
        str(g1[1]),
        activation.activate_job_id,
    )
    if result != "superseded":
        raise AssertionError("installed authority returned an invalid stale-provider proof")
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT state, attempt, worker_id FROM jobs WHERE id=%s",
            (activation.activate_job_id,),
        )
        job_after = await cur.fetchone()
        await cur.execute(
            "SELECT authority_id, generation, sequence, digest, head_record "
            "FROM external_boot_authority_journal_heads WHERE system_id=%s",
            (config.system_id,),
        )
        head_after = await cur.fetchone()
    if job_after != job_before or head_after != head_before:
        raise AssertionError("stale provider denial changed g2 job or journal head")


async def running_job_claim(db_url: str, job_id: str) -> RunningJobClaim:
    """Read the actual leased job holder; no caller-crafted worker identity is accepted."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT worker_id, attempt, lease_expires_at, clock_timestamp() FROM jobs "
            "WHERE id = %s AND state = 'running' AND worker_id IS NOT NULL "
            "AND lease_expires_at IS NOT NULL",
            (job_id,),
        )
        row = await cur.fetchone()
    if row is None or not isinstance(row[0], str) or not isinstance(row[1], int):
        raise AssertionError("fault barrier has no durable running job claim")
    if not isinstance(row[2], datetime) or not isinstance(row[3], datetime):
        raise AssertionError("fault barrier running job has no lease deadline")
    return RunningJobClaim(row[0], row[1], row[2], row[3])


async def wait_for_reclaimed_job(
    db_url: str, job_id: str, original: RunningJobClaim
) -> RunningJobClaim:
    """Observe genuine lease expiry and a distinct worker claim within the bounded native arm."""
    started = time.monotonic()
    deadline = min(
        started + _TAKEOVER_WAIT_SECONDS,
        started
        + max(0.0, (original.lease_expires_at - original.server_time).total_seconds())
        + 120.0,
    )
    while time.monotonic() < deadline:
        async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT worker_id, attempt, lease_expires_at, clock_timestamp() "
                "FROM jobs WHERE id = %s",
                (job_id,),
            )
            row = await cur.fetchone()
        if (
            row is not None
            and isinstance(row[0], str)
            and isinstance(row[1], int)
            and isinstance(row[2], datetime)
            and isinstance(row[3], datetime)
            and row[1] > original.attempt
            and row[0] != original.worker_id
        ):
            return RunningJobClaim(row[0], row[1], row[2], row[3])
        await asyncio.sleep(1.0)
    raise TimeoutError(
        "native takeover did not reclaim the paused worker lease before its deadline"
    )


async def wait_for_stale_worker_commit_observed(
    job_id: str, invocation_id: str, *, timeout_seconds: float = 60.0
) -> None:
    """Observe the exact retained systemd invocation dropping its fenced commit."""
    if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
        raise ValueError("stale worker commit requires a validated systemd invocation ID")
    expected = f"external boot job {job_id} was reclaimed; result dropped"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "journalctl",
                "-b",
                "--no-pager",
                "-o",
                "json",
                "_SYSTEMD_INVOCATION_ID=" + invocation_id,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            try:
                entries = [json.loads(line) for line in result.stdout.splitlines()]
                messages = [
                    json.loads(entry["MESSAGE"])
                    for entry in entries
                    if isinstance(entry, dict)
                    and isinstance(entry.get("MESSAGE"), str)
                    and len(entry["MESSAGE"]) <= 4096
                ]
            except json.JSONDecodeError, KeyError, TypeError:
                messages = []
            if any(
                isinstance(message, dict)
                and message.get("logger") == "kdive.jobs.worker"
                and message.get("msg") == expected
                for message in messages
            ):
                return
        await asyncio.sleep(0.2)
    raise TimeoutError("stale paused worker did not attempt its fenced core commit")


def restart_authority_after_fault(config: NativeAuthorityConfig) -> None:
    """Restart only the configured authority service after a reached native checkpoint."""
    _output("sudo", "-n", "systemctl", "restart", config.authority_service)
    if _output("systemctl", "is-active", config.authority_service) != "active":
        raise AssertionError("authority service did not return active after fault restart")


def stop_authority_for_journal_loss(config: NativeAuthorityConfig) -> None:
    """Stop only the configured authority before hiding its lane for an inventory proof."""
    _output("sudo", "-n", "systemctl", "stop", config.authority_service)
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", config.authority_service], check=False
    )
    if result.returncode == 0:
        raise AssertionError("authority service remained active before journal-loss proof")


def stop_authority_after_inventory_refusal(config: NativeAuthorityConfig) -> None:
    """Fence an auto-restarting failed authority before restoring its hidden lane."""
    _output("sudo", "-n", "systemctl", "stop", config.authority_service)
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", config.authority_service], check=False
    )
    if result.returncode == 0:
        raise AssertionError("authority service remained active before journal lane restoration")


def require_journal_inventory_refusal(config: NativeAuthorityConfig) -> None:
    """Require a hidden owned lane to prevent startup; a missing lane cannot silently recreate."""
    result = subprocess.run(
        ["sudo", "-n", "systemctl", "start", config.authority_service],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        raise AssertionError("authority accepted a missing journal lane")
    if _output("sudo", "-n", _IDENTITY_PYTHON, "-c", _JOURNAL_INVENTORY_FAILURE) != (
        "inventory-mismatch"
    ):
        raise AssertionError(
            "authority startup did not refuse the missing lane as inventory-mismatch"
        )


def restore_after_journal_inventory_refusal(
    config: NativeAuthorityConfig, proof: JournalLaneProof
) -> None:
    """Restore only after the failed authority is stopped, including auto-restart attempts."""
    try:
        require_journal_inventory_refusal(config)
    finally:
        stop_authority_after_inventory_refusal(config)
        restore_authority_journal_lane(config, proof)
        restart_authority_after_fault(config)


def run_installed_local_authority_journal_restore_recovery() -> None:
    """Prove a renamed owned journal lane blocks startup and restores byte-for-byte before retry."""
    config = load_config()
    if config is None:
        pytest.skip("installed local-authority carrier is not configured")
    require_fault_barrier(config)
    installed = _output("sudo", "-n", "cat", "/opt/kdive-provider-authority/revision")
    assert installed == config.installed_revision, (
        "installed authority revision does not match config"
    )
    running_workers = _output(
        "systemctl", "list-units", "kdive-live-worker@*.service", "--state=running", "--no-legend"
    )
    require_deployed_revision(config, require_stack(), running_workers)
    require_installed_authority_routes(config, running_workers)
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    assert db_url, "native authority carrier requires KDIVE_DATABASE_URL"
    issuer = require_issuer()
    token = mint_role_token(
        issuer, project=config.project, agent_session=config.ownership_prefix, role="admin"
    )
    ledger = ResourceLedger(config.ownership_prefix)

    async def run() -> None:
        await provision_authority_fixture(db_url, config)
        require_authority_artifact_confinement(config, running_workers)
        client = LiveStackClient.over_http(require_stack(), token)
        async with client:
            primary: Exception | None = None
            try:
                activation = await start_external_boot_activation(
                    client,
                    config,
                    ledger,
                    before_activate=lambda run_id: arm_fault_barrier(
                        config, run_id, "activate", "after-provider"
                    ),
                )
                wait_for_fault_barrier(config)
                stop_authority_for_journal_loss(config)
                proof = hide_authority_journal_lane(config)
                restore_after_journal_inventory_refusal(config, proof)
                await drain_job(client, "activate-journal-restore", activation.activate_job_id)
                release = ok(
                    await scalar(client, "runs.release_external_boot", run_id=activation.run_id),
                    "release",
                )
                await drain_job(client, "release", release.object_id)
                await assert_root_release_completion(
                    db_url,
                    NormalOperationJobs(
                        activation.investigation_id,
                        activation.run_id,
                        activation.activate_job_id,
                        release.object_id,
                    ),
                )
            except Exception as exc:
                primary = exc
            finally:
                failures = [primary] if primary is not None else []
                for resource in reversed(
                    [item for item in ledger.resources if item.kind == "investigation"]
                ):
                    try:
                        closed = await client.call_tool(
                            "investigations.close",
                            investigation_id=resource.identity,
                            summary="Native authority proof cleanup",
                        )
                        assert not isinstance(closed, list) and closed.status not in {
                            "error",
                            "failed",
                        }
                    except Exception as exc:
                        failures.append(exc)
                if len(failures) == 1:
                    raise failures[0]
                if failures:
                    raise ExceptionGroup("native carrier and cleanup failures", failures)

    asyncio.run(run())


async def assert_root_release_completion(db_url: str, operations: NormalOperationJobs) -> None:
    """Require the root release job's exact derived recover/cleanup finalizer evidence."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        for job_id, operation in (
            (operations.activate_job_id, "activate"),
            (operations.release_job_id, "release"),
        ):
            await cur.execute(
                "SELECT state, payload->'external_boot_authority_v1'->>'operation', "
                "payload->'external_boot_authority_v1'->>'run_id' FROM jobs WHERE id = %s",
                (job_id,),
            )
            if await cur.fetchall() != [("succeeded", operation, operations.run_id)]:
                raise AssertionError(
                    f"root release completion lacks succeeded {operation} job {job_id}"
                )
        await cur.execute(
            "SELECT activation.state, activation.cleanup_complete, attempt.state, "
            "attempt.authority_generation = root.generation, "
            "attempt.terminal_evidence IS NOT NULL, root.state, root.purpose, root.operation, "
            "root.activation_id = activation.id, root.system_id = activation.system_id, "
            "root.run_id = activation.run_id, root.plan_identity = activation.plan_identity, "
            "root.job_id = receipt.job_id, root.job_attempt = receipt.job_attempt, "
            "receipt.consumed, "
            "receipt.adopted_from_root_authority_id IS NULL, head.phase, "
            "head.authority_id = root.id, head.generation = root.generation, "
            "head.sequence = receipt.journal_sequence, head.digest = receipt.journal_digest, "
            "head.head_record->>'operation', "
            "head.head_record->>'operation_identity' = receipt.operation_identity, "
            "head.head_record->>'operation_digest' = receipt.operation_digest, "
            "head.head_record #>> '{observation,category}', "
            "head.head_record #>> '{observation,composite_state}' = "
            "receipt.observed_absent_digest, "
            "(SELECT count(*) FROM external_boot_reservation_releases AS credit "
            " WHERE credit.activation_id = activation.id), "
            "NOT EXISTS (SELECT 1 FROM external_boot_reservations AS pending "
            "            WHERE pending.activation_id = activation.id) "
            "FROM external_boot_activations AS activation "
            "JOIN external_boot_recovery_attempts AS attempt "
            "  ON attempt.activation_id = activation.id "
            " AND attempt.attempt_id = activation.current_attempt_id "
            "JOIN external_boot_release_cleanup_receipts AS receipt "
            "  ON receipt.activation_id = activation.id "
            "JOIN external_boot_authorities AS root ON root.id = receipt.root_authority_id "
            "JOIN external_boot_authority_journal_heads AS head "
            "  ON head.system_id = root.system_id "
            " AND head.authority_instance = root.authority_instance "
            "WHERE activation.run_id = %s AND receipt.job_id = %s "
            "  AND receipt.run_id = activation.run_id AND receipt.system_id = activation.system_id "
            "  AND receipt.plan_identity = activation.plan_identity",
            (operations.run_id, operations.release_job_id),
        )
        expected = [
            (
                "recovered",
                True,
                "recovered",
                True,
                True,
                "retired",
                "release",
                "release",
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                "terminal",
                True,
                True,
                True,
                True,
                "cleanup",
                True,
                True,
                "absent",
                True,
                1,
                True,
            )
        ]
        actual = await cur.fetchall()
        if actual != expected:
            raise AssertionError(
                f"root release completion lacks exact derived finalizer evidence: {actual!r}"
            )


async def provision_authority_fixture(db_url: str, config: NativeAuthorityConfig) -> None:
    """Create or read-only verify only the selected disposable authority fixture."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT provisioning_profile FROM systems WHERE id = %s AND project = %s",
            (config.system_id, config.project),
        )
        row = await cur.fetchone()
    if row is None:
        raise ValueError(
            "selected authority fixture System is absent or belongs to another project"
        )
    script = Path(__file__).resolve().parents[2] / "scripts/live-vm/provision-authority-fixture.py"
    arguments = [
        "sudo",
        "-n",
        "/opt/kdive-provider-authority/.venv/bin/python",
        str(script),
    ]
    if config.fixture_mode == "verify-existing":
        arguments.append("--verify-existing")
    arguments.append(str(config.system_id))
    result = subprocess.run(
        arguments,
        input=json.dumps(row[0]),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise RuntimeError(f"authority fixture {config.fixture_mode} failed: {detail}")


async def drive_normal_operations(
    client: LiveStackClient,
    config: NativeAuthorityConfig,
    ledger: ResourceLedger,
) -> NormalOperationJobs:
    """Drive activate then release/cleanup through public tools and real job polling.

    ``runs.boot`` is the public activation admission. ``runs.release_external_boot`` is the
    public release admission; its one root release job owns the derived recover and cleanup
    phases, whose durable finalizer evidence the native carrier verifies after polling.
    """
    activation = await start_external_boot_activation(client, config, ledger)
    await drain_job(client, "activate", activation.activate_job_id)
    release = ok(
        await scalar(client, "runs.release_external_boot", run_id=activation.run_id),
        "release",
    )
    await drain_job(client, "release", release.object_id)
    return NormalOperationJobs(
        investigation_id=activation.investigation_id,
        run_id=activation.run_id,
        activate_job_id=activation.activate_job_id,
        release_job_id=release.object_id,
    )


async def start_external_boot_activation(
    client: LiveStackClient,
    config: NativeAuthorityConfig,
    ledger: ResourceLedger,
    *,
    before_activate: Callable[[str], None] | None = None,
) -> ActivationJob:
    """Create, install, and publicly admit one activation without polling its job."""
    opened = ok(
        await scalar(
            client,
            "investigations.open",
            project=config.project,
            title=config.ownership_prefix,
        ),
        "open-investigation",
    )
    investigation_id = opened.object_id
    ledger.record(OwnedResource(kind="investigation", identity=investigation_id))
    created = ok(
        await scalar(
            client,
            "runs.create",
            investigation_id=investigation_id,
            system_id=str(config.system_id),
            build_profile={"schema_version": 1, "arch": "x86_64"},
            label=config.ownership_prefix,
        ),
        "create-run",
    )
    run_id = created.object_id
    ledger.record(OwnedResource(kind="run", identity=run_id))
    await build_and_upload_kernel(client, run_id=run_id)
    install = ok(await scalar(client, "runs.install", run_id=run_id), "install")
    await drain_job(client, "install", install.object_id)
    if before_activate is not None:
        before_activate(run_id)
    activate = ok(await scalar(client, "runs.boot", run_id=run_id), "activate")
    return ActivationJob(
        investigation_id=investigation_id,
        run_id=run_id,
        activate_job_id=activate.object_id,
    )
