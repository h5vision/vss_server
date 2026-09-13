# Admin Web 상세 기술 레퍼런스

최종 갱신: 2026-09-10 KST
대상: `module/admin_web/` 및 Snapshot Backend의 `/v1/admin/*` 관리 경계
현재 UI 세대: **Apple Liquid Glass UX v4**

## 1. 문서 목적과 정본 범위

이 문서는 `vss_server/module`에 포함된 독립 Admin Web의 현재 구현을 한곳에서 이해하기 위한 기술 레퍼런스입니다.
기존 `07_ADMIN_WEB_HANDOFF.md`가 단계별 인계 계약과 과거 운영 결정을 보존하는 문서라면, 이 문서는 **현재 코드 기준의 구조, 기술 스택, 화면 기능, UX, 보안, 배포, 검증 방법**을 정리합니다.

권위 우선순위는 다음과 같습니다.

1. 실제 코드와 Backend OpenAPI/route 구현
2. `module/AGENTS.md`와 `module/docs/architecture/ARCHITECTURE.md`
3. 이 문서
4. 과거 단계별 handoff/briefing 문서

이 문서가 VSS의 Chat/RAG/index 의미론을 재정의하지 않습니다. 특히 VSS가 노출하지 않는 세션, Frontend identity, telemetry를 Admin Web 또는 Module이 추측해서 만들어서는 안 됩니다.

---

## 2. Admin Web의 역할

Admin Web은 VS Code Webview가 아니라 **독립 브라우저 관리 애플리케이션**입니다.
브라우저에서 Repository/Branch/Snapshot/VSS/모델/운영 상태를 보고 명시적인 관리 작업을 수행하는 것이 목적입니다.

핵심 경계는 다음과 같습니다.

```text
Desktop / iPhone / iPad Browser
Chrome / Safari / compatible browser
            │
            │ HTTP(S) :4180
            ▼
┌──────────────────────────────────────┐
│ Independent Admin Web                │
│ module/admin_web                     │
│                                      │
│  HTML + CSS + Vanilla JavaScript     │
│  FastAPI same-origin BFF             │
│  Session / CSRF / RBAC               │
└──────────────────┬───────────────────┘
                   │ signed same-host request
                   │ loopback only
                   ▼
┌──────────────────────────────────────┐
│ Snapshot Backend :8000               │
│ /v1/admin/*                          │
│                                      │
│ Repository / Snapshot / Chat         │
│ Runtime / Audit / VSS integration    │
└───────────┬─────────────┬────────────┘
            │             │
            │             └────────────► Ollama :11434
            │
            ├──────────────────────────► PostgreSQL :5432
            │
            └──────────────────────────► VSS :8200
```

Browser는 다음 대상에 직접 접근하지 않습니다.

- Snapshot Backend의 loopback 포트 `8000`
- VSS HTTP API `8200`
- Ollama `11434`
- PostgreSQL `5432`
- Git credential
- materialized filesystem 내부 경로

따라서 Admin Web은 단순 정적 dashboard가 아니라 **관리자 인증 경계 + same-origin BFF + 운영 UI**의 역할을 동시에 수행합니다.

---

## 3. 기술 스택

### 3.1 Browser UI

| 구분 | 현재 기술 |
|---|---|
| Markup | HTML5, semantic element, native `<dialog>` |
| Styling | CSS3, CSS custom properties, media query |
| Client logic | Vanilla JavaScript, strict mode |
| Navigation animation | View Transition API + CSS fallback |
| Glass rendering | `backdrop-filter` + `-webkit-backdrop-filter` |
| Responsive layout | CSS Grid/Flexbox + breakpoint + `dvh` |
| Mobile safe area | `viewport-fit=cover`, `env(safe-area-inset-*)` |
| Accessibility | ARIA, focus management, keyboard navigation, reduced motion/transparency/contrast |
| External UI framework | 없음 |
| React / Next.js / Vue | 사용하지 않음 |
| npm bundler/build step | 없음 |
| External CDN/font/script | 없음 |

UI는 브라우저 기본 시스템 폰트를 우선 사용합니다.

```css
-apple-system,
BlinkMacSystemFont,
"SF Pro Text",
"Segoe UI",
system-ui,
sans-serif
```

Apple 계열 시스템에서는 가능한 경우 시스템 San Francisco 계열 글꼴이 사용되며, Windows에서는 Segoe UI/system font로 자연스럽게 fallback합니다.

### 3.2 Admin Web BFF

