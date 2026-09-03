"""비용 산출 — PoC / 개발 / 운영 세 국면으로 나눠 계산한다.

  python -m eval.cost_report                      # 콘솔 + eval/results/cost_report_<시각>.md
  python -m eval.cost_report --shorts-per-month 300

계산 근거
  ① 실측 API 사용량: out/real/llm_usage.jsonl (호출별 토큰·모델·추정 USD)
  ② 실측 STT 처리 시간: out/real/*transcript*.json의 meta
  ③ 단가: poc/llm.py PRICE_PER_M (LLM), 아래 RATES (인프라)

세 국면의 정의
  PoC   지금까지 쓴 검증 비용. 모델 비교·재실행·시행착오 포함 — 한 번 쓰고 끝나는 돈.
  개발  실제 제품으로 만들 때 드는 API 비용. 프롬프트 튜닝·회귀 테스트 반복분.
  운영  방송 1건을 쇼츠로 만드는 단가 × 월 물량. 실제로 매달 나가는 돈.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
USAGE_LOG = ROOT / "out" / "real" / "llm_usage.jsonl"

USD_KRW = 1450  # 환율 가정 — 바뀌면 여기만 수정

# 인프라 단가 가정 (LLM 외 비용)
RATES = {
    # Whisper 로컬은 API 비용 0. 대신 GPU 서버를 쓴다면 시간당 요금이 붙는다.
    # 참고: 클라우드 GPU(T4급) 시간당 $0.35~0.60, 이 PC(GTX 1080)는 전기료뿐.
    "gpu_hour_usd": 0.40,
    # 렌더링은 CPU. 쇼츠 1개당 10~20초 소요 → 사실상 무시할 수준이나 명시한다.
    "cpu_hour_usd": 0.05,
    # 저장·전송: 쇼츠 1개 3~12MB. 월 수백 개면 GB 단위 — 오브젝트 스토리지 기준.
    "storage_gb_month_usd": 0.023,
    "egress_gb_usd": 0.09,
}


def load_usage(path: Path = USAGE_LOG) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def load_stt_meta(pattern: str = "out/real/*transcript*.json") -> list[dict]:
    metas = []
    for f in sorted(ROOT.glob(pattern)):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        m = d.get("meta")
        if m:
            m = dict(m)
            m["file"] = f.name
            metas.append(m)
    return metas


def poc_cost(usage: list[dict], stt: list[dict]) -> dict:
    """지금까지 실제로 쓴 검증 비용."""
    by_model: dict[str, dict] = defaultdict(lambda: {"calls": 0, "usd": 0.0})
    for r in usage:
        b = by_model[r["model"]]
        b["calls"] += 1
        b["usd"] += r.get("est_usd") or 0.0

    stt_api_usd = sum(m.get("est_usd") or 0.0 for m in stt)
    stt_gpu_sec = sum(m.get("infer_sec") or 0.0 for m in stt if (m.get("device") or "").startswith("cuda"))
    stt_gpu_usd = stt_gpu_sec / 3600 * RATES["gpu_hour_usd"]

    llm_usd = sum(b["usd"] for b in by_model.values())
    return {
        "by_model": dict(by_model),
        "llm_calls": sum(b["calls"] for b in by_model.values()),
        "llm_usd": llm_usd,
        "stt_runs": len(stt),
        "stt_api_usd": stt_api_usd,
        "stt_gpu_sec": stt_gpu_sec,
        "stt_gpu_usd": stt_gpu_usd,
        "total_usd": llm_usd + stt_api_usd + stt_gpu_usd,
    }


def unit_cost(usage: list[dict], model: str) -> dict | None:
    """방송 1건 처리 단가 — M1 1회 + M2 3회 (쇼츠 3개) 기준."""
    m1 = [r for r in usage if r["model"] == model and r["tag"].startswith(("m1", "bench-m1"))]
    m2 = [r for r in usage if r["model"] == model and r["tag"].startswith(("m2", "bench-m2"))]
    if not m1 or not m2:
        return None
    m1_usd = statistics.mean([r.get("est_usd") or 0 for r in m1])
    m2_usd = statistics.mean([r.get("est_usd") or 0 for r in m2])
    m1_sec = statistics.mean([r.get("elapsed_sec") or 0 for r in m1])
    m2_sec = statistics.mean([r.get("elapsed_sec") or 0 for r in m2])
    return {
        "model": model,
        "m1_usd": m1_usd, "m2_usd": m2_usd,
        "broadcast_usd": m1_usd + 3 * m2_usd,
        "extra_short_usd": m2_usd,
        "m1_sec": m1_sec, "m2_sec": m2_sec,
        "broadcast_sec": m1_sec + 3 * m2_sec,
        "samples": len(m1) + len(m2),
    }


def stt_unit(stt: list[dict], engine_model: str = "large-v3") -> dict | None:
    """방송 1건 STT 단가 — 실측 처리 시간 기준."""
    runs = [m for m in stt if m.get("model") == engine_model and m.get("infer_sec")]
    if not runs:
        return None
    # 오디오 길이 대비 처리 시간 비율로 정규화 (10분 방송 기준으로 환산)
    ratios = [m["infer_sec"] / m["audio_sec"] for m in runs if m.get("audio_sec")]
    r = statistics.mean(ratios) if ratios else 0
    sec_10min = r * 600
    return {
        "engine": runs[0].get("engine"),
        "model": engine_model,
        "device": runs[0].get("device"),
        "ratio": r,
        "sec_per_10min": sec_10min,
        "gpu_usd_per_10min": sec_10min / 3600 * RATES["gpu_hour_usd"],
        "runs": len(runs),
    }


def render_report(poc: dict, units: list[dict], stt_u: dict | None,
                  shorts_per_month: int, broadcasts_per_month: int) -> str:
    w = lambda usd: usd * USD_KRW
    L = []
    L.append(f"# 비용 산출 보고 ({datetime.now():%Y-%m-%d})")
    L.append("")
    L.append(f"환율 {USD_KRW}원/USD 가정. LLM 단가는 `poc/llm.py` PRICE_PER_M, 인프라 단가는 `eval/cost_report.py` RATES.")
    L.append("STT는 Whisper 로컬이라 **API 비용 0원**이며, GPU 시간만 계산에 넣는다.")
    L.append("")

    # ── 1. PoC ────────────────────────────────────────────────
    L.append("## 1. PoC 비용 (지금까지 쓴 검증 비용)")
    L.append("")
    L.append("모델 비교·재실행·시행착오를 모두 포함한 실측치. 한 번 쓰고 끝나는 돈이다.")
    L.append("")
    L.append("| 항목 | 호출/횟수 | 비용 |")
    L.append("|---|---|---|")
    for m, b in sorted(poc["by_model"].items(), key=lambda kv: -kv[1]["usd"]):
        L.append(f"| LLM {m} | {b['calls']}회 | ${b['usd']:.4f} ({w(b['usd']):.0f}원) |")
    L.append(f"| STT Whisper 로컬 (GPU {poc['stt_gpu_sec']:.0f}초) | {poc['stt_runs']}회 | "
             f"${poc['stt_gpu_usd']:.4f} ({w(poc['stt_gpu_usd']):.0f}원) |")
    if poc["stt_api_usd"]:
        L.append(f"| STT 클라우드(Gemini 오디오) | — | ${poc['stt_api_usd']:.4f} ({w(poc['stt_api_usd']):.0f}원) |")
    L.append(f"| **합계** | **{poc['llm_calls']}회** | **${poc['total_usd']:.4f} ({w(poc['total_usd']):.0f}원)** |")
    L.append("")
    L.append("> 렌더링(FFmpeg)·움직임 분석은 로컬 CPU/GPU라 추가 과금이 없다.")
    L.append("")

    # ── 2. 개발 ───────────────────────────────────────────────
    L.append("## 2. 개발 비용 (제품화 단계 추정)")
    L.append("")
    L.append("프롬프트 튜닝과 회귀 테스트를 반복하는 비용. PoC 1회분을 '벤치 1사이클'로 보고 곱한다.")
    L.append("")
    cycle = poc["total_usd"]
    L.append("| 시나리오 | 사이클 | 비용 |")
    L.append("|---|---|---|")
    for n, label in ((10, "가벼운 튜닝"), (30, "표준"), (60, "충분한 검증")):
        L.append(f"| {label} | {n}회 | ${cycle*n:.2f} ({w(cycle*n):,.0f}원) |")
    L.append("")
    L.append("> 개발 인건비는 제외한 API·인프라 비용만이다. 실제로는 이 금액보다 인건비가 압도적으로 크다.")
    L.append("")

    # ── 3. 운영 ───────────────────────────────────────────────
    L.append("## 3. 운영 비용 (방송 1건 → 쇼츠 3개 기준)")
    L.append("")
    if stt_u:
        L.append(f"STT: {stt_u['engine']} {stt_u['model']} ({stt_u['device']}), "
                 f"실시간 대비 {1/stt_u['ratio']:.1f}배속 — 10분 방송에 {stt_u['sec_per_10min']:.0f}초, "
                 f"GPU 환산 ${stt_u['gpu_usd_per_10min']:.4f} ({w(stt_u['gpu_usd_per_10min']):.1f}원)")
        L.append("")
    L.append("| 모델 | M1 1회 | M2 1회 | 방송 1건(M1+M2×3) | 쇼츠 1개 추가 | 처리 시간 |")
    L.append("|---|---|---|---|---|---|")
    for u in units:
        L.append(f"| {u['model']} | {w(u['m1_usd']):.1f}원 | {w(u['m2_usd']):.1f}원 | "
                 f"**{w(u['broadcast_usd']):.1f}원** | {w(u['extra_short_usd']):.1f}원 | {u['broadcast_sec']:.0f}초 |")
    L.append("")
    stt_won = w(stt_u["gpu_usd_per_10min"]) if stt_u else 0
    L.append(f"위 표에 STT {stt_won:.1f}원을 더하면 방송 1건 총원가가 된다.")
    L.append("")
    L.append(f"### 월 {broadcasts_per_month}건 처리 시 (쇼츠 {shorts_per_month}개)")
    L.append("")
    L.append("| 모델 | LLM | STT(GPU) | 저장·전송 | 월 합계 |")
    L.append("|---|---|---|---|---|")
    # 저장·전송: 쇼츠 1개 평균 8MB 가정
    gb = shorts_per_month * 8 / 1024
    infra = gb * (RATES["storage_gb_month_usd"] + RATES["egress_gb_usd"])
    for u in units:
        llm_m = u["broadcast_usd"] * broadcasts_per_month
        stt_m = (stt_u["gpu_usd_per_10min"] if stt_u else 0) * broadcasts_per_month
        tot = llm_m + stt_m + infra
        L.append(f"| {u['model']} | {w(llm_m):,.0f}원 | {w(stt_m):,.0f}원 | {w(infra):,.0f}원 | "
                 f"**{w(tot):,.0f}원** |")
    L.append("")
    L.append(f"> 저장·전송은 쇼츠 1개 8MB, 월 {gb:.1f}GB 기준. 실제 조회수에 따라 전송량이 달라진다.")
    L.append("> GPU를 상시 띄우지 않고 처리할 때만 켜는 것을 가정했다. 전용 서버를 두면 월 고정비로 잡아야 한다.")
    L.append("")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(prog="eval.cost_report")
    ap.add_argument("--usage", default=str(USAGE_LOG))
    ap.add_argument("--broadcasts-per-month", type=int, default=100)
    ap.add_argument("--shorts-per-month", type=int, default=300)
    ap.add_argument("--models", default="", help="운영 단가를 낼 모델 (쉼표 구분, 기본: 사용량 로그의 전체)")
    args = ap.parse_args()

    usage = load_usage(Path(args.usage))
    stt = load_stt_meta()
    if not usage:
        raise SystemExit(f"사용량 로그가 없습니다: {args.usage}")

    poc = poc_cost(usage, stt)
    models = [m.strip() for m in args.models.split(",") if m.strip()] or sorted(poc["by_model"])
    units = [u for u in (unit_cost(usage, m) for m in models) if u]
    units.sort(key=lambda u: u["broadcast_usd"])
    stt_u = stt_unit(stt)

    md = render_report(poc, units, stt_u, args.shorts_per_month, args.broadcasts_per_month)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"cost_report_{datetime.now():%Y%m%d_%H%M%S}.md"
    out.write_text(md, encoding="utf-8")
    print(md)
    print(f"저장: {out}")


if __name__ == "__main__":
    main()
