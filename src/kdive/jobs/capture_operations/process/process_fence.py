"""Exact process-membership attestation and signaling for capture launches."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from kdive.jobs.capture_operations.process.linux_identity import LinuxIdentity, scan_launch_token

_SIGNAL_WAIT_SECONDS = 5.0


class _ProcessMembershipChanged(ProcessLookupError):
    """A sampled process-group member vanished, moved, or reused its PID."""


def _parse_process_stat(pid: int, stat_line: str) -> tuple[int, int]:
    closing = stat_line.rfind(")")
    fields = stat_line[closing + 2 :].split() if closing > 1 else []
    if len(fields) <= 19:
        raise RuntimeError(f"malformed /proc/{pid}/stat during capture handoff")
    try:
        process_group = int(fields[2])
        start_ticks = int(fields[19])
    except ValueError as error:
        raise RuntimeError(f"malformed /proc/{pid}/stat during capture handoff") from error
    if start_ticks < 0:
        raise RuntimeError(f"malformed /proc/{pid}/stat during capture handoff")
    return process_group, start_ticks


def _identity_from_group_stat(
    pid: int,
    stat_line: str,
    *,
    process_group: int,
    host_instance: str,
) -> LinuxIdentity | None:
    observed_group, observed_start = _parse_process_stat(pid, stat_line)
    if observed_group != process_group:
        return None
    try:
        identity = LinuxIdentity.read(pid, host_instance=host_instance)
    except ProcessLookupError as error:
        raise _ProcessMembershipChanged(f"process-group member {pid} vanished") from error
    if identity.start_ticks != observed_start:
        raise _ProcessMembershipChanged(f"process-group member {pid} reused its identity")
    return identity


def _read_process_group_member(
    pid: int,
    *,
    process_group: int,
    host_instance: str,
    proc_root: Path = Path("/proc"),
) -> LinuxIdentity | None:
    try:
        stat_line = (proc_root / str(pid) / "stat").read_text()
    except FileNotFoundError as error:
        raise _ProcessMembershipChanged(f"process-group member {pid} vanished") from error
    except OSError as error:
        raise RuntimeError(f"cannot read /proc/{pid}/stat during capture handoff") from error
    return _identity_from_group_stat(
        pid,
        stat_line,
        process_group=process_group,
        host_instance=host_instance,
    )


def _process_group_members(
    process_group: int,
    proc_root: Path = Path("/proc"),
    *,
    host_instance: str,
) -> dict[int, LinuxIdentity]:
    members: dict[int, LinuxIdentity] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError as error:
        raise RuntimeError("cannot enumerate /proc for capture child handoff") from error
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            stat_line = (entry / "stat").read_text()
        except FileNotFoundError, ProcessLookupError:
            continue
        except OSError as error:
            raise RuntimeError(f"cannot read {entry}/stat during capture handoff") from error
        pid = int(entry.name)
        identity = _identity_from_group_stat(
            pid,
            stat_line,
            process_group=process_group,
            host_instance=host_instance,
        )
        if identity is not None:
            members[pid] = identity
    return members


def _task_members(pid: int, proc_root: Path = Path("/proc")) -> set[int]:
    try:
        return {int(entry.name) for entry in (proc_root / str(pid) / "task").iterdir()}
    except OSError as error:
        raise RuntimeError(f"cannot enumerate /proc/{pid}/task during capture handoff") from error


type _ProcessHandle = tuple[LinuxIdentity, int]


def _attest_process_members(
    members: Mapping[int, LinuxIdentity],
    *,
    process_group: int,
    host_instance: str,
    existing: dict[int, _ProcessHandle],
) -> dict[int, _ProcessHandle]:
    """Open and revalidate every observed member before any member is signaled."""
    handles = dict(existing)
    opened: list[int] = []
    try:
        for pid, observed in sorted(members.items()):
            prior = handles.get(pid)
            if prior is not None:
                if prior[0] != observed:
                    raise _ProcessMembershipChanged(
                        f"process-group member {pid} reused its identity"
                    )
                continue
            try:
                pidfd = observed.open_pidfd(current_host_instance=host_instance)
                opened.append(pidfd)
                current = _read_process_group_member(
                    pid,
                    process_group=process_group,
                    host_instance=host_instance,
                )
            except ProcessLookupError as error:
                raise _ProcessMembershipChanged(
                    f"process-group member {pid} changed before cleanup"
                ) from error
            if current != observed:
                raise _ProcessMembershipChanged(
                    f"process-group member {pid} changed before cleanup"
                )
            handles[pid] = (observed, pidfd)
    except BaseException:
        for pidfd in opened:
            os.close(pidfd)
        raise
    return handles


async def _wait_for_pidfd_ready(pidfd: int) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def _mark_ready() -> None:
        if not ready.done():
            ready.set_result(None)

    loop.add_reader(pidfd, _mark_ready)
    try:
        await ready
    finally:
        loop.remove_reader(pidfd)


def _close_process_handles(handles: Mapping[int, _ProcessHandle]) -> None:
    for _identity, pidfd in handles.values():
        os.close(pidfd)


async def _signal_and_wait_exact(
    handles: dict[int, _ProcessHandle], process: asyncio.subprocess.Process
) -> None:
    """SIGKILL exact pidfds and await every member plus the leader for five seconds total."""
    for identity, pidfd in handles.values():
        with suppress(ProcessLookupError):
            identity.signal(pidfd, signal.SIGKILL)
    waits = [_wait_for_pidfd_ready(pidfd) for _identity, pidfd in handles.values()]
    waits.append(process.wait())
    try:
        await asyncio.wait_for(asyncio.gather(*waits), timeout=_SIGNAL_WAIT_SECONDS)
    except TimeoutError as error:
        raise RuntimeError("capture launch cleanup exceeded 5 seconds") from error


async def _complete_token_scan(
    launch_token: str, *, interpreter: Path, host_instance: str
) -> tuple[LinuxIdentity, ...]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                scan_launch_token,
                launch_token,
                interpreter=interpreter,
                host_instance=host_instance,
            ),
            timeout=_SIGNAL_WAIT_SECONDS,
        )
    except TimeoutError as error:
        raise RuntimeError("complete launch-token scan exceeded 5 seconds") from error


async def _complete_process_group_scan(
    process_group: int,
    *,
    host_instance: str,
) -> dict[int, LinuxIdentity]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _process_group_members,
                process_group,
                host_instance=host_instance,
            ),
            timeout=_SIGNAL_WAIT_SECONDS,
        )
    except TimeoutError as error:
        raise RuntimeError("complete process-group scan exceeded 5 seconds") from error


def _acquire_token_handle(
    identity: LinuxIdentity,
    *,
    host_instance: str,
    handles: dict[int, _ProcessHandle],
    observed: Mapping[int, LinuxIdentity],
) -> int | None:
    prior_observation = observed.get(identity.pid)
    if prior_observation is not None and prior_observation != identity:
        raise _ProcessMembershipChanged(f"launch-token recovery observed reused pid {identity.pid}")
    prior = handles.get(identity.pid)
    if prior is not None and prior[0] == identity:
        return None
    if prior is not None:
        raise RuntimeError("launch-token recovery observed a reused process identity")
    try:
        pidfd = identity.open_pidfd(current_host_instance=host_instance)
    except ProcessLookupError:
        return None
    handles[identity.pid] = (identity, pidfd)
    return pidfd


async def _token_recovery_handles(
    launch_token: str,
    *,
    interpreter: Path,
    host_instance: str,
    existing: dict[int, _ProcessHandle],
    observed: Mapping[int, LinuxIdentity],
    matches: tuple[LinuxIdentity, ...] | None = None,
) -> dict[int, _ProcessHandle]:
    if matches is None:
        matches = await _complete_token_scan(
            launch_token,
            interpreter=interpreter,
            host_instance=host_instance,
        )
    handles = dict(existing)
    opened: list[int] = []
    try:
        for identity in matches:
            pidfd = _acquire_token_handle(
                identity,
                host_instance=host_instance,
                handles=handles,
                observed=observed,
            )
            if pidfd is not None:
                opened.append(pidfd)
    except BaseException:
        for pidfd in opened:
            os.close(pidfd)
        raise
    return handles


def _attest_observed_members(
    members: Mapping[int, LinuxIdentity] | None,
    *,
    process_group: int,
    host_instance: str,
    handles: dict[int, _ProcessHandle],
) -> tuple[dict[int, _ProcessHandle], bool]:
    if members is None:
        return handles, False
    try:
        return (
            _attest_process_members(
                members,
                process_group=process_group,
                host_instance=host_instance,
                existing=handles,
            ),
            False,
        )
    except _ProcessMembershipChanged:
        return handles, True


def _verify_recovered_members(
    recovered: Mapping[int, LinuxIdentity], observed: Mapping[int, LinuxIdentity]
) -> None:
    for pid, identity in recovered.items():
        prior_observation = observed.get(pid)
        if prior_observation is not None and prior_observation != identity:
            raise _ProcessMembershipChanged(f"process-group recovery observed reused pid {pid}")


async def _acquire_recovery_handles(
    process_group: int,
    *,
    launch_token: str,
    interpreter: Path,
    host_instance: str,
    handles: dict[int, _ProcessHandle],
    observed: Mapping[int, LinuxIdentity],
    recover_group: bool,
) -> dict[int, _ProcessHandle]:
    caller_owned = set(handles)
    token_matches: tuple[LinuxIdentity, ...] | None = None
    try:
        if recover_group:
            recovered_members = await _complete_process_group_scan(
                process_group,
                host_instance=host_instance,
            )
            token_matches = await _complete_token_scan(
                launch_token,
                interpreter=interpreter,
                host_instance=host_instance,
            )
            _verify_recovered_members(recovered_members, observed)
            handles = _attest_process_members(
                recovered_members,
                process_group=process_group,
                host_instance=host_instance,
                existing=handles,
            )
        return await _token_recovery_handles(
            launch_token,
            interpreter=interpreter,
            host_instance=host_instance,
            existing=handles,
            observed=observed,
            matches=token_matches,
        )
    except BaseException:
        helper_owned = {pid: handle for pid, handle in handles.items() if pid not in caller_owned}
        _close_process_handles(helper_owned)
        raise


async def _confirm_launch_absence(
    process_group: int,
    *,
    launch_token: str,
    interpreter: Path,
    host_instance: str,
    recover_group: bool,
) -> None:
    remaining = await _complete_token_scan(
        launch_token,
        interpreter=interpreter,
        host_instance=host_instance,
    )
    remaining_group: Mapping[int, LinuxIdentity] = {}
    if recover_group:
        remaining_group = await _complete_process_group_scan(
            process_group,
            host_instance=host_instance,
        )
    if remaining:
        raise RuntimeError("complete launch-token scan still finds capture children")
    if remaining_group:
        raise RuntimeError("complete process-group scan still finds capture children")
