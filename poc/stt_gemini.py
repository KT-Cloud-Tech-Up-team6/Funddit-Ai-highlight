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

# 큐 하나가 구간 분할과 자막의 최소 단위다. 길면 60~120초 구간 경계를
# 맞출 수 없고 자막도 뭉개진다. Whisper large-v3 가 20분 방송에서 340개를
# 뽑았는데 (큐당 3.5초), 기본 프롬프트의 Gemini 는 20개(큐당 60초)였다.
# 그래서 길이 상한을 숫자로 못박는다 — "문장 단위"만으로는 안 지킨다.
PROMPT = """다음 한국어 라이브 쇼핑 방송 오디오를 받아써라.

가장 중요한 규칙 — 짧게 쪼갠다:
- 한 세그먼트는 **최대 6초**다. 6초를 넘기면 쉼표·어절 경계에서 반드시 끊는다.
- 한 세그먼트는 **최대 40자**다. 길면 끊는다.
- 긴 문장은 한 덩어리로 두지 말고 여러 세그먼트로 나눈다.
- 10분 오디오면 세그먼트가 **100개 이상** 나와야 정상이다. 20~30개면 너무 적다.

그 밖의 규칙:
- 각 세그먼트의 시작·끝 시각(초, 소수 1자리)을 적는다. 시각은 겹치지 않게.
- 들리는 대로 정확히. 숫자·가격·단위는 아라비아 숫자로 (예: 20만 원, 90도, 12.5cm, 20,000Pa).
- 없는 말을 만들지 말고, 음악·무음 구간은 건너뛴다.
{hint}
출력 JSON: {{"segments": [{{"start": 0.0, "end": 3.2, "text": "..."}}, ...]}}"""


#: `{"start": 1.0, "end": 2.0, "text": "..."}` 한 덩어리. 중간에 키 이름이
#: 빠진 항목이 섞여 있어도 성한 것만 집어낸다.
_SEG_RE = re.compile(
    r'\{\s*"start"\s*:\s*([\d.]+)\s*,\s*(?:"end"\s*:\s*)?([\d.]+)\s*,\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"',
)
#: 텍스트 모드 폴백: `[12.3-15.0] 문장`
_LINE_RE = re.compile(r"\[\s*([\d.]+)\s*[-~]\s*([\d.]+)\s*\]\s*(.+)")


def _salvage_segments(text: str) -> list[dict]:
    """깨진 JSON 에서 성한 세그먼트만 건진다."""
    segs = [{"start": m.group(1), "end": m.group(2), "text": m.group(3)}
            for m in _SEG_RE.finditer(text)]
    if segs:
        return segs
    return [{"start": m.group(1), "end": m.group(2), "text": m.group(3)}
            for m in _LINE_RE.finditer(text)]


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
    text = resp.text or ""
    try:
        data = _extract_json(text)
        if not data.get("segments"):
            raise ValueError("segments 없음")
    except Exception:  # noqa: BLE001
        # 세그먼트가 수백 개면 그중 하나만 깨져도 json.loads 가 전부 버린다.
        # (실제로 `{"start": 596.5, 600.0, "text": ...}` 처럼 "end" 키가
        #  빠진 응답을 받았다.) 성한 것만 건져 쓴다 — 한 줄 때문에 10분치
        # 전사를 버릴 이유가 없다.
        data = {"segments": _salvage_segments(text)}
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
