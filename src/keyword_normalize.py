# -*- coding: utf-8 -*-
"""keyword_normalize.py — 키워드 변형을 '주제'로 묶는 단일 출처(규칙 정규화). (D1/D2)

배경: posts.keyword는 1,829종의 변형으로 쪼개져 있다("사회복지사2급 취업/비용/시험"이
각각 다른 값). 그대로 집계하면 한 주제가 수십 조각으로 흩어져 조회수·트렌드가 과소집계된다.
이 모듈은 keyword에서 '의도·수식 꼬리말'을 규칙으로 떼어 '주제 core'로 묶는다.

★ 지어내지 않는다(불변): 남는 core는 실제 keyword 문자열의 일부다. 사람이 관리하는 건
  두 목록뿐 —
  - TAILS  : 떼어낼 생성어(취업·비용·시험일정 등, 실데이터 관찰 기반)
  - ALIAS  : 검수(D2)로 확인된 동의어(어순·표기 차이만). 새 주제를 만들지 않는다.
★ 단일 출처: viewer·분석·생성이 모두 normalize()를 부른다. 여기만 고치면 전부 반영.
★ 급(1급/2급/3급)은 의미 있는 구분이라 떼지 않는다(사회복지사2급 ≠ 사회복지사1급).

자체 검증:  python src/keyword_normalize.py
"""
import re

# 의도/수식 꼬리말·일반어 — 이 말들을 keyword에서 떼면 '주제 core'가 남는다.
# (긴 표현이 먼저 지워지도록 normalize에서 길이 역순 정렬해 적용)
TAILS = [
    "자격증취득방법", "자격증 취득방법", "취득방법", "자격증", "응시자격", "응시조건", "응시료",
    "시험일정", "시험 일정", "시험", "난이도", "합격률", "기출", "필기", "실기", "커리큘럼", "과목",
    "취업", "전망", "연봉", "되는법", "되는 법", "하는일", "하는 일", "비용", "기간", "실습", "교육원",
    "학원", "인강", "독학", "후기", "뜻", "종류", "조건", "방법", "안내", "정보", "총정리", "준비", "공부",
    "학점은행제", "학점", "학위", "학사학위", "전문학사", "편입", "온라인", "신청", "등록", "선임기준",
    "혜택", "자격",
]

# 꼬리말을 다 떼고 이것만 남으면 '주제 없음'(None) — 일반어라 주제로 못 씀
GENERIC = {"학점은행제", "자격증", "방법", "정보", "온라인"}

# 검수(D2)로 확인된 동의어 — 왼쪽(변형 core) → 오른쪽(대표 주제). 어순·표기 차이만.
# 새 주제를 만들지 않으며, 오른쪽은 실제로 존재하는 다른 keyword의 core다.
# 확장은 이 목록에 한 줄 추가로 끝난다(단일 출처).
ALIAS = {
    "미용종합면허증": "종합미용면허증",   # 어순만 다름
    "사이버대학교": "사이버대학",         # 표기 차이(교)
}

# 성능·정확 — 정렬은 불변이라 모듈 로드 시 한 번만(호출마다 재정렬 안 함)
_TAILS_SORTED = sorted(TAILS, key=len, reverse=True)
_TAILS_SET = set(TAILS)


def _strip_token(tok):
    """한 토큰의 '뒤쪽'에 붙은 꼬리말만 반복 제거. 앞·중간은 건드리지 않는다.
    ★ 문자열 어디서든 지우면 고유명이 깨진다("정보처리기사"의 정보, "취업성공패키지"의 취업).
      꼬리말은 진짜 '뒤'에 붙는 것이므로 endswith로만 떼어 과제거를 막는다."""
    changed = True
    while changed:
        changed = False
        for t in _TAILS_SORTED:
            if len(tok) > len(t) and tok.endswith(t):
                tok = tok[:-len(t)]
                changed = True
                break
    return tok


def normalize(keyword):
    """keyword(원본 문자열) → 주제 core(str) 또는 None(일반어만 남을 때).

    1) 공백으로 토큰 분리
    2) 순수 꼬리말 토큰(취업·비용 등)은 통째로 제거
    3) 남은 토큰은 '뒤에 붙은' 꼬리말만 떼어냄(앞·중간 미변경 → 고유명 보존)
    4) GENERIC만 남으면 None
    5) ALIAS 대표어로 치환
    """
    kept = []
    for tok in (keyword or "").split():
        if tok in _TAILS_SET:      # 순수 꼬리말 토큰은 버림
            continue
        tok = _strip_token(tok)
        if tok and tok not in _TAILS_SET:
            kept.append(tok)
    s = " ".join(kept).strip()
    if not s or s in GENERIC:
        return None
    return ALIAS.get(s, s)


def _selftest():
    cases = {
        "사회복지사2급 취업": "사회복지사2급",
        "사회복지사2급 비용": "사회복지사2급",
        "사회복지사2급자격증취득방법": "사회복지사2급",
        "사회복지사되는법": "사회복지사",             # 붙은 꼬리말(되는법) 접미 제거
        "사회복지사1급": "사회복지사1급",           # 급은 유지 → 2급과 다른 주제
        "사회복지사": "사회복지사",                 # 급 없는 것도 별도 주제
        "미용종합면허증": "종합미용면허증",           # ALIAS 어순
        "종합미용면허증 난이도": "종합미용면허증",
        "사이버대학교 학점은행제": "사이버대학",       # ALIAS 표기 + TAILS
        # ★ 과제거 가드 — 고유명 앞·중간의 '정보/취업/시험' 등은 지우면 안 됨(접미만 제거)
        "정보처리기사": "정보처리기사",
        "정보보안기사 시험일정": "정보보안기사",
        "취업성공패키지": "취업성공패키지",
        "시험감독관": "시험감독관",
        "학점은행제": None,                         # 일반어만 → 주제 없음
        "자격증": None,
    }
    bad = 0
    for kw, want in cases.items():
        got = normalize(kw)
        mark = "OK " if got == want else "!! "
        if got != want:
            bad += 1
        print(f"  {mark}{kw!r:32} → {got!r}  (기대 {want!r})")
    print(f"\n실패 {bad}건 / 총 {len(cases)}건")
    return bad == 0


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ok = _selftest()
    raise SystemExit(0 if ok else 1)
