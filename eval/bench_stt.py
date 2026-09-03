"""STT 정량 벤치 — 자막 파일들을 키워드·숫자 정확도, CER, 처리 시간, 비용으로 비교한다.

  python -m eval.bench_stt out/real/transcript_*.json [--reference data/real/reference_transcript.txt]

지표
  kw_score    stt_keywords.json의 키워드별 min(등장횟수/min_count, 1) 평균 — 제품명·가격·수치를 제대로 받아썼는가
  kw_hit      min_count를 채운 키워드 비율
  wrong       오인식 형태(wrong 목록) 등장 횟수
  cer         기준 전사(reference)가 있으면 문자 오류율 (공백·문장부호 제거 후 편집거리/기준길이)
  pair_cer    엔진 간 상호 CER 평균 (기준 전사가 없을 때의 대체 일관성 신호)
  cues / punct  큐 수, 문장부호로 끝나는 큐 비율 / max_gap 최대 무발화 구간(초) / mono 타임코드 단조성
  span        마지막 큐 종료 시각 / 오디오 길이 — 1.0에서 멀면 타임코드가 압축·팽창됨
  ts_drift    키워드 등장 시각을 기준 엔진(--ts-ref, 기본 Whisper large-v3)과 맞춰본 중앙값 오차(초) — 자막 싱크·컷 정확도
  sec / usd   처리 시간(meta.infer_sec) / 추정 비용(meta.est_usd, 로컬 0)
결과: eval/results/bench_stt_<시각>.md + .json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from datetime import datetime
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
STRIP_RE = re.compile(r"[\s.,!?~·'\"()\[\]:;]")


def normalize(s: str) -> str:
    return STRIP_RE.sub("", s)


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(hyp: str, ref: str) -> float:
    h, r = normalize(hyp), normalize(ref)
    return levenshtein(h, r) / max(len(r), 1)


def load(path: str):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return d.get("meta", {}), d["transcript"]


def keyword_scores(text: str, keywords: list[dict]) -> dict:
    per = []
    wrong = 0
    for k in keywords:
        cnt = sum(text.count(v) for v in k["variants"])
        per.append({"key": k["key"], "count": cnt, "need": k["min_count"], "score": min(cnt / k["min_count"], 1.0)})
        wrong += sum(text.count(w) for w in k.get("wrong", []))
    return {
        "kw_score": statistics.mean(p["score"] for p in per),
        "kw_hit": sum(p["score"] >= 1 for p in per) / len(per),
        "wrong": wrong,
        "per_keyword": per,
    }


def keyword_times(cues: list[dict], keywords: list[dict]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for c in cues:
        for k in keywords:
            if any(v in c["text"] for v in k["variants"]):
                out.setdefault(k["key"], []).append(c["start_ms"])
    return out


def ts_drift(cues: list[dict], ref_cues: list[dict], keywords: list[dict]) -> float | None:
    """키워드가 등장한 큐의 시작 시각을 기준 엔진의 가장 가까운 등장 시각과 비교 → 중앙값 |오차| (초)."""
    a, b = keyword_times(cues, keywords), keyword_times(ref_cues, keywords)
    diffs = []
    for key, ts in a.items():
        if key not in b:
            continue
        for t in ts:
            diffs.append(min(abs(t - r) for r in b[key]) / 1000)
    return statistics.median(diffs) if diffs else None


def fmt(x, nd=2):
    if x is None:
        return "?"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcripts", nargs="+")
    ap.add_argument("--keywords", default="data/real/stt_keywords.json")
    ap.add_argument("--reference", default="data/real/reference_transcript.txt")
    ap.add_argument("--ts-ref", default="out/real/transcript_large.json", help="타임코드 기준 엔진 자막")
    ap.add_argument("--audio-sec", type=float, default=560.4)
    args = ap.parse_args()
    ts_ref_cues = load(args.ts_ref)[1] if Path(args.ts_ref).exists() else None

    kws = json.loads(Path(args.keywords).read_text(encoding="utf-8"))["keywords"]
    ref_path = Path(args.reference)
    ref_text = None
    if ref_path.exists():
        ref_text = " ".join(re.sub(r"^\[\d\d:\d\d\]\s*", "", ln) for ln in ref_path.read_text(encoding="utf-8").splitlines()
                            if ln.strip() and not ln.startswith("#"))

    rows = []
    texts = {}
    for p in args.transcripts:
        meta, cues = load(p)
        name = Path(p).stem.replace("transcript_", "")
        full = " ".join(c["text"] for c in cues)
        texts[name] = full
        ks = keyword_scores(full, kws)
        gaps = [(b["start_ms"] - a["end_ms"]) / 1000 for a, b in zip(cues, cues[1:])]
        mono = all(b["start_ms"] >= a["start_ms"] for a, b in zip(cues, cues[1:])) and all(c["end_ms"] >= c["start_ms"] for c in cues)
        rows.append({
            "name": name, "engine": meta.get("engine", "?"), "model": meta.get("model", "?"),
            "device": meta.get("device") or meta.get("location") or "",
            "kw_score": ks["kw_score"], "kw_hit": ks["kw_hit"], "wrong": ks["wrong"], "per_keyword": ks["per_keyword"],
            "cer": cer(full, ref_text) if ref_text else None,
            "cues": len(cues), "chars": len(normalize(full)),
            "punct": sum(c["text"].rstrip().endswith((".", "?", "!")) for c in cues) / max(len(cues), 1),
            "max_gap": max(gaps) if gaps else 0.0, "mono": mono,
            "span": (cues[-1]["end_ms"] / 1000 / args.audio_sec) if cues else 0.0,
            "ts_drift": ts_drift(cues, ts_ref_cues, kws) if ts_ref_cues else None,
            "sec": meta.get("infer_sec") or meta.get("total_sec"),
            "usd": meta.get("est_usd", 0.0) or 0.0,
        })
    pair = {n: [] for n in texts}
    for a, b in combinations(texts, 2):
        pair[a].append(cer(texts[a], texts[b]))
        pair[b].append(cer(texts[b], texts[a]))
    for r in rows:
        r["pair_cer"] = statistics.mean(pair[r["name"]]) if pair[r["name"]] else None
    rows.sort(key=lambda r: ((r["cer"] if r["cer"] is not None else 0), -r["kw_score"]))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    h = ["자막", "엔진/모델", "kw_score", "kw_hit", "오인식", "CER", "pair_CER", "큐", "문장부호", "max_gap", "span", "ts_drift", "초", "USD"]
    lines = [f"# STT 벤치 {ts}", "",
             f"- 키워드 {len(kws)}개 (`{args.keywords}`), 기준 전사: {'있음' if ref_text else '없음 → CER 생략, pair_CER 참고'}",
             "- 지표 정의는 `eval/bench_stt.py` docstring.", "",
             "| " + " | ".join(h) + " |", "|" + "---|" * len(h)]
    for r in rows:
        lines.append("| " + " | ".join([
            r["name"], f"{r['engine']}/{r['model']} {r['device']}".strip(), fmt(r["kw_score"]),
            f"{r['kw_hit']*100:.0f}%", str(r["wrong"]), fmt(r["cer"]), fmt(r["pair_cer"]), str(r["cues"]),
            f"{r['punct']*100:.0f}%", fmt(r["max_gap"], 0), fmt(r["span"]), fmt(r["ts_drift"], 1), fmt(r["sec"], 0), fmt(r["usd"], 4),
        ]) + " |")
    lines += ["", "## 키워드별 등장 횟수 (필요 횟수)", "",
              "| 키워드 | " + " | ".join(r["name"] for r in rows) + " |", "|---|" + "---|" * len(rows)]
    for i, k in enumerate(kws):
        lines.append(f"| {k['key']} (>={k['min_count']}) | " + " | ".join(str(r["per_keyword"][i]["count"]) for r in rows) + " |")
    md = "\n".join(lines) + "\n"
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"bench_stt_{ts}.md").write_text(md, encoding="utf-8")
    (RESULTS / f"bench_stt_{ts}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
