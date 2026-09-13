# 저장된 RAG 평가 결과 차트

측정일: 2026-09-09. 새로 실행한 평가가 아닙니다.

- `01_overview.png`: 두 저장소의 Pipeline Hit@3 및 근거 없음 판정 비교.
- `02_api_test_detail.png`: api-test의 모드별 Hit@1, Hit@3, MRR.
- `03_fastapi_cli_detail.png`: fastapi-cli의 모드별 Hit@1, Hit@3, MRR.

모든 수치는 아래 원본 JSON에서 직접 읽었습니다. 문항별 rows 재집계로 summary와 일치를 확인했습니다.

- [20260909T071832Z-710318](../../runs/20260909T071832Z-710318.json) / suite hash `99bd500d96ffb655`
- [20260909T071934Z-bc6c8e](../../runs/20260909T071934Z-bc6c8e.json) / suite hash `02271791a7358c74`

AST-v3에는 재정렬 효과도 포함됩니다. 생성 답변 품질을 측정한 값이 아닙니다. 저장소별 질문·정답 기준이 다릅니다.
이후 작성된 r1·dev·holdout 평가본의 실행 결과는 포함하지 않습니다. 전체 출처와 설정은 `manifest.json`에 있습니다.
