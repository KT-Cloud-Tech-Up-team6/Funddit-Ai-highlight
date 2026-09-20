# 하이라이트 쇼츠 · 타임라인 API — 백엔드 연동 명세

라이브 커머스 방송 VOD를 **다시보기 타임라인**과 **하이라이트 쇼츠**로 변환하는 AI 서비스입니다.

- **Base Path** — `/api/v1/ai` (전사 공통 URL 경로 버저닝 규칙)
- 호스트 — 배포 후 전달 (로컬 개발: `http://localhost:8000`)
- 인증 — 현재 없음 (내부망 전제. 필요하면 요청 주세요)
- 응답 — 모두 `application/json`, UTF-8
- Swagger — 서버 실행 후 `/docs`

> **두 기능은 독립적입니다.** 타임라인만, 혹은 쇼츠만 써도 됩니다.
> 한쪽이 실패해도 다른 쪽은 정상 동작합니다.

---

## 1. 전체 흐름

```
① POST /api/v1/ai/jobs                       영상 + 라이브 채팅 업로드  → job_id
       ↓  (백그라운드: 소재판정 → 음성인식 → 구간분할 → 타임라인)
② GET  /api/v1/ai/jobs/{id}                  폴링으로 진행 상태 확인
       ↓
③ GET  /api/v1/ai/jobs/{id}/timeline         다시보기 타임라인          ← 여기서 끝나도 됨
③ GET  /api/v1/ai/jobs/{id}/candidates       쇼츠 후보 목록
       ↓
④ POST /api/v1/ai/jobs/{id}/select           판매자가 고른 구간 전달
       ↓  (백그라운드: 자막 생성 → 렌더링)
⑤ GET  /api/v1/ai/jobs/{id}/shorts           완성된 쇼츠
```

**④에서 한 번 멈춥니다.** 자막 생성이 구간당 약 9원이라, 선택하지 않은 구간까지
미리 만들면 낭비입니다. 판매자가 고른 뒤에 처리합니다.

### 처리 시간 (20분 방송 기준)

| 단계 | 소요 |
| --- | --- |
| ① → ③ (분석) | 약 6분 |
| ④ → ⑤ (쇼츠 3개 렌더링) | 약 1분 20초 |

같은 영상을 다시 요청하면 음성 인식 결과를 재사용해 **42초**에 끝납니다.

---

## 2. 엔드포인트 목록

| 메서드 | 경로 | 용도 |
| --- | --- | --- |
| `GET` | `/api/v1/ai/health` | 서버·의존성 상태 |
| `POST` | `/api/v1/ai/screen` | 소재 적합성만 사전 판정 (빠름) |
| `POST` | `/api/v1/ai/jobs` | 작업 생성 (영상 업로드) |
| `GET` | `/api/v1/ai/jobs/{job_id}` | 진행 상태 폴링 |
| `GET` | `/api/v1/ai/jobs/{job_id}/timeline` | 다시보기 타임라인 |
| `GET` | `/api/v1/ai/jobs/{job_id}/candidates` | 쇼츠 후보 목록 |
| `POST` | `/api/v1/ai/jobs/{job_id}/select` | 구간 선택 → 쇼츠 생성 |
| `GET` | `/api/v1/ai/jobs/{job_id}/shorts` | 완성된 쇼츠 |
| `PATCH` | `/api/v1/ai/jobs/{job_id}/shorts/{candidate_id}/title` | 쇼츠 제목 수정 |
| `DELETE` | `/api/v1/ai/jobs/{job_id}` | 작업 삭제 (결과 파일 포함) |
| `GET` | `/api/v1/ai/files/{job_id}/{path}` | 결과 파일 서빙 (영상·썸네일) |

---

## 3. 상세

### 3-1. `POST /api/v1/ai/jobs` — 작업 생성

