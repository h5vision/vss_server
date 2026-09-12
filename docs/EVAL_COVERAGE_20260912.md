# 시험지 수집 점검 — 2026-09-12

평가 수치를 인용하기 전에 붙여야 할 조건을 모았다. 재는 자가 몇 눈금짜리인지, 그리고 정답 자리가 애초에
코퍼스에 들어가기는 했는지 두 가지다. **여기 있는 값은 전부 아래 명령이 그 자리에서 낸 것이다. 손으로 적은 수치는 없다.**

```powershell
.\.venv\Scripts\python.exe adocs\scripts\suite_coverage.py <시험지> <레포 사본 경로> [--detail] [--min-chunk] [--lost]
```

모델도 DB 도 부르지 않는다. 레포 사본과 청커만 쓴다. 값이 의심되면 다시 돌리면 된다.

## 왜 이걸 따로 재는가

`Hit@3` 와 `MRR` 은 **후보 안에서 정답을 위로 올리는 능력**을 재는 자다. 정답 자리가 인덱스에 아예 없으면
그건 순위 실패가 아니라 수집 실패다. 둘을 한 수치에 섞으면 점수가 떨어졌을 때 순위가 나빠진 건지 내용을
잃은 건지 가를 수 없다. 그래서 순위는 run 이 재고, 수집은 이 문서가 센다.

## 1. 답지가 코퍼스에 들어 있다 — 재기 전에 치워야 한다

조건 중에 제일 무겁다. `RAG_TEST.md` 와 `RAG_TEST.json` 이 여섯 레포에 git 으로 들어 있다.
한 레포당 61문항이고 질문과 정답 파일 경로가 그대로 적혀 있다. 검색기가 그 파일을 읽으면
정답 근처가 아니라 답지를 집는다.

그중 셋은 **EC2 인덱스가 답지를 넣은 바로 그 커밋으로 만들어져 있다.** 레포의 마지막 커밋이 답지 커밋이다.

| 레포 | 인덱스 commit | 그 커밋의 내용 | 지금 재면 |
|---|---|---|---|
| fastapi-new | `86c34c2` | "RAG_TEST 생성" | 답지 들어감 |
| flask-realworld-example-app | `411a17f` | "Rag Test 생성" | 답지 들어감 |
| sqlalchemy | `cbef63a9` | "프로젝트 브리핑과 RAG TEST 제작" | 답지 들어감 |
| asyncer | `263e33f` | "RAG_TEST 생성" | 인덱스 없음 |
| flask-restplus-server-example | `73ba0b6` | "Rag test 생성" | 인덱스 없음 |
| mockserver-monorepo | `e092b92` | "RAG_TEST 제작" | 인덱스 없음 |

`fastapi-cli`(`10d7e65`)만 답지가 없다. **지금 인용할 수 있는 유일한 깨끗한 레포다.**

치우는 방법은 레포에서 커밋으로 지우는 것이다. WinSCP 로 지우면 인덱스 meta 에 `dirty: true` 가
박혀 커밋으로 코퍼스를 되짚을 수 없고, `--exclude` 는 CLI 로 만든 인덱스만 깨끗하게 해서
프론트가 만드는 인덱스에는 답지가 계속 들어간다(`POST /index` 는 `profile.exclude_globs` 를 안 보낸다).

## 2. 문항 수가 곧 판정 수가 아니다

같은 청크를 가리키는 문항 둘은 같은 청크가 잡히는지를 두 번 묻는 것이다. 그 청크가 잡히면 둘 다 맞고
안 잡히면 둘 다 틀린다. 그래서 **노이즈선은 `1/문항수` 가 아니라 `1/독립청크` 다.**

ast-v3 기준이다.

