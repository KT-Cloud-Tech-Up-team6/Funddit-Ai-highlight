"""STT 결과(transcript JSON) 로드·조회·문장 단위 재병합."""
from __future__ import annotations

import json
from pathlib import Path

from poc.models import Cue

SENTENCE_END = ("다.", "요.", "죠.", "까?", "요?", "죠?", ".", "?", "!")


def load_cues(path: str | Path) -> list[Cue]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Cue(**c) for c in data["transcript"]]


def save_cues(cues: list[Cue], path: str | Path, meta: dict | None = None) -> None:
    data = {"transcript": [c.__dict__ for c in cues]}
    if meta:
        data["meta"] = meta
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cue_index(cues: list[Cue]) -> dict[str, Cue]:
    return {c.cue_id: c for c in cues}


def full_text(cues: list[Cue]) -> str:
    return "\n".join(c.text for c in cues)


def cues_in_range(cues: list[Cue], start_cue_id: str, end_cue_id: str) -> list[Cue]:
    ids = [c.cue_id for c in cues]
    i, j = ids.index(start_cue_id), ids.index(end_cue_id)
    return cues[i : j + 1]


# 원본 세그먼트 사이 무음이 이보다 길면 문장이 안 끝났어도 큐를 나눈다
GAP_SPLIT_MS = 1_200
# 문장부호가 없어도 이 길이를 넘으면 끊는다 (진행자가 이어 말하는 방송 대응)
SOFT_MAX_CHARS = 90
# 큐 끝에 붙는 무음 꼬리를 이만큼만 남기고 잘라낸다
TAIL_PAD_MS = 400


def merge_to_sentences(cues: list[Cue], max_ms: int = 15_000) -> list[Cue]:
    """Whisper 세그먼트는 문장 경계와 안 맞는 경우가 많다.
    M1 규칙("문장이 시작되는 큐에서 시작한다")이 성립하려면 문장 단위 재병합이 필요하다.

    끊는 조건 (하나라도 만족하면):
      - 문장이 끝났다 (문장부호)
      - 다음 세그먼트까지 무음이 GAP_SPLIT_MS 이상 — 말이 실제로 끊긴 지점
      - 누적 길이가 max_ms 이상, 또는 누적 글자수가 SOFT_MAX_CHARS 이상
    """
    merged: list[Cue] = []
    buf: Cue | None = None
    speech_end = 0  # 버퍼에 담긴 마지막 '발화'의 끝 (무음 꼬리 제외)

    for i, c in enumerate(cues):
        if buf is None:
            buf = Cue(c.cue_id, c.start_ms, c.end_ms, c.text.strip())
        else:
            buf.text = (buf.text + " " + c.text.strip()).strip()
            buf.end_ms = c.end_ms
        speech_end = c.end_ms

        nxt = cues[i + 1] if i + 1 < len(cues) else None
        gap_after = (nxt.start_ms - c.end_ms) if nxt else 0

        if (
            buf.text.endswith(SENTENCE_END)
            or gap_after >= GAP_SPLIT_MS
            or (buf.end_ms - buf.start_ms) >= max_ms
            or len(buf.text) >= SOFT_MAX_CHARS
        ):
            buf.end_ms = min(buf.end_ms, speech_end + TAIL_PAD_MS)
            merged.append(buf)
            buf = None

    if buf:
        buf.end_ms = min(buf.end_ms, speech_end + TAIL_PAD_MS)
        merged.append(buf)

    for i, c in enumerate(merged, 1):
        c.cue_id = f"t_{i:03d}"
    return merged
