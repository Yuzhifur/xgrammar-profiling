"""Command-line interface for the XGrammar profiling study."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Sequence

from .analysis import analyze_run
from .config import load_config, profiling_root, repo_root, resolve_path, sha256_file
from .dataset import prepare_bfcl, production_support_validator, production_trace_validator
from .reports import verify_reports
from .suite import (
    SuiteError,
    freeze_from_pilot,
    resolve_assets,
    run_authoritative,
    run_pilot,
    timestamped_dir,
    validate_variants,
)
from .tokenizer_snapshot import prepare_tokenizer, verify_snapshot
from .variants import load_variant, verify_dependency_environment, verify_import


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _default_config() -> Path:
    return profiling_root() / "configs" / "v0.2.7.json"


def command_prepare_tokenizer(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    output = resolve_path(args.output or config["tokenizer"]["snapshot_dir"])
    local_source = Path(args.local_source).expanduser().resolve() if args.local_source else None
    manifest = prepare_tokenizer(
        repository=config["tokenizer"]["repository"],
        revision=config["tokenizer"]["revision"],
        output=output,
        local_source=local_source,
    )
    _print({"output": str(output), "manifest": manifest})


def command_prepare_bfcl(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    output = resolve_path(args.output or config["bfcl"]["snapshot_dir"])
    source = Path(args.source_dir).expanduser().resolve() if args.source_dir else None
    source_revision = None
    if source is not None:
        completed = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise SuiteError(
                "--source-dir must be a git checkout at the declared immutable BFCL revision"
            )
        source_revision = completed.stdout.strip().lower()
        if source_revision != args.revision.lower():
            raise SuiteError(
                f"BFCL source HEAD {source_revision} differs from --revision {args.revision}"
            )
        status = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=normal"],
            text=True,
            capture_output=True,
            check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            raise SuiteError("--source-dir BFCL checkout must be clean, including untracked files")
    snapshot = resolve_path(args.tokenizer_snapshot or config["tokenizer"]["snapshot_dir"])
    verify_snapshot(snapshot, expected_revision=config["tokenizer"]["revision"])
    variant_root = Path(args.variant_root).expanduser().resolve()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root(), text=True, capture_output=True, check=True
    ).stdout.strip()
    variant = load_variant(variant_root, "production-profile", expected_source_commit=head)
    verify_dependency_environment({variant.name: variant}, sys.executable)
    import_info = verify_import(variant, sys.executable)
    manifest = prepare_bfcl(
        revision=args.revision,
        output=output,
        source_dir=source,
        source_revision=source_revision,
        support_validator=lambda candidates: production_support_validator(
            candidates, tokenizer_snapshot=snapshot, variant=variant
        ),
        trace_validator=lambda traces: production_trace_validator(
            traces, tokenizer_snapshot=snapshot, variant=variant
        ),
        validation_provenance={
            "production_variant_manifest_sha256": variant.manifest_sha256,
            "tokenizer_manifest_sha256": sha256_file(snapshot / "manifest.json"),
            "build_config": import_info["profiling_build_config"],
        },
    )
    _print({"output": str(output), "manifest": manifest})


def command_build(args: argparse.Namespace) -> None:
    script = profiling_root() / "scripts" / "build_variants.sh"
    if not script.is_file():
        raise SuiteError(
            f"build driver is unavailable: {script}; restore profiling/scripts/build_variants.sh"
        )
    output = resolve_path(args.output_root or "profiling/build/variants")
    command = [str(script)]
    if args.all:
        command.append("--all")
    else:
        command.extend(["--variant", args.variant])
    command.extend(["--output-root", str(output)])
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise SuiteError(f"variant build failed with exit code {completed.returncode}")


def command_validate(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    root_override = Path(args.variant_root).expanduser().resolve() if args.variant_root else None
    snapshot, _, bfcl_dir, _, variants = resolve_assets(config, variant_root=root_override)
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else timestamped_dir(
            resolve_path(config.get("results_root", "profiling/results")), "validation"
        )
    )
    result = validate_variants(config, output, variants, snapshot, bfcl_dir=bfcl_dir)
    _print({"output": str(output), **result})


def command_pilot(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    root_override = Path(args.variant_root).expanduser().resolve() if args.variant_root else None
    snapshot, _, _, _, variants = resolve_assets(config, variant_root=root_override)
    run_dir = (
        Path(args.run_dir).expanduser().resolve()
        if args.run_dir
        else timestamped_dir(resolve_path(config.get("results_root", "profiling/results")), "pilot")
    )
    qualification = Path(args.qualification).expanduser().resolve() if args.qualification else None
    result = run_pilot(
        config, run_dir, variants, snapshot, quick=args.quick, qualification_path=qualification
    )
    _print({"run_dir": str(run_dir), **result})


def command_freeze(args: argparse.Namespace) -> None:
    pilot_dir = Path(args.pilot_results).expanduser().resolve()
    output = Path(args.output).expanduser().resolve() if args.output else None
    config = freeze_from_pilot(pilot_dir, output)
    _print(
        {
            "output": str(output or (pilot_dir / "frozen-config.json")),
            "config_hash": config["config_hash"],
        }
    )


def command_run(args: argparse.Namespace) -> None:
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path, require_frozen=True)
    root_override = Path(args.variant_root).expanduser().resolve() if args.variant_root else None
    run_dir = (
        Path(args.run_dir).expanduser().resolve()
        if args.run_dir
        else timestamped_dir(resolve_path(config.get("results_root", "profiling/results")), "run")
    )
    records = run_authoritative(config, run_dir, root_override)
    _print({"run_dir": str(run_dir), "records": len(records), "config_hash": config["config_hash"]})


def command_analyze(args: argparse.Namespace) -> None:
    run_dir = Path(args.run).expanduser().resolve()
    summary = analyze_run(run_dir)
    _print(
        {
            "run_dir": str(run_dir),
            "cells": len(summary["cells"]),
            "comparisons": len(summary["comparisons"]),
        }
    )


def command_verify_reports(args: argparse.Namespace) -> None:
    run_dir = Path(args.run).expanduser().resolve()
    comprehensive = (
        Path(args.comprehensive).expanduser().resolve()
        if args.comprehensive
        else profiling_root() / "reports" / "comprehensive-report.md"
    )
    one_page = (
        Path(args.one_page).expanduser().resolve()
        if args.one_page
        else profiling_root() / "reports" / "one-page-report.md"
    )
    _print(verify_reports(run_dir, comprehensive, one_page))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xgrammar-profile",
        description="Reproducible XGrammar v0.2.7 cache and repetition profiling",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_tokenizer_parser = subparsers.add_parser(
        "prepare-tokenizer", help="create the pinned offline tokenizer snapshot"
    )
    prepare_tokenizer_parser.add_argument("--config", type=Path, default=_default_config())
    prepare_tokenizer_parser.add_argument("--output")
    prepare_tokenizer_parser.add_argument(
        "--local-source", help="existing local HF tokenizer directory; disables network access"
    )
    prepare_tokenizer_parser.set_defaults(handler=command_prepare_tokenizer)

    prepare_bfcl_parser = subparsers.add_parser(
        "prepare-bfcl", help="normalize and production-validate BFCL at an immutable commit"
    )
    prepare_bfcl_parser.add_argument("--config", type=Path, default=_default_config())
    prepare_bfcl_parser.add_argument("--revision", required=True)
    prepare_bfcl_parser.add_argument(
        "--variant-root",
        required=True,
        help="completed six-variant build root containing production-profile",
    )
    prepare_bfcl_parser.add_argument("--tokenizer-snapshot")
    prepare_bfcl_parser.add_argument("--output")
    prepare_bfcl_parser.add_argument(
        "--source-dir", help="local checkout for an offline preparation"
    )
    prepare_bfcl_parser.set_defaults(handler=command_prepare_bfcl)

    build_variant_parser = subparsers.add_parser("build", help="build isolated XGrammar variants")
    choice = build_variant_parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--all", action="store_true")
    choice.add_argument("--variant")
    build_variant_parser.add_argument("--output-root")
    build_variant_parser.set_defaults(handler=command_build)

    validate_parser = subparsers.add_parser(
        "validate", help="run cross-variant semantic qualification"
    )
    validate_parser.add_argument("--all", action="store_true", required=True)
    validate_parser.add_argument("--config", type=Path, default=_default_config())
    validate_parser.add_argument("--variant-root")
    validate_parser.add_argument("--output")
    validate_parser.set_defaults(handler=command_validate)

    pilot_parser = subparsers.add_parser(
        "pilot", help="run resource, variance, watchdog, and profiler preflights"
    )
    pilot_parser.add_argument("--config", type=Path, required=True)
    pilot_parser.add_argument("--run-dir")
    pilot_parser.add_argument("--variant-root")
    pilot_parser.add_argument(
        "--quick", action="store_true", help="Mac plumbing smoke only; output cannot be frozen"
    )
    pilot_parser.add_argument(
        "--qualification",
        help="passed qualification JSON from validate + existing test suites; required unless --quick",
    )
    pilot_parser.set_defaults(handler=command_pilot)

    freeze_parser = subparsers.add_parser(
        "freeze", help="freeze allowed operational decisions from a qualifying pilot"
    )
    freeze_parser.add_argument("--pilot-results", required=True)
    freeze_parser.add_argument("--output")
    freeze_parser.set_defaults(handler=command_freeze)

    run_parser = subparsers.add_parser("run", help="run the authoritative frozen matrix")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--run-dir")
    run_parser.add_argument("--variant-root")
    run_parser.set_defaults(handler=command_run)

    analyze_parser = subparsers.add_parser(
        "analyze", help="validate and summarize append-only raw JSONL"
    )
    analyze_parser.add_argument("--run", required=True)
    analyze_parser.set_defaults(handler=command_analyze)

    reports_parser = subparsers.add_parser(
        "verify-reports", help="verify final report artifacts and word count"
    )
    reports_parser.add_argument("--run", required=True)
    reports_parser.add_argument("--comprehensive")
    reports_parser.add_argument("--one-page")
    reports_parser.set_defaults(handler=command_verify_reports)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
