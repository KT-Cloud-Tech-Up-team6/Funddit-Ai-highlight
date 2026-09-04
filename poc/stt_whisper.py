"""STT — Whisper 로컬 (faster-whisper). 선택 설치: pip install faster-whisper

GPU(CUDA)로 돌리려면 pip으로 런타임 라이브러리를 받는다 (별도 CUDA 툴킷 설치 불필요):
  pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
CUDA를 못 잡으면 자동으로 CPU(int8)로 폴백한다.

Google STT 후보는 poc/stt_google.py (STEP 3).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from poc.models import Cue
from poc.transcript import merge_to_sentences, save_cues


def _register_nvidia_dlls() -> None:
    """pip 설치된 nvidia-* 휠의 bin 디렉터리를 DLL 검색 경로에 추가 (Windows)."""
    if sys.platform != "win32":
        return
    try:
        import nvidia  # type: ignore
    except ImportError:
        return
    for base in nvidia.__path__:
        for name in os.listdir(base):
            b = Path(base) / name / "bin"
            if b.is_dir():
                os.add_dll_directory(str(b))
                os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")


# 모델 캐시 — large-v3 로드에 수십 초가 걸린다. API 서버에서 호출마다 로드하면 안 된다.
_MODEL_CACHE: dict[tuple[str, str], tuple] = {}
_CACHE_LOCK = threading.Lock()


class SttUnavailable(RuntimeError):
    """STT 의존성이 없을 때 발생. SystemExit을 쓰면 서버 프로세스가 죽는다."""


def _load_model(model_size: str, device: str):
    from faster_whisper import WhisperModel

    if device == "cpu":
        return WhisperModel(model_size, device="cpu", compute_type="int8"), "cpu/int8"
    _register_nvidia_dlls()
    import numpy as np

    # GTX 10xx(Pascal)는 float16 미지원 → int8_float32 → float32 순으로 시도
    for ct in ("float16", "int8_float32", "float32"):
        try:
            m = WhisperModel(model_size, device="cuda", compute_type=ct)
            # 실제 커널 로드는 첫 encode에서 일어나므로 워밍업으로 실패를 조기 감지
            list(m.transcribe(np.zeros(16000, dtype=np.float32), language="ko")[0])
            return m, f"cuda/{ct}"
        except Exception as e:  # noqa: BLE001 - cuBLAS/cuDNN 미설치, 미지원 compute_type 등
            print(f"[stt] CUDA {ct} 실패 ({type(e).__name__}: {str(e)[:70]})")
    print("[stt] CUDA 사용 불가 -> CPU int8로 폴백")
    return WhisperModel(model_size, device="cpu", compute_type="int8"), "cpu/int8"


def transcribe(
    video: str | Path,
    out_json: str | Path,
    model_size: str = "large-v3",
    vad: bool = True,
    merge: bool = True,
    device: str = "auto",
    initial_prompt: str | None = None,
) -> list[Cue]:
    try:
        import faster_whisper  # noqa: F401
    except ImportError as e:
        raise SttUnavailable(
            "faster-whisper가 설치되어 있지 않습니다: pip install faster-whisper\n"
            "(빠른 확인은 --model small 권장 — large-v3는 CPU에서 10분 영상에 수십 분 걸릴 수 있음)"
        ) from e

    t0 = time.time()
    key = (model_size, device)
    with _CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
    if cached is not None:
        model, dev = cached
    else:
        model, dev = _load_model(model_size, device)
        with _CACHE_LOCK:
            _MODEL_CACHE[key] = (model, dev)
    t_load = time.time() - t0
    print(f"[stt] 모델 {model_size} 로드 {t_load:.0f}초 ({dev})")

    # VAD: 계획서 14번 — 무음 구간 환각 대응
    # initial_prompt: 제품명·고유명사 힌트 (상품 용어 목록에서 생성)
    t1 = time.time()
    segments, info = model.transcribe(
        str(video), language="ko", vad_filter=vad, beam_size=5, initial_prompt=initial_prompt
    )
    raw = [
        Cue(f"t_{i:03d}", int(s.start * 1000), int(s.end * 1000), s.text.strip())
        for i, s in enumerate(segments, 1)
    ]
    t_run = time.time() - t1
    print(
        f"[stt] 추론 {t_run:.0f}초 / 오디오 {info.duration:.0f}초 "
        f"(x{info.duration / max(t_run, 1e-6):.1f} 실시간) / 원시 세그먼트 {len(raw)}개"
    )

    cues = merge_to_sentences(raw) if merge else raw
    save_cues(cues, out_json, meta={
        "engine": "faster-whisper",
        "model": model_size,
        "device": dev,
        "load_sec": round(t_load, 1),
        "infer_sec": round(t_run, 1),
        "audio_sec": round(info.duration, 1),
        "raw_segments": len(raw),
    })
    return cues
