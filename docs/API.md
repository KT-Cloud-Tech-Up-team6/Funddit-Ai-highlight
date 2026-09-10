# API 연동 가이드

백엔드에서 이 서비스를 붙일 때 필요한 내용입니다.
서버 실행 후 `/docs`에서 스키마를 직접 확인할 수도 있습니다.

---

## 1. 전체 흐름

```
① POST /jobs                     영상 + 댓글 업로드      → job_id
② GET  /jobs/{id}                2~5초 폴링             → 진행 상태
③ GET  /jobs/{id}/timeline       다시보기 챕터
④ GET  /jobs/{id}/candidates     쇼츠 후보 목록
⑤ POST /jobs/{id}/select         고른 구간 전달
⑥ GET  /jobs/{id}/shorts         완성된 쇼츠
```

`③`과 `④`는 순서 상관없이 호출할 수 있습니다. 둘 다 `ready_for_selection` 상태에서 준비됩니다.

---

## 2. 엔드포인트

### 2-1. `GET /health`

서버와 모델 상태를 확인합니다. 배포 후 헬스체크에 씁니다.

```json
{
  "ok": true,
  "ffmpeg": true,
  "whisper": true,
  "gemini_key": true,
  "llm_model": "gemini-3.6-flash",
  "stt_model": "large-v3",
  "gpu": "NVIDIA GeForce GTX 1080",
  "active_jobs": 0,
  "notes": []
}
```

`ok`가 `false`면 `notes`에 원인이 들어갑니다.

---

### 2-2. `POST /screen`

영상만 넣어 쇼츠 소재로 쓸 만한지 판정합니다. 음성 인식을 돌리지 않아 빠릅니다 (20분 영상 기준 약 40초).

**요청** — `multipart/form-data`

| 필드 | 타입 | 필수 |
| --- | --- | --- |
| `video` | 파일 | 필수 |

**응답**

```json
{
  "suitable": false,
  "duration_sec": 560,
  "total_cuts": 20,
  "cuts_per_min": 2.1,
  "moving_sec": 9,
  "still_pct": 98.2,
  "threshold": 10.0,
  "reason": "장면 전환이 분당 2.1회로 기준(10회) 미만입니다. 쇼츠로 만들면 화면이 멈춘 것처럼 보입니다."
}
```

업로드 단계에서 미리 걸러 사용자에게 안내할 때 씁니다.

---

### 2-3. `POST /jobs`

처리를 시작합니다. 즉시 `job_id`를 돌려주고 백그라운드에서 실행합니다.

**요청** — `multipart/form-data`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `video` | 파일 | 필수 | 방송 영상 (MP4) |
| `comments` | 파일 | 권장 | 댓글 시계열 JSON |
| `terms` | 파일 | 선택 | 상품 용어 목록 JSON |

**댓글 형식**

```json
{
  "comments": [
    { "ts_ms": 174000, "text": "40만원대면 얼마예요" },
    { "ts_ms": 176600, "text": "정확한 가격 알려주세요" }
  ]
}
```

`ts_ms`는 방송 시작 기준 밀리초입니다. 본문은 저장하되 분석에는 시각만 사용합니다.
댓글을 주면 "시청자가 궁금해한 구간"이 후보에 추가됩니다.

**상품 용어 형식**

```json
{
  "product_name": "로보락 F25",
  "terms": [
    { "surface": "이만 파스칼", "canonical": "20,000Pa" }
  ],
  "specs": [
    { "name": "최대 흡입력", "value": "20,000Pa" }
  ]
}
```

`terms`는 음성 인식 정확도를 높이고, `specs`는 자막 숫자 검증 기준이 됩니다.
앞단 제품에서 이미 받은 상품 정보를 그대로 넘기면 됩니다.

**응답** — `202 Accepted`

```json
{
  "job_id": "e8a4328f30a2",
  "status": "queued",
  "message": "접수했습니다. GET /jobs/{job_id}로 진행 상태를 확인하세요."
}
```

---

### 2-4. `GET /jobs/{job_id}`

진행 상태를 조회합니다. 2~5초 간격 폴링을 권장합니다.

