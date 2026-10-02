"""API 서버 설정 — 경로·모델·임계값을 한 곳에 모은다."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── 저장소 ────────────────────────────────────────────────────────────
WORKSPACE = Path(os.environ.get("SHORTS_WORKSPACE", ROOT / "workspace"))

# ── 모델 (PoC 비교 결과 확정, 2026-09-03) ─────────────────────────────
# 근거: 방송 2건 × 7등급 × 3회 실측. 자막 정보량 최다(10개/수치 5개),
#       최저 점수 0.87, 상위권 모델 중 안정성 1위(0.81).
LLM_MODEL = "gemini-3.6-flash"   # 환경변수 GEMINI_MODEL로 덮어쓸 수 있다 (ensure_runtime_env에서 반영)
STT_MODEL = os.environ.get("SHORTS_STT_MODEL", "large-v3")
# 음성 인식 엔진. whisper 는 GPU 가 있어야 제 속도가 나고 CPU 로 떨어지면
# 메모리 1.7GB 를 상주로 먹는다 (20분 영상 피크 5GB+). GPU 가 없는 환경에서는
# gemini 로 돌린다 — 실측 품질이 동등하고 메모리를 쓰지 않는다.
#   로보락 20분 실측: 키워드 적중 둘 다 100%, 오인식 0건,
#                     큐 340(whisper) vs 307(gemini), 299초 vs 40초
# 기본값이 gemini 인 이유: 배포 환경에 GPU 가 없다. whisper 로 두면 CPU 로
# 떨어져 파드가 OOM 으로 재시작된다. GPU 노드가 붙으면 whisper 로 되돌린다.
STT_ENGINE = os.environ.get("SHORTS_STT_ENGINE", "gemini").strip().lower()
# gemini 엔진이 쓸 모델. STT_MODEL(large-v3 등)은 whisper 전용이라 따로 둔다.
STT_GEMINI_MODEL = os.environ.get("SHORTS_STT_GEMINI_MODEL", "gemini-3.7-flash")

# ── 소재 적합성 판정 임계값 ───────────────────────────────────────────
# 실측: 분당 컷 2.1회 영상은 쇼츠에서 화면이 멈춘 것처럼 보였다.
#       분당 45~47회 영상은 정상. 안전 계수를 두어 10으로 잡는다.
MIN_CUTS_PER_MIN = float(os.environ.get("SHORTS_MIN_CUTS_PER_MIN", "10"))

# ── P2 (질문 집중 구간) ───────────────────────────────────────────────
P2_WINDOW_MS = 60_000
P2_STEP_MS = 10_000
P2_MIN_COMMENTS = int(os.environ.get("SHORTS_P2_MIN_COMMENTS", "8"))

# ── 렌더링 기본값 ─────────────────────────────────────────────────────
DEFAULT_LAYOUT = "letterbox"   # 원본 비율 유지 + 상하 여백 (잘림 없음)
DEFAULT_BG_BLUR = True

# ── 동시 실행 ─────────────────────────────────────────────────────────
# STT가 GPU를 점유하므로 무제한 병렬은 위험하다.
MAX_CONCURRENT_JOBS = int(os.environ.get("SHORTS_MAX_JOBS", "2"))


def _load_dotenv() -> None:
    """.env를 환경변수로 올린다.

    poc 패키지가 임포트 시점에 하지만, API는 poc를 늦게(작업 실행 시) 임포트한다.
    헬스체크가 키 유무를 먼저 물어보므로 서버 기동 시점에 직접 로드한다."""
    from poc.env import load_dotenv

    load_dotenv()


def ensure_runtime_env() -> None:
    """모듈이 CWD 상대경로에 쓰지 않도록 절대경로를 주입한다.

    poc/llm.py는 사용량 로그를 기본 'out/llm_usage.jsonl'(CWD 상대)에 남긴다.
    uvicorn을 레포 루트가 아닌 곳에서 띄우면 엉뚱한 위치에 out/이 생긴다."""
    # poc 모듈들이 한글·기호를 print한다. Windows 콘솔(cp949)에서 죽지 않게 UTF-8로 고정.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    _load_dotenv()
    global LLM_MODEL, STT_MODEL, STT_ENGINE, STT_GEMINI_MODEL
    LLM_MODEL = os.environ.get("GEMINI_MODEL", LLM_MODEL)
    STT_MODEL = os.environ.get("SHORTS_STT_MODEL", STT_MODEL)
    STT_ENGINE = os.environ.get("SHORTS_STT_ENGINE", STT_ENGINE).strip().lower()
    STT_GEMINI_MODEL = os.environ.get("SHORTS_STT_GEMINI_MODEL", STT_GEMINI_MODEL)
    from api import logging_config
    logging_config.setup()

    # 컨테이너에서 /app 이 읽기전용일 수 있으므로 기본값을 작업 폴더 아래로 둔다.
    os.environ.setdefault("LLM_USAGE_LOG", str(WORKSPACE / "llm_usage.jsonl"))
    Path(os.environ["LLM_USAGE_LOG"]).parent.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)

# 콜백으로 보내는 파일 URL의 호스트. BE 가 바로 재생할 수 있어야 한다.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")

# 작업 산출물 보관 시간. 0이면 보관 정책 미적용(무기한).
# 영상 원본이 방송당 수백 MB라 기본 7일로 둔다.
RETENTION_HOURS = int(os.environ.get("SHORTS_RETENTION_HOURS", "168"))
