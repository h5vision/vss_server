#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

module_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="full"
require_aws_contract="false"

usage() {
    cat <<'EOF'
Usage: scripts/verify_module_sandbox.sh [--quick] [--aws-contract]

  --quick         Run Phase 7 focused tests instead of the full pytest suite.
  --aws-contract  Require Ubuntu 22.04 and Python 3.10, matching the AWS host.

The script uses only temporary SQLite/local Git/mock HTTP resources. It does not
call live VSS, Ollama, GitHub, GitLab, PostgreSQL, systemd, or external networks.
EOF
}

for argument in "$@"; do
    case "${argument}" in
        --quick)
            mode="quick"
            ;;
        --aws-contract)
            require_aws_contract="true"
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            printf '[FAIL] 지원하지 않는 인자입니다: %s\n' "${argument}" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -n "${SNAPSHOT_SANDBOX_PYTHON:-}" ]]; then
    sandbox_python="${SNAPSHOT_SANDBOX_PYTHON}"
elif [[ -x "${module_root}/.venv/bin/python" ]]; then
    sandbox_python="${module_root}/.venv/bin/python"
elif [[ -x "${module_root}/.venv/Scripts/python.exe" ]]; then
    sandbox_python="${module_root}/.venv/Scripts/python.exe"
else
    printf '[FAIL] module .venv Python을 찾을 수 없습니다.\n' >&2
    exit 1
fi

command -v git >/dev/null 2>&1 || {
    printf '[FAIL] git 명령이 필요합니다.\n' >&2
    exit 1
}

sandbox_parent="${TMPDIR:-/tmp}"
sandbox_root="$(mktemp -d "${sandbox_parent%/}/vss-module-sandbox.XXXXXX")"

cleanup() {
    case "${sandbox_root}" in
        "${sandbox_parent%/}"/vss-module-sandbox.*)
            rm -rf -- "${sandbox_root}"
            ;;
        *)
            printf '[WARN] 예상하지 못한 sandbox 경로라 자동 정리를 건너뜁니다.\n' >&2
            ;;
    esac
}
trap cleanup EXIT
trap 'printf "[FAIL] sandbox 검증이 line %s에서 중단됐습니다.\n" "${LINENO}" >&2' ERR

mkdir -p "${sandbox_root}/snapshots"

export PYTHONDONTWRITEBYTECODE=1

cd "${module_root}"

"${sandbox_python}" - <<'PY'
import sys

if not ((3, 10) <= sys.version_info[:2] < (3, 15)):
    raise SystemExit("지원 Python은 3.10 이상, 3.15 미만이어야 합니다.")
PY
printf '[PASS] 지원 Python과 격리 환경 확인\n'

if [[ "${require_aws_contract}" == "true" ]]; then
    [[ -r /etc/os-release ]] || {
        printf '[FAIL] AWS contract 검증에는 /etc/os-release가 필요합니다.\n' >&2
        exit 1
    }
    # shellcheck disable=SC1091
    source /etc/os-release
    [[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "22.04" ]] || {
        printf '[FAIL] --aws-contract는 Ubuntu 22.04에서만 실행할 수 있습니다.\n' >&2
        exit 1
    }
    "${sandbox_python}" - <<'PY'
import sys

if sys.version_info[:2] != (3, 10):
    raise SystemExit("AWS contract는 Python 3.10을 요구합니다.")
PY
    printf '[PASS] AWS Ubuntu 22.04 / Python 3.10 contract 확인\n'
fi

"${sandbox_python}" -c \
    'import alembic, aiosqlite, fastapi, httpx2, pydantic, sqlalchemy; print("[PASS] Python dependency import")'
"${sandbox_python}" -m compileall -q backend alembic tests scripts
printf '[PASS] compileall\n'
"${sandbox_python}" -m ruff check backend admin_web tests alembic scripts
printf '[PASS] Ruff\n'

phase_tests=(
    tests/unit/change_requests
    tests/unit/repository_tags
    tests/unit/commit_catalog
    tests/unit/repository_collection/test_git_client.py
    tests/integration/test_change_request_provider_flow.py
    tests/integration/test_commit_catalog_flow.py
    tests/integration/test_repository_collection_flow.py
    tests/integration/test_vss_source_api.py
)
"${sandbox_python}" -m pytest -q "${phase_tests[@]}"
printf '[PASS] Phase 7 contract/unit/integration sandbox\n'

if [[ "${mode}" == "full" ]]; then
    "${sandbox_python}" -m pytest -q
    printf '[PASS] 전체 pytest 회귀\n'
fi

alembic_head="$("${sandbox_python}" -m alembic heads)"
[[ "${alembic_head}" == "0009_repository_sync_fencing (head)" ]] || {
    printf '[FAIL] 예상 Alembic head가 아닙니다: %s\n' "${alembic_head}" >&2
    exit 1
}
printf '[PASS] Alembic head 0009_repository_sync_fencing\n'

DATABASE_URL='postgresql+asyncpg://snapshot:placeholder@127.0.0.1/snapshot' \
    "${sandbox_python}" -m alembic upgrade head --sql > "${sandbox_root}/upgrade.sql"
DATABASE_URL='postgresql+asyncpg://snapshot:placeholder@127.0.0.1/snapshot' \
    "${sandbox_python}" -m alembic downgrade 0009_repository_sync_fencing:base --sql \
    > "${sandbox_root}/downgrade.sql"
grep -q 'lease_generation' "${sandbox_root}/upgrade.sql"
grep -q 'CREATE TABLE snapshot.repository_tags' "${sandbox_root}/upgrade.sql"
grep -q 'DROP TABLE snapshot.repository_tags' "${sandbox_root}/downgrade.sql"
printf '[PASS] PostgreSQL offline upgrade/downgrade DDL\n'

git diff --check -- .
printf '[PASS] git diff whitespace 검사\n'

printf '\nMODULE SANDBOX VERIFICATION: PASS\n'
printf 'mode=%s aws_contract=%s\n' "${mode}" "${require_aws_contract}"
