"""코드 검증 게이트 — 기계적으로 확인 가능한 체크리스트 항목은 사람 눈이 아니라 여기서 걸러낸다.

ERROR: 구조적 오류·환각 (해당 결과물 탈락/재시도 대상)
WARN : 품질 규칙 위반 (사람 판정 참고용)
"""
from __future__ import annotations

import re

from poc.models import (
    Caption,
    Cue,
    EMPHASIS_TYPES,
    MODEL_PART_TYPES,
    Segment,
    Violation,
)
from poc.numbers import extract_numbers

SEG_MIN_MS = 60_000
SEG_MAX_MS = 120_000
SEG_MAX_GAP_MS = 10_000  # 구간 내 허용 무발화 갭
# 구간이 '살아있는가'는 poc.motion.liveliness가 판정한다 (장면 전환 빈도 또는 장면 안 움직임)
MAX_SEGMENTS = 6
MAX_LABEL_LEN = 12
MAX_CAPTION_LEN = 12
CAPTIONS_MIN = 3
CAPTIONS_MAX = 6


def _core_len(s: str) -> int:
    """12자 규칙용 글자 수 — 공백·쉼표·마침표 등은 세지 않는다."""
    return len(re.sub(r"[\s,.·!?%~\-()]", "", s))


def validate_segments(segments: list[Segment], cues: list[Cue],
                      motion: list[float] | None = None,
                      cuts: list[int] | None = None) -> list[Violation]:
    """motion/cuts: poc.motion의 초당 움직임·장면 전환. 주면 화면이 멈춘 구간(SEG_STILL)을 판정한다."""
    v: list[Violation] = []
    idx = {c.cue_id: c for c in cues}
    order = {c.cue_id: i for i, c in enumerate(cues)}
    transcript_text = "\n".join(c.text for c in cues)

    if len(segments) > MAX_SEGMENTS:
        v.append(Violation("WARN", "SEG_COUNT", f"구간이 {len(segments)}개 — 최대 {MAX_SEGMENTS}개 규칙 위반"))

    seen_types: set[str] = set()
    timed: list[tuple[int, int, str]] = []

    for i, s in enumerate(segments):
        ref = f"segments[{i}]({s.part_type}/{s.label})"
        if s.part_type not in MODEL_PART_TYPES:
            v.append(Violation("ERROR", "SEG_PART_TYPE", f"{ref}: 허용되지 않은 파트 유형 (P2는 코드 판별)"))
            continue
        if s.part_type in seen_types:
            v.append(Violation("WARN", "SEG_DUP_TYPE", f"{ref}: 같은 파트 유형 중복 (규칙 5)"))
        seen_types.add(s.part_type)

        if s.start_cue_id not in idx or s.end_cue_id not in idx:
            v.append(Violation("ERROR", "SEG_CUE_MISSING", f"{ref}: 존재하지 않는 cue_id"))
            continue
        if order[s.start_cue_id] > order[s.end_cue_id]:
            v.append(Violation("ERROR", "SEG_CUE_ORDER", f"{ref}: 시작 큐가 종료 큐보다 뒤에 있음"))
            continue

        start_ms = idx[s.start_cue_id].start_ms
        end_ms = idx[s.end_cue_id].end_ms
        dur = end_ms - start_ms
        timed.append((start_ms, end_ms, ref))
        if not (SEG_MIN_MS <= dur <= SEG_MAX_MS):
            v.append(Violation("WARN", "SEG_LENGTH", f"{ref}: 길이 {dur/1000:.0f}초 — 60~120초 범위 밖"))

        if _core_len(s.label) > MAX_LABEL_LEN:
            v.append(Violation("WARN", "SEG_LABEL_LEN", f"{ref}: 라벨이 {MAX_LABEL_LEN}자 초과"))

        # 화면이 거의 안 바뀌면 "말만 흐르고 그림은 멈춘" 쇼츠가 된다.
        # 장면 전환이 잦거나 장면 안에서 피사체가 움직이면 통과 — 둘 다 아니면 경고.
        if motion:
            from poc.motion import liveliness

            live = liveliness(motion, cuts or [], start_ms, end_ms)
            if not live["ok"]:
                v.append(Violation("WARN", "SEG_STILL",
                                   f"{ref}: 화면 변화가 적음 (분당 컷 {live['cuts_per_min']}회, "
                                   f"움직임 {live['moving_ratio']*100:.0f}%)"))

        # 구간 안에 긴 무발화 갭(음악·화면 전환)이 끼면 쇼츠에 빈 구간이 생긴다 — 실제 방송에서 34초 갭 발견
        i0, i1 = order[s.start_cue_id], order[s.end_cue_id]
        for a, b in zip(cues[i0:i1], cues[i0 + 1 : i1 + 1]):
            gap = b.start_ms - a.end_ms
            if gap >= SEG_MAX_GAP_MS:
                v.append(Violation("WARN", "SEG_GAP", f"{ref}: {a.cue_id}→{b.cue_id} 사이 무발화 {gap/1000:.0f}초"))

        if not s.evidence:
            v.append(Violation("ERROR", "SEG_NO_EVIDENCE", f"{ref}: evidence 없음"))
        for ev in s.evidence:
            if ev.strip() not in transcript_text:
                v.append(Violation("ERROR", "SEG_EVIDENCE_FAKE", f"{ref}: 자막에 없는 evidence — {ev!r}"))

    timed.sort()
    for (s1, e1, r1), (s2, e2, r2) in zip(timed, timed[1:]):
        if s2 < e1:
            v.append(Violation("ERROR", "SEG_OVERLAP", f"구간 겹침: {r1} ↔ {r2}"))
    return v


