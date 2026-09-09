# Snapshot Backend module

## 2026-09-04 확정 운영 계약

이 절은 이전 문서의 충돌하는 자동 인덱싱·`vss_pull` 우선 표현보다 우선합니다.

- **VSS가 유일한 Indexer입니다.** Snapshot Module은 파일 수집 정책, chunking, embedding, BM25, vector/vector-store build·promote를 구현하거나 복제하지 않습니다. 실제 인덱싱은 `vss_server`의 `POST /index -> indexer.start_index()` 경로만 사용합니다.
- Repository 등록/동기화는 **인덱싱과 분리**합니다. 수집한 Repository는 `SNAPSHOT_REPOSITORY_ROOT=/home/ubuntu/repos` 아래 관리하고, sync는 clone/fetch·ref 관측·commit catalog 갱신까지만 수행하며 VSS `POST /index`를 자동 호출하지 않습니다.
- VSS에 넘길 입력은 mutable working copy가 아니라 `SNAPSHOT_MATERIALIZATION_ROOT=/home/ubuntu/vss-snapshots` 아래의 **검증된 immutable exact Snapshot**입니다. `VSS_REPOS_DIR=/home/ubuntu/repos`는 VSS의 repository 발견/표시 용도로 사용할 수 있지만 Module의 정식 `/index` 입력 경로는 아닙니다.
- 인덱싱 시작은 **Admin의 명시적 Index 요청**이 소유합니다. 목표 Admin API는 `POST /v1/admin/snapshots/{snapshot_id}/index`이며, materialized Snapshot만 대상으로 `project_root`, `project_id`, `force=false`, `briefing`, `note`를 VSS `POST /index`에 전달합니다. VSS의 `remote` clone 기능은 Module 연동 경로에서 사용하지 않습니다.
- Module은 VSS의 `GET /index/status`와 `GET /index/exists`를 관측하고, `state=done`뿐 아니라 `index.commit == snapshot.target_revision`까지 확인한 경우에만 Snapshot을 `completed`로 수렴시킵니다.
- 현재 운영 오케스트레이션 방향은 **`module_push`**이지만 의미는 “sync 시 자동 push”가 아니라 **Admin 요청으로 생성된 IndexCommand를 Module이 VSS에 제출**한다는 뜻입니다. `vss_pull`과 `/v1/internal/vss/*`는 provenance/read-model 및 향후 선택 기능으로 유지하며 현재 pre-rag VSS의 필수 data plane으로 간주하지 않습니다.
- Commit History/Compare는 Admin 분석 기능으로 유지합니다. **비교 결과로 reference commit SHA를 자동 선택하거나 VSS에 전달하는 기능, multi-revision 답변 context는 구현 보류**입니다.

> **현재 구현 상태:** PR 9.2-A managed repository/root split은 GitHub `module` commit `22d1082`로 반영됐고,
> PR 9.2-B는 Repository sync/materialization의 VSS 자동 제출 제거와 full gate를 완료했습니다.
> PR 9.2-C는 Admin `POST /v1/admin/snapshots/{snapshot_id}/index`, Operator RBAC, audit, Admin Web Index 버튼,
> exact immutable Snapshot 재검증과 VSS `force=false` 제출까지 구현했으며 전체 257 tests + sandbox를 통과했습니다.
> 다음 구현은 PR 9.2-D status/reconciler입니다.


이 디렉터리는 `vss_server/main`의 VSS 런타임과 섞이지 않는 독립 Snapshot Backend
모듈입니다. 사용자가 선택한 Repository/Branch의 commit SHA를 수집하고 `/home/ubuntu/repos`에
관리 Repository를 유지하며, 필요한 exact revision을 `/home/ubuntu/vss-snapshots`에 immutable
Snapshot으로 materialize하는 것이 기본 경계입니다. VSS 인덱싱은 Repository sync나 overlay
수신에 자동 결합하지 않고, Admin의 명시적 Index 요청에서만 VSS 서버의 `POST /index`를
호출합니다. 기존 `vision/frontend` overlay route는 현재 구현 호환 경계로만 유지합니다.

?? provenance read model? Repository/Branch, exact commit, Snapshot/VSS ??? ?????.
`/v1/internal/vss/*` pull? ?? ?? ???? ?? VSS ??? data plane?? ???? ????.
module? Chat?? ??? ???? ?? Git ??, immutable source? VSS `index.commit` ??? ?????.

?? ?? ???? Repository/Branch ??, immutable Snapshot materialization, commit catalog, Admin
history/compare, explicit VSS Index? exact completion reconciliation? ?????. `/v1/internal/vss/refs`?
`/context`? revision/branch selector? ?????. PR/MR catalog/provider ? Repository Tag current/history?
2026-09-09 ?? ??? ???? ?? VSS runtime??? ???? ?? ??? ???? ??????.
?? Alembic `0006`/`0008`? migration ???? ????, `0010_remove_unused_phase7a`? ? optional
table? ?? ??? ??? ? ?????. ???? ?? ??? migration? ?????.

