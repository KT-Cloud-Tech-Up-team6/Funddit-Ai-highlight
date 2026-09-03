# 하이라이트 쇼츠 자동화 PoC — 기초 환경

10분 라이브 펀딩 방송 1건 → 파트 구간 분할(M1) → 판매자 선택 → 포인트 자막(M2) → FFmpeg 쇼츠 렌더링.

## 계획서 대비 변경점 (구조적 안전장치)

1. **M2도 시각(ms)을 만들지 않는다** — 모델은 `source_cue_id`만 지정. 표시 시각·길이(1.5~4초)·겹침 해소·쇼츠 로컬 타임라인 변환은 전부 코드([m2_captions.py](poc/m2_captions.py))가 처리.
2. **기계 검증 항목은 코드 게이트** ([gates.py](poc/gates.py)) — 사람 눈은 주관 항목(라벨 적절성, 크롭 품질, "올려도 될 만한가")에만 쓴다.
   - ERROR(탈락·재시도): 없는 cue_id, 구간 겹침, 자막에 없는 evidence, source_text 변조, **근거 없는 숫자**(한글 수사 "삼십구만 구천 원" ↔ "399,000원" 대조 포함)
   - WARN(참고): 60~120초 밖, 12자 초과, 개수 규칙
3. **Whisper 큐 문장 재병합** — STT 세그먼트가 문장 중간에서 끊기는 문제 대응 ([transcript.py](poc/transcript.py) `merge_to_sentences`).

## 빠른 시작

```powershell
# 1) 오프라인 스모크 — API 키·FFmpeg 없이 게이트/로직 검증 (환각 조작 테스트 포함)
python -m eval.smoke_offline

# 2) 데모 — 목데이터(로보락 F25 ACE 가상 방송 9분) + 테스트 영상으로
#    M1 → P2 → M2 → 쇼츠 렌더링(9:16 크롭 + 자막 번인)까지 전 과정 실행
python -m poc.pipeline demo
# 결과: out/short_p1.mp4, out/short_p1_thumb.jpg, out/segments.json, out/p1_captions.json
```

## 실제 방송으로 돌릴 때 (STEP 3~7)

실제 영상: `data/input/roborock_f25.mp4` (로보락 F25, KT알파쇼핑 "위대한 쇼픽", 9분 20초, 1280x720). git에는 안 들어감(`*.mp4`).

```powershell
pip install -r requirements.txt          # google-genai + faster-whisper(+CUDA 런타임) + google-cloud-speech
copy .env.example .env                   # 키 입력 후 아래 한 줄로 환경변수 로드
Get-Content .env | ? { $_ -match '^\s*[^#].*=' } | % { $k,$v = $_ -split '=',2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim()) }

.\run_real.ps1 stt            # Whisper small/large-v3 (+Google STT, 자격증명 있으면) → out/real/transcript_*.json + 비교표
.\run_real.ps1 m1 large       # M1 구간 분할 — Flash-Lite, Flash 두 번 → out/real/segments_large_*.json
.\run_real.ps1 m2 large P1    # 선택 파트 M2 + 렌더링 (두 모델) → out/real/large_*/short_p1.mp4
```

개별 명령:

```powershell
python -m poc.pipeline stt --video 방송.mp4 --out out/transcript.json [--model small|large-v3] [--device cpu] [--terms data/real/product_terms.json]
python -m poc.pipeline stt --engine google --video 방송.mp4 --out out/transcript_google.json [--model chirp_3] [--terms ...]
python -m eval.compare_stt out/real/transcript_small.json out/real/transcript_large.json   # 처리시간·숫자 표현·병렬 텍스트
python -m poc.pipeline m1 --transcript out/transcript.json [--cuesheet 큐시트.json] --out out/segments.json
python -m poc.pipeline p2 --comments 댓글.json
python -m poc.pipeline m2 --segments out/segments.json --pick P1 --transcript out/transcript.json --terms data/real/product_terms.json --outdir out
python -m poc.pipeline render --video 방송.mp4 --captions out/p1_captions.json --out out/short_p1.mp4 [--crop-cx 0.62]
```

`--mock` 플래그를 붙이면 M1/M2가 `data/mock_llm/`의 저장 응답을 사용한다(API 불필요).
`--terms`는 상품 용어 목록을 STT 힌트(Whisper initial_prompt / Google phrase boost)로 넣는다.

### 실측 기록 (2026-09-03, 이 PC: GTX 1080 8GB, faster-whisper cuda/int8_float32)

