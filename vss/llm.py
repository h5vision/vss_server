"""
LLM 호출 — Ollama /api/chat. 이 서버가 직접 호출합니다 (P 게이트웨이 소멸에 따른 결정, DECISIONS 참조).

  chat()           전체 답변 문자열
  chat_stream()    조각(str) generator. 마지막에 done 정보를 attribute 로 남기지 않고 호출자가 이어 붙입니다.
  pick_model()     요청 경로 — Ollama 에 **올라온** completion 모델 중에서만 고른다. 없으면 ModelNotLoaded.
  ensure_loaded()  기동 경로 — 목표 모델이 없으면 올린다. 이 모듈에서 .env 모델 이름이 Ollama 로 가는 **유일한** 자리.

모델 상태는 기동 때만 바꾸고 요청 경로에서는 절대 바꾸지 않는다 (md 결정 2026-09-05, 기동 예외는 2026-09-06).
모든 요청은 keep_alive=-1 을 싣는다 — Ollama 는 마지막 요청의 keep_alive 로 만료를 다시 잡으므로 한 요청이라도 빠지면 데몬 기본값(5분)으로 돌아간다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator

from .config import CFG


class LLMError(RuntimeError):
    pass


class ModelNotLoaded(LLMError):
    """요청·설정된 생성 모델이 Ollama 메모리에 없다. 서버는 모델을 올리지 않는다 (md 결정 2026-09-05).

    이름을 Ollama 에 던지는 것이 곧 로드 요청이고, VRAM 이 모자라면 상주 모델이 내려간다.
    그래서 `/api/ps` 에 없는 모델은 부르지 않고 이 예외로 끝낸다. HTTP 는 503 `model_not_loaded`.
    """
    code = "model_not_loaded"

    def __init__(self, requested: str | None, loaded: list[str]):
        self.requested = requested
        self.loaded = list(loaded)
        want = f"모델 '{requested}' 이(가)" if requested else "생성 모델이"
        have = ", ".join(self.loaded) if self.loaded else "없음"
        super().__init__(f"{want} Ollama 에 올라와 있지 않습니다 (올라온 completion 모델: {have}). "
                         "서버는 모델을 올리지 않습니다 — 먼저 띄운 뒤 다시 요청하십시오.")


class ThinkUnsupported(LLMError):
    """보낸 `think` 값을 모델·Ollama 가 거부했다 (예: thinking 을 끌 수 없는 모델에 false).
    조용히 값을 바꿔 다시 보내지 않는다 — 브리핑은 이 코드로 즉시 실패하고 설정을 바꾸라고 알린다 (2026-09-09)."""
    code = "think_unsupported"


def _request(payload: dict, timeout: int):
    req = urllib.request.Request(
        f"{CFG.ollama_url.rstrip('/')}/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise LLMError(f"Ollama HTTP {e.code}: {e.read()[:300]!r}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"Ollama 접속 실패 ({CFG.ollama_url}): {e.reason}") from e


def think_flag() -> bool | None:
    """VSS_THINK 를 Ollama 의 payload 값으로. 비어 있으면 None — 요청에 싣지 않습니다.

    thinking 은 `options` 가 아니라 **payload 최상위** 필드입니다 (Ollama 0.9+).
    추론 내용은 `message.thinking` 으로 분리돼 오므로 켜져 있어도 답변이 오염되지는 않고, 느려질 뿐입니다.
    """
    v = (CFG.think or "").strip().lower()
    if not v:
        return None
    return v in ("1", "true", "yes", "on")


def parse_think(value: str | None) -> bool | str | None:
    """브리핑 전용 설정(VSS_BRIEFING_THINK)을 payload 의 `think` 값으로. think_flag() 와 달리 문자열을 살린다.

    비면 None(호출자가 VSS_THINK 를 따른다), true/false 계열은 bool, 그 밖은 소문자 문자열 그대로 — gpt-oss 처럼
    thinking 을 끌 수 없고 low|medium|high 만 받는 모델용. 값이 맞는지는 Ollama 가 판정한다 (ThinkUnsupported)."""
    v = (value or "").strip()
    if not v:
        return None
    low = v.lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    return low


KEEP_ALIVE = -1     # 상주. 기동 때 올린 모델을 요청이 다시 5분짜리로 만들지 않게 모든 payload 에 싣는다.


def _payload(model: str | None, messages: list[dict], *, stream: bool, options: dict,
             think: bool | str | None = None) -> dict:
    """받은 이름을 **그대로** 쓴다. 다시 해석하지 않는다 — 예전 resolve_model 재호출이 override=false 에서
    pick_model 이 고른 모델을 .env 모델로 바꿔 보내 로드 요청을 만들었다 (2026-09-05 검증에서 재현).
    None 이면 pick_model() — 올라온 것 중에서.
    think: None 이면 VSS_THINK(think_flag) 를 따르고, 값이 있으면 그 값을 싣는다 (브리핑 전용 경로, 2026-09-09)."""
    p = {"model": model or pick_model(), "messages": messages, "stream": stream, "options": options,
         "keep_alive": KEEP_ALIVE}
    effective = think_flag() if think is None else think
    if effective is not None:
        p["think"] = effective
    return p


def _norm(name: str) -> str:
    """Ollama 는 태그 없는 이름을 `:latest` 로 본다. 비교도 그 규칙으로 한다 (`qwen3.8` ≠ `qwen3.8:27b`)."""
    return name if ":" in name else f"{name}:latest"


def pick_model(requested: str | None = None, *, purpose: str = "chat",
               loaded: list[str] | None = None) -> str:
    """Ollama 에 **올라와 있는** completion 모델 중에서 고른다. 없으면 ModelNotLoaded.

    순서:
      1. 요청 모델(`model_id`, override 허용 시) — 올라와 있으면 그것, 아니면 **예외**. 요청은 명시적이라 조용히 바꾸지 않는다.
      2. 요청이 없으면 .env 선호값(briefing 이면 VSS_BRIEFING_MODEL → VSS_CHAT_MODEL) — 올라와 있으면 그것.
      3. 선호값도 없으면 올라온 것 중 첫 번째(`/api/ps` 순서). .env 값은 "반드시" 가 아니라 "여럿이면 이걸 먼저" 다.
      4. 하나도 없으면 예외.
    `loaded` 를 주면 `/api/ps` 를 부르지 않는다 (테스트·한 요청 안에서 재사용).
    """
    if loaded is None:
        loaded = models()
    by_norm = {_norm(n): n for n in loaded}

    if requested and CFG.allow_model_override:
        hit = by_norm.get(_norm(requested))
        if hit is None:
            raise ModelNotLoaded(requested, loaded)
        return hit

    prefs = [CFG.briefing_model, CFG.chat_model] if purpose == "briefing" else [CFG.chat_model]
    for p in prefs:
        if p and _norm(p) in by_norm:
            return by_norm[_norm(p)]
    if loaded:
        return loaded[0]
    raise ModelNotLoaded(None, loaded)


def chat_result(messages: list[dict], *, model: str, temperature: float = 0.1,
                num_predict: int = 2000, response_format: str | dict | None = None,
                timeout: int | None = None, think: bool | str | None = None) -> dict:
    """Briefing-only structured response; preserve selected name, keep-alive and context.

    No model loading/warmup call. Existing chat()/chat_stream() behavior stays intact.
    Caller checks residency and input budget. Tokens are Ollama's actual response counts.
    think: 브리핑 전용 값(parse_think 결과). None 이면 VSS_THINK 그대로 — /v1/chat 과 같은 규칙.
    """
    options = {"num_ctx": CFG.num_ctx, "temperature": temperature, "num_predict": num_predict}
    payload = _payload(model, messages, stream=False, options=options, think=think)
    if response_format is not None:
        payload["format"] = response_format
    with _request(payload, timeout or CFG.chat_timeout) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise LLMError(str(data["error"])[:300])
    return {"content": (data.get("message") or {}).get("content", ""),
            "done_reason": data.get("done_reason"),
            "stats": {k: data[k] for k in ("prompt_eval_count", "eval_count", "total_duration",
                       "prompt_eval_duration", "eval_duration", "load_duration") if k in data}}


def chat(messages: list[dict], *, model: str | None = None, temperature: float = 0.2,
         num_predict: int | None = None, timeout: int | None = None) -> str:
    options = {"num_ctx": CFG.num_ctx, "temperature": temperature}
    if num_predict:
        options["num_predict"] = num_predict
    payload = _payload(model, messages, stream=False, options=options)
    with _request(payload, timeout or CFG.chat_timeout) as r:
        data = json.loads(r.read())
    return (data.get("message") or {}).get("content", "")


def chat_stream(messages: list[dict], *, model: str | None = None, temperature: float = 0.2,
                num_predict: int | None = None, timeout: int | None = None) -> Iterator[dict]:
    """yield {"delta": str} ... 마지막에 {"done": True, "stats": {...}}"""
    options = {"num_ctx": CFG.num_ctx, "temperature": temperature}
    if num_predict:
        options["num_predict"] = num_predict
    payload = _payload(model, messages, stream=True, options=options)
    with _request(payload, timeout or CFG.chat_timeout) as r:
        for raw in r:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            piece = (obj.get("message") or {}).get("content", "")
            if piece:
                yield {"delta": piece}
            if obj.get("done"):
                stats = {k: obj.get(k) for k in ("eval_count", "eval_duration", "prompt_eval_count",
                                                 "prompt_eval_duration", "total_duration") if k in obj}
                yield {"done": True, "stats": stats}
                return


def loaded_names() -> list[str]:
    """/api/ps 에 올라온 모델 이름 전부 (임베딩 모델 포함, 중복 제거). 접속 실패는 LLMError."""
    base = CFG.ollama_url.rstrip("/")
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{base}/api/ps"), timeout=30) as r:
            running = json.loads(r.read()).get("models", [])
    except Exception as e:
        raise LLMError(f"Ollama 접속 실패: {e}") from e
    out: list[str] = []
    for row in running:
        name = row.get("name") or row.get("model")
        if name and name not in out:
            out.append(name)
    return out


def models() -> list[str]:
    """현재 Ollama 메모리에 올라온 모델 중 대화 생성이 가능한 모델 이름만 반환합니다."""
    base = CFG.ollama_url.rstrip("/")
    try:
        out: list[str] = []
        for name in loaded_names():
            req = urllib.request.Request(
                f"{base}/api/show",
                data=json.dumps({"model": name, "verbose": False}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                capabilities = json.loads(r.read()).get("capabilities")
            if not isinstance(capabilities, list):
                raise LLMError(f"Ollama 모델 능력 정보 없음: {name}")
            if "completion" in capabilities:
                out.append(name)
        return out
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(f"Ollama 접속 실패: {e}") from e


def ensure_loaded(model: str | None = None, *, timeout: int = 600) -> dict:
    """기동 전용. 목표 모델(기본 .env VSS_CHAT_MODEL)이 Ollama 에 없으면 올린다. 있으면 아무 요청도 보내지 않는다.

    올리는 방법은 `/api/chat` 에 빈 messages — Ollama 는 이를 "생성 없이 로드" 로 처리한다. keep_alive=-1.
    다른 모델은 건드리지 않는다 (내리기 없음 — 공용 노드, md 결정 2026-09-06). VRAM 이 모자라면 Ollama 스케줄러가
    스스로 무언가를 내릴 수 있으므로 올린 뒤 /api/ps 를 다시 읽어 결과를 돌려준다. 호출자가 그 한 줄을 기동 로그에 찍는다.

    반환: {"model": 실제 이름, "action": "none"|"loaded", "ok": bool, "others": [함께 떠 있는 다른 모델]}
    설치돼 있지 않은 모델(404)·접속 실패는 LLMError 로 올린다 — 서버는 그래도 뜬다 (호출자가 잡는다).
    """
    target = model or CFG.chat_model
    before = loaded_names()
    by_norm = {_norm(n): n for n in before}
    hit = by_norm.get(_norm(target))
    if hit is not None:
        return {"model": hit, "action": "none", "ok": True, "others": [n for n in before if n != hit]}
    with _request({"model": target, "messages": [], "keep_alive": KEEP_ALIVE}, timeout) as r:
        r.read()
    after = loaded_names()
    ok = _norm(target) in {_norm(n) for n in after}
    return {"model": target, "action": "loaded", "ok": ok,
            "others": [n for n in after if _norm(n) != _norm(target)]}
