"""하이라이트 쇼츠 자동화 API.

  uvicorn api.main:app --reload

흐름 (Base: /api/v1/ai)
  POST /api/v1/ai/jobs                  영상 업로드 → 소재판정·STT·구간분할 (백그라운드)
  GET  /api/v1/ai/jobs/{id}             진행 상태
  GET  /api/v1/ai/jobs/{id}/timeline    다시보기 타임라인
  GET  /api/v1/ai/jobs/{id}/candidates  구간 후보 (판매자 선택 화면용)
  POST /api/v1/ai/jobs/{id}/select      고른 구간으로 쇼츠 생성 (백그라운드)
  GET  /api/v1/ai/jobs/{id}/shorts      완성된 쇼츠
  GET  /api/v1/ai/files/{id}/{path}     결과 파일 서빙
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
import tempfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from api import jobs, settings, storage
from api.schemas import (
    Candidate,
    CandidateList,
    Chapter,
    HealthCheck,
    JobCreated,
    JobState,
    JobStatus,
    ScreenResult,
    SelectRequest,
    ShortList,
    ShortOut,
    TimelineOut,
    TitleUpdate,
    HighlightRequest,
    HighlightAccepted,
    HighlightJobState,
)
from api.storage import JobPaths
from api.viewer import VIEWER_HTML

settings.ensure_runtime_env()

app = FastAPI(
    title="하이라이트 쇼츠 자동화 API",
    description="라이브 방송을 쇼츠로 자동 변환한다. "
                f"구간 분할·자막은 {settings.LLM_MODEL}, 음성 인식은 Whisper {settings.STT_MODEL}.",
    version="0.1.0",
)

# 전사 공통 규칙: URL 경로 버저닝 (/api/v1/...). AI 서비스는 /api/v1/ai 하위.
# 응답에 담기는 파일 URL도 같은 접두를 써야 하므로 storage 와 값을 공유한다.
BASE = storage.API_BASE

_log = logging.getLogger("api.main")


@app.on_event("startup")
def _startup() -> None:
    """기동 시 만료된 작업을 정리하고, 이후 주기적으로 반복한다.

    영상 원본이 방송당 수백 MB라 쌓이면 디스크가 찬다."""
    if settings.RETENTION_HOURS <= 0:
        _log.info("보관 정책 미적용 — 작업 산출물을 지우지 않는다")
        return

    def sweep() -> None:
        while True:
            try:
                removed = storage.purge_expired(settings.RETENTION_HOURS)
                if removed:
                    _log.info("만료 작업 정리",
                              extra={"removed": len(removed),
                                     "retention_hours": settings.RETENTION_HOURS})
            except Exception as e:  # noqa: BLE001 — 정리 실패가 서버를 죽이면 안 된다
                _log.error("작업 정리 실패", extra={"error": f"{type(e).__name__}: {e}"})
            time.sleep(3600)

    threading.Thread(target=sweep, daemon=True, name="purge-expired").start()
    _log.info("작업 산출물 보관 정책 적용",
              extra={"retention_hours": settings.RETENTION_HOURS})


# ── 헬스체크 ──────────────────────────────────────────────────────────
@app.get(BASE + "/health", response_model=HealthCheck)
def health() -> HealthCheck:
    import os

    notes: list[str] = []

    ffmpeg = shutil.which("ffmpeg") is not None
    if not ffmpeg:
        notes.append("ffmpeg를 찾을 수 없습니다. 렌더링이 불가능합니다.")

    try:
        import faster_whisper  # noqa: F401
        whisper_ok = True
    except ImportError:
        whisper_ok = False
        notes.append("faster-whisper가 없습니다. pip install faster-whisper")

    gemini_key = bool(os.environ.get("GEMINI_API_KEY"))
    if not gemini_key:
        notes.append("GEMINI_API_KEY가 없습니다. .env를 확인하세요.")

    gpu = None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            gpu = r.stdout.strip().splitlines()[0] if r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        pass
    if gpu is None:
        notes.append("GPU를 찾을 수 없습니다. 음성 인식이 CPU로 돌아 느려집니다.")

    return HealthCheck(
        ok=ffmpeg and whisper_ok and gemini_key,
        ffmpeg=ffmpeg, whisper=whisper_ok, gemini_key=gemini_key,
        llm_model=settings.LLM_MODEL, stt_model=settings.STT_MODEL,
        gpu=gpu, active_jobs=jobs.active_count(), notes=notes,
    )


# ── 소재 적합성만 사전 확인 ───────────────────────────────────────────
@app.post(BASE + "/screen", response_model=ScreenResult)
def screen(video: UploadFile = File(...)) -> ScreenResult:
    """영상만 넣어 쇼츠 소재로 쓸 만한지 판정한다. STT를 돌리지 않아 빠르다."""
    tmp = Path(tempfile.mkdtemp(prefix="screen_"))
    try:
        dst = tmp / "video.mp4"
        with dst.open("wb") as f:
            shutil.copyfileobj(video.file, f)
        return jobs.screen_video(dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 1단계: 업로드 → 후보 구간 ─────────────────────────────────────────
@app.post(BASE + "/jobs", response_model=JobCreated, status_code=202)
def create_job(
    background: BackgroundTasks,
    video: UploadFile = File(..., description="방송 영상"),
    terms: UploadFile | None = File(None, description="상품 용어 목록 JSON"),
    comments: UploadFile | None = File(None, description="라이브 채팅 JSON"),
    broadcast_start_ms: int | None = Form(
        None,
        description="방송 시작 시각(epoch ms). 채팅이 절대시각으로 오는 경우에만 필요하다. "
                    "채팅 시각이 이미 방송 시작 기준 경과시간(ms)이면 생략한다.",
    ),
) -> JobCreated:
    job_id = jobs.new_job_id()
    paths = JobPaths(job_id)
    paths.prepare()

    with paths.video.open("wb") as f:
        shutil.copyfileobj(video.file, f)
    for upload, dst in ((terms, paths.terms), (comments, paths.comments)):
        if upload is not None:
            with dst.open("wb") as f:
                shutil.copyfileobj(upload.file, f)

    jobs._set(job_id, status=JobStatus.QUEUED, stage_detail="대기 중", progress=0.0,
              filename=video.filename, has_terms=terms is not None,
              has_comments=comments is not None,
              broadcast_start_ms=broadcast_start_ms)
    background.add_task(jobs.run_analysis, job_id)
    return JobCreated(job_id=job_id, status=JobStatus.QUEUED,
                      message="접수했습니다. GET /api/v1/ai/jobs/{job_id}로 진행 상태를 확인하세요.")


@app.get(BASE + "/jobs/{job_id}", response_model=JobState)
def job_state(job_id: str) -> JobState:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"작업을 찾을 수 없습니다: {job_id}")
    screen = job.get("screen")
    return JobState(
        job_id=job_id,
        status=job.get("status", JobStatus.QUEUED),
        stage_detail=job.get("stage_detail"),
        progress=job.get("progress", 0.0),
        error=job.get("error"),
        screen=ScreenResult(**screen) if screen else None,
        candidate_count=job.get("candidate_count"),
        chapter_count=job.get("chapter_count"),
        short_count=job.get("short_count"),
        elapsed_sec=job.get("elapsed_sec"),
        stt_reused=job.get("stt_reused", False),
    )


@app.get(BASE + "/jobs/{job_id}/candidates", response_model=CandidateList)
def candidates(job_id: str) -> CandidateList:
    """판매자 선택 화면용 후보 목록."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"작업을 찾을 수 없습니다: {job_id}")
    status = job.get("status")
    if status == JobStatus.REJECTED:
        raise HTTPException(409, (job.get("screen") or {}).get("reason", "소재로 쓸 수 없는 영상입니다"))
    if status in (JobStatus.QUEUED, JobStatus.SCREENING,
                  JobStatus.TRANSCRIBING, JobStatus.SEGMENTING):
        raise HTTPException(409, f"아직 처리 중입니다 (상태: {status})")

    paths = JobPaths(job_id)
    f = paths.root / "candidates.json"
    if not f.exists():
        raise HTTPException(404, "후보 목록이 아직 없습니다")

    items = []
    for c in storage.read_json(f):
        thumb = paths.thumb(c["id"])
        items.append(Candidate(**c, thumbnail_url=paths.url(thumb) if thumb.exists() else None))
    return CandidateList(job_id=job_id, candidates=items)


