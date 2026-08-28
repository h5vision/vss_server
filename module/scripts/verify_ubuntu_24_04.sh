#!/usr/bin/env bash
set -euo pipefail

module_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -r /etc/os-release ]]; then
    echo "OS_RELEASE_NOT_FOUND" >&2
    exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]] || ! dpkg --compare-versions "${VERSION_ID:-0}" ge "24.04"; then
    echo "UBUNTU_24_04_OR_NEWER_REQUIRED" >&2
    exit 1
fi

python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'
git --version

verification_root="$(mktemp -d)"
cleanup() {
    rm -rf -- "${verification_root:?}"
}
trap cleanup EXIT

# bytecode와 테스트 cache를 checkout 밖에 둔다. AWS에서 module을 읽기 전용으로
# mount해도 같은 검증 절차를 사용할 수 있어야 한다.
export PYTHONPYCACHEPREFIX="${verification_root}/pycache"
export PYTEST_ADDOPTS="-p no:cacheprovider"

python3 -m venv "${verification_root}/venv"
python_bin="${verification_root}/venv/bin/python"
"${python_bin}" -m pip install --quiet --upgrade pip
"${python_bin}" -m pip install --quiet -e "${module_root}[dev]"

cd "${module_root}"
bash -n scripts/*.sh
"${python_bin}" -m ruff check backend tests alembic scripts
"${python_bin}" -m compileall -q backend alembic tests scripts
"${python_bin}" -m pytest -q
