# Funddit AI Highlight

라이브 커머스 방송을 **다시보기 타임라인**과 **하이라이트 쇼츠**로 자동 변환하는 AI 서비스입니다.

방송이 끝난 뒤 사용자가 요청하면, 영상과 댓글을 함께 분석해 두 가지 결과물을 만듭니다.

| | 다시보기 타임라인 | 하이라이트 쇼츠 |
| --- | --- | --- |
| 목적 | 시청자가 원하는 지점으로 이동 | SNS 업로드용 세로 영상 |
| 커버 범위 | 방송 전체 (빈틈 없음) | 판매자가 고른 구간만 |
| 사람 개입 | 없음 — 자동 생성 | 후보 중 선택 |
| 비용 (방송 1건) | 26원 | 21원 + 구간당 9원 |

**두 기능은 독립적입니다.** 코드(`poc/timeline.py` ↔ `poc/m1_segments.py`), 프롬프트, API, 결과 파일이 모두 분리되어 있어 하나만 사용해도 됩니다. 한쪽이 실패해도 다른 쪽은 정상 동작합니다.

---

## 빠른 시작

```bash
# 1. 설치
pip install -r requirements.txt

# 2. 키 설정 — 프로젝트 루트에 .env 파일 생성
echo "GEMINI_API_KEY=발급받은_키" > .env

# 3. 서버 실행
uvicorn api.main:app --host 0.0.0.0 --port 8000

# 4. 확인
curl http://localhost:8000/health
open http://localhost:8000/          # 작업 목록 (로컬 확인용 화면)
```

API 문서는 서버 실행 후 `/docs`에서 볼 수 있습니다.
백엔드 연동은 **[docs/API.md](docs/API.md)** 를 참고하세요.

---

## 요구 사항

| 항목 | 버전·비고 |
| --- | --- |
| Python | 3.10 이상 |
| FFmpeg | PATH에 등록 필요 (렌더링·프레임 추출) |
| GPU | 권장 — 없으면 음성 인식이 CPU로 돌아 수 배 느려짐 |
| Gemini API 키 | 필수 (`GEMINI_API_KEY`) |

### 환경 변수

`.env` 파일 또는 시스템 환경 변수로 설정합니다.

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `GEMINI_API_KEY` | — | **필수** |
| `GEMINI_MODEL` | `gemini-3.6-flash` | 모델 변경 시 |
| `SHORTS_WORKSPACE` | `./workspace` | 작업 폴더 위치 |
| `SHORTS_STT_MODEL` | `large-v3` | 음성 인식 모델 |
| `SHORTS_MIN_CUTS_PER_MIN` | `10` | 소재 적합성 기준 |
| `SHORTS_P2_MIN_COMMENTS` | `8` | 질문 집중 판정 기준 (60초 창) |
| `SHORTS_MAX_JOBS` | `2` | 동시 실행 제한 |

GPU를 쓰려면 CUDA 런타임이 필요합니다. 별도 툴킷 설치 없이 pip으로 받습니다.

```bash
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

---

## 처리 흐름

```
영상 + 댓글 업로드
   ↓
① 소재 적합성 판정      분당 장면 전환 10회 미만이면 중단
   ↓
② 음성 인식            Whisper large-v3 (로컬)
   ↓
③ 구간 분할 + 질문 집중 탐지
   ├─ 영상 내용 기반    모델이 자막을 읽고 시연·홍보·스펙 구간을 찾는다
   └─ 시청자 관심 기반  코드가 댓글 수를 세어 질문이 몰린 구간을 찾는다
   ↓
④ 타임라인 생성        방송 전체를 주제별 챕터로 나눈다
   ↓
⑤ 판매자 선택 ← 여기서 멈춘다
   ↓
