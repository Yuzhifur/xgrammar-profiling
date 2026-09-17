import sys
import tempfile
import unittest
from pathlib import Path

from xgrammar_profile.process_guard import run_guarded


class ProcessGuardTests(unittest.TestCase):
    def _endpoint_result(
        self, *, delay, malformed=False, timestamp_before_delay=False, timeout=0.05, poll=0.15
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline_ready = root / "baseline-ready"
            baseline_ack = root / "baseline-ack"
            end_ready = root / "end-ready"
            end_ack = root / "end-ack"
            end_temporary = root / "end-ready.tmp"
            payload = (
                "'{'"
                if malformed
                else "json.dumps({'worker_rss_bytes':1,"
                "'measurement_completed_monotonic_ns':completed})"
            )
            completion_steps = (
                f"completed=time.monotonic_ns(); time.sleep({delay!r}); "
                if timestamp_before_delay
                else f"time.sleep({delay!r}); completed=time.monotonic_ns(); "
            )
            script = "".join(
                [
                    "import json,pathlib,time; ",
                    f"pathlib.Path({str(baseline_ready)!r}).write_text('ready'); ",
                    f"p=pathlib.Path({str(baseline_ack)!r}); ",
                    "exec('while not p.exists():\\n  time.sleep(.002)'); ",
                    completion_steps,
                    f"q=pathlib.Path({str(end_temporary)!r}); q.write_text({payload}); ",
                    f"q.replace(pathlib.Path({str(end_ready)!r})); ",
                    f"p=pathlib.Path({str(end_ack)!r}); ",
                    "exec('while not p.exists():\\n  time.sleep(.002)')",
                ]
            )
            return run_guarded(
                [sys.executable, "-c", script],
                timeout_seconds=timeout,
                rss_limit_bytes=128 * 1024 * 1024,
                poll_interval_seconds=poll,
                grace_seconds=0.03,
                cwd=root,
                baseline_ready_path=baseline_ready,
                baseline_ack_path=baseline_ack,
                measurement_end_ready_path=end_ready,
                measurement_end_ack_path=end_ack,
            )

    def test_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_guarded(
                [sys.executable, "-c", "print('ok')"],
                timeout_seconds=2,
                rss_limit_bytes=128 * 1024 * 1024,
                poll_interval_seconds=0.01,
                grace_seconds=0.05,
                cwd=Path(directory),
            )
        self.assertEqual(result.status, "success", result)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_timeout_kills_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_guarded(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout_seconds=0.08,
                rss_limit_bytes=128 * 1024 * 1024,
                poll_interval_seconds=0.01,
                grace_seconds=0.05,
                cwd=Path(directory),
            )
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.ended_by, "parent")

    def test_large_stdout_cannot_fill_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_guarded(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * (2 * 1024 * 1024))"],
                timeout_seconds=3,
                rss_limit_bytes=128 * 1024 * 1024,
                poll_interval_seconds=0.01,
                grace_seconds=0.05,
                cwd=Path(directory),
            )
        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.stdout), 2 * 1024 * 1024)

    def test_post_baseline_peak_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"
            ack = root / "ack"
            script = (
                "import json,pathlib,time; "
                f"pathlib.Path({str(ready)!r}).write_text('ready'); "
                f"p=pathlib.Path({str(ack)!r}); "
                "deadline=time.monotonic()+1; "
                "exec('while not p.exists():\\n  assert time.monotonic()<deadline\\n  time.sleep(.002)'); "
                "x=bytearray(24*1024*1024); time.sleep(.15)"
            )
            result = run_guarded(
                [sys.executable, "-c", script],
                timeout_seconds=2,
                rss_limit_bytes=128 * 1024 * 1024,
                poll_interval_seconds=0.01,
                grace_seconds=0.05,
                cwd=root,
                baseline_ready_path=ready,
                baseline_ack_path=ack,
            )
        self.assertTrue(result.baseline_ready_observed)
        self.assertIsNotNone(result.measurement_peak_rss_bytes)
        self.assertGreater(result.measurement_peak_rss_bytes, 16 * 1024 * 1024)

    def test_measurement_timeout_starts_after_delayed_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"
            ack = root / "ack"
            script = (
                "import pathlib,time; time.sleep(.25); "
                f"pathlib.Path({str(ready)!r}).write_text('ready'); "
                f"p=pathlib.Path({str(ack)!r}); "
                "exec('while not p.exists():\\n  time.sleep(.002)'); "
                "time.sleep(.25)"
            )
            result = run_guarded(
                [sys.executable, "-c", script],
                # Each phase fits comfortably, while their combined 0.5 s exceeds
                # this limit and therefore proves that the handshake resets it.
                timeout_seconds=0.4,
                rss_limit_bytes=128 * 1024 * 1024,
                poll_interval_seconds=0.005,
                grace_seconds=0.05,
                cwd=root,
                baseline_ready_path=ready,
                baseline_ack_path=ack,
            )
        self.assertEqual(result.status, "success")
        self.assertTrue(result.baseline_ready_observed)

    def test_fast_allocation_is_captured_by_retained_endpoint_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline_ready = root / "baseline-ready"
            baseline_ack = root / "baseline-ack"
            end_ready = root / "end-ready"
            end_ack = root / "end-ack"
            end_temporary = root / "end-ready.tmp"
            script = (
                "import json,os,pathlib,psutil,time; "
                f"pathlib.Path({str(baseline_ready)!r}).write_text('ready'); "
                f"p=pathlib.Path({str(baseline_ack)!r}); "
                "exec('while not p.exists():\\n  time.sleep(.002)'); "
                "x=bytearray(96*1024*1024); "
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
            result = run_guarded(
                [sys.executable, "-c", script],
                timeout_seconds=2,
                rss_limit_bytes=256 * 1024 * 1024,
                # The worker finishes allocating before this ordinary polling
                # interval elapses, then stays live at the endpoint barrier.
                poll_interval_seconds=0.25,
                grace_seconds=0.05,
                cwd=root,
                baseline_ready_path=baseline_ready,
                baseline_ack_path=baseline_ack,
                measurement_end_ready_path=end_ready,
                measurement_end_ack_path=end_ack,
            )
        self.assertEqual(result.status, "success", result)
        self.assertTrue(result.measurement_end_observed)
        self.assertIsNotNone(result.measurement_peak_rss_bytes)
        self.assertGreater(result.measurement_peak_rss_bytes, 64 * 1024 * 1024)
        self.assertGreater(result.measurement_end_rss_bytes, 64 * 1024 * 1024)

    def test_on_time_endpoint_observed_after_poll_deadline_succeeds(self):
        result = self._endpoint_result(delay=0)
        self.assertEqual(result.status, "success", result)
        self.assertTrue(result.measurement_end_observed)

    def test_on_time_completion_survives_slow_endpoint_publication(self):
        result = self._endpoint_result(
            delay=0.08, timestamp_before_delay=True, timeout=0.05, poll=0.01
        )
        self.assertEqual(result.status, "success", result)
        self.assertTrue(result.measurement_end_observed)

    def test_late_endpoint_timestamp_is_timeout(self):
        result = self._endpoint_result(delay=0.08)
        self.assertEqual(result.status, "timeout", result)
        self.assertTrue(result.measurement_end_observed)

    def test_malformed_endpoint_is_fatal(self):
        result = self._endpoint_result(delay=0, malformed=True, timeout=0.5, poll=0.05)
        self.assertEqual(result.status, "program_error", result)
        self.assertFalse(result.measurement_end_observed)

    def test_slow_exit_after_endpoint_ack_is_still_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline_ready = root / "baseline-ready"
            baseline_ack = root / "baseline-ack"
            end_ready = root / "end-ready"
            end_ack = root / "end-ack"
            end_temporary = root / "end-ready.tmp"
            script = "".join(
                [
                    "import json,pathlib,time; ",
                    f"pathlib.Path({str(baseline_ready)!r}).write_text('ready'); ",
                    f"p=pathlib.Path({str(baseline_ack)!r}); ",
                    "exec('while not p.exists():\\n  time.sleep(.002)'); ",
                    "completed=time.monotonic_ns(); ",
                    f"q=pathlib.Path({str(end_temporary)!r}); ",
                    "q.write_text(json.dumps({'worker_rss_bytes':1,"
                    "'measurement_completed_monotonic_ns':completed})); ",
                    f"q.replace(pathlib.Path({str(end_ready)!r})); ",
                    f"p=pathlib.Path({str(end_ack)!r}); ",
                    "exec('while not p.exists():\\n  time.sleep(.002)'); ",
                    # Teardown slower than the old 1-second drain floor and the grace value.
                    "time.sleep(1.5)",
                ]
            )
            result = run_guarded(
                [sys.executable, "-c", script],
                timeout_seconds=5,
                rss_limit_bytes=256 * 1024 * 1024,
                poll_interval_seconds=0.05,
                grace_seconds=0.05,
                cwd=root,
                baseline_ready_path=baseline_ready,
                baseline_ack_path=baseline_ack,
                measurement_end_ready_path=end_ready,
                measurement_end_ack_path=end_ack,
            )
        self.assertEqual(result.status, "success", result)
        self.assertTrue(result.measurement_end_observed)

    def test_startup_without_handshake_remains_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_guarded(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout_seconds=0.08,
                rss_limit_bytes=128 * 1024 * 1024,
                poll_interval_seconds=0.005,
                grace_seconds=0.03,
                cwd=root,
                baseline_ready_path=root / "ready",
                baseline_ack_path=root / "ack",
            )
        self.assertEqual(result.status, "timeout")
        self.assertFalse(result.baseline_ready_observed)

    def test_rss_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_guarded(
                [sys.executable, "-c", "import time; x=bytearray(64*1024*1024); time.sleep(2)"],
                timeout_seconds=3,
                rss_limit_bytes=32 * 1024 * 1024,
                poll_interval_seconds=0.01,
                grace_seconds=0.05,
                cwd=Path(directory),
            )
        self.assertEqual(result.status, "rss_limit")
        limit = 32 * 1024 * 1024
        self.assertGreaterEqual(result.peak_rss_bytes, limit)
        if result.ended_by == "cgroup":
            self.assertIsNotNone(result.cgroup_events)
            self.assertGreater(result.cgroup_events.get("oom_kill", 0), 0)
        else:
            self.assertEqual(result.ended_by, "parent")
            self.assertGreater(result.peak_rss_bytes, limit)


if __name__ == "__main__":
    unittest.main()
