# RAG 정확도 — 지금 아는 것과 아직 모르는 것 (2026-09-04)

이 문서는 두 가지를 한자리에 모읍니다.

1. **지금까지 측정된 값** — `data/evaluation/` 의 run 이 정본입니다.
2. **그 값에 섞인 측정 결함과, 아직 재지 않은 것** — 숫자가 없는 자리를 숨기지 않고 적습니다.

> ⚠ 이 문서의 표는 저장된 run 을 **다시 센 값**입니다. 정본이 아닙니다.
> 정본은 `data/evaluation/runs/*.json` 과 `reports/*.md` 이고, `python scripts/make_status.py` 가 만드는 `STATUS.md` 입니다.
> 아래 「7. 다시 세는 법」에 계산 방법을 적어 두었으니 숫자가 의심스러우면 그 절차로 직접 확인하십시오.

---

## 1. 무엇을 어떤 조건에서 쟀나

| | api-test | fastapi-cli |
|---|---|---|
| run_id | `20260904T005826Z-2f0879` | `20260904T005910Z-5e6307` |
| 인덱스 | `api-test--ast-v2` 2,078청크 | `fastapi-cli--ast-v2` 315청크 |
| 코퍼스 커밋 | `2dea3d71` | `10d7e65a` |
| gold suite | `api-test-v1.jsonl` 40문항 (답 30 + hard negative 10) | `fastapi-cli-full.jsonl` 61문항 (답 46 + hard negative 15) |
| 저장소 | pgvector | pgvector |

공통 조건: 임베딩 `bge-m3:latest` 1024차원 cosine, 청커 `ast-v2` + 맥락 헤더, BM25 하이브리드(RRF), fusion pool 20, threshold 0.54, matrix `top_k` 4.

**지표가 뜻하는 것**

- `Hit@k` — gold 청크가 검색 결과 상위 k 안에 있는가. `top_k` 와 무관하게 pool 20 안의 순위로 잽니다.
- `pool_recall` — gold 가 pool 20 **안에는** 있는가. 재정렬로 고칠 수 있는 한계선입니다.
- `no-evidence recall` — 답이 코퍼스에 없는 질문(hard negative)을 "근거 없음" 으로 막았는가.
- **생성 품질(답이 맞았는가, 환각이 없는가)은 이 표에 없습니다.** `eval run` 은 LLM 을 부르지 않습니다.

---

## 2. 측정된 값 (as-measured)

### api-test — 답 있는 문항 30개, 문항 하나가 3.3%p

| cell | search | Hit@1 | Hit@3 | Hit@5 | MRR | pool_recall | no-evidence |
|---|---|---:|---:|---:|---:|---:|---:|
| baseline(line-window) | hybrid | 40.0% | 66.7% | 73.3% | 0.536 | 93.3% | 70.0% |
| ast+header (ast-v1) | hybrid | 43.3% | 63.3% | 73.3% | 0.575 | 100.0% | 70.0% |
| **ast-v2+header** | **hybrid** | **56.7%** | **80.0%** | **93.3%** | **0.716** | **100.0%** | 60.0% |
| ast-v2+header | vector | 60.0% | 76.7% | 83.3% | 0.714 | 96.7% | 60.0% |

### fastapi-cli — 답 있는 문항 46개, 문항 하나가 2.2%p

| cell | search | Hit@1 | Hit@3 | Hit@5 | MRR | pool_recall | no-evidence |
|---|---|---:|---:|---:|---:|---:|---:|
| baseline(line-window) | hybrid | 28.3% | 52.2% | 67.4% | 0.445 | 89.1% | 93.3% |
| ast+header (ast-v1) | hybrid | 37.0% | 58.7% | 65.2% | 0.508 | 84.8% | 93.3% |
| **ast-v2+header** | **hybrid** | **39.1%** | **60.9%** | 67.4% | 0.526 | 87.0% | 93.3% |
| ast-v2+header | vector | 45.7% | 58.7% | 69.6% | 0.552 | 80.4% | 93.3% |

