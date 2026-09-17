import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xgrammar_profile.environment import machine_identity_fingerprint
from xgrammar_profile.suite import SuiteError, _update_terminal_censor, run_authoritative, run_pilot


class SuiteStateTests(unittest.TestCase):
    def test_warmup_censor_does_not_count_toward_three_confirmations(self):
        count = _update_terminal_censor(0, "timeout", measured=False)
        self.assertEqual(count, 0)
        count = _update_terminal_censor(count, "timeout", measured=True)
        self.assertEqual(count, 1)
        count = _update_terminal_censor(count, "rss_limit", measured=True)
        self.assertEqual(count, 2)
        count = _update_terminal_censor(count, "timeout", measured=True)
        self.assertEqual(count, 3)

    def test_success_resets_only_measured_sequence(self):
        self.assertEqual(_update_terminal_censor(2, "success", measured=True), 0)

    def test_full_pilot_rejects_non_linux(self):
        config = {"variants": {"root": "unused"}, "execution": {}}
        variant = SimpleNamespace(directory=Path("/tmp/variants/production-profile"))
        with (
            patch("xgrammar_profile.suite.platform.system", return_value="Darwin"),
            patch("xgrammar_profile.suite.available_cpu_ids", return_value=[0]),
            patch("xgrammar_profile.suite.physical_cpu_ids", return_value=[0]),
            patch(
                "xgrammar_profile.suite.stable_machine_identity", return_value={"system": "Darwin"}
            ),
        ):
            with self.assertRaisesRegex(SuiteError, "requires Linux"):
                run_pilot(
                    config,
                    Path("/tmp/not-created"),
                    {"production-profile": variant},
                    Path("/tmp/tokenizer"),
                    quick=False,
                )

    def test_run_completion_copies_qualification_and_machine_fingerprints(self):
        identity = {"system": "Linux", "machine": "x86_64"}
        config = {
            "config_hash": "0" * 64,
            "source_head": "a" * 40,
            "machine_identity": identity,
            "machine_fingerprint": machine_identity_fingerprint(identity),
            "qualification_canonical_sha256": "b" * 64,
            "tokenizer_manifest_sha256": "c" * 64,
            "variant_manifest_hashes": {},
            "execution": {"primary_cpu": 0, "physical_cpu_ids": [0]},
        }
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("xgrammar_profile.suite.platform.system", return_value="Linux"),
                patch("xgrammar_profile.suite._tracked_tree_clean", return_value=True),
                patch("xgrammar_profile.suite._git_head", return_value="a" * 40),
                patch("xgrammar_profile.suite.stable_machine_identity", return_value=identity),
                patch("xgrammar_profile.suite.physical_cpu_ids", return_value=[0]),
                patch(
                    "xgrammar_profile.suite.resolve_assets",
                    return_value=(Path("/snapshot"), Path("/manifest"), None, None, {}),
                ),
                patch("xgrammar_profile.suite.capture_environment", return_value={}),
                patch("xgrammar_profile.suite.execute_suite", return_value=[]),
                patch("xgrammar_profile.suite.execute_diagnostics", return_value=[]),
            ):
                root = Path(directory) / "run"
                run_authoritative(config, root)
                completion = json.loads((root / "run-complete.json").read_text())
        self.assertEqual(completion["qualification_canonical_sha256"], "b" * 64)
        self.assertEqual(completion["machine_fingerprint"], machine_identity_fingerprint(identity))

    def test_run_authoritative_runs_diagnostics_before_timing_matrix(self):
        identity = {"system": "Linux", "machine": "x86_64"}
        config = {
            "config_hash": "0" * 64,
            "source_head": "a" * 40,
            "machine_identity": identity,
            "machine_fingerprint": machine_identity_fingerprint(identity),
            "qualification_canonical_sha256": "b" * 64,
            "tokenizer_manifest_sha256": "c" * 64,
            "variant_manifest_hashes": {},
            "execution": {"primary_cpu": 0, "physical_cpu_ids": [0]},
        }
        order = []
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("xgrammar_profile.suite.platform.system", return_value="Linux"),
                patch("xgrammar_profile.suite._tracked_tree_clean", return_value=True),
                patch("xgrammar_profile.suite._git_head", return_value="a" * 40),
                patch("xgrammar_profile.suite.stable_machine_identity", return_value=identity),
                patch("xgrammar_profile.suite.physical_cpu_ids", return_value=[0]),
                patch(
                    "xgrammar_profile.suite.resolve_assets",
                    return_value=(Path("/snapshot"), Path("/manifest"), None, None, {}),
                ),
                patch("xgrammar_profile.suite.capture_environment", return_value={}),
                patch(
                    "xgrammar_profile.suite.execute_suite",
                    side_effect=lambda *a, **k: order.append("timing") or [],
                ),
                patch(
                    "xgrammar_profile.suite.execute_diagnostics",
                    side_effect=lambda *a, **k: order.append("diagnostics") or [],
                ),
            ):
                run_authoritative(config, Path(directory) / "run")
        self.assertEqual(order, ["diagnostics", "timing"])

    def test_dispatch_emits_operator_progress_line_on_stderr(self):
        import io
        from contextlib import redirect_stderr

        from xgrammar_profile.suite import _dispatch

        variant = SimpleNamespace(name="production-profile", manifest_sha256="d" * 64)
        config = {"execution": {}, "config_hash": "0" * 64}
        job = {
            "case_id": "cache-fake",
            "block_id": 3,
            "measured": True,
            "arm": "full",
            "requests": [],
        }
        records = [
            {
                "record_type": "stream-summary",
                "status": "success",
                "measured": True,
                "measurement_end_observed": True,
            },
            {
                "record_type": "guard",
                "status": "success",
                "wall_time_ns": 2_500_000_000,
                "steal_flagged": False,
            },
        ]
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            with patch("xgrammar_profile.suite.run_worker", return_value=records):
                with redirect_stderr(stream):
                    produced = _dispatch(run_dir, run_dir / "raw.jsonl", config, variant, job, 7)
        self.assertEqual(produced, records)
        line = stream.getvalue()
        self.assertIn("job 000007 cache-fake arm=full block=3 measured=True status=success", line)
        self.assertIn("wall=2.5s", line)
        self.assertIn("steal_flagged=False", line)


if __name__ == "__main__":
    unittest.main()
