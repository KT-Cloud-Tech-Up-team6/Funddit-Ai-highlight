"""평가 데이터셋 실행 — data/eval/ 정답셋으로 각 영역을 채점한다.

  python -m eval.run_dataset                    # 코드 판정 영역만 (API 호출 없음)
  python -m eval.run_dataset --with-llm         # 구간 분할·자막까지 (API 과금)

코드로 판정할 수 있는 영역(소재 적합성·음성 인식·질문 집중)은 무료로 매번 돌린다.
LLM 영역은 과금되므로 명시적으로 요청할 때만 실행한다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "data" / "eval"


def load(name: str) -> dict:
    return json.loads((EVAL / name).read_text(encoding="utf-8"))


def _pct(hit: int, total: int) -> str:
    return f"{hit}/{total} ({hit / total * 100:.0f}%)" if total else "0/0"


# ── 영역 1: 소재 적합성 ───────────────────────────────────────────────
def run_screening() -> dict:
    from api.settings import MIN_CUTS_PER_MIN

    cases = load("01_screening.json")["cases"]
    hit = 0
    fails = []
    for c in cases:
        predicted = c["cuts_per_min"] >= MIN_CUTS_PER_MIN
        if predicted == c["expected_suitable"]:
            hit += 1
        else:
            fails.append(f"{c['id']} 분당컷 {c['cuts_per_min']} "
                         f"예측={predicted} 정답={c['expected_suitable']}")
    return {"area": "소재 적합성", "total": len(cases), "hit": hit, "fails": fails}


# ── 영역 2: 음성 인식 ─────────────────────────────────────────────────
def run_stt() -> dict:
    """이미 생성된 자막 파일로 채점한다. STT를 다시 돌리지 않는다."""
    transcripts = {
        "roborock_v2.mp4": ROOT / "out" / "real" / "rb2_transcript_large.json",
        "video2.mp4": ROOT / "out" / "real" / "v2_transcript_large.json",
        "roborock_f25.mp4": ROOT / "out" / "real" / "transcript_large.json",
    }
    texts = {}
    for video, path in transcripts.items():
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            texts[video] = " ".join(c["text"] for c in d["transcript"])

    cases = load("02_stt_keywords.json")["cases"]
    hit = wrong = skipped = 0
    fails = []
    for c in cases:
        full = texts.get(c["video"])
        if full is None:
            skipped += 1
            continue
        n = sum(full.count(v) for v in c["accept"])
        if n >= c["min_count"]:
            hit += 1
        else:
            fails.append(f"{c['id']} '{c['keyword']}' {n}회 (필요 {c['min_count']})")
        wrong += sum(full.count(r) for r in c.get("reject", []))
    return {"area": "음성 인식", "total": len(cases) - skipped, "hit": hit,
            "extra": f"오인식 {wrong}건, 자막 없어 건너뜀 {skipped}건", "fails": fails}


# ── 영역 5: 질문 집중 구간 ────────────────────────────────────────────
def run_questions() -> dict:
    from poc.comments import find_p2_windows

    def iou(a, b):
        i = max(0, min(a[1], b[1]) - max(a[0], b[0]))
        u = (a[1] - a[0]) + (b[1] - b[0]) - i
        return i / u if u > 0 else 0.0

    cases = load("05_question_windows.json")["cases"]
    by_file: dict[str, list] = {}
    for c in cases:
        by_file.setdefault(c["comments_file"], []).append(c)

    hit = total = 0
    fails = []
    for fname, group in by_file.items():
        comments = json.loads((EVAL / fname).read_text(encoding="utf-8"))["comments"]
        found = find_p2_windows(comments)
        expected = [c for c in group if not c.get("expected_absent")]
        absent = [c for c in group if c.get("expected_absent")]

        for c in expected:
            total += 1
            best = max((iou((c["start_ms"], c["end_ms"]), (f["start_ms"], f["end_ms"]))
                        for f in found), default=0.0)
            if best >= 0.5:
                hit += 1
            else:
                fails.append(f"{c['id']} 미검출 (최대 IoU {best:.2f}) — {c['context']}")
        for c in absent:
            total += 1
            if not found:
                hit += 1
            else:
                fails.append(f"{c['id']} 오탐 {len(found)}건 — {c['context']}")
    return {"area": "질문 집중 구간", "total": total, "hit": hit, "fails": fails}


# ── 영역 3·4: LLM 필요 ────────────────────────────────────────────────
def run_llm_areas() -> list[dict]:
    """구간 분할·자막은 실제 모델 호출이 필요하다. eval.bench_llm을 쓰라고 안내만 한다."""
    seg = load("03_segments.json")["cases"]
    cap = load("04_caption_facts.json")["cases"]
    return [
        {"area": "구간 분할", "total": len(seg), "hit": None,
         "extra": "python -m eval.bench_llm 로 실행 (API 과금)"},
        {"area": "포인트 자막", "total": len(cap), "hit": None,
         "extra": "python -m eval.bench_llm 로 실행 (API 과금)"},
    ]


def main() -> None:
    ap = argparse.ArgumentParser(prog="eval.run_dataset")
    ap.add_argument("--verbose", action="store_true", help="실패 케이스 전부 표시")
    args = ap.parse_args()

    idx = load("00_index.json")
    print("=" * 62)
    print(f"평가 데이터셋 실행 — 총 {idx['total']}건")
    print("=" * 62)

    results = [run_screening(), run_stt(), run_questions()]
    for r in results:
        score = _pct(r["hit"], r["total"])
        print(f"\n[{r['area']}] {score}")
        if r.get("extra"):
            print(f"  {r['extra']}")
        fails = r.get("fails", [])
        if fails:
            show = fails if args.verbose else fails[:3]
            for f in show:
                print(f"  X {f}")
            if len(fails) > len(show):
                print(f"  ... 외 {len(fails) - len(show)}건 (--verbose로 전체 표시)")

    for r in run_llm_areas():
        print(f"\n[{r['area']}] 정답 {r['total']}건 — {r['extra']}")

    graded = [r for r in results if r["total"]]
    if graded:
        th = sum(r["hit"] for r in graded)
        tt = sum(r["total"] for r in graded)
        print("\n" + "=" * 62)
        print(f"코드 판정 영역 종합: {_pct(th, tt)}")


if __name__ == "__main__":
    main()
