"""M1·M2 정량 벤치 — 여러 모델을 같은 입력으로 N회 돌려 수치로 비교한다.

  python -m eval.bench_llm --models gemini-3.5-flash-lite,gemini-3.7-flash,claude-haiku-4-5 --repeats 3

입력 고정: STT 자막(out/real/transcript_large.json), 기준 라벨(data/real/reference_*.json).
결과: eval/results/bench_llm_<시각>.json + .md (모델별 표), 콘솔 요약.

M1 지표 (구간 분할)
  recall      기대 파트(P1/P3/P4) 중 찾은 비율
  iou         찾은 파트의 시간 IoU (기준 구간 alternatives 중 최대) 평균
  len_ok      60~120초 규칙을 지킨 구간 비율
  gap_free    10초 이상 무발화를 포함하지 않은 구간 비율
  fp          기대되지 않는 파트(P5 등) 반환 수
  err         코드 게이트 ERROR 수 (없는 cue_id, 가짜 evidence 등)
  stability   반복 실행 간 같은 파트 구간의 평균 IoU (일관성)
  M1 score  = 0.35*recall + 0.30*iou + 0.15*len_ok + 0.10*gap_free + 0.10*(err==0) - 0.10*fp
M2 지표 (포인트 자막, 기준 구간 고정 → 모델 간 입력 동일)
  coverage    파트별 핵심 사실(reference_facts) 커버 비율
  count_ok    자막 수 3~6개
  len_ok      12자 이내 비율
  err         숫자 환각·근거 없음 ERROR 수
  M2 score  = 0.45*coverage + 0.15*count_ok + 0.20*len_ok + 0.20*(err==0)
비용·지연: 호출당 평균 토큰·초·USD (PRICE_PER_M 기준, 모르면 ?)
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import traceback
from datetime import datetime
from itertools import combinations
from pathlib import Path

from poc import m1_segments, m2_captions
from poc.gates import _core_len
from poc.llm import LLM
from poc.models import Cue, Segment
from poc.transcript import load_cues

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"


def _load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def iou(a: tuple[int, int], b: tuple[int, int]) -> float:
    inter = max(0, min(a[1], b[1]) - max(a[0], b[0]))
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union > 0 else 0.0


def ref_segment(cues: list[Cue], start_ms: int, end_ms: int, part: str, label: str) -> Segment:
    inside = [c for c in cues if c.start_ms >= start_ms - 500 and c.end_ms <= end_ms + 500]
    s = Segment(part_type=part, start_cue_id=inside[0].cue_id, end_cue_id=inside[-1].cue_id, label=label, evidence=[])
    s.start_ms, s.end_ms = inside[0].start_ms, inside[-1].end_ms
    return s


def _usage(llm: LLM) -> dict:
    return {k: v for k, v in (llm.last_usage or {}).items() if k not in ("tag", "model")}


def bench_m1(model: str, cues: list[Cue], ref: dict, run: int) -> dict:
    llm = LLM(model=model, tag=f"bench-m1-r{run}")
    rec = {"model": model, "run": run, "task": "m1", "ok": False}
    try:
        segments, violations = m1_segments.run_m1(cues, [], llm)
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        rec.update(_usage(llm))
        return rec
    rec["ok"] = True
    rec.update(_usage(llm))
    parts = ref["parts"]
    expected = [p for p, d in parts.items() if d["expected"]]
    found: dict[str, tuple[int, int]] = {}
    fp = 0
    for s in segments:
        if s.start_ms is None or s.end_ms is None:
            continue
        if s.part_type in parts and parts[s.part_type]["expected"]:
            found.setdefault(s.part_type, (s.start_ms, s.end_ms))
        else:
            fp += 1
    ious = {}
    for p, (a, b) in found.items():
        ious[p] = max((iou((a, b), (alt["start_ms"], alt["end_ms"])) for alt in parts[p]["alternatives"]), default=0.0)
    durs = [(s.end_ms - s.start_ms) for s in segments if s.end_ms]
    warn_gap = sum(1 for v in violations if v.code == "SEG_GAP")
    err = sum(1 for v in violations if v.level == "ERROR")
    n = max(len(segments), 1)
    rec.update({
        "n_segments": len(segments),
        "found": {p: list(v) for p, v in found.items()},
        "recall": len(found) / len(expected),
        "iou": statistics.mean(ious.values()) if ious else 0.0,
        "iou_by_part": ious,
        "len_ok": sum(60_000 <= d <= 120_000 for d in durs) / n,
        "gap_free": 1 - min(warn_gap, n) / n,
        "fp": fp,
        "err": err,
        "warn": sum(1 for v in violations if v.level == "WARN"),
        "segments": [{"part": s.part_type, "label": s.label, "start_ms": s.start_ms, "end_ms": s.end_ms} for s in segments],
    })
    rec["score"] = round(0.35 * rec["recall"] + 0.30 * rec["iou"] + 0.15 * rec["len_ok"]
                         + 0.10 * rec["gap_free"] + 0.10 * (err == 0) - 0.10 * fp, 3)
    return rec


def bench_m2(model: str, cues: list[Cue], ref: dict, facts: dict, terms: dict, run: int) -> list[dict]:
    out = []
    for part, d in ref["parts"].items():
        if not d["expected"] or part not in facts:
            continue
        alt = d["alternatives"][0]
        seg = ref_segment(cues, alt["start_ms"], alt["end_ms"], part, alt["desc"][:12])
        llm = LLM(model=model, tag=f"bench-m2-{part}-r{run}")
        rec = {"model": model, "run": run, "task": "m2", "part": part, "ok": False}
        try:
            captions, violations = m2_captions.run_m2(seg, cues, terms, llm)
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            rec.update(_usage(llm))
            out.append(rec)
            continue
        rec["ok"] = True
        rec.update(_usage(llm))
        texts = [c.text for c in captions]
        joined = " ".join(texts)
        hits = [f["fact"] for f in facts[part] if any(v in joined for v in f["variants"])]
        err = sum(1 for v in violations if v.level == "ERROR")
        rec.update({
            "n_captions": len(captions),
            "captions": texts,
            "coverage": len(hits) / len(facts[part]),
            "hits": hits,
            "count_ok": 1.0 if 3 <= len(captions) <= 6 else 0.0,
            "len_ok": (sum(_core_len(t) <= 12 for t in texts) / len(texts)) if texts else 0.0,
            "err": err,
            "warn": sum(1 for v in violations if v.level == "WARN"),
        })
        rec["score"] = round(0.45 * rec["coverage"] + 0.15 * rec["count_ok"] + 0.20 * rec["len_ok"] + 0.20 * (err == 0), 3)
        out.append(rec)
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def _fmt(x, nd=2, pct=False):
    if x is None:
        return "?"
    return f"{x*100:.0f}%" if pct else f"{x:.{nd}f}"


def summarize(runs: list[dict], models: list[str]) -> list[dict]:
    rows = []
    for m in models:
        m1 = [r for r in runs if r["model"] == m and r["task"] == "m1"]
        m2 = [r for r in runs if r["model"] == m and r["task"] == "m2"]
        m1ok = [r for r in m1 if r["ok"]]
        m2ok = [r for r in m2 if r["ok"]]
        stab = []
        for a, b in combinations(m1ok, 2):
            for p in set(a["found"]) & set(b["found"]):
                stab.append(iou(tuple(a["found"][p]), tuple(b["found"][p])))
        allc = m1 + m2
        m1_usd = _mean([r.get("est_usd") for r in m1ok])
        m2_usd = _mean([r.get("est_usd") for r in m2ok])
        rows.append({
            "model": m,
            "calls": len(allc),
            "fail": sum(1 for r in allc if not r["ok"]),
            "m1_score": _mean([r["score"] for r in m1ok]),
            "m1_recall": _mean([r["recall"] for r in m1ok]),
            "m1_iou": _mean([r["iou"] for r in m1ok]),
            "m1_len_ok": _mean([r["len_ok"] for r in m1ok]),
            "m1_gap_free": _mean([r["gap_free"] for r in m1ok]),
            "m1_fp": _mean([r["fp"] for r in m1ok]),
            "m1_err": _mean([r["err"] for r in m1ok]),
            "m1_stability": _mean(stab),
            "m2_score": _mean([r["score"] for r in m2ok]),
            "m2_coverage": _mean([r["coverage"] for r in m2ok]),
            "m2_count_ok": _mean([r["count_ok"] for r in m2ok]),
            "m2_len_ok": _mean([r["len_ok"] for r in m2ok]),
            "m2_err": _mean([r["err"] for r in m2ok]),
            "sec_per_call": _mean([r.get("elapsed_sec") for r in allc]),
            "usd_per_call": _mean([r.get("est_usd") for r in allc]),
            "usd_per_broadcast": (m1_usd + 3 * m2_usd) if m1_usd is not None and m2_usd is not None else None,
        })
    for r in rows:
        r["total"] = round(0.5 * (r["m1_score"] or 0) + 0.5 * (r["m2_score"] or 0), 3)
    rows.sort(key=lambda r: -r["total"])
    return rows


def to_markdown(rows: list[dict], meta: dict) -> str:
    h = ["모델", "총점", "M1 점수", "recall", "IoU", "길이OK", "갭없음", "오탐", "ERR", "안정성",
         "M2 점수", "사실커버", "개수OK", "12자OK", "ERR", "초/호출", "USD/호출", "USD/방송", "실패"]
    lines = [f"# LLM 벤치 {meta['ts']}", "",
             f"- 입력: `{meta['transcript']}` (큐 {meta['n_cues']}개), 반복 {meta['repeats']}회, M2는 기준 구간 고정(P1·P3·P4)",
             "- 점수 정의는 `eval/bench_llm.py` docstring 참고. USD는 단가표 기준 추정.", "",
             "| " + " | ".join(h) + " |", "|" + "---|" * len(h)]
    for r in rows:
        lines.append("| " + " | ".join([
            r["model"], _fmt(r["total"]), _fmt(r["m1_score"]), _fmt(r["m1_recall"], pct=True), _fmt(r["m1_iou"]),
            _fmt(r["m1_len_ok"], pct=True), _fmt(r["m1_gap_free"], pct=True), _fmt(r["m1_fp"], 1), _fmt(r["m1_err"], 1),
            _fmt(r["m1_stability"]), _fmt(r["m2_score"]), _fmt(r["m2_coverage"], pct=True), _fmt(r["m2_count_ok"], pct=True),
            _fmt(r["m2_len_ok"], pct=True), _fmt(r["m2_err"], 1), _fmt(r["sec_per_call"], 1),
            _fmt(r["usd_per_call"], 4), _fmt(r["usd_per_broadcast"], 4), str(r["fail"]),
        ]) + " |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True, help="쉼표 구분: gemini-3.5-flash-lite,claude-haiku-4-5,...")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--transcript", default="out/real/transcript_large.json")
    ap.add_argument("--ref-segments", default="data/real/reference_segments.json")
    ap.add_argument("--ref-facts", default="data/real/reference_facts.json")
    ap.add_argument("--terms", default="data/real/product_terms.json")
    ap.add_argument("--skip-m2", action="store_true")
    ap.add_argument("--tag", default="")
    ap.add_argument("--resume", help="이전 bench_llm_*.json — 거기 있는 모델은 건너뛰고 결과를 합친다")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    cues = load_cues(args.transcript)
    ref, facts, terms = _load(args.ref_segments), _load(args.ref_facts), _load(args.terms)
    RESULTS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_json = RESULTS / f"bench_llm_{ts}{args.tag}.json"
    runs: list[dict] = []
    done: set[str] = set()
    if args.resume:
        prev = _load(args.resume).get("runs", [])
        done = {r["model"] for r in prev if r["task"] == "m1" and r["run"] == args.repeats}
        runs.extend(r for r in prev if r["model"] in done)  # 끝까지 못 돈 모델은 버리고 다시 돈다
        print(f"resume: {len(prev)}건 로드, 완료 모델 {sorted(done)}")
    all_models = list(dict.fromkeys([r["model"] for r in runs] + models))
    t0 = time.time()
    for m in models:
        if m in done:
            continue
        for r in range(1, args.repeats + 1):
            print(f"=== {m} run {r}/{args.repeats}")
            try:
                runs.append(bench_m1(m, cues, ref, r))
                if not args.skip_m2:
                    runs.extend(bench_m2(m, cues, ref, facts, terms, r))
            except Exception:  # noqa: BLE001 — 한 모델이 죽어도 나머지는 계속
                traceback.print_exc()
            out_json.write_text(json.dumps({"runs": runs}, ensure_ascii=False, indent=1), encoding="utf-8")
    rows = summarize(runs, all_models)
    meta = {"ts": ts, "transcript": args.transcript, "n_cues": len(cues), "repeats": args.repeats,
            "elapsed_sec": round(time.time() - t0)}
    out_json.write_text(json.dumps({"meta": meta, "summary": rows, "runs": runs}, ensure_ascii=False, indent=1), encoding="utf-8")
    md = to_markdown(rows, meta)
    (RESULTS / f"bench_llm_{ts}{args.tag}.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"저장: eval/results/bench_llm_{ts}{args.tag}.md ({meta['elapsed_sec']}초)")


if __name__ == "__main__":
    main()
