"""작업 디렉터리 관리 — 나중에 클라우드 스토리지로 바꿀 때 이 파일만 고친다.

동시 요청이 서로의 파일을 덮어쓰지 않도록 작업마다 격리된 폴더를 준다.
render_short가 ffmpeg를 `cwd=ass_path.parent`로 실행하기 때문에
같은 폴더에 ASS를 쓰면 두 요청이 충돌한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from api.settings import WORKSPACE

_logger = logging.getLogger("api.storage")

# 응답에 담는 파일 URL의 접두. api/main.py 의 BASE 와 같아야 한다.
API_BASE = "/api/v1/ai"


class JobPaths:
    """작업 하나의 파일 배치를 캡슐화한다."""

    def __init__(self, job_id: str, root: Path | None = None):
        self.job_id = job_id
        self.root = (root or WORKSPACE) / job_id

    # ── 디렉터리 ──────────────────────────────────────────────────
    @property
    def input_dir(self) -> Path:
        return self.root / "input"

    @property
    def thumbs_dir(self) -> Path:
        return self.root / "thumbs"

    @property
    def shorts_dir(self) -> Path:
        return self.root / "shorts"

    def short_dir(self, candidate_id: str) -> Path:
        return self.shorts_dir / candidate_id

    # ── 파일 ──────────────────────────────────────────────────────
    @property
    def video(self) -> Path:
        return self.input_dir / "video.mp4"

    @property
    def terms(self) -> Path:
        return self.input_dir / "terms.json"

    @property
    def comments(self) -> Path:
        return self.input_dir / "comments.json"

    @property
    def motion(self) -> Path:
        return self.root / "motion.json"

    @property
    def transcript(self) -> Path:
        return self.root / "transcript.json"

    @property
    def segments(self) -> Path:
        return self.root / "segments.json"

    @property
    def timeline(self) -> Path:
        return self.root / "timeline.json"

    @property
    def state(self) -> Path:
        return self.root / "job.json"

    def thumb(self, candidate_id: str) -> Path:
        return self.thumbs_dir / f"{candidate_id}.jpg"

    def captions(self, candidate_id: str) -> Path:
        return self.short_dir(candidate_id) / "captions.json"

    def ass(self, candidate_id: str) -> Path:
        return self.short_dir(candidate_id) / "captions.ass"

    def short_video(self, candidate_id: str) -> Path:
        return self.short_dir(candidate_id) / "short.mp4"

    def short_thumb(self, candidate_id: str) -> Path:
        return self.short_dir(candidate_id) / "thumb.jpg"

    # ── 생성 ──────────────────────────────────────────────────────
    def prepare(self) -> None:
        """작업 시작 전 디렉터리를 만든다.
        poc 모듈 대부분이 부모 폴더를 만들지 않으므로 여기서 보장한다."""
        for d in (self.input_dir, self.thumbs_dir, self.shorts_dir):
            d.mkdir(parents=True, exist_ok=True)

    def prepare_short(self, candidate_id: str) -> Path:
        d = self.short_dir(candidate_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def delete(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # ── URL 변환 ──────────────────────────────────────────────────
    def url(self, path: Path) -> str | None:
        """작업 폴더 안의 파일을 서빙 URL로 바꾼다."""
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            return None
        return f"{API_BASE}/files/{self.job_id}/{rel.as_posix()}"


def _default(o: Any):
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"직렬화할 수 없는 타입: {type(o)}")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=_default),
                    encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def video_fingerprint(path: Path) -> str:
    """영상 파일의 지문 — 같은 방송을 다시 올렸는지 판별해 STT를 재사용한다.

    전체를 해싱하면 20분 영상에 수 초가 걸리므로 크기 + 앞뒤 1MB만 읽는다."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    chunk = 1024 * 1024
    with path.open("rb") as f:
        h.update(f.read(chunk))
        if size > chunk * 2:
            f.seek(-chunk, 2)
            h.update(f.read(chunk))
    return h.hexdigest()[:16]


def find_cached_transcript(fingerprint: str, exclude_job: str | None = None) -> Path | None:
    """같은 영상으로 이미 STT를 끝낸 작업이 있으면 그 transcript를 돌려준다.

    STT가 전체 처리 시간의 90%를 차지하므로(20분 영상 기준 5분), 재사용 효과가 크다."""
    if not WORKSPACE.exists():
        return None
    for state_file in WORKSPACE.glob("*/job.json"):
        if exclude_job and state_file.parent.name == exclude_job:
            continue
        try:
            st = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if st.get("video_fingerprint") != fingerprint:
            continue
        t = state_file.parent / "transcript.json"
        if t.exists():
            return t
    return None


#: 쇼츠를 올릴 S3 버킷. 없으면 업로드하지 않고 로컬 서빙 URL을 쓴다 —
#: 로컬 개발과 S3 없는 환경에서 그대로 돌아가야 한다.
MEDIA_S3_BUCKET = os.environ.get("MEDIA_S3_BUCKET", "")
#: 버킷 안 경로. CloudFront 가 media/ 아래를 그대로 노출한다 (인프라 회신 10-02).
MEDIA_S3_PREFIX = os.environ.get("MEDIA_S3_PREFIX", "media/shorts")
_S3_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

