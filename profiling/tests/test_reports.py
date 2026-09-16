import tempfile
import unittest
import hashlib
from pathlib import Path

from xgrammar_profile.config import canonical_json, sha256_file, write_json_atomic
from xgrammar_profile.reports import ReportError, verify_reports, word_count


class ReportTests(unittest.TestCase):
    def _write_evidence(self, root, summary):
        (root / "analysis").mkdir()
        (root / "raw").mkdir()
        (root / "jobs").mkdir()
        for name in ("frozen-config.json", "environment.json", "variant-manifests.json"):
            write_json_atomic(root / name, {"name": name})
        write_json_atomic(root / "jobs" / "000.json", {"case": "x"})
        provenance_files = {
            name: sha256_file(root / name)
            for name in ("frozen-config.json", "environment.json", "variant-manifests.json")
        }
        job_files = {"jobs/000.json": sha256_file(root / "jobs" / "000.json")}
        write_json_atomic(root / "analysis" / "summary.json", summary)
        write_json_atomic(
            root / "analysis" / "completeness.json",
            {"passed": True, "config_hash": "h", "raw_files": {}},
        )
        write_json_atomic(
            root / "run-complete.json",
            {
                "complete": True,
                "config_hash": "h",
                "raw_files": {},
                "provenance_files": provenance_files,
                "job_count": len(job_files),
                "jobs_manifest_sha256": hashlib.sha256(canonical_json(job_files)).hexdigest(),
            },
        )
        write_json_atomic(
            root / "analysis" / "analysis-manifest.json",
            {
                "config_hash": "h",
                "raw_files": {},
                "run_complete_sha256": sha256_file(root / "run-complete.json"),
                "summary_sha256": sha256_file(root / "analysis" / "summary.json"),
                "completeness_sha256": sha256_file(root / "analysis" / "completeness.json"),
            },
        )

    def test_word_count_ignores_code_and_urls(self):
        self.assertEqual(word_count("one two [three](https://example.com) ```four five```"), 3)

    def test_verification_enforces_one_page_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_evidence(
                root,
                {"cells": [{"case": "x"}], "completeness": {"passed": True, "config_hash": "h"}},
            )
            comprehensive = root / "comprehensive.md"
            comprehensive.write_text("context " * 600, encoding="utf-8")
            short = root / "short.md"
            short.write_text("word " * 449, encoding="utf-8")
            with self.assertRaises(ReportError):
                verify_reports(root, comprehensive, short)
            page = root / "page.md"
            page.write_text("word " * 500, encoding="utf-8")
            result = verify_reports(root, comprehensive, page)
        self.assertEqual(result["one_page_word_count"], 500)
        self.assertEqual(len(result["comprehensive_sha256"]), 64)
        self.assertEqual(len(result["one_page_sha256"]), 64)

    def test_bracketed_template_placeholder_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_evidence(root, {"cells": [1]})
            comprehensive = root / "comprehensive.md"
            comprehensive.write_text(("real context " * 500) + "[RATIO]", encoding="utf-8")
            page = root / "page.md"
            page.write_text("word " * 500, encoding="utf-8")
            with self.assertRaisesRegex(ReportError, "bracketed"):
                verify_reports(root, comprehensive, page)

    def test_raw_change_after_analysis_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_evidence(root, {"cells": [1]})
            (root / "raw" / "late.jsonl").write_text("{}\n", encoding="utf-8")
            comprehensive = root / "comprehensive.md"
            comprehensive.write_text("context " * 600, encoding="utf-8")
            page = root / "page.md"
            page.write_text("word " * 500, encoding="utf-8")
            with self.assertRaisesRegex(ReportError, "stale"):
                verify_reports(root, comprehensive, page)

    def test_provenance_or_job_change_after_analysis_is_rejected(self):
        for relative in ("environment.json", "jobs/000.json"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._write_evidence(root, {"cells": [1]})
                (root / relative).write_text("changed\n", encoding="utf-8")
                comprehensive = root / "comprehensive.md"
                comprehensive.write_text("context " * 600, encoding="utf-8")
                page = root / "page.md"
                page.write_text("word " * 500, encoding="utf-8")
                with self.assertRaisesRegex(ReportError, "stale"):
                    verify_reports(root, comprehensive, page)


if __name__ == "__main__":
    unittest.main()