| 구분 | 현재 기술 |
|---|---|
| Application framework | FastAPI `>=0.115,<1` |
| ASGI server | Uvicorn `>=0.34,<1` |
| Backend HTTP client | `httpx2 >=2,<3` AsyncClient |
| Session middleware | Starlette `SessionMiddleware` |
| Request validation | Pydantic |
| Password hashing | `pwdlib[argon2]` / Argon2 |
| Cookie signing dependency | itsdangerous |
| Configuration | Python dataclass + environment validation |

### 3.3 Snapshot Backend / persistence dependencies

Admin Web 자체는 PostgreSQL에 직접 연결하지 않지만 같은 `vss-snapshot-module` 패키지의 Backend는 다음을 사용합니다.

- SQLAlchemy 2.x
- Alembic
- asyncpg
- PostgreSQL
- FastAPI
- Pydantic Settings

Admin Web은 이 저장 계층을 반드시 `/v1/admin/*` Backend API를 통해서만 사용합니다.

### 3.4 개발/검증 도구

- pytest
- Ruff
- Python `compileall`
- Node `--check`를 이용한 JavaScript syntax 검사 가능
- `git diff --check`
- module sandbox verification script

현재 repository에는 Playwright가 설치되어 있지 않습니다. 따라서 v4 시점의 iPhone Safari/WebKit 대응은 코드/정적 계약과 표준 호환 설계를 통해 검증했고, 실제 WebKit Playwright E2E는 별도 후속 quality gate로 남아 있습니다.

---

## 4. 소스 구조

```text
module/
├─ admin_web/
│  ├─ __init__.py
│  ├─ __main__.py
│  ├─ app.py                    # FastAPI app, auth route, BFF, static serving
│  ├─ auth.py                   # user loading, Argon2 authentication, rate limit
│  ├─ config.py                 # env/security configuration
│  ├─ proxy.py                  # strict API allowlist, RBAC, HMAC signing
│  ├─ passwords.py              # admin password/hash helper
│  ├─ index.html                # application shell and semantic UI
│  ├─ app.js                    # state, rendering, API, interactions, UX behavior
│  ├─ styles.css                # responsive Liquid Glass design system
│  └─ assets/
│     └─ apple-glass-wallpaper.svg
├─ backend/
│  └─ ...                       # authoritative Admin API implementation
├─ tests/
│  └─ unit/admin/
│     └─ test_admin_web_ui.py   # Admin Web static/UX contract
└─ docs/agent/
   ├─ 07_ADMIN_WEB_HANDOFF.md
   └─ 24_ADMIN_WEB_TECHNICAL_REFERENCE.md
```

`index.html`, `app.js`, `styles.css`, wallpaper asset은 동일 FastAPI app에서 제공됩니다. Browser와 Backend API가 같은 origin을 사용하므로 별도 frontend CORS data plane을 만들지 않습니다.

---

## 5. HTTP/BFF 요청 흐름

일반적인 관리 요청은 다음 순서로 흐릅니다.

```text
1. Browser
   │
   │ fetch('/v1/admin/...')
   │ same-origin session cookie
   │ mutation이면 X-CSRF-Token
   ▼
2. Admin Web FastAPI :4180
   │
   ├─ session 확인
   ├─ route allowlist 확인
   ├─ viewer/operator/admin RBAC 확인
   ├─ mutation Origin 확인
   ├─ mutation CSRF 확인
   ├─ body size 제한
   └─ Backend 요청 HMAC 서명
   │
   ▼
3. Snapshot Backend :8000 loopback
   │
   ├─ service token / HMAC 검증
   ├─ Admin API use case 실행
   ├─ audit 기록
   └─ 필요 시 VSS/Ollama/PostgreSQL/Git 사용
   │
   ▼
4. Admin Web BFF
   │
   ├─ upstream error 정규화
   ├─ X-Request-ID 유지
   └─ 허용된 response만 Browser로 전달
   │
   ▼
5. Browser UI
```

BFF는 임의 Backend URL proxy가 아닙니다. `proxy.py`의 정규식 allowlist에 존재하는 route/method만 전달합니다.

---

## 6. 인증과 보안 모델

### 6.1 사용자 인증

Admin 사용자 registry는 외부 JSON 파일로 관리하며 최소 다음 의미를 가집니다.

- `username`
- Argon2 `password_hash`
- `viewer | operator | admin`
- `active`

로그인 성공 시 세션에는 다음 값이 저장됩니다.

- username
- role
- CSRF token

세션 cookie 이름은 `snapshot_admin_session`입니다.

### 6.2 세션 정책

- 세션 최대 수명: **1800초 / 30분 고정**
- SameSite: `strict`
- 운영 기본 Secure cookie: true
- local HTTP 개발에서만 `ADMIN_WEB_SECURE_COOKIES=false` 허용

