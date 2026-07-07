# -*- coding: utf-8 -*-
"""masking.py — 개인정보·직원 식별정보 마스킹 (단일 출처)

CLAUDE.md 동기화 필수 쌍: "린터의 개인정보 패턴 ↔ 정제(마스킹)의 개인정보 패턴 —
패턴 정의는 한 곳에 두고 양쪽이 참조." → 패턴은 DB(rulebook_pii_patterns, CA-1 적재)에
두고, 정제(CA-3)와 린터(CA-4)가 모두 이 모듈을 통해 같은 패턴을 쓴다.

- regex 패턴: 전화번호·오픈채팅·직원 호칭(OO쌤/멘토/팀장) 등 (CA-1이 v9 §8 기준 적재)
- name_list 패턴: 직원 실명·카페 닉네임 — 정규식으로 못 잡으므로 이름 목록을 받아 정확일치 치환
  (직원 실명 목록은 staff 테이블에서 공급 → CA-2 적재분 재사용)
"""
import re

DEFAULT_NAME_REPLACEMENT = "[담당자]"


def load_regex_patterns(conn):
    """DB에서 regex형 개인정보 패턴을 읽어 (name, compiled, replacement) 목록으로."""
    rows = conn.execute(
        "SELECT name, pattern, replacement FROM rulebook_pii_patterns "
        "WHERE pattern_type='regex' AND pattern IS NOT NULL").fetchall()
    out = []
    for r in rows:
        try:
            out.append((r["name"], re.compile(r["pattern"]), r["replacement"] or "[가림]"))
        except re.error:
            continue  # 잘못된 패턴은 건너뜀(적재 시 검증되지만 방어)
    return out


def _name_regex(names):
    """이름 목록 → 경계 포함 정규식. 긴 이름부터(부분매칭 방지), 2자 이상만."""
    names = sorted({n.strip() for n in names if n and len(n.strip()) >= 2}, key=len, reverse=True)
    if not names:
        return None
    # 이름 뒤에 쌤/님/멘토/팀장/선생 등 호칭이 붙는 경우까지 함께 가림
    alt = "|".join(re.escape(n) for n in names)
    return re.compile(r"(?:" + alt + r")(?:\s?(?:쌤|님|멘토|팀장|선생님|선생))?")


def mask_text(text, regex_patterns, name_list=None,
              name_replacement=DEFAULT_NAME_REPLACEMENT):
    """text를 마스킹. (masked_text, hits) 반환.

    hits: [{"type": 패턴명, "original": 원문조각}] — 요약·검수·린트 리포트에 사용.
    """
    if text is None:
        return None, []
    hits = []
    masked = text
    for name, rx, repl in regex_patterns:
        def _sub(m, _name=name, _repl=repl):
            hits.append({"type": _name, "original": m.group(0)})
            return _repl
        masked = rx.sub(_sub, masked)
    name_rx = _name_regex(name_list or [])
    if name_rx is not None:
        def _sub_name(m):
            hits.append({"type": "직원 실명/닉네임", "original": m.group(0)})
            return name_replacement
        masked = name_rx.sub(_sub_name, masked)
    return masked, hits


def load_staff_names(conn):
    """staff 테이블(CA-2 적재)에서 이름 목록 — name_list 마스킹 재료."""
    return [r["staff_name"] for r in conn.execute(
        "SELECT staff_name FROM staff WHERE staff_name IS NOT NULL")]