@app.get(BASE + "/jobs/{job_id}/timeline", response_model=TimelineOut)
def timeline(job_id: str) -> TimelineOut:
    """다시보기 타임라인 — 방송 전체를 주제별 챕터로 나눈 목록.

    쇼츠 후보(/candidates)와 다르다. 후보는 잘라 쓸 구간만 고르지만,
    타임라인은 방송 전체를 빈틈없이 덮는다."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"작업을 찾을 수 없습니다: {job_id}")
    f = JobPaths(job_id).timeline
    if not f.exists():
        raise HTTPException(409, f"타임라인이 아직 없습니다 (상태: {job.get('status')})")

    data = storage.read_json(f)
    chapters = [Chapter(
        start_ms=c["start_ms"], end_ms=c["end_ms"], timestamp=c["timestamp"],
        duration_sec=round((c["end_ms"] - c["start_ms"]) / 1000, 1),
        title=c["title"], category=c["category"],
        category_name=c["category_name"], summary=c.get("summary", ""),
    ) for c in data["chapters"]]
    return TimelineOut(job_id=job_id, chapters=chapters,
                       warnings=data.get("warnings", []))


# ── 2단계: 선택 → 쇼츠 ────────────────────────────────────────────────
@app.post(BASE + "/jobs/{job_id}/select", response_model=JobState, status_code=202)
def select(job_id: str, req: SelectRequest, background: BackgroundTasks) -> JobState:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"작업을 찾을 수 없습니다: {job_id}")
    if job.get("status") not in (JobStatus.READY_FOR_SELECTION, JobStatus.DONE):
        raise HTTPException(409, f"선택할 수 있는 상태가 아닙니다 (상태: {job.get('status')})")
    if req.layout not in ("letterbox", "crop"):
        raise HTTPException(422, "layout은 letterbox 또는 crop이어야 합니다")

    paths = JobPaths(job_id)
    known = {c["id"] for c in storage.read_json(paths.root / "candidates.json")}
    unknown = [c for c in req.candidate_ids if c not in known]
    if unknown:
        raise HTTPException(422, f"없는 후보입니다: {', '.join(unknown)}")

    jobs._set(job_id, status=JobStatus.RENDERING, stage_detail="쇼츠 생성 준비",
              progress=0.0, selected=req.candidate_ids)
    background.add_task(jobs.run_render, job_id, req.candidate_ids,
                        req.layout, req.bg_blur, req.crop_cx)
    return job_state(job_id)


@app.get(BASE + "/jobs/{job_id}/shorts", response_model=ShortList)
def shorts(job_id: str) -> ShortList:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"작업을 찾을 수 없습니다: {job_id}")
    paths = JobPaths(job_id)
    f = paths.root / "shorts.json"
    if not f.exists():
        raise HTTPException(409, f"아직 쇼츠가 없습니다 (상태: {job.get('status')})")

    items = []
    for s in storage.read_json(f):
        cid = s["candidate_id"]
        thumb = paths.short_thumb(cid)
        items.append(ShortOut(
            **{k: v for k, v in s.items() if k != "candidate_id"},
            candidate_id=cid,
            video_url=paths.url(paths.short_video(cid)) or "",
            thumbnail_url=paths.url(thumb) if thumb.exists() else None,
        ))
    return ShortList(job_id=job_id, shorts=items)


@app.patch(BASE + "/jobs/{job_id}/shorts/{candidate_id}/title", response_model=ShortOut)
def update_short_title(job_id: str, candidate_id: str, req: TitleUpdate) -> ShortOut:
    """쇼츠 제목을 바꾼다.

    제목은 `[상품명] AI가 정한 제목` 형식으로 자동 생성된다.
    판매자가 고치고 싶을 때 이 엔드포인트를 쓴다.

    이미 렌더링된 영상의 화면 자막은 바뀌지 않는다. 메타데이터상의
    제목만 바뀐다 (업로드 시 쓸 제목)."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"작업을 찾을 수 없습니다: {job_id}")

    paths = JobPaths(job_id)
    f = paths.root / "shorts.json"
    if not f.exists():
        raise HTTPException(409, f"아직 쇼츠가 없습니다 (상태: {job.get('status')})")

    data = storage.read_json(f)
    target = next((s for s in data if s["candidate_id"] == candidate_id), None)
    if target is None:
        raise HTTPException(404, f"쇼츠를 찾을 수 없습니다: {candidate_id}")

    target["title"] = req.title.strip()
    storage.write_json(f, data)

    thumb = paths.short_thumb(candidate_id)
    return ShortOut(
        **{k: v for k, v in target.items() if k != "candidate_id"},
        candidate_id=candidate_id,
        video_url=paths.url(paths.short_video(candidate_id)) or "",
        thumbnail_url=paths.url(thumb) if thumb.exists() else None,
    )


