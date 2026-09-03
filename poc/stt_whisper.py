"""STT — Whisper 로컬 (faster-whisper). 선택 설치: pip install faster-whisper

Google STT 후보는 실제 영상 확보 후 비교 단계(STEP 3)에서 추가한다.
"""
from __future__ import annotations

from pathlib import Path

from poc.models import Cue
from poc.transcript import merge_to_sentences, save_cues


def transcribe(
    video: str | Path,
    out_json: str | Path,
    model_size: str = "large-v3",
    vad: bool = True,
    merge: bool = True,
) -> list[Cue]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise SystemExit(
            "faster-whisper가 설치되어 있지 않습니다: pip install faster-whisper\n"
            "(빠른 확인은 --model small 권장 — large-v3는 CPU에서 10분 영상에 수십 분 걸릴 수 있음)"
        ) from e

    model = WhisperModel(model_size, device="auto", compute_type="auto")
    # VAD: 계획서 14번 — 무음 구간 환각 대응
    segments, _info = model.transcribe(str(video), language="ko", vad_filter=vad)

    cues = [
        Cue(f"t_{i:03d}", int(s.start * 1000), int(s.end * 1000), s.text.strip())
        for i, s in enumerate(segments, 1)
    ]
    if merge:
        cues = merge_to_sentences(cues)
    save_cues(cues, out_json)
    return cues
