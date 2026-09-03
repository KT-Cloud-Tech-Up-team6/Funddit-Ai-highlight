"""STT 결과 비교 — 계획서 9번 "숫자를 틀리게 받아쓰는가" 판정 보조.

  python -m eval.compare_stt out/real/transcript_small.json out/real/transcript_large.json [...]

출력: 엔진별 메타(처리 시간·큐 수) / 숫자·가격 표현 토큰 목록 / 시간순 병렬 텍스트(30초 창).
정답 없이 사람이 눈으로 보는 용도 — 어느 쪽이 제품명·가격을 제대로 받아썼는지 대조.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path



WINDOW_MS = 30_000
UNIT = r"(?:원|%|도|cm|mm|분|시간|대|파스칼|Pa|kg|g|명|개|번|장|배)"
# 아라비아 숫자: 12.5cm / 20만 원 / 69만 / 925원 / 0%
NUM_RE = re.compile(r"[\d][\d,.]*\s*(?:(?:만|천|백)\s*" + UNIT + r"?|" + UNIT + r")")
# 한글 수사: "이만 파스칼", "삼십구만 구천 원". 한 글자짜리(이/만 ...)는 조사·어미와 헷갈리므로
#   숫자어+단위어 조합(이만, 삼십)이거나 단위(원/도/...)가 뒤따를 때만 잡는다.
DIG = "[일이삼사오육칠팔구]"
POW = "[십백천만억]"
KOR_NUM_RE = re.compile(
    rf"(?:{DIG}{POW}(?:{DIG}?{POW})*{DIG}?|{POW}{DIG})\s*" + UNIT + "?"
    rf"|(?:{DIG}|{POW})+\s*" + UNIT
)


def load(path: str):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return d.get("meta", {}), d["transcript"]


def number_tokens(cues: list[dict]) -> list[tuple[int, str]]:
    out = []
    for c in cues:
        spans = [(m.start(), m.end()) for m in NUM_RE.finditer(c["text"])]
        for m in KOR_NUM_RE.finditer(c["text"]):
            if not any(a <= m.start() < b for a, b in spans):  # 아라비아 숫자 매치와 겹치면 제외
                spans.append((m.start(), m.end()))
        for a, b in sorted(spans):
            out.append((c["start_ms"], c["text"][a:b].strip()))
    return out


def main(paths: list[str]) -> None:
    runs = [(Path(p).stem, *load(p)) for p in paths]

    print("=" * 70)
    print("메타")
    for name, meta, cues in runs:
        dev = meta.get("device") or meta.get("location", "")
        secs = meta.get("infer_sec") or meta.get("total_sec")
        print(f"  {name:28s} engine={meta.get('engine','?')} model={meta.get('model','?')} {dev} "
              f"처리 {secs}초 / 오디오 {meta.get('audio_sec','?')}초, 큐 {len(cues)}개 "
              f"(원시 {meta.get('raw_segments','?')})")

    print("=" * 70)
    print("숫자·가격 표현 (시각순) — 엔진 간 대조용")
    for name, _, cues in runs:
        toks = number_tokens(cues)
        print(f"  [{name}] {len(toks)}개")
        for ms, t in toks:
            print(f"     {ms/1000:6.1f}s  {t}")

    print("=" * 70)
    print(f"병렬 텍스트 ({WINDOW_MS//1000}초 창)")
    end = max(c["end_ms"] for _, _, cues in runs for c in cues)
    for w0 in range(0, end, WINDOW_MS):
        w1 = w0 + WINDOW_MS
        print(f"\n--- {w0/1000:.0f}~{w1/1000:.0f}초")
        for name, _, cues in runs:
            txt = " ".join(c["text"] for c in cues if w0 <= c["start_ms"] < w1)
            print(f"  [{name[:14]:14s}] {txt if txt else '(없음)'}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    main(sys.argv[1:])
