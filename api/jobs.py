"""작업 실행 — 업로드된 방송을 후보 구간까지 처리하고, 선택 후 쇼츠를 만든다.

처리 정책 (사용자 결정, 2026-09-04):
  요청이 들어올 때만 전체 파이프라인을 돌린다. 사전 생성은 하지 않는다.
  대신 같은 영상을 다시 요청하면 STT 결과를 재사용한다 —
  STT가 전체의 90%(20분 영상에 5분)라 재사용 효과가 크다.
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import asdict
from pathlib import Path

from api import settings, storage
from api.schemas import JobStatus, ScreenResult
from api.storage import JobPaths

# 작업 상태는 메모리에 두고, 스냅샷을 job.json으로 남긴다 (서버 재시작 시 조회용).
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
# STT가 GPU를 점유하므로 동시 실행을 제한한다.
_SLOTS = threading.Semaphore(settings.MAX_CONCURRENT_JOBS)


# ── 상태 관리 ─────────────────────────────────────────────────────────
def _set(job_id: str, **fields) -> dict:
    with _LOCK:
        job = _JOBS.setdefault(job_id, {"job_id": job_id})
        job.update(fields)
        snapshot = dict(job)
    paths = JobPaths(job_id)
    if paths.root.exists():
        try:
            storage.write_json(paths.state, snapshot)
        except OSError:
            pass
    return snapshot


def get(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            return dict(job)
    # 서버 재시작 후에도 완료된 작업은 조회할 수 있게 파일에서 복구
    state = JobPaths(job_id).state
    if state.exists():
        try:
            return storage.read_json(state)
        except (OSError, ValueError):
            return None
    return None


def active_count() -> int:
    running = {JobStatus.SCREENING, JobStatus.TRANSCRIBING,
               JobStatus.SEGMENTING, JobStatus.RENDERING}
    with _LOCK:
        return sum(1 for j in _JOBS.values() if j.get("status") in running)


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


# ── 소재 적합성 판정 (범위 3) ─────────────────────────────────────────
def screen_video(video: Path, out_json: Path | None = None) -> ScreenResult:
    """분당 장면 전환 수로 쇼츠 소재 적합성을 판정한다.

    실측: 분당 2.1회 영상은 쇼츠에서 화면이 멈춘 것처럼 보였고,
          분당 45~47회 영상은 정상이었다."""
    from poc import motion as motion_mod

    r = motion_mod.analyze(video, out_json)
    cpm = r["cuts_per_min"]
    suitable = cpm >= settings.MIN_CUTS_PER_MIN
    reason = None
    if not suitable:
        reason = (f"장면 전환이 분당 {cpm:.1f}회로 기준({settings.MIN_CUTS_PER_MIN:.0f}회) 미만입니다. "
                  f"쇼츠로 만들면 화면이 멈춘 것처럼 보입니다.")
    return ScreenResult(
        suitable=suitable,
        duration_sec=r["duration_sec"],
        total_cuts=r["total_cuts"],
        cuts_per_min=cpm,
        moving_sec=r["moving_sec"],
        still_pct=r["still_pct"],
        threshold=settings.MIN_CUTS_PER_MIN,
        reason=reason,
    )


# ── 1단계: 업로드 → 후보 구간 ─────────────────────────────────────────
def run_analysis(job_id: str) -> None:
    """소재 판정 → STT → P2 → 구간 분할까지. 판매자 선택을 기다리며 멈춘다."""
    paths = JobPaths(job_id)
    t0 = time.time()
    with _SLOTS:
        try:
            _analysis_stages(job_id, paths, t0)
        except Exception as e:  # noqa: BLE001 — 어떤 실패든 작업 상태로 보고한다
            _set(job_id, status=JobStatus.FAILED,
                 error=f"{type(e).__name__}: {e}",
                 traceback=traceback.format_exc()[-2000:],
                 elapsed_sec=round(time.time() - t0, 1))


def _analysis_stages(job_id: str, paths: JobPaths, t0: float) -> None:
    from poc import comments as comments_mod
    from poc import m1_segments
    from poc.llm import LLM
    from poc.transcript import load_cues

    # ① 소재 적합성 — 부적합하면 여기서 멈춘다 (STT 5분을 아낀다)
    _set(job_id, status=JobStatus.SCREENING, stage_detail="영상 움직임 분석", progress=0.05)
    screen = screen_video(paths.video, paths.motion)
    if not screen.suitable:
        _set(job_id, status=JobStatus.REJECTED, screen=screen.model_dump(),
             stage_detail="소재 부적합", progress=1.0,
             elapsed_sec=round(time.time() - t0, 1))
        return
    _set(job_id, screen=screen.model_dump(), progress=0.1)

    # ② STT — 같은 영상을 이미 처리했으면 재사용
    fingerprint = storage.video_fingerprint(paths.video)
    _set(job_id, video_fingerprint=fingerprint)
    cached = storage.find_cached_transcript(fingerprint, exclude_job=job_id)
    if cached and cached.exists():
        _set(job_id, status=JobStatus.TRANSCRIBING,
             stage_detail="이전 음성 인식 결과 재사용", progress=0.15)
        paths.transcript.write_text(cached.read_text(encoding="utf-8"), encoding="utf-8")
        _set(job_id, stt_reused=True, progress=0.6)
    else:
        _set(job_id, status=JobStatus.TRANSCRIBING,
             stage_detail=f"음성 인식 ({settings.STT_MODEL})", progress=0.15)
        from poc.stt_whisper import transcribe

        initial_prompt = _terms_prompt(paths.terms)
        transcribe(paths.video, paths.transcript,
                   model_size=settings.STT_MODEL, initial_prompt=initial_prompt)
        _set(job_id, stt_reused=False, progress=0.6)

    cues = load_cues(paths.transcript)

    # ③ P2 — 댓글이 있으면 질문 집중 구간을 코드로 찾는다 (범위 2)
    p2_windows: list[dict] = []
    if paths.comments.exists():
        _set(job_id, stage_detail="질문 집중 구간 탐지", progress=0.65)
        p2_windows = comments_mod.find_p2_windows(
            comments_mod.load_comments(paths.comments),
            window_ms=settings.P2_WINDOW_MS,
            step_ms=settings.P2_STEP_MS,
            min_count=settings.P2_MIN_COMMENTS,
        )

    # ④ 구간 분할 — 움직임 정보를 함께 넘겨 정지 구간을 피하게 한다
    _set(job_id, status=JobStatus.SEGMENTING,
         stage_detail=f"구간 분할 ({settings.LLM_MODEL})", progress=0.7)
    from poc import motion as motion_mod

    motion, cuts = motion_mod.load_full(paths.motion)
    llm = LLM(model=settings.LLM_MODEL, tag=f"m1-{job_id}")
    segments, violations = m1_segments.run_m1(
        cues, [], llm, out_path=paths.segments, motion=motion, cuts=cuts)

    # ⑤ 다시보기 타임라인 — 방송 전체를 챕터로 나눈다 (쇼츠 후보와 별개)
    _set(job_id, status=JobStatus.TIMELINE,
         stage_detail="다시보기 타임라인 생성", progress=0.8)
    chapter_count = 0
    try:
        from poc import timeline as timeline_mod

        tl_llm = LLM(model=settings.LLM_MODEL, tag=f"timeline-{job_id}")
        chapters, tl_warnings = timeline_mod.run_timeline(
            cues, tl_llm, out_path=paths.timeline,
            comments=comments_mod.load_comments(paths.comments) if paths.comments.exists() else None)
        chapter_count = len(chapters)
    except Exception as e:  # noqa: BLE001 — 타임라인 실패가 쇼츠 생성을 막지는 않는다
        _set(job_id, timeline_error=f"{type(e).__name__}: {e}")

    # ⑥ 후보 목록 구성 + 썸네일
    _set(job_id, stage_detail="후보 썸네일 생성", progress=0.9)
    candidates = _build_candidates(paths, segments, violations, p2_windows)
    storage.write_json(paths.root / "candidates.json", candidates)

    _set(job_id, status=JobStatus.READY_FOR_SELECTION,
         stage_detail="판매자 선택 대기", progress=1.0,
         candidate_count=len(candidates), chapter_count=chapter_count,
         elapsed_sec=round(time.time() - t0, 1))


def _terms_prompt(terms_path: Path) -> str | None:
    """상품 용어를 STT 힌트로 넘긴다 (제품명·수치 인식 보조)."""
    if not terms_path.exists():
        return None
    try:
        terms = storage.read_json(terms_path)
    except (OSError, ValueError):
        return None
    words = [terms.get("product_name", "")] + [t.get("canonical", "") for t in terms.get("terms", [])]
    joined = ", ".join(w for w in words if w)
    return joined or None


def _build_candidates(paths: JobPaths, segments, violations, p2_windows) -> list[dict]:
    """모델 구간 + 댓글 기반 P2 구간을 하나의 후보 목록으로 합친다."""
    from poc.models import PART_TYPES
    from poc import render

    def warns_for(idx: int) -> list[dict]:
        prefix = f"segments[{idx}]"
        return [asdict(v) for v in violations if v.message.startswith(prefix)]

    out: list[dict] = []
    for i, s in enumerate(segments):
        if s.start_ms is None or s.end_ms is None:
            continue  # cue_id를 못 찾은 구간 — 게이트가 ERROR로 잡았을 것
        cid = f"seg_{i}"
        out.append({
            "id": cid,
            "part_type": s.part_type,
            "part_name": PART_TYPES.get(s.part_type, s.part_type),
            "label": s.label,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "duration_sec": round((s.end_ms - s.start_ms) / 1000, 1),
            "source": "model",
            "evidence": s.evidence[:5],
            "warnings": warns_for(i),
            "start_cue_id": s.start_cue_id,
            "end_cue_id": s.end_cue_id,
        })

    for j, w in enumerate(p2_windows):
        out.append({
            "id": f"p2_{j}",
            "part_type": "P2",
            "part_name": PART_TYPES.get("P2", "질문 집중"),
            "label": f"질문 집중 ({w['comment_count']}건)",
            "start_ms": w["start_ms"],
            "end_ms": w["end_ms"],
            "duration_sec": round((w["end_ms"] - w["start_ms"]) / 1000, 1),
            "source": "comments",
            "evidence": [],
            "comment_count": w["comment_count"],
            "warnings": [],
        })

    out.sort(key=lambda c: c["start_ms"])

    # 카드 UI용 썸네일 — 구간 시작 2초 뒤 프레임
    for c in out:
        try:
            render.thumbnail(paths.video, c["start_ms"] + 2000, paths.thumb(c["id"]))
        except Exception:  # noqa: BLE001 — 썸네일 실패가 작업 전체를 막지는 않는다
            pass
    return out


# ── 2단계: 선택 → 쇼츠 생성 ───────────────────────────────────────────
def run_render(job_id: str, candidate_ids: list[str], layout: str,
               bg_blur: bool, crop_cx: float) -> None:
    paths = JobPaths(job_id)
    t0 = time.time()
    with _SLOTS:
        try:
            _render_stages(job_id, paths, candidate_ids, layout, bg_blur, crop_cx, t0)
        except Exception as e:  # noqa: BLE001
            _set(job_id, status=JobStatus.FAILED,
                 error=f"{type(e).__name__}: {e}",
                 traceback=traceback.format_exc()[-2000:],
                 elapsed_sec=round(time.time() - t0, 1))


def _render_stages(job_id: str, paths: JobPaths, candidate_ids: list[str],
                   layout: str, bg_blur: bool, crop_cx: float, t0: float) -> None:
    from poc import m2_captions, render
    from poc.llm import LLM
    from poc.models import Segment
    from poc.transcript import load_cues

    cues = load_cues(paths.transcript)
    terms = storage.read_json(paths.terms) if paths.terms.exists() else {}
    all_candidates = {c["id"]: c for c in storage.read_json(paths.root / "candidates.json")}

    results: list[dict] = []
    total = len(candidate_ids)
    for n, cid in enumerate(candidate_ids, 1):
        cand = all_candidates.get(cid)
        if cand is None:
            continue
        _set(job_id, status=JobStatus.RENDERING,
             stage_detail=f"{cid} 자막 생성 ({n}/{total})",
             progress=round((n - 1) / max(total, 1) * 0.9, 2))

        seg = _segment_for(cand, cues)
        out_dir = paths.prepare_short(cid)
        llm = LLM(model=settings.LLM_MODEL, tag=f"m2-{cid}-{job_id}")
        captions, violations = m2_captions.run_m2(
            seg, cues, terms, llm, out_path=paths.captions(cid))

        title = m2_captions.load_title(paths.captions(cid))
        duration_ms = seg.end_ms - seg.start_ms
        # 전체 자막 — 구간 안의 발화를 그대로 따라가는 작은 자막
        speech = [c for c in cues if c.end_ms > seg.start_ms and c.start_ms < seg.end_ms]
        render.build_ass(captions, paths.ass(cid), title=title,
                         duration_ms=duration_ms, layout=layout,
                         speech_cues=speech, seg_start_ms=seg.start_ms)

        _set(job_id, stage_detail=f"{cid} 렌더링 ({n}/{total})",
             progress=round((n - 0.5) / max(total, 1) * 0.9, 2))
        render.render_short(paths.video, seg.start_ms, seg.end_ms,
                            paths.ass(cid), paths.short_video(cid),
                            crop_cx=crop_cx, layout=layout, bg_blur=bg_blur)
        try:
            render.thumbnail(paths.video, seg.start_ms + 2000, paths.short_thumb(cid))
        except Exception:  # noqa: BLE001
            pass

        video_file = paths.short_video(cid)
        results.append({
            "candidate_id": cid,
            "part_type": cand["part_type"],
            "title": title,
            "duration_sec": round(duration_ms / 1000, 1),
            "size_bytes": video_file.stat().st_size if video_file.exists() else 0,
            "captions": [
                {"text": c.text, "highlight": c.highlight, "emphasis": c.emphasis,
                 "start_ms": c.start_ms, "end_ms": c.end_ms}
                for c in captions
            ],
            "violations": [asdict(v) for v in violations],
        })

    storage.write_json(paths.root / "shorts.json", results)
    _set(job_id, status=JobStatus.DONE, stage_detail="완료", progress=1.0,
         short_count=len(results), elapsed_sec=round(time.time() - t0, 1))


def _segment_for(cand: dict, cues) -> "Segment":
    """후보 정보를 run_m2가 받는 Segment로 되돌린다.

    P2 구간은 모델이 만든 게 아니라 cue_id가 없으므로, 시각으로 가장 가까운 큐를 찾는다."""
    from poc.models import Segment

    start_cue = cand.get("start_cue_id")
    end_cue = cand.get("end_cue_id")
    if not start_cue or not end_cue:
        inside = [c for c in cues
                  if c.start_ms >= cand["start_ms"] - 500 and c.end_ms <= cand["end_ms"] + 500]
        if not inside:  # 창 안에 완전히 들어가는 큐가 없으면 겹치는 큐로 넓힌다
            inside = [c for c in cues
                      if c.end_ms > cand["start_ms"] and c.start_ms < cand["end_ms"]]
        if not inside:
            raise ValueError(f"{cand['id']} 구간에 해당하는 발화가 없습니다")
        start_cue, end_cue = inside[0].cue_id, inside[-1].cue_id

    seg = Segment(part_type=cand["part_type"], start_cue_id=start_cue,
                  end_cue_id=end_cue, label=cand["label"], evidence=cand.get("evidence", []))
    idx = {c.cue_id: c for c in cues}
    seg.start_ms = idx[start_cue].start_ms
    seg.end_ms = idx[end_cue].end_ms
    return seg