설정 코드 자체가 session max age가 1800초가 아니면 거부하도록 되어 있습니다.

### 6.3 Login rate limiting

기본값:

- 60초 window
- 최대 5회 실패
- 초과 시 `429 LOGIN_RATE_LIMITED`

### 6.4 CSRF + Origin

`POST`, `PATCH`, `PUT`, `DELETE`는 mutation으로 취급합니다.

mutation은 모두 다음을 통과해야 합니다.

1. `Origin`이 `ADMIN_WEB_ALLOWED_ORIGINS`에 정확히 존재
2. Session CSRF token과 `X-CSRF-Token`이 constant-time 비교로 일치
3. route가 BFF allowlist에 존재
4. 사용자 role이 최소 요구 role 이상

### 6.5 BFF → Backend 서명

BFF는 Browser identity를 그대로 신뢰해서 Backend에 넘기지 않습니다. 다음 canonical data를 HMAC-SHA256으로 서명합니다.

```text
HTTP METHOD
raw path + raw query
SHA256(request body)
actor
role
timestamp
request ID
```

전달 header에는 다음 계열이 포함됩니다.

```text
Authorization: Bearer <service token>
X-Admin-Actor
X-Admin-Role
X-Admin-Timestamp
X-Admin-Request-ID
X-Admin-Content-SHA256
X-Admin-Signature
```

encoded path(`%40` 등) 문제를 피하기 위해 서명 canonical target에는 ASGI raw path/query를 사용합니다.

### 6.6 Backend 주소 제한

`ADMIN_WEB_BACKEND_URL`은 HTTP/HTTPS **loopback IP origin만** 허용합니다.

따라서 BFF 설정을 이용해 외부 임의 host로 proxy하는 SSRF 형태의 사용을 허용하지 않습니다.

### 6.7 Security headers

Admin Web 응답은 기본적으로 다음 정책을 적용합니다.

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- CSP
  - `default-src 'self'`
  - `script-src 'self'`
  - `style-src 'self'`
  - `img-src 'self' data:`
  - `connect-src 'self'`
  - `frame-ancestors 'none'`
  - `base-uri 'none'`
  - `form-action 'self'`

관리 API와 auth API는 `Cache-Control: no-store`, 정적 UI는 `no-cache`로 제공합니다.

### 6.8 Request body 제한

기본 Admin BFF request body 상한은 1 MiB입니다.

```text
ADMIN_WEB_MAX_REQUEST_BODY_BYTES=1048576
```

Content-Length와 실제 읽은 body 모두 검사합니다.

---

## 7. 역할과 권한

### viewer

조회 중심 권한입니다.

- Repository 목록/상세
- Branch catalog
- Commit history/detail
- Sync history
- Tracked Branch 조회/head history
- Snapshot 조회
- VSS project catalog 조회
- runtime model 상태 조회

### operator

viewer 권한 + 실행성 작업을 수행합니다.

- Repository Sync
- Commit materialize
- Commit compare
- Tracked Branch Index
- Snapshot Index
- Snapshot Retry
- Ollama model Up / Down / Reload
- Ollama model Auto Up 정책 변경

### admin

operator 권한 + 설정/파괴성/감사/관측 관리 작업을 수행합니다.

- Repository 등록/수정/deactivate/purge
- Repository GitHub discovery
- Tracked Branch 등록/수정/untrack
- Branch Binding 등록/수정/deactivate
- VSS vector/project 삭제
- Module service 상태/재시작
- Audit Log
- VSS request failures
- Chat observability 조회/삭제/retention purge

Browser에서 버튼을 숨기는 것은 편의 UX일 뿐 보안 경계가 아닙니다. 실제 권한은 BFF와 Backend에서 다시 검증합니다.

---

## 8. 주요 관리 화면

### 8.1 Repositories

표시 정보:

- canonical name
- display name
- provider
- default branch ref
- active
- updated time

주요 기능:

- Repository 등록
- GitHub repository metadata discovery
- 수정
- Sync
- deactivate
- purge
- Commits 이동

GitHub 등록 UX는 URL 중심입니다.

```text
Repository URL
        ↓
GitHub repository detected
        ↓
canonical repository metadata 조회
        ↓
canonical name / provider / default branch 자동 채움
```

`main` 또는 `master`를 추측하지 않고 GitHub가 제공한 공식 `default_branch`를 사용합니다.
GitHub가 아닌 provider URL은 기존 수동 입력 경로를 유지합니다.

### 8.2 Tracked branches

표시 정보:

- repository
- exact `refs/heads/*`
- VSS project ID
- current HEAD SHA
- tracked 상태
- last fetched time

