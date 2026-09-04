# Gemini 3.8 Module Startup Instructions

이 파일은 `vss_server/module` 프로젝트에 Gemini 3.8이 들어올 때 가장 먼저 읽어야 하는
진입 문서입니다. 코드나 명령을 실행하기 전에 이 파일과 연결된 정본을 반드시 읽고, 현재
구현 상태와 외부 검증 상태를 구분해야 합니다.

## Mandatory Reading Order

1. `AGENTS.md`
2. `docs/agent/20_GEMINI_3_8_STARTUP_GUIDE.md`
3. `docs/agent/09_CURRENT_AND_NEXT_BRIEFING.md`
4. `docs/agent/05_IMPLEMENTATION_PLAN.md`
5. 작업에 직접 관련된 `docs/agent/01~19_*.md`

읽은 문서와 실제 코드가 다르면 문서의 완료 문구를 사실로 가정하지 말고 코드·테스트·배포
증거를 다시 확인합니다.

## Non-negotiable Rules

- **진행 사항 문서화 최우선 원칙**: 모든 작업(리팩터링, 신규 구현, 버그 수정 등) 착수 전 및 매 스텝/PR 완료 시 반드시 `docs/agent/09_CURRENT_AND_NEXT_BRIEFING.md` 및 관련 주제 문서(`.md`)에 진행 상황, 코드 변경점, 검증 결과를 기록·동기화해야 합니다. 문서가 뒤처진 상태에서 임의로 다음 단계로 진행하는 것을 엄격히 금지합니다.
- **스텝별 명시적 브리핑 및 대기 원칙**: 하나의 스텝 또는 PR이 완료될 때마다 작업을 멈추고 코드 레벨 상세 리뷰와 검증 결과를 사용자에게 브리핑한 뒤, 사용자의 명시적인 승인(허가)이 있을 때만 다음 스텝으로 진행합니다.
- 변경 범위는 `module/` 안으로 제한합니다. `vss/`, main 소유 파일과 Frontend 참조 저장소는 수정하지 않습니다. `git add module/`만 허용됩니다.
- Snapshot은 exact Git commit/tree를 재현하는 계층입니다. 임의 SHA, diff-only directory, 유사한 VSS project ID를 사용하지 않습니다.
- VSS는 `/v1/chat`, 검색, 청킹, 임베딩과 답변을 소유합니다. module은 Git reference, Snapshot, VSS 증거를 제공하고 Chat을 proxy하지 않습니다.
- VSS는 module의 인증된 localhost 내부 API를 pull합니다. module DB를 VSS에 직접 공개하거나 VSS Store를 import하지 않습니다.
- sLLM/Ollama 모델 성능, prefill, top-k와 tok/s는 module 작업·검증 범위가 아닙니다.
- token, DSN, credential, 파일 본문, Git stderr와 server-local 경로를 출력·commit·문서화하지 않습니다. token 설정 위치 안내가 필요한 경우에도 값은 절대 노출하지 않습니다.
- 사용자의 기존 dirty 변경은 보존합니다. `git reset --hard`, `git checkout --`와 광범위한 삭제를 사용하지 않습니다.
- commit/push는 사용자가 명시적으로 요청한 경우에만 수행합니다.

## Verification Gate

코드 변경 전 관련 테스트를 먼저 작성하고 RED를 확인합니다. 변경 후 다음 순서로 검증합니다.

```bash
python -m ruff check backend admin_web tests alembic scripts
python -m compileall -q backend alembic tests scripts
python -m pytest -q
bash scripts/verify_module_sandbox.sh
```

실제 AWS를 대상으로 할 때는 `--migrate`, `--restart`, `--run-sync`를 묵시적으로 사용하지
않습니다. 각각 DB 변경, service 재시작, 실제 Repository/VSS 작업을 뜻하므로 명시된 실행
명령으로만 사용합니다.

## Current Phase

- **정본 아키텍처**: `docs/architecture/ARCHITECTURE.md`
- **리팩터링 진행 현황**: `docs/agent/17_ARCHITECTURE_REFACTORING.md`
- 현재 구현 상태는 **Phase 7B-3 On-demand Snapshot 승격 완료 (`661520c`)** 후, 시스템 아키텍처를 점진적으로 정렬하는 **Strangler 리팩터링 (PR 1 완료, PR 2 예정)** 진행 중입니다.

상세 규칙과 다음 작업은 `docs/agent/20_GEMINI_3_8_STARTUP_GUIDE.md` 및 `docs/agent/09_CURRENT_AND_NEXT_BRIEFING.md`를 정본으로 사용합니다.
