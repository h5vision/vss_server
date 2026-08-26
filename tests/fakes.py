"""테스트용 가짜 임베더·LLM. Ollama 없이 파이프라인 전체를 돌립니다."""

from __future__ import annotations

import hashlib
import math
import re

DIM = 1024
_TOK = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[가-힣]{2,}|\d+")


def fake_embed_many(texts, *, model=None, expected_dim=None):
    """단어 해시 기반 희소 벡터. 같은 단어를 공유하면 cosine 이 높아지므로 검색 테스트에 충분합니다."""
    out = []
    for t in texts:
        v = [0.0] * DIM
        toks = _TOK.findall((t or "").lower())
        for tok in toks:
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % DIM] += 1.0
            v[(h // DIM) % DIM] += 0.5
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def fake_embed_one(text, *, model=None, expected_dim=None):
    return fake_embed_many([text], model=model, expected_dim=expected_dim)[0]


class FakeLLM:
    def __init__(self, answer="이 함수는 검색 결과를 조립합니다 [1]. 자세한 규칙은 문서에 있습니다 [2]."):
        self.answer = answer
        self.calls = []

    def chat(self, messages, *, model=None, temperature=0.2, num_predict=None, timeout=None):
        self.calls.append(messages)
        user = messages[-1]["content"]
        if "## 기능 목록" in user:
            return "## 이 프로젝트는\n검색 계층 프로젝트입니다 [1].\n\n## 기능 목록\n- 인덱싱 [1]\n- 검색 [2]"
        if "문서 하나를 요약" in user:
            m = re.search(r"\[(\d+)\]", user)
            return f"이 문서는 규칙을 설명합니다 [{m.group(1) if m else 1}]."
        return self.answer

    def chat_stream(self, messages, *, model=None, temperature=0.2, num_predict=None, timeout=None):
        self.calls.append(messages)
        for piece in re.findall(r"\S+\s*", self.answer):
            yield {"delta": piece}
        yield {"done": True, "stats": {"eval_count": 20, "eval_duration": 1_000_000_000}}
