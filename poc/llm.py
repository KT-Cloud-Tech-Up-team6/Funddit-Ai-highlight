"""Gemini 호출 래퍼. mock_file을 주면 API 없이 저장된 응답을 반환한다 (오프라인 테스트용)."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_MODEL = "gemini-3.5-flash-lite"


class LLM:
    def __init__(self, model: str | None = None, mock_file: str | Path | None = None):
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.mock_file = mock_file

    def generate_json(self, system_prompt: str, payload: dict) -> dict:
        if self.mock_file:
            return json.loads(Path(self.mock_file).read_text(encoding="utf-8"))

        from google import genai  # 지연 임포트 — 오프라인 스모크는 패키지 없이 돈다

        client = genai.Client()  # GEMINI_API_KEY 환경변수 사용
        contents = system_prompt + "\n\n[입력]\n" + json.dumps(payload, ensure_ascii=False)
        resp = client.models.generate_content(
            model=self.model,
            contents=contents,
            config={"response_mime_type": "application/json", "temperature": 0.2},
        )
        return json.loads(resp.text)