### 여기서 읽히는 것

- **ast-v2 는 api-test 에서 확실히 이깁니다.** ast-v1 대비 Hit@3 +16.7%p, MRR +0.14 — 노이즈선(3.3%p)의 5배입니다.
- **fastapi-cli 에서는 같은 변경이 +2.2%p(1문항)라 효과가 안 보입니다.** 노이즈선이 2.2%p 라 판정 불가입니다. 코퍼스가 306 → 315청크로 거의 안 바뀐 것이 원인으로 보입니다.
- **api-test 는 pool_recall 이 100%** 입니다. 답 30개 전부가 top-20 안에 있고, 못 찾는 건 순위 문제뿐입니다.
- **fastapi-cli 는 87.0%** 입니다. 6문항은 top-20 밖이라 재정렬로는 못 고칩니다.

---

## 3. 이 값에 섞인 측정 결함 네 가지

아래 넷은 **RAG 성능이 아니라 측정이 만든 실패**입니다. 위 표의 숫자를 그대로 인용하기 전에 읽어야 합니다.

### (a) 두 레포의 채점 자가 다릅니다 — fastapi-cli 만 −10.9%p 손해

`api-test-v1.jsonl` 의 gold 36건은 전부 `{path, symbol}` 이고, `fastapi-cli-full.jsonl` 의 gold 79건은 전부 `{path, line_start, line_end}` 입니다. 뒤쪽은 **같은 파일을 찾아도 줄이 안 겹치면 오답** 처리됩니다. gold span 이 2~8줄인 문항이 16개나 됩니다.

파일 단위로 채점을 맞추면:

| | strict (지금) | path-level |
|---|---:|---:|
| api-test Hit@3 | 80.0% | 80.0% (±0) |
| fastapi-cli Hit@3 | 60.9% | **71.7%** (+10.9%p) |
| fastapi-cli Hit@1 | 39.1% | **52.2%** (+13.1%p) |

api-test 는 `symbol` 검사가 한 번도 걸러내지 않아 원래부터 사실상 파일 단위였습니다. **두 레포의 19.1%p 차이 중 10.9%p 가 자 탓입니다.**

### (b) 측정이 서비스와 다른 `top_k` 로 돕니다 — 두 레포 합쳐 6문항 오계측

`evaluation/matrices/*.json` 은 `top_k: 4` 인데 서버 기본값은 8 입니다(`vss/config.py:145`, 2026-09-01 결정). 그래서 순위 5~8위인 gold 는 **실제 서비스에서는 프롬프트에 들어가는데 eval 은 실패로 셉니다.**

gold 청크가 실제로 프롬프트(근거)에 들어간 비율:

| | matrix 설정 `top_k=4` | 서빙 설정 `top_k=8` |
|---|---:|---:|
| api-test | 86.7% (26/30) | **96.7% (29/30)** |
| fastapi-cli | 63.0% (29/46) | **69.6% (32/46)** |

api-test 는 실제 서비스에서 30문항 중 29개가 근거를 담습니다. 못 담는 건 API-029 하나입니다.

fastapi-cli 는 다릅니다 — **9문항은 `top_k` 를 늘려도 안 들어옵니다.** threshold 0.54 가 먼저 막습니다. 그쪽 병목은 순위가 아니라 임계값입니다.

### (c) 코퍼스에 BOM 결함이 있습니다 — `ast-v2` 인데 ast 가 아닌 청크

`vss/chunker.py:66` 의 인코딩 시도 순서가 `("utf-8", "utf-8-sig", ...)` 라 UTF-8 BOM 이 안 벗겨집니다. `ast.parse` 가 `U+FEFF` 로 실패하고 줄 윈도우로 조용히 폴백합니다.