```json
{
  "job_id": "e8a4328f30a2",
  "status": "transcribing",
  "stage_detail": "음성 인식 (large-v3)",
  "progress": 0.15,
  "error": null,
  "screen": { "suitable": true, "cuts_per_min": 47.3, "...": "..." },
  "candidate_count": null,
  "chapter_count": null,
  "short_count": null,
  "elapsed_sec": null,
  "stt_reused": false
}
```

**상태 전이**

```
queued → screening → transcribing → segmenting → timeline
       → ready_for_selection → rendering → done

           ↘ rejected (소재 부적합)
           ↘ failed   (오류)
```

| 상태 | 의미 | 화면 표시 예 |
| --- | --- | --- |
| `queued` | 접수됨 | "대기 중" |
| `screening` | 소재 적합성 판정 | "영상 확인 중" |
| `rejected` | 소재 부적합 | `screen.reason` 표시 후 중단 |
| `transcribing` | 음성 인식 | "음성 인식 중" (가장 오래 걸림) |
| `segmenting` | 구간 분할 | "구간 찾는 중" |
| `timeline` | 타임라인 생성 | "타임라인 만드는 중" |
| `ready_for_selection` | 준비 완료 | 타임라인·후보 노출 |
| `rendering` | 쇼츠 생성 | "쇼츠 만드는 중" |
| `done` | 완료 | 결과 표시 |
| `failed` | 실패 | `error` 표시 |

`progress`는 0.0~1.0입니다. `stt_reused`가 `true`면 이전 분석을 재사용해 훨씬 빨리 끝납니다.

---

### 2-5. `GET /jobs/{job_id}/timeline`

다시보기 타임라인입니다. 방송 전체를 주제별 챕터로 나눕니다.

```json
{
  "job_id": "e8a4328f30a2",
  "chapters": [
    {
      "start_ms": 11100,
      "end_ms": 154000,
      "timestamp": "00:11",
      "duration_sec": 142.9,
      "title": "로보락 F25 방송 시작",
      "category": "intro",
      "category_name": "도입",
      "summary": "제품 소개와 방송 안내"
    }
  ],
  "warnings": ["공백 11:07~12:33을 앞 챕터로 채움"]
}
```

**분류 8종**

| `category` | `category_name` |
| --- | --- |
| `intro` | 도입 |
| `feature` | 기능 설명 |
| `demo` | 시연 |
| `spec` | 제품 스펙 |
| `funding` | 펀딩 정보 |
| `qna` | 질문 응답 |
| `story` | 제작 배경 |
| `closing` | 마무리 |

**화면 반영**

- `timestamp`를 그대로 표시하고, 클릭 시 `start_ms`로 이동
- `category`로 아이콘·색상 분기
- 챕터는 빈틈 없이 이어집니다. 진행바 위에 겹쳐 표시할 수 있습니다.
- `warnings`는 코드가 자동 보정한 내역입니다. 사용자에게 보일 필요는 없습니다.

**상태 코드**

| 코드 | 상황 |
| --- | --- |
| 200 | 정상 |
| 409 | 아직 생성 전 |
| 404 | 없는 작업 |

---

### 2-6. `GET /jobs/{job_id}/candidates`

쇼츠 후보 목록입니다. 판매자 선택 화면에 씁니다.

```json
{
  "job_id": "e8a4328f30a2",
  "candidates": [
    {
      "id": "seg_1",
      "part_type": "P1",
      "part_name": "시연",
      "label": "강력한 오염 청소 시연",
      "start_ms": 259000,
      "end_ms": 378000,
      "duration_sec": 119.0,
      "source": "model",
      "thumbnail_url": "/files/e8a4328f30a2/thumbs/seg_1.jpg",
      "evidence": ["고추기름을 저희가 한번 해봤어요"],
      "comment_count": null,
      "warnings": []
    },
    {
      "id": "p2_1",
      "part_type": "P2",
      "part_name": "질문 집중",
      "label": "질문 집중 (9건)",
      "start_ms": 430000,
      "end_ms": 520000,
      "duration_sec": 90.0,
      "source": "comments",
      "thumbnail_url": "/files/e8a4328f30a2/thumbs/p2_1.jpg",
      "evidence": [],
      "comment_count": 9,
      "warnings": []
    }
  ]
}
```