| STT | 모델 로드 | 추론 (오디오 560초) | 실시간 배수 | 큐 수 | 숫자·제품명 |
| --- | --- | --- | --- | --- | --- |
| Whisper small | 3초 | 36초 | x15.5 | 114 | "로봐락", "이만 파스타", "열풍곤저" 등 오인식 다수. 가격(20만·40만·69만 9천·925원)은 맞음 |
| Whisper large-v3 | 292초 (첫 다운로드 포함) | 109초 | x5.1 | 134 | "로보락", "열풍건조", "온수" 정상. "이만 파스카이"(20,000Pa), "위대한 쇼핑"(쇼픽) 오인식. 가격 전부 맞음. 한 문장 중복 1회 |
| Whisper large-v3 + `--terms` (initial_prompt) | 캐시 후 수초 | 138초 | x4.1 | 212 (원시 236) | "20,000Pa"로 표기 개선, "로보락"·"F25" 유지. "쇼픽"은 여전히 "쇼핑". 세그먼트가 잘게 쪼개져("네.", "어머.") 큐 수 증가 — M1 입력이 길어짐 |
| Google STT v2 chirp_3 | — | — | — | — | 서비스 계정 키 대기 |

세로 크롭 확인 (`out/real/frames/`): 방송 화면 왼쪽 1/4이 가격·스펙 패널이라 중앙 고정 크롭(0.5)이면 진행자가 잘리고 패널만 반쯤 걸린다.
`--crop-cx 0.62`로 중심을 오른쪽으로 옮기면 진행자+제품이 들어온다. 하단 1/6은 전화번호 띠라 자막 MarginV를 260→380으로 올려 그 위에 배치.
장면마다 피사체 위치가 달라(제품 클로즈업 0.55, 진행자 시연 0.74) 고정 크롭은 한계 — 계획서 7번 "추적 크롭" 후보.

## 구조

| 경로 | 역할 |
| --- | --- |
| [poc/models.py](poc/models.py) | Cue / Segment / Caption / Violation |
| [poc/stt_whisper.py](poc/stt_whisper.py) | S1: Whisper 로컬 STT (+VAD, 문장 재병합, CUDA 자동 감지·CPU 폴백) |
| [poc/stt_google.py](poc/stt_google.py) | S1: Google STT v2 (무음 경계 55초 조각 → 동기 API, GCS 불필요, 용어 boost) |
| [poc/m1_segments.py](poc/m1_segments.py) | M1: 구간 분할 + 게이트 + cue_id→ms 조회 |
| [poc/comments.py](poc/comments.py) | P2: 댓글 수 세기 (모델 미사용) |
| [poc/m2_captions.py](poc/m2_captions.py) | M2: 포인트 자막 + 게이트 + 표시 타이밍 계산 |
| [poc/gates.py](poc/gates.py) | 코드 검증 게이트 (환각·구조 오류 차단) |
| [poc/numbers.py](poc/numbers.py) | 한글 수사 ↔ 숫자 변환 (숫자 환각 게이트용) |
| [poc/render.py](poc/render.py) | FFmpeg: 컷 + 9:16 크롭 + ASS 번인 + 썸네일 |
| [poc/prompts.py](poc/prompts.py) | M1/M2 프롬프트 (M2는 cue_id 지정 방식으로 개정) |
| [data/](data/) | 목데이터: 가상 방송 자막·댓글·큐시트·상품 용어 목록 |
| [eval/smoke_offline.py](eval/smoke_offline.py) | 오프라인 스모크 (정상 경로 + 환각 조작 테스트) |
| [eval/compare_stt.py](eval/compare_stt.py) | STT 결과 비교 (처리시간·숫자 표현 추출·병렬 텍스트) |
| [run_real.ps1](run_real.ps1) | 실제 방송 CFG-1/2/3 일괄 실행 |
| [data/real/](data/real/) | 실제 방송용 상품 용어 목록 (발화+화면 기준 초안) |

## 남은 일 (계획서 STEP 기준)

- STEP 1 (사용자): 댓글 시계열 목데이터(`data/mock_comments.json` 형식), 큐시트(선택)
- STEP 2 (사용자): `data/real/product_terms.json`의 `_check` 항목을 상품 상세로 확인
- STEP 3 (사용자): Google STT 서비스 계정 키 + 프로젝트 ID → `.env` (CFG-2/3). 생략하면 Whisper만 비교
- STEP 4~10: Gemini API 키(`.env`) 확보 후 `run_real.ps1 m1` → 체크리스트 → `m2` → 육안 판정 → 비용 정리

STT 잠정 판정: large-v3는 가격·수치를 전부 맞게 받아써서 계획서 11번 기준(숫자 틀리면 탈락) 통과. small은 제품명 오인식이 많아 탈락. 용어 힌트는 단위 표기엔 도움이 되나 큐가 잘게 쪼개지는 부작용이 있어 M1 결과 보고 결정.

메모: 이 PC는 GTX 1080(Pascal)이라 float16 불가 → int8_float32. large-v3 10분 영상 추론 약 2분. M4 맥 실측은 별도.
