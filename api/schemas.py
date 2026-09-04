"""API 요청·응답 모델."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from api.settings import DEFAULT_BG_BLUR, DEFAULT_LAYOUT


class JobStatus(str, Enum):
    QUEUED = "queued"                      # 접수됨, 처리 대기
    SCREENING = "screening"                # 소재 적합성 판정 중
    REJECTED = "rejected"                  # 소재 부적합 (컷이 너무 적음)
    TRANSCRIBING = "transcribing"          # 음성 인식 중 (가장 오래 걸림)
    SEGMENTING = "segmenting"              # 구간 분할 중
    TIMELINE = "timeline"                  # 다시보기 타임라인 생성 중
    READY_FOR_SELECTION = "ready_for_selection"   # 후보 준비 완료 — 판매자 선택 대기
    RENDERING = "rendering"                # 선택된 구간 자막·렌더링 중
    DONE = "done"                          # 쇼츠 완성
    FAILED = "failed"


class Violation(BaseModel):
    level: str
    code: str
    message: str


class ScreenResult(BaseModel):
    """소재 적합성 판정 결과."""
    suitable: bool
    duration_sec: int
    total_cuts: int
    cuts_per_min: float
    moving_sec: int
    still_pct: float
    threshold: float
    reason: str | None = None


class JobCreated(BaseModel):
    job_id: str
    status: JobStatus
    message: str


class JobState(BaseModel):
    job_id: str
    status: JobStatus
    stage_detail: str | None = None
    progress: float = Field(0.0, ge=0.0, le=1.0)
    error: str | None = None
    screen: ScreenResult | None = None
    candidate_count: int | None = None
    chapter_count: int | None = None
    short_count: int | None = None
    elapsed_sec: float | None = None
    stt_reused: bool = False


class Chapter(BaseModel):
    """다시보기 타임라인의 한 구간."""
    start_ms: int
    end_ms: int
    timestamp: str                # "02:34" 형식 — 화면에 그대로 표시
    duration_sec: float
    title: str
    category: str                 # intro / feature / demo / spec / funding / qna / story / closing
    category_name: str            # 한글 표시명
    summary: str = ""


class TimelineOut(BaseModel):
    job_id: str
    chapters: list[Chapter]
    warnings: list[str] = []      # 코드가 정리한 항목 (겹침·공백 등)


class Candidate(BaseModel):
    """판매자 선택 화면에 뿌릴 구간 후보 카드."""
    id: str
    part_type: str
    part_name: str
    label: str
    start_ms: int
    end_ms: int
    duration_sec: float
    source: str                       # "model" | "comments"
    thumbnail_url: str | None = None
    evidence: list[str] = []
    comment_count: int | None = None  # P2 후보만
    warnings: list[Violation] = []


class CandidateList(BaseModel):
    job_id: str
    candidates: list[Candidate]


class SelectRequest(BaseModel):
    candidate_ids: list[str] = Field(..., min_length=1)
    layout: str = DEFAULT_LAYOUT      # "letterbox" | "crop"
    bg_blur: bool = DEFAULT_BG_BLUR
    crop_cx: float = Field(0.5, ge=0.0, le=1.0)


class CaptionOut(BaseModel):
    text: str
    highlight: str = ""
    emphasis: str
    start_ms: int
    end_ms: int


class ShortOut(BaseModel):
    candidate_id: str
    part_type: str
    title: str
    duration_sec: float
    size_bytes: int
    video_url: str
    thumbnail_url: str | None = None
    captions: list[CaptionOut]
    violations: list[Violation] = []


class ShortList(BaseModel):
    job_id: str
    shorts: list[ShortOut]


class HealthCheck(BaseModel):
    ok: bool
    ffmpeg: bool
    whisper: bool
    gemini_key: bool
    llm_model: str
    stt_model: str
    gpu: str | None = None
    active_jobs: int
    notes: list[str] = []
