"""파이프라인 CLI.

  python -m poc.pipeline demo                          # 목데이터 + 테스트 영상으로 전 과정 검증 (API 불필요)
  python -m poc.pipeline stt --video 방송.mp4 --out out/transcript.json [--model small]
  python -m poc.pipeline p2 --comments data/mock_comments.json
  python -m poc.pipeline m1 --transcript ... --cuesheet ... --out out/segments.json [--mock]
  python -m poc.pipeline m2 --segments out/segments.json --pick P1 --transcript ... --terms ... --outdir out [--mock]
  python -m poc.pipeline render --video 방송.mp4 --captions out/p1_captions.json --out out/short_p1.mp4
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):  # Windows 콘솔(cp949)에서 한글·기호 출력 깨짐 방지
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from poc import comments as comments_mod
from poc import m1_segments, m2_captions, render
from poc.gates import format_report, has_errors
from poc.llm import LLM
from poc.transcript import load_cues

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "out"


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """.env가 있으면 환경변수로 로드 (이미 설정된 값은 유지). python-dotenv 없이 단순 파싱."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and k not in os.environ:
            os.environ[k] = v


_load_dotenv()


def _load_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _terms_prompt(terms_path: str | None) -> str | None:
    """상품 용어 목록 → STT 힌트 프롬프트 (제품명·고유명사 인식 보조)."""
    if not terms_path:
        return None
    terms = _load_json(terms_path)
    words = [terms.get("product_name", "")] + [t["canonical"] for t in terms.get("terms", [])]
    return ", ".join(w for w in words if w)


def cmd_stt(args):
    t0 = time.time()
    if args.engine == "whisper":
        from poc.stt_whisper import transcribe

        cues = transcribe(
            args.video, args.out, model_size=args.model or "large-v3", device=args.device,
            initial_prompt=_terms_prompt(args.terms),
        )
    else:
        from poc.stt_google import transcribe

        cues = transcribe(args.video, args.out, model=args.model or "chirp_3", terms_path=args.terms)
    print(f"STT 완료: 큐 {len(cues)}개 → {args.out} ({time.time()-t0:.0f}초 소요)")


def cmd_p2(args):
    cs = comments_mod.load_comments(args.comments)
    windows = comments_mod.find_p2_windows(cs, min_count=args.min_count)
    print(json.dumps(windows, ensure_ascii=False, indent=2))


def cmd_m1(args):
    cues = load_cues(args.transcript)
    cuesheet = _load_json(args.cuesheet) if args.cuesheet else []
    llm = LLM(mock_file=DATA / "mock_llm" / "m1_response.json" if args.mock else None, tag="m1")
    segments, violations = m1_segments.run_m1(cues, cuesheet, llm, out_path=args.out)
    print(f"M1 완료 ({llm.model}): 구간 {len(segments)}개 → {args.out}")
    for sg in segments:
        dur = (sg.end_ms - sg.start_ms) / 1000 if sg.end_ms else 0
        print(f"  {sg.part_type} [{sg.start_cue_id}~{sg.end_cue_id}] {sg.start_ms/1000:.0f}~{sg.end_ms/1000:.0f}초 ({dur:.0f}초) {sg.label}")
    print(format_report(violations))


def cmd_m2(args):
    cues = load_cues(args.transcript)
    terms = _load_json(args.terms)
    segments = m1_segments.load_segments(args.segments)
    target = next((s for s in segments if s.part_type == args.pick), None)
    if target is None:
        raise SystemExit(f"선택한 파트 {args.pick} 구간이 없습니다")
    llm = LLM(mock_file=DATA / "mock_llm" / "m2_response.json" if args.mock else None, tag=f"m2-{args.pick}")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    cap_path = outdir / f"{args.pick.lower()}_captions.json"
    captions, violations = m2_captions.run_m2(target, cues, terms, llm, out_path=cap_path)
    ass_path = outdir / f"{args.pick.lower()}.ass"
    render.build_ass(captions, ass_path)
    print(f"M2 완료: 자막 {len(captions)}개 → {cap_path}, {ass_path}")
    print(format_report(violations))


def cmd_render(args):
    seg, captions = m2_captions.load_captions(args.captions)
    ass_path = Path(args.captions).with_suffix(".ass")
    if not ass_path.exists():
        render.build_ass(captions, ass_path)
    t0 = time.time()
    out = render.render_short(args.video, seg.start_ms, seg.end_ms, ass_path, args.out, crop_cx=args.crop_cx)
    thumb = Path(args.out).with_suffix(".jpg")
    render.thumbnail(args.video, seg.start_ms, thumb)
    size_mb = Path(out).stat().st_size / 1024 / 1024
    print(f"렌더링 완료: {out} ({size_mb:.1f}MB, {time.time()-t0:.0f}초 소요), 썸네일 {thumb}")


