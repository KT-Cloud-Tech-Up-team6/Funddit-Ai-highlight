"""LLM 호출 래퍼 — 모델 이름 접두사로 프로바이더를 고른다.

  gemini-*  → google-genai (GEMINI_API_KEY)
  claude-*  → anthropic    (ANTHROPIC_API_KEY)
  gpt-* / o* → openai      (OPENAI_API_KEY)

mock_file을 주면 API 없이 저장된 응답을 반환한다 (오프라인 테스트용).
호출마다 사용량(토큰·소요 시간·추정 비용)을 self.last_usage에 남기고 LLM_USAGE_LOG(기본 out/llm_usage.jsonl)에 한 줄 추가한다.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

DEFAULT_MODEL = "gemini-3.5-flash-lite"

# 단가 (USD / 1M tokens). 비용 표 추정용 — None이면 비용을 '?'로 표시한다.
# Claude: 공식 표(2026-06). Gemini: 공개 가격 기준 추정치(변동 가능). OpenAI: 키 확보 후 확인.
PRICE_PER_M: dict[str, tuple[float, float] | None] = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-3-flash-preview": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.075, 0.30),
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-3.6-flash": (0.30, 2.50),
    "gemini-3.7-flash": (0.30, 2.50),
    "gemini-3.8-flash": (0.30, 2.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
}


def provider_of(model: str) -> str:
    if model.startswith("gemini"):
        return "gemini"
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    raise ValueError(f"프로바이더를 알 수 없는 모델명: {model}")


def _extract_json(text: str) -> dict:
    """```json 펜스·앞뒤 잡담이 붙어도 첫 { ... } 블록을 파싱한다."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    elif not text.startswith("{"):
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            text = text[i : j + 1]
    return json.loads(text)


class LLM:
    def __init__(self, model: str | None = None, mock_file: str | Path | None = None, tag: str = ""):
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.provider = provider_of(self.model)
        self.mock_file = mock_file
        self.tag = tag
        self.last_usage: dict | None = None

    # ---- 프로바이더별 호출: (text, in_tokens, out_tokens, thinking_tokens) 반환 ----
    def _call_gemini(self, system_prompt: str, user: str):
        from google import genai

        client = genai.Client()
        resp = client.models.generate_content(
            model=self.model,
            contents=system_prompt + "\n\n" + user,
            config={"response_mime_type": "application/json", "temperature": 0.2},
        )
        u = resp.usage_metadata
        return resp.text or "", u.prompt_token_count or 0, u.candidates_token_count or 0, getattr(u, "thoughts_token_count", 0) or 0

    def _call_anthropic(self, system_prompt: str, user: str):
        import anthropic

        client = anthropic.Anthropic()
        kwargs = dict(
            model=self.model,
            max_tokens=16000,
            system=system_prompt + "\n\n출력은 JSON 객체 하나만. 설명·코드펜스 없이.",
            messages=[{"role": "user", "content": user}],
        )
        # Opus 5 / Sonnet 5는 adaptive thinking이 기본(생략). Haiku 4.5는 thinking 미사용.
        resp = client.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"{self.model} 거부: {getattr(resp.stop_details, 'explanation', '')}")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return text, resp.usage.input_tokens, resp.usage.output_tokens, 0

    def _call_openai(self, system_prompt: str, user: str):
        from openai import OpenAI

        client = OpenAI()
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt + "\n\n출력은 JSON 객체 하나만."},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        u = resp.usage
        think = 0
        if u and getattr(u, "completion_tokens_details", None):
            think = getattr(u.completion_tokens_details, "reasoning_tokens", 0) or 0
        return resp.choices[0].message.content or "", (u.prompt_tokens if u else 0), (u.completion_tokens if u else 0), think

    def generate_json(self, system_prompt: str, payload: dict) -> dict:
        if self.mock_file:
            return json.loads(Path(self.mock_file).read_text(encoding="utf-8"))

        user = "[입력]\n" + json.dumps(payload, ensure_ascii=False)
        call = {"gemini": self._call_gemini, "anthropic": self._call_anthropic, "openai": self._call_openai}[self.provider]

        t0 = time.time()
        text = ""
        for attempt in range(6):  # 429/503/529(과부하·한도)은 지수 백오프로 재시도
            try:
                text, in_tok, out_tok, think_tok = call(system_prompt, user)
                break
            except Exception as e:  # noqa: BLE001
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                if code not in (429, 503, 529, 500, 502) or attempt == 5:
                    raise
                wait = 5 * 2**attempt
                print(f"[llm] {self.model} {code} → {wait}초 후 재시도 ({attempt+1}/5)")
                time.sleep(wait)
        elapsed = time.time() - t0

        price = PRICE_PER_M.get(self.model)
        est = round((in_tok * price[0] + (out_tok + think_tok) * price[1]) / 1e6, 5) if price else None
        self.last_usage = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "tag": self.tag,
            "model": self.model,
            "provider": self.provider,
            "in_tokens": in_tok,
            "out_tokens": out_tok,
            "thinking_tokens": think_tok,
            "elapsed_sec": round(elapsed, 1),
            "est_usd": est,
        }
        log = Path(os.environ.get("LLM_USAGE_LOG", "out/llm_usage.jsonl"))
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.last_usage, ensure_ascii=False) + "\n")
        cost = f"${est:.4f}" if est is not None else "$?"
        print(f"[llm] {self.model} {self.tag}: in={in_tok} out={out_tok} think={think_tok} {elapsed:.1f}s ≈ {cost}")
        try:
            return _extract_json(text)
        except json.JSONDecodeError:
            raw_path = log.parent / f"llm_raw_{self.model}_{self.tag or 'resp'}_{int(t0)}.txt"
            raw_path.write_text(text or "", encoding="utf-8")
            raise RuntimeError(f"모델 응답이 JSON이 아님 → {raw_path}")
