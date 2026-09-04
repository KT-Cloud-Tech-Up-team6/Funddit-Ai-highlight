"""FFmpeg 렌더링 — 컷 + 세로 9:16 중앙 크롭 + 포인트 자막(ASS) 번인 + 썸네일."""
from __future__ import annotations

import subprocess
from pathlib import Path

from poc.models import Caption

# 자막 폰트 — Han Santteut Dotum Bold. 맑은 고딕보다 자간이 넓고 획이 둥글어 쇼츠에 어울린다.
# 시스템에 없으면 libass가 대체 폰트를 쓰므로 FONT_FALLBACK을 함께 지정한다.
FONT = "Han Santteut Dotum"
FONT_FALLBACK = "Malgun Gothic"

# 파스텔 팔레트 (ASS는 BGR 순서. &HAABBGGRR 형식에서 AA=00이 불투명)
#   형광색 대신 채도를 낮춘 색을 쓴다. 어두운 영상 위에서도 눈이 편하다.
C_WHITE = "&H00FFFFFF"     # 기본 흰색
C_CREAM = "&H00E8F4FF"     # 크림 (아주 옅은 노랑) — 제목
C_MINT = "&H00D4F0D0"      # 민트 — 기능·스펙
C_CORAL = "&H00A0A0FF"     # 코랄 (연한 분홍빨강) — 혜택·가격
C_LAVENDER = "&H00F0D8C0"  # 라벤더 (연한 파랑보라) — 시연 결과
C_INK = "&H00302820"       # 외곽선 (완전 검정 대신 살짝 따뜻한 먹색)
C_SHADOW = "&H60000000"    # 그림자 (반투명)

# 숫자·핵심어 강조색 — 코랄. 형광 주황보다 부드럽다.
HIGHLIGHT_COLOR = "&H007090FF&"

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,{font},78,{cream},{white},{ink},{shadow},-1,0,0,0,100,100,3,0,1,5,3,8,60,60,{title_mv},1
Style: Speech,{font},48,{white},{white},{ink},&H90000000,0,0,0,0,100,100,0,0,3,7,0,2,80,80,{speech_mv},1
Style: Point,{font},80,{white},{white},{ink},{shadow},-1,0,0,0,100,100,2,0,1,6,3,2,60,60,{point_mv},1
Style: Spec,{font},80,{mint},{white},{ink},{shadow},-1,0,0,0,100,100,2,0,1,6,3,2,60,60,{point_mv},1
Style: Benefit,{font},86,{coral},{white},{ink},{shadow},-1,0,0,0,102,102,2,0,1,6,3,2,60,60,{point_mv},1
Style: Result,{font},82,{lavender},{white},{ink},{shadow},-1,0,0,0,100,100,2,0,1,6,3,2,60,60,{point_mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# 강조 유형 → 스타일. 색으로 정보 종류를 구분한다.
#   spec/feature 민트 (사실·기능)  benefit 코랄 (혜택·가격)  result 라벤더 (시연 결과)
EMPHASIS_STYLE = {"spec": "Spec", "feature": "Spec",
                  "benefit": "Benefit", "result": "Result", "story": "Point"}

# 자막 세로 위치 (PlayResY=1920 기준)
#   Title  상단 고정 (제목)
#   Point  중하단 (강조 문구) — 눈이 가장 먼저 가는 자리
#   Speech 최하단 작게 (발화 따라감) — 포인트 자막과 겹치지 않게 아래로
MARGIN_CROP = {"title_mv": 190, "point_mv": 470, "speech_mv": 210}
MARGIN_LETTERBOX = {"title_mv": 230, "point_mv": 400, "speech_mv": 120}


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
              layout: str = "crop", speech_cues: list | None = None, seg_start_ms: int = 0) -> Path:
    """포인트 자막 + (선택) 상단 고정 제목. title은 구간 전체(0 ~ duration_ms)에 표시.

    layout: "crop"(9:16 꽉 채움) | "letterbox"(원본 비율 유지, 위아래 여백) — 자막 세로 위치가 달라진다."""
    margins = MARGIN_LETTERBOX if layout == "letterbox" else MARGIN_CROP
    lines = [ASS_HEADER.format(
        font=FONT, white=C_WHITE, cream=C_CREAM, mint=C_MINT,
        coral=C_CORAL, lavender=C_LAVENDER, ink=C_INK, shadow=C_SHADOW,
        **margins)]
    if title:
        end = duration_ms if duration_ms else max((c.end_ms for c in captions if c.end_ms), default=0) + 10_000
        lines.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(end)},Title,,0,0,0,,{_ass_escape(title)}\n")
    # 전체 자막 — 발화를 따라가는 작은 자막 (하단). 시청자가 내용을 놓치지 않게 한다.
    for cue in (speech_cues or []):
        st = cue.start_ms - seg_start_ms
        en = cue.end_ms - seg_start_ms
        if en <= 0 or (duration_ms and st >= duration_ms):
            continue
        st = max(st, 0)
        if duration_ms:
            en = min(en, duration_ms)
        text = _ass_escape(cue.text)
        if len(text) > 40:                       # 너무 길면 두 줄로 나눈다
            mid = text.rfind(" ", 0, len(text) // 2 + 8)
            if mid > 10:
                text = text[:mid] + "\\N" + text[mid + 1:]
        lines.append(f"Dialogue: 0,{_ass_time(st)},{_ass_time(en)},Speech,,0,0,0,,{text}\n")

    # 포인트 자막 — 크게, 색으로 강조
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
