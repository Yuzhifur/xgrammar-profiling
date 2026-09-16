"""Capture enough host metadata to interpret performance measurements."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable


def stable_machine_identity() -> Dict[str, Any]:
    """Return stable host/topology fields suitable for pilot-to-run binding."""
    identity: Dict[str, Any] = {"system": platform.system(), "machine": platform.machine()}
    selected_lscpu = {
        "Architecture",
        "CPU(s)",
        "On-line CPU(s) list",
        "Thread(s) per core",
        "Core(s) per socket",
        "Socket(s)",
        "NUMA node(s)",
        "Vendor ID",
        "Model name",
        "CPU family",
        "Model",
        "Stepping",
        "Hypervisor vendor",
        "Virtualization type",
    }
    if shutil.which("lscpu"):
        completed = subprocess.run(
            ["lscpu"], text=True, capture_output=True, timeout=15, check=False
        )
        if completed.returncode == 0:
            identity["lscpu"] = {
                key.strip(): value.strip()
                for line in completed.stdout.splitlines()
                if ":" in line
                for key, value in [line.split(":", 1)]
                if key.strip() in selected_lscpu
            }
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                identity["mem_total_kib"] = int(line.split()[1])
                break
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    dmi: Dict[str, str] = {}
    for field in ("sys_vendor", "product_name", "product_uuid"):
        try:
            dmi[field] = Path(f"/sys/class/dmi/id/{field}").read_text(encoding="ascii").strip()
        except (FileNotFoundError, PermissionError, UnicodeDecodeError):
            pass
    if dmi:
        identity["dmi"] = dmi
    if shutil.which("systemd-detect-virt"):
        completed = subprocess.run(
            ["systemd-detect-virt"], text=True, capture_output=True, timeout=15, check=False
        )
        identity["virtualization"] = completed.stdout.strip() or "none"
    return identity


def machine_identity_fingerprint(identity: Dict[str, Any] | None = None) -> str:
    payload = identity if identity is not None else stable_machine_identity()
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def require_machine_identity(expected: Dict[str, Any], actual: Dict[str, Any]) -> None:
    if machine_identity_fingerprint(expected) != machine_identity_fingerprint(actual):
        raise ValueError("current machine identity/topology differs from the frozen pilot host")


def _command(command: Iterable[str]) -> Dict[str, Any]:
    argv = list(command)
    if shutil.which(argv[0]) is None:
        return {"command": argv, "available": False}
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": argv, "available": True, "error": str(exc)}
    return {
        "command": argv,
        "available": True,
        "returncode": result.returncode,
        "stdout": result.stdout[-50000:],
        "stderr": result.stderr[-10000:],
    }


def capture_environment() -> Dict[str, Any]:
    commands = [
        ["uname", "-a"],
        ["lscpu"],
        ["free", "-b"],
        ["df", "-B1", "."],
        ["systemd-detect-virt"],
        ["cmake", "--version"],
        ["ninja", "--version"],
        ["c++", "--version"],
        ["ld", "--version"],
    ]
    stable_identity = stable_machine_identity()
    result: Dict[str, Any] = {
        "captured_at_unix_ns": time.time_ns(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
        "python_executable": sys.executable,
        "cpu_count": os.cpu_count(),
        "cpu_affinity": (
            sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
        ),
        "stable_machine_identity": stable_identity,
        "stable_machine_fingerprint": machine_identity_fingerprint(stable_identity),
        "commands": {" ".join(command): _command(command) for command in commands},
    }
    for path in (
        Path("/proc/cpuinfo"),
        Path("/proc/meminfo"),
        Path("/proc/cmdline"),
        Path("/proc/stat"),
    ):
        try:
            result[path.as_posix()] = path.read_text(encoding="utf-8")[-200000:]
        except (FileNotFoundError, PermissionError, UnicodeDecodeError):
            pass
    return result


def available_cpu_ids() -> list[int]:
    if hasattr(os, "sched_getaffinity"):
        return sorted(os.sched_getaffinity(0))
    return list(range(os.cpu_count() or 1))


def physical_cpu_ids(*, preferred: int | None = None) -> list[int]:
    """Choose one allowed logical CPU from each physical package/core pair."""
    available = available_cpu_ids()
    groups: Dict[tuple[str, str], list[int]] = {}
    for cpu in available:
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        try:
            package = (topology / "physical_package_id").read_text(encoding="ascii").strip()
            core = (topology / "core_id").read_text(encoding="ascii").strip()
            key = (package, core)
        except (FileNotFoundError, PermissionError):
            key = ("logical", str(cpu))
        groups.setdefault(key, []).append(cpu)
    selected = [min(cpus) for _, cpus in sorted(groups.items())]
    if preferred is not None:
        if preferred not in available:
            raise ValueError(f"requested CPU {preferred} is not in allowed affinity {available}")
        preferred_key = next(key for key, cpus in groups.items() if preferred in cpus)
        selected = [cpu for cpu in selected if cpu not in groups[preferred_key]]
        selected.insert(0, preferred)
    return selected


def steal_ticks(cpu_affinity: Iterable[int] | None = None) -> int | None:
    try:
        lines = Path("/proc/stat").read_text(encoding="ascii").splitlines()
        requested = list(cpu_affinity) if cpu_affinity is not None else None
        labels = {f"cpu{cpu}" for cpu in requested} if requested is not None else {"cpu"}
        values = []
        for line in lines:
            parts = line.split()
            if parts and parts[0] in labels and len(parts) >= 9:
                values.append(int(parts[8]))
        if len(values) != len(labels):
            return None
        return sum(values)
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return None


def perf_preflight() -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="xgrammar-perf-") as directory:
        perf_data = str(Path(directory) / "perf.data")
        return {
            "hardware": _command(["perf", "stat", "-e", "cycles,instructions", "true"]),
            "software_sampling": _command(
                ["perf", "record", "-o", perf_data, "-e", "cpu-clock", "-g", "--", "true"]
            ),
        }