`multipart/form-data`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `video` | 파일 | **필수** | 방송 영상 (MP4) |
| `comments` | 파일 | 권장 | 라이브 채팅 JSON |
| `terms` | 파일 | 선택 | 상품 용어 목록 JSON |
| `broadcast_start_ms` | 정수 | 조건부 | 방송 시작 시각(epoch ms). 채팅이 절대시각일 때만 |

**응답 `202`**

```json
{
  "job_id": "e8a4328f30a2",
  "status": "queued",
  "message": "접수했습니다. GET /api/v1/ai/jobs/{job_id}로 진행 상태를 확인하세요."
}
```

#### 라이브 채팅 형식

가장 단순한 형태입니다. `ts_ms`는 방송 시작 기준 밀리초입니다.

```json
{
  "comments": [
    { "ts_ms": 174000, "text": "40만원대면 얼마예요" },
    { "ts_ms": 176600, "text": "정확한 가격 알려주세요" }
  ]
}
```

**플랫폼 형식 그대로 보내도 됩니다.** 필드명이 달라도 자동으로 맞춥니다.

```json
{
  "messages": [
    { "createdAt": 1700000015000, "nickname": "user1", "msg": "가격 얼마예요?", "type": "CHAT" },
    { "createdAt": 1700000016000, "nickname": "user2", "type": "LIKE" }
  ]
}
```

| 항목 | 허용되는 키 |
| --- | --- |
| 배열 | `comments` · `chats` · `messages` · `items` · `data` · `events` 또는 최상위 리스트 |
| 시각 | `ts_ms` · `createdAt` · `offset` · `timestamp` · `playtime` 등 |
| 본문 | `text` · `msg` · `message` · `content` · `body` 등 |
| 작성자 | `author` · `user` · `userId` · `nickname` 등 |
| 종류 | `kind` · `type` · `event` → `chat` / `like` / `purchase` / `join` / `system` |

시각 형식: epoch ms, epoch 초, ISO8601, `"MM:SS"`, `"HH:MM:SS"`, 경과 ms

> **시각이 절대시각(epoch)이면 `broadcast_start_ms`를 반드시 함께 보내세요.**
> 없으면 경과시간으로 환산할 수 없습니다. 이미 경과시간이면 생략합니다.

좋아요·구매 이벤트도 반응 강도 계산에 반영됩니다.
채팅 본문은 저장하되 **분석에는 시각만 사용**합니다.
채팅 처리가 실패해도 쇼츠·타임라인 생성은 계속됩니다.

#### 상품 용어 목록 (`terms`)

쇼츠 제목의 `[상품명]`과 음성 인식 정확도에 쓰입니다.

```json
{
  "product_name": "로보락 F25",
  "terms": ["JawScrapers", "FlatReach", "20,000Pa"]
}
```

---

### 3-2. `GET /api/v1/ai/jobs/{job_id}` — 진행 상태

폴링용입니다. **3~5초 간격**을 권장합니다.

```json
{
  "job_id": "e8a4328f30a2",
  "status": "ready_for_selection",
  "stage_detail": "후보 썸네일 생성",
  "progress": 0.9,
  "error": null,
  "screen": { "suitable": true, "cuts_per_min": 47.3 },
  "candidate_count": 4,
  "chapter_count": 14,
  "short_count": null,
  "elapsed_sec": 372.5,
  "stt_reused": false
}
```

#### status 값

| 값 | 의미 | 다음 행동 |
| --- | --- | --- |
| `queued` | 대기 중 | 폴링 계속 |
| `screening` | 소재 적합성 판정 | 폴링 계속 |
| `transcribing` | 음성 인식 (가장 오래 걸림) | 폴링 계속 |
| `segmenting` | 구간 분할 | 폴링 계속 |
| `timeline` | 타임라인 생성 | 폴링 계속 |
| `ready_for_selection` | **분석 완료** | `/candidates`, `/timeline` 조회 |
| `rendering` | 쇼츠 렌더링 중 | 폴링 계속 |
| `done` | **쇼츠 완료** | `/shorts` 조회 |
| `rejected` | 소재 부적합 | `screen.reason` 표시 후 중단 |
| `failed` | 처리 실패 | `error` 표시 |