# ── BE(live-service) 연동 ─────────────────────────────────────────────
@app.post(BASE + "/lives/{live_id}/highlights",
          response_model=HighlightAccepted, status_code=202)
def request_highlights(live_id: str, req: HighlightRequest,
                       background: BackgroundTasks) -> HighlightAccepted:
    """방송 종료 후 하이라이트·타임라인 생성 요청 (live-service 전용).

    /jobs 흐름과 달리 판매자 선택 단계가 없다. 분석부터 렌더링까지 한 번에 돌리고
    결과를 POST /internal/v1/lives/{liveId}/highlights 로 밀어준다.

    타임라인 챕터는 MARKER, 쇼츠는 CLIP(최대 3개)으로 함께 보낸다."""
    job_id = jobs.new_job_id()
    paths = JobPaths(job_id)
    paths.prepare()

    # 영상은 URL로 온다. 파일 업로드가 아니므로 내려받아 둔다.
    try:
        storage.download(req.vod_url, paths.video)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"VOD를 내려받지 못했습니다: {type(e).__name__}: {e}")

    if req.chats:
        # 작성자 식별자는 분석에 쓰지 않으므로 저장하지 않는다.
        # 구간 탐지는 시각만 보고, 본문은 근거 확인에만 쓴다.
        storage.write_json(paths.comments, {
            "comments": [
                {k: v for k, v in c.model_dump().items() if k != "sender_id"}
                for c in req.chats
            ]
        })
    if req.product_name or req.terms:
        storage.write_json(paths.terms,
                           {"product_name": req.product_name or "", "terms": req.terms})

    start_ms = _iso_to_ms(req.broadcast_started_at)
    jobs._set(job_id, status=JobStatus.QUEUED, stage_detail="대기 중", progress=0.0,
              live_id=live_id, highlight_id=req.highlight_id,
              broadcast_start_ms=start_ms,
              has_terms=bool(req.product_name or req.terms),
              has_comments=bool(req.chats))

    background.add_task(jobs.run_for_live, job_id, live_id,
                        req.highlight_id, req.layout, settings.PUBLIC_BASE_URL)
    return HighlightAccepted(
        live_id=live_id, job_id=job_id, status=JobStatus.QUEUED,
        message="접수했습니다. 결과는 /internal/v1/lives/{liveId}/highlights 로 전송됩니다.")


