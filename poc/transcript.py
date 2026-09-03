"""STT 결과(transcript JSON) 로드·조회·문장 단위 재병합."""
from __future__ import annotations

import json
from pathlib import Path

from poc.models import Cue

SENTENCE_END = ("다.", "요.", "죠.", "까?", "요?", "죠?", ".", "?", "!")


def load_cues(path: str | Path) -> list[Cue]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Cue(**c) for c in data["transcript"]]


def save_cues(cues: list[Cue], path: str | Path) -> None:
    data = {"transcript": [c.__dict__ for c in cues]}
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cue_index(cues: list[Cue]) -> dict[str, Cue]:
    return {c.cue_id: c for c in cues}


def full_text(cues: list[Cue]) -> str:
    return "\n".join(c.text for c in cues)


def cues_in_range(cues: list[Cue], start_cue_id: str, end_cue_id: str) -> list[Cue]:
    ids = [c.cue_id for c in cues]
    i, j = ids.index(start_cue_id), ids.index(end_cue_id)
    return cues[i : j + 1]


def merge_to_sentences(cues: list[Cue], max_ms: int = 15_000) -> list[Cue]:
    """Whisper 세그먼트는 문장 경계와 안 맞는 경우가 많다.
    M1 규칙("문장이 시작되는 큐에서 시작한다")이 성립하려면 문장 단위 재병합이 필요하다."""
    merged: list[Cue] = []
    buf: Cue | None = None
    for c in cues:
        if buf is None:
            buf = Cue(c.cue_id, c.start_ms, c.end_ms, c.text.strip())
        else:
            buf.text = (buf.text + " " + c.text.strip()).strip()
            buf.end_ms = c.end_ms
        if buf.text.endswith(SENTENCE_END) or (buf.end_ms - buf.start_ms) >= max_ms:
            merged.append(buf)
            buf = None
    if buf:
        merged.append(buf)
    for i, c in enumerate(merged, 1):
        c.cue_id = f"t_{i:03d}"
    return merged
