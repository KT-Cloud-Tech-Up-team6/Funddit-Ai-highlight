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

# 인접 프레임의 평균 픽셀 차이(0~255). 이 값 미만인 초는 정지 화면으로 본다.
# 실측(로보락 F25): 움직임 있는 초는 0.3 이상, 정지 화면은 0.15 이하로 뚜렷하게 갈렸다.
STILL_THRESHOLD = 0.3
# 이 길이 이상 이어지는 정지 구간만 보고한다
MIN_STILL_SEC = 5
# 장면 전환 한 프레임이 통째로 "움직임"으로 잡히지 않도록, 이 값을 넘는 차이는 전환으로 간주.
# 실제 피사체 움직임은 이 값을 넘지 않는다 (전환은 20~50대로 확연히 크다).
CUT_THRESHOLD = 18.0
SAMPLE_FPS = 2


def motion_per_second(video: str | Path, fps: int = SAMPLE_FPS) -> list[float]:
    """초당 움직임 강도. 값이 클수록 화면이 많이 바뀐다. 정지 화면은 0에 가깝다.

    장면 전환(컷)은 값이 매우 크게 튀므로 CUT_THRESHOLD로 잘라내고 집계한다 —
    "컷이 자주 바뀐다"와 "화면이 실제로 움직인다"는 다른 것이기 때문."""
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
            return []
        arr = np.stack([np.array(Image.open(f).convert("L"), dtype=np.int16) for f in files])
        pair = np.abs(np.diff(arr, axis=0)).mean(axis=(1, 2))
        pair = np.where(pair > CUT_THRESHOLD, 0.0, pair)  # 컷은 움직임으로 세지 않는다
        return [float(pair[i:i + fps].mean()) for i in range(0, max(len(pair) - 1, 1), fps)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def still_ranges(motion: list[float], threshold: float = STILL_THRESHOLD,
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
                threshold: float = STILL_THRESHOLD) -> float:
    """구간 안에서 정지 화면이 차지하는 비율 (0~1). 게이트 판정용."""
    a = start_ms // 1000
    b = max(end_ms // 1000, a + 1)
    window = motion[a:b]
    if not window:
        return 0.0
    return sum(v < threshold for v in window) / len(window)


def motion_score(motion: list[float], start_ms: int, end_ms: int) -> float:
    """구간의 평균 움직임 강도. 후보 구간을 고를 때 높은 쪽을 우선한다."""
    a = start_ms // 1000
    b = max(end_ms // 1000, a + 1)
    window = motion[a:b]
    return sum(window) / len(window) if window else 0.0


def live_windows(motion: list[float], min_sec: int = 20, merge_gap: int = 10,
                 threshold: float = STILL_THRESHOLD) -> list[dict]:
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
    motion = motion_per_second(video)
    stills = still_ranges(motion)
    still_total = sum((b - a) // 1000 for a, b in stills)
    result = {
        "video": str(video),
        "duration_sec": len(motion),
        "threshold": STILL_THRESHOLD,
        "motion_per_sec": [round(v, 3) for v in motion],
        "still_ranges": [{"start_ms": a, "end_ms": b, "sec": (b - a) // 1000} for a, b in stills],
        "still_total_sec": still_total,
        "still_pct": round(still_total / max(len(motion), 1) * 100, 1),
        "live_windows": live_windows(motion),
    }
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def load(path: str | Path) -> list[float]:
    """analyze가 저장한 JSON에서 motion_per_sec만 읽는다."""
    return json.loads(Path(path).read_text(encoding="utf-8"))["motion_per_sec"]


def main():
    ap = argparse.ArgumentParser(prog="poc.motion")
    ap.add_argument("video")
    ap.add_argument("--out")
    args = ap.parse_args()
    r = analyze(args.video, args.out)
    print(f"길이 {r['duration_sec']}초, 정지 화면 {r['still_total_sec']}초 ({r['still_pct']}%)")
    print(f"정지 구간 {len(r['still_ranges'])}개:")
    for s in r["still_ranges"][:12]:
        print(f"  {s['start_ms']/1000:6.0f} ~ {s['end_ms']/1000:6.0f}초  ({s['sec']}초)")
    print(f"실사(움직임) 구간 {len(r['live_windows'])}개:")
    for w in r["live_windows"]:
        print(f"  {w['start_ms']/1000:6.0f} ~ {w['end_ms']/1000:6.0f}초  ({w['sec']}초)")
    if args.out:
        print(f"저장: {args.out}")


if __name__ == "__main__":
    main()
