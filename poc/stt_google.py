"""STT — Google Cloud Speech-to-Text v2 (CFG-2/3용). 선택 설치: pip install google-cloud-speech

준비물 (사용자):
  1) GCP 프로젝트에서 Speech-to-Text API 활성화
  2) 서비스 계정 JSON 키 → 환경변수 GOOGLE_APPLICATION_CREDENTIALS=키경로
  3) 환경변수 GOOGLE_CLOUD_PROJECT=프로젝트ID
  (선택) GOOGLE_STT_LOCATION — chirp_3 기본 "us". 한국 리전 지원 여부는 콘솔에서 확인.

설계:
  - 동기 Recognize는 오디오 1분/10MB 한도 → GCS 버킷 없이 돌리기 위해
    ffmpeg silencedetect로 무음 경계에서 ≤55초 조각으로 나눠 호출하고, 시각은 조각 오프셋을 더해 합친다.
  - 단어 단위 타임코드(enable_word_time_offsets)로 큐 start/end를 잡고,
    Whisper와 동일하게 merge_to_sentences로 문장 재병합 → 두 엔진의 큐 형식이 같아진다.
  - 상품 용어 목록은 adaptation(phrase set, boost)으로 주입 → 제품명·숫자 인식 보조.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from poc.models import Cue
from poc.transcript import merge_to_sentences, save_cues

CHUNK_MAX_SEC = 55.0
SILENCE_DB = -35
SILENCE_MIN_SEC = 0.4


def extract_wav(video: str | Path, wav: str | Path) -> float:
    """16kHz mono PCM 추출. 반환: 길이(초)."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(wav)],
        check=True,
    )
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def detect_silences(wav: str | Path, noise_db: int = SILENCE_DB, min_sec: float = SILENCE_MIN_SEC) -> list[tuple[float, float]]:
    """ffmpeg silencedetect → (start, end) 목록."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(wav), "-af",
         f"silencedetect=noise={noise_db}dB:d={min_sec}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", proc.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    return list(zip(starts, ends))


def plan_chunks(
    duration: float,
    silences: list[tuple[float, float]],
    max_sec: float = CHUNK_MAX_SEC,
    fallback_silences: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    """무음 중앙점을 절단 후보로 써서 각 조각이 max_sec 이하가 되게 나눈다.
    1차 후보(엄격한 무음)가 없으면 2차 후보(배경음악 구간용 느슨한 무음), 그것도 없으면 강제 절단."""
    primary = sorted((s + e) / 2 for s, e in silences)
    secondary = sorted((s + e) / 2 for s, e in (fallback_silences or []))
    chunks: list[tuple[float, float]] = []
    start = 0.0
    while duration - start > max_sec:
        limit = start + max_sec
        cands = [c for c in primary if start + 5.0 < c <= limit]
        if not cands:
            cands = [c for c in secondary if start + 5.0 < c <= limit]
        if cands:
            cut = max(cands)
        else:
            cut = limit
            print(f"[stt-google] {start:.0f}~{limit:.0f}초 사이에 무음 없음 → {limit:.0f}초에서 강제 절단")
        chunks.append((start, cut))
        start = cut
    chunks.append((start, duration))
    return chunks


def plan_chunks_for(wav: str | Path, duration: float) -> list[tuple[float, float]]:
    return plan_chunks(duration, detect_silences(wav), fallback_silences=detect_silences(wav, noise_db=-22, min_sec=0.25))


def _cut(wav: Path, start: float, end: float, out: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(wav), "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
         "-c", "copy", str(out)],
        check=True,
    )


def _phrases_from_terms(terms_path: str | Path | None) -> list[str]:
    if not terms_path:
        return []
    terms = json.loads(Path(terms_path).read_text(encoding="utf-8"))
    out = [terms.get("product_name", "")]
    out += [t["canonical"] for t in terms.get("terms", [])]
    out += [t["surface"] for t in terms.get("terms", [])]
    return [p for p in dict.fromkeys(out) if p]


def transcribe(
    video: str | Path,
    out_json: str | Path,
    model: str = "chirp_3",
    terms_path: str | Path | None = None,
    merge: bool = True,
    location: str | None = None,
) -> list[Cue]:
    try:
        from google.cloud.speech_v2 import SpeechClient
        from google.cloud.speech_v2.types import cloud_speech
        from google.api_core.client_options import ClientOptions
    except ImportError as e:
        raise SystemExit("google-cloud-speech가 없습니다: pip install google-cloud-speech") from e

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SystemExit("환경변수 GOOGLE_CLOUD_PROJECT(프로젝트 ID)와 GOOGLE_APPLICATION_CREDENTIALS(서비스 계정 키)가 필요합니다")
    location = location or os.environ.get("GOOGLE_STT_LOCATION", "us")
    endpoint = "speech.googleapis.com" if location == "global" else f"{location}-speech.googleapis.com"
    client = SpeechClient(client_options=ClientOptions(api_endpoint=endpoint))
    recognizer = f"projects/{project}/locations/{location}/recognizers/_"

    phrases = _phrases_from_terms(terms_path)
    adaptation = None
    if phrases:
        adaptation = cloud_speech.SpeechAdaptation(
            phrase_sets=[cloud_speech.SpeechAdaptation.AdaptationPhraseSet(
                inline_phrase_set=cloud_speech.PhraseSet(
                    phrases=[cloud_speech.PhraseSet.Phrase(value=p, boost=10.0) for p in phrases]
                )
            )]
        )
    config = cloud_speech.RecognitionConfig(
        auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
        language_codes=["ko-KR"],
        model=model,
        features=cloud_speech.RecognitionFeatures(
            enable_word_time_offsets=True,
            enable_automatic_punctuation=True,
        ),
        adaptation=adaptation,
    )

    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        duration = extract_wav(video, wav)
        chunks = plan_chunks_for(wav, duration)
        print(f"[stt-google] 오디오 {duration:.0f}초 → 조각 {len(chunks)}개 (model={model}, location={location})")

        raw: list[Cue] = []
        api_sec = 0.0
        billed_sec = 0.0
        for i, (cs, ce) in enumerate(chunks, 1):
            part = Path(td) / f"chunk_{i:02d}.wav"
            _cut(wav, cs, ce, part)
            billed_sec += ce - cs
            t1 = time.time()
            resp = client.recognize(request=cloud_speech.RecognizeRequest(
                recognizer=recognizer, config=config, content=part.read_bytes()
            ))
            api_sec += time.time() - t1
            for r in resp.results:
                if not r.alternatives:
                    continue
                alt = r.alternatives[0]
                text = alt.transcript.strip()
                if not text:
                    continue
                if alt.words:
                    s = alt.words[0].start_offset.total_seconds()
                    e = alt.words[-1].end_offset.total_seconds()
                else:
                    s, e = 0.0, ce - cs
                raw.append(Cue(f"t_{len(raw)+1:03d}", int((cs + s) * 1000), int((cs + e) * 1000), text))
            print(f"[stt-google] 조각 {i}/{len(chunks)} ({cs:.0f}~{ce:.0f}초) 결과 {len(resp.results)}개")

    cues = merge_to_sentences(raw) if merge else raw
    save_cues(cues, out_json, meta={
        "engine": "google-stt-v2",
        "model": model,
        "location": location,
        "chunks": len(chunks),
        "api_sec": round(api_sec, 1),
        "total_sec": round(time.time() - t0, 1),
        "audio_sec": round(duration, 1),
        "billed_audio_sec": round(billed_sec, 1),
        "raw_segments": len(raw),
        "phrase_hints": len(phrases),
    })
    return cues


if __name__ == "__main__":  # 조각 계획만 확인 (API 불필요): python -m poc.stt_google 영상.mp4
    import sys

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        d = extract_wav(sys.argv[1], wav)
        sil = detect_silences(wav)
        ch = plan_chunks_for(wav, d)
        print(f"길이 {d:.1f}초, 무음 {len(sil)}개, 조각 {len(ch)}개")
        for s, e in ch:
            print(f"  {s:6.1f} ~ {e:6.1f}  ({e-s:4.1f}초)")
