"""M2 — 포인트 자막 생성 + 코드 게이트 + 표시 타이밍 계산.

모델은 source_cue_id만 지정한다. 표시 시각(쇼츠 로컬 기준)·길이·겹침 해소는
전부 여기(코드)서 처리한다 — 시각 환각을 구조적으로 차단.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from poc.gates import validate_captions
from poc.llm import LLM
from poc.models import Caption, Cue, Segment, Violation
from poc.prompts import M2_PROMPT
from poc.transcript import cues_in_range

MIN_SHOW_MS = 1_500
MAX_SHOW_MS = 4_000


def assign_timing(captions: list[Caption], cues: list[Cue], seg_start_ms: int) -> list[Caption]:
    """표시 시각을 쇼츠 로컬 타임라인(구간 시작 = 0) 기준으로 계산한다."""
    idx = {c.cue_id: c for c in cues}
    caps = [c for c in captions if c.source_cue_id in idx]
    caps.sort(key=lambda c: idx[c.source_cue_id].start_ms)
    prev_end = -1
    for c in caps:
        cue = idx[c.source_cue_id]
        # 같은 큐에 자막이 여럿이면(긴 발화 하나에 포인트 2개) 앞 자막 뒤에 이어 붙인다 — 0.5초짜리 자막 방지
        c.start_ms = max(cue.start_ms - seg_start_ms, prev_end)
        show = max(MIN_SHOW_MS, min(MAX_SHOW_MS, cue.end_ms - cue.start_ms))
        c.end_ms = c.start_ms + show
        prev_end = c.end_ms
    # 다음 자막의 큐가 먼저 시작하면 앞 자막을 거기서 자른다 (최소 표시 시간은 보장)
    for a, b in zip(caps, caps[1:]):
        if a.end_ms > b.start_ms:
            a.end_ms = max(a.start_ms + MIN_SHOW_MS, b.start_ms)
            b.start_ms = max(b.start_ms, a.end_ms)
            b.end_ms = max(b.end_ms, b.start_ms + MIN_SHOW_MS)
    return caps


def run_m2(
    segment: Segment,
    all_cues: list[Cue],
    terms: dict,
    llm: LLM,
    out_path: str | Path | None = None,
) -> tuple[list[Caption], list[Violation]]:
    seg_cues = cues_in_range(all_cues, segment.start_cue_id, segment.end_cue_id)
    payload = {
        "part_type": segment.part_type,
        "label": segment.label,
        "transcript": [
            {"cue_id": c.cue_id, "text": c.text} for c in seg_cues
        ],
        "product_terms": terms,
    }
    raw = llm.generate_json(M2_PROMPT, payload)

    captions: list[Caption] = []
    for c in raw.get("captions", []):
        text = c.get("text", "")
        hl = c.get("highlight", "") or ""
        captions.append(
            Caption(
                text=text,
                source_cue_id=c.get("source_cue_id", ""),
                emphasis=c.get("emphasis", ""),
                source_text=c.get("source_text", ""),
                highlight=hl if hl and hl in text else "",  # text의 부분 문자열이 아니면 강조 무시
            )
        )
    # 상단 제목: 모델이 안 주면 상품명 + 구간 라벨로 대체
    title = (raw.get("title") or "").strip() or f"{terms.get('product_name', '')} {segment.label}".strip()

    violations = validate_captions(segment, captions, all_cues, terms)
    # ERROR가 붙은 자막(숫자 환각·근거 없음)은 렌더링에서 제외
    bad_idx = {
        int(v.message.split("[")[1].split("]")[0])
        for v in violations
        if v.level == "ERROR" and v.message.startswith("captions[")
    } if any(v.level == "ERROR" for v in violations) else set()
    kept = [c for i, c in enumerate(captions) if i not in bad_idx]

    kept = assign_timing(kept, all_cues, segment.start_ms)

    if out_path:
        result = {
            "segment": asdict(segment),
            "title": title,
            "captions": [asdict(c) for c in kept],
            "dropped": len(captions) - len(kept),
            "violations": [asdict(v) for v in violations],
        }
        Path(out_path).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return kept, violations


def load_captions(path: str | Path) -> tuple[Segment, list[Caption]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Segment(**data["segment"]), [Caption(**c) for c in data["captions"]]


def load_title(path: str | Path) -> str:
    return json.loads(Path(path).read_text(encoding="utf-8")).get("title", "")
