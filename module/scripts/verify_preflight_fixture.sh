#!/usr/bin/env bash
set -euo pipefail

module_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
verification_root="$(mktemp -d)"
server_pid=''

cleanup() {
    if [[ -n "${server_pid}" ]]; then
        kill "${server_pid}" 2>/dev/null || true
        wait "${server_pid}" 2>/dev/null || true
    fi
    rm -rf -- "${verification_root:?}"
}
trap cleanup EXIT

mkdir "${verification_root}/snapshots"
python3 -m http.server 18200 \
    --bind 127.0.0.1 \
    --directory "${module_root}/tests/fixtures/vss/preflight" \
    >"${verification_root}/http.log" 2>&1 &
server_pid="$!"

# 임시 서버가 요청을 받을 때까지 짧게 확인한다. 전체 대기 시간은 5초를 넘지 않는다.
for _ in {1..50}; do
    if python3 - <<'PY' >/dev/null 2>&1
from urllib.request import ProxyHandler, build_opener

with build_opener(ProxyHandler({})).open("http://127.0.0.1:18200/health", timeout=0.2):
    pass
PY
    then
        break
    fi
    sleep 0.1
done

export DATABASE_URL='postgresql+asyncpg://fixture:fixture@127.0.0.1:5432/fixture'
export SNAPSHOT_MATERIALIZATION_ROOT="${verification_root}/snapshots"
export SNAPSHOT_VSS_API_TOKEN='fixture-inbound-token'
export VSS_BASE_URL='http://127.0.0.1:18200'
export VSS_EXPECTED_SOURCE_REVISION='1111111111111111111111111111111111111111'

if DATABASE_URL='postgresql+asyncpg://fixture:fixture@192.0.2.10:5432/fixture' \
    bash "${module_root}/scripts/preflight_ubuntu_24_04.sh" \
    >"${verification_root}/non-loopback-db.log" 2>&1; then
    printf '[FAIL] 비루프백 PostgreSQL 주소가 사전검사를 통과했습니다.\n' >&2
    exit 1
fi
grep -q 'DATABASE_URL host는 127.0.0.1' \
    "${verification_root}/non-loopback-db.log"

if VSS_BASE_URL='http://192.0.2.11:8200' \
    bash "${module_root}/scripts/preflight_ubuntu_24_04.sh" \
    >"${verification_root}/non-loopback-vss.log" 2>&1; then
    printf '[FAIL] 비루프백 VSS 주소가 사전검사를 통과했습니다.\n' >&2
    exit 1
fi
grep -q 'VSS_BASE_URL host는 127.0.0.1' \
    "${verification_root}/non-loopback-vss.log"

printf '[PASS] 동일 인스턴스 DB·VSS 비루프백 주소 차단 확인\n'

bash "${module_root}/scripts/preflight_ubuntu_24_04.sh"
