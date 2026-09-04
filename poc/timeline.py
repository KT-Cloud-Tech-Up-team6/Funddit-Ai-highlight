"""타임라인 — 방송 다시보기에 주요 장면 구분 스탬프를 남긴다.

하이라이트 쇼츠와 다른 점:
  쇼츠 후보는 "잘라서 쓸 만한 구간"만 고른다 (실측 20분 방송에서 33%만 커버).
  타임라인은 방송 전체를 빈틈없이 나눈다 — 시청자가 원하는 지점으로 바로 이동해야 하므로
  공백이 있으면 안 된다.

  쇼츠:    [   ] ...공백... [  ] [ ] ...공백...     골라낸 구간만
  타임라인: [][][][][][][][][][][][][][][][][][]    전 구간 분할

  python -m poc.timeline --transcript out/real/rb2_transcript_large.json --out out/timeline.json
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from poc.llm import LLM
from poc.models import Cue
from poc.prompts import TIMELINE_PROMPT
from poc.transcript import load_cues

# 타임라인 챕터 길이 기준 — 너무 짧으면 목록이 길어지고, 너무 길면 이동 의미가 없다
MIN_CHAPTER_MS = 30_000
MAX_CHAPTER_MS = 300_000
# 이 이상 공백이 남으면 채운다
MAX_GAP_MS = 20_000


@dataclass
class Chapter:
    """다시보기 타임라인의 한 구간."""
    start_ms: int
    end_ms: int
    title: str                 # 시청자에게 보이는 이름 (예: "물걸레 세척 기능 설명")
    category: str              # 기능설명 / 펀딩정보 / 시연 / 질문응답 / 도입 / 마무리
    start_cue_id: str = ""
    end_cue_id: str = ""
    summary: str = ""          # 한 줄 요약 (선택)


CATEGORIES = {
    "intro": "도입",
    "feature": "기능 설명",
    "demo": "시연",
    "spec": "제품 스펙",
    "funding": "펀딩 정보",
    "qna": "질문 응답",
    "story": "제작 배경",
    "closing": "마무리",
}


def timestamp(ms: int) -> str:
    """00:00 또는 0:00:00 형식."""
    s = ms // 1000
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def run_timeline(
    cues: list[Cue],
    llm: LLM,
    out_path: str | Path | None = None,
    comments: list[dict] | None = None,
) -> tuple[list[Chapter], list[str]]:
    """방송 전체를 챕터로 나눈다. 반환: (챕터 목록, 경고 목록)."""
    payload = {
        "transcript": [
            {"cue_id": c.cue_id, "start_ms": c.start_ms, "text": c.text} for c in cues
        ],
        "categories": CATEGORIES,
    }
    if comments:
        from poc.comments import find_p2_windows

        payload["question_windows_ms"] = find_p2_windows(comments)

    raw = llm.generate_json(TIMELINE_PROMPT, payload)
    idx = {c.cue_id: c for c in cues}

    chapters: list[Chapter] = []
    for ch in raw.get("chapters", []):
        sc, ec = ch.get("start_cue_id", ""), ch.get("end_cue_id", "")
        if sc not in idx or ec not in idx:
            continue  # 없는 큐를 가리키면 버린다 (환각 차단)
        chapters.append(Chapter(
            start_ms=idx[sc].start_ms,
            end_ms=idx[ec].end_ms,
            title=str(ch.get("title", ""))[:30],
            category=ch.get("category", "feature"),
            start_cue_id=sc, end_cue_id=ec,
            summary=str(ch.get("summary", ""))[:80],
        ))

    chapters.sort(key=lambda c: c.start_ms)
    chapters, warnings = _repair(chapters, cues)

    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps({
            "chapters": [{**asdict(c), "timestamp": timestamp(c.start_ms),
                          "category_name": CATEGORIES.get(c.category, c.category)}
                         for c in chapters],
            "warnings": warnings,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    return chapters, warnings


def _repair(chapters: list[Chapter], cues: list[Cue]) -> tuple[list[Chapter], list[str]]:
    """겹침·공백을 코드가 정리한다. 타임라인은 빈틈이 없어야 한다."""
    warnings: list[str] = []
    if not chapters:
        return [], ["챕터가 하나도 생성되지 않았습니다"]

    # 겹침 해소 — 앞 챕터를 뒤 챕터 시작에 맞춰 자른다
    for a, b in zip(chapters, chapters[1:]):
        if a.end_ms > b.start_ms:
            warnings.append(f"겹침 정리: {timestamp(a.start_ms)} 챕터를 {timestamp(b.start_ms)}에서 끊음")
            a.end_ms = b.start_ms

    first_ms, last_ms = cues[0].start_ms, cues[-1].end_ms

    # 앞뒤 여백 — 방송 시작·끝까지 덮는다
    if chapters[0].start_ms - first_ms > MAX_GAP_MS:
        warnings.append(f"도입부 {timestamp(first_ms)}~{timestamp(chapters[0].start_ms)} 공백을 채움")
    chapters[0].start_ms = first_ms
    if last_ms - chapters[-1].end_ms > MAX_GAP_MS:
        warnings.append(f"마무리 {timestamp(chapters[-1].end_ms)}~{timestamp(last_ms)} 공백을 채움")
    chapters[-1].end_ms = last_ms

    # 중간 공백 — 앞 챕터를 늘려 메운다
    for a, b in zip(chapters, chapters[1:]):
        gap = b.start_ms - a.end_ms
        if gap > MAX_GAP_MS:
            warnings.append(f"공백 {timestamp(a.end_ms)}~{timestamp(b.start_ms)} ({gap//1000}초)을 앞 챕터로 채움")
        if gap > 0:
            a.end_ms = b.start_ms

    # 너무 짧은 챕터는 앞 챕터에 병합
    merged: list[Chapter] = []
    for c in chapters:
        if merged and (c.end_ms - c.start_ms) < MIN_CHAPTER_MS:
            warnings.append(f"{timestamp(c.start_ms)} '{c.title}' 이 30초 미만이라 앞 챕터에 병합")
            merged[-1].end_ms = c.end_ms
        else:
            merged.append(c)
    return merged, warnings


def validate(chapters: list[Chapter], cues: list[Cue]) -> list[str]:
    """타임라인 품질 검사 — 게이트."""
    issues: list[str] = []
    if not chapters:
        return ["챕터 없음"]
    for i, c in enumerate(chapters):
        d = c.end_ms - c.start_ms
        if d > MAX_CHAPTER_MS:
            issues.append(f"[{i}] {timestamp(c.start_ms)} '{c.title}' {d//1000}초 — 5분 초과")
        if not c.title.strip():
            issues.append(f"[{i}] {timestamp(c.start_ms)} 제목 없음")
        if c.category not in CATEGORIES:
            issues.append(f"[{i}] 알 수 없는 분류 '{c.category}'")
    for a, b in zip(chapters, chapters[1:]):
        if a.end_ms != b.start_ms:
            issues.append(f"{timestamp(a.end_ms)}에서 타임라인이 끊김")
    coverage = (chapters[-1].end_ms - chapters[0].start_ms) / max(cues[-1].end_ms - cues[0].start_ms, 1)
    if coverage < 0.95:
        issues.append(f"방송 커버리지 {coverage*100:.0f}% — 타임라인은 전 구간을 덮어야 함")
    return issues


def main() -> None:
    ap = argparse.ArgumentParser(prog="poc.timeline")
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--comments")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model")
    args = ap.parse_args()

    cues = load_cues(args.transcript)
    comments = None
    if args.comments:
        from poc.comments import load_comments
        comments = load_comments(args.comments)

    llm = LLM(model=args.model, tag="timeline")
    chapters, warnings = run_timeline(cues, llm, out_path=args.out, comments=comments)

    print(f"타임라인 {len(chapters)}개 챕터 → {args.out}")
    for c in chapters:
        print(f"  {timestamp(c.start_ms)}  [{CATEGORIES.get(c.category, c.category)}] {c.title}")
    if warnings:
        print("\n코드가 정리한 항목:")
        for w in warnings:
            print(f"  - {w}")
    issues = validate(chapters, cues)
    if issues:
        print("\n검사 결과:")
        for i in issues:
            print(f"  ! {i}")
    else:
        print("\n검사 통과: 전 구간 연속, 제목·분류 정상")


if __name__ == "__main__":
    main()
