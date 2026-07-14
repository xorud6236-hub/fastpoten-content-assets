# -*- coding: utf-8 -*-
"""bulk_extract.py — 카페 시트(들)의 본문 없는 글을 일괄 추출. 재개 가능·건마다 커밋·속도제한.

검증된 extract_cafe.process_one(파싱·마스킹·저장·불변 준수)을 그대로 반복 호출한다.
실패는 로그로 남기고 멈추지 않는다. 이미 추출된 글(body_raw_path 있음)은 자동으로 건너뛴다.

사용:
  python src/bulk_extract.py --list                 # 시트별 전체/추출완료/남음 보기
  python src/bulk_extract.py "소감아 현황" "장기요양 현황"   # 지정 시트 추출
  python src/bulk_extract.py --all                  # 추출 안 된 카페 글 전부
  python src/bulk_extract.py "의편사 현황" 100        # (숫자 인자) 그 시트에서 100건만 — 시범용

주의: 대량은 시간이 걸린다(글당 ~12초). 백그라운드/별도 터미널 권장. 중간에 멈춰도 다시 실행하면 이어감.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_cafe as ex  # noqa: E402
from db import get_connection, init_db  # noqa: E402

DELAY = 1.5  # 글 사이 대기(초) — 차단 방지


def _list(conn):
    print("시트별 전체 / 추출완료 / 남음(추출 대상):")
    rows = conn.execute(
        "SELECT COALESCE(source_sheet,'(미상)') s, COUNT(*) n, "
        "SUM(CASE WHEN body_raw_path IS NOT NULL THEN 1 ELSE 0 END) done "
        "FROM posts GROUP BY source_sheet ORDER BY n DESC").fetchall()
    for r in rows:
        remain = r["n"] - (r["done"] or 0)
        print(f"  {r['s'][:24]:24} 전체 {r['n']:>5} · 추출 {r['done'] or 0:>5} · 남음 {remain:>5}")


def _targets(conn, sheets, limit=None):
    if sheets:
        ph = ",".join("?" * len(sheets))
        q = ("SELECT post_id, normalized_url FROM posts "
             f"WHERE source_sheet IN ({ph}) AND body_raw_path IS NULL "
             "AND normalized_url LIKE '%cafe.naver.com/%' ORDER BY source_sheet, post_id")
        rows = conn.execute(q, sheets).fetchall()
    else:  # --all
        rows = conn.execute(
            "SELECT post_id, normalized_url FROM posts "
            "WHERE body_raw_path IS NULL AND normalized_url LIKE '%cafe.naver.com/%' "
            "ORDER BY source_sheet, post_id").fetchall()
    t = [(r["post_id"], r["normalized_url"]) for r in rows]
    return t[:limit] if limit else t


def run(sheets, limit=None):
    conn = get_connection()
    init_db(conn)
    targets = _targets(conn, sheets, limit)
    label = ", ".join(sheets) if sheets else "전체(추출 안 된 카페 글)"
    print(f"대상 {len(targets)}건 · 예상 ~{len(targets) * 12.5 / 3600:.1f}시간 ({label})", flush=True)
    ok = fail = 0
    for i, (pid, url) in enumerate(targets, 1):
        try:
            post = ex.find_post(conn, url)
            if post is None:
                fail += 1
            else:
                res = ex.process_one(conn, post)  # fetch→파싱→마스킹→저장, 건마다 커밋
                if res and res.get("ok"):
                    ok += 1
                else:
                    fail += 1
        except Exception as e:  # 어떤 실패도 멈추지 않음
            fail += 1
            print(f"  [{i}] 예외 post_id={pid}: {e!r}", flush=True)
        if i % 20 == 0 or i == len(targets):
            print(f"[{i}/{len(targets)}] 성공 {ok} · 실패 {fail}", flush=True)
        time.sleep(DELAY)
    print(f"완료: 성공 {ok} / 실패 {fail} / 총 {len(targets)}", flush=True)
    conn.close()
    return ok, fail


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--list":
        conn = get_connection()
        init_db(conn)
        _list(conn)
        conn.close()
        return 0
    if args[0] == "--all":
        run(None)
        return 0
    # 마지막 인자가 숫자면 limit(시범용)
    limit = None
    if len(args) >= 2 and args[-1].isdigit():
        limit = int(args[-1])
        args = args[:-1]
    run(args, limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
