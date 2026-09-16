#!/usr/bin/env bash

# Shared safety checks for the droplet operator scripts. This file is sourced; it is not a
# standalone entry point.

set -Eeuo pipefail

readonly XGRAMMAR_RELEASE_COMMIT="82505d0d987c36a4209fb3d8571cf6b0f28b5acd"
readonly PROFILE_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly PROFILE_REPO_ROOT="$(cd -- "${PROFILE_SCRIPT_DIR}/../.." && pwd -P)"

profile_die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

profile_note() {
  printf '[xgrammar-profile] %s\n' "$*"
}

profile_require_command() {
  command -v "$1" >/dev/null 2>&1 || profile_die "required command not found: $1"
}

profile_require_authoritative_source() {
  profile_require_command git
  local allow_report_edits="${1:-no}"

  local expected="${XGRAMMAR_PROFILE_COMMIT:-}"
  [[ "${expected}" =~ ^[0-9a-f]{40}$ ]] || profile_die \
    "set XGRAMMAR_PROFILE_COMMIT to the exact 40-hex implementation commit"

  local actual
  actual="$(git -C "${PROFILE_REPO_ROOT}" rev-parse HEAD)"
  [[ "${actual}" == "${expected}" ]] || profile_die \
    "HEAD is ${actual}; expected XGRAMMAR_PROFILE_COMMIT=${expected}"

  git -C "${PROFILE_REPO_ROOT}" merge-base --is-ancestor \
    "${XGRAMMAR_RELEASE_COMMIT}" "${actual}" || profile_die \
    "implementation commit does not descend from XGrammar v0.2.7 release ${XGRAMMAR_RELEASE_COMMIT}"

  local dirty
  dirty="$(git -C "${PROFILE_REPO_ROOT}" status --porcelain --untracked-files=normal)"
  if [[ "${allow_report_edits}" == "allow-report-edits" ]]; then
    dirty="$(printf '%s\n' "${dirty}" \
      | grep -vE '^.. profiling/reports/(comprehensive-report|one-page-report)\.md$' || true)"
  fi
  [[ -z "${dirty}" ]] || {
    printf '%s\n' "${dirty}" >&2
    profile_die "working tree is not clean; authoritative builds/runs require committed source"
  }
}

profile_venv_dir() {
  printf '%s\n' "${XGRAMMAR_PROFILE_VENV:-${PROFILE_REPO_ROOT}/profiling/.venv}"
}

profile_activate_venv() {
  local venv_dir
  venv_dir="$(profile_venv_dir)"
  [[ -x "${venv_dir}/bin/python" ]] || profile_die \
    "profiling virtual environment is missing; run profiling/scripts/bootstrap_ubuntu.sh"
  # shellcheck disable=SC1091
  source "${venv_dir}/bin/activate"
  export PYTHONNOUSERSITE=1
  export PYTHONHASHSEED="${PYTHONHASHSEED:-0}"
}

profile_free_disk_gib() {
  df -Pk "${PROFILE_REPO_ROOT}" | awk 'NR == 2 {printf "%.2f", ($4 * 1024) / (1024^3)}'
}

profile_require_free_disk_gib() {
  local minimum_gib="$1"
  local available_kib
  available_kib="$(df -Pk "${PROFILE_REPO_ROOT}" | awk 'NR == 2 {print $4}')"
  [[ "${available_kib}" =~ ^[0-9]+$ ]] || profile_die "could not determine free disk space"
  (( available_kib >= minimum_gib * 1024 * 1024 )) || profile_die \
    "at least ${minimum_gib} GiB free disk is required; found $(profile_free_disk_gib) GiB"
}

profile_require_new_path() {
  local path="$1"
  [[ ! -e "${path}" ]] || profile_die \
    "refusing to overwrite existing path: ${path}; choose a new run/output path"
}