| 시험지 | 답 문항 | 독립 청크 | 노이즈선 |
|---|---:|---:|---:|
| flask-realworld-example-app.md (`adocs/test/`) | 88 | 74 | 1.35%p |
| fastapi-cli-r1 | 42 | 36 | 2.78%p |
| sqlalchemy.dev | 20 | 19 | 5.26%p |
| flask-realworld-example-app.dev | 20 | 16 | 6.25%p |
| fastapi-new.dev | 20 | 12 | 8.33%p |
| sqlalchemy.holdout | 10 | 10 | 10.00%p |
| flask-realworld-example-app.holdout | 10 | 10 | 10.00%p |
| fastapi-new.holdout | 10 | 9 | 11.11%p |
| **api-test-v1** (9/9 run 이 쓴 것) | 30 | 13 | 7.69%p |
| **fastapi-cli-full** (9/9 run 이 쓴 것) | 46 | 7 | 14.29%p |

`conduit/app.py` 처럼 파일 전체가 청크 하나가 되는 경우가 있어 문항 일곱이 같은 청크를 본다.
`--detail` 을 붙이면 어느 묶음인지 나온다. 청커를 바꾸면 이 숫자도 바뀐다 — `fastapi-cli-r1` 은
line-window 에서 독립 청크가 18개로 줄고 1문항을 못 찾는다.

**코덱스 개정본 여섯 장(`*.dev`, `*.holdout`)에는 한계 둘이 더 붙는다.**

- 표본이 작다. dev 20문항은 한 문항이 5%p, holdout 10문항은 10%p 다. 독립 청크로 보면 위 표대로
  더 거칠다. n≥30 기준에 못 미친다
- **holdout 이 독립적인 블라인드가 아니다.** dev 와 같은 작성자(코덱스)가 만들었다. 코덱스가
  자기 README(`adocs/test/revised-20260910/README.md`)에 적어 뒀다. "holdout 을 보고 검색 설정을
  조정하지 않는다" 는 규칙으로만 지킨다
- `fastapi-new` 는 레포에 함수·클래스가 22개뿐이라 dev 20문항이 독립 청크 12개를 본다. 무엇을 해도
  이 레포 단독으로는 판정이 안 된다

## 3. 지금 있는 기준선 수치에 붙일 조건

`data/evaluation/runs/20260909T071934Z-bc6c8e`(fastapi-cli)와 `20260909T071832Z-710318`(api-test)이
지금 인용할 수 있는 유일한 측정이다. 그 둘의 자는 위 표 마지막 두 줄이다.

- **fastapi-cli-full 은 답 46문항이지만 독립 청크가 7개다.** 청크 하나가 14.29%p 를 움직인다.
  청커를 바꿔도 7개 그대로다. 셀 사이의 차이가 14%p 미만이면 그 시험지로는 우열을 가릴 수 없다.
- **api-test-v1 은 30문항에 독립 청크 13개다.** 7.69%p 다.
- fastapi-cli-full 은 gold 하나가 어느 청크에도 없다(`fastcli-q016`, `src/fastapi_cli/__main__.py`).
  api-test-v1 은 못 찾는 문항이 없다.

이 조건 없이 그 수치를 단독으로 쓰면 안 된다.

## 4. ast 청커가 짧은 메서드를 코퍼스에서 떨어뜨린다

`VSS_MIN_CHUNK`(기본 80자)는 짧은 조각을 코퍼스에서 빼는 필터다. 그런데 ast 청커는 심볼마다 청크를
따로 내므로, 두세 줄짜리 메서드는 자기 청크가 유일한 사본인데도 이 필터에 걸려 통째로 사라진다.
상위 클래스 청크에 남지도 않는다 — `conduit/profile/models.py` 의 클래스 청크는 `class UserProfile(...)`
한 줄뿐이다.

flask-realworld 에서 이렇게 사라진 자리 여덟이다.

| 파일 | 자리 |
|---|---|
| `conduit/articles/models.py` | `__repr__` |
| `conduit/exceptions.py` | `to_json`, `user_not_found`, `unknown_error` |
| `conduit/profile/models.py` | `username`, `bio`, `image`, `email` |

line-window 청커에서는 여덟 다 남는다. 줄 단위로 자르니 버려지는 줄이 없다.

**그래서 이 여덟을 시험지에서 뺐다.** 남겨 두면 ast 계열만 천장이 8.3%p 낮아지는데, 그 차이는 순위
품질과 무관하다. 대신 수집 결함으로 여기 적는다. 뺀 자리는 `adocs/test/` 시험지 머리말에도 적혀 있다.

