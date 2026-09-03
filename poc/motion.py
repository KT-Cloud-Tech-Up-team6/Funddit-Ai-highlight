"""영상 움직임 분석 — 정지 화면 구간을 찾아 구간 선택에서 거른다.

실제 방송(로보락 F25)에서 발견한 문제: 9분 20초 중 대부분이 정지 화면이었다.
장면이 몇 초마다 바뀌지만 장면 안에서는 그림이 멈춰 있는, 슬라이드쇼에 가까운 편집이다.
발화만 보고 구간을 고르면 "말은 흘러가는데 화면은 멈춘" 쇼츠가 나온다.
그래서 구간별 움직임을 재서 M1 입력과 게이트에 함께 쓴다.

측정 방법 (중요):
  ffmpeg의 signalstats/scene_score는 화면 전체 평균이라 작은 피사체 움직임이 0으로 묻힌다.
  또 `-ss`를 `-i` 앞에 두면 키프레임으로 튀어 엉뚱한 구간을 잰다.
  여기서는 프레임을 PNG로 뽑아 인접 프레임의 픽셀 차이를 직접 계산한다.

  python -m poc.motion 방송.mp4 --out out/real/motion.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

# 인접 프레임의 평균 픽셀 차이(0~255).
# 실측(로보락 F25): 장면 전환은 8 이상으로 크게 튀고, 장면 안 움직임은 대부분 0.01 미만이었다.
CUT_THRESHOLD = 8.0        # 이 이상이면 장면 전환(컷)
MOTION_THRESHOLD = 0.3     # 컷이 아니면서 이 이상이면 장면 안에서 피사체가 움직인 것

# 구간이 "살아있다"고 보는 기준 — 둘 중 하나만 만족해도 된다.
#   ① 장면 전환이 MIN_CUTS_PER_MIN회/분 이상 (컷으로 화면이 계속 바뀜)
#   ② 장면 안 움직임이 있는 초가 MIN_MOVING_RATIO 이상
MIN_CUTS_PER_MIN = 3.0
# 한 화면이 이 길이 이상 이어지면 '긴 정지 샷'으로 보고한다
MIN_STILL_SEC = 5
MIN_MOVING_RATIO = 0.3
SAMPLE_FPS = 2


def measure(video: str | Path, fps: int = SAMPLE_FPS) -> tuple[list[float], list[int]]:
    """초당 (장면 안 움직임 강도, 장면 전환 횟수)를 반환한다.

    장면 전환과 피사체 움직임은 다른 현상이라 나눠서 잰다.
    ffmpeg signalstats/scene_score는 화면 전체 평균이라 작은 움직임이 묻히고,
    `-ss`를 `-i` 앞에 두면 키프레임으로 튀므로 프레임을 뽑아 직접 계산한다."""
    import numpy as np
    from PIL import Image

    tmp = Path(tempfile.mkdtemp(prefix="motion_"))
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps={fps},scale=160:90",
             str(tmp / "f_%05d.png")],
            check=True,
        )
        files = sorted(tmp.glob("f_*.png"))
        if len(files) < 2:
            return [], []
        arr = np.stack([np.asarray(Image.open(f).convert("L")).astype(np.int16) for f in files])
        pair = np.abs(np.diff(arr, axis=0)).mean(axis=(1, 2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    is_cut = pair > CUT_THRESHOLD
    inner = np.where(is_cut, 0.0, pair)  # 컷은 "장면 안 움직임"이 아니다
    motion, cuts = [], []
    for i in range(0, max(len(pair) - 1, 1), fps):
        motion.append(float(inner[i:i + fps].mean()))
        cuts.append(int(is_cut[i:i + fps].sum()))
    return motion, cuts


def motion_per_second(video: str | Path, fps: int = SAMPLE_FPS) -> list[float]:
    """장면 안 움직임만 (하위 호환)."""
    return measure(video, fps)[0]


def still_ranges(motion: list[float], threshold: float = MOTION_THRESHOLD,
                 min_sec: int = MIN_STILL_SEC) -> list[tuple[int, int]]:
    """정지 구간 [(start_ms, end_ms)] 목록."""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for i, v in enumerate(motion):
        if v < threshold:
            if start is None:
                start = i
        elif start is not None:
            if i - start >= min_sec:
                out.append((start * 1000, i * 1000))
            start = None
    if start is not None and len(motion) - start >= min_sec:
        out.append((start * 1000, len(motion) * 1000))
    return out


def still_ratio(motion: list[float], start_ms: int, end_ms: int,
                threshold: float = MOTION_THRESHOLD) -> float:
    """구간 안에서 정지 화면이 차지하는 비율 (0~1). 게이트 판정용."""
    a = start_ms // 1000
    b = max(end_ms // 1000, a + 1)
    window = motion[a:b]
    if not window:
        return 0.0
    return sum(v < threshold for v in window) / len(window)


def motion_score(motion: list[float], start_ms: int, end_ms: int) -> float:
    """구간의 평균 움직임 강도."""
    a = start_ms // 1000
    b = max(end_ms // 1000, a + 1)
    window = motion[a:b]
    return sum(window) / len(window) if window else 0.0


def cuts_per_min(cuts: list[int], start_ms: int, end_ms: int) -> float:
    """구간의 분당 장면 전환 횟수. 컷이 잦으면 화면이 계속 바뀌므로 쇼츠가 지루하지 않다."""
    a = start_ms // 1000
    b = max(end_ms // 1000, a + 1)
    window = cuts[a:b]
    if not window:
        return 0.0
    return sum(window) / len(window) * 60


def liveliness(motion: list[float], cuts: list[int], start_ms: int, end_ms: int) -> dict:
    """구간이 "화면이 살아있는가"를 판정한다.

    ok=False면 그 구간으로 만든 쇼츠는 멈춘 그림처럼 보인다.
    컷이 잦거나(①) 장면 안에서 피사체가 움직이면(②) 통과."""
    a = start_ms // 1000
    b = max(end_ms // 1000, a + 1)
    win = motion[a:b]
    moving_ratio = (sum(v >= MOTION_THRESHOLD for v in win) / len(win)) if win else 0.0
    cpm = cuts_per_min(cuts, start_ms, end_ms) if cuts else 0.0
    return {
        "cuts_per_min": round(cpm, 1),
        "moving_ratio": round(moving_ratio, 2),
        "ok": cpm >= MIN_CUTS_PER_MIN or moving_ratio >= MIN_MOVING_RATIO,
    }


def live_windows(motion: list[float], min_sec: int = 20, merge_gap: int = 10,
                 threshold: float = MOTION_THRESHOLD) -> list[dict]:
    """움직임이 있는 시간대 — M1에 "여기서 고르라"고 줄 후보. 짧은 정지는 이어 붙인다."""
    live = [i for i, v in enumerate(motion) if v >= threshold]
    if not live:
        return []
    out: list[dict] = []
    s = p = live[0]
    for i in live[1:]:
        if i - p <= merge_gap:
            p = i
            continue
        if p - s + 1 >= min_sec:
            out.append({"start_ms": s * 1000, "end_ms": (p + 1) * 1000, "sec": p - s + 1})
        s = p = i
    if p - s + 1 >= min_sec:
        out.append({"start_ms": s * 1000, "end_ms": (p + 1) * 1000, "sec": p - s + 1})
    return out


def analyze(video: str | Path, out_path: str | Path | None = None) -> dict:
    motion, cuts = measure(video)
    stills = still_ranges(motion)
    still_total = sum((b - a) // 1000 for a, b in stills)
    total_cuts = sum(cuts)
    dur = max(len(motion), 1)
    result = {
        "video": str(video),
        "duration_sec": len(motion),
        "motion_threshold": MOTION_THRESHOLD,
        "cut_threshold": CUT_THRESHOLD,
        "motion_per_sec": [round(v, 3) for v in motion],
        "cuts_per_sec": cuts,
        "total_cuts": total_cuts,
        "cuts_per_min": round(total_cuts / dur * 60, 1),
        "sec_per_cut": round(dur / total_cuts, 1) if total_cuts else None,
        "moving_sec": sum(v >= MOTION_THRESHOLD for v in motion),
        "still_ranges": [{"start_ms": a, "end_ms": b, "sec": (b - a) // 1000} for a, b in stills],
        "still_total_sec": still_total,
        "still_pct": round(still_total / dur * 100, 1),
        "live_windows": live_windows(motion),
    }
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def load(path: str | Path) -> list[float]:
    """analyze가 저장한 JSON에서 motion_per_sec만 읽는다 (하위 호환)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))["motion_per_sec"]


def load_full(path: str | Path) -> tuple[list[float], list[int]]:
    """(장면 안 움직임, 초당 컷 수)를 함께 읽는다."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return d["motion_per_sec"], d.get("cuts_per_sec", [])


def main():
    ap = argparse.ArgumentParser(prog="poc.motion")
    ap.add_argument("video")
    ap.add_argument("--out")
    args = ap.parse_args()
    r = analyze(args.video, args.out)
    print(f"길이 {r['duration_sec']}초")
    print(f"  장면 전환 {r['total_cuts']}회 (분당 {r['cuts_per_min']}회"
          + (f", 평균 {r['sec_per_cut']}초마다" if r["sec_per_cut"] else "") + ")")
    print(f"  장면 안 움직임이 있는 초: {r['moving_sec']} / {r['duration_sec']}")
    print(f"  한 장면이 5초 이상 이어진 구간: {len(r['still_ranges'])}개, 총 {r['still_total_sec']}초")
    if args.out:
        print(f"저장: {args.out}")


if __name__ == "__main__":
    main()
