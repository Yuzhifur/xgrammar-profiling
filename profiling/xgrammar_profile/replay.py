"""Private testing-hook adapters and matcher replay signatures."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List


class ProfilingCapabilityError(RuntimeError):
    pass


def current_rss_bytes() -> int:
    try:
        import psutil

        return int(psutil.Process().memory_info().rss)
    except ImportError:
        try:
            with open(f"/proc/{os.getpid()}/status", encoding="ascii") as stream:
                for line in stream:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except (FileNotFoundError, PermissionError, ValueError):
            pass
    return 0


@contextlib.contextmanager
def worker_stdout_guard() -> Iterator[None]:
    """Route every stdout write made inside the block to stderr.

    Worker stdout is parsed as JSONL evidence.  A stray library print, whether from Python
    or from native code, would otherwise turn a valid measurement into a fatal
    ``invalid_output`` outcome for the entire run.
    """
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def baseline_handshake(job: Dict[str, Any], baseline_rss_bytes: int) -> None:
    """Pause the worker until the supervisor opens its post-baseline RSS window."""
    ready_value = job.get("baseline_ready_path")
    ack_value = job.get("baseline_ack_path")
    if not ready_value or not ack_value:
        return
    ready, ack = Path(ready_value), Path(ack_value)
    ready.write_text(
        json.dumps({"baseline_rss_bytes": baseline_rss_bytes}) + "\n", encoding="utf-8"
    )
    deadline = time.monotonic() + 10
    while not ack.is_file():
        if time.monotonic() >= deadline:
            raise ProfilingCapabilityError("supervisor did not acknowledge the RSS baseline")
        time.sleep(0.005)


def measurement_end_handshake(job: Dict[str, Any]) -> None:
    """Keep the completed compiler/result live until the supervisor samples RSS."""
    ready_value = job.get("measurement_end_ready_path")
    ack_value = job.get("measurement_end_ack_path")
    if not ready_value or not ack_value:
        return
    ready, ack = Path(ready_value), Path(ack_value)
    # Capture benchmark completion before doing endpoint-protocol work so a slow
    # RSS query cannot turn an on-time compilation into a timeout.
    completed_ns = time.monotonic_ns()
    payload = {
        "worker_rss_bytes": current_rss_bytes(),
        # CLOCK_MONOTONIC is shared by parent and child, unlike perf_counter's
        # unspecified reference point on all supported Python versions.
        "measurement_completed_monotonic_ns": completed_ns,
    }
    temporary = ready.with_name(f".{ready.name}.tmp")
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(temporary, ready)
    deadline = time.monotonic() + 10
    while not ack.is_file():
        if time.monotonic() >= deadline:
            raise ProfilingCapabilityError("supervisor did not acknowledge the final RSS sample")
        time.sleep(0.005)


def _testing_function(name: str) -> Callable[..., Any] | None:
    try:
        from xgrammar import testing
    except ImportError:
        return None
    function = getattr(testing, name, None)
    return function if callable(function) else None


def compiler_hook(compiler: Any, name: str, *, required: bool = False) -> Any:
    method = getattr(compiler, name, None)
    if callable(method):
        try:
            return method()
        except (AttributeError, RuntimeError, TypeError) as exc:
            if required:
                raise ProfilingCapabilityError(
                    f"required profiling API `{name}` failed: {exc}"
                ) from exc
            return None
    function = _testing_function(name)
    if function is not None:
        try:
            return function(compiler)
        except (AttributeError, RuntimeError, TypeError) as exc:
            if required:
                raise ProfilingCapabilityError(
                    f"required profiling API `{name}` failed: {exc}"
                ) from exc
            return None
    if required:
        raise ProfilingCapabilityError(
            f"selected build lacks required private profiling API `{name}`; rebuild with "
            "XGRAMMAR_ENABLE_PROFILING_API=ON"
        )
    return None


def compiled_hook(compiled: Any, name: str = "get_compiled_grammar_stats") -> Any:
    method = getattr(compiled, name, None)
    if callable(method):
        try:
            return method()
        except (AttributeError, RuntimeError, TypeError):
            return None
    function = _testing_function(name)
    if function is not None:
        try:
            return function(compiled)
        except (AttributeError, RuntimeError, TypeError):
            return None
    return None


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return repr(value)


def profiling_snapshot(
    compiler: Any, compiled: Any | None = None, *, detailed: bool = False
) -> Dict[str, Any]:
    result = {
        "rule_cache_size_bytes": compiler_hook(compiler, "get_rule_cache_size_bytes"),
        "grammar_cache_size_bytes": compiler_hook(compiler, "get_grammar_cache_size_bytes"),
    }
    if detailed:
        result["profiling_stats"] = compiler_hook(compiler, "get_profiling_stats")
    if detailed and compiled is not None:
        result["compiled_grammar_stats"] = compiled_hook(compiled)
    return json_safe(result)


def serialize_structural_tag(tag: Any) -> str:
    if hasattr(tag, "model_dump_json"):
        return str(tag.model_dump_json())
    if hasattr(tag, "json"):
        return str(tag.json())
    if isinstance(tag, str):
        return tag
    return json.dumps(tag, separators=(",", ":"), ensure_ascii=False)


def matcher_signature(
    compiled: Any, text: str, token_ids: Iterable[int] | None = None
) -> Dict[str, Any]:
    import xgrammar as xgr

    string_matcher = xgr.GrammarMatcher(compiled, terminate_without_stop_token=True)
    string_accepted = bool(string_matcher.accept_string(text))
    signature: Dict[str, Any] = {
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "string_accepted": string_accepted,
        "string_terminated": bool(string_matcher.is_terminated()),
    }
    if token_ids is None:
        return signature
    token_matcher = xgr.GrammarMatcher(compiled, terminate_without_stop_token=True)
    vocab_size = compiled.tokenizer_info.vocab_size
    bitmask = xgr.allocate_token_bitmask(1, vocab_size)
    masks: List[str] = []
    applies: List[bool] = []
    accepts: List[bool] = []
    replay_times: List[int] = []
    for token_id in token_ids:
        xgr.reset_token_bitmask(bitmask)
        started = time.perf_counter_ns()
        applies.append(bool(token_matcher.fill_next_token_bitmask(bitmask)))
        accepts.append(bool(token_matcher.accept_token(int(token_id))))
        replay_times.append(time.perf_counter_ns() - started)
        # Hashing is correctness bookkeeping and deliberately outside replay timing.
        masks.append(hashlib.sha256(bitmask.numpy().tobytes()).hexdigest())
    xgr.reset_token_bitmask(bitmask)
    applies.append(bool(token_matcher.fill_next_token_bitmask(bitmask)))
    masks.append(hashlib.sha256(bitmask.numpy().tobytes()).hexdigest())
    signature["tokens"] = {
        "ids": list(token_ids),
        "mask_sha256": masks,
        "need_apply": applies,
        "accepted": accepts,
        "terminated": bool(token_matcher.is_terminated()),
        "per_token_time_ns": replay_times,
        "median_time_ns": statistics.median(replay_times) if replay_times else None,
        "p95_time_ns": (
            sorted(replay_times)[
                max(0, min(len(replay_times) - 1, int(0.95 * len(replay_times)) - 1))
            ]
            if replay_times
            else None
        ),
    }
    return signature
