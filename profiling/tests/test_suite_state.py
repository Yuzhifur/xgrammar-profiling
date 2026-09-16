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


if __name__ == "__main__":
    unittest.main()
