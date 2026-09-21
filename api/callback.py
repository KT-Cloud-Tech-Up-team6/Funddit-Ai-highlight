"""live-service 로 하이라이트·타임라인 결과를 밀어주는 콜백.

BE 계약 (Fundit-backend / live-service):
  POST {LIVE_SERVICE_URL}/internal/v1/lives/{liveId}/highlights
  body: HighlightCallback[]

타임라인 챕터는 MARKER(시점, endSec=null, 개수 무제한),
쇼츠는 CLIP(구간, 방송 1회당 3개)으로 보낸다. 한 번에 같이 보낸다.

BE 가 검증하는 것 (InternalLiveController.HighlightCallback):
  - kind / sceneLabel / startSec / status 는 필수. 비면 500 이 난다
  - sceneLabel 은 enum — 모르는 값이면 400
  - endSec 이 있으면 startSec 보다 커야 한다
  - CLIP 은 3개 초과분을 BE 가 잘라낸다
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# 우리 분류(7종) → BE SceneLabel.
#
# INTRO / CLOSING 은 BE enum 에 아직 없다. 임의로 다른 값에 뭉개면
# 타임라인이 방송 전체를 덮는다는 설계가 깨지고, 시청자는 "방송 시작"과
# "마무리" 지점을 찾을 수 없다. 그래서 값을 죽이지 않고 그대로 보낸다.
#
# BE 가 enum 에 INTRO / CLOSING 을 추가하면 그대로 동작한다.
# 추가 전까지는 FALLBACK_SCENE_LABEL=1 로 켜서 임시 매핑할 수 있다.
SCENE_LABEL = {
    "demo": "DEMO",
    "spec": "SPEC",
    "price": "PRICE_BENEFIT",
    "compare": "COMPARISON",
    "qna": "AUDIENCE_REACTION",
    "intro": "INTRO",
    "closing": "CLOSING",
}

# BE enum 확장 전 임시 운용용. 기본은 꺼져 있다 (값을 죽이지 않는다).
FALLBACK = {"INTRO": "SPEC", "CLOSING": "SPEC"}

# 쇼츠 후보 part_type → SceneLabel
PART_SCENE_LABEL = {
    "P1": "DEMO",
    "P2": "AUDIENCE_REACTION",
    "P3": "PRICE_BENEFIT",
    "P4": "SPEC",
}

MAX_CLIPS = 3  # BE 와 동일한 상한. 미리 잘라 보내 초과분 유실을 눈에 보이게 한다.


def scene_label(category: str) -> str:
    label = SCENE_LABEL.get(str(category or "").lower(), "SPEC")
    if os.environ.get("FALLBACK_SCENE_LABEL", "").lower() in ("1", "true"):
        return FALLBACK.get(label, label)
    return label


def to_markers(chapters: list[dict]) -> list[dict]:
    """타임라인 챕터 → MARKER 콜백 항목."""
    out = []
    for c in chapters:
        start = int(c.get("start_ms", 0)) // 1000
        out.append({
            "highlightId": None,
            "kind": "MARKER",
            "sceneLabel": scene_label(c.get("category", "")),
            # 제목은 생성 단계에서 이미 18자로 맞춘다. 여기서 또 자르지 않는다.
            "title": c.get("title") or None,
            "startSec": max(0, start),
            "endSec": None,                       # MARKER 는 시점이므로 null
            "clipUrl": None,
            "caption": c.get("summary") or None,
            "status": "COMPLETED",
        })
    return out


def to_clips(shorts: list[dict], base_url: str,
             highlight_id: str | None = None) -> list[dict]:
    """완성된 쇼츠 → CLIP 콜백 항목.

    highlight_id 는 재생성 대상이다. 받은 값을 그대로 돌려줘야 BE 가
    기존 행을 갱신한다. 안 돌려주면 원래 행이 GENERATING 으로 남고
    클립 수가 상한에 걸려 재생성 자체가 막힌다.
    """
    out = []
    for s in shorts[:MAX_CLIPS]:
        start = int(s.get("start_ms", 0)) // 1000
        end = int(s.get("end_ms", 0)) // 1000
        if end <= start:                          # BE 가 400 으로 되돌려보낸다
            end = start + 1
        url = s.get("video_url") or ""
        out.append({
            "highlightId": highlight_id,
            "kind": "CLIP",
            "sceneLabel": PART_SCENE_LABEL.get(s.get("part_type", ""), "DEMO"),
            "title": s.get("title") or None,
            "startSec": max(0, start),
            "endSec": end,
            "clipUrl": absolute(url, base_url),   # BE 는 바로 재생할 URL 을 기대한다
            "caption": s.get("caption") or None,
            "status": "COMPLETED",
        })
    return out


def failed(highlight_id: str | None, reason: str) -> list[dict]:
    """처리 실패를 알린다. 안 보내면 BE 행이 GENERATING 으로 영영 남는다."""
    return [{
        "highlightId": highlight_id,
        "kind": "CLIP",
        "sceneLabel": "DEMO",
        "title": None,
        "startSec": 0,
        "endSec": None,
        "clipUrl": None,
        "caption": reason[:200] if reason else None,
        "status": "FAILED",
    }]


def absolute(url: str, base_url: str) -> str | None:
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    return f"{base_url.rstrip('/')}{url}"


def send(live_id: str, payload: list[dict], *, timeout: int = 20) -> bool:
    """콜백 전송. 실패해도 예외를 올리지 않고 False 를 돌려준다.

    콜백 실패가 이미 끝난 생성 작업을 되돌릴 이유는 없다.
    결과물은 우리 쪽에 남아 있으므로 BE 가 재요청하면 된다.
    """
    base = os.environ.get("LIVE_SERVICE_URL", "").rstrip("/")
    if not base:
        return False

    req = urllib.request.Request(
        f"{base}/internal/v1/lives/{live_id}/highlights",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    secret = os.environ.get("INTERNAL_GATEWAY_SECRET")
    if secret:
        # live-service 의 InternalGatewaySecretFilter 가 확인하는 헤더
        req.add_header("X-Internal-Secret", secret)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
