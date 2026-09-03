"""P2(질문 집중 파트) 판별 — 모델이 아니라 댓글 수 세기(코드)로 처리한다."""
from __future__ import annotations

import json
from pathlib import Path


def load_comments(path: str | Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["comments"]


def find_p2_windows(
    comments: list[dict],
    window_ms: int = 60_000,
    step_ms: int = 10_000,
    min_count: int = 8,
) -> list[dict]:
    """슬라이딩 윈도로 댓글이 몰린 구간을 찾고, 겹치는 창은 병합한다."""
    if not comments:
        return []
    ts = sorted(c["ts_ms"] for c in comments)
    hits: list[list[int]] = []
    t = 0
    while t <= ts[-1]:
        n = sum(1 for x in ts if t <= x < t + window_ms)
        if n >= min_count:
            if hits and t <= hits[-1][1]:
                hits[-1][1] = max(hits[-1][1], t + window_ms)
                hits[-1][2] = max(hits[-1][2], n)
            else:
                hits.append([t, t + window_ms, n])
        t += step_ms
    return [
        {"part_type": "P2", "start_ms": s, "end_ms": e, "comment_count": n}
        for s, e, n in hits
    ]