주요 기능:

- Branch 추적 등록
- History
- current tracked HEAD Index
- 수정
- Untrack

Index는 Repository sync와 분리된 **명시적 operator action**입니다.

### 8.3 Branch bindings

Frontend workspace/project와 Repository exact branch, VSS project ID 사이의 binding을 관리합니다.

주의: Module은 Frontend가 누구인지 네트워크 정보로 추론하지 않습니다. VSS 또는 공식 계약이 제공하지 않는 identity/session 값을 Admin UI가 만들어내지 않습니다.

### 8.4 Commits

기능:

- Repository별 commit history
- short SHA 표시
- full SHA tooltip
- GitHub repository인 경우 short SHA → GitHub commit page
- commit details
- associated branch/tag/change request refs
- commit availability 상태
- git-only commit materialization
- 두 commit 선택 후 comparison

Commit URL은 별도 row별 URL을 DB에 저장하는 방식이 아니라 canonical GitHub repository URL + full SHA에서 안전하게 파생합니다.

### 8.5 Sync history

Repository fetch/HEAD observation 실행의 이력을 표시합니다.

### 8.6 Snapshots

표시 정보:

- repository / branch
- base / target revision
- Snapshot state
- VSS state/reason
- attempt count
- update time

기능:

- detail
- materialized Snapshot Index
- retryable 실패 Snapshot Retry

Browser는 `project_root`를 직접 선택하거나 보내지 않습니다. Backend가 검증된 Snapshot/working-copy 정책으로 VSS 요청을 구성합니다.

### 8.7 VSS projects

VSS가 공식적으로 노출한 exact project catalog를 보여줍니다.
Admin 권한은 허용된 delete 경로를 통해 vector/project 삭제를 요청할 수 있습니다.

Admin Web은 VSS가 노출하지 않은 telemetry를 재구성하거나 가짜 값으로 표시하지 않습니다.

### 8.8 Runtime Models

Top-level runtime control은 Backend의 공식 runtime Admin API만 사용합니다.

표시:

- installed models
- Running/resident
- Stopped
- Auto Up 대상

operator action:

- Up
- Down
- Reload
- Auto Up ON/OFF

Browser가 Ollama API를 직접 호출하지 않습니다.

### 8.9 Module service controls

admin-only 기능입니다.

- Snapshot Backend restart
- Admin Web restart
- Module stack restart

실제 service 제어는 별도 Backend 운영 경계를 통해 수행됩니다. UI가 직접 `systemctl`을 실행하지 않습니다.

### 8.10 Chat observability

3-pane 관측 UI:

```text
Conversation list | Transcript | Trace inspector
```

표시 가능한 내용은 Backend가 저장하고 허용한 관측 데이터뿐입니다.

- conversation/message/response records
- VSS request trace
- model/embedding model observation
- timing
- source metadata
- retention preview/purge
- explicit conversation deletion

중요한 의미 경계:

- 이 기록은 **모델 conversational memory가 아닙니다.**
- 기록된 transcript를 자동으로 VSS prompt에 재주입하지 않습니다.
- VSS가 session/conversation identity를 공식 제공하지 않으면 Module이 Frontend session을 추론해 정본으로 만들지 않습니다.
- requester/origin도 VSS가 제공하지 않으면 `unknown`/unavailable semantics를 유지해야 합니다.

### 8.11 VSS request failures

VSS → Module inbound 관리/관측 경계에서 정상 success status가 아닌 요청을 운영자가 조사할 수 있도록 제공하는 admin-only 화면입니다.

### 8.12 Audit log

관리 mutation의 actor/action/target/outcome/reason/request ID를 조사합니다.
민감 credential이나 파일 본문을 감사 화면에 노출하지 않습니다.

---

## 9. Apple Liquid Glass UX v4

v4의 목표는 “Apple처럼 보이는 버튼”이 아니라 **macOS/iOS에서 익숙한 정보 구조와 interaction hierarchy를 Web Admin에 적용**하는 것입니다.

### 9.1 Liquid Glass visual system

사용 요소:

- translucent top bar
- translucent navigation/sidebar
- grouped control surfaces
- glass table/card surfaces
- glass modal/sheet
- semantic system-blue primary action
- semantic green/orange/red status
- soft depth/shadow
- system typography
- wallpaper를 통한 실제 background-through-glass depth

custom wallpaper:

```text
/assets/apple-glass-wallpaper.svg
```

배경은 천천히 drift하며, fine pointer 환경에서는 포인터 위치에 따라 glass highlight가 미세하게 이동합니다.

### 9.2 Hover / Press feedback