### 레포 전체로는 얼마나 사라지는가

시험지에 걸린 여덟만이 아니라 레포 전체에서 센 값이다(`--lost`). 테스트와 도구 경로는 뺐다.

| 레포 | 함수·클래스 | 사라진 것 | 비율 | dunder / `_`내부용 / 공개 |
|---|---:|---:|---:|---|
| fastapi-cli | 42 | 0 | 0% | 0 / 0 / 0 |
| sqlalchemy | 8,765 | 295 | 3.4% | 106 / 34 / 155 |
| flask-realworld-example-app | 101 | 8 | 7.9% | 1 / 0 / 7 |

**세는 방법 주의.** 청크 집합을 비교하면 안 된다. 한 파일의 심볼 청크가 전부 버려지면 그 파일은
줄 단위 청킹으로 되살아나므로(`vss/chunker.py` 마지막 fallback), 내용이 살아 있는데도 "빠졌다" 로
세어진다. 이름이 코퍼스 본문에 남는지로 판정해야 한다. `conduit/utils.py` 가 그 경우다 —
함수 둘 다 80자 미만이라 심볼 청크가 없지만 파일이 통째로 한 청크가 되어 내용은 남는다.

**그래서 규칙이 뒤집힌다. 심볼이 전부 짧은 파일은 안전하고, 일부만 짧은 파일이 위험하다.**
fallback 은 청크가 하나도 안 남았을 때만 돈다. 긴 함수와 짧은 함수가 섞인 파일은 긴 것만 청크로
남고 짧은 것은 아무 데도 안 남는다. `conduit/exceptions.py` 가 그 모양이다.

**비율이 레포마다 갈리는 이유도 여기서 나온다. 한 줄짜리 메서드가 많은 코드베이스일수록 많이 잃는다.**

- `fastapi-cli` 0% — CLI 도구라 함수가 다 일정 길이 이상이다. 버려질 것이 없다
- `flask-realworld-example-app` 7.9% — 작은 CRUD 앱이라 `return self.user.bio` 꼴 접근자와
  `return cls(**USER_NOT_FOUND)` 꼴 오류 팩토리가 많다
- `sqlalchemy` 3.4% — 절대 수는 295개로 크지만 전체가 8,765개다. 사라진 것 대부분이 dialect 의
  `visit_HSTORE` 류 한 줄 오버라이드다

프레임워크나 드라이버처럼 작은 오버라이드가 많은 코드일수록 손해가 크다. 우리 데모 레포 중에서는
`flask-realworld-example-app` 이 제일 불리하다.

**사라진 것의 성격이 갈린다.** 절반은 정말 사소하다. flask-realworld 의 `bio`, `image`, `email`,
`username` 은 `return self.user.X` 한 줄짜리 접근자고 `__repr__` 은 dunder 다. 검색할 일이 없다.
sqlalchemy 에서 사라진 295개 중 106개도 dunder 다.

나머지 절반은 사소하지 않다.

- `conduit/exceptions.py` 의 `to_json`, `user_not_found`, `unknown_error` — 이 서버의 오류 계약이다.
  "이 API 는 어떤 오류를 내나요" 는 신입이 실제로 묻는 질문이고 이 제품이 답해야 하는 종류다
- sqlalchemy 는 공개 이름 155개를 잃는다. `engine/interfaces.py` 의 `fetchone`, `fetchmany`,
  `nextset`, `dialects/postgresql/base.py` 의 `visit_HSTORE`·`visit_DATERANGE` 처럼 찾아볼 만한 것들이다

**판단.** 0~8% 로 작고 레포마다 들쭉날쭉해서 점수 차이를 설명하는 요인은 아니다. 발표에서 앞세울
내용이 아니라 "남은 한계" 에 한 줄로 들어갈 것이다. 다만 **조용히 사라진다는 점**과 **청커마다
다르다는 점**(line-window 는 0) 때문에 수치로 남긴다. 시험지에서 여덟 문항을 뺀 근거가 그 비대칭이다.