`rejected`와 `failed`는 다릅니다. `rejected`는 영상이 쇼츠 소재로 부적합하다는
정상 판정이고, `failed`는 처리 중 오류입니다.

---

### 3-3. `GET /api/v1/ai/jobs/{job_id}/timeline` — 다시보기 타임라인

방송 전체를 **빈틈없이** 덮는 챕터 목록입니다. 시청자가 원하는 지점으로 이동하는 데 씁니다.

```json
{
  "job_id": "e8a4328f30a2",
  "chapters": [
    {
      "start_ms": 154480,
      "end_ms": 195920,
      "timestamp": "02:34",
      "duration_sec": 41.4,
      "title": "20만원 할인 및 경품 혜택",
      "category": "price",
      "category_name": "가격·혜택",
      "summary": "백화점 동일 최신상 모델의 방송 한정 20만 원 할인 안내"
    }
  ],
  "warnings": []
}
```

#### category 값

| 값 | 표시명 | 내용 |
| --- | --- | --- |
| `intro` | 도입 | 인사, 제품 소개, 만든 배경 |
| `price` | 가격·혜택 | 할인, 구성, 사은품, 무이자, 마감 |
| `demo` | 시연 | 실제로 써 보이는 구간 |
| `spec` | 스펙·기능 | 수치, 사양, 말로 하는 설명 |
| `compare` | 비교 | 이 제품이 없을 때와 견주는 구간 |
| `qna` | 질문 응답 | 진행자가 시청자 질문에 답하는 구간 |
| `closing` | 마무리 | 정리, 마지막 안내 |

**해당 내용이 없는 분류는 응답에 나타나지 않습니다.** 질문 응답이 없는 방송이면
`qna` 챕터는 하나도 없습니다. 화면에서는 실제 등장한 분류만 범례로 그리면 됩니다.

#### 화면 구현 참고

- `timestamp`는 그대로 표시하면 됩니다 (`02:34`, 1시간 넘으면 `1:02:34`)
- 클릭 시 `start_ms / 1000` 으로 플레이어 시킹
- 챕터는 `start_ms` 오름차순이고 서로 겹치지 않으며 빈 구간이 없습니다
- 챕터 수 — 10분당 약 10개 (20분 방송이면 14~18개)
- `title`은 **18자 이내**로 생성됩니다
- `warnings`는 내부 보정 기록입니다. 화면에 표시할 필요 없습니다

---

### 3-4. `GET /api/v1/ai/jobs/{job_id}/candidates` — 쇼츠 후보

판매자 선택 화면용입니다. 타임라인과 달리 **잘라 쓸 만한 구간만** 담깁니다.

```json
{
  "job_id": "e8a4328f30a2",
  "candidates": [
    {
      "id": "seg_1",
      "part_type": "P1",
      "part_name": "핵심 시연",
      "label": "강력 오염 청소 시연",
      "start_ms": 244000,
      "end_ms": 363000,
      "duration_sec": 119.0,
      "source": "model",
      "thumbnail_url": "/api/v1/ai/files/e8a4328f30a2/thumbs/seg_1.jpg",
      "evidence": ["고추기름도 순식간에", "2만 파스칼"],
      "comment_count": null,
      "warnings": []
    },
    {
      "id": "p2_0",
      "part_type": "P2",
      "part_name": "질문 집중",
      "label": "질문 집중 (12건)",
      "start_ms": 130000,
      "end_ms": 240000,
      "duration_sec": 110.0,
      "source": "comments",
      "thumbnail_url": "/api/v1/ai/files/e8a4328f30a2/thumbs/p2_0.jpg",
      "evidence": [],
      "comment_count": 12,
      "warnings": []
    }
  ]
}
```

