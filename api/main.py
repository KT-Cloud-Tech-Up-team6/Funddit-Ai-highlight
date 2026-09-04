"""하이라이트 쇼츠 자동화 API.

  uvicorn api.main:app --reload

흐름
  POST /jobs                     영상 업로드 → 소재판정·STT·구간분할 (백그라운드)
  GET  /jobs/{id}                진행 상태
  GET  /jobs/{id}/candidates     구간 후보 (판매자 선택 화면용)
  POST /jobs/{id}/select         고른 구간으로 쇼츠 생성 (백그라운드)
  GET  /jobs/{id}/shorts         완성된 쇼츠
  GET  /files/{id}/{path}        결과 파일 서빙
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from api import jobs, settings, storage
from api.schemas import (
    Candidate,
    CandidateList,
    HealthCheck,
    JobCreated,
    JobState,
    JobStatus,
    ScreenResult,
    SelectRequest,
    ShortList,
    ShortOut,
)
from api.storage import JobPaths

settings.ensure_runtime_env()

app = FastAPI(
    title="하이라이트 쇼츠 자동화 API",
    description="라이브 방송을 쇼츠로 자동 변환한다. "
                f"구간 분할·자막은 {settings.LLM_MODEL}, 음성 인식은 Whisper {settings.STT_MODEL}.",
    version="0.1.0",
)


# ── 헬스체크 ──────────────────────────────────────────────────────────
@app.get("/health", response_model=HealthCheck)
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
@app.post("/screen", response_model=ScreenResult)
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
@app.post("/jobs", response_model=JobCreated, status_code=202)
def create_job(
    background: BackgroundTasks,
    video: UploadFile = File(..., description="방송 영상"),
    terms: UploadFile | None = File(None, description="상품 용어 목록 JSON"),
    comments: UploadFile | None = File(None, description="댓글 시계열 JSON"),
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
              has_comments=comments is not None)
    background.add_task(jobs.run_analysis, job_id)
    return JobCreated(job_id=job_id, status=JobStatus.QUEUED,
                      message="접수했습니다. GET /jobs/{job_id}로 진행 상태를 확인하세요.")


@app.get("/jobs/{job_id}", response_model=JobState)
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
        short_count=job.get("short_count"),
        elapsed_sec=job.get("elapsed_sec"),
        stt_reused=job.get("stt_reused", False),
    )


@app.get("/jobs/{job_id}/candidates", response_model=CandidateList)
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


# ── 2단계: 선택 → 쇼츠 ────────────────────────────────────────────────
@app.post("/jobs/{job_id}/select", response_model=JobState, status_code=202)
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


@app.get("/jobs/{job_id}/shorts", response_model=ShortList)
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


# ── 파일 서빙 ─────────────────────────────────────────────────────────
@app.get("/files/{job_id}/{file_path:path}")
def serve(job_id: str, file_path: str) -> FileResponse:
    root = JobPaths(job_id).root.resolve()
    target = (root / file_path).resolve()
    if not str(target).startswith(str(root)):   # 경로 탈출 차단
        raise HTTPException(403, "잘못된 경로입니다")
    if not target.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    return FileResponse(target)


@app.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: str) -> None:
    if jobs.get(job_id) is None:
        raise HTTPException(404, f"작업을 찾을 수 없습니다: {job_id}")
    JobPaths(job_id).delete()
