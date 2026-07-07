# -*- coding: utf-8 -*-
"""load_rulebook.py — 룰북 V4.2 최소 적재 (CA-1)

적재 범위(개발기획서 §5 CA-1): 카테고리(①시트) + 금지어(④시트) + 개인정보 패턴.
개인정보 패턴은 룰북 엑셀에 없으므로 서비스기획서 v9 §8·§8-2 정의를 seed로 넣는다.

멱등: 같은 source_version 행을 지우고 다시 넣으므로 몇 번을 실행해도 결과 동일.
`② 팩트 룰북`·`③ 매칭 규칙`·`⑤ 키워드 목록`은 CA-1 범위 밖(이후 차수에서 적재).

사용: python src/load_rulebook.py [룰북.xlsx 경로]
"""
import glob
import os
import sys

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import ROOT_DIR, get_connection, init_db  # noqa: E402

SOURCE_VERSION = "V4.2"
CATEGORY_SHEET = "① 상품 카테고리"
BANNED_SHEET = "④ 금지어 및 CTA"

# 개인정보·직원 식별정보 패턴 seed — 근거: 서비스기획서 v9 §8(마스킹)·§8-2(자동삽입 금지)
# 카페 닉네임·직원 실명은 정규식으로 못 잡으므로 name_list 자리만 마련(CA-3에서 목록 채움).
PII_PATTERN_SEEDS = [
    ("전화번호(일반)", "regex",
     r"0\d{1,2}[-.\s)]?\d{3,4}[-.\s]?\d{4}",
     "[전화번호]", "휴대폰·지역번호 형식 전화번호", "서비스기획서 v9 §8"),
    ("전화번호(대표번호)", "regex",
     r"1[568]\d{2}[-.\s]?\d{4}",
     "[전화번호]", "15xx/16xx/18xx 대표번호", "서비스기획서 v9 §8"),
    ("오픈채팅 링크", "regex",
     r"(?:https?://)?open\.kakao\.com/[^\s)\]}<>가-힣]+",
     "[오픈채팅링크]", "카카오 오픈채팅 URL(괄호·한글 앞에서 종료)", "서비스기획서 v9 §8"),
    ("직원 호칭(쌤/멘토/팀장)", "regex",
     r"[가-힣]{1,4}\s?(?:쌤|멘토|팀장)",
     "[담당자]", "OO쌤·OO멘토·OO팀장 등 내부 호칭", "서비스기획서 v9 §8-2"),
    ("직원 실명", "name_list",
     None,
     "[담당자]", "staff 테이블 확보 후 이름 목록으로 탐지(CA-3)", "서비스기획서 v9 §8"),
    ("카페 닉네임/활동명", "name_list",
     None,
     "[닉네임]", "계정별 활동명 목록 확보 후 탐지(CA-3)", "서비스기획서 v9 §8"),
]


def find_rulebook_path() -> str:
    """저장소 루트에서 룰북 엑셀을 찾는다."""
    hits = glob.glob(os.path.join(ROOT_DIR, "*룰북*.xlsx"))
    if not hits:
        raise FileNotFoundError("저장소 루트에서 '*룰북*.xlsx' 파일을 찾지 못했습니다.")
    return hits[0]


def load_categories(conn, wb) -> int:
    """①시트: No.가 숫자인 행만 카테고리로 적재."""
    ws = wb[CATEGORY_SHEET]
    conn.execute("DELETE FROM rulebook_categories WHERE source_version = ?", (SOURCE_VERSION,))
    count = 0
    for row in ws.iter_rows(values_only=True):
        no = row[0]
        if not isinstance(no, (int, float)):
            continue  # 제목·헤더 행 스킵
        top_cat, name, examples, link, kw_count, freq, active = (
            row[1], row[2], row[3], row[4], row[5], row[6], row[7])
        if not name:
            continue
        conn.execute(
            """INSERT INTO rulebook_categories
               (no, top_category, category_name, keyword_examples, credit_bank_link,
                unique_keyword_count, total_post_frequency, active, source_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (int(no), _s(top_cat), _s(name), _s(examples), _s(link),
             _i(kw_count), _i(freq),
             1 if _s(active) in (None, "Y", "y", "1") else 0,
             SOURCE_VERSION))
        count += 1
    return count


def load_banned_words(conn, wb) -> int:
    """④시트: '금지어 목록' 구간의 No.가 숫자인 행만 적재. 'CTA 문구 풀' 이후는 범위 밖."""
    ws = wb[BANNED_SHEET]
    conn.execute("DELETE FROM rulebook_banned_words WHERE source_version = ?", (SOURCE_VERSION,))
    count = 0
    in_banned_section = False
    for row in ws.iter_rows(values_only=True):
        first = _s(row[0])
        if first == "금지어 목록":
            in_banned_section = True
            continue
        if first == "CTA 문구 풀":
            break  # CTA는 CA-1 범위 밖
        if not in_banned_section:
            continue
        no = row[0]
        if not isinstance(no, (int, float)):
            continue  # 헤더 행(No. | 금지어 | ...) 스킵
        word, reason, replacement = _s(row[1]), _s(row[2]), _s(row[3])
        if not word:
            continue
        conn.execute(
            """INSERT INTO rulebook_banned_words (no, word, reason, replacement, source_version)
               VALUES (?, ?, ?, ?, ?)""",
            (int(no), word, reason, replacement, SOURCE_VERSION))
        count += 1
    return count


def load_pii_patterns(conn) -> int:
    """개인정보 패턴 seed 적재(v9 §8 기준)."""
    conn.execute("DELETE FROM rulebook_pii_patterns")
    for name, ptype, pattern, replacement, desc, source in PII_PATTERN_SEEDS:
        conn.execute(
            """INSERT INTO rulebook_pii_patterns
               (name, pattern_type, pattern, replacement, description, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, ptype, pattern, replacement, desc, source))
    return len(PII_PATTERN_SEEDS)


def _s(v):
    """셀 값 → 공백 정리 문자열(빈 값은 None)."""
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _i(v):
    return int(v) if isinstance(v, (int, float)) else None


def run(xlsx_path: str = None, db_path: str = None) -> dict:
    """룰북 최소 적재 실행. 적재 건수 dict 반환."""
    xlsx_path = xlsx_path or find_rulebook_path()
    conn = get_connection(db_path) if db_path else get_connection()
    init_db(conn)
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        counts = {
            "categories": load_categories(conn, wb),
            "banned_words": load_banned_words(conn, wb),
            "pii_patterns": load_pii_patterns(conn),
        }
        conn.commit()
    finally:
        wb.close()
        conn.close()
    return counts


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    xlsx = sys.argv[1] if len(sys.argv) > 1 else find_rulebook_path()
    print(f"룰북 파일: {os.path.basename(xlsx)}")
    counts = run(xlsx)
    print(f"적재 완료 (source_version={SOURCE_VERSION}, 재실행 안전):")
    print(f"  - 카테고리      : {counts['categories']}건")
    print(f"  - 금지어        : {counts['banned_words']}건")
    print(f"  - 개인정보 패턴 : {counts['pii_patterns']}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