api_test 의 `.py` 300개 중 **19개가 BOM** 입니다(`backend/contexts/registry.py`, `backend/snapshots/repository.py` 등). 그 파일에서 나온 청크 약 103개에 `symbol`·`kind` 가 제대로 안 붙습니다. `ast-v2` 라 부르는 코퍼스의 일부가 실제로는 line-window 입니다.

지금 점수에 손해가 확인된 문항은 없지만(API-004 는 gold 심볼이 코퍼스에 없는데도 rank 1), **코퍼스 결함 위에서 잰 값**이라는 사실은 남습니다. 고치려면 재인덱싱이 필요합니다.

### (d) gold 라벨이 좁아 정답을 오답으로 세는 문항이 있습니다 — 최소 3건

- **API-014** — 1위로 나온 `README.md:244-246` 이 `GET /v1/IngestResponse` 라고 정답 endpoint 를 정확히 답합니다. gold 는 `backend/api/v1/projects.py` 만 인정합니다.
- **API-028 · API-029** — 1~2위인 `backend/legacy_app.py` 에 gold 와 **바이트 단위로 같은** description 문자열과 실제 SSE 구현이 있습니다. gold(`backend/api/v1/chat.py`)에는 문자열만 있습니다.
- **API-016** — 1위 `README.md:417` 이 "`limit` 은 1~500" 이라고 답하는데, 코드는 `default=5000, ge=1, le=10000` 입니다. **문서가 코드와 어긋난 것**이고 검색은 제 할 일을 했습니다.

이 넷은 "검색이 틀렸다" 가 아니라 "정답을 하나로만 인정했다" 입니다. 라벨을 넓힐지는 결정이 필요합니다.

---

## 4. 보정 후 값

(a)와 (b)만 걷어낸 값입니다. (c)·(d)는 그대로 남아 있습니다.

### 검색 — gold 가 pool 안 몇 위인가

| | Hit@1 | Hit@3 | 95% 신뢰구간 |
|---|---:|---:|---|
| api-test (n=30) | 56.7% | 80.0% | 62.7 – 90.5 |
| fastapi-cli (n=46) | 52.2% | 71.7% | 57.5 – 82.7 |

**두 레포 차이가 19.1%p → 8.3%p 로 줄고 신뢰구간이 겹칩니다.** Fisher 검정 p=0.129 → 0.589. "api_test 는 되는데 fastapi-cli 는 안 된다" 는 통계적으로 성립하지 않습니다.

### 서빙 — gold 가 실제로 근거에 들어갔나

| | top_k=8 (실제 서비스) |
|---|---:|
| api-test | **96.7%** (29/30) |
| fastapi-cli | **69.6%** (32/46) |

---

## 5. 임계값은 레포마다 답이 반대입니다

저장된 run 을 다시 훑은 산수입니다(`python -m vss.eval sweep <run_id>`). 재질의가 필요 없습니다.

**api-test — 0.56 이 손실 없는 개선**

| threshold | 답 있는 문항 통과 | hard negative 차단 | Hit@3 |
|---|---:|---:|---:|
| 0.54 (현재) | 30/30 | 6/10 | 80.0% |
| **0.56** | **30/30** | **9/10** | **80.0%** |
| 0.60 | 28/30 | 10/10 | 73.3% |

잘못 통과한 4건 중 3건의 점수가 0.5453 · 0.5478 · 0.5509 로 임계값 바로 위에 몰려 있습니다.

**fastapi-cli — 0.54 가 이미 최적**

| threshold | 답 있는 문항 통과 | hard negative 차단 | balanced acc |
|---|---:|---:|---:|
| 0.52 | 42/46 | 12/15 | 85.7% |
| **0.54 (현재)** | **41/46** | **14/15** | **91.2%** |
| 0.56 | 39/46 | 14/15 | 89.1% |

올리면 답 있는 문항 2개를 잃고 얻는 게 없습니다. **하나의 전역 threshold 로 두 레포를 다 맞출 수 없습니다.**

---