**파트 유형**

| `part_type` | `part_name` | 판별 |
| --- | --- | --- |
| `P1` | 시연 | 모델 |
| `P2` | 질문 집중 | 코드 (댓글 수) |
| `P3` | 펀딩 홍보 | 모델 |
| `P4` | 제품 스펙 | 모델 |
| `P5` | 제작 배경 | 모델 |

**화면 반영**

- 카드형 목록: `thumbnail_url` + `part_name` + `label` + `duration_sec`
- `source`가 `comments`면 "댓글 N건" 배지를 붙여 구분
- `warnings`는 **숨기지 말고 표시하세요.** 길이 규칙 위반 등을 판매자가 알고 고르게 하는 것이 설계 의도입니다.
- 다중 선택 가능

**상태 코드**

| 코드 | 상황 |
| --- | --- |
| 200 | 정상 |
| 409 | 처리 중이거나 소재 부적합 (메시지에 사유) |
| 404 | 없는 작업 |

---

### 2-7. `POST /jobs/{job_id}/select`

고른 구간으로 쇼츠 생성을 시작합니다.

**요청** — `application/json`

```json
{
  "candidate_ids": ["seg_1", "p2_1"],
  "layout": "letterbox",
  "bg_blur": true,
  "crop_cx": 0.5
}
```

| 필드 | 기본값 | 설명 |
| --- | --- | --- |
| `candidate_ids` | 필수 | 후보 ID 배열 (1개 이상) |
| `layout` | `letterbox` | `letterbox` 원본 비율 유지 + 상하 여백 / `crop` 9:16 잘라 채움 |
| `bg_blur` | `true` | 여백을 흐린 배경으로 채움 (`false`면 검정) |
| `crop_cx` | `0.5` | `crop` 모드에서 잘라낼 중심 (0~1) |

`letterbox`를 권장합니다. 진행자나 제품이 잘리지 않습니다.

**응답** — `202 Accepted`, 본문은 `GET /jobs/{id}`와 같은 형식입니다.

**상태 코드**

| 코드 | 상황 |
| --- | --- |
| 202 | 접수됨 |
| 409 | 선택 가능한 상태가 아님 |
| 422 | 없는 후보 ID이거나 잘못된 `layout` |

---

### 2-8. `GET /jobs/{job_id}/shorts`

완성된 쇼츠입니다.

```json
{
  "job_id": "e8a4328f30a2",
  "shorts": [
    {
      "candidate_id": "seg_1",
      "part_type": "P1",
      "title": "로보락 F25 강력 청소 시연",
      "duration_sec": 119.0,
      "size_bytes": 28311552,
      "video_url": "/files/e8a4328f30a2/shorts/seg_1/short.mp4",
      "thumbnail_url": "/files/e8a4328f30a2/shorts/seg_1/thumb.jpg",
      "captions": [
        {
          "text": "진공+물걸레 2가지 동시",
          "highlight": "2가지 동시",
          "emphasis": "feature",
          "start_ms": 14900,
          "end_ms": 18900
        }
      ],
      "violations": []
    }
  ]
}
```

`captions`는 자막 수정 기능을 붙일 때 씁니다. `violations`가 비어 있으면 검증을 모두 통과한 것입니다.

---

### 2-9. `GET /files/{job_id}/{path}`

결과 파일을 서빙합니다. 다른 응답의 `video_url`, `thumbnail_url`이 이 형식입니다.

경로 탈출은 차단되어 있습니다.

---

### 2-10. `DELETE /jobs/{job_id}`

작업 폴더를 통째로 삭제합니다. `204 No Content`를 반환합니다.

---

## 3. 연동 시 주의점

### 3-1. 폴링 간격

음성 인식이 20분 영상에 약 5분 걸립니다. 2~5초 간격이면 충분하고, 더 자주 부를 이유는 없습니다.

### 3-2. 타임아웃

`POST /jobs`와 `POST /select`는 즉시 `202`를 반환하므로 타임아웃 걱정이 없습니다.
`POST /screen`은 동기 처리라 20분 영상 기준 약 40초 걸립니다. 클라이언트 타임아웃을 60초 이상으로 잡으세요.

