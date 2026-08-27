# Snapshot Backend Agent 문서

`vss_server`의 최상위 `module/` 경로에서 관리되는 Snapshot Backend가
`vss_server/main`의 exact SHA VSS Python package를 사용하는 기준 문서입니다.
상위 지침은 `module/AGENTS.md`이며 main의 루트 문서와 파일은 수정하지 않습니다.

| 문서 | 목적 |
|---|---|
| `01_REFERENCE_REPOSITORIES.md` | Frontend, VSS와 구현 저장소의 기준 SHA·권위 파일 |
| `02_EXTERNAL_CONTRACTS.md` | Frontend HTTP, Backend 내부, VSS 모듈, Admin API 계약 |
| `03_TARGET_STRUCTURE.md` | materialization과 VSS adapter 중심 목표 구조 |
| `04_REQUIRED_FEATURES.md` | 상태, 멱등성, revision, 오류·운영 요구사항 |
| `05_IMPLEMENTATION_PLAN.md` | 재기준화된 Phase 0R~6 구현 순서와 완료 조건 |
| `06_READINESS_AND_VERIFICATION.md` | 필수 입력값, 차단 조건, 검증 증거 |
| `07_ADMIN_WEB_HANDOFF.md` | 독립 Admin Web의 VSS 기준 API/UI 인계 계약 |

`vision/model`과 `/index/update/files`를 전제로 한 이전 문구·schema·fixture는 폐기됐습니다.
