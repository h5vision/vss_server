"""
LLM 호출 — Ollama /api/chat. 이 서버가 직접 호출합니다 (P 게이트웨이 소멸에 따른 결정, DECISIONS 참조).

  chat()         전체 답변 문자열
  chat_stream()  조각(str) generator. 마지막에 done 정보를 attribute 로 남기지 않고 호출자가 이어 붙입니다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator

from .config import CFG


class LLMError(RuntimeError):
    pass


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


def resolve_model(requested: str | None = None, *, purpose: str = "chat") -> str:
    if requested and CFG.allow_model_override:
        return requested
    if purpose == "briefing" and CFG.briefing_model:
        return CFG.briefing_model
    return CFG.chat_model


def chat(messages: list[dict], *, model: str | None = None, temperature: float = 0.2,
         num_predict: int | None = None, timeout: int | None = None) -> str:
    options = {"num_ctx": CFG.num_ctx, "temperature": temperature}
    if num_predict:
        options["num_predict"] = num_predict
    payload = {"model": resolve_model(model), "messages": messages, "stream": False, "options": options}
    with _request(payload, timeout or CFG.chat_timeout) as r:
        data = json.loads(r.read())
    return (data.get("message") or {}).get("content", "")


def chat_stream(messages: list[dict], *, model: str | None = None, temperature: float = 0.2,
                num_predict: int | None = None, timeout: int | None = None) -> Iterator[dict]:
    """yield {"delta": str} ... 마지막에 {"done": True, "stats": {...}}"""
    options = {"num_ctx": CFG.num_ctx, "temperature": temperature}
    if num_predict:
        options["num_predict"] = num_predict
    payload = {"model": resolve_model(model), "messages": messages, "stream": True, "options": options}
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


def models() -> list[str]:
    req = urllib.request.Request(f"{CFG.ollama_url.rstrip('/')}/api/tags")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception as e:
        raise LLMError(f"Ollama 접속 실패: {e}") from e


def warmup(model: str | None = None) -> None:
    """콜드스타트(수 초) 회피. 데모 전 서버 기동 시 호출합니다."""
    chat([{"role": "user", "content": "hi"}], model=model, num_predict=1, timeout=120)