fine pointer 환경에서:

- hover elevation
- local pointer position 기반 specular highlight
- active press scale
- 빠른 release

Touch/coarse pointer에서는 hover를 핵심 UX로 요구하지 않습니다.

### 9.3 View Transition

사이드 navigation 이동 시 지원 브라우저에서는 View Transition API를 사용합니다.

- 앞으로 이동: 한 방향
- 이전 메뉴로 이동: 반대 방향
- blur/opacity/translation 조합

View Transition API가 없으면 CSS fallback을 사용합니다.
`prefers-reduced-motion: reduce`에서는 transition을 사실상 제거합니다.

### 9.4 Command Palette

Desktop shortcut:

```text
⌘K
Ctrl+K
/
```

검색/실행 대상:

- 전체 Admin navigation
- Refresh
- 현재 role에서 사용할 수 있는 create action
- Repository
- 현재 화면의 row

동작 예:

```text
⌘K
→ "vision"
→ h5vision/vision
→ Commits 화면으로 이동
```

Palette는 dialog를 사용하고 keyboard focus를 search field로 이동합니다.

### 9.5 Selection Inspector

Desktop에서는 main content 오른쪽의 detail pane으로 동작합니다.

```text
Navigation | Main content | Inspector
```

row 선택 시:

- 현재 entity label
- 안전한 필드/value
- 가능한 action
- GitHub/commit link

을 표시합니다.

행 내부의 기존 action button/link/input을 직접 누를 때는 row-selection이 가로채지 않습니다.

### 9.6 Keyboard-first table navigation

interactive data row에는 keyboard focus가 가능합니다.

- `ArrowDown`: 다음 row
- `ArrowUp`: 이전 row
- `Enter`: Inspector 열기
- `Escape`: Inspector 닫기 및 focus 복원

### 9.7 Activity Center

Admin mutation의 최근 실행 상태를 Browser memory에서 표시합니다.

기록 범위:

- operation label
- running/success/failed
- update time
- HTTP status 수준의 detail

기록하지 않는 것:

- request body
- password
- API key/token
- CSRF token
- Git credential
- source content

최대 최근 20개를 client memory에 유지하며 page reload 시 영속되지 않습니다.
Toolbar badge는 **현재 running operation 수가 1개 이상일 때만** 표시합니다.

---

## 10. Desktop responsive model

넓은 화면의 기본 구조는 macOS `NavigationSplitView + Inspector`에 대응하는 Web layout입니다.

```text
┌──────────────┬──────────────────────────────┬───────────────────┐
│ Navigation   │ Main content                 │ Inspector         │
│              │                              │                   │
│ Repository   │ Table / Chat / Operations    │ selected entity   │
│ Operations   │                              │ detail + actions  │
│ Observability│                              │                   │
└──────────────┴──────────────────────────────┴───────────────────┘
```

Inspector가 닫힌 상태에서는 main content가 전체 available width를 사용합니다.

---

## 11. iPhone / mobile responsive model

v4는 작은 화면을 desktop 축소판으로 취급하지 않습니다.

### 11.1 iPhone browser 목표

지원 대상으로 고려한 환경:

- iPhone Safari
- iPhone Chrome
- iPad Safari/Chrome
- Android/Chrome 계열

iOS의 Chrome도 OS WebKit 제약을 공유하므로 WebKit-safe CSS가 중요합니다.

### 11.2 Mobile navigation

Desktop sidebar 대신 **off-canvas glass navigation sheet**를 사용합니다.

Top bar의 Menu 버튼으로 열고 닫습니다.
Navigation item을 선택하면 mobile navigation은 자동으로 닫힙니다.

### 11.3 Mobile controls

Ollama model control과 Module service control은 좁은 top bar에 억지로 모두 배치하지 않습니다.

`Controls` 버튼 → bottom sheet

형태로 열어 touch target과 가독성을 확보합니다.

### 11.4 Mobile Inspector

Desktop right-side Inspector는 iPhone에서 **bottom sheet**로 변환됩니다.

- viewport 하단에서 등장
- 홈 인디케이터 safe area 확보
- 최대 높이 제한 + 내부 scroll
- backdrop tap / close button / Escape 지원

### 11.5 Mobile Activity Center

Activity Center도 bottom sheet로 표시합니다.
Running task badge는 top-level control에서 확인할 수 있습니다.

### 11.6 Mobile table → record cards

iPhone width에서는 넓은 table header를 숨기고 각 `tr`을 card-like record로 재배치합니다.
각 cell은 `data-label`을 통해 필드명을 함께 보여줍니다.

