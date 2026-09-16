#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# Manifest/package-tree hashing must use the same bytewise pathname order as the Python verifier.
export LC_ALL=C

usage() {
  cat <<'EOF'
Usage:
  build_variants.sh --all --output-root NEW_DIR
  build_variants.sh --variant NAME --output-root NEW_DIR

Variants built by --all:
  pristine, production-profile, no-rule-cache, no-repeat-compression,
  production-diagnostic, no-rule-cache-diagnostic

Optional: --variant sampling-diagnostic builds a stats-enabled RelWithDebInfo/frame-pointer binary
for cpu-clock sampling. It is not qualified or used for authoritative timing.

Each wheel is built in its own CMake directory and installed with --no-deps into its own Python
target. Runtime dependencies live once in the profiling virtual environment.
EOF
}

mode=""
selected_variant=""
output_root=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)
      [[ -z "${mode}" ]] || profile_die "choose exactly one of --all or --variant"
      mode="all"
      shift
      ;;
    --variant)
      [[ -z "${mode}" && $# -ge 2 ]] || profile_die "--variant requires one name"
      mode="one"
      selected_variant="$2"
      shift 2
      ;;
    --output-root)
      [[ $# -ge 2 ]] || profile_die "--output-root requires a path"
      output_root="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      profile_die "unknown argument: $1"
      ;;
  esac
done

[[ -n "${mode}" ]] || profile_die "choose --all or --variant"
[[ -n "${output_root}" ]] || profile_die "--output-root is required"
if [[ "${output_root}" != /* ]]; then
  output_root="${PROFILE_REPO_ROOT}/${output_root}"
fi

all_variants=(
  pristine
  production-profile
  no-rule-cache
  no-repeat-compression
  production-diagnostic
  no-rule-cache-diagnostic
)
supported_variants=("${all_variants[@]}" sampling-diagnostic)

if [[ "${mode}" == "all" ]]; then
  variants=("${all_variants[@]}")
else
  known=no
  for candidate in "${supported_variants[@]}"; do
    if [[ "${selected_variant}" == "${candidate}" ]]; then
      known=yes
      break
    fi
  done
  [[ "${known}" == yes ]] || profile_die "unknown variant: ${selected_variant}"
  variants=("${selected_variant}")
fi

profile_require_authoritative_source
profile_activate_venv
profile_require_command jq
profile_require_command sha256sum
profile_require_free_disk_gib 8
profile_require_new_path "${output_root}"
mkdir -p "${output_root}"
python -m pip freeze --all >"${output_root}/dependency-freeze.txt"
dependency_freeze_sha256="$(sha256sum "${output_root}/dependency-freeze.txt" | awk '{print $1}')"

# Two compile jobs are conservative on the 8 GiB target. This affects build duration only.
export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-2}"
export MAX_JOBS="${MAX_JOBS:-${CMAKE_BUILD_PARALLEL_LEVEL}}"
export SOURCE_DATE_EPOCH="$(git -C "${PROFILE_REPO_ROOT}" show -s --format=%ct HEAD)"

dirty_patch_sha256="$(git -C "${PROFILE_REPO_ROOT}" diff --binary HEAD | sha256sum | awk '{print $1}')"
compiler_version="$(c++ --version | head -n 1)"
linker_version="$(ld --version 2>/dev/null | head -n 1 || true)"
python_version="$(python --version 2>&1)"

build_one() {
  local variant="$1"
  local disable_rule=OFF
  local disable_repeat=OFF
  local api=ON
  local stats=OFF
  local cxx_tests=OFF
  local build_type=Release
  local relwithdebinfo_flags=""

  case "${variant}" in
    pristine)
      api=OFF
      cxx_tests=ON
      ;;
    production-profile) ;;
    no-rule-cache)
      disable_rule=ON
      ;;
    no-repeat-compression)
      disable_repeat=ON
      ;;
    production-diagnostic)
      stats=ON
      ;;
    no-rule-cache-diagnostic)
      disable_rule=ON
      stats=ON
      ;;
    sampling-diagnostic)
      stats=ON
      build_type=RelWithDebInfo
      relwithdebinfo_flags="-O3 -g -fno-omit-frame-pointer -DNDEBUG"
      ;;
    *) profile_die "internal error: unsupported variant ${variant}" ;;
  esac

  local variant_dir="${output_root}/${variant}"
  local cmake_dir="${variant_dir}/cmake-build"
  local wheel_dir="${variant_dir}/wheelhouse"
  local site_dir="${variant_dir}/site-packages"
  mkdir -p "${cmake_dir}" "${wheel_dir}"

  # A build-directory config takes precedence over cmake/config.cmake. CACHE+FORCE avoids the
  # release file's normal-variable assignments shadowing CXX_TESTS/PYTHON_BINDINGS.
  {
    printf 'set(CMAKE_BUILD_TYPE %s CACHE STRING "" FORCE)\n' "${build_type}"
    if [[ -n "${relwithdebinfo_flags}" ]]; then
      printf 'set(CMAKE_CXX_FLAGS_RELWITHDEBINFO "%s" CACHE STRING "" FORCE)\n' \
        "${relwithdebinfo_flags}"
    fi
    printf 'set(XGRAMMAR_BUILD_PYTHON_BINDINGS ON CACHE BOOL "" FORCE)\n'
    printf 'set(XGRAMMAR_BUILD_CXX_TESTS %s CACHE BOOL "" FORCE)\n' "${cxx_tests}"
    printf 'set(XGRAMMAR_ENABLE_CPPTRACE OFF CACHE BOOL "" FORCE)\n'
    printf 'set(XGRAMMAR_ENABLE_COVERAGE OFF CACHE BOOL "" FORCE)\n'
    printf 'set(XGRAMMAR_ENABLE_INTERNAL_CHECK OFF CACHE BOOL "" FORCE)\n'
    printf 'set(XGRAMMAR_PROFILE_DISABLE_RULE_LEVEL_CACHE %s CACHE BOOL "" FORCE)\n' "${disable_rule}"
    printf 'set(XGRAMMAR_PROFILE_DISABLE_REPETITION_COMPRESSION %s CACHE BOOL "" FORCE)\n' "${disable_repeat}"
    printf 'set(XGRAMMAR_ENABLE_PROFILING_API %s CACHE BOOL "" FORCE)\n' "${api}"
    printf 'set(XGRAMMAR_ENABLE_PROFILING_STATS %s CACHE BOOL "" FORCE)\n' "${stats}"
  } >"${cmake_dir}/config.cmake"

  profile_note "Building ${variant} (type=${build_type}, rule_off=${disable_rule}, repeat_off=${disable_repeat}, stats=${stats})"
  python -m pip wheel \
    --no-deps \
    --no-build-isolation \
    --wheel-dir "${wheel_dir}" \
    --config-settings "build-dir=${cmake_dir}" \
    --config-settings "cmake.build-type=${build_type}" \
    "${PROFILE_REPO_ROOT}" 2>&1 | tee "${variant_dir}/build.log"

  local wheel
  wheel="$(find "${wheel_dir}" -maxdepth 1 -type f -name 'xgrammar-*.whl' -print -quit)"
  [[ -n "${wheel}" ]] || profile_die "wheel was not produced for ${variant}"
  python -m pip install --no-deps --target "${site_dir}" "${wheel}" \
    >"${variant_dir}/install.log" 2>&1

  local native_extension
  native_extension="$(find "${site_dir}" -type f \
    \( -name '*xgrammar_bindings*.so' -o -name '*xgrammar_bindings*.dylib' \) -print -quit)"
  [[ -n "${native_extension}" ]] || profile_die "native extension missing for ${variant}"
  local native_sha
  native_sha="$(sha256sum "${native_extension}" | awk '{print $1}')"
  local wheel_sha
  wheel_sha="$(sha256sum "${wheel}" | awk '{print $1}')"
  local cmake_cache="${cmake_dir}/CMakeCache.txt"
  [[ -f "${cmake_cache}" ]] || profile_die "CMake cache missing for ${variant}"
  local cmake_cache_sha
  cmake_cache_sha="$(sha256sum "${cmake_cache}" | awk '{print $1}')"
  local package_tree_sha
  package_tree_sha="$(
    cd "${site_dir}"
    find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 \
      | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}'
  )"

  PYTHONPATH="${site_dir}" PYTHONNOUSERSITE=1 python - <<'PY' \
    >"${variant_dir}/import-smoke.txt"
import pathlib
from importlib.metadata import version
import xgrammar

print(f"version={version('xgrammar')}")
print(f"module={pathlib.Path(xgrammar.__file__).resolve()}")
PY

  jq -n \
    --arg variant "${variant}" \
    --arg source_commit "${XGRAMMAR_PROFILE_COMMIT}" \
    --arg release_commit "${XGRAMMAR_RELEASE_COMMIT}" \
    --arg dirty_sha "${dirty_patch_sha256}" \
    --arg python_path "${site_dir}" \
    --arg native_path "${native_extension}" \
    --arg native_sha "${native_sha}" \
    --arg wheel_path "${wheel}" \
    --arg wheel_sha "${wheel_sha}" \
    --arg cmake_cache_path "${cmake_cache}" \
    --arg cmake_cache_sha "${cmake_cache_sha}" \
    --arg dependency_freeze_path "${output_root}/dependency-freeze.txt" \
    --arg dependency_freeze_sha "${dependency_freeze_sha256}" \
    --arg package_tree_sha "${package_tree_sha}" \
    --arg compiler "${compiler_version}" \
    --arg linker "${linker_version}" \
    --arg python "${python_version}" \
    --arg disable_rule "${disable_rule}" \
    --arg disable_repeat "${disable_repeat}" \
    --arg api "${api}" \
    --arg stats "${stats}" \
    --arg cxx_tests "${cxx_tests}" \
    --arg build_type "${build_type}" \
    --arg relwithdebinfo_flags "${relwithdebinfo_flags}" \
    '{
      schema_version: 1,
      variant: $variant,
      source_commit: $source_commit,
      release_commit: $release_commit,
      dirty: false,
      source_dirty_patch_sha256: $dirty_sha,
      python_path: $python_path,
      native_extension_path: $native_path,
      native_extension_sha256: $native_sha,
      wheel_path: $wheel_path,
      wheel_sha256: $wheel_sha,
      cmake_cache_path: $cmake_cache_path,
      cmake_cache_sha256: $cmake_cache_sha,
      dependency_freeze_path: $dependency_freeze_path,
      dependency_freeze_sha256: $dependency_freeze_sha,
      python_package_tree_sha256: $package_tree_sha,
      artifacts: [
        {path: $native_path, sha256: $native_sha},
        {path: $wheel_path, sha256: $wheel_sha},
        {path: $cmake_cache_path, sha256: $cmake_cache_sha},
        {path: $dependency_freeze_path, sha256: $dependency_freeze_sha}
      ],
      build_type: $build_type,
      compiler: $compiler,
      linker: $linker,
      python: $python,
      cmake_options: {
        XGRAMMAR_PROFILE_DISABLE_RULE_LEVEL_CACHE: $disable_rule,
        XGRAMMAR_PROFILE_DISABLE_REPETITION_COMPRESSION: $disable_repeat,
        XGRAMMAR_ENABLE_PROFILING_API: $api,
        XGRAMMAR_ENABLE_PROFILING_STATS: $stats,
        XGRAMMAR_BUILD_CXX_TESTS: $cxx_tests,
        CMAKE_CXX_FLAGS_RELWITHDEBINFO: $relwithdebinfo_flags
      }
    }' >"${variant_dir}/manifest.json"
}

profile_note "Building isolated variants at ${output_root} with ${CMAKE_BUILD_PARALLEL_LEVEL} jobs"
for variant in "${variants[@]}"; do
  build_one "${variant}"
done

find "${output_root}" -mindepth 2 -maxdepth 2 -name manifest.json -type f -print0 \
  | sort -z \
  | xargs -0 sha256sum >"${output_root}/manifest-sha256.txt"

for required in "${all_variants[@]}"; do
  if [[ "${mode}" == "all" ]]; then
    manifest="${output_root}/${required}/manifest.json"
    [[ -f "${manifest}" ]] || profile_die "missing required variant manifest: ${manifest}"
  fi
done

profile_require_free_disk_gib 8
profile_note "Build complete. Free disk: $(profile_free_disk_gib) GiB"
profile_note "Use this exact variant root: ${output_root}"