⑥ 포인트 자막 + 렌더링  선택한 구간만 처리
```

`⑤`에서 멈추는 이유는 비용입니다. 자막 생성은 구간당 약 9원이므로, 선택하지 않은 구간까지 미리 만들면 낭비입니다.

---

## 사용 모델

| 단계 | 모델 | 선정 근거 |
| --- | --- | --- |
| 음성 인식 | Whisper large-v3 (로컬) | 정확도 100%, 타임코드 오차 0초, API 비용 0원 |
| 타임라인·구간 분할·자막 | Gemini 3.6 Flash | 7개 등급 × 2개 영상 × 3회 비교 결과 1위 |

모델 비교 실측은 `eval/results/`에 있습니다.

---

## 비용 (실측 기준)

방송 20분 1건 → 타임라인 + 쇼츠 3개

| 항목 | 비용 | 소요 |
| --- | --- | --- |
| 음성 인식 | 50원 (GPU 시간) | 5분 |
| 타임라인 | 26원 | 25초 |
| 구간 분할 | 21원 | 22초 |
| 포인트 자막 ×3 | 28원 | 36초 |
| 렌더링 | 0원 | 45초 |
| **합계** | **약 125원** | **6분 30초** |

같은 영상을 다시 요청하면 음성 인식 결과를 재사용해 42초 만에 끝납니다.

환율 1 USD = 1,500원, GPU 시간당 0.40 USD 가정입니다.

---

## 프로젝트 구조

```
api/            FastAPI 서버
  main.py       라우트
  jobs.py       작업 실행 (백그라운드)
  storage.py    작업별 격리 디렉터리
  schemas.py    요청·응답 모델
  settings.py   설정 (모델·임계값·경로)
  viewer.py     로컬 확인용 화면

poc/            처리 파이프라인
  motion.py     영상 움직임 분석 (소재 적합성)
  stt_whisper.py  음성 인식
  timeline.py   다시보기 타임라인
  m1_segments.py  구간 분할
  comments.py   질문 집중 구간 (댓글 수 세기)
  m2_captions.py  포인트 자막
  render.py     FFmpeg 렌더링
  gates.py      검증 게이트 (환각 차단)
  prompts.py    모델 프롬프트

eval/           평가·벤치마크
  run_dataset.py  평가 데이터셋 실행
  bench_llm.py    모델 비교
  bench_stt.py    음성 인식 비교
  cost_report.py  비용 산출

data/eval/      평가 데이터셋 119건
docs/           문서 · 아키텍처 이미지
```

---

## 검증

```bash
# 오프라인 검증 (API 키·FFmpeg 없이 게이트 로직만)
python -m eval.smoke_offline

# 평가 데이터셋 실행 (코드 판정 영역, API 과금 없음)
python -m eval.run_dataset

# 모델 비교 (API 과금 발생)
python -m eval.bench_llm --models gemini-3.6-flash --repeats 3 \
  --transcript out/real/rb2_transcript_large.json \
  --ref-segments data/real/rb2_reference_segments.json \
  --ref-facts data/real/rb2_reference_facts.json \
  --terms data/real/rb2_product_terms.json

# 비용 산출
python -m eval.cost_report --broadcasts-per-month 100
```

### 현재 검증 상태

| 영역 | 결과 |
| --- | --- |
| 소재 적합성 판정 | 13/13 |
| 음성 인식 키워드 | 49/50 |
| 질문 집중 구간 | 4/4 |
| 자막 숫자 오류 | 42회 실행 중 0건 |

---

## 환각 차단 설계

숫자가 틀린 자막은 품질 문제가 아니라 법적 문제입니다. 그래서 모델이 틀릴 수 있는 자리를 구조적으로 없앴습니다.

- 모델은 **시각(ms)을 만들지 않습니다.** 큐 번호만 지정하고 시각은 코드가 조회합니다.
- 자막의 숫자는 **근거 발화나 상품 정보에 있어야** 통과합니다. 한글 수사도 변환해 대조합니다 (`이만 파스칼` ↔ `20,000Pa`).
- 인용은 **유사도 0.90 이상**만 인정합니다. 그 아래는 경고, 0.75 미만은 차단입니다.
- 통과하지 못한 자막은 렌더링 전에 제외됩니다.

게이트 20종의 구현은 `poc/gates.py`에 있습니다.

---

## 소재 영상 조건

모든 방송이 쇼츠 소재가 되지는 않습니다.

| 영상 | 분당 장면 전환 | 판정 |
| --- | --- | --- |
| 로보락 F25 (20분) | 47.3회 | 적합 |
| 올리고365 (7분) | 45.1회 | 적합 |
| 로보락 F25 (9분, 다른 편집본) | 2.1회 | **부적합** |

장면 전환이 분당 10회 미만이면 쇼츠로 만들었을 때 화면이 멈춘 것처럼 보입니다. 업로드 단계에서 `POST /screen`으로 미리 판정할 수 있습니다.

---

## 라이선스·주의 사항

- 방송 VOD는 판매자 소유입니다. 2차 저작물 생성 동의가 이용약관에 포함되어야 합니다.
- 진행자 초상권은 방송 계약서에 쇼츠 관련 조항이 필요합니다.
- 댓글은 본문을 저장하되 분석에는 시각만 사용합니다.
- Gemini API 사용 시 입력 데이터가 학습에 쓰이지 않도록 설정을 확인하세요.