```text
┌─────────────────────────────┐
│ CANONICAL NAME              │
│ h5vision/vss_server         │
│                             │
│ PROVIDER                    │
│ github                      │
│                             │
│ DEFAULT BRANCH REF          │
│ refs/heads/main             │
│                             │
│ [Commits] [Sync] [...]      │
└─────────────────────────────┘
```

따라서 Repository/Commit/Snapshot 기본 탐색에서 수평 스크롤을 강요하지 않습니다.

### 11.7 iOS viewport/safe area

`index.html` viewport:

```html
width=device-width, initial-scale=1, viewport-fit=cover
```

주요 mobile sheet/nav는 다음을 고려합니다.

```text
env(safe-area-inset-top)
env(safe-area-inset-right)
env(safe-area-inset-bottom)
env(safe-area-inset-left)
```

또한 `100dvh`를 사용해 Safari 주소창이 접히고 펼쳐질 때의 viewport 변화에 대응합니다.

### 11.8 iOS input zoom 회피

모바일 Command Palette의 검색 입력은 16px 이상 font size를 사용하여 iOS Safari가 focus 시 임의로 page zoom하는 현상을 줄입니다.

### 11.9 Touch behavior

`hover: none`, `pointer: coarse` 환경에서는:

- hover 전제 제거
- 44px 수준 minimum touch target
- pointer-follow highlight 비활성/무의미화
- 직접적인 tap feedback 유지

---

## 12. Accessibility

현재 고려하는 accessibility 축은 다음과 같습니다.

### Keyboard

- Command Palette keyboard open
- row Arrow navigation
- Enter Inspector
- Escape close
- dialog focus
- `aria-current=page`
- button/select/input native keyboard semantics

### Motion

```css
@media (prefers-reduced-motion: reduce)
```

에서는 wallpaper/transition/button animation을 최소화합니다.

### Transparency

```css
@media (prefers-reduced-transparency: reduce)
```

에서는 backdrop blur 의존도를 낮추고 더 불투명한 surface로 fallback합니다.

### Contrast

```css
@media (prefers-contrast: more)
```

에서는 border와 focus visibility를 강화합니다.

### Dark mode

```css
@media (prefers-color-scheme: dark)
```

에서 wallpaper 밝기, glass surface, text/status 색을 별도로 조정합니다.

---

## 13. Error/Loading/Empty 상태

Admin Web은 HTTP code만 보고 오류 문구를 임의 추정하지 않습니다.
Backend/BFF의 구조화된 오류를 사용합니다.

```text
reason
detail
retryable
request_id
```

Table state:

- loading
- ready
- empty
- error

특정 binding error는 관련 Binding 화면으로 이동할 수 있는 복구 action을 제공합니다.

401 중에서도 `AUTHENTICATION_REQUIRED`인 경우에만 login state로 전환해, Backend 업무 오류와 session 만료를 혼동하지 않습니다.

---

## 14. Repository discovery UX

GitHub repository URL을 등록할 때 server-side discovery가 canonical metadata를 조회합니다.

목표는 다음과 같습니다.

```text
사용자가 입력
Repository URL

Module이 발견
provider
github owner/name
canonical repository name
canonical clone URL
default branch
provider repository identity/visibility

사용자가 결정
실제로 추적/index할 branch
```

보안상 discovery가 사용자가 지정한 임의 URL을 server-side fetch하는 구조가 되지 않도록 GitHub host를 확인하고 공식 GitHub API 경계만 사용합니다.

---

## 15. Commit/GitHub UX

GitHub repository인 경우 Commits 화면의 short SHA는 클릭 가능한 링크입니다.

표시:

```text
93f4769a
```

실제 link identity:

```text
<canonical GitHub repository URL>/commit/<full 40-char SHA>
```

- 새 tab
- `noopener noreferrer`
- tooltip에 full SHA
- GitHub가 아니거나 canonical URL을 안전하게 만들 수 없으면 plain text fallback

---

## 16. Chat observability UI

Chat 화면은 운영 관측을 위한 UI이며 일반 Chat client가 아닙니다.

### Session pane

- 저장된 관측 conversation 검색
- requester/model 등 저장된 allowlisted metadata 검색
- retention action

### Transcript pane

- user/assistant/system message display
- response 선택
- selected response trace와 연동

### Trace Inspector

- VSS trace lifecycle
- timing
- source/reference metadata
- model observation
- runtime resident observation

VSS가 제공하지 않은 origin/requester/session identity를 네트워크 peer나 User-Agent에서 추정해서 표시해서는 안 됩니다.

---

## 17. Cache/version strategy

현재 HTML은 정적 asset에 query version을 붙입니다.