_s3_client = None


def _s3():
    """boto3 클라이언트를 한 번만 만든다. 자격증명은 IRSA 가 준다 (키 주입 없음)."""
    global _s3_client
    if _s3_client is None:
        import boto3

        _s3_client = boto3.client("s3", region_name=_S3_REGION)
    return _s3_client


def upload_media(local: Path, key_suffix: str, content_type: str) -> str | None:
    """쇼츠·썸네일을 S3 에 올리고 버킷 기준 경로를 돌려준다.

    돌려주는 값은 `media/shorts/...` 같은 상대 경로다. 앞에 붙일 도메인은
    PUBLIC_BASE_URL 이 정한다 — 버킷과 CDN 도메인이 따로 바뀔 수 있어
    여기서 합치지 않는다.

    버킷 미설정이거나 업로드가 실패하면 None 을 돌려준다. 호출부는 그때
    로컬 서빙 URL 로 돌아간다 — 업로드 실패가 생성 결과를 버릴 이유는 없다.
    """
    if not MEDIA_S3_BUCKET or not local.exists():
        return None

    key = f"{MEDIA_S3_PREFIX.strip('/')}/{key_suffix.lstrip('/')}"
    try:
        _s3().upload_file(str(local), MEDIA_S3_BUCKET, key,
                          ExtraArgs={"ContentType": content_type})
    except Exception as e:  # noqa: BLE001 — 업로드 실패로 결과물을 버리지 않는다
        _logger.error("S3 업로드 실패", extra={"key": key, "bucket": MEDIA_S3_BUCKET,
                                             "error": f"{type(e).__name__}: {e}"})
        return None
    _logger.info("S3 업로드", extra={"key": key, "bytes": local.stat().st_size})
    return key


#: HLS 재생목록의 첫 줄. 확장자가 없거나 틀려도 내용으로 판별한다.
_HLS_MAGIC = b"#EXTM3U"
#: 재생목록은 텍스트라 작다. 이보다 크면 통짜 영상으로 본다.
_PLAYLIST_MAX = 4 * 1024 * 1024


def download(url: str, dst: Path, *, timeout: int = 300) -> Path:
    """원격 VOD를 내려받는다 (live-service 는 파일이 아니라 URL을 준다).

    통짜 파일(mp4)과 HLS(m3u8 + .ts 조각) 둘 다 받는다. IVS 녹화는 HLS 로
    오고, 미리 올려둔 영상은 mp4 로 온다 — 어느 쪽이 올지 호출부가 몰라도 된다.

    확장자를 믿지 않고 받은 내용으로 판별한다. CDN 이 쿼리스트링을 붙이거나
    확장자 없이 주는 경우가 있어서다.
    """
    import urllib.request

    dst.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "funddit-ai-highlight/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, dst.open("wb") as f:
        head = resp.read(len(_HLS_MAGIC))
        f.write(head)
        shutil.copyfileobj(resp, f)

    size = dst.stat().st_size
    if size == 0:
        raise ValueError("빈 파일을 받았습니다")

    # 재생목록을 그대로 두면 ffmpeg·Whisper 가 깨진 영상으로 읽는다.
    # 조각을 합쳐 통짜 파일로 바꾼다.
    if head.startswith(_HLS_MAGIC) and size <= _PLAYLIST_MAX:
        return _merge_hls(url, dst, timeout=timeout)
    return dst


def _merge_hls(url: str, dst: Path, *, timeout: int) -> Path:
    """HLS 재생목록을 따라가 조각을 하나의 mp4 로 합친다.

    재인코딩하지 않는다 (-c copy) — 조각을 이어 붙이기만 하므로 20분 방송도
    수십 초면 끝난다. 다시 인코딩하면 화질이 떨어지고 몇 분씩 걸린다.
    """
    import subprocess

    tmp = dst.with_suffix(".hls.mp4")
    cmd = [
        "ffmpeg", "-nostdin", "-y",
        # 마스터 플레이리스트가 상대 경로로 하위 목록을 가리킨다.
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-i", url,
        "-c", "copy",
        # .ts 의 MPEG-TS 타임스탬프를 mp4 용으로 다시 매긴다.
        # 안 하면 첫 프레임 시각이 0 이 아니라 자막 싱크가 밀린다.
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(tmp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        tail = (r.stderr or "").strip().splitlines()[-3:]
        raise ValueError("HLS 병합 실패: " + " / ".join(tail))

    tmp.replace(dst)
    return dst


def purge_expired(retention_hours: int) -> list[str]:
    """보관 기간이 지난 작업 폴더를 지운다.

    영상 원본이 방송당 수백 MB라 무기한 보관하면 디스크가 찬다.
    job.json 의 수정 시각을 기준으로 판단한다 — 작업이 끝나면 더 갱신되지 않는다.

    retention_hours <= 0 이면 아무것도 지우지 않는다 (보관 정책 미적용).
    """
    if retention_hours <= 0 or not WORKSPACE.exists():
        return []

    import time

    cutoff = time.time() - retention_hours * 3600
    removed: list[str] = []
    for state in WORKSPACE.glob("*/job.json"):
        try:
            if state.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(state.parent, ignore_errors=True)
            removed.append(state.parent.name)
        except OSError:
            continue
    return removed