## 6. 아직 비어 있는 측정

숫자가 없는 자리입니다. 여기에 대한 주장은 전부 추측입니다.

| 무엇 | 왜 비어 있나 | 채우려면 |
|---|---|---|
| **생성 품질** (답 정확도, 환각, 충실도) | `eval run` 이 LLM 을 안 부릅니다. 문항당 0.25초 고정 | 사람 채점, 또는 LLM judge |
| **`use_symbols` 켠 셀** | 코드는 있는데(`vss/symbols.py`) 한 번도 측정된 적이 없습니다. matrix 에 셀이 없습니다 | matrix 에 셀 추가, 재인덱싱 불필요 |
| **hard negative 가 LLM 까지 갔을 때** | 임계값을 통과한 4건이 실제로 답을 지어내는지 확인 안 됨. 모델이 스스로 막을 수도 있습니다 | EC2 에서 그 4문항 `/v1/chat` |
| **`rag_lab`** | EC2 에 코퍼스가 없습니다. 36문항 suite 가 묶여 있습니다 | 코퍼스 업로드 + 인덱싱 |
| **`.py` 외 확장자** | AST 청킹이 `.py` 에만 걸립니다(`chunker.py:663`). `CODE_EXT` 21종 중 20종은 줄 윈도우라 `symbol`·`kind`·`enclosing` 이 전부 `None` 입니다 | 청커 확장 |
| **pool 6~20위** | run 이 `top_paths` 5개만 저장합니다. 재정렬 기법을 오프라인으로 흉내낼 수 없습니다 | `runner.py` 에 pool 로깅 추가 |
| **부모 문맥의 값어치** | 답변 문맥을 바꾸는 것이라 검색 지표로 안 잡힙니다 | 생성 품질 측정이 먼저 |
| **BOM 수정 후 값** | 재인덱싱 필요 | `chunker.py:66` 수정 후 재인덱싱 |

### 표본 크기

답 있는 문항이 30개면 Hit@3 80% 의 95% 신뢰구간이 **±14%p** 입니다. 지금 자로는 10%p 미만의 개선을 판정할 수 없습니다. `api_test` 는 8/27 의 n=6 에서 n=30 으로 올라와 문항 하나의 무게가 16.7%p → 3.3%p 로 떨어졌지만, 여전히 작은 개선은 못 봅니다.

---

## 7. 다시 세는 법

정본 값:

```bash
python scripts/make_status.py            # STATUS.md — 전체 run 이력
python -m vss.eval runs                  # run 목록
python -m vss.eval report <run_id>       # 문항별 실패 목록 포함
python -m vss.eval sweep <run_id>        # 임계값 후보별 confusion matrix (값은 안 바꿈)
```

이 문서 3~4절의 보정값은 저장된 run JSON 을 다시 센 것입니다. 계산 방법:

- **path-level Hit@k** — `cells[].modes.retrieval.rows[].top_paths` 앞 k 개에 gold 의 `path` 가 있으면 정답. `rows[].rank`(strict) 대신 이것을 씁니다.
- **서빙 `top_k=8`** — `modes.pipeline.rows[].passed_paths` 에 gold 가 있으면 이미 들어간 것. 없고 `passed_paths` 길이가 `top_k` 를 꽉 채웠으며 pool 순위가 8 이하면 창을 넓혔을 때 들어옵니다. `passed_paths` 가 `top_k` 를 못 채웠으면 그 아래는 전부 threshold 미달이라 `top_k` 를 늘려도 안 들어옵니다.
- **pool_recall** — `rows[].rank` 가 `null` 이 아닌 비율.

수치를 문서에 손으로 옮기지 않는다는 규칙(CHARTER 불변 조건 6)에 따라, 이 문서의 표는 **판단 근거를 공유하기 위한 사본**이며 코퍼스나 코드가 바뀌면 그 즉시 낡습니다. 인용할 때는 run_id 를 함께 적으십시오.
