#!/usr/bin/env bash
set -euo pipefail

# 기존 배포 자동화가 사용하던 파일명은 유지하고, 지원 OS와 service Python 검사는
# OS 중립 preflight의 단일 구현을 사용한다.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${script_dir}/preflight_ubuntu_runtime.sh" "$@"
