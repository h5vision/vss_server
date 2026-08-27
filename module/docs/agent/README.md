# Snapshot Backend Agent 문서

`vss_server`의 최상위 `module/` 경로에서 관리되는 Snapshot Backend가
`vss_server/main`의 exact SHA로 배포된 VSS HTTP API를 사용하는 기준 문서입니다.
상위 지침은 `module/AGENTS.md`이며 main의 루트 문서와 파일은 수정하지 않습니다.

| 문서 | 목적 |
|---|---|
| `01_REFERENCE_REPOSITORIES.md` | Frontend, VSS와 구현 저장소의 기준 SHA·권위 파일 |
| `02_EXTERNAL_CONTRACTS.md` | Frontend HTTP, Backend 내부, VSS HTTP, Admin API 계약 |
| `03_TARGET_STRUCTURE.md` | materialization과 VSS HTTP client 중심 목표 구조 |
| `04_REQUIRED_FEATURES.md` | 상태, 멱등성, revision, 오류·운영 요구사항 |
| `05_IMPLEMENTATION_PLAN.md` | 재기준화된 Phase 0R~6 구현 순서와 완료 조건 |
| `06_READINESS_AND_VERIFICATION.md` | 필수 입력값, 차단 조건, 검증 증거 |
| `07_ADMIN_WEB_HANDOFF.md` | 독립 Admin Web의 VSS 기준 API/UI 인계 계약 |
| `08_CODE_REVIEW_AND_CONFORMANCE.md` | 코드 리뷰 및 명세 정합성 검토 보고서 |
| `09_CURRENT_AND_NEXT_BRIEFING.md` | 현재 구현 결과와 다음 Phase 작업 브리핑 |

`vision/model`과 `/index/update/files`를 전제로 한 이전 문구·schema·fixture는 폐기됐습니다.

## 현재 단계

- 완료: Phase 0R, 1, 2H
- 로컬 완료: Phase 3A-1 ORM·Alembic·Repository/Binding 저장소
- 로컬 완료: Phase 3B-1 app lifecycle/readiness, exact binding과 Frontend 조회 proxy
- 로컬 완료: Phase 4 핵심 Git materialization, Snapshot/attempt 영속화와 VSS 제출 route
- 외부 결정 대기: Phase 3A-2 Admin 인증/RBAC/UI, 실제 PostgreSQL migration
- 외부 검증 대기: Phase 3B-2 실제 VSS artifact와 shared path
- 다음 구현: Phase 5 상태 동기화·복구·재시도
- 미구현: 인증된 Snapshot 이력/Admin 화면

구현 순서의 정본은 `05_IMPLEMENTATION_PLAN.md`, 현재 코드와의 대조 정본은
`08_CODE_REVIEW_AND_CONFORMANCE.md`, 현재/다음 단계 요약은
`09_CURRENT_AND_NEXT_BRIEFING.md`입니다. 요구사항 문서에 적힌 기능 목록을 현재 구현 완료
목록으로 해석하지 않습니다.
