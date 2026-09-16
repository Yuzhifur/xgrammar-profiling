"""Mechanical verification for generated profiling reports."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict

from .config import canonical_json, sha256_file, write_json_atomic


class ReportError(RuntimeError):
    pass


WORD_RE = re.compile(r"\b[\w][\w'’.-]*\b", re.UNICODE)


def word_count(markdown: str) -> int:
    without_code = re.sub(r"```.*?```", " ", markdown, flags=re.DOTALL)
    without_links = re.sub(r"!?(?:\[([^]]*)\])\([^)]*\)", r"\1", without_code)
    return len(WORD_RE.findall(without_links))


def verify_reports(run_dir: Path, comprehensive: Path, one_page: Path) -> Dict[str, Any]:
    summary_path = run_dir / "analysis" / "summary.json"
    if not summary_path.is_file():
        raise ReportError("analysis/summary.json is missing; run `analyze` first")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not summary.get("cells"):
        raise ReportError("analysis contains no measured cells")
    completion_path = run_dir / "run-complete.json"
    completeness_path = run_dir / "analysis" / "completeness.json"
    analysis_manifest_path = run_dir / "analysis" / "analysis-manifest.json"
    if (
        not completion_path.is_file()
        or not completeness_path.is_file()
        or not analysis_manifest_path.is_file()
    ):
        raise ReportError("run completion/completeness evidence is missing")
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    completeness = json.loads(completeness_path.read_text(encoding="utf-8"))
    if completion.get("complete") is not True or completeness.get("passed") is not True:
        raise ReportError("authoritative run is not complete and verified")
    if completeness.get("config_hash") != completion.get("config_hash"):
        raise ReportError("analysis is not bound to the completed run config")
    analysis_manifest = json.loads(analysis_manifest_path.read_text(encoding="utf-8"))
    current_raw = {
        path.relative_to(run_dir).as_posix(): sha256_file(path)
        for path in sorted((run_dir / "raw").glob("*.jsonl"))
    }
    current_provenance = {
        name: sha256_file(run_dir / name)
        for name in ("frozen-config.json", "environment.json", "variant-manifests.json")
        if (run_dir / name).is_file()
    }
    current_jobs = {
        path.relative_to(run_dir).as_posix(): sha256_file(path)
        for path in sorted((run_dir / "jobs").glob("*.json"))
    }
    current_jobs_sha256 = __import__("hashlib").sha256(canonical_json(current_jobs)).hexdigest()
    if (
        completion.get("raw_files") != current_raw
        or completion.get("provenance_files") != current_provenance
        or len(current_provenance) != 3
        or completion.get("job_count") != len(current_jobs)
        or completion.get("jobs_manifest_sha256") != current_jobs_sha256
        or completeness.get("raw_files") != current_raw
        or analysis_manifest.get("raw_files") != current_raw
        or analysis_manifest.get("config_hash") != completion.get("config_hash")
        or analysis_manifest.get("run_complete_sha256") != sha256_file(completion_path)
        or analysis_manifest.get("summary_sha256") != sha256_file(summary_path)
        or analysis_manifest.get("completeness_sha256") != sha256_file(completeness_path)
    ):
        raise ReportError("analysis is stale or an authoritative artifact changed after analysis")
    missing = [str(path) for path in (comprehensive, one_page) if not path.is_file()]
    if missing:
        raise ReportError(f"missing report files: {', '.join(missing)}")
    comprehensive_text = comprehensive.read_text(encoding="utf-8")
    one_page_text = one_page.read_text(encoding="utf-8")
    if len(comprehensive_text.strip()) < 1000:
        raise ReportError("comprehensive report is unexpectedly short")
    count = word_count(one_page_text)
    if not 450 <= count <= 600:
        raise ReportError(f"one-page report has {count} words; required range is 450-600")
    combined = comprehensive_text + "\n" + one_page_text
    forbidden = [
        marker
        for marker in (
            "TODO",
            "TBD",
            "INSERT RESULT",
            "results template",
            "Draft template",
            "not a result",
            "Do not publish",
            "Replace every bracketed",
        )
        if marker.lower() in combined.lower()
    ]
    if forbidden:
        raise ReportError(f"unresolved report placeholders: {forbidden}")
    without_links = re.sub(r"!?(?:\[[^]]*\])\([^)]*\)", "", combined)
    bracketed = re.findall(r"\[[^\]\n]{2,}\]", without_links)
    if bracketed:
        raise ReportError(f"unresolved bracketed placeholders: {bracketed[:10]}")
    snapshot_dir = run_dir / "analysis" / "verified-reports"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    comprehensive_snapshot = snapshot_dir / "comprehensive.md"
    one_page_snapshot = snapshot_dir / "one-page.md"
    for target in (comprehensive_snapshot, one_page_snapshot):
        if target.exists():
            raise ReportError(f"refusing to overwrite an already sealed report snapshot: {target}")
    shutil.copyfile(comprehensive, comprehensive_snapshot)
    shutil.copyfile(one_page, one_page_snapshot)
    result = {
        "schema_version": 1,
        "verified": True,
        "one_page_word_count": count,
        "comprehensive_bytes": len(comprehensive_text.encode()),
        "summary_sha256": __import__("hashlib").sha256(summary_path.read_bytes()).hexdigest(),
        "run_complete_sha256": __import__("hashlib")
        .sha256(completion_path.read_bytes())
        .hexdigest(),
        "completeness_sha256": __import__("hashlib")
        .sha256(completeness_path.read_bytes())
        .hexdigest(),
        "comprehensive": str(comprehensive.resolve()),
        "one_page": str(one_page.resolve()),
        "comprehensive_sha256": sha256_file(comprehensive_snapshot),
        "one_page_sha256": sha256_file(one_page_snapshot),
        "comprehensive_snapshot": str(comprehensive_snapshot.resolve()),
        "one_page_snapshot": str(one_page_snapshot.resolve()),
    }
    write_json_atomic(run_dir / "analysis" / "report-verification.json", result)
    return result
