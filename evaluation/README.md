# evaluation/ — 질문 suite · 실험 matrix

- `suites/*.jsonl` 질문. 한 줄 = 한 문항. 계약은 `schemas/question.schema.json` 과 `tags.json`.
- `matrices/*.json` 어떤 인덱스(project_id)를 어떤 검색 프로필로 어떤 모드로 재는가.
- 결과는 `data/evaluation/runs/<run_id>.json`, 보고서는 `data/evaluation/reports/<run_id>.md` (둘 다 append-only 이력). `data/` 중 이 폴더만 git 에 들어갑니다 — 수치의 정본이라 EC2 에서 run 뒤 바로 커밋합니다:
  `git add data/evaluation && git commit -m "eval: <run_id>" && git push`. 노트북은 `git pull` 로 받아 README·STATUS 를 만듭니다.

```bash
python -m vss.eval validate evaluation/matrices/rag-lab.json     # 파일·심볼·태그 검증 (데이터 변경 없음)
python -m vss.eval run      evaluation/matrices/rag-lab.json --note "baseline"
python -m vss.eval runs                                            # 지금까지의 run 한 줄씩
```

인덱스 이름 규칙: `<repo>--lines` (줄 윈도우 기준선) · `<repo>--ast` (AST 청킹 + 맥락 헤더). 둘 다 BM25 를 함께 만들어 두고
검색 프로필 `vector` / `hybrid` 로 나눠 잽니다 (같은 벡터에서 BM25 효과만 분리).

```bash
python -m vss.cli index ~/repos/rag_lab --project rag-lab--lines --chunker line-window-v1 --context-header off --bm25 on --no-briefing
python -m vss.cli index ~/repos/rag_lab --project rag-lab--ast   --chunker ast-v1          --context-header on  --bm25 on
```

코퍼스 제외 규칙 (2026-08-27 확정 — 문항 작성 전에 볼 것):

- 모든 레포 공통: `data/`·`.snapshot-admin-backup/` 등은 항상 인덱스에서 빠집니다 (`vss/config.py` SKIP_DIRS).
- `api_test` 추가 제외: `tests,admin/**,.snapshot-admin-backup/**`. **`tests/` 와 `admin/` 파일을 정답(gold)으로 하는 answerable:true 문항을 만들지 마세요** — 검색될 수 없습니다.
  admin 기능을 묻는 문항은 `answerable:false` (hard negative) 로는 환영합니다 — "근거 없음" 판정 검증에 쓰입니다.

비교 규칙: repository commit · suite hash · 인덱스 fingerprint 가 같을 때만 셀끼리 비교합니다. 답 있는 문항 n 과 1/n 노이즈선을 항상 같이 적습니다.
