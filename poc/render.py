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
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,Malgun Gothic,92,&H00FFFFFF,&H00FFFFFF,&H00202020,&H00000000,-1,0,0,0,100,100,2,0,1,7,5,8,50,50,{title_mv},1
Style: Point,Malgun Gothic,84,&H00FFFFFF,&H00FFFFFF,&H00151515,&H00000000,-1,0,0,0,100,100,1,0,1,7,4,2,50,50,{point_mv},1
Style: Spec,Malgun Gothic,84,&H00F5F5F5,&H00FFFFFF,&H00151515,&H00000000,-1,0,0,0,100,100,1,0,1,7,4,2,50,50,{point_mv},1
Style: Benefit,Malgun Gothic,90,&H0000E5FF,&H00FFFFFF,&H00101010,&H00000000,-1,0,0,0,105,105,1,0,1,8,4,2,50,50,{point_mv},1
Style: Result,Malgun Gothic,88,&H0000FFFF,&H00FFFFFF,&H00101010,&H00000000,-1,0,0,0,100,100,1,0,1,8,4,2,50,50,{point_mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# 강조 유형 → 스타일. benefit(혜택·가격)은 주황, result(시연 결과)는 노랑, spec은 기본 흰색.
EMPHASIS_STYLE = {"spec": "Spec", "benefit": "Benefit", "result": "Result"}
HIGHLIGHT_COLOR = "&H0000A5FF&"  # 주황 (BGR) — 숫자·핵심어 강조

# 자막 세로 위치 (PlayResY=1920 기준)
#   crop 모드: 화면이 꽉 차므로 하단 전화번호 띠를 피해 위로 올린다
#   letterbox 모드: 영상이 가운데 띠로 들어가고 위아래가 여백 → 제목은 위 여백, 자막은 아래 여백에
MARGIN_CROP = {"title_mv": 200, "point_mv": 380}
MARGIN_LETTERBOX = {"title_mv": 250, "point_mv": 250}


def _ass_time(ms: int) -> str:
    cs = int(ms / 10)
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6_000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(t: str) -> str:
    return t.replace("{", "(").replace("}", ")").replace("\n", " ")


def _with_highlight(c: Caption) -> str:
    text = _ass_escape(c.text)
    hl = _ass_escape(getattr(c, "highlight", "") or "")
    if hl and hl in text:
        return text.replace(hl, "{\\c" + HIGHLIGHT_COLOR + "}" + hl + "{\\r}", 1)
    return text


def build_ass(captions: list[Caption], out_path: str | Path, title: str = "", duration_ms: int | None = None,
              layout: str = "crop") -> Path:
    """포인트 자막 + (선택) 상단 고정 제목. title은 구간 전체(0 ~ duration_ms)에 표시.

    layout: "crop"(9:16 꽉 채움) | "letterbox"(원본 비율 유지, 위아래 여백) — 자막 세로 위치가 달라진다."""
    margins = MARGIN_LETTERBOX if layout == "letterbox" else MARGIN_CROP
    lines = [ASS_HEADER.format(**margins)]
    if title:
        end = duration_ms if duration_ms else max((c.end_ms for c in captions if c.end_ms), default=0) + 10_000
        lines.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(end)},Title,,0,0,0,,{_ass_escape(title)}\n")
    for c in captions:
        style = EMPHASIS_STYLE.get(c.emphasis, "Point")
        lines.append(
            f"Dialogue: 1,{_ass_time(c.start_ms)},{_ass_time(c.end_ms)},{style},,0,0,0,,{_with_highlight(c)}\n"
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
    vertical: bool = True,
    layout: str = "crop",
    bg_blur: bool = False,
) -> Path:
    """구간 컷 + 세로 크롭 + 자막 번인. -ss가 -i 앞이라 출력 타임스탬프는 0부터 시작
    → ASS의 쇼츠 로컬 시각과 일치한다.
    crop_cx: 크롭 창 중심의 가로 위치(0~1). 0.5=중앙 고정. 방송 화면 왼쪽에 가격 패널이 붙어
    진행자·제품이 오른쪽으로 치우친 경우 0.6~0.7로 조정 (계획서 7번 "문제 있으면 추적 크롭 검토")."""
    video, ass_path, out_path = Path(video).resolve(), Path(ass_path).resolve(), Path(out_path).resolve()
    # 크롭 창 x = 중심 - 창너비/2, 화면 밖으로 안 나가게 clip
    crop_x = f"clip(iw*{crop_cx:.4f}-ih*9/32\\,0\\,iw-ih*9/16)"  # 쉼표는 필터 구분자라 이스케이프
    # 레이아웃 3가지
    #   crop      9:16으로 잘라 꽉 채움 (피사체가 잘릴 수 있음)
    #   letterbox 원본 비율 그대로 1080x1920 캔버스 가운데 배치, 위아래는 여백 (잘림 없음)
    #   wide      크롭 없이 원본 16:9 (1920x1080)
    if layout == "letterbox":
        # 원본을 가로 1080에 맞춰 축소 → 세로 608px 띠가 화면 중앙에 오고 위아래에 여백이 남는다.
        # 여백은 검정(기본) 또는 원본을 흐리게 깐 배경(bg_blur=True).
        if bg_blur:
            vf = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                  "crop=1080:1920,gblur=sigma=28,eq=brightness=-0.22[bg];"
                  "[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
        else:
            vf = "scale=1080:-2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    elif vertical:
        vf = f"crop=ih*9/16:ih:{crop_x}:0,scale=1080:1920"
    else:
        vf = "scale=1920:1080"
    # Windows 드라이브 콜론 이스케이프 문제를 피하려고 ASS 파일이 있는 폴더에서 실행
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_ms/1000:.3f}",
        "-to", f"{end_ms/1000:.3f}",
        "-i", str(video),
        ("-filter_complex" if vf.startswith("[0:v]") else "-vf"), f"{vf},ass={ass_path.name}",
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
