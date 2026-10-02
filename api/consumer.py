"""live.ended.v1 구독 — 방송이 끝나면 하이라이트·타임라인을 자동 생성한다.

BE 계약 (Fundit-backend / live-service):
  토픽       live.ended.v1
  파티션 키  liveId
  페이로드   봉투 없는 평평한 JSON (event-convention.md 4·5번)

BE 가 현재 보내는 필드 (LiveEventTransport.LiveEndedEvent):
  { "eventId": "...", "liveId": "...", "projectId": "...", "endedAt": "..." }

vodUrl 이 페이로드에 없다 — 방송 종료 시점에는 IVS 녹화가 아직 안 끝나
BE 도 값을 모른다. BE 를 고치지 않고 우리가 해결한다:

  이벤트 수신 → GET /api/v1/lives/{liveId}/vod 를 주기적으로 조회
              → playbackUrl 이 채워지면 그때 생성 시작

이 엔드포인트는 인증이 없고 (LiveController.vod), 종료된 방송이면
vodUrl·vodReadyAt 을 그대로 돌려준다. 페이로드에 vodUrl 이 실려 오면
폴링 없이 바로 시작한다 — BE 가 나중에 추가해도 코드 변경이 필요 없다.

실행:
  ENABLE_KAFKA_CONSUMER=1 로 켠다. 기본은 꺼져 있다 —
  Kafka 가 없는 환경에서 서버가 못 뜨면 안 된다.

실행:
  ENABLE_KAFKA_CONSUMER=1 로 켠다. 기본은 꺼져 있다 —
  Kafka 가 없는 환경에서 서버가 못 뜨면 안 된다.
"""
from __future__ import annotations

import json
import logging
import os
import threading

_logger = logging.getLogger("api.consumer")

# BE 가 vodUrl 을 어느 토픽에 실어 보낼지 확정되면 여기만 고친다.
# 쉼표로 여러 개를 줄 수 있다 (예: "live.ended.v1,live.vod-ready.v1").
TOPICS = os.environ.get("KAFKA_TOPICS", "live.ended.v1")
GROUP_ID = os.environ.get("KAFKA_GROUP_ID", "ai-highlight")

# 같은 이벤트를 두 번 받아도 두 번 만들지 않는다.
# Kafka 는 at-least-once 라 재조정·재시도 때 중복이 정상적으로 발생한다.
# ponytail: 프로세스 메모리 기준. 워커가 1개라 지금은 충분하고,
#           여러 파드로 늘리면 Redis 등 공유 저장소로 옮긴다.
_seen: set[str] = set()
_seen_lock = threading.Lock()
_SEEN_MAX = 10_000


def _already_handled(event_id: str) -> bool:
    if not event_id:
        return False
    with _seen_lock:
        if event_id in _seen:
            return True
        if len(_seen) >= _SEEN_MAX:
            _seen.clear()          # 오래된 것부터 지우는 대신 통째로 비운다
        _seen.add(event_id)
    return False


