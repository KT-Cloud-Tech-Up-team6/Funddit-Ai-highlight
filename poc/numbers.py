"""숫자 추출·한글 수사 변환 — M2 숫자 환각 게이트에서 사용.

주의: 한글 수사 추출은 "일반", "이번" 같은 단어에서 1, 2를 잘못 뽑을 수 있다.
게이트 방향이 "자막의 숫자는 근거 발화·상품 용어에 존재해야 한다"이므로
근거 쪽의 오탐은 허용 범위를 넓힐 뿐 검증을 깨뜨리지는 않는다.
"""
from __future__ import annotations

import re

_DIGITS = {"영": 0, "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
_SMALL = {"십": 10, "백": 100, "천": 1000}
_BIG = {"만": 10_000, "억": 100_000_000}

_KOR_CHARS = "영일이삼사오육칠팔구십백천만억"
_KOR_RUN = rf"[{_KOR_CHARS}]+(?:\s+[{_KOR_CHARS}]+)*(?:\s*점\s*[영일이삼사오육칠팔구]+)?"
_DIGIT_RUN = r"\d[\d,]*(?:\.\d+)?"


def parse_korean_number(s: str) -> float | None:
    """'삼십구만 구천' -> 399000, '만' -> 10000, '사 점 삼' -> 4.3"""
    s = s.replace(" ", "")
    if not s:
        return None
    if "점" in s:
        left, _, right = s.partition("점")
        base = parse_korean_number(left)
        if base is None or not right:
            return None
        frac = ""
        for ch in right:
            if ch in _DIGITS:
                frac += str(_DIGITS[ch])
            else:
                return None
        return base + float("0." + frac)

    total = 0
    section = 0
    cur = 0
    for ch in s:
        if ch in _DIGITS:
            cur = _DIGITS[ch]
        elif ch in _SMALL:
            section += (cur if cur else 1) * _SMALL[ch]
            cur = 0
        elif ch in _BIG:
            unit = section + cur
            total += (unit if unit else 1) * _BIG[ch]
            section = 0
            cur = 0
        else:
            return None
    return float(total + section + cur)


def extract_numbers(text: str) -> set[float]:
    """텍스트에서 숫자값 집합 추출 (아라비아 숫자 + 한글 수사)."""
    found: set[float] = set()
    for m in re.finditer(_DIGIT_RUN, text):
        try:
            found.add(float(m.group().replace(",", "")))
        except ValueError:
            pass
    for m in re.finditer(_KOR_RUN, text):
        v = parse_korean_number(m.group())
        if v is not None:
            found.add(v)
    return found
