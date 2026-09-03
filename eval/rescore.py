"""이미 저장된 벤치 결과를 새 기준 라벨로 다시 채점한다 (API 재호출 없음).

  python -m eval.rescore eval/results/bench_llm_*.json --ref-segments data/real/rb2_reference_segments.json

기준 구간을 고치면 IoU가 달라지는데, 그것 때문에 API를 다시 호출할 이유는 없다.
모델이 무엇을 골랐는지는 이미 결과 JSON에 들어 있으므로 채점만 다시 한다.
"""
from __future__ import annotations

import argparse
import json
import statistics
from itertools import combinations
from pathlib import Path

from eval.bench_llm import iou, summarize, to_markdown

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"


def rescore_m1(rec: dict, parts: dict) -> dict:
    """저장된 M1 결과의 IoU·recall·score를 새 기준으로 재계산."""
    if not rec.get("ok"):
        return rec
    expected = [p for p, d in parts.items() if d["expected"]]
    found = {p: tuple(v) for p, v in rec.get("found", {}).items() if p in parts and parts[p]["expected"]}
    ious = {
        p: max((iou(v, (alt["start_ms"], alt["end_ms"])) for alt in parts[p]["alternatives"]), default=0.0)
        for p, v in found.items()
    }
    rec = dict(rec)
    rec["recall"] = len(found) / len(expected) if expected else 0.0
    rec["iou"] = statistics.mean(ious.values()) if ious else 0.0
    rec["iou_by_part"] = ious
    rec["score"] = round(
        0.35 * rec["recall"] + 0.30 * rec["iou"] + 0.15 * rec["len_ok"]
        + 0.10 * rec["gap_free"] + 0.10 * (rec["err"] == 0) - 0.10 * rec["fp"], 3)
    return rec


def main():
    ap = argparse.ArgumentParser(prog="eval.rescore")
    ap.add_argument("result_json")
    ap.add_argument("--ref-segments", required=True)
    ap.add_argument("--out-tag", default="_rescored")
    args = ap.parse_args()

    data = json.loads(Path(args.result_json).read_text(encoding="utf-8"))
    parts = json.loads(Path(args.ref_segments).read_text(encoding="utf-8"))["parts"]
    runs = [rescore_m1(r, parts) if r.get("task") == "m1" else r for r in data["runs"]]
    models = list(dict.fromkeys(r["model"] for r in runs))
    rows = summarize(runs, models)

    meta = dict(data.get("meta", {}))
    meta["rescored_with"] = args.ref_segments
    meta.setdefault("ts", "rescored")
    meta.setdefault("transcript", "?")
    meta.setdefault("n_cues", 0)
    meta.setdefault("repeats", 0)

    stem = Path(args.result_json).stem + args.out_tag
    (RESULTS / f"{stem}.json").write_text(
        json.dumps({"meta": meta, "summary": rows, "runs": runs}, ensure_ascii=False, indent=1), encoding="utf-8")
    md = to_markdown(rows, meta)
    (RESULTS / f"{stem}.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"저장: eval/results/{stem}.md")


if __name__ == "__main__":
    main()