def handle(payload: dict) -> str:
    """이벤트 1건 처리. 반환값은 처리 결과 사유(로그·테스트용)."""
    from api import jobs, settings, storage
    from api.storage import JobPaths

    live_id = payload.get("liveId") or payload.get("live_id")
    if not live_id:
        return "liveId 없음 — 건너뜀"

    event_id = str(payload.get("eventId") or payload.get("event_id") or "")
    if _already_handled(event_id):
        return "이미 처리한 이벤트 — 건너뜀"

    vod_url = payload.get("vodUrl") or payload.get("vod_url")
    if not vod_url:
        # 페이로드에 없으면 BE 에서 직접 조회한다. 녹화가 끝날 때까지 기다린다.
        vod_url = wait_for_vod(live_id)
    if not vod_url:
        _logger.error("VOD 를 기다렸지만 준비되지 않았다",
                      extra={"live_id": live_id, "event_id": event_id})
        from api import callback
        callback.send(live_id, callback.failed(None, "VOD 가 준비되지 않았습니다"))
        return "VOD 미준비 — 중단"

    job_id = jobs.new_job_id()
    paths = JobPaths(job_id)
    paths.prepare()

    try:
        storage.download(vod_url, paths.video)
    except Exception as e:  # noqa: BLE001 — 다운로드 실패가 컨슈머를 죽이면 안 된다
        _logger.error("VOD 다운로드 실패",
                      extra={"live_id": live_id, "job_id": job_id,
                             "error": f"{type(e).__name__}: {e}"})
        from api import callback
        callback.send(live_id, callback.failed(None, f"VOD 다운로드 실패: {type(e).__name__}"))
        return "다운로드 실패"

    # 쇼츠 제목이 `[상품명] 내용` 형식이라 상품명을 먼저 받아 둔다.
    # 없으면 제목에서 빠질 뿐 생성은 그대로 진행한다.
    product = fetch_product_name(payload.get("projectId") or payload.get("project_id") or "")
    if product:
        storage.write_json(paths.terms, {"product_name": product, "terms": []})

    jobs._set(job_id, status="queued", stage_detail="대기 중", progress=0.0,
              live_id=live_id, highlight_id=None,
              product_name=product,
              broadcast_start_ms=_iso_to_ms(payload.get("startedAt")),
              source="kafka")

    threading.Thread(
        target=jobs.run_for_live,
        args=(job_id, live_id, None, "crop", settings.PUBLIC_BASE_URL),
        daemon=True, name=f"highlight-{job_id}",
    ).start()

    _logger.info("하이라이트 생성 시작", extra={"live_id": live_id, "job_id": job_id})
    return f"생성 시작 (job {job_id})"


def wait_for_vod(live_id: str, *, timeout_sec: int | None = None,
                 interval_sec: int = 30) -> str | None:
    """BE 에서 VOD 주소가 채워질 때까지 기다린다.

    GET {LIVE_SERVICE_URL}/api/v1/lives/{liveId}/vod 를 폴링한다.
    IVS 녹화는 방송 길이에 비례해 걸리므로 기본 30분까지 기다린다.

    BE 를 고치지 않고 (a)안을 돌리기 위한 우회다. BE 가 vodUrl 을
    이벤트에 실어주면 이 함수는 호출되지 않는다.
    """
    import time
    import urllib.error
    import urllib.request

    base = os.environ.get("LIVE_SERVICE_URL", "").rstrip("/")
    if not base:
        _logger.error("LIVE_SERVICE_URL 미설정 — VOD 를 조회할 수 없다",
                      extra={"live_id": live_id})
        return None

    limit = timeout_sec if timeout_sec is not None else int(
        os.environ.get("VOD_WAIT_TIMEOUT_SEC", "1800"))
    deadline = time.time() + limit
    url = f"{base}/api/v1/lives/{live_id}/vod"

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            vod = data.get("playbackUrl")
            if vod:
                _logger.info("VOD 준비 확인", extra={"live_id": live_id})
                return vod
        except (urllib.error.URLError, OSError, ValueError) as e:
            # 아직 준비 안 됨(404)·일시 장애 모두 재시도 대상이다.
            _logger.info("VOD 대기 중",
                         extra={"live_id": live_id, "reason": f"{type(e).__name__}"})
        time.sleep(interval_sec)

    return None


def fetch_product_name(project_id: str, *, timeout: int = 10) -> str | None:
    """프로젝트 상세에서 상품명을 가져온다.

    쇼츠 제목이 `[상품명] 내용` 형식인데, live.ended.v1 페이로드에는
    상품명이 없고 projectId 만 온다. API 로 들어오는 요청은 BE 가
    product_name 을 같이 보내지만 (api/main.py), Kafka 경로는 여기서
    채우지 않으면 제목에 상품명이 빠진다.

    인증이 없는 공개 상세라 내부 키가 필요 없다. 실패해도 None 을
    돌려준다 — 상품명이 없다고 생성을 멈출 이유는 없다.
    """
    import urllib.error
    import urllib.request

    # project-service 는 live-service 와 다른 서비스라 주소가 따로다
    # (클러스터에서 http://fundit-project-svc:8080). 미설정이면 조용히 건너뛴다.
    base = os.environ.get("PROJECT_SERVICE_URL", "").rstrip("/")
    if not base or not project_id:
        return None
    url = f"{base}/api/v1/projects/{project_id}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            title = (json.loads(resp.read().decode("utf-8")) or {}).get("title")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        _logger.warning("상품명 조회 실패 — 제목에서 생략한다",
                        extra={"project_id": project_id,
                               "error": f"{type(e).__name__}: {e}"})
        return None
    title = str(title or "").strip()
    if title:
        _logger.info("상품명 확인", extra={"project_id": project_id, "title": title})
    return title or None


