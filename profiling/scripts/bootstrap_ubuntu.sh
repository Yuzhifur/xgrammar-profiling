#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

profile_require_authoritative_source

[[ -r /etc/os-release ]] || profile_die "cannot identify the operating system"
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || profile_die "this bootstrap supports Ubuntu only (found ${ID:-unknown})"

profile_require_free_disk_gib 12

mem_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
[[ "${mem_kib}" =~ ^[0-9]+$ ]] || profile_die "could not read total RAM"
(( mem_kib >= 7 * 1024 * 1024 )) || profile_die \
  "the profiling plan requires an 8 GB droplet (at least 7 GiB visible RAM)"

if (( EUID == 0 )); then
  sudo_cmd=()
else
  profile_require_command sudo
  sudo_cmd=(sudo)
  "${sudo_cmd[@]}" -v
fi

profile_note "Installing build/runtime packages (no distribution upgrade and no model weights)."
"${sudo_cmd[@]}" apt-get update
DEBIAN_FRONTEND=noninteractive "${sudo_cmd[@]}" apt-get install -y --no-install-recommends \
  build-essential \
  ca-certificates \
  cmake \
  curl \
  git \
  jq \
  ninja-build \
  numactl \
  pkg-config \
  python3-dev \
  python3-pip \
  python3-venv \
  tmux \
  time

# linux-tools-$(uname -r) is unavailable on some DigitalOcean kernels. perf is optional, so a
# missing package must not block wall-clock/CPU/RSS measurements.
if ! command -v perf >/dev/null 2>&1; then
  profile_note "Trying optional perf installation; failure only disables sampling/counters."
  if apt-cache show "linux-tools-$(uname -r)" >/dev/null 2>&1; then
    if ! DEBIAN_FRONTEND=noninteractive "${sudo_cmd[@]}" apt-get install -y --no-install-recommends \
      linux-tools-common "linux-tools-$(uname -r)"; then
      profile_note "perf packages could not be installed; record this as unavailable."
    fi
  elif ! DEBIAN_FRONTEND=noninteractive "${sudo_cmd[@]}" apt-get install -y \
    --no-install-recommends linux-tools-common linux-tools-generic; then
    profile_note "perf packages could not be installed; record this as unavailable."
  fi
fi

profile_note "Initializing exactly the submodule revisions recorded by the implementation commit."
git -C "${PROFILE_REPO_ROOT}" submodule update --init --recursive

venv_dir="$(profile_venv_dir)"
if [[ ! -d "${venv_dir}" ]]; then
  python3 -m venv "${venv_dir}"
fi
# shellcheck disable=SC1091
source "${venv_dir}/bin/activate"
export PYTHONNOUSERSITE=1
python -m pip install --upgrade pip setuptools wheel
python -m pip install --editable "${PROFILE_REPO_ROOT}/profiling[prepare,test]"

# Install shared build/runtime/test dependencies once. Variant wheels are later installed with
# --no-deps into separate targets, so Torch and the rest are not copied six times on a 25 GB disk.
# Installing the Python packages does not download any model weights.
# Use PyTorch's CPU wheel index on the CPU-only droplet. This avoids several gigabytes of unused
# CUDA runtime packages; Triton is intentionally absent because no GPU kernel is in scope.
python -m pip install --index-url https://download.pytorch.org/whl/cpu "torch>=1.10.0"
python -m pip install \
  "apache-tvm-ffi>=0.1.10" \
  "scikit-build-core>=0.10.0" \
  pydantic \
  "transformers>=4.38.0" \
  numpy \
  "typing-extensions>=4.9.0" \
  protobuf \
  "cohere_melody==0.13.0; python_version >= '3.10'" \
  "sentencepiece<0.2.2; python_version < '3.10'" \
  "sentencepiece; python_version >= '3.10'" \
  tiktoken

profile_require_free_disk_gib 8

profile_note "Bootstrap complete. Environment summary follows."
printf 'source_commit=%s\n' "$(git -C "${PROFILE_REPO_ROOT}" rev-parse HEAD)"
printf 'release_base=%s\n' "${XGRAMMAR_RELEASE_COMMIT}"
printf 'ubuntu=%s\n' "${PRETTY_NAME:-unknown}"
printf 'kernel=%s\n' "$(uname -r)"
printf 'virtualization=%s\n' "$(systemd-detect-virt 2>/dev/null || printf 'unknown')"
printf 'python=%s\n' "$(python --version 2>&1)"
printf 'cmake=%s\n' "$(cmake --version | head -n 1)"
printf 'ninja=%s\n' "$(ninja --version)"
printf 'free_disk_gib=%s\n' "$(profile_free_disk_gib)"
printf 'venv=%s\n' "${venv_dir}"
if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
  printf 'cgroup=v2\n'
  printf 'cgroup_root_writable=%s\n' "$( [[ -w /sys/fs/cgroup ]] && printf yes || printf no )"
else
  printf 'cgroup=not-v2-or-unavailable\n'
fi
printf 'perf=%s\n' "$(command -v perf 2>/dev/null || printf unavailable)"

profile_note "Next: source ${venv_dir}/bin/activate, build the isolated variants, then prepare data."