### min_chunk 을 없애면

없애는 쪽(1로 내리는 쪽)의 비용이다.

| 레포 | 청크 (80자) | 청크 (1자) | 증가 |
|---|---:|---:|---:|
| flask-realworld-example-app | 233 | 412 | +77% |
| fastapi-cli | 315 | 457 | +45% |
| sqlalchemy | 49,651 | 60,037 | +21% (+10,386) |

늘어난 것의 정체가 결정적이다. sqlalchemy 기준으로 클래스 안 대입문 6,614, 모듈 대입문 1,708,
메서드 1,406, docstring 459 순이다. 본문 길이 중앙값이 28~39자라 벡터가 토큰 두세 개에 좌우되고,
`top_k=4` `per_file_cap=2` 인 지금 설정에서 진짜 답을 밀어낼 수 있다.

**비용 대비 회수가 맞지 않는다.** 세 레포에서 실제로 되찾는 것은 위 절의 303개(0 + 295 + 8)뿐인데,
필터를 없애면 청크가 10,787개 늘어난다. 되찾는 303개의 절반 가까이는 dunder 와 한 줄 접근자다.

그리고 `min_chunk_chars` 는 fingerprint 에 들어간다(`vss/config.py`). 값을 바꾸면 EC2 의 인덱스가
전부 다른 조건이 되고 지금까지 잰 수치를 인용할 수 없게 된다(CHARTER 불변 조건 6).

좁은 선택지는 있다. `vss/chunker.py` 에 이미 "함수 안에 든 정의는 자기 청크가 유일한 사본이라 짧아도
버리지 않는다" 는 예외가 있다(`force` 플래그). 그 예외를 클래스 안 메서드까지 넓히면 대입문 잡음
없이 사라진 것만 되찾는다. 늘어나는 청크 수는 재보지 않았다. 다만 fingerprint 가 바뀌는 것은 같아서
재인덱싱은 똑같이 따라오므로 9/16 발표까지의 일정에서 할 일은 아니다.

## 5. flask-realworld 시험지에서 뺀 자리

`adocs/test/flask-realworld-example-app.md` 는 원본 150문항을 자리마다 한 문항으로 정리해
118문항(답 88 + 답없음 30)이 됐다. 뺀 이유는 둘이다.

- **파일 안에서 이름이 겹쳐 (파일, 심볼)로 자리를 특정할 수 없는 것 5개** — `articles/models.py` 의
  `__init__`(한 파일에 3개), `articles/serializers.py` 의 `dump_article`, `Meta`, `make_comment`,
  `dump_comment`(각 2개). 채점기가 심볼 이름이 청크 본문에 있는지로 맞히므로 그대로 두면 엉뚱한
  청크가 정답 처리된다.
- **코퍼스에 안 남는 것 8개** — 위 4절.

답없음 문항 둘도 갈아 끼웠다. "비밀번호를 해시하는 함수" 는 `conduit/user/models.py` 의 `set_password`
가 실제로 하는 일이라 답이 있었고, "스키마를 자동으로 마이그레이션하는 곳" 은 `register_extensions` 의
`migrate.init_app` 이 걸리는데 그 청크는 다른 문항의 정답이다.

## 6. 청커별 상세 (flask-realworld, 답 88문항)

| 청커 | 못 찾는 문항 | 독립 청크 | 노이즈선 |
|---|---:|---:|---:|
| ast-v3 | 0 | 74 | 1.35%p |
| ast-v2 | 0 | 74 | 1.35%p |
| ast-v1 | 5 | 65 | 1.54%p |
| line-window-v1 | 0 | 30 | 3.33%p |

ast-v1 이 5문항을 더 잃는다. 이건 ast-v2 가 v1 대비 나아진 실제 차이라 시험지에 남겼다.
line-window 는 다 찾지만 독립 청크가 30개뿐이라 같은 문항 수로도 훨씬 거친 자가 된다 —
줄 단위 창이 여러 심볼을 한 청크에 담기 때문이다.
