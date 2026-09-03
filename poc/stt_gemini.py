"""STT — Gemini 오디오 입력 (클라우드 STT 후보, Gemini 키 하나로 동작).

Google Cloud STT(별도 GCP 준비 필요) 대신/병행으로 쓰는 후보.
오디오를 Files API로 올리고 타임코드 포함 JSON 자막을 받는다. 타임코드는 모델 추정치라 Whisper보다 거칠 수 있다.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from poc.llm import PRICE_PER_M, _extract_json
from poc.models import Cue
from poc.transcript import merge_to_sentences, save_cues

PROMPT = """다음 한국어 라이브 쇼핑 방송 오디오를 받아써라.
- 발화를 문장 단위로 나누고 각 문장의 시작·끝 시각(초, 소수 1자리)을 적는다.
- 들리는 대로 정확히. 숫자·가격·단위는 아라비아 숫자로 (예: 20만 원, 90도, 12.5cm, 20,000Pa).
- 없는 말을 만들지 말고, 음악·무음 구간은 건너뛴다.
{hint}
출력 JSON: {{"segments": [{{"start": 0.0, "end": 3.2, "text": "..."}}, ...]}}"""


def transcribe(video: str | Path, out_json: str | Path, model: str = "gemini-3.7-flash",
               terms_path: str | Path | None = None, merge: bool = True) -> list[Cue]:
    from google import genai

    client = genai.Client()
    hint = ""
    if terms_path:
        terms = json.loads(Path(terms_path).read_text(encoding="utf-8"))
        words = [terms.get("product_name", "")] + [t["canonical"] for t in terms.get("terms", [])]
        hint = "- 고유명사·용어 참고: " + ", ".join(w for w in words if w)

    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        audio = Path(td) / "audio.mp3"  # 업로드 크기 절약 (16kHz mono 64kbps)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                        "-b:a", "64k", str(audio)], check=True)
        up = client.files.upload(file=str(audio))
        t_up = time.time() - t0
        def _gen(config, extra=""):
            for attempt in range(5):  # 503/429는 지수 백오프 재시도
                try:
                    return client.models.generate_content(
                        model=model, contents=[up, PROMPT.format(hint=hint) + extra], config=config)
                except Exception as e:  # noqa: BLE001
                    code = getattr(e, "code", None) or getattr(e, "status_code", None)
                    if code not in (429, 503) or attempt == 4:
                        raise
                    wait = 10 * 2**attempt
                    print(f"[stt-gemini] {model} {code} → {wait}초 후 재시도")
                    time.sleep(wait)

        try:
            resp = _gen({"response_mime_type": "application/json", "temperature": 0.0})
        except Exception as e:  # noqa: BLE001 — 'JSON mode is not enabled for this model'
            if "JSON mode" not in str(e):
                raise
            print(f"[stt-gemini] {model}: JSON 모드 미지원 → 텍스트 모드")
            resp = _gen({"temperature": 0.0}, "\n(JSON을 쓸 수 없으면 한 줄에 하나씩 '[시작초-끝초] 문장' 형식으로)")
        try:
            client.files.delete(name=up.name)
        except Exception:  # noqa: BLE001
            pass
    t_all = time.time() - t0
    try:
        data = _extract_json(resp.text or "")
    except Exception:  # noqa: BLE001 — 텍스트 모드: '[12.3-15.0] 문장' 줄 파싱
        segs = []
        for m in re.finditer(r"\[\s*([\d.]+)\s*[-~]\s*([\d.]+)\s*\]\s*(.+)", resp.text or ""):
            segs.append({"start": m.group(1), "end": m.group(2), "text": m.group(3)})
        data = {"segments": segs}
    raw = []
    for i, s in enumerate(data.get("segments", []), 1):
        try:
            raw.append(Cue(f"t_{i:03d}", int(float(s["start"]) * 1000), int(float(s["end"]) * 1000), str(s["text"]).strip()))
        except (KeyError, ValueError, TypeError):
            continue
    raw = [c for c in raw if c.text]
    u = resp.usage_metadata
    in_tok, out_tok = u.prompt_token_count or 0, u.candidates_token_count or 0
    think = getattr(u, "thoughts_token_count", 0) or 0
    price = PRICE_PER_M.get(model)
    est = round((in_tok * price[0] + (out_tok + think) * price[1]) / 1e6, 5) if price else None
    print(f"[stt-gemini] {model}: 업로드 {t_up:.0f}초, 전체 {t_all:.0f}초, in={in_tok} out={out_tok} think={think} ≈ ${est}")
    cues = merge_to_sentences(raw) if merge else raw
    save_cues(cues, out_json, meta={
        "engine": "gemini-audio", "model": model, "infer_sec": round(t_all - t_up, 1), "total_sec": round(t_all, 1),
        "audio_sec": None, "raw_segments": len(raw), "in_tokens": in_tok, "out_tokens": out_tok,
        "thinking_tokens": think, "est_usd": est,
    })
    return cues