```text
/styles.css?v=apple-ui-v4
/app.js?v=apple-ui-v4
```

Admin Web 자체도 정적 asset에 `Cache-Control: no-cache`를 적용합니다.
AWS에서 새 UI commit을 pull한 뒤 service를 재시작하고 browser가 이전 asset을 계속 들고 있는 문제를 줄이기 위한 전략입니다.

---

## 18. 환경 변수

핵심 Admin Web 설정:

| 변수 | 의미 | 기본/제약 |
|---|---|---|
| `ADMIN_WEB_USERS_FILE` | Argon2 admin user JSON | 필수 |
| `ADMIN_WEB_SESSION_SECRET` | session signing secret | 32 bytes 이상, 필수 |
| `ADMIN_WEB_BACKEND_SERVICE_TOKEN` | Backend service auth | 24 chars 이상, 필수 |
| `ADMIN_WEB_BACKEND_SIGNING_SECRET` | Backend request HMAC | 32 bytes 이상, 필수 |
| `ADMIN_WEB_BACKEND_URL` | Snapshot Backend | 기본 `http://127.0.0.1:8000`, loopback IP만 |
| `ADMIN_WEB_ALLOWED_ORIGINS` | mutation 허용 origin | 명시적 HTTP(S) origin |
| `ADMIN_WEB_SECURE_COOKIES` | Secure session cookie | 기본 true |
| `ADMIN_WEB_BACKEND_TIMEOUT_SECONDS` | 일반 Backend timeout | 기본 30s |
| `ADMIN_WEB_INDEX_TIMEOUT_SECONDS` | Index/Retry timeout | 기본 120s |
| `ADMIN_WEB_RUNTIME_MODEL_TIMEOUT_SECONDS` | model lifecycle timeout | 기본 210s |
| `ADMIN_WEB_MAX_REQUEST_BODY_BYTES` | request body 제한 | 기본 1 MiB |

세 가지 credential(`session secret`, `service token`, `signing secret`)은 서로 동일할 수 없습니다.

---

## 19. 로컬 실행

Windows 개발 예:

```powershell
cd module
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

.venv\Scripts\python.exe -m admin_web.passwords
$env:ADMIN_WEB_USERS_FILE = 'C:\secure\admin-users.json'
$env:ADMIN_WEB_SESSION_SECRET = '<32-byte-or-longer-secret>'
$env:ADMIN_WEB_BACKEND_SERVICE_TOKEN = '<backend-service-token>'
$env:ADMIN_WEB_BACKEND_SIGNING_SECRET = '<different-32-byte-signing-secret>'
$env:ADMIN_WEB_SECURE_COOKIES = 'false'

.venv\Scripts\python.exe -m admin_web
```

local HTTP 개발이 아니라 production HTTPS라면 Secure cookie를 끄지 않습니다.

---

## 20. AWS 운영 배치

현재 동일 AWS host의 기본 서비스 경계:

```text
Admin Web          :4180
Snapshot Backend   127.0.0.1:8000
VSS                127.0.0.1:8200
Ollama             127.0.0.1:11434
PostgreSQL         127.0.0.1:5432
```

systemd service:

```text
vss-admin-web.service
vss-snapshot.service
vss-server.service
```

Admin Web 정적 HTML/CSS/JS/BFF 코드만 변경된 배포는 보통 다음이면 충분합니다.

```bash
cd /home/ubuntu/vss_server
git pull
sudo systemctl restart vss-admin-web.service
```

Snapshot Backend 코드/API가 함께 변경된 경우에는 별도로 `vss-snapshot.service` restart 여부를 판단합니다.
VSS 자체가 변경되지 않았다면 `vss-server.service`를 불필요하게 restart하지 않습니다.

Admin Web health:

```bash
curl -fsS http://127.0.0.1:4180/health
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4180/
```

`curl -I /`은 HEAD를 보내므로 GET-only root route와 동작이 다를 수 있습니다. UI availability 확인은 GET을 기준으로 합니다.

---

## 21. 테스트와 quality gate

Admin Web 변경 후 최소 확인:

```powershell
cd module
.venv\Scripts\python.exe -m pytest -q tests/unit/admin/test_admin_web_ui.py
.venv\Scripts\python.exe -m ruff check backend admin_web tests alembic scripts
.venv\Scripts\python.exe -m compileall -q backend admin_web tests alembic scripts
node --check admin_web/app.js
git diff --check
.venv\Scripts\python.exe -m pytest -q
```

v4 구현 직후 확인된 full suite 기준:

```text
324 passed
1 skipped
2 existing warnings
```