def _iso_to_ms(value) -> int | None:
    if not value:
        return None
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def start() -> None:
    """백그라운드 스레드로 컨슈머를 띄운다. 실패해도 서버는 계속 뜬다."""
    enabled = os.environ.get("ENABLE_KAFKA_CONSUMER", "").lower() in ("1", "true")
    brokers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")

    if not enabled:
        # 브로커만 있고 플래그가 없으면 설정 누락일 가능성이 높다. 조용히 넘기면
        # 파드도 헬스체크도 정상인데 이벤트만 안 들어와 원인 찾기가 오래 걸린다.
        if brokers:
            _logger.warning(
                "ENABLE_KAFKA_CONSUMER 미설정 — 브로커는 있는데 구독하지 않는다. "
                "의도한 것이 아니면 ENABLE_KAFKA_CONSUMER=1 을 넣어야 한다",
                extra={"kafka_bootstrap_servers": brokers})
        else:
            _logger.info("Kafka 컨슈머 비활성 (ENABLE_KAFKA_CONSUMER 미설정)")
        return

    if not brokers:
        _logger.warning("KAFKA_BOOTSTRAP_SERVERS 미설정 — 컨슈머를 띄우지 않는다")
        return

    # 구독해도 LIVE_SERVICE_URL 이 없으면 VOD 조회·콜백이 모두 실패한다.
    # 이벤트를 받고 나서 알면 늦으므로 기동 시점에 먼저 알린다.
    if not os.environ.get("LIVE_SERVICE_URL"):
        _logger.warning(
            "LIVE_SERVICE_URL 미설정 — 이벤트를 받아도 VOD 조회와 콜백이 모두 실패한다")

    threading.Thread(target=_run, args=(brokers,), daemon=True, name="kafka-consumer").start()


def _run(brokers: str) -> None:
    try:
        from kafka import KafkaConsumer
    except ImportError:
        _logger.error("kafka-python 이 없다 — pip install kafka-python")
        return

    topics = [t.strip() for t in TOPICS.split(",") if t.strip()]
    try:
        consumer = KafkaConsumer(
            *topics,
            bootstrap_servers=brokers.split(","),
            group_id=GROUP_ID,
            # 오프셋이 사라진 상황에서 방송을 건너뛰기보다 다시 읽는 쪽이 낫다.
            # 중복은 eventId 로 흡수한다 (notification-service 와 같은 판단).
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        )
    except Exception as e:  # noqa: BLE001
        _logger.error("Kafka 연결 실패", extra={"error": f"{type(e).__name__}: {e}"})
        return

    _logger.info("Kafka 컨슈머 시작", extra={"topics": topics, "group_id": GROUP_ID})

    for msg in consumer:
        # handle 은 VOD 를 최대 30분 기다린다. 루프에서 직접 부르면 그동안
        # 다음 메시지를 못 읽고 컨슈머가 그룹에서 쫓겨난다.
        threading.Thread(target=_handle_safely, args=(msg.topic, msg.value or {}),
                         daemon=True).start()


def _handle_safely(topic: str, value: dict) -> None:
    try:
        result = handle(value)
        _logger.info("이벤트 처리", extra={"topic": topic, "result": result})
    except Exception as e:  # noqa: BLE001 — 한 건 실패로 컨슈머를 죽이지 않는다
        _logger.error("이벤트 처리 실패",
                      extra={"topic": topic, "error": f"{type(e).__name__}: {e}"})
