#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  run_suite.sh validate NEW_OUTPUT_DIR
  run_suite.sh pilot QUALIFICATION_JSON NEW_PILOT_DIR
  run_suite.sh freeze PILOT_DIR [NEW_FROZEN_CONFIG]
  run_suite.sh run FROZEN_CONFIG NEW_RUN_DIR
  run_suite.sh analyze RUN_DIR
  run_suite.sh verify-reports RUN_DIR

Environment:
  XGRAMMAR_PROFILE_COMMIT   required exact 40-hex implementation commit
  XGRAMMAR_VARIANT_ROOT     isolated build root (defaults to commit-specific root)
  XGRAMMAR_PROFILE_CONFIG   main config (defaults to profiling/configs/v0.2.7.json)
  XGRAMMAR_PILOT_CONFIG     pilot config (defaults to profiling/configs/pilot.json)
  XGRAMMAR_PROFILE_CPU      optional primary worker CPU frozen by the pilot
  XGRAMMAR_PROFILE_VENV     optional profiling virtual environment path

Every command refuses a dirty/wrong checkout. validate, pilot, and run require new output paths so
raw evidence is never overwritten. Extra ad-hoc CLI flags are intentionally not accepted: change
and commit the configuration, or invoke the harness directly for non-authoritative exploration.
EOF
}

