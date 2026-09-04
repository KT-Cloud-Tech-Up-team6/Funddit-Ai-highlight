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
    global LLM_MODEL, STT_MODEL
    LLM_MODEL = os.environ.get("GEMINI_MODEL", LLM_MODEL)
    STT_MODEL = os.environ.get("SHORTS_STT_MODEL", STT_MODEL)
    os.environ.setdefault("LLM_USAGE_LOG", str(ROOT / "out" / "llm_usage.jsonl"))
    (ROOT / "out").mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
