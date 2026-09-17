"""라이브 채팅 수신·정규화 — 실제 채팅 데이터가 들어올 자리.

현재는 목데이터(`ts_ms`, `text`)만 쓰지만, 실제 라이브 채팅에는
작성자·좋아요·구매 이벤트 등이 함께 넘어온다. 플랫폼마다 필드명도 다르다.

이 모듈은 **어떤 형태로 들어오든 내부 표준 형식으로 바꾸는 것**만 책임진다.
분석 로직(`comments.py`)은 표준 형식만 보므로, 나중에 실제 데이터가 붙어도
분석 코드는 건드리지 않는다.

내부 표준 형식:
    {
      "ts_ms":  int,    # 방송 시작 기준 경과 시간(ms) — 필수
      "text":   str,    # 채팅 본문 (없으면 빈 문자열)
      "author": str,    # 작성자 식별자 (없으면 빈 문자열)
      "kind":   str,    # chat | like | purchase | join | system
    }

`kind` 를 두는 이유:
    "채팅이 활발한 구간"을 셀 때 좋아요·구매 알림까지 포함할지
    선택할 수 있어야 한다. 플랫폼에 따라 이런 이벤트가 섞여 들어온다.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# 플랫폼마다 다른 필드명을 내부 표준으로 매핑한다.
# 새 플랫폼이 붙으면 여기에 후보를 추가하면 된다.
_TS_KEYS = (
    "ts_ms", "timestamp_ms", "offset_ms", "playtime_ms", "elapsed_ms",
    "ts", "timestamp", "offset", "playtime", "time", "createdAt", "created_at",
)
_TEXT_KEYS = ("text", "message", "msg", "content", "body", "comment")
_AUTHOR_KEYS = ("author", "user", "userId", "user_id", "nickname", "nick", "sender")
_KIND_KEYS = ("kind", "type", "event", "eventType", "event_type", "messageType")

# 채팅 외 이벤트로 취급할 값들 (플랫폼별 표기 차이 흡수)
_KIND_ALIASES = {
    "like": "like", "likes": "like", "heart": "like", "좋아요": "like",
    "purchase": "purchase", "order": "purchase", "buy": "purchase", "구매": "purchase",
    "join": "join", "enter": "join", "입장": "join",
    "system": "system", "notice": "system", "announcement": "system",
}


class ChatFormatError(ValueError):
    """채팅 데이터를 표준 형식으로 바꿀 수 없을 때."""


def load(path: str | Path, *, broadcast_start_ms: int | None = None) -> list[dict]:
    """파일에서 채팅을 읽어 표준 형식으로 반환한다."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return normalize(raw, broadcast_start_ms=broadcast_start_ms)


def normalize(
    raw: Any,
    *,
    broadcast_start_ms: int | None = None,
) -> list[dict]:
    """어떤 형태로 들어오든 표준 형식 리스트로 바꾼다.

    받아들이는 형태:
      - {"comments": [...]} / {"chats": [...]} / {"messages": [...]} / {"items": [...]}
      - [...]  (리스트 그대로)

    broadcast_start_ms 를 주면 절대시각(epoch ms 또는 ISO8601)을
    방송 시작 기준 경과시간으로 환산한다. 실제 라이브 채팅은 대부분
    절대시각으로 오므로 이 인자가 필요하다.
    """
    items = _unwrap(raw)

    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ts = _pick_ts(item, broadcast_start_ms)
        if ts is None:
            continue
        out.append({
            "ts_ms": ts,
            "text": str(_pick(item, _TEXT_KEYS) or "").strip(),
            "author": str(_pick(item, _AUTHOR_KEYS) or "").strip(),
            "kind": _pick_kind(item),
        })

    out.sort(key=lambda c: c["ts_ms"])
    return out


def _unwrap(raw: Any) -> Iterable[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("comments", "chats", "chat", "messages", "items", "data", "events"):
            v = raw.get(key)
            if isinstance(v, list):
                return v
        raise ChatFormatError(
            "채팅 배열을 찾지 못했습니다. "
            "comments/chats/messages/items 중 하나를 쓰거나 리스트를 직접 주세요."
        )
    raise ChatFormatError(f"지원하지 않는 형태입니다: {type(raw).__name__}")


def _pick(item: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in item and item[k] not in (None, ""):
            return item[k]
    return None


def _pick_kind(item: dict) -> str:
    v = _pick(item, _KIND_KEYS)
    if v is None:
        return "chat"
    return _KIND_ALIASES.get(str(v).strip().lower(), "chat")


def _pick_ts(item: dict, broadcast_start_ms: int | None) -> int | None:
    """시각 필드를 ms 경과시간으로 환산한다.

    단위 판별:
      - 1e11 이상이면 epoch ms 로 본다 (1973년 이후)
      - 1e9 이상이면 epoch 초로 본다
      - 그 미만이면 이미 경과시간(ms)으로 본다
      - 문자열이면 ISO8601 또는 "MM:SS" / "HH:MM:SS" 로 파싱
    """
    v = _pick(item, _TS_KEYS)
    if v is None:
        return None

    if isinstance(v, str):
        v = _parse_time_string(v)
        if v is None:
            return None

    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None

    v = float(v)

    if v >= 1e11:                      # epoch ms
        return _to_offset(int(v), broadcast_start_ms)
    if v >= 1e9:                       # epoch sec
        return _to_offset(int(v * 1000), broadcast_start_ms)
    return max(0, int(v))              # 이미 경과시간(ms)


def _to_offset(epoch_ms: int, broadcast_start_ms: int | None) -> int:
    if broadcast_start_ms is None:
        # 시작 시각을 모르면 환산할 수 없다. 호출 측이 알려줘야 한다.
        raise ChatFormatError(
            "채팅이 절대시각으로 들어왔습니다. "
            "broadcast_start_ms 를 함께 주세요."
        )
    return max(0, epoch_ms - broadcast_start_ms)


def _parse_time_string(s: str) -> float | None:
    s = s.strip()
    if not s:
        return None

    # "01:23" / "01:23:45"
    if ":" in s and s.replace(":", "").replace(".", "").isdigit():
        parts = [float(p) for p in s.split(":")]
        sec = 0.0
        for p in parts:
            sec = sec * 60 + p
        return sec * 1000

    if s.replace(".", "", 1).isdigit():
        return float(s)

    # ISO8601
    try:
        t = s.replace("Z", "+00:00")
        return datetime.fromisoformat(t).timestamp() * 1000
    except ValueError:
        return None
