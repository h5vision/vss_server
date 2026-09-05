#!/usr/bin/env bash
set -euo pipefail

module_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
service_python="${SNAPSHOT_SERVICE_PYTHON:-${module_root}/.venv/bin/python}"

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
if [[ "${ID:-}" != "ubuntu" ]] || ! dpkg --compare-versions "${VERSION_ID:-0}" ge '22.04'; then
    fail 'Ubuntu 22.04 이상이 필요합니다.'
fi
pass "지원 Ubuntu 확인 (${VERSION_ID})"

command -v git >/dev/null 2>&1 || fail 'git 명령이 없습니다.'
if [[ "${service_python}" != /* ]]; then
    fail 'SNAPSHOT_SERVICE_PYTHON은 절대경로여야 합니다.'
fi
if [[ ! -x "${service_python}" ]]; then
    fail 'systemd가 사용할 Python 실행 파일을 찾을 수 없습니다.'
fi

# Ubuntu 22.04의 system python은 3.10일 수 있으므로 python3가 아니라 systemd
# ExecStart와 동일한 interpreter를 직접 검사한다.
if ! "${service_python}" -c \
    'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] < (3, 15)))'; then
    fail '서비스 Python은 3.10 이상, 3.15 미만이어야 합니다.'
fi
service_python_version="$("${service_python}" -c 'import platform; print(platform.python_version())')"
pass "서비스 Python ${service_python_version}와 Git 실행 환경 확인"

for variable_name in \
    DATABASE_URL \
    SNAPSHOT_REPOSITORY_ROOT \
    SNAPSHOT_MATERIALIZATION_ROOT \
    SNAPSHOT_VSS_API_TOKEN \
    VSS_BASE_URL; do
    if [[ -z "${!variable_name:-}" ]]; then
        fail "${variable_name} 환경변수가 필요합니다."
    fi
done
pass '필수 환경변수 존재 확인(값은 출력하지 않음)'

# URL에 포함된 계정정보나 token을 출력하지 않고 형식만 검증한다.
"${service_python}" - <<'PY'
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

repository_root = Path(os.environ["SNAPSHOT_REPOSITORY_ROOT"])
snapshot_root = Path(os.environ["SNAPSHOT_MATERIALIZATION_ROOT"])
if not repository_root.is_absolute() or repository_root == Path(repository_root.anchor):
    raise SystemExit("SNAPSHOT_REPOSITORY_ROOT must be an absolute non-root directory")
if any(p.is_symlink() for p in (repository_root, *repository_root.parents)):
    raise SystemExit("Repository root must not contain symlinks")
if not repository_root.is_dir():
    raise SystemExit("Repository root must exist before service startup")
repo_resolved, snapshot_resolved = repository_root.resolve(), snapshot_root.resolve()
if (repo_resolved == snapshot_resolved or repo_resolved in snapshot_resolved.parents
        or snapshot_resolved in repo_resolved.parents):
    raise SystemExit("Repository and Snapshot roots must be separate non-nested directories")
with tempfile.TemporaryDirectory(prefix=".repository-preflight-", dir=repository_root) as probe:
    source = Path(probe) / "source"
    source.write_text("probe", encoding="ascii")
    source.rename(Path(probe) / "promoted")

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
"${service_python}" - <<'PY'
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
