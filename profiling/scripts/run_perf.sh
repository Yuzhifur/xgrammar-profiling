#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  run_perf.sh preflight NEW_OUTPUT_DIR
  run_perf.sh stat NEW_OUTPUT_DIR -- COMMAND [ARG ...]
  run_perf.sh record NEW_OUTPUT_DIR -- COMMAND [ARG ...]

`preflight` tests the exact hardware-stat and software-sampling operations promised by the plan.
Either or both may be unavailable on a VM; that is a recorded capability result, not a failed
benchmark. `stat` records cycles/instructions for one representative command; `record` captures
cpu-clock samples. Both are diagnostic and never write into authoritative raw JSONL.
EOF
}

[[ $# -ge 1 ]] || {
  usage >&2
  exit 2
}

subcommand="$1"
shift
if [[ "${subcommand}" == "-h" || "${subcommand}" == "--help" || "${subcommand}" == "help" ]]; then
  usage
  exit 0
fi
profile_require_authoritative_source

case "${subcommand}" in
  preflight)
    [[ $# -eq 1 ]] || profile_die "preflight requires NEW_OUTPUT_DIR"
    output_dir="$1"
    profile_require_new_path "${output_dir}"
    mkdir -p "${output_dir}"

    {
      printf 'date_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      printf 'kernel=%s\n' "$(uname -srvm)"
      printf 'perf_event_paranoid=%s\n' "$(cat /proc/sys/kernel/perf_event_paranoid 2>/dev/null || printf unavailable)"
      printf 'kptr_restrict=%s\n' "$(cat /proc/sys/kernel/kptr_restrict 2>/dev/null || printf unavailable)"
      if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
        printf 'cgroup_version=2\n'
        printf 'cgroup_root_writable=%s\n' "$( [[ -w /sys/fs/cgroup ]] && printf yes || printf no )"
        printf 'self_cgroup=%s\n' "$(awk -F: '$1 == "0" {print $3}' /proc/self/cgroup)"
      else
        printf 'cgroup_version=unavailable-or-v1\n'
      fi
    } >"${output_dir}/environment.txt"

    if ! command -v perf >/dev/null 2>&1; then
      printf 'perf executable unavailable\n' >"${output_dir}/hardware-stat.txt"
      printf 'perf executable unavailable\n' >"${output_dir}/software-sampling.txt"
      profile_note "perf is unavailable. Wall/CPU/RSS measurements remain authoritative."
      exit 0
    fi

    set +e
    perf stat -e cycles,instructions true >"${output_dir}/hardware-stat.txt" 2>&1
    hardware_status=$?
    perf record -e cpu-clock -g --output "${output_dir}/preflight.data" -- true \
      >"${output_dir}/software-sampling.txt" 2>&1
    sampling_status=$?
    set -e

    {
      printf 'hardware_stat_exit=%s\n' "${hardware_status}"
      printf 'software_sampling_exit=%s\n' "${sampling_status}"
      printf 'hardware_stat_available=%s\n' "$( (( hardware_status == 0 )) && printf yes || printf no )"
      printf 'software_sampling_available=%s\n' "$( (( sampling_status == 0 )) && printf yes || printf no )"
    } >"${output_dir}/capabilities.txt"

    profile_note "perf/cgroup preflight saved to ${output_dir}"
    ;;
  stat|record)
    [[ $# -ge 3 && "$2" == "--" ]] || profile_die \
      "${subcommand} requires NEW_OUTPUT_DIR -- COMMAND [ARG ...]"
    output_dir="$1"
    shift 2
    profile_require_new_path "${output_dir}"
    profile_require_command perf
    mkdir -p "${output_dir}"

    printf '%q ' "$@" >"${output_dir}/command.txt"
    printf '\n' >>"${output_dir}/command.txt"
    if [[ "${subcommand}" == "stat" ]]; then
      set +e
      perf stat -e cycles,instructions --output "${output_dir}/counters.txt" -- "$@" \
        >"${output_dir}/command.stdout" 2>"${output_dir}/command.stderr"
      perf_status=$?
      set -e
      printf 'perf_stat_exit=%s\n' "${perf_status}" >"${output_dir}/status.txt"
      (( perf_status == 0 )) || profile_die \
        "representative perf stat failed; preserved diagnostics in ${output_dir}"
      profile_note "hardware counters saved to ${output_dir}; they are diagnostic, not timing evidence"
    else
      perf record -e cpu-clock -g --call-graph dwarf --output "${output_dir}/perf.data" -- "$@" \
        >"${output_dir}/command.stdout" 2>"${output_dir}/command.stderr"
      perf report --stdio --input "${output_dir}/perf.data" \
        >"${output_dir}/report.txt" 2>"${output_dir}/report.stderr"
      profile_note "sampling profile saved to ${output_dir}; this is diagnostic, not timing evidence"
    fi
    ;;
  *)
    usage >&2
    profile_die "unknown subcommand: ${subcommand}"
    ;;
esac