### 3-3. 동시 실행 제한

GPU 점유 때문에 동시 2건으로 제한되어 있습니다. 초과분은 큐에서 대기합니다.
`SHORTS_MAX_JOBS` 환경변수로 조정할 수 있습니다.

### 3-4. 음성 인식 재사용

같은 영상을 다시 올리면 이전 분석 결과를 재사용합니다 (파일 크기 + 앞뒤 1MB 해시로 판별).
`stt_reused: true`로 확인할 수 있고, 처리 시간이 6분에서 42초로 줄어듭니다.

### 3-5. 실패 처리

`status`가 `failed`면 `error`에 사유가 들어갑니다.
`rejected`는 실패가 아니라 정상 판정입니다. `screen.reason`을 사용자에게 안내하세요.

### 3-6. 파일 정리

작업 폴더는 자동 삭제되지 않습니다. 보관 정책에 맞춰 `DELETE /jobs/{id}`를 호출하거나 배치로 정리하세요.
20분 영상 1건 기준 약 150MB를 씁니다 (원본 55MB + 쇼츠 4건 90MB + 중간 산출물).

---

## 4. 환경 변수

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `GEMINI_API_KEY` | — | **필수** |
| `GEMINI_MODEL` | `gemini-3.6-flash` | 모델 변경 시 |
| `SHORTS_WORKSPACE` | `./workspace` | 작업 폴더 위치 |
| `SHORTS_STT_MODEL` | `large-v3` | 음성 인식 모델 |
| `SHORTS_MIN_CUTS_PER_MIN` | `10` | 소재 적합성 기준 |
| `SHORTS_P2_MIN_COMMENTS` | `8` | 질문 집중 판정 기준 (60초 창) |
| `SHORTS_MAX_JOBS` | `2` | 동시 실행 제한 |
| `LLM_USAGE_LOG` | `./out/llm_usage.jsonl` | API 사용량 로그 |

---

## 5. 연동 예시

```python
import time
import requests

BASE = "http://localhost:8000"

# ① 업로드
with open("broadcast.mp4", "rb") as v, open("comments.json", "rb") as c:
    r = requests.post(f"{BASE}/jobs", files={"video": v, "comments": c})
job_id = r.json()["job_id"]

# ② 폴링
while True:
    st = requests.get(f"{BASE}/jobs/{job_id}").json()
    if st["status"] in ("ready_for_selection", "rejected", "failed"):
        break
    time.sleep(3)

if st["status"] == "rejected":
    print("소재 부적합:", st["screen"]["reason"])
    exit()

# ③ 타임라인 — 다시보기 화면에 바로 노출
timeline = requests.get(f"{BASE}/jobs/{job_id}/timeline").json()
for ch in timeline["chapters"]:
    print(ch["timestamp"], ch["category_name"], ch["title"])

# ④ 쇼츠 후보 — 판매자에게 보여주고 선택 받기
cands = requests.get(f"{BASE}/jobs/{job_id}/candidates").json()
selected = [c["id"] for c in cands["candidates"][:3]]

# ⑤ 선택 → 생성
requests.post(f"{BASE}/jobs/{job_id}/select",
              json={"candidate_ids": selected, "layout": "letterbox"})

while True:
    st = requests.get(f"{BASE}/jobs/{job_id}").json()
    if st["status"] in ("done", "failed"):
        break
    time.sleep(5)

# ⑥ 결과
shorts = requests.get(f"{BASE}/jobs/{job_id}/shorts").json()
for s in shorts["shorts"]:
    print(s["title"], BASE + s["video_url"])
```

---

## 6. 아직 없는 것

운영 전에 추가가 필요한 항목입니다.

| 항목 | 비고 |
| --- | --- |
| 인증·권한 | 현재 누구나 호출 가능 |
| 클라우드 스토리지 | 로컬 디스크 사용 (`api/storage.py`만 교체하면 됨) |
| 서버 재시작 시 작업 복구 | 완료된 작업 조회는 가능, 진행 중 작업은 유실 |
| 상품 유형별 금칙어 | 의료기기법 등 규제 대응 |
| 웹훅 | 폴링만 지원 |
