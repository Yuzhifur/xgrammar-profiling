import json
import sys
import tempfile
import unittest
from pathlib import Path

from xgrammar_profile.measurement import initialize_jsonl, run_worker
from xgrammar_profile.variants import Variant


def _record(record_type="sample", status="success", request_index=None):
    value = {
        "schema_version": 1,
        "record_type": record_type,
        "experiment": "cache",
        "status": status,
        "case_id": "fake-cache",
        "block_id": 0,
        "measured": True,
        "variant": "production-profile",
        "arm": "full",
        "compile_time_ns": 1 if status == "success" else None,
    }
    if request_index is not None:
        value["request_index"] = request_index
    return value


class MeasurementTests(unittest.TestCase):
    def _run(self, script_text):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        worker = root / "a" / "b" / "worker.py"
        worker.parent.mkdir(parents=True)
        worker.write_text(script_text, encoding="utf-8")
        variant = Variant(
            name="production-profile",
            directory=root,
            manifest_path=root / "manifest.json",
            manifest={},
            manifest_sha256="a" * 64,
            python_paths=[root],
        )
        raw = root / "raw.jsonl"
        initialize_jsonl(raw)
        job_path = root / "job.json"
        records = run_worker(
            worker=worker,
            job={"case_id": "fake-cache"},
            job_path=job_path,
            raw_path=raw,
            variant=variant,
            execution={
                "timeout_seconds": 0.12,
                "rss_limit_bytes": 256 * 1024 * 1024,
                "rss_poll_interval_seconds": 0.01,
                "termination_grace_seconds": 0.03,
            },
            metadata={
                "experiment": "cache",
                "case_id": "fake-cache",
                "block_id": 0,
                "measured": True,
                "variant": variant.name,
                "arm": "full",
                "config_hash": "b" * 64,
                "cpu_affinity": None,
            },
        )
        return root, job_path, records

    def test_timeout_after_partial_output_has_one_canonical_outcome(self):
        partial = json.dumps(_record(request_index=0), separators=(",", ":"))
        root, job_path, records = self._run(
            "import time\n" f"print({partial!r}, flush=True)\n" "time.sleep(5)\n"
        )
        outcomes = [record for record in records if record["record_type"] == "stream-summary"]
        self.assertEqual([record["status"] for record in outcomes], ["timeout"])
        self.assertTrue(any(record.get("request_index") == 0 for record in records))
        self.assertNotIn("baseline_ready_path", json.loads(job_path.read_text()))
        self.assertEqual(list(root.glob("*.baseline.*")), [])
        self.assertEqual(list(root.glob("*.measurement-end.*")), [])
        self.assertFalse(job_path.with_suffix(".json.guarded").exists())

    def test_worker_program_error_is_canonicalized_once(self):
        failure = json.dumps(_record(status="program_error"), separators=(",", ":"))
        _, _, records = self._run(f"print({failure!r}, flush=True)\n" "raise SystemExit(1)\n")
        outcomes = [record for record in records if record["record_type"] == "stream-summary"]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "program_error")

    def test_success_without_final_rss_handshake_is_invalid_output(self):
        summary = json.dumps(_record(record_type="stream-summary"), separators=(",", ":"))
        _, _, records = self._run(f"print({summary!r}, flush=True)\n")
        outcomes = [record for record in records if record["record_type"] == "stream-summary"]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "invalid_output")
        self.assertIn("retained-RSS handshake", outcomes[0]["parse_error"])

    def test_successful_two_way_rss_handshakes_and_cleanup(self):
        summary = json.dumps(_record(record_type="stream-summary"), separators=(",", ":"))
        script = (
            "import json,pathlib,sys,time\n"
            "job_path=pathlib.Path(sys.argv[sys.argv.index('--job')+1])\n"
            "job=json.loads(job_path.read_text())\n"
            "ready=pathlib.Path(job['baseline_ready_path'])\n"
            "ack=pathlib.Path(job['baseline_ack_path'])\n"
            "ready.write_text('ready')\n"
            "while not ack.exists(): time.sleep(.002)\n"
            "end=pathlib.Path(job['measurement_end_ready_path'])\n"
            "tmp=end.with_name('.'+end.name+'.tmp')\n"
            "tmp.write_text(json.dumps({'worker_rss_bytes':1,"
            "'measurement_completed_monotonic_ns':time.monotonic_ns()}))\n"
            "tmp.replace(end)\n"
            "end_ack=pathlib.Path(job['measurement_end_ack_path'])\n"
            "while not end_ack.exists(): time.sleep(.002)\n"
            f"print({summary!r}, flush=True)\n"
        )
        root, job_path, records = self._run(script)
        outcomes = [record for record in records if record["record_type"] == "stream-summary"]
        self.assertEqual([record["status"] for record in outcomes], ["success"])
        self.assertTrue(outcomes[0]["baseline_ready_observed"])
        self.assertTrue(outcomes[0]["measurement_end_observed"])
        self.assertEqual(list(root.glob("*measurement-end*")), [])
        self.assertFalse(job_path.with_suffix(".json.guarded").exists())


if __name__ == "__main__":
    unittest.main()
