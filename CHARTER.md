---
문서: CHARTER
상태: 초안 — md 승인 대기
버전: v0.1
작성: 2026-08-26
역할: 이 레포의 앵커. 목표·범위·불변 조건·역할·마감. 거의 바뀌지 않는다. 바뀌면 DECISIONS 에 이유를 남긴다.
---

# vss_server — 한 장 헌장

## 목표

신입 개발자가 낯선 코드베이스에 질문하면 **출처와 함께** 답하고, 인덱싱이 끝나면 **프로젝트 브리핑**을 자동으로 만들어 주는
VSCode 확장의 서버. 2026-09-09 에 작업을 동결하고 2026-09-16 최종 발표에서 라이브로 보여준다.

## 범위 — 남은 11일에 하는 것 세 가지

1. **RAG 완성도** — 데모 레포 2개(`api_test`, `rag_lab`)에서 같은 질문 suite 로 재는 Hit@3·MRR·no-evidence recall 을 기준선 대비 올린다.
   지렛대는 코퍼스 규칙(tests·백업·번역 제외) → AST 청킹 + 맥락 헤더 → BM25 하이브리드 → 생성 모델 교체 순.
2. **스냅샷** — Extension 이 보낸 레포가 서버에서 인덱싱되고 revision 이 남는다. DB 스냅샷은 P 담당, RAG 와의 경계는
   `(project_id, revision, 서버 로컬 경로)` 세 값이다.
3. **브리핑** — 프로젝트 이름 · 문서 요약 · 진입점 목록 · 진입점별 함수 헤더 · 기능 목록 · (여유 시) Mermaid 구조도를 Markdown 으로.
   추출은 AST 로 결정적으로, 요약만 LLM 이 한다.

**하지 않는 것**: P 의 FastAPI 게이트웨이(삭제), 증분 인덱싱(전체 재인덱싱으로 대체), 중단 재개 인덱싱, 다중 레포 배치,
fine-tuning(9/3 관문에서 폐기 판단), 히스토리 UI, Marketplace 배포.

## 불변 조건 (rag_lab 사고에서 나온 규칙 — 바꾸려면 DECISIONS 에 먼저 적는다)

1. 임베딩은 `bge-m3:latest` 1024차원, 거리는 cosine, **폴백 없음**(실패는 예외로 드러난다).
2. 전체 인덱싱은 **선삭제하지 않는다**. 빌드 → 임베딩 전부 성공 → 승격. 실패한 빌드는 자동으로 지우지 않는다.
3. **인덱스 상태의 정본은 저장소 자신**(Chroma 컬렉션 이름 / PostgreSQL revisions 행)이다. 별도 상태 파일은 이력일 뿐이다.
4. 프롬프트 형식은 `vss/prompt.py` 하나가 정본이다. `[N]` 은 contexts 배열 인덱스+1 과 1:1 이고 배열을 정렬·필터링하지 않는다.
   출처만 추릴 때도 `n` 번호는 재부여하지 않는다.
5. 근거 없음 판정은 **벡터 점수**로만 한다. BM25 는 순서만 바꾼다. `top_score` 는 pool 의 최대 벡터 점수이고 `top_score >= threshold ⟺ has_evidence`.
6. 측정 기록의 조건은 **인덱스가 저장한 fingerprint** 에서 읽는다(현재 환경변수가 아니라). 수치는 문서에 손으로 옮기지 않고
   `data/evaluation/` 의 run·report 가 정본이다.
7. 코드 비유출: 사내 코드·문서는 외부 상용 API 로 나가지 않는다. LLM·임베딩은 EC2 의 Ollama 다.

## 아키텍처

```text
[VSCode Extension]  →  http://<EC2>:8200   vss_server (이 레포)
                              ├─ /v1/chat      검색 → 프롬프트 → Ollama 스트리밍 → 출처 확정  (서버 안에서 한 번에)
                              ├─ /index        스냅샷 디렉터리 → 청킹(AST) → 임베딩 → 저장(빌드→승격) → BM25 → 브리핑
                              ├─ /briefing     Markdown 브리핑 (캐시)
                              └─ 저장소: Chroma (기본) | PostgreSQL+pgvector (VSS_STORE=pgvector)
                     Ollama :11434  bge-m3 (임베딩) · qwen2.5-coder:7b 또는 교체 후보 (생성)
```

## 역할

| 담당 | 범위 |
|---|---|
| md | 서버 전체(검색·프롬프트·LLM 호출·브리핑·인덱싱·평가), EC2 와 DB 운용, 발표 수치 |
| P | 스냅샷(레포 → DB → 서버 로컬 디렉터리 materialize), `snapshot` 스키마 |
| 팀원(gold 담당) | `api_test` 질문 suite 40문항(답 30 + hard negative 10), 데모 시나리오 질문 5개 |
| K·Y | Extension — `/v1/chat` SSE 수신, 출처 표시, `has_evidence=false` 화면, 인덱싱 진행률, 브리핑 표시 |

## 마감과 관문

| 날짜 | 관문 |
|---|---|
| 8/27(목) 점심 | pgvector 전환 go/no-go — 조건 4개(PLAN 참조). go 면 목요일 오후 전환, 금요일 검증 |
| 8/28(금) | 코퍼스 동결(데모 레포 revision + 문서). 이후 측정 시리즈는 이 코퍼스 위에서만 |
| 9/3(목) | fine-tuning 관문 — 세 항목이 데모 가능 상태가 아니면 폐기 확정 |
| 9/4(금) | 데모 리허설 1차 (고정 질문 5개, `has_evidence=false` 화면, 콜드스타트) |
| 9/9(수) | 코드·수치 동결. 이후는 발표 자료만 |
| 9/16(수) | 최종 발표 |

## 정본과 읽는 순서

정본은 **레포(GitHub)** 다 — 코드 · `README.md` · 이 문서 · `docs/API.md` · `SALVAGE.md` · `evaluation/`. 팀 전체가 본다.
팀이 보는 현황은 `README.md` 하나이며(구성 · 코드 구조 · 구현 현황 · 사용법 — 인수인계 수준으로 유지), md 가 갱신한다.
현황 수치는 `STATUS.md`(생성본) 또는 EC2 의 `python -m vss.cli doctor` · `python -m vss.eval runs` 로 본다.

| 문서 | 위치 | 성격 | 누가 고치는가 |
|---|---|---|---|
| `CHARTER.md` | 레포 | 앵커 (목표·범위·불변 조건·관문) | md 만. 컨셉이 바뀔 때 |
| `README.md` | 레포 | 팀용 인수인계 문서: 소개 · 구성 · 코드 구조 · 구현 현황과 다음 작업 · 사용법 | md (자동 구역은 생성, 문장은 세션마다 갱신) |
| `docs/API.md` · `SALVAGE.md` | 레포 | 계약 · 출처 기록 | 코드가 바뀔 때만 |
| `STATUS.md` | 레포(생성, git 제외) | 인덱스·평가 이력 상세 | 스크립트 생성. 손대지 않음 |