| 필드 | 설명 |
| --- | --- |
| `source` | `model` — 영상 내용 기반 / `comments` — 채팅 밀집 기반 |
| `comment_count` | `source: "comments"`일 때만 값이 있음 |
| `evidence` | 이 구간을 고른 근거 발화. 카드에 보여주면 판단에 도움 |
| `warnings` | 길이 규칙 위반 등. 있으면 표시해 판매자가 알고 고르게 |

`409`가 오면 아직 분석 중입니다. `status`가 `ready_for_selection`이 된 뒤 호출하세요.

---

### 3-5. `POST /api/v1/ai/jobs/{job_id}/select` — 구간 선택

```json
{
  "candidate_ids": ["seg_1", "p2_0"],
  "layout": "crop",
  "bg_blur": false,
  "crop_cx": 0.5
}
```

| 필드 | 기본값 | 설명 |
| --- | --- | --- |
| `candidate_ids` | — | **필수**, 1개 이상 |
| `layout` | `crop` | `crop` — 세로 꽉 채움 / `letterbox` — 원본 비율 + 여백 |
| `bg_blur` | `false` | `letterbox`일 때 여백을 블러 처리 |
| `crop_cx` | `0.5` | `crop`일 때 가로 중심 (0.0 왼쪽 ~ 1.0 오른쪽) |

**응답 `202`** — `JobState` (status: `rendering`). 이후 폴링으로 `done`을 기다립니다.

---

### 3-6. `GET /api/v1/ai/jobs/{job_id}/shorts` — 완성된 쇼츠

```json
{
  "job_id": "e8a4328f30a2",
  "shorts": [
    {
      "candidate_id": "seg_1",
      "part_type": "P1",
      "title": "[로보락 F25] 고추기름도 한 번에",
      "duration_sec": 119.0,
      "size_bytes": 28214602,
      "video_url": "/api/v1/ai/files/e8a4328f30a2/shorts/seg_1/short.mp4",
      "thumbnail_url": "/api/v1/ai/files/e8a4328f30a2/shorts/seg_1/thumb.jpg",
      "captions": [
        {
          "text": "물걸레+진공 동시 청소",
          "highlight": "동시 청소",
          "emphasis": "feature",
          "start_ms": 14920,
          "end_ms": 18920
        }
      ],
      "violations": []
    }
  ]
}
```

`title`은 **`[상품명] AI가 정한 제목`** 형식입니다.
상품명은 `terms`의 `product_name`을 코드가 붙이고, 뒷부분만 모델이 만듭니다.
`terms`를 안 보내면 상품명 없이 제목만 나옵니다.

`captions`는 자막 수정 기능을 붙일 때 씁니다.
`violations`가 비어 있으면 검증을 모두 통과한 것입니다.

---

### 3-7. `PATCH /api/v1/ai/jobs/{job_id}/shorts/{candidate_id}/title` — 제목 수정

AI가 정한 제목을 판매자가 바꿉니다.

```json
{ "title": "[로보락 F25] 직접 정한 제목" }
```

**응답 `200`** — 수정된 쇼츠 1건 (`/shorts` 항목과 동일 구조)

이미 렌더링된 **영상 화면의 자막은 바뀌지 않습니다.** 업로드에 쓸 메타데이터 제목만 바뀝니다.

---

### 3-8. `POST /api/v1/ai/screen` — 소재 적합성 사전 판정

업로드 전에 이 영상이 쇼츠 소재로 쓸 만한지 빠르게 확인합니다.
음성 인식을 돌리지 않아 **수 초**에 끝납니다.

`multipart/form-data` — `video` 파일 하나

```json
{
  "suitable": true,
  "duration_sec": 1211,
  "total_cuts": 955,
  "cuts_per_min": 47.3,
  "moving_sec": 1189,
  "still_pct": 1.8,
  "threshold": 10.0,
  "reason": null
}
```

분당 장면 전환이 **10회 미만**이면 `suitable: false`이고 `reason`에 사유가 담깁니다.
화면이 거의 정지한 영상으로 쇼츠를 만들면 멈춘 것처럼 보이기 때문입니다.

