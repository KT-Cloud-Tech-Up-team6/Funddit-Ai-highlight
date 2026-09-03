"""Gemini 호출 래퍼. mock_file을 주면 API 없이 저장된 응답을 반환한다 (오프라인 테스트용).

호출마다 사용량(토큰·소요 시간)을 self.last_usage에 남기고, LLM_USAGE_LOG 환경변수(또는 기본 out/llm_usage.jsonl)에
한 줄씩 추가한다 → 계획서 10번 비용 표 실측용.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

DEFAULT_MODEL = "gemini-3.5-flash-lite"

# 단가 (USD / 1M tokens) — 2026-09 기준 공개 가격, 비용 표 추정용. 바뀌면 여기만 수정.
PRICE_PER_M = {
    "gemini-3.5-flash-lite": {"in": 0.10, "out": 0.40},
    "gemini-3.7-flash": {"in": 0.30, "out": 2.50},
}


class LLM:
    def __init__(self, model: str | None = None, mock_file: str | Path | None = None, tag: str = ""):
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.mock_file = mock_file
        self.tag = tag
        self.last_usage: dict | None = None

    def generate_json(self, system_prompt: str, payload: dict) -> dict:
        if self.mock_file:
            return json.loads(Path(self.mock_file).read_text(encoding="utf-8"))

        from google import genai  # 지연 임포트 — 오프라인 스모크는 패키지 없이 돈다

        client = genai.Client()  # GEMINI_API_KEY 환경변수 사용
        contents = system_prompt + "\n\n[입력]\n" + json.dumps(payload, ensure_ascii=False)
        t0 = time.time()
        resp = None
        for attempt in range(6):  # 503/429(과부하·한도)은 지수 백오프로 재시도
            try:
                resp = client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config={"response_mime_type": "application/json", "temperature": 0.2},
                )
                break
            except Exception as e:  # noqa: BLE001
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                if code not in (429, 503) or attempt == 5:
                    raise
                wait = 5 * 2 ** attempt
                print(f"[llm] {self.model} {code} → {wait}초 후 재시도 ({attempt+1}/5)")
                time.sleep(wait)
        elapsed = time.time() - t0
        u = resp.usage_metadata
        price = PRICE_PER_M.get(self.model, {"in": 0, "out": 0})
        in_tok = u.prompt_token_count or 0
        out_tok = u.candidates_token_count or 0
        think_tok = getattr(u, "thoughts_token_count", None) or 0
        self.last_usage = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "tag": self.tag,
            "model": self.model,
            "in_tokens": in_tok,
            "out_tokens": out_tok,
            "thinking_tokens": think_tok,
            "elapsed_sec": round(elapsed, 1),
            "est_usd": round((in_tok * price["in"] + (out_tok + think_tok) * price["out"]) / 1e6, 5),
        }
        log = Path(os.environ.get("LLM_USAGE_LOG", "out/llm_usage.jsonl"))
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.last_usage, ensure_ascii=False) + "\n")
        print(f"[llm] {self.model} {self.tag}: in={in_tok} out={out_tok} think={think_tok} "
              f"{elapsed:.1f}s ≈ ${self.last_usage['est_usd']:.4f}")
        try:
            return json.loads(resp.text)
        except json.JSONDecodeError:
            raw_path = log.parent / f"llm_raw_{self.tag or 'resp'}_{int(t0)}.txt"
            raw_path.write_text(resp.text or "", encoding="utf-8")
            raise RuntimeError(f"모델 응답이 JSON이 아님 → {raw_path}")
