#!/usr/bin/env bash
set -euo pipefail

module_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_python="${SNAPSHOT_SERVICE_PYTHON:-$(command -v python3 || true)}"

if [[ ! -r /etc/os-release ]]; then
    echo "OS_RELEASE_NOT_FOUND" >&2
    exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]] || ! dpkg --compare-versions "${VERSION_ID:-0}" ge "22.04"; then
    echo "UBUNTU_22_04_OR_NEWER_REQUIRED" >&2
    exit 1
fi

if [[ -z "${runtime_python}" || ! -x "${runtime_python}" ]]; then
    echo "SERVICE_PYTHON_NOT_EXECUTABLE" >&2
    exit 1
fi
"${runtime_python}" -c \
    'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] < (3, 15)))'
git --version

verification_root="$(mktemp -d)"
cleanup() {
    rm -rf -- "${verification_root:?}"
}
trap cleanup EXIT

# bytecode와 테스트 cache를 checkout 밖에 둔다. AWS에서 module을 읽기 전용으로
# 제공해도 같은 검증 절차를 사용할 수 있어야 한다.
export PYTHONPYCACHEPREFIX="${verification_root}/pycache"
export PYTEST_ADDOPTS="-p no:cacheprovider"

"${runtime_python}" -m venv "${verification_root}/venv"
python_bin="${verification_root}/venv/bin/python"
"${python_bin}" -m pip install --quiet --upgrade pip
"${python_bin}" -m pip install --quiet -e "${module_root}[dev]"

cd "${module_root}"
bash -n scripts/*.sh
"${python_bin}" -m ruff check backend tests alembic scripts
"${python_bin}" -m compileall -q backend alembic tests scripts
"${python_bin}" -m pytest -q
