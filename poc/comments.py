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


def find_hot_windows(
    comments: list[dict],
    window_ms: int = 60_000,
    step_ms: int = 10_000,
    top_n: int = 5,
    kinds: tuple[str, ...] = ("chat", "like", "purchase"),
    min_ratio: float = 0.3,
) -> list[dict]:
    """채팅이 가장 활발했던 구간을 상위 N개 반환한다.

    find_p2_windows 와 다른 점:
      - P2는 "질문이 몰린 곳"을 임계값(min_count)으로 찾는다 → 쇼츠 후보용
      - 여기는 "반응이 가장 뜨거운 곳"을 상대 순위로 찾는다 → 타임라인 표시용

    임계값이 아니라 순위를 쓰는 이유:
      방송마다 시청자 수가 달라 절대 건수 기준이 통하지 않는다.
      100명 방송의 20건과 10,000명 방송의 20건은 의미가 다르다.

    kinds 로 세는 이벤트를 고른다. 좋아요·구매 알림이 섞여 오는
    플랫폼에서는 그것까지 포함해야 실제 반응 강도가 잡힌다.

    min_ratio 는 최고 구간 대비 하한이다. top_n 을 채우려고
    1~2건짜리 한산한 구간까지 "활발"로 뽑는 것을 막는다.
    """
    if not comments:
        return []

    ts = sorted(
        c["ts_ms"] for c in comments
        if c.get("kind", "chat") in kinds
    )
    if not ts:
        return []

    # 각 창의 건수를 구한 뒤 순위로 자른다.
    scored: list[tuple[int, int, int]] = []
    t = 0
    while t <= ts[-1]:
        n = sum(1 for x in ts if t <= x < t + window_ms)
        if n > 0:
            scored.append((t, t + window_ms, n))
        t += step_ms

    if not scored:
        return []

    # 최고 구간 대비 min_ratio 미만은 애초에 후보에서 뺀다.
    peak_all = max(n for _, _, n in scored)
    floor = peak_all * min_ratio

    # 건수 내림차순으로 보면서, 이미 고른 구간과 겹치면 건너뛴다.
    # (같은 봉우리에서 창이 여러 개 잡히는 것을 막는다)
    picked: list[tuple[int, int, int]] = []
    for s, e, n in sorted(scored, key=lambda x: -x[2]):
        if n < floor:
            break
        if any(s < pe and e > ps for ps, pe, _ in picked):
            continue
        picked.append((s, e, n))
        if len(picked) >= top_n:
            break

    if not picked:
        return []

    picked.sort(key=lambda x: x[0])

    peak = max(n for _, _, n in picked)
    return [
        {
            "start_ms": s,
            "end_ms": e,
            "comment_count": n,
            # 가장 뜨거운 구간 대비 상대 강도 (0~1)
            "intensity": round(n / peak, 3),
        }
        for s, e, n in picked
    ]