[[ $# -ge 1 ]] || {
  usage >&2
  exit 2
}

command_name="$1"
shift
if [[ "${command_name}" == "-h" || "${command_name}" == "--help" || "${command_name}" == "help" ]]; then
  usage
  exit 0
fi

if [[ "${command_name}" == "verify-reports" ]]; then
  # Final findings necessarily modify the two tracked report templates. Continue to reject every
  # source, config, script, or other tracked/untracked change.
  profile_require_authoritative_source allow-report-edits
else
  profile_require_authoritative_source
fi
profile_activate_venv

variant_root="${XGRAMMAR_VARIANT_ROOT:-${PROFILE_REPO_ROOT}/profiling/build/variants/${XGRAMMAR_PROFILE_COMMIT}}"
profile_config="${XGRAMMAR_PROFILE_CONFIG:-${PROFILE_REPO_ROOT}/profiling/configs/v0.2.7.json}"
pilot_config="${XGRAMMAR_PILOT_CONFIG:-${PROFILE_REPO_ROOT}/profiling/configs/pilot.json}"

[[ -d "${variant_root}" ]] || profile_die "variant root does not exist: ${variant_root}"
[[ -f "${profile_config}" ]] || profile_die "main config does not exist: ${profile_config}"
[[ -f "${pilot_config}" ]] || profile_die "pilot config does not exist: ${pilot_config}"

if [[ -n "${XGRAMMAR_PROFILE_CPU:-}" ]]; then
  profile_require_command taskset
  [[ "${XGRAMMAR_PROFILE_CPU}" =~ ^[0-9]+$ ]] || profile_die "XGRAMMAR_PROFILE_CPU must be one CPU number"
  grep -qw "${XGRAMMAR_PROFILE_CPU}" /sys/devices/system/cpu/online 2>/dev/null || {
    # The kernel's online format may be a range (for example 0-3), so let taskset perform the final
    # validation when a direct word lookup is inconclusive.
    taskset --cpu-list "${XGRAMMAR_PROFILE_CPU}" true >/dev/null 2>&1 || profile_die \
      "CPU ${XGRAMMAR_PROFILE_CPU} is not available"
  }
fi

case "${command_name}" in
  validate)
    [[ $# -eq 1 ]] || profile_die "validate requires NEW_OUTPUT_DIR"
    validation_output="$1"
    profile_require_new_path "${validation_output}"
    python -m xgrammar_profile.cli validate --all \
      --config "${profile_config}" --variant-root "${variant_root}" --output "${validation_output}"

    pristine_cmake="${variant_root}/pristine/cmake-build"
    pristine_python="${variant_root}/pristine/site-packages"
    [[ -f "${pristine_cmake}/CTestTestfile.cmake" ]] || profile_die \
      "pristine CTest metadata is missing: ${pristine_cmake}/CTestTestfile.cmake"
    [[ -d "${pristine_python}/xgrammar" ]] || profile_die \
      "pristine Python package is missing: ${pristine_python}/xgrammar"

    cxx_test_count="$(ctest --test-dir "${pristine_cmake}" --show-only=json-v1 \
      | jq '.tests | length')"
    (( cxx_test_count > 0 )) || profile_die "pristine CTest build contains no tests"

    set +e
    ctest --test-dir "${pristine_cmake}" --output-on-failure \
      >"${validation_output}/pristine-ctest.log" 2>&1
    ctest_status=$?
    PYTHONPATH="${pristine_python}:${PROFILE_REPO_ROOT}/profiling" PYTHONNOUSERSITE=1 \
      python -m pytest "${PROFILE_REPO_ROOT}/tests" "${PROFILE_REPO_ROOT}/profiling/tests" \
      -m "not hf_token_required" --junitxml "${validation_output}/pristine-pytest.xml" \
      >"${validation_output}/pristine-pytest.log" 2>&1
    pytest_status=$?
    set -e

    jq -n \
      --argjson cxx_test_count "${cxx_test_count}" \
      --argjson ctest_exit "${ctest_status}" \
      --argjson pytest_exit "${pytest_status}" \
      '{
        schema_version: 1,
        cxx_test_count: $cxx_test_count,
        pristine_ctest_exit: $ctest_exit,
        pristine_pytest_exit: $pytest_exit,
        passed: ($ctest_exit == 0 and $pytest_exit == 0)
      }' >"${validation_output}/baseline-tests.json"

    if (( ctest_status != 0 || pytest_status != 0 )); then
      tail -n 80 "${validation_output}/pristine-ctest.log" >&2 || true
      tail -n 80 "${validation_output}/pristine-pytest.log" >&2 || true
      profile_die "pristine C++ or Python test suite failed; see ${validation_output}"
    fi

    # The no-repeat-compression ablation intentionally changes representation. These ten upstream
    # tests assert RepeatRef presence or exact normalized serialization rather than language
    # semantics. They are the entire allowlist: run the rest of the suite normally, then require
    # every allowlisted node to fail as an assertion (not error or skip) when run individually.
    no_repeat_expected_failures=(
      "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_exact"
      "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_range"
      "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_boundary"
      "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_multichar_rule"
      "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_range_from_zero"
      "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_nested_inner"
      "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_nested_outer"
      "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_sequence_with_repeat"
      "tests/python/test_grammar_matcher_ebnf.py::test_repeat_ref_complex_nested"
      "tests/python/test_grammar_parser.py::test_repetition_normalizer"
    )

    timing_variant_suites='{}'
    for timing_variant in production-profile no-rule-cache no-repeat-compression; do
      timing_python="${variant_root}/${timing_variant}/site-packages"
      [[ -d "${timing_python}/xgrammar" ]] || profile_die \
        "timing-variant Python package is missing for ${timing_variant}"
      pytest_extra=()
      if [[ "${timing_variant}" == "no-repeat-compression" ]]; then
        for node_id in "${no_repeat_expected_failures[@]}"; do
          pytest_extra+=(--deselect "${node_id}")
        done
      fi
      set +e
      PYTHONPATH="${timing_python}:${PROFILE_REPO_ROOT}/profiling" PYTHONNOUSERSITE=1 \
        python -m pytest "${PROFILE_REPO_ROOT}/tests" "${PROFILE_REPO_ROOT}/profiling/tests" \
        -m "not hf_token_required" "${pytest_extra[@]}" \
        --junitxml "${validation_output}/${timing_variant}-full.xml" \
        >"${validation_output}/${timing_variant}-full.log" 2>&1
      timing_pytest_status=$?
      set -e
      timing_log_sha="$(sha256sum "${validation_output}/${timing_variant}-full.log" | awk '{print $1}')"
      timing_xml_sha="$(sha256sum "${validation_output}/${timing_variant}-full.xml" | awk '{print $1}')"
      timing_suite="$(jq -n \
        --arg status "$( (( timing_pytest_status == 0 )) && printf passed || printf failed )" \
        --arg command "python -m pytest tests profiling/tests -m 'not hf_token_required' [${timing_variant}]" \
        --argjson exit_code "${timing_pytest_status}" \
        --arg log_sha "${timing_log_sha}" \
        --arg xml_sha "${timing_xml_sha}" \
        --argjson deselected "$( \
          [[ "${timing_variant}" == "no-repeat-compression" ]] \
            && printf '%s' "${#no_repeat_expected_failures[@]}" || printf '0' \
        )" \
        '{status: $status, command: $command, exit_code: $exit_code, expected_mechanism_tests_deselected: $deselected, log_sha256: $log_sha, junit_xml_sha256: $xml_sha}')"
      timing_variant_suites="$(jq -c \
        --arg name "full_python_${timing_variant}" --argjson suite "${timing_suite}" \
        '. + {($name): $suite}' <<<"${timing_variant_suites}")"
      if (( timing_pytest_status != 0 )); then
        tail -n 100 "${validation_output}/${timing_variant}-full.log" >&2 || true
        profile_die "non-allowlisted Python suite failure for ${timing_variant}"
      fi
    done
    printf '%s\n' "${timing_variant_suites}" | jq . \
      >"${validation_output}/timing-variant-suites.json"

    expected_ids='[]'
    expected_observed='[]'
    expected_unexpected='[]'
    expected_index=0
    no_repeat_python="${variant_root}/no-repeat-compression/site-packages"
    for node_id in "${no_repeat_expected_failures[@]}"; do
      expected_ids="$(jq -c --arg node_id "${node_id}" '. + [$node_id]' <<<"${expected_ids}")"
      expected_log="${validation_output}/no-repeat-expected-${expected_index}.log"
      expected_xml="${validation_output}/no-repeat-expected-${expected_index}.xml"
      set +e
      PYTHONPATH="${no_repeat_python}:${PROFILE_REPO_ROOT}/profiling" PYTHONNOUSERSITE=1 \
        python -m pytest "${PROFILE_REPO_ROOT}/${node_id}" --junitxml "${expected_xml}" \
        >"${expected_log}" 2>&1
      expected_exit=$?
      python -c \
        'import sys, xml.etree.ElementTree as ET; root=ET.parse(sys.argv[1]).getroot(); suite=root if root.tag.endswith("testsuite") else root.find("testsuite"); ok=suite is not None and int(suite.attrib.get("tests", 0)) == 1 and int(suite.attrib.get("failures", 0)) == 1 and int(suite.attrib.get("errors", 0)) == 0 and int(suite.attrib.get("skipped", 0)) == 0; raise SystemExit(0 if ok else 1)' \
        "${expected_xml}"
      xml_is_expected_failure=$?
      set -e
      expected_log_sha="$(sha256sum "${expected_log}" | awk '{print $1}')"
      expected_xml_sha="$(sha256sum "${expected_xml}" | awk '{print $1}')"
      if (( expected_exit == 1 && xml_is_expected_failure == 0 )); then
        expected_item="$(jq -n --arg node_id "${node_id}" --arg log_sha "${expected_log_sha}" \
          --arg xml_sha "${expected_xml_sha}" \
          '{node_id: $node_id, status: "expected_failure", log_sha256: $log_sha, junit_xml_sha256: $xml_sha}')"
        expected_observed="$(jq -c --argjson item "${expected_item}" '. + [$item]' \
          <<<"${expected_observed}")"
      else
        expected_item="$(jq -n --arg node_id "${node_id}" --argjson exit_code "${expected_exit}" \
          --argjson xml_expected "${xml_is_expected_failure}" \
          '{node_id: $node_id, status: "unexpected_outcome", exit_code: $exit_code, expected_failure_xml_check_exit: $xml_expected}')"
        expected_unexpected="$(jq -c --argjson item "${expected_item}" '. + [$item]' \
          <<<"${expected_unexpected}")"
      fi
      expected_index=$((expected_index + 1))
    done
    jq -n \
      --arg variant "no-repeat-compression" \
      --argjson ids "${expected_ids}" \
      --argjson observed "${expected_observed}" \
      --argjson unexpected "${expected_unexpected}" \
      '{
        variant: $variant,
        rationale: "representation-only RepeatRef/serialization assertions disabled by the ablation",
        expected_node_ids: $ids,
        observed_expected_failures: $observed,
        unexpected_outcomes: $unexpected,
        zero_unexplained_failures: ($unexpected | length == 0),
        semantic_replacements: [
          "validation.json repetition_cases at 127/128/129/130 with family-specific transitions",
          "tests/python/test_profiling_hooks.py::test_compiled_structure_stats_report_repetition_ablation"
        ]
      }' >"${validation_output}/expected-disabled.json"
    jq -e \
      '(.expected_node_ids | length) == 10 and (.observed_expected_failures | length) == 10 and (.unexpected_outcomes | length) == 0 and .zero_unexplained_failures == true' \
      "${validation_output}/expected-disabled.json" >/dev/null || profile_die \
      "no-repeat expected-failure allowlist did not match exactly; see expected-disabled.json"

    variant_hook_suites='{}'
    variant_hook_failed=0
    for hook_variant in \
      production-profile no-rule-cache no-repeat-compression \
      production-diagnostic no-rule-cache-diagnostic; do
      hook_python="${variant_root}/${hook_variant}/site-packages"
      [[ -d "${hook_python}/xgrammar" ]] || profile_die \
        "profiling-hook test package is missing for ${hook_variant}"
      set +e
      PYTHONPATH="${hook_python}:${PROFILE_REPO_ROOT}/profiling" PYTHONNOUSERSITE=1 \
        python -m pytest "${PROFILE_REPO_ROOT}/tests/python/test_profiling_hooks.py" \
        --junitxml "${validation_output}/${hook_variant}-hooks.xml" \
        >"${validation_output}/${hook_variant}-hooks.log" 2>&1
      hook_status=$?
      set -e
      hook_log_sha="$(sha256sum "${validation_output}/${hook_variant}-hooks.log" | awk '{print $1}')"
      hook_xml_sha="$(sha256sum "${validation_output}/${hook_variant}-hooks.xml" | awk '{print $1}')"
      if (( hook_status != 0 )); then
        variant_hook_failed=1
      fi
      hook_suite="$(jq -n \
        --arg status "$( (( hook_status == 0 )) && printf passed || printf failed )" \
        --arg command "python -m pytest tests/python/test_profiling_hooks.py [${hook_variant}]" \
        --argjson exit_code "${hook_status}" \
        --arg log_sha "${hook_log_sha}" \
        --arg xml_sha "${hook_xml_sha}" \
        '{status: $status, command: $command, exit_code: $exit_code, log_sha256: $log_sha, junit_xml_sha256: $xml_sha}')"
      variant_hook_suites="$(jq -c \
        --arg name "profiling_hooks_${hook_variant}" --argjson suite "${hook_suite}" \
        '. + {($name): $suite}' <<<"${variant_hook_suites}")"
    done
    printf '%s\n' "${variant_hook_suites}" | jq . \
      >"${validation_output}/variant-hook-tests.json"
    if (( variant_hook_failed != 0 )); then
      tail -n 80 "${validation_output}"/*-hooks.log >&2 || true
      profile_die "one or more profiling-hook variant tests failed; see ${validation_output}"
    fi

    differential_validation="${validation_output}/validation.json"
    [[ -f "${differential_validation}" ]] || profile_die \
      "harness did not produce ${differential_validation}"
    jq -e \
      '.passed == true
       and (.variant_manifest_hashes | type == "object")
       and (.tokenizer_manifest_sha256 | length == 64)
       and (.bfcl_manifest_sha256 | length == 64)
       and (.bfcl_revision | test("^[0-9a-f]{40}$"))
       and (.bfcl_traces_sha256 | length == 64)' \
      "${differential_validation}" >/dev/null || profile_die \
      "differential validation did not produce passing, asset-bound evidence"
    differential_sha="$(sha256sum "${differential_validation}" | awk '{print $1}')"
    differential_raw="${validation_output}/raw/validation.jsonl"
    [[ -s "${differential_raw}" ]] || profile_die \
      "differential validation raw JSONL is missing or empty: ${differential_raw}"
    differential_raw_sha="$(sha256sum "${differential_raw}" | awk '{print $1}')"
    differential_record_count="$(wc -l <"${differential_raw}" | tr -d '[:space:]')"
    [[ "${differential_record_count}" =~ ^[1-9][0-9]*$ ]] || profile_die \
      "differential validation raw JSONL has an invalid record count"
    ctest_log_sha="$(sha256sum "${validation_output}/pristine-ctest.log" | awk '{print $1}')"
    pytest_log_sha="$(sha256sum "${validation_output}/pristine-pytest.log" | awk '{print $1}')"
    pytest_xml_sha="$(sha256sum "${validation_output}/pristine-pytest.xml" | awk '{print $1}')"

    jq -n \
      --slurpfile differential "${differential_validation}" \
      --slurpfile baseline "${validation_output}/baseline-tests.json" \
      --slurpfile timing_suites "${validation_output}/timing-variant-suites.json" \
      --slurpfile variant_suites "${validation_output}/variant-hook-tests.json" \
      --slurpfile expected_disabled "${validation_output}/expected-disabled.json" \
      --arg differential_sha "${differential_sha}" \
      --arg differential_raw_sha "${differential_raw_sha}" \
      --argjson differential_record_count "${differential_record_count}" \
      --arg ctest_log_sha "${ctest_log_sha}" \
      --arg pytest_log_sha "${pytest_log_sha}" \
      --arg pytest_xml_sha "${pytest_xml_sha}" \
      '{
        schema_version: 1,
        passed: ($differential[0].passed == true and $baseline[0].passed == true),
        source_commit: $differential[0].source_commit,
        release_commit: $differential[0].release_commit,
        config_hash: $differential[0].config_hash,
        variant_manifest_hashes: $differential[0].variant_manifest_hashes,
        tokenizer_manifest_sha256: $differential[0].tokenizer_manifest_sha256,
        bfcl_manifest_sha256: $differential[0].bfcl_manifest_sha256,
        bfcl_revision: $differential[0].bfcl_revision,
        bfcl_traces_sha256: $differential[0].bfcl_traces_sha256,
        bfcl_production_variant_manifest_sha256: $differential[0].bfcl_production_variant_manifest_sha256,
        bfcl_tokenizer_manifest_sha256: $differential[0].bfcl_tokenizer_manifest_sha256,
        bfcl_validation_build_config: $differential[0].bfcl_validation_build_config,
        differential_validation_sha256: $differential_sha,
        expected_disabled: $expected_disabled[0],
        suites: ({
          differential: {
            status: (if $differential[0].passed then "passed" else "failed" end),
            command: "python -m xgrammar_profile.cli validate --all",
            record_count: $differential_record_count,
            raw_jsonl_sha256: $differential_raw_sha
          },
          pristine_ctest: {
            status: (if $baseline[0].pristine_ctest_exit == 0 then "passed" else "failed" end),
            command: "ctest --test-dir <variant-root>/pristine/cmake-build --output-on-failure",
            exit_code: $baseline[0].pristine_ctest_exit,
            test_count: $baseline[0].cxx_test_count,
            log_sha256: $ctest_log_sha
          },
          pristine_pytest: {
            status: (if $baseline[0].pristine_pytest_exit == 0 then "passed" else "failed" end),
            command: "python -m pytest tests profiling/tests -m \"not hf_token_required\"",
            exit_code: $baseline[0].pristine_pytest_exit,
            log_sha256: $pytest_log_sha,
            junit_xml_sha256: $pytest_xml_sha
          },
          no_repeat_semantic_replacements: {
            status: "passed",
            command: "differential repetition qualification plus focused ablation hook",
            evidence: $expected_disabled[0].semantic_replacements
          }
        } + $timing_suites[0] + $variant_suites[0])
      }' >"${validation_output}/qualification.json"
    jq -e \
      '.passed == true
       and (.variant_manifest_hashes | length == 6)
       and .bfcl_production_variant_manifest_sha256 == .variant_manifest_hashes["production-profile"]
       and .bfcl_tokenizer_manifest_sha256 == .tokenizer_manifest_sha256
       and (.bfcl_validation_build_config | type == "object")
       and all(.suites[]; .status == "passed")
       and .expected_disabled.zero_unexplained_failures == true' \
      "${validation_output}/qualification.json" >/dev/null || profile_die \
      "generated qualification evidence failed its final consistency check"
    profile_note "Passing qualification evidence: ${validation_output}/qualification.json"
    ;;
  pilot)
    [[ $# -eq 2 ]] || profile_die "pilot requires QUALIFICATION_JSON NEW_PILOT_DIR"
    [[ -f "$1" ]] || profile_die "qualification evidence does not exist: $1"
    jq -e '.schema_version == 1 and .passed == true' "$1" >/dev/null || profile_die \
      "qualification evidence is invalid or not passing: $1"
    profile_require_new_path "$2"
    profile_require_free_disk_gib 8
    python -m xgrammar_profile.cli pilot --config "${pilot_config}" \
      --qualification "$1" --run-dir "$2" --variant-root "${variant_root}"
    ;;
  freeze)
    [[ $# -ge 1 && $# -le 2 ]] || profile_die "freeze requires PILOT_DIR [NEW_FROZEN_CONFIG]"
    [[ -d "$1" ]] || profile_die "pilot directory does not exist: $1"
    frozen_output="${2:-$1/frozen-config.json}"
    profile_require_new_path "${frozen_output}"
    python -m xgrammar_profile.cli freeze --pilot-results "$1" --output "${frozen_output}"
    ;;
  run)
    [[ $# -eq 2 ]] || profile_die "run requires FROZEN_CONFIG NEW_RUN_DIR"
    [[ -f "$1" ]] || profile_die "frozen config does not exist: $1"
    profile_require_new_path "$2"
    profile_require_free_disk_gib 8
    python -m xgrammar_profile.cli run --config "$1" \
      --run-dir "$2" --variant-root "${variant_root}"
    ;;
  analyze)
    [[ $# -eq 1 ]] || profile_die "analyze requires RUN_DIR"
    [[ -d "$1" ]] || profile_die "run directory does not exist: $1"
    python -m xgrammar_profile.cli analyze --run "$1"
    ;;
  verify-reports)
    [[ $# -eq 1 ]] || profile_die "verify-reports requires RUN_DIR"
    [[ -d "$1" ]] || profile_die "run directory does not exist: $1"
    python -m xgrammar_profile.cli verify-reports --run "$1" \
      --comprehensive "${PROFILE_REPO_ROOT}/profiling/reports/comprehensive-report.md" \
      --one-page "${PROFILE_REPO_ROOT}/profiling/reports/one-page-report.md"
    ;;
  *)
    usage >&2
    profile_die "unknown subcommand: ${command_name}"
    ;;
esac