def cmd_demo(args):
    """목데이터 → M1 → P2 → M2 → 테스트 영상 렌더링까지 전 과정 검증."""
    OUT.mkdir(exist_ok=True)
    cues = load_cues(DATA / "mock_transcript.json")
    cuesheet = _load_json(DATA / "mock_cuesheet.json")
    terms = _load_json(DATA / "product_terms.json")

    print("=" * 60)
    print("[1/5] M1 구간 분할 (mock LLM)")
    llm1 = LLM(mock_file=DATA / "mock_llm" / "m1_response.json")
    segments, v1 = m1_segments.run_m1(cues, cuesheet, llm1, out_path=OUT / "segments.json")
    for s in segments:
        print(f"  {s.part_type} {s.label}: {s.start_ms/1000:.0f}~{s.end_ms/1000:.0f}초 ({(s.end_ms-s.start_ms)/1000:.0f}초)")
    print("  " + format_report(v1).replace("\n", "\n  "))

    print("[2/5] P2 질문 집중 구간 (댓글 수 세기 — 코드)")
    p2 = comments_mod.find_p2_windows(comments_mod.load_comments(DATA / "mock_comments.json"))
    for w in p2:
        print(f"  P2: {w['start_ms']/1000:.0f}~{w['end_ms']/1000:.0f}초 (댓글 {w['comment_count']}개/분)")

    print("[3/5] M2 포인트 자막 — P1 선택 가정 (mock LLM)")
    p1 = next(s for s in segments if s.part_type == "P1")
    llm2 = LLM(mock_file=DATA / "mock_llm" / "m2_response.json")
    captions, v2 = m2_captions.run_m2(p1, cues, terms, llm2, out_path=OUT / "p1_captions.json")
    for c in captions:
        print(f"  [{c.start_ms/1000:5.1f}s ~ {c.end_ms/1000:5.1f}s] {c.text} ({c.emphasis})")
    print("  " + format_report(v2).replace("\n", "\n  "))
    ass = render.build_ass(captions, OUT / "p1_captions.ass")

    test_video = OUT / "test_video.mp4"
    if test_video.exists() and not args.remake_video:
        print(f"[4/5] 테스트 영상 재사용: {test_video}")
    else:
        print("[4/5] 테스트 영상 생성 중 (540초, testsrc)...")
        t0 = time.time()
        render.make_test_video(540, test_video)
        print(f"  생성 완료 ({time.time()-t0:.0f}초 소요)")

    print("[5/5] 쇼츠 렌더링 (컷 + 9:16 크롭 + 자막 번인)...")
    t0 = time.time()
    out = render.render_short(test_video, p1.start_ms, p1.end_ms, ass, OUT / "short_p1.mp4")
    render.thumbnail(test_video, p1.start_ms, OUT / "short_p1_thumb.jpg")
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"  완료: {out} ({size_mb:.1f}MB, {time.time()-t0:.0f}초 소요)")

    print("=" * 60)
    ok = not (has_errors(v1) or has_errors(v2))
    print("데모 결과:", "성공 — 전체 파이프라인 동작 확인" if ok else "게이트 ERROR 있음 — 위 로그 확인")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(prog="poc.pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stt", help="STT (whisper 로컬 / google 클라우드)")
    s.add_argument("--video", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--engine", choices=["whisper", "google"], default="whisper")
    s.add_argument("--model", default=None, help="whisper: large-v3(기본)/small 등, google: chirp_3(기본)/long 등")
    s.add_argument("--device", choices=["auto", "cpu"], default="auto", help="whisper 전용")
    s.add_argument("--terms", help="상품 용어 목록 JSON — 제품명·용어를 STT 힌트로 주입")
    s.set_defaults(fn=cmd_stt)

    s = sub.add_parser("p2", help="댓글 수 기반 P2 구간 탐지")
    s.add_argument("--comments", required=True)
    s.add_argument("--min-count", type=int, default=8)
    s.set_defaults(fn=cmd_p2)

    s = sub.add_parser("m1", help="구간 분할")
    s.add_argument("--transcript", required=True)
    s.add_argument("--cuesheet")
    s.add_argument("--out", required=True)
    s.add_argument("--mock", action="store_true")
    s.set_defaults(fn=cmd_m1)

    s = sub.add_parser("m2", help="포인트 자막")
    s.add_argument("--segments", required=True)
    s.add_argument("--pick", required=True, help="파트 유형 예: P1")
    s.add_argument("--transcript", required=True)
    s.add_argument("--terms", required=True)
    s.add_argument("--outdir", default=str(OUT))
    s.add_argument("--mock", action="store_true")
    s.set_defaults(fn=cmd_m2)

    s = sub.add_parser("render", help="쇼츠 렌더링")
    s.add_argument("--video", required=True)
    s.add_argument("--captions", required=True, help="m2가 저장한 *_captions.json")
    s.add_argument("--out", required=True)
    s.add_argument("--crop-cx", type=float, default=0.5, help="세로 크롭 중심 가로 위치 0~1 (기본 0.5=중앙)")
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser("demo", help="목데이터로 전 과정 검증")
    s.add_argument("--remake-video", action="store_true")
    s.set_defaults(fn=cmd_demo)

    args = p.parse_args()
    raise SystemExit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