@app.get(BASE + "/lives/{live_id}/highlights/status", response_model=HighlightJobState)
def highlight_status(live_id: str) -> HighlightJobState:
    """진행 상태 조회. 콜백을 받지 못했을 때 확인용이다."""
    job_id = jobs.live_job_id(live_id)
    if job_id is None:
        raise HTTPException(404, f"해당 방송의 작업을 찾을 수 없습니다: {live_id}")
    job = jobs.get(job_id) or {}
    return HighlightJobState(
        live_id=live_id, job_id=job_id,
        status=job.get("status"), stage_detail=job.get("stage_detail"),
        progress=job.get("progress", 0.0), error=job.get("error"),
        marker_count=job.get("marker_count"), clip_count=job.get("clip_count"),
    )


def _iso_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


# ── 로컬 확인용 뷰어 ──────────────────────────────────────────────────
@app.get("/view/{job_id}", response_class=HTMLResponse)
def view(job_id: str) -> str:
    """브라우저에서 타임라인·쇼츠를 확인한다 (MVP 검증용)."""
    if jobs.get(job_id) is None:
        raise HTTPException(404, f"작업을 찾을 수 없습니다: {job_id}")
    return VIEWER_HTML


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """작업 목록 — 최근 것부터."""
    rows = []
    if settings.WORKSPACE.exists():
        items = sorted(settings.WORKSPACE.glob("*/job.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for f in items[:20]:
            try:
                st = storage.read_json(f)
            except (OSError, ValueError):
                continue
            jid = f.parent.name
            rows.append(
                f'<tr><td><a href="/view/{jid}">{jid}</a></td>'
                f'<td>{st.get("status", "?")}</td>'
                f'<td>{st.get("chapter_count") or "-"}</td>'
                f'<td>{st.get("candidate_count") or "-"}</td>'
                f'<td>{st.get("short_count") or "-"}</td></tr>')
    body = "".join(rows) or '<tr><td colspan="5">작업이 없습니다</td></tr>'
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>하이라이트 쇼츠 자동화</title><style>
body{{background:#0f0e13;color:#eceaf2;font-family:"Malgun Gothic",sans-serif;padding:40px}}
table{{border-collapse:collapse;font-size:14px}}
th,td{{padding:8px 16px;border-bottom:1px solid #2c2a36;text-align:left}}
th{{color:#8f8b9c;font-size:12px}} a{{color:#ff8a5c}}
</style></head><body>
<h1 style="font-size:20px">하이라이트 쇼츠 자동화</h1>
<p style="color:#8f8b9c;font-size:13px">모델 {settings.LLM_MODEL} · STT {settings.STT_MODEL}</p>
<table><tr><th>작업</th><th>상태</th><th>챕터</th><th>후보</th><th>쇼츠</th></tr>
{body}</table>
<p style="color:#8f8b9c;font-size:12px;margin-top:24px">API 문서: <a href="/docs">/docs</a></p>
</body></html>"""


# ── 파일 서빙 ─────────────────────────────────────────────────────────
@app.get(BASE + "/files/{job_id}/{file_path:path}")
def serve(job_id: str, file_path: str) -> FileResponse:
    root = JobPaths(job_id).root.resolve()
    target = (root / file_path).resolve()
    if not str(target).startswith(str(root)):   # 경로 탈출 차단
        raise HTTPException(403, "잘못된 경로입니다")
    if not target.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    return FileResponse(target)


@app.delete(BASE + "/jobs/{job_id}", status_code=204)
def delete_job(job_id: str) -> None:
    if jobs.get(job_id) is None:
        raise HTTPException(404, f"작업을 찾을 수 없습니다: {job_id}")
    JobPaths(job_id).delete()
