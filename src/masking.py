# -*- coding: utf-8 -*-
"""masking.py — 개인정보·직원 식별정보 마스킹 (단일 출처)

CLAUDE.md 동기화 필수 쌍: "린터의 개인정보 패턴 ↔ 정제(마스킹)의 개인정보 패턴 —
패턴 정의는 한 곳에 두고 양쪽이 참조." → 패턴은 DB(rulebook_pii_patterns, CA-1 적재)에
두고, 정제(CA-3)와 린터(CA-4)가 모두 이 모듈을 통해 같은 패턴을 쓴다.

- regex 패턴: 전화번호·오픈채팅·직원 호칭(OO쌤/멘토/팀장) 등 (CA-1이 v9 §8 기준 적재)
- name_list 패턴: 직원 실명·카페 닉네임 — 정규식으로 못 잡으므로 이름 목록을 받아 정확일치 치환
  (직원 실명 목록은 staff 테이블에서 공급 → CA-2 적재분 재사용)
"""
import hashlib
import json
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


# ★ 세는 방식의 번호. 아래 _name_regex의 규칙(호칭 접미 목록·이름 최소 길이)을 고치면
#   가림 건수가 달라지므로 이 숫자를 1 올려라 → 저장된 옛 건수가 전부 '다시 세기 필요'가 된다.
#   (이 숫자는 rules_fingerprint의 재료다. 안 올리면 옛 숫자가 맞는 척 화면에 뜬다.)
MASK_ALGO_VERSION = 1


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


def rules_fingerprint(conn):
    """지금의 가림 규칙을 나타내는 짧은 확인값(지문).

    쓰는 곳: 글마다 저장해 둔 '가림 건수'가 아직 유효한지 판단(posts.mask_rules_fingerprint).
    지문이 달라지면 저장된 옛 건수는 못 믿음 → 화면은 '다시 세기 필요'.

    재료는 셋뿐:
      1) DB의 개인정보 패턴(load_regex_patterns) — 마스킹이 실제로 쓰는 그 함수
      2) DB의 직원 이름 목록(load_staff_names) — 마찬가지
      3) MASK_ALGO_VERSION — 코드에 박힌 규칙(호칭 접미 목록·이름 최소 길이)은 DB에 없어서
         지문이 자동으로 못 잡는다. 그 코드를 고칠 때 사람이 이 번호를 올려야 지문이 바뀐다.
    → 이 셋 밖의 변화는 지문이 잡지 못한다. 예: mask_text는 패턴을 DB 행 순서대로 치환하는데
      지문은 정렬 후 계산이라, 겹치는 패턴의 적재 순서만 바뀌면 건수가 달라져도 지문은 같다
      (일어나려면 룰북 재적재 순서가 바뀌어야 함 — 지금은 감수. 문제가 생기면 순서를 재료에 추가).

    순서에 안 흔들림: 규칙이 같으면 행 순서·이름 순서가 흔들려도 같은 지문(정렬 후 계산).

    ★ 불변 1: 반환값은 되돌릴 수 없는 해시. 패턴·이름 원문은 반환하지 않는다.
    """
    pats = sorted((name, rx.pattern, repl) for name, rx, repl in load_regex_patterns(conn))
    names = sorted({n.strip() for n in load_staff_names(conn) if n and n.strip()})
    blob = json.dumps({"algo": MASK_ALGO_VERSION, "patterns": pats, "names": names},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
