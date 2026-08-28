#!/usr/bin/env bash
set -euo pipefail

pass() {
    printf '[PASS] %s\n' "$1"
}

fail() {
    printf '[FAIL] %s\n' "$1" >&2
    exit 1
}

if [[ ! -r /etc/os-release ]]; then
    fail 'OS 정보를 읽을 수 없습니다.'
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]] || ! dpkg --compare-versions "${VERSION_ID:-0}" ge '24.04'; then
    fail 'Ubuntu 24.04 이상이 필요합니다.'
fi
pass 'Ubuntu 24.04 이상 확인'

command -v python3 >/dev/null 2>&1 || fail 'python3 명령이 없습니다.'
command -v git >/dev/null 2>&1 || fail 'git 명령이 없습니다.'
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' \
    || fail 'Python 3.12 이상이 필요합니다.'
pass 'Python과 Git 실행 환경 확인'

for variable_name in DATABASE_URL SNAPSHOT_MATERIALIZATION_ROOT VSS_BASE_URL; do
    if [[ -z "${!variable_name:-}" ]]; then
        fail "${variable_name} 환경변수가 필요합니다."
    fi
done
pass '필수 환경변수 존재 확인(값은 출력하지 않음)'

# URL에 포함된 계정정보나 token을 출력하지 않고 형식만 검증한다.
python3 - <<'PY'
import os
from pathlib import Path
from urllib.parse import urlsplit

database = urlsplit(os.environ["DATABASE_URL"])
if database.scheme not in {"postgresql", "postgresql+asyncpg"}:
    raise SystemExit("DATABASE_URL은 PostgreSQL이어야 합니다.")
if database.username is None or database.hostname is None or not database.path.strip("/"):
    raise SystemExit("DATABASE_URL에 사용자, host와 database 이름이 필요합니다.")
if database.hostname != "127.0.0.1":
    raise SystemExit("동일 인스턴스 배포의 DATABASE_URL host는 127.0.0.1이어야 합니다.")

vss = urlsplit(os.environ["VSS_BASE_URL"])
if vss.scheme not in {"http", "https"} or vss.hostname is None:
    raise SystemExit("VSS_BASE_URL은 유효한 HTTP(S) URL이어야 합니다.")
if vss.username is not None or vss.password is not None:
    raise SystemExit("VSS_BASE_URL에 계정정보를 포함하지 마세요.")
if vss.hostname != "127.0.0.1":
    raise SystemExit("동일 인스턴스 배포의 VSS_BASE_URL host는 127.0.0.1이어야 합니다.")

root = Path(os.environ["SNAPSHOT_MATERIALIZATION_ROOT"])
if not root.is_absolute() or root == Path(root.anchor):
    raise SystemExit("SNAPSHOT_MATERIALIZATION_ROOT는 filesystem root가 아닌 절대경로여야 합니다.")

revision = os.environ.get("VSS_EXPECTED_SOURCE_REVISION", "")
if revision and (len(revision) != 40 or any(c not in "0123456789abcdefABCDEF" for c in revision)):
    raise SystemExit("VSS_EXPECTED_SOURCE_REVISION은 40자리 Git SHA여야 합니다.")
PY
pass 'DB·VSS URL과 materialization root 형식 확인'

materialization_root="${SNAPSHOT_MATERIALIZATION_ROOT}"
if [[ ! -d "${materialization_root}" ]]; then
    fail 'materialization root가 존재하지 않습니다. 운영 담당자가 먼저 생성해야 합니다.'
fi
if [[ -L "${materialization_root}" ]]; then
    fail 'materialization root는 symlink일 수 없습니다.'
fi

probe_dir="$(mktemp --directory --tmpdir="${materialization_root}" '.snapshot-preflight.XXXXXX')" \
    || fail 'service user가 materialization root에 쓸 수 없습니다.'
cleanup_probe() {
    rm -f -- "${probe_dir}/source" "${probe_dir}/promoted"
    rmdir -- "${probe_dir}" 2>/dev/null || true
}
trap cleanup_probe EXIT
printf 'snapshot-preflight\n' > "${probe_dir}/source"
mv -- "${probe_dir}/source" "${probe_dir}/promoted"
[[ -r "${probe_dir}/promoted" ]] || fail 'atomic rename 결과를 읽을 수 없습니다.'
cleanup_probe
trap - EXIT
pass 'materialization root 쓰기·atomic rename 확인'

# 실제 Backend와 같은 인증 header를 사용하되 URL과 token은 출력하지 않는다.
python3 - <<'PY'
import json
import os
from urllib.request import ProxyHandler, Request, build_opener

base_url = os.environ["VSS_BASE_URL"].rstrip("/")
headers = {"Accept": "application/json"}
token = os.environ.get("VSS_TOKEN", "")
if token:
    headers["X-VSS-Token"] = token
timeout = float(os.environ.get("VSS_READ_TIMEOUT_SECONDS", "10"))
request = Request(f"{base_url}/health", headers=headers, method="GET")
opener = build_opener(ProxyHandler({}))
with opener.open(request, timeout=timeout) as response:
    if response.status != 200:
        raise SystemExit("VSS /health가 HTTP 200을 반환하지 않았습니다.")
    payload = json.load(response)
if payload.get("ok") is not True:
    raise SystemExit("VSS /health 응답의 ok가 true가 아닙니다.")
for key in ("store", "ollama", "embed_model"):
    if not payload.get(key):
        raise SystemExit(f"VSS /health 응답에 {key}가 없습니다.")
PY
pass 'VSS /health 인증·응답 계약 확인'

printf '[WAIT] PostgreSQL 실제 연결, shared path 양방향 확인과 인덱싱은 별도 E2E가 필요합니다.\n'
