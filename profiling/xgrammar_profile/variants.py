"""Build-variant discovery and manifest verification."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .config import RELEASE_COMMIT, sha256_file


class VariantError(RuntimeError):
    pass


EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
EXPECTED_BUILD_CONFIG = {
    "pristine": (False, False, False, False),
    "production-profile": (False, False, True, False),
    "no-rule-cache": (True, False, True, False),
    "no-repeat-compression": (False, True, True, False),
    "production-diagnostic": (False, False, True, True),
    "no-rule-cache-diagnostic": (True, False, True, True),
}
BUILD_KEYS = (
    "XGRAMMAR_PROFILE_DISABLE_RULE_LEVEL_CACHE",
    "XGRAMMAR_PROFILE_DISABLE_REPETITION_COMPRESSION",
    "XGRAMMAR_ENABLE_PROFILING_API",
    "XGRAMMAR_ENABLE_PROFILING_STATS",
)


@dataclass(frozen=True)
class Variant:
    name: str
    directory: Path
    manifest_path: Path
    manifest: Dict[str, Any]
    manifest_sha256: str
    python_paths: List[Path]

    def environment(self, base: Dict[str, str] | None = None) -> Dict[str, str]:
        env = dict(base or os.environ)
        harness_root = Path(__file__).resolve().parents[1]
        paths = [str(path) for path in self.python_paths] + [str(harness_root)]
        env["PYTHONPATH"] = os.pathsep.join(paths)
        env["PYTHONNOUSERSITE"] = "1"
        env["TOKENIZERS_PARALLELISM"] = "false"
        return env


def _resolve_python_paths(directory: Path, manifest: Dict[str, Any]) -> List[Path]:
    value = manifest.get("python_path", manifest.get("python_paths"))
    values = [value] if isinstance(value, str) else value
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(item, str) for item in values)
    ):
        # A standard pip --target layout places the package directly below the variant dir.
        values = ["."]
    result = []
    for item in values:
        path = Path(item)
        if not path.is_absolute():
            path = directory / path
        path = path.resolve()
        if not path.exists():
            raise VariantError(f"variant Python path does not exist: {path}")
        result.append(path)
    return result


def package_tree_sha256(python_paths: Iterable[Path]) -> str:
    roots = [path / "xgrammar" for path in python_paths if (path / "xgrammar").is_dir()]
    if len(roots) != 1:
        raise VariantError(f"expected exactly one installed xgrammar package, found {roots}")
    site = roots[0].parent
    files = sorted(
        (
            path
            for path in site.rglob("*")
            if path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.parts
        ),
        key=lambda path: path.relative_to(site).as_posix().encode(),
    )
    digest = hashlib.sha256()
    for path in files:
        # Match GNU `sha256sum` fed by `find .`: its filename field retains `./`.
        relative = "./" + path.relative_to(site).as_posix()
        digest.update(f"{sha256_file(path)}  {relative}\n".encode())
    return digest.hexdigest()


def _manifest_build_config(manifest: Dict[str, Any], name: str) -> Dict[str, bool]:
    options = manifest.get("cmake_options")
    if not isinstance(options, dict):
        raise VariantError(f"variant {name} has no cmake_options object")
    result: Dict[str, bool] = {}
    for key in BUILD_KEYS:
        value = options.get(key)
        if value not in ("ON", "OFF", True, False):
            raise VariantError(f"variant {name} has invalid/missing {key}: {value!r}")
        result[key] = value in ("ON", True)
    expected = dict(zip(BUILD_KEYS, EXPECTED_BUILD_CONFIG[name]))
    if result != expected:
        raise VariantError(
            f"variant {name} CMake controls are mislabeled: {result}, expected {expected}"
        )
    return result


def load_variant(
    root: Path, name: str, *, authoritative: bool = False, expected_source_commit: str | None = None
) -> Variant:
    if name not in EXPECTED_BUILD_CONFIG:
        raise VariantError(f"unrecognized profiling variant: {name}")
    directory = (root / name).resolve()
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise VariantError(f"missing build manifest for {name}: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VariantError(f"invalid variant manifest {manifest_path}: {exc}") from exc
    if manifest.get("variant", manifest.get("name")) != name:
        raise VariantError(f"variant manifest name does not match directory: {name}")
    release = manifest.get("release_commit", manifest.get("base_commit"))
    if release != RELEASE_COMMIT:
        raise VariantError(f"variant {name} was not based on reviewed v0.2.7 commit")
    source_commit = manifest.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(char not in "0123456789abcdef" for char in source_commit)
    ):
        raise VariantError(f"variant {name} lacks an exact source_commit")
    if expected_source_commit is not None and source_commit != expected_source_commit:
        raise VariantError(
            f"variant {name} source_commit {source_commit} != expected {expected_source_commit}"
        )
    if manifest.get("dirty") is not False:
        raise VariantError(f"variant {name} must record dirty=false")
    if manifest.get("source_dirty_patch_sha256") != EMPTY_SHA256:
        raise VariantError(f"variant {name} was built with a non-empty tracked source diff")
    _manifest_build_config(manifest, name)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) < 3:
        raise VariantError(f"variant {name} lacks required hashed artifacts")
    for entry in artifacts:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise VariantError(f"variant {name} has an invalid artifact entry")
        path = Path(entry["path"])
        if not path.is_absolute():
            path = directory / path
        expected = entry.get("sha256")
        if expected and (not path.is_file() or sha256_file(path) != expected):
            raise VariantError(f"variant artifact hash mismatch: {path}")
    python_paths = _resolve_python_paths(directory, manifest)
    expected_tree = manifest.get("python_package_tree_sha256")
    actual_tree = package_tree_sha256(python_paths)
    if not isinstance(expected_tree, str) or actual_tree != expected_tree:
        raise VariantError(
            f"installed Python package tree hash mismatch for {name}: {actual_tree} != {expected_tree}"
        )
    return Variant(
        name=name,
        directory=directory,
        manifest_path=manifest_path,
        manifest=manifest,
        manifest_sha256=sha256_file(manifest_path),
        python_paths=python_paths,
    )


def load_variants(
    root: Path,
    names: Iterable[str],
    *,
    authoritative: bool = False,
    expected_source_commit: str | None = None,
) -> Dict[str, Variant]:
    return {
        name: load_variant(
            root, name, authoritative=authoritative, expected_source_commit=expected_source_commit
        )
        for name in names
    }


def verify_import(variant: Variant, python_executable: str) -> Dict[str, Any]:
    script = (
        "import json, pathlib, xgrammar; from xgrammar.testing import get_profiling_build_config; "
        "print(json.dumps({'version':getattr(xgrammar,'__version__',None),"
        "'module':str(pathlib.Path(xgrammar.__file__).resolve()),"
        "'profiling_build_config':get_profiling_build_config()}))"
    )
    completed = subprocess.run(
        [python_executable, "-c", script],
        env=variant.environment(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise VariantError(f"cannot import variant {variant.name}: {completed.stderr[-2000:]}")
    try:
        info = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise VariantError(f"variant {variant.name} import emitted invalid output") from exc
    module = Path(info["module"])
    if not any(path == module.parent or path in module.parents for path in variant.python_paths):
        raise VariantError(
            f"wrong XGrammar imported for {variant.name}: {module}; expected under {variant.python_paths}"
        )
    runtime = info.get("profiling_build_config")
    expected = _manifest_build_config(variant.manifest, variant.name)
    if runtime != expected:
        raise VariantError(
            f"runtime build controls for {variant.name} do not match manifest: {runtime} != {expected}"
        )
    return info


def manifest_hashes(variants: Dict[str, Variant]) -> Dict[str, str]:
    return {name: variant.manifest_sha256 for name, variant in sorted(variants.items())}


def verify_dependency_environment(
    variants: Dict[str, Variant], python_executable: str
) -> Dict[str, Any]:
    """Bind the active profiling venv to the freeze captured during variant build."""
    expected_paths: set[Path] = set()
    expected_hashes: set[str] = set()
    for variant in variants.values():
        value = variant.manifest.get("dependency_freeze_path")
        digest = variant.manifest.get("dependency_freeze_sha256")
        if not isinstance(value, str) or not isinstance(digest, str):
            raise VariantError(f"variant {variant.name} lacks dependency-freeze provenance")
        path = Path(value)
        if not path.is_absolute():
            path = variant.directory / path
        expected_paths.add(path.resolve())
        expected_hashes.add(digest)
    if len(expected_paths) != 1 or len(expected_hashes) != 1:
        raise VariantError("profiling variants do not share one dependency freeze")
    path = next(iter(expected_paths))
    expected_hash = next(iter(expected_hashes))
    if not path.is_file() or sha256_file(path) != expected_hash:
        raise VariantError(f"dependency freeze is missing or changed: {path}")
    completed = subprocess.run(
        [python_executable, "-m", "pip", "freeze", "--all"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise VariantError(
            f"cannot inspect live profiling dependencies: {completed.stderr[-2000:]}"
        )

    def normalized_lines(text: str) -> List[str]:
        return sorted(line.strip() for line in text.splitlines() if line.strip())

    expected_lines = normalized_lines(path.read_text(encoding="utf-8"))
    actual_lines = normalized_lines(completed.stdout)
    if actual_lines != expected_lines:
        missing = sorted(set(expected_lines) - set(actual_lines))
        added = sorted(set(actual_lines) - set(expected_lines))
        raise VariantError(
            "live profiling dependencies differ from dependency-freeze.txt; "
            f"missing={missing[:10]}, added={added[:10]}"
        )
    return {"path": str(path), "sha256": expected_hash, "line_count": len(expected_lines)}
