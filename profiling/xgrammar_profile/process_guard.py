"""Subprocess isolation, timeout handling, and aggregate resident-memory supervision."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

try:
    import psutil
except ImportError:  # pragma: no cover - exercised only in broken environments
    psutil = None


@dataclass
class GuardResult:
    status: str
    command: List[str]
    exit_code: int | None
    signal: int | None
    elapsed_ns: int
    peak_rss_bytes: int
    last_rss_bytes: int
    measurement_peak_rss_bytes: int | None
    baseline_ready_observed: bool
    measurement_end_observed: bool
    measurement_end_rss_bytes: int | None
    measurement_completed_monotonic_ns: int | None
    rss_poll_interval_seconds: float
    ended_by: str
    stdout: str
    stderr_tail: str
    cgroup_peak_bytes: int | None = None
    cgroup_events: Dict[str, int] | None = None

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class _CgroupScope:
    def __init__(self, limit_bytes: int):
        self.path: Path | None = None
        self.limit_bytes = limit_bytes

    def create(self) -> bool:
        root = Path("/sys/fs/cgroup")
        if os.name != "posix" or not (root / "cgroup.controllers").is_file():
            return False
        candidate = root / f"xgrammar-profile-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            candidate.mkdir()
            (candidate / "memory.max").write_text(str(self.limit_bytes), encoding="ascii")
            swap = candidate / "memory.swap.max"
            if swap.exists():
                swap.write_text("0", encoding="ascii")
        except OSError:
            try:
                candidate.rmdir()
            except OSError:
                pass
            return False
        self.path = candidate
        return True

    def attach(self, pid: int) -> bool:
        if self.path is None:
            return False
        try:
            (self.path / "cgroup.procs").write_text(str(pid), encoding="ascii")
            return True
        except OSError:
            self.cleanup()
            return False

    def metrics(self) -> tuple[int | None, Dict[str, int] | None]:
        if self.path is None:
            return None, None
        peak: int | None = None
        events: Dict[str, int] = {}
        try:
            text = (self.path / "memory.peak").read_text(encoding="ascii").strip()
            peak = int(text)
        except (OSError, ValueError):
            pass
        try:
            for line in (self.path / "memory.events").read_text(encoding="ascii").splitlines():
                key, value = line.split()
                events[key] = int(value)
        except (OSError, ValueError):
            events = {}
        return peak, events or None

    def cleanup(self) -> None:
        if self.path is None:
            return
        try:
            self.path.rmdir()
        except OSError:
            pass
        self.path = None


def _aggregate_rss(pid: int) -> int:
    if psutil is None:
        status = Path(f"/proc/{pid}/status")
        try:
            for line in status.read_text(encoding="ascii").splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (FileNotFoundError, PermissionError, ValueError):
            return 0
        return 0
    try:
        process = psutil.Process(pid)
        try:
            processes = [process] + process.children(recursive=True)
        except (psutil.Error, OSError, PermissionError):
            # Hardened macOS sandboxes can allow per-process inspection while denying
            # the all-process sysctl used to discover descendants. Workers do not spawn
            # children in local tests; Linux authoritative runs retain aggregate polling.
            processes = [process]
    except (psutil.Error, OSError, PermissionError):
        return 0
    total = 0
    for child in processes:
        try:
            total += child.memory_info().rss
        except (psutil.Error, OSError, PermissionError):
            continue
    return total


def _terminate_group(process: subprocess.Popen[str], grace_seconds: float) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover
            process.terminate()
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover
                process.kill()
        except ProcessLookupError:
            pass


def run_guarded(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    rss_limit_bytes: int,
    poll_interval_seconds: float = 0.05,
    grace_seconds: float = 5.0,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    baseline_ready_path: Path | None = None,
    baseline_ack_path: Path | None = None,
    measurement_end_ready_path: Path | None = None,
    measurement_end_ack_path: Path | None = None,
) -> GuardResult:
    if timeout_seconds <= 0 or rss_limit_bytes <= 0 or poll_interval_seconds <= 0:
        raise ValueError("guard limits and polling interval must be positive")
    # The ready/ack files are synchronization primitives, not durable inputs.  Clear
    # both immediately before launch so a retried job cannot consume stale state.
    handshake_paths = [
        baseline_ready_path,
        baseline_ack_path,
        measurement_end_ready_path,
        measurement_end_ack_path,
    ]
    if measurement_end_ready_path is not None:
        handshake_paths.append(
            measurement_end_ready_path.with_name(f".{measurement_end_ready_path.name}.tmp")
        )
    for handshake_path in handshake_paths:
        if handshake_path is not None:
            try:
                handshake_path.unlink()
            except FileNotFoundError:
                pass
    start = time.perf_counter_ns()
    scope = _CgroupScope(rss_limit_bytes)
    scope.create()
    # File-backed streams cannot fill a bounded pipe and deadlock a verbose worker while
    # the parent is polling memory. They also avoid a reader thread perturbing measurements.
    with (
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file,
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file,
    ):
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env else None,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            start_new_session=os.name == "posix",
        )
        scope.attach(process.pid)
        peak = 0
        measurement_peak = 0
        baseline_ready_observed = False
        measurement_started_ns: int | None = None
        measurement_end_observed = False
        measurement_end_rss: int | None = None
        measurement_completed_ns: int | None = None
        post_end_deadline_ns: int | None = None
        last = 0
        requested: str | None = None
        deadline_ns = time.monotonic_ns() + int(timeout_seconds * 1e9)
        endpoint_publication_grace_ns = int(max(1.0, 2 * poll_interval_seconds) * 1e9)
        while process.poll() is None:
            last = _aggregate_rss(process.pid)
            peak = max(peak, last)
            if (
                not baseline_ready_observed
                and baseline_ready_path is not None
                and baseline_ready_path.is_file()
            ):
                baseline_ready_observed = True
                measurement_peak = last
                # Import/tokenizer/compiler setup has a separate bounded startup
                # allowance.  The experiment's full timeout begins only once the
                # worker declares its post-baseline measurement window ready.
                measurement_started_ns = time.monotonic_ns()
                deadline_ns = measurement_started_ns + int(timeout_seconds * 1e9)
                if baseline_ack_path is not None:
                    baseline_ack_path.write_text("ack\n", encoding="ascii")
            if baseline_ready_observed:
                measurement_peak = max(measurement_peak, last)
            if (
                not measurement_end_observed
                and measurement_end_ready_path is not None
                and measurement_end_ready_path.is_file()
            ):
                # The worker keeps its compiler, cache, and compiled result alive
                # until this acknowledgement.  Take a fresh endpoint sample so a
                # fast compile cannot finish entirely between polling intervals.
                try:
                    endpoint_payload = json.loads(
                        measurement_end_ready_path.read_text(encoding="utf-8")
                    )
                    worker_rss = endpoint_payload["worker_rss_bytes"]
                    completed_ns = endpoint_payload["measurement_completed_monotonic_ns"]
                    now_ns = time.monotonic_ns()
                    if (
                        not baseline_ready_observed
                        or measurement_started_ns is None
                        or not isinstance(worker_rss, int)
                        or isinstance(worker_rss, bool)
                        or worker_rss <= 0
                        or not isinstance(completed_ns, int)
                        or isinstance(completed_ns, bool)
                        or not measurement_started_ns <= completed_ns <= now_ns
                    ):
                        raise ValueError("invalid endpoint values")
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                    requested = "program_error"
                    _terminate_group(process, grace_seconds)
                    break
                last = max(_aggregate_rss(process.pid), worker_rss)
                peak = max(peak, last)
                measurement_peak = max(measurement_peak, last)
                measurement_end_observed = True
                measurement_end_rss = last
                measurement_completed_ns = completed_ns
                if last > rss_limit_bytes:
                    requested = "rss_limit"
                    _terminate_group(process, grace_seconds)
                    break
                if completed_ns > deadline_ns:
                    requested = "timeout"
                    _terminate_group(process, grace_seconds)
                    break
                if measurement_end_ack_path is not None:
                    measurement_end_ack_path.write_text("ack\n", encoding="ascii")
                drain_seconds = max(1.0, 2 * poll_interval_seconds, grace_seconds)
                post_end_deadline_ns = time.monotonic_ns() + int(drain_seconds * 1e9)
            if last > rss_limit_bytes:
                requested = "rss_limit"
                _terminate_group(process, grace_seconds)
                break
            now_ns = time.monotonic_ns()
            if not measurement_end_observed and now_ns >= deadline_ns:
                # Completion is timestamped before the worker queries RSS and
                # atomically publishes its endpoint marker.  Allow a short,
                # bounded protocol grace, but continue to judge the scientific
                # timeout against the original embedded completion timestamp.
                publication_deadline_ns = deadline_ns + endpoint_publication_grace_ns
                if (
                    not baseline_ready_observed
                    or measurement_end_ready_path is None
                    or now_ns >= publication_deadline_ns
                ):
                    requested = "timeout"
                    _terminate_group(process, grace_seconds)
                    break
            if (
                measurement_end_observed
                and post_end_deadline_ns is not None
                and now_ns >= post_end_deadline_ns
            ):
                requested = "program_error"
                _terminate_group(process, grace_seconds)
                break
            time.sleep(
                min(poll_interval_seconds, 0.005)
                if measurement_end_observed
                else poll_interval_seconds
            )
        process.wait()
        stdout_file.flush()
        stderr_file.flush()
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()
    last = _aggregate_rss(process.pid) or last
    peak = max(peak, last)
    cgroup_peak, events = scope.metrics()
    if cgroup_peak is not None:
        peak = max(peak, cgroup_peak)
    scope.cleanup()
    exit_code = process.returncode
    signum = -exit_code if exit_code is not None and exit_code < 0 else None
    oom_kill = bool(events and events.get("oom_kill", 0) > 0)
    if requested is not None:
        status, ended_by = requested, "parent"
    elif oom_kill:
        status, ended_by = "rss_limit", "cgroup"
    elif exit_code == 0:
        status, ended_by = "success", "program"
    elif signum is not None:
        status, ended_by = "kernel_termination", "kernel"
    else:
        status, ended_by = "program_error", "program"
    return GuardResult(
        status=status,
        command=list(command),
        exit_code=exit_code,
        signal=signum,
        elapsed_ns=time.perf_counter_ns() - start,
        peak_rss_bytes=peak,
        last_rss_bytes=last,
        measurement_peak_rss_bytes=measurement_peak if baseline_ready_observed else None,
        baseline_ready_observed=baseline_ready_observed,
        measurement_end_observed=measurement_end_observed,
        measurement_end_rss_bytes=measurement_end_rss,
        measurement_completed_monotonic_ns=measurement_completed_ns,
        rss_poll_interval_seconds=poll_interval_seconds,
        ended_by=ended_by,
        stdout=stdout,
        stderr_tail=stderr[-8192:],
        cgroup_peak_bytes=cgroup_peak,
        cgroup_events=events,
    )


def watchdog_self_test(python_executable: str, *, workdir: Path) -> Dict[str, bool]:
    timeout = run_guarded(
        [python_executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=0.15,
        rss_limit_bytes=256 * 1024 * 1024,
        poll_interval_seconds=0.02,
        grace_seconds=0.1,
        cwd=workdir,
    )
    memory = run_guarded(
        [python_executable, "-c", "import time; x=bytearray(96*1024*1024); time.sleep(2)"],
        timeout_seconds=5,
        rss_limit_bytes=48 * 1024 * 1024,
        poll_interval_seconds=0.01,
        grace_seconds=0.1,
        cwd=workdir,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        baseline_ready = root / "baseline-ready"
        baseline_ack = root / "baseline-ack"
        end_ready = root / "end-ready"
        end_ack = root / "end-ack"
        end_temporary = root / "end-ready.tmp"
        endpoint_script = (
            "import json,os,pathlib,psutil,time; "
            f"pathlib.Path({str(baseline_ready)!r}).write_text('ready'); "
            f"p=pathlib.Path({str(baseline_ack)!r}); "
            "exec('while not p.exists():\\n  time.sleep(.002)'); "
            "x=bytearray(64*1024*1024); "
            "exec('for i in range(0,len(x),4096):\\n  x[i]=1'); "
            "completed=time.monotonic_ns(); "
            f"q=pathlib.Path({str(end_temporary)!r}); "
            "q.write_text(json.dumps({'worker_rss_bytes':"
            "psutil.Process(os.getpid()).memory_info().rss,"
            "'measurement_completed_monotonic_ns':completed})); "
            f"q.replace(pathlib.Path({str(end_ready)!r})); "
            f"p=pathlib.Path({str(end_ack)!r}); "
            "exec('while not p.exists():\\n  time.sleep(.002)')"
        )
        endpoint = run_guarded(
            [python_executable, "-c", endpoint_script],
            timeout_seconds=2,
            rss_limit_bytes=256 * 1024 * 1024,
            poll_interval_seconds=0.2,
            grace_seconds=0.1,
            cwd=workdir,
            baseline_ready_path=baseline_ready,
            baseline_ack_path=baseline_ack,
            measurement_end_ready_path=end_ready,
            measurement_end_ack_path=end_ack,
        )
    return {
        "timeout": timeout.status == "timeout",
        "rss_limit": memory.status == "rss_limit",
        "retained_endpoint": bool(
            endpoint.status == "success"
            and endpoint.measurement_end_observed
            and endpoint.measurement_peak_rss_bytes is not None
            and endpoint.measurement_peak_rss_bytes > 48 * 1024 * 1024
        ),
    }
