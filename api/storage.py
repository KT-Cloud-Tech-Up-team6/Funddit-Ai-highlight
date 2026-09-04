"""작업 디렉터리 관리 — 나중에 클라우드 스토리지로 바꿀 때 이 파일만 고친다.

동시 요청이 서로의 파일을 덮어쓰지 않도록 작업마다 격리된 폴더를 준다.
render_short가 ffmpeg를 `cwd=ass_path.parent`로 실행하기 때문에
같은 폴더에 ASS를 쓰면 두 요청이 충돌한다.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from api.settings import WORKSPACE


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
        return f"/files/{self.job_id}/{rel.as_posix()}"


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
