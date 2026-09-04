# Phase 7A-3 TDD 검증 증거

**검증일**: 2026-09-03 KST
**대상**: GitHub PR/GitLab MR provider adapter, provider-owned ref 검증, Repository Tag 이력

## 사용자 여정

- Repository 운영자는 opt-in sync로 GitHub PR과 GitLab MR의 base/head/merge SHA를
  read-only API에서 수집하고 Git object와 대조할 수 있어야 합니다.
- VSS는 provider metadata가 아니라 검증된 PR/MR revision과 Tag commit만 context 후보로
  사용할 수 있어야 합니다.
- 운영자는 lightweight/annotated Tag의 commit 이동·삭제·재생성을 append-only로 추적할 수
  있어야 합니다.
- token, provider 응답 본문, Git stderr와 내부 경로는 오류나 로그에 노출되지 않아야 합니다.

## RED 증거

| 보장 | 실행 명령 | RED 결과 |
|---|---|---|
| GitHub/GitLab client 필요 | `pytest -q tests/unit/change_requests/test_provider_clients.py` | provider package 부재로 collection error |
| provider 설정 필요 | `pytest -q tests/unit/core/test_config.py -k change_request_provider` | Settings 필드 부재로 2 failed |
| provider-owned ref fetch 필요 | `pytest -q tests/unit/repository_collection/test_git_client.py -k change_request_fetch` | method 부재로 2 failed |
| Tag current/history 필요 | `pytest -q tests/unit/repository_tags/test_repository_tag_service.py` | repository_tags package 부재로 collection error |

프로젝트 규칙상 commit/push는 사용자 명시 요청에서만 수행하므로 RED checkpoint commit은
만들지 않았습니다. 위 명령과 실제 실패가 RED 증거입니다.

## GREEN 증거

| 보장 | 테스트 | 결과 |
|---|---|---|
| GitHub mapping·Link pagination·synthetic merge SHA 배제 | `test_provider_clients.py` | 3 passed |
| GitLab detail fallback·diff_refs mapping | `test_provider_clients.py` | PASS |
| token과 provider response body 비노출 | `test_provider_clients.py` | PASS |
| 같은 PR/MR revision Git fetch·DB history 멱등성 | `test_change_request_collection_service.py` | 1 passed |
| GitHub/GitLab provider-owned ref와 SHA 검증 | `test_git_client.py -k change_request_fetch` | 2 passed |
| 실제 GitHub REST→PR ref→Snapshot/VSS→commit catalog | `test_change_request_provider_flow.py` | 1 passed |
| lightweight/annotated Tag commit 정규화 | `test_git_client.py -k tags_resolve` | 1 passed |
| Tag created/moved/deleted append-only 이력 | `test_repository_tag_service.py` | 1 passed |
| opt-in app lifespan wiring | `test_app_startup.py` | PASS |
| 장시간 provider/Tag 수집 중 Repository sync lease 갱신 | service unit·collection integration | PASS |
| 전체 회귀 | `pytest -q` | 192 passed, 1 POSIX-only skipped |

추가 검증:

```text
ruff check backend admin_web tests alembic scripts                         PASS
compileall -q backend alembic tests scripts                               PASS
alembic heads                                                              0008_repository_tags
alembic upgrade head --sql                                                 PASS
alembic downgrade 0008_repository_tags:base --sql                          PASS
```

## 공식 외부 계약

- GitHub REST Pull Requests: `https://docs.github.com/en/rest/pulls/pulls`
- GitLab Merge Requests API: `https://docs.gitlab.com/api/merge_requests/`

GitHub는 read-only Pull requests 권한, `state=all`, page/per_page와 API version header를
사용합니다. GitLab은 project merge request 목록과 detail `diff_refs`를 사용하고
`X-Next-Page`를 따릅니다.

## 보안 검토

- provider token은 `SecretStr` 환경변수이며 DB·응답·로그에 저장하지 않습니다.
- HTTP client는 `trust_env=False`, 고정 base URL과 percent-encoded Repository path를
  사용합니다.
- GitHub는 정확한 `owner/repo`, GitLab은 안전한 group/project path만 허용합니다.
- pagination은 최대 page, Tag는 최대 개수로 제한합니다.
- provider 목록과 각 Git ref/Tag 검증 사이에 Repository sync lease를 갱신합니다.
- PR/MR 제목은 제어문자와 개행을 제거하고 512자로 제한합니다.
- open GitHub PR의 synthetic `merge_commit_sha`는 merge 결과로 저장하지 않습니다.
- fork head는 임의 URL 대신 target remote의 provider-owned ref만 fetch합니다.

## Coverage와 외부 대기

현재 dev dependency에 `pytest-cov`가 없어 coverage 백분율은 측정하지 못했습니다. 새 기능은
unit, integration과 실제 local Git E2E로 검증했으며 전체 회귀를 통과했습니다.

다음은 외부 검증 대기입니다.

- 실제 GitHub/GitLab credential과 private/fork Repository
- 실제 PostgreSQL `0008` migration·제약·동시성
- AWS provider rate limit과 timeout
- 실제 VSS가 PR/MR/Tag context를 pull하는 Phase 7C E2E
