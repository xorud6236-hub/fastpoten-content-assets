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

# 재시도해도 소용없는 확정 실패 → 대상에서 영구 제외(실패 무한루프 차단).
# '링크오류'는 아래 카페 URL 필터에서 이미 탈락하지만(normalized_url NULL), 이중 방어로 남긴다.
TERMINAL_FAILURES = ("실패-삭제된글", "실패-비공개게시판", "링크오류")
RETRY_CAP = 2  # 기타 실패는 총 2회까지만 재시도(extraction_logs MAX(attempt_no)>=2면 제외)

# 추출 대상 조건(위 두 가드 포함). --list의 '재시도 대상'과 _targets가 같은 조건을 쓰도록 한 곳에 둔다.
_ELIGIBLE_SQL = (
    "p.body_raw_path IS NULL "
    "AND p.normalized_url LIKE '%cafe.naver.com/%' "
    f"AND (p.extraction_status IS NULL OR p.extraction_status NOT IN ({','.join('?' * len(TERMINAL_FAILURES))})) "
    "AND COALESCE((SELECT MAX(l.attempt_no) FROM extraction_logs l WHERE l.post_id=p.post_id),0) < ?"
)
_ELIGIBLE_PARAMS = list(TERMINAL_FAILURES) + [RETRY_CAP]


def _list(conn):
    print("시트별 전체 / 추출완료 / 남음 / 재시도 대상(가드 통과):")
    rows = conn.execute(
        "SELECT COALESCE(p.source_sheet,'(미상)') s, COUNT(*) n, "
        "SUM(CASE WHEN p.body_raw_path IS NOT NULL THEN 1 ELSE 0 END) done, "
        f"SUM(CASE WHEN {_ELIGIBLE_SQL} THEN 1 ELSE 0 END) todo "
        "FROM posts p GROUP BY p.source_sheet ORDER BY n DESC", _ELIGIBLE_PARAMS).fetchall()
    for r in rows:
        remain = r["n"] - (r["done"] or 0)
        print(f"  {r['s'][:24]:24} 전체 {r['n']:>5} · 추출 {r['done'] or 0:>5} · "
              f"남음 {remain:>5} · 재시도 대상 {r['todo'] or 0:>5}")
    print("  (남음 − 재시도 대상 = 제외된 글: 확정 실패·재시도 상한 초과·카페 링크 아님)")


def _targets(conn, sheets, limit=None):
    # process_one이 posts 행 전체를 받으므로 p.* 그대로 넘긴다(재조회 불필요).
    q = f"SELECT p.* FROM posts p WHERE {_ELIGIBLE_SQL}"
    params = list(_ELIGIBLE_PARAMS)
    if sheets:
        ph = ",".join("?" * len(sheets))
        q += f" AND p.source_sheet IN ({ph})"
        params += list(sheets)
    q += " ORDER BY p.source_sheet, p.post_id"
    rows = conn.execute(q, params).fetchall()
    return rows[:limit] if limit else rows


def run(sheets, limit=None):
    conn = get_connection()
    # data/는 구글드라이브 스트리밍 junction — Drive가 DB 파일을 동기화하며 잠깐 잠근다.
    # 기본 busy timeout(5s)로는 업로드 중 'database is locked'로 터짐 → 60s로 상향(순간 잠금 대기).
    conn.execute("PRAGMA busy_timeout = 60000")
    init_db(conn)
    targets = _targets(conn, sheets, limit)
    label = ", ".join(sheets) if sheets else "전체(추출 안 된 카페 글)"
    print(f"대상 {len(targets)}건 · 예상 ~{len(targets) * 12.5 / 3600:.1f}시간 ({label})", flush=True)
    ok = fail = 0
    for i, row in enumerate(targets, 1):
        try:
            res = ex.process_one(conn, row)  # fetch→파싱→마스킹→저장·실패기록, 건마다 커밋
            if res and res.get("ok"):
                ok += 1
            else:
                fail += 1
        except Exception as e:  # 어떤 실패도 멈추지 않음
            fail += 1
            print(f"  [{i}] 예외 post_id={row['post_id']}: {e!r}", flush=True)
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
        conn.execute("PRAGMA busy_timeout = 60000")  # 읽기도 Drive 동기화 잠금에 걸릴 수 있음
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
