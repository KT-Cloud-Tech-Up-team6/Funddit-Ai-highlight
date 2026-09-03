"""파이프라인 공용 데이터 모델."""
from __future__ import annotations

from dataclasses import dataclass, field

PART_TYPES = {
    "P1": "시연",
    "P2": "질문 집중",
    "P3": "펀딩 홍보",
    "P4": "제품 스펙",
    "P5": "제작 배경",
}
# P2는 댓글 수 세기(코드)로 판별하므로 모델이 반환하면 안 된다
MODEL_PART_TYPES = {"P1", "P3", "P4", "P5"}

EMPHASIS_TYPES = {"spec", "feature", "benefit", "result", "story"}


@dataclass
class Cue:
    cue_id: str
    start_ms: int
    end_ms: int
    text: str


@dataclass
class Segment:
    part_type: str
    start_cue_id: str
    end_cue_id: str
    label: str
    evidence: list[str] = field(default_factory=list)
    # 시각(ms)은 모델이 만들지 않는다 — 코드가 cue_id로 조회해서 채운다
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass
class Caption:
    text: str
    source_cue_id: str
    emphasis: str
    source_text: str
    highlight: str = ""  # text 안에서 색으로 강조할 부분(숫자·핵심어). text의 부분 문자열이어야 한다 (코드가 검증)
    # 쇼츠 로컬 타임라인 기준 표시 시각 — 코드가 계산한다 (모델 생성 금지)
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass
class Violation:
    level: str  # "ERROR" | "WARN"
    code: str
    message: str
