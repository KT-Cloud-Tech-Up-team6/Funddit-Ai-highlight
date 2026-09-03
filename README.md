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

```powershell
pip install -r requirements.txt          # google-genai
pip install faster-whisper               # STT 로컬 (선택)
copy .env.example .env                   # GEMINI_API_KEY 입력 후 환경변수로 로드

python -m poc.pipeline stt --video 방송.mp4 --out out/transcript.json --model small
python -m poc.pipeline m1 --transcript out/transcript.json --cuesheet data/mock_cuesheet.json --out out/segments.json
python -m poc.pipeline p2 --comments 댓글.json
# (판매자 선택을 가정하고 파트 하나 고르기)
python -m poc.pipeline m2 --segments out/segments.json --pick P1 --transcript out/transcript.json --terms data/product_terms.json --outdir out
python -m poc.pipeline render --video 방송.mp4 --captions out/p1_captions.json --out out/short_p1.mp4
```

`--mock` 플래그를 붙이면 M1/M2가 `data/mock_llm/`의 저장 응답을 사용한다(API 불필요).

## 구조

| 경로 | 역할 |
| --- | --- |
| [poc/models.py](poc/models.py) | Cue / Segment / Caption / Violation |
| [poc/stt_whisper.py](poc/stt_whisper.py) | S1: Whisper 로컬 STT (+VAD, 문장 재병합) |
| [poc/m1_segments.py](poc/m1_segments.py) | M1: 구간 분할 + 게이트 + cue_id→ms 조회 |
| [poc/comments.py](poc/comments.py) | P2: 댓글 수 세기 (모델 미사용) |
| [poc/m2_captions.py](poc/m2_captions.py) | M2: 포인트 자막 + 게이트 + 표시 타이밍 계산 |
| [poc/gates.py](poc/gates.py) | 코드 검증 게이트 (환각·구조 오류 차단) |
| [poc/numbers.py](poc/numbers.py) | 한글 수사 ↔ 숫자 변환 (숫자 환각 게이트용) |
| [poc/render.py](poc/render.py) | FFmpeg: 컷 + 9:16 크롭 + ASS 번인 + 썸네일 |
| [poc/prompts.py](poc/prompts.py) | M1/M2 프롬프트 (M2는 cue_id 지정 방식으로 개정) |
| [data/](data/) | 목데이터: 가상 방송 자막·댓글·큐시트·상품 용어 목록 |
| [eval/smoke_offline.py](eval/smoke_offline.py) | 오프라인 스모크 (정상 경로 + 환각 조작 테스트) |

## 남은 일 (계획서 STEP 기준)

- STEP 1~2: 실제 10분 방송 영상·댓글 시계열·큐시트·상품 용어 목록 확보
- STEP 3: Google STT 클라이언트 추가 후 Whisper와 비교 (현재 Whisper만 구현)
- STEP 4~10: 실제 영상으로 CFG-1/2/3 실행 → 체크리스트 판정 → 비용 정리

메모: Whisper large-v3는 이 PC(CPU)에서 느릴 수 있음 — 빠른 확인은 `--model small`, 본 측정은 M4 맥에서.
