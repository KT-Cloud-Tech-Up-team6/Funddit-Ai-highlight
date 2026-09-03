"""FFmpeg 렌더링 — 컷 + 세로 9:16 중앙 크롭 + 포인트 자막(ASS) 번인 + 썸네일."""
from __future__ import annotations

import subprocess
from pathlib import Path

from poc.models import Caption

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Point,Malgun Gothic,76,&H00FFFFFF,&H00FFFFFF,&H00000000,&H7F000000,-1,0,0,0,100,100,0,0,3,10,0,2,60,60,380,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_time(ms: int) -> str:
    cs = int(ms / 10)
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6_000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass(captions: list[Caption], out_path: str | Path) -> Path:
    lines = [ASS_HEADER]
    for c in captions:
        lines.append(
            f"Dialogue: 0,{_ass_time(c.start_ms)},{_ass_time(c.end_ms)},Point,,0,0,0,,{c.text}\n"
        )
    out = Path(out_path)
    out.write_text("".join(lines), encoding="utf-8")
    return out


def _run(cmd: list[str], cwd: str | Path | None = None) -> None:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg 실패 (exit {p.returncode}):\n{p.stderr[-2000:]}")


def render_short(
    video: str | Path,
    start_ms: int,
    end_ms: int,
    ass_path: str | Path,
    out_path: str | Path,
    crop_cx: float = 0.5,
) -> Path:
    """구간 컷 + 세로 크롭 + 자막 번인. -ss가 -i 앞이라 출력 타임스탬프는 0부터 시작
    → ASS의 쇼츠 로컬 시각과 일치한다.
    crop_cx: 크롭 창 중심의 가로 위치(0~1). 0.5=중앙 고정. 방송 화면 왼쪽에 가격 패널이 붙어
    진행자·제품이 오른쪽으로 치우친 경우 0.6~0.7로 조정 (계획서 7번 "문제 있으면 추적 크롭 검토")."""
    video, ass_path, out_path = Path(video).resolve(), Path(ass_path).resolve(), Path(out_path).resolve()
    # 크롭 창 x = 중심 - 창너비/2, 화면 밖으로 안 나가게 clip
    crop_x = f"clip(iw*{crop_cx:.4f}-ih*9/32\\,0\\,iw-ih*9/16)"  # 쉼표는 필터 구분자라 이스케이프
    # Windows 드라이브 콜론 이스케이프 문제를 피하려고 ASS 파일이 있는 폴더에서 실행
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_ms/1000:.3f}",
        "-to", f"{end_ms/1000:.3f}",
        "-i", str(video),
        "-vf", f"crop=ih*9/16:ih:{crop_x}:0,scale=1080:1920,ass={ass_path.name}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-movflags", "+faststart",
        str(out_path),
    ]
    _run(cmd, cwd=ass_path.parent)
    return out_path


def thumbnail(video: str | Path, at_ms: int, out_path: str | Path) -> Path:
    """판매자 선택 화면 후보 카드용 썸네일 1장."""
    out = Path(out_path).resolve()
    _run([
        "ffmpeg", "-y", "-ss", f"{at_ms/1000:.3f}", "-i", str(Path(video).resolve()),
        "-frames:v", "1", "-q:v", "2", str(out),
    ])
    return out


def make_test_video(duration_s: int, out_path: str | Path) -> Path:
    """실제 방송 영상이 없을 때 파이프라인 검증용 테스트 영상 생성."""
    out = Path(out_path).resolve()
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", str(duration_s),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "aac",
        str(out),
    ])
    return out