skip은 POSIX directory permission이 필요한 테스트이며, 두 warning은 기존 admin use-case mock의 unawaited coroutine warning입니다.

### 향후 권장 E2E gate

현재 Playwright runtime은 module 개발 환경에 설치되어 있지 않습니다.
향후 브라우저 quality gate를 추가한다면 다음 project matrix를 권장합니다.

```text
Desktop Chromium
Desktop WebKit
Desktop Firefox
iPhone-size WebKit
mobile Chromium viewport
```

E2E 핵심 scenario:

```text
login
→ Command Palette
→ Repository navigation
→ row keyboard selection
→ Inspector
→ mutation confirm dialog
→ Activity Center
→ mobile menu
→ mobile controls sheet
→ mobile bottom-sheet Inspector
→ logout
```

animation 검사는 arbitrary sleep보다 실제 visible/network/state condition을 기다려야 합니다.

---

## 22. v4 구현 검증 포인트

정적/코드 계약에서 최소 다음 marker를 확인합니다.

```text
apple-ui-v4
viewport-fit=cover
command-palette
selection-inspector
activity-panel
mobile-controls-panel
openCommandPalette
openInspector
renderActivities
safe-area-inset-bottom
-webkit-backdrop-filter
pointer: coarse
```

이 marker 검사는 실제 E2E의 대체물이 아니라 기능이 실수로 제거되지 않도록 하는 회귀 방지 장치입니다.

---

## 23. 현재 의도적으로 사용하지 않는 것

Admin Web은 현재 다음을 도입하지 않습니다.

- React/Next/Vue SPA framework
- client-side router library
- Redux/Zustand 등 external state store
- Tailwind/Bootstrap/MUI
- 외부 analytics/tracking script
- 외부 CDN resource
- Browser의 직접 PostgreSQL/VSS/Ollama 접근
- Frontend implementation-specific contract
- VSS가 제공하지 않는 identity/telemetry 추론
- Chat observability history의 자동 prompt injection

현재 규모에서는 build-less static UI + FastAPI BFF가 배포 단순성, CSP, 운영 추적성 면에서 장점이 있습니다. UI 복잡도가 더 커질 경우 framework 도입은 별도 architecture decision으로 검토합니다.

---

## 24. 현재 한계와 후속 개선 후보

### 실제 WebKit/iPhone E2E

v4는 iOS/WebKit safe-area와 CSS/browser 특성을 고려해 구현됐지만 실제 자동화 WebKit matrix는 아직 없습니다.
가장 가치가 높은 다음 quality 작업입니다.

### Visual regression

Liquid Glass는 blur/transparency가 많으므로 DOM contract만으로는 시각 회귀를 완전히 잡을 수 없습니다.
향후 Playwright screenshot baseline을 light/dark/mobile 조합으로 추가하는 것이 좋습니다.

### View state preservation

현재 top-level navigation은 각 view를 다시 load하는 구조입니다.
필터/scroll/selected row를 view별로 장기 보존하는 navigation state cache는 추가 개선 여지가 있습니다.

### Activity persistence

Activity Center는 의도적으로 Browser memory-only입니다.
서버 측 장기 operation history는 Audit/Sync/Snapshot 등 authoritative Backend record를 사용하고, client activity를 별도 DB truth로 만들지 않습니다.

### Mobile gesture

현재 mobile sheet는 button/backdrop/keyboard 중심입니다.
향후 실제 iPhone 검증을 거쳐 drag-to-dismiss를 추가할 수 있지만, scroll과 충돌하는 custom gesture state machine을 먼저 도입하지 않습니다.

---

## 25. 설계 원칙 요약

Admin Web의 현재 설계는 다음 원칙을 유지합니다.

```text
보안 경계는 UI가 아니라 BFF/Backend가 가진다.
Browser는 관리 대상 runtime에 직접 접근하지 않는다.
VSS 의미론을 Module이 추측하지 않는다.
Repository sync와 VSS Index를 분리한다.
파괴적/실행성 작업은 명시적 사용자 action으로 시작한다.
Desktop은 split-view + inspector를 사용한다.
Mobile은 desktop 축소판이 아니라 sheet/card interaction을 사용한다.
Glass는 장식보다 hierarchy를 표현하는 데 사용한다.
색은 semantic state/primary action에 사용한다.
Hover는 보조 신호이며 touch UX의 전제가 아니다.
Reduced motion/transparency와 safe area를 기본 설계에 포함한다.
Client observability와 authoritative server state를 구분한다.
```

이 원칙을 깨는 UX 변경은 시각적으로 더 화려하더라도 현재 Admin Web의 개선으로 간주하지 않습니다.