VSS?? Module? `/v1/internal/vss/*`? ???? ? ??? 200/202? ??? warning log?
`snapshot.audit_logs`? `vss_inbound_request` ???? ????. ? ? ?? ? VSS route? 404? ????
query token/secret/password ?? ?? ?? redaction???. Admin ??
`GET /v1/admin/vss/request-failures`? Admin Web? `VSS request failures` ???? ??? ? ????.

Phase 7A-2 Repository commit catalog, parent graph? bounded scanner? ?????. ?? commit?
Snapshot/VSS index? ??? ?? ?? ??? ??? metadata graph? ??? ? ??? ?? commit?
on-demand Snapshot?? ?????. ?? catalog root? tracked Branch, Branch HEAD history, Snapshot???.

## 디렉터리 경계

```text
vss_server/
├─ vss/                 # main 소유, 이 모듈에서 수정하지 않음
└─ module/              # Snapshot Backend 변경분 전용
   ├─ backend/
   ├─ admin_web/
   ├─ docs/agent/
   ├─ tests/
   ├─ AGENTS.md
   ├─ main.py
   └─ pyproject.toml
```

이 프로젝트는 내부 Python package인 `backend*`, `admin_web*`를 설치합니다. VSS는 별도 서버로
배포하고 `VSS_BASE_URL`과 선택적 `VSS_TOKEN`으로 연결합니다. materialized
`project_root`는 VSS 서버 프로세스에서도 같은 경로로 읽을 수 있어야 합니다.

## 개발 검증

```powershell
cd module
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m compileall -q backend alembic tests scripts
.venv\Scripts\python.exe -m ruff check backend admin_web tests alembic scripts
.venv\Scripts\python.exe -m pytest -q
# Docker가 준비된 개발 환경에서 실제 PostgreSQL 17 migration·동시성 검증
.venv\Scripts\python.exe scripts\verify_postgresql_17.py
```

## 로컬 Admin Web

Admin Web은 `4180`에서 정적 UI와 같은-origin BFF를 제공하고 Backend는 loopback `8000`에
둡니다. 브라우저 세션은 30분 Strict cookie, 사용자는 Argon2 hash JSON 파일, Backend 호출은
별도 서비스 토큰과 요청 HMAC을 사용합니다.

```powershell
cd module
.venv\Scripts\python.exe -m admin_web.passwords
$env:ADMIN_WEB_USERS_FILE = 'C:\secure\admin-users.json'
$env:ADMIN_WEB_SESSION_SECRET = '<32-byte-or-longer-secret>'
$env:ADMIN_WEB_BACKEND_SERVICE_TOKEN = '<backend-service-token>'
$env:ADMIN_WEB_BACKEND_SIGNING_SECRET = '<different-32-byte-signing-secret>'
$env:ADMIN_WEB_SECURE_COOKIES = 'false' # 로컬 HTTP 개발에서만
.venv\Scripts\python.exe -m admin_web
```

사용자 파일은 `username`, 생성된 `password_hash`, `viewer|operator|admin` 역할과 `active`
boolean을 가진 JSON 배열입니다. 운영 예제는 `ops/ubuntu*/vss-admin-web.service.example`을
따르며 HTTPS 환경에서 Secure cookie를 유지합니다.

필수 계약과 다음 페이즈는 `AGENTS.md` 및 `docs/agent/` 문서를 따릅니다.
Ubuntu 24.04 통과 기록은 `docs/agent/10_UBUNTU_24_04_VALIDATION.md`, 실제 AWS Ubuntu
22.04.5와 Python 3.10.12 호환 기준은 `docs/agent/14_UBUNTU_22_04_AWS_COMPATIBILITY.md`를
따릅니다. systemd는 module `.venv/bin/python`을 사용하고 preflight도 같은 interpreter를
검증합니다.
VSS 측 검증자는 `docs/agent/11_VSS_VALIDATOR_HANDOFF.md`를 단일 실행 진입점으로 사용합니다.
실제 PostgreSQL 로컬 검증의 범위와 운영 미검증 경계는
`docs/agent/12_POSTGRESQL_RUNTIME_VALIDATION.md`를 따릅니다.
운영 환경을 건드리지 않는 반복 검증은 `scripts/verify_module_sandbox.sh`와
`docs/agent/18_MODULE_SANDBOX_VALIDATION.md`를 사용합니다.

## 동일 AWS 인스턴스 주소 경계

일반 Linux service로 함께 실행하는 Snapshot Backend, VSS와 PostgreSQL은 각각
`127.0.0.1:8000`, `127.0.0.1:8200`, `127.0.0.1:5432`를 사용합니다. Backend는 외부
인터페이스에 직접 bind하지 않습니다. 독립 Admin service의 확정 포트는 `4180`이며,
Nginx를 필수 구성으로 두지 않습니다. Browser가 외부에서 접근할 때는 승인된 AWS 주소의
`:4180`과 보안 그룹/VPN을 사용하고, 공개 HTTPS가 필요하면 ALB 같은 별도 TLS 경계를
사용합니다. 외부 클라이언트의 `127.0.0.1`은 해당 클라이언트 자신이므로 AWS 서버 주소로
사용하지 않습니다.
