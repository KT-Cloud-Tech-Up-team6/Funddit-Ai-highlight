"""오프라인 스모크 테스트 — API 키·FFmpeg 없이 게이트와 파이프라인 로직을 검증한다.

  python -m eval.smoke_offline

1) 정상 mock 응답이 게이트를 통과하는지
2) 조작된(환각) 응답을 게이트가 잡아내는지
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from poc.comments import find_p2_windows, load_comments
from poc.gates import has_errors, validate_captions, validate_segments
from poc.llm import LLM
from poc.m1_segments import run_m1
from poc.m2_captions import assign_timing, run_m2
from poc.models import Caption, Segment
from poc.numbers import extract_numbers, parse_korean_number
from poc.transcript import load_cues

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((PASS if ok else FAIL, name))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    cues = load_cues(DATA / "mock_transcript.json")
    cuesheet = json.loads((DATA / "mock_cuesheet.json").read_text(encoding="utf-8"))
    terms = json.loads((DATA / "product_terms.json").read_text(encoding="utf-8"))

    print("== 0. 한글 수사 파서 ==")
    check("만 -> 10000", parse_korean_number("만") == 10000)
    check("삼십구만 구천 -> 399000", parse_korean_number("삼십구만 구천") == 399000)
    check("사 점 삼 -> 4.3", parse_korean_number("사 점 삼") == 4.3)
    check("육십 -> 60", parse_korean_number("육십") == 60)
    check("추출: '지금 최대 만 파스칼까지' ⊇ 10000", 10000.0 in extract_numbers("지금 최대 만 파스칼까지 올라간 상태입니다."))

    print("== 1. M1 정상 경로 ==")
    llm1 = LLM(mock_file=DATA / "mock_llm" / "m1_response.json")
    segments, v1 = run_m1(cues, cuesheet, llm1)
    check("구간 4개 반환", len(segments) == 4)
    check("게이트 ERROR 없음", not has_errors(v1), "; ".join(x.message for x in v1 if x.level == "ERROR"))
    check("시각을 코드가 채움", all(s.start_ms is not None and s.end_ms is not None for s in segments))
    check("전 구간 60~120초", all(60_000 <= s.end_ms - s.start_ms <= 120_000 for s in segments))

    print("== 2. P2 댓글 수 세기 ==")
    p2 = find_p2_windows(load_comments(DATA / "mock_comments.json"))
    check("질문 집중 구간 1개 탐지", len(p2) == 1)
    if p2:
        check("탐지 위치가 QnA 구간(320~415초)과 겹침", p2[0]["start_ms"] < 415_000 and p2[0]["end_ms"] > 320_000)

    print("== 3. M2 정상 경로 (P1 선택 가정) ==")
    p1 = next(s for s in segments if s.part_type == "P1")
    llm2 = LLM(mock_file=DATA / "mock_llm" / "m2_response.json")
    captions, v2 = run_m2(p1, cues, terms, llm2)
    check("자막 4개 유지", len(captions) == 4)
    check("게이트 ERROR 없음", not has_errors(v2), "; ".join(x.message for x in v2 if x.level == "ERROR"))
    check("표시 시각이 쇼츠 로컬 기준", all(0 <= c.start_ms < (p1.end_ms - p1.start_ms) for c in captions))
    ordered = all(a.end_ms <= b.start_ms for a, b in zip(captions, captions[1:]))
    check("자막 표시 시간 겹침 없음", ordered)

    print("== 4. 게이트가 환각을 잡는가 (조작 테스트) ==")
    # 4-1. 없는 문장을 evidence로 넣은 구간
    bad_seg = copy.deepcopy(segments)
    bad_seg[0].evidence = ["이 제품은 흡입력이 이만 파스칼입니다."]
    v = validate_segments(bad_seg, cues)
    check("가짜 evidence 차단", any(x.code == "SEG_EVIDENCE_FAKE" for x in v))

    # 4-2. 존재하지 않는 cue_id
    bad_seg = copy.deepcopy(segments)
    bad_seg[0].end_cue_id = "t_999"
    v = validate_segments(bad_seg, cues)
    check("없는 cue_id 차단", any(x.code == "SEG_CUE_MISSING" for x in v))

    # 4-3. 근거 없는 숫자 자막 (20,000Pa — 발화·용어 어디에도 없음)
    fake = [Caption(text="최대 흡입력 20,000Pa", source_cue_id="t_030", emphasis="spec",
                    source_text="지금 최대 만 파스칼까지 올라간 상태입니다.")]
    v = validate_captions(p1, fake, cues, terms)
    check("숫자 환각(20,000Pa) 차단", any(x.code == "CAP_NUMBER_FAKE" for x in v))

    # 4-4. 용어 목록에 있는 숫자는 허용 (399,000원 — 발화는 '삼십구만 구천 원')
    ok_cap = [Caption(text="얼리버드 399,000원", source_cue_id="t_046", emphasis="benefit",
                      source_text="그런데 오늘 얼리버드 펀딩가는 삼십구만 구천 원입니다.")]
    p3 = next(s for s in segments if s.part_type == "P3")
    v = validate_captions(p3, ok_cap, cues, terms)
    check("한글 수사 근거 숫자 허용", not any(x.code == "CAP_NUMBER_FAKE" for x in v))

    # 4-5. source_text 변조
    fake = [Caption(text="먼지 그대로 흡입", source_cue_id="t_026", emphasis="result",
                    source_text="먼지가 하나도 남지 않고 완벽하게 사라집니다.")]
    v = validate_captions(p1, fake, cues, terms)
    check("source_text 변조 차단", any(x.code == "CAP_SOURCE_FAKE" for x in v))

    # 4-6. 구간 밖 cue 참조
    fake = [Caption(text="얼리버드 399,000원", source_cue_id="t_046", emphasis="benefit",
                    source_text="그런데 오늘 얼리버드 펀딩가는 삼십구만 구천 원입니다.")]
    v = validate_captions(p1, fake, cues, terms)
    check("선택 구간 밖 cue 차단", any(x.code == "CAP_CUE_OUT_OF_SEGMENT" for x in v))

    print("=" * 50)
    fails = [n for r, n in results if r == FAIL]
    print(f"결과: {len(results) - len(fails)}/{len(results)} 통과" + (f" — 실패: {fails}" if fails else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
