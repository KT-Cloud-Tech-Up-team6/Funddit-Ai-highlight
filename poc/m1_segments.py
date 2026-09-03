"""M1 — 구간 분할 실행 + 코드 게이트."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from poc.gates import validate_segments
from poc.llm import LLM
from poc.models import Cue, Segment, Violation
from poc.prompts import M1_PROMPT


def run_m1(
    cues: list[Cue],
    cuesheet: list[dict],
    llm: LLM,
    out_path: str | Path | None = None,
) -> tuple[list[Segment], list[Violation]]:
    payload = {
        "transcript": [
            {"cue_id": c.cue_id, "start_ms": c.start_ms, "text": c.text} for c in cues
        ],
        "cuesheet": cuesheet,
    }
    raw = llm.generate_json(M1_PROMPT, payload)

    segments: list[Segment] = []
    for s in raw.get("segments", []):
        segments.append(
            Segment(
                part_type=s.get("part_type", ""),
                start_cue_id=s.get("start_cue_id", ""),
                end_cue_id=s.get("end_cue_id", ""),
                label=s.get("label", ""),
                evidence=s.get("evidence", []),
            )
        )

    violations = validate_segments(segments, cues)

    # 시각은 코드가 cue_id로 조회해 채운다
    idx = {c.cue_id: c for c in cues}
    for s in segments:
        if s.start_cue_id in idx and s.end_cue_id in idx:
            s.start_ms = idx[s.start_cue_id].start_ms
            s.end_ms = idx[s.end_cue_id].end_ms

    if out_path:
        result = {
            "segments": [asdict(s) for s in segments],
            "violations": [asdict(v) for v in violations],
        }
        Path(out_path).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return segments, violations


def load_segments(path: str | Path) -> list[Segment]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Segment(**s) for s in data["segments"]]