---

### 3-9. `GET /api/v1/ai/files/{job_id}/{path}` — 결과 파일

응답의 `video_url` · `thumbnail_url`을 그대로 붙여 쓰면 됩니다.
Range 요청을 지원하므로 `<video>` 태그에 직접 넣어도 됩니다.

---

### 3-10. `GET /api/v1/ai/health` — 상태 확인

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

`ok: false`면 `notes`에 원인이 담깁니다.

---

### 3-11. `DELETE /api/v1/ai/jobs/{job_id}` — 작업 삭제

결과 파일까지 함께 지웁니다. 영상 파일이 커서 정리가 필요합니다.

---

## 4. 연동 시 주의할 점

**동시 실행에 제한이 있습니다.** 기본 2건입니다. 초과 요청은 대기열에서 기다립니다
(거부되지 않습니다). 운영 환경 규모는 협의가 필요합니다.

**서버 재시작 시 진행 중이던 작업은 복구되지 않습니다.** 완료된 결과 파일은 남지만
진행 중이던 작업은 다시 요청해야 합니다.

**`job_id`는 서버가 발급합니다.** 클라이언트가 지정할 수 없습니다.
BE 쪽 방송 ID와 매핑해서 보관해 주세요.

**폴링 간격은 3~5초를 권장합니다.** 음성 인식 단계가 20분 영상에서 약 5분 걸립니다.

**`/timeline`과 `/candidates`는 서로 독립입니다.** 타임라인 생성이 실패해도
쇼츠 후보는 정상적으로 나옵니다. 반대도 마찬가지입니다.

**HTTP 상태 코드**

| 코드 | 의미 |
| --- | --- |
| `202` | 접수됨 (백그라운드 처리 시작) |
| `404` | `job_id` 또는 리소스 없음 |
| `409` | 아직 준비되지 않음 (처리 중) 또는 소재 부적합 |
| `422` | 요청 형식 오류 |

---

## 5. 연동 예시 (Python)

```python
import time
import requests

BASE = "http://localhost:8000/api/v1/ai"

# ① 업로드
with open("broadcast.mp4", "rb") as v, open("chat.json", "rb") as c:
    r = requests.post(
        f"{BASE}/jobs",
        files={"video": v, "comments": c},
        data={"broadcast_start_ms": 1700000000000},  # 채팅이 절대시각일 때만
    )
job_id = r.json()["job_id"]

# ② 분석 완료까지 폴링
while True:
    s = requests.get(f"{BASE}/jobs/{job_id}").json()
    if s["status"] == "ready_for_selection":
        break
    if s["status"] in ("rejected", "failed"):
        raise RuntimeError(s.get("error") or s["screen"]["reason"])
    time.sleep(5)

# ③ 타임라인 — 여기서 끝내도 됨
timeline = requests.get(f"{BASE}/jobs/{job_id}/timeline").json()
for ch in timeline["chapters"]:
    print(ch["timestamp"], ch["category_name"], ch["title"])

# ④ 쇼츠 후보 → 판매자 선택
cands = requests.get(f"{BASE}/jobs/{job_id}/candidates").json()["candidates"]
picked = [c["id"] for c in cands[:2]]

requests.post(f"{BASE}/jobs/{job_id}/select",
              json={"candidate_ids": picked, "layout": "crop"})

# ⑤ 렌더링 완료까지 폴링
while requests.get(f"{BASE}/jobs/{job_id}").json()["status"] != "done":
    time.sleep(5)

shorts = requests.get(f"{BASE}/jobs/{job_id}/shorts").json()["shorts"]
for s in shorts:
    print(s["title"], BASE + s["video_url"])
```

---

## 6. 문의

- 저장소 — https://github.com/KT-Cloud-Tech-Up-team6/Funddit-Ai-highlight
- Swagger — 서버 실행 후 `/docs`
- 로컬 확인용 화면 — `/view/{job_id}` (타임라인·쇼츠를 브라우저에서 바로 확인)
