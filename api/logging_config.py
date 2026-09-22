"""구조화 로깅 — 컨테이너 stdout 을 수집기가 파싱할 수 있게 한다.

인프라 문의 회신에서 "stdout 적재 환경 확인되면 JSON 구조화 로깅으로 전환"이라고
답한 부분의 구현이다.

  LOG_FORMAT=json  (기본)  한 줄 JSON — CloudWatch·ELK 수집용
  LOG_FORMAT=text          사람이 읽는 형식 — 로컬 개발용
  LOG_LEVEL=INFO   (기본)

job_id·live_id 는 로그 레코드의 extra 로 넣으면 JSON 최상위 필드로 올라간다.
장애 추적 시 한 작업의 전 단계를 이 값으로 묶어 볼 수 있어야 한다.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

# LogRecord 기본 속성 — 이 목록에 없는 키만 extra 로 간주해 JSON 에 싣는다.
_STANDARD = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
                          .isoformat(timespec="milliseconds")
                          .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """로컬 개발용. extra 를 뒤에 덧붙인다."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = {k: v for k, v in record.__dict__.items()
                 if k not in _STANDARD and not k.startswith("_")}
        return f"{base}  {extra}" if extra else base


def setup() -> None:
    """루트 로거를 설정한다. 서버 기동 시 1회 호출한다."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = os.environ.get("LOG_FORMAT", "json").lower()

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "text":
        handler.setFormatter(TextFormatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    else:
        handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()          # uvicorn 기본 핸들러와 중복 출력 방지
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn 로거도 같은 핸들러를 쓰게 해 액세스 로그 형식을 통일한다.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    # 외부 라이브러리 잡음 억제
    for name in ("httpx", "httpcore", "urllib3", "faster_whisper"):
        logging.getLogger(name).setLevel("WARNING")