def validate_captions(
    segment: Segment,
    captions: list[Caption],
    cues: list[Cue],
    terms: dict,
) -> list[Violation]:
    v: list[Violation] = []
    idx = {c.cue_id: c for c in cues}
    order = {c.cue_id: i for i, c in enumerate(cues)}
    lo, hi = order[segment.start_cue_id], order[segment.end_cue_id]

    # 상품 용어 목록의 숫자값은 자막에 써도 되는 값으로 인정
    terms_numbers: set[float] = set()
    for spec in terms.get("specs", []):
        terms_numbers |= extract_numbers(str(spec.get("value", "")))

    if not (CAPTIONS_MIN <= len(captions) <= CAPTIONS_MAX):
        v.append(Violation("WARN", "CAP_COUNT", f"자막 {len(captions)}개 — 구간당 {CAPTIONS_MIN}~{CAPTIONS_MAX}개 규칙 위반"))

    for i, c in enumerate(captions):
        ref = f"captions[{i}]({c.text})"
        cue = idx.get(c.source_cue_id)
        if cue is None:
            v.append(Violation("ERROR", "CAP_CUE_MISSING", f"{ref}: 존재하지 않는 source_cue_id"))
            continue
        if not (lo <= order[c.source_cue_id] <= hi):
            v.append(Violation("ERROR", "CAP_CUE_OUT_OF_SEGMENT", f"{ref}: source_cue_id가 선택 구간 밖"))
        if c.source_text.strip() not in cue.text:
            v.append(Violation("ERROR", "CAP_SOURCE_FAKE", f"{ref}: source_text가 해당 큐 원문과 다름"))

        if _core_len(c.text) > MAX_CAPTION_LEN:
            v.append(Violation("WARN", "CAP_TEXT_LEN", f"{ref}: 문구가 {MAX_CAPTION_LEN}자 초과"))
        if c.emphasis not in EMPHASIS_TYPES:
            v.append(Violation("WARN", "CAP_EMPHASIS", f"{ref}: 알 수 없는 emphasis {c.emphasis!r}"))

        # 숫자 환각 게이트 — 자막의 모든 숫자는 근거 발화 또는 상품 용어 목록에 있어야 한다
        need = extract_numbers(c.text)
        allowed = extract_numbers(cue.text) | terms_numbers
        missing = need - allowed
        if missing:
            v.append(Violation("ERROR", "CAP_NUMBER_FAKE", f"{ref}: 근거 없는 숫자 {sorted(missing)}"))
    return v


def has_errors(violations: list[Violation]) -> bool:
    return any(x.level == "ERROR" for x in violations)


def format_report(violations: list[Violation]) -> str:
    if not violations:
        return "게이트 통과: 위반 없음"
    lines = [f"[{x.level}] {x.code}: {x.message}" for x in violations]
    return "\n".join(lines)
