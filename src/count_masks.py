# -*- coding: utf-8 -*-
"""count_masks.py — 이미 추출된 글의 '개인정보 가림 건수'를 세어 창고에 채우는 정비 명령 (2차)

왜: 글목록 화면이 글마다 본문 파일을 다시 열어 개인정보를 다시 세느라 느렸다. 같은 글 + 같은
    규칙이면 그 숫자는 늘 같으므로 창고에 저장해 둔다. 함께 저장하는 '규칙 지문'이 지금 규칙과
    다르면 화면은 옛 숫자를 쓰지 않는다 → 그때 이 명령을 한 번 돌리면 다시 맞는다.
    (예: 직원 이름 목록에서 '테스트'를 빼면 지문이 바뀐다 → 이 명령 한 번 = 끝)

★ 불변 1: 창고에 넣는 건 건수(정수)와 지문뿐. 원본 문자열(전화번호·이름)은 넣지 않는다.
★ 불변 4: 본문 파일은 읽기만 한다(절대 쓰지 않음).

사용:
  python src/count_masks.py          # 아직 안 셌거나 규칙이 바뀐 글만 (재실행 안전)
  python src/count_masks.py --all    # 전부 다시 세기(세는 방식 자체를 고쳤을 때)

중간에 Ctrl+C로 멈춰도 그때까지 센 것은 남는다(다시 실행하면 이어서 센다).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import ROOT_DIR, get_connection, init_db  # noqa: E402
import masking  # noqa: E402

COMMIT_EVERY = 100  # 묶음 커밋 — 중간에 멈춰도 진행분 보존


def count_one(body_clean_path, regex_pats, staff_names):
    """본문 파일(body_clean)을 읽어 가림 '건수'만 센다. 파일이 없으면 None(=못 셈).

    ★ 불변 1: hits[i]['original']은 읽지 않는다 — 개수만 쓴다.
    세는 재료(body_clean)와 방식(mask_text)은 상세 화면의 viewer.mask_type_counts와 동일하므로
    저장된 건수 = 그 화면의 '총 N건'과 일치한다.
    """
    if not body_clean_path:
        return None
    fp = os.path.join(ROOT_DIR, body_clean_path)
    if not os.path.exists(fp):
        return None
    with open(fp, encoding="utf-8") as f:
        text = f.read()
    _, hits = masking.mask_text(text, regex_pats, staff_names)
    return len(hits)


def targets(conn, fingerprint, recount_all=False):
    """셀 대상 = 본문 있는 글. 기본은 '아직 안 셌거나 규칙이 바뀐 글'만(재실행·중단 안전)."""
    q = "SELECT post_id, body_clean_path FROM posts WHERE body_clean_path IS NOT NULL"
    params = []
    if not recount_all:
        q += (" AND (mask_count IS NULL OR mask_rules_fingerprint IS NULL "
              "OR mask_rules_fingerprint <> ?)")
        params.append(fingerprint)
    q += " ORDER BY post_id"
    return conn.execute(q, params).fetchall()


def run(recount_all=False, db_path=None):
    conn = get_connection(db_path) if db_path else get_connection()
    # corpus/(그리고 data/)가 구글드라이브 연결 — 동기화 중 순간 잠금 대기(bulk_extract 선례)
    conn.execute("PRAGMA busy_timeout = 60000")
    init_db(conn)

    # ★ 규칙·이름 목록은 시작할 때 한 번만 로드(글마다 재조회하던 것이 느림의 원인이었다)
    fingerprint = masking.rules_fingerprint(conn)
    regex_pats = masking.load_regex_patterns(conn)
    staff_names = masking.load_staff_names(conn)

    total = conn.execute(
        "SELECT COUNT(*) c FROM posts WHERE body_clean_path IS NOT NULL").fetchone()["c"]
    rows = targets(conn, fingerprint, recount_all)
    stats = {"total": total, "counted": 0, "skipped": total - len(rows),
             "missing_file": 0, "hits_total": 0}
    print(f"본문 있는 글 {total}건 · 이번에 셀 글 {len(rows)}건"
          f" (이미 지금 규칙으로 세어둔 글 {stats['skipped']}건은 건너뜀)", flush=True)

    missing_ids = []
    try:
        for i, r in enumerate(rows, 1):
            n = count_one(r["body_clean_path"], regex_pats, staff_names)
            if n is None:                      # 본문 파일이 없는 글 — 조용히 넘기지 않고 아래에서 보고
                stats["missing_file"] += 1
                if len(missing_ids) < 10:
                    missing_ids.append(r["post_id"])
                continue
            # 숫자 2칸만 쓴다. updated_at은 건드리지 않는다(글 내용이 바뀐 게 아니라 센 결과일 뿐).
            conn.execute(
                "UPDATE posts SET mask_count=?, mask_rules_fingerprint=? WHERE post_id=?",
                (n, fingerprint, r["post_id"]))
            stats["counted"] += 1
            stats["hits_total"] += n
            if i % COMMIT_EVERY == 0:
                conn.commit()
                print(f"  [{i}/{len(rows)}] 센 글 {stats['counted']}건", flush=True)
    except KeyboardInterrupt:
        print("\n멈춤 요청 — 여기까지 센 것은 저장합니다(다시 실행하면 이어서 셉니다).", flush=True)
    conn.commit()

    print(f"완료: 센 글 {stats['counted']}건 · 건너뜀 {stats['skipped']}건 · "
          f"본문 파일 없음 {stats['missing_file']}건 · 가림 합계 {stats['hits_total']}건", flush=True)
    if stats["missing_file"]:
        print(f"  ↳ 본문 파일을 찾지 못한 글(앞 10건): {missing_ids}"
              f" — 이 글들은 화면에 '다시 세기 필요'로 남습니다.", flush=True)
    conn.close()
    return stats


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if args and args[0] != "--all":
        print(__doc__)
        return 0 if args[0] in ("-h", "--help") else 1
    run(recount_all=bool(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
