# -*- coding: utf-8 -*-
"""ingest_excel.py — 엑셀 '○○ 현황' 시트 적재 (CA-2, Phase 1-A)

기준: 개발기획서 v1 §5 CA-2 + 서비스기획서 v9 §1-A·§5.
- 대상: 시트명에 '현황'이 들어간 탭만. 마케팅표·키워드·계정정보·휴일선정표는 자동 제외.
- 각 행의 링크를 URL 정규화(카페ID·게시글ID 기준) → 중복 제거 → posts(메타) 적재.
- 순위 텍스트는 reference_signals로 적재(성과 아님 — 참고 신호, rank_bucket 부여).
- 시트 구조가 제각각이라 2단계 파싱:
    1) 헤더 블록 탐지: '링크' 헤더 셀을 앵커로 좌우 컬럼 역할을 헤더명으로 매핑
       (한 시트에 옆으로 여러 표가 붙은 경우 각각 별도 블록으로 처리)
    2) 폴백: 어떤 블록에도 안 잡힌 URL 셀은 위치 휴리스틱으로 수습(헤더 없는 표 대응)
- 멱등: 이미 적재된 normalized_url / (시트,행) 은 건너뜀 → 재실행해도 중복 누적 없음.

사용: python src/ingest_excel.py [엑셀 경로]
"""
import datetime
import glob
import os
import re
import sys
import urllib.parse

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import ROOT_DIR, get_connection, init_db  # noqa: E402

# 헤더가 이 이름일 때 해당 필드로 매핑 (완전일치 기준, 공백 제거 후)
RANK_PRIORITY = [  # 블록에 여러 순위 컬럼이 있으면 앞선 것 하나만 신호로 채택
    "통합탭 변동 순위",
    "순위(view통합)",
    "순위",
    "카페탭순위",
    "카페탭순위(1212이후)",
]


# ---------------------------------------------------------------------------
# URL 정규화 — 카페명·게시글ID 기준 표준형: https://cafe.naver.com/{cafe}/{id}
# ---------------------------------------------------------------------------
_URL_PATTERNS = [
    re.compile(r"cafe\.naver\.com/ca-fe/web/cafes/([^/?#]+)/articles/(\d+)"),
    re.compile(r"cafe\.naver\.com/([A-Za-z0-9_-]+)/(\d+)"),
]


def normalize_url(raw: str):
    """네이버 카페 URL → (정규화 URL, 오류사유). 글ID 없으면 (None, '링크오류')."""
    if not raw or "cafe.naver.com" not in raw:
        return None, "링크오류"
    u = raw.strip()
    # iframe형: cafe.naver.com/xxx?iframe_url_utf8=%2FArticleRead.nhn%253F...articleid%3D123
    if "iframe_url" in u:
        decoded = urllib.parse.unquote(urllib.parse.unquote(u))
        m = re.search(r"articleid=(\d+)", decoded, re.IGNORECASE)
        c = re.search(r"cafe\.naver\.com/([A-Za-z0-9_-]+)", u)
        if m and c:
            return f"https://cafe.naver.com/{c.group(1).lower()}/{m.group(1)}", None
    for pat in _URL_PATTERNS:
        m = pat.search(u)
        if m:
            cafe, article = m.group(1).lower(), m.group(2)
            if cafe in ("ca-fe",):  # 안전장치
                continue
            return f"https://cafe.naver.com/{cafe}/{article}", None
    return None, "링크오류"  # 카페 홈 링크 등 글ID 없음


# ---------------------------------------------------------------------------
# 값 정규화
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"(\d{4})[.,\-/년\s]+(\d{1,2})[.,\-/월\s]+(\d{1,2})")


def normalize_date(v):
    """날짜 → 'YYYY-MM-DD'. 못 읽으면 원문 그대로(정보 손실 방지)."""
    if v is None:
        return None
    if isinstance(v, datetime.datetime):
        return v.date().isoformat()
    if isinstance(v, datetime.date):
        return v.isoformat()
    s = str(v).strip()
    m = _DATE_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # 연도 오타(0226·2323 등)는 ISO로 둔갑시키지 않고 원문 유지 → 나중에 눈에 띔
        if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return s or None


_RANK_RE = re.compile(r"(\d+)\s*[pP]\s*(\d+)\s*등?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}$")


def parse_rank(text):
    """순위 텍스트 → (rank_value, rank_bucket). 버킷: v9 §5 기준.

    '통합탭 그외 카페탭 1p 1등' 같은 복합 텍스트는 통합탭 구간만 판단.
    페이지당 10개 가정: 절대순위 = (페이지-1)*10 + 등수.
    """
    if text is None:
        return None, "Unknown"
    t = str(text).strip()
    if not t:
        return None, "Unknown"
    frag = t
    if "통합탭" in t:  # 통합탭 구간만 잘라 판단(카페탭 순위와 혼동 방지)
        m = re.search(r"통합탭\s*(.*?)(?=카페탭|$)", t, re.DOTALL)
        if m:
            frag = m.group(1)
    m = _RANK_RE.search(frag)
    if m:
        rank = (int(m.group(1)) - 1) * 10 + int(m.group(2))
        if rank <= 3:
            return rank, "Top3"
        if rank <= 10:
            return rank, "Top10"
        if rank <= 30:
            return rank, "Top30"
        return rank, "Other"
    if "그외" in frag or "그 외" in frag:
        return None, "Other"
    if "미반영" in frag or "누락" in frag:
        return None, "Not Exposed"
    return None, "Unknown"  # 변동없음·통합탭·카페탭·인기글 등 판단 불가


def _s(v):
    if v is None:
        return None
    s = str(v).strip().strip("()")  # 아이디가 (id) 로 적힌 행 대응
    return s if s else None


def sheet_cafe_name(title: str) -> str:
    """시트명 → 카페명: '공준모 현황(예전꺼)' → '공준모'."""
    name = re.sub(r"\(.*?\)", "", title).replace("현황", "").strip()
    return name


# ---------------------------------------------------------------------------
# 블록 탐지 — '링크' 헤더 셀 앵커 기반
# ---------------------------------------------------------------------------
class Block:
    def __init__(self, header_row, link_col):
        self.header_row = header_row
        self.link_col = link_col
        self.kw_col = link_col - 2      # 표준 배열: 키워드|키워드 개수|링크
        self.cafe_col = None            # '추가턴 카페 현황'만 카페명 컬럼 보유
        self.account_col = None
        self.board_col = None
        self.date_col = None
        self.staff_col = None
        self.exposure_col = None
        self.rank_col = None
        self.rank_source = None
        self.end_col = None             # 블록 오른쪽 경계(포함)
        self.end_row = None             # 데이터 마지막 행(포함)


def detect_blocks(grid):
    """grid[r][c] (0-기준) 에서 '링크' 헤더 앵커를 찾아 블록 목록 구성."""
    anchors = []
    for r, row in enumerate(grid):
        for c, v in enumerate(row):
            if v is not None and str(v).strip() == "링크":
                anchors.append((r, c))
    blocks = [Block(r, c) for r, c in anchors]
    kw_cols = sorted({b.kw_col for b in blocks})

    for b in blocks:
        # 오른쪽 경계: 다음 블록의 키워드 컬럼 직전까지
        rights = [k for k in kw_cols if k > b.kw_col]
        b.end_col = (min(rights) - 1) if rights else (b.link_col + 13)
        # 아래 경계: 같은 링크 컬럼에 반복 헤더가 또 있으면 그 직전까지
        belows = [x.header_row for x in blocks
                  if x.link_col == b.link_col and x.header_row > b.header_row]
        b.end_row = (min(belows) - 1) if belows else (len(grid) - 1)

        header = grid[b.header_row]

        def h(col):
            if col < 0 or col >= len(header) or header[col] is None:
                return ""
            return str(header[col]).strip()

        if h(b.kw_col - 1) == "카페명":
            b.cafe_col = b.kw_col - 1
        rank_found = {}
        for col in range(b.link_col + 1, b.end_col + 1):
            name = h(col)
            if name == "아이디":
                b.account_col = col
            elif name == "게시판":
                b.board_col = col
            elif name == "날짜":
                b.date_col = col
            elif name == "담당자":
                b.staff_col = col
            elif name == "노출구좌":
                b.exposure_col = col
            elif "순위" in name and "체크" not in name and "최신화" not in name:
                rank_found[name] = col
        # 아이디 헤더가 공백인 시트(예전꺼 옆 블록) 대응: 링크 바로 오른쪽을 아이디로 추정
        if b.account_col is None and not h(b.link_col + 1):
            b.account_col = b.link_col + 1
        # 순위 컬럼: 우선순위 목록에서 첫 번째 것 하나만 채택
        for name in RANK_PRIORITY:
            if name in rank_found:
                b.rank_col, b.rank_source = rank_found[name], name
                break
        else:
            if rank_found:  # 목록 밖 이름(예: '12/12이후 1p 순위')이라도 하나 채택
                name = sorted(rank_found)[0]
                b.rank_col, b.rank_source = rank_found[name], name
    return blocks


def parse_fallback_row(row, col):
    """헤더 없는 표의 URL 셀 → 위치 휴리스틱 필드 추출.

    관측 배열: 키워드 | 키워드개수 | 링크 | 아이디 | [게시판] | [순위] | 날짜 | 담당자
    날짜 셀을 찾은 뒤 그 다음 칸을 담당자로 본다.
    """
    def cell(i):
        return row[i] if 0 <= i < len(row) else None

    keyword = _s(cell(col - 2))
    account = _s(cell(col + 1))
    board = date = staff = rank_text = None
    for i in range(col + 2, col + 7):
        v = cell(i)
        if v is None:
            continue
        if isinstance(v, (datetime.date, datetime.datetime)) or _DATE_RE.search(str(v)):
            date = normalize_date(v)
            staff = _s(cell(i + 1))
            break
        s = str(v).strip()
        if _RANK_RE.search(s) or "그외" in s or "인기" in s or s in ("통합탭", "카페탭", "미반영", "누락"):
            rank_text = s
        elif board is None:
            board = s
    return keyword, account, board, date, staff, rank_text


# ---------------------------------------------------------------------------
# 적재
# ---------------------------------------------------------------------------
def find_status_xlsx() -> str:
    hits = glob.glob(os.path.join(ROOT_DIR, "*포스팅 시트_계정정보제거.xlsx"))
    if not hits:
        hits = glob.glob(os.path.join(ROOT_DIR, "*포스팅 시트.xlsx"))
    if not hits:
        raise FileNotFoundError("현황 엑셀(*포스팅 시트*.xlsx)을 찾지 못했습니다.")
    return hits[0]


def ingest(xlsx_path=None, db_path=None):
    xlsx_path = xlsx_path or find_status_xlsx()
    conn = get_connection(db_path) if db_path else get_connection()
    init_db(conn)

    # 멱등 준비 — 유효 글은 정규화 URL로, 링크오류 글은 (시트,행,원본URL)로 식별
    db_urls = {r["normalized_url"] for r in conn.execute(
        "SELECT normalized_url FROM posts WHERE normalized_url IS NOT NULL")}
    db_err = {(r["source_sheet"], r["source_row_no"], r["original_url"]) for r in conn.execute(
        "SELECT source_sheet, source_row_no, original_url FROM posts WHERE normalized_url IS NULL")}
    run_urls, run_err = set(), set()

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    sheets = [ws for ws in wb.worksheets if "현황" in ws.title]
    # 예전꺼 시트는 뒤로 — 중복 시 현행 시트가 우선 적재되도록
    sheets.sort(key=lambda ws: "예전" in ws.title)

    report = {}
    staff_names = set()
    for ws in sheets:
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
        blocks = detect_blocks(grid)
        stats = {"url_cells": 0, "loaded": 0, "dup": 0, "link_error": 0,
                 "already": 0, "fallback": 0, "realigned": 0}
        covered = set()  # 블록이 소화한 (row, col)

        def load_row(sheet, row_no, url_raw, keyword, cafe, account, board,
                     date, staff, rank_text, rank_source, exposure):
            stats["url_cells"] += 1
            normalized, err = normalize_url(url_raw)
            if normalized:
                if normalized in db_urls:    # 재실행: 이미 DB에 있음
                    stats["already"] += 1
                    return
                if normalized in run_urls:   # 이번 실행 내 중복 링크(같은 글)
                    stats["dup"] += 1
                    return
            else:
                ekey = (sheet, row_no, url_raw.strip())
                if ekey in db_err:
                    stats["already"] += 1
                    return
                if ekey in run_err:
                    stats["dup"] += 1
                    return
            if staff:
                staff_names.add(staff)
            cur = conn.execute(
                """INSERT INTO posts (original_url, normalized_url, cafe_name, board_name,
                       keyword, staff_name, account_id, publish_date,
                       source_sheet, source_row_no, extraction_status, extraction_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (url_raw.strip(), normalized, cafe, board, keyword, staff, account,
                 date, sheet, row_no,
                 "링크오류" if err else None,
                 "글ID 없는 링크(카페 홈 등)" if err else None))
            post_id = cur.lastrowid
            if normalized:
                run_urls.add(normalized)
                stats["loaded"] += 1
            else:
                run_err.add((sheet, row_no, url_raw.strip()))
                stats["link_error"] += 1
            if rank_text or exposure:
                rank_value, bucket = parse_rank(rank_text)
                conn.execute(
                    """INSERT INTO reference_signals
                       (post_id, rank_source, rank_value, rank_bucket,
                        exposure_status, view_bucket, collected_from_sheet)
                       VALUES (?, ?, ?, ?, ?, 'Unknown', ?)""",
                    (post_id, rank_source or "순위(헤더미상)", rank_value, bucket,
                     _s(exposure), sheet))

        # 1단계: 헤더 블록
        for b in blocks:
            for r in range(b.header_row + 1, b.end_row + 1):
                row = grid[r]
                if b.link_col >= len(row):
                    continue
                url_raw = row[b.link_col]
                # 블록이 소화하는 건 자기 링크 컬럼뿐 — 범위 안이라도 다른 URL 셀은
                # 폴백이 처리하게 남겨둔다(헤더 없는 옆 표 데이터 손실 방지)
                covered.add((r, b.link_col))
                if url_raw is None or "cafe.naver.com" not in str(url_raw):
                    continue

                def cell(col):
                    return row[col] if col is not None and 0 <= col < len(row) else None

                cafe = _s(cell(b.cafe_col)) or sheet_cafe_name(ws.title)
                board = _s(cell(b.board_col))
                date = normalize_date(cell(b.date_col))
                staff = _s(cell(b.staff_col))
                rank_text, rank_source = _s(cell(b.rank_col)), b.rank_source
                exposure = cell(b.exposure_col)
                # 행 보정: 날짜 자리에 이름·순위가 있으면 데이터가 헤더와 어긋난 행
                # (의편사·닥공사 등 수기 밀림) — 내용 기반으로 다시 읽는다
                if date is not None and not _ISO_DATE.match(date):
                    _, _, board2, date2, staff2, rank2 = parse_fallback_row(row, b.link_col)
                    if date2 and _ISO_DATE.match(date2):
                        stats["realigned"] += 1
                        board, date, staff = board2, date2, staff2
                        rank_text, rank_source, exposure = rank2, "순위(행보정)", None
                load_row(ws.title, r + 1, str(url_raw), _s(cell(b.kw_col)), cafe,
                         _s(cell(b.account_col)), board, date, staff,
                         rank_text, rank_source, exposure)

        # 2단계: 폴백 — 어떤 블록도 소화 못 한 URL 셀
        for r, row in enumerate(grid):
            for c, v in enumerate(row):
                if v is None or "cafe.naver.com" not in str(v) or (r, c) in covered:
                    continue
                keyword, account, board, date, staff, rank_text = parse_fallback_row(row, c)
                stats["fallback"] += 1
                load_row(ws.title, r + 1, str(v), keyword, sheet_cafe_name(ws.title),
                         account, board, date, staff, rank_text, None, None)

        report[ws.title] = stats
    wb.close()

    # 담당자 → staff 테이블 (문체 집계·마스킹 이름목록의 기준, v9 설계노트 1)
    for name in sorted(staff_names):
        conn.execute("INSERT OR IGNORE INTO staff (staff_name) VALUES (?)", (name,))
    conn.commit()

    totals = {k: sum(s[k] for s in report.values())
              for k in ("url_cells", "loaded", "dup", "link_error",
                        "already", "fallback", "realigned")}
    n_posts = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
    n_sig = conn.execute("SELECT COUNT(*) c FROM reference_signals").fetchone()["c"]
    n_staff = conn.execute("SELECT COUNT(*) c FROM staff").fetchone()["c"]
    conn.close()
    return {"sheets": report, "totals": totals,
            "db": {"posts": n_posts, "reference_signals": n_sig, "staff": n_staff}}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    xlsx = sys.argv[1] if len(sys.argv) > 1 else find_status_xlsx()
    print(f"엑셀: {os.path.basename(xlsx)}")
    result = ingest(xlsx)
    print(f"{'시트':<22}{'URL셀':>7}{'적재':>7}{'중복':>7}{'링크오류':>9}"
          f"{'기존':>7}{'폴백':>7}{'행보정':>7}")
    for name, s in result["sheets"].items():
        print(f"{name:<22}{s['url_cells']:>7}{s['loaded']:>7}{s['dup']:>7}"
              f"{s['link_error']:>9}{s['already']:>7}{s['fallback']:>7}{s['realigned']:>7}")
    t = result["totals"]
    print("-" * 69)
    print(f"{'합계':<22}{t['url_cells']:>7}{t['loaded']:>7}{t['dup']:>7}"
          f"{t['link_error']:>9}{t['already']:>7}{t['fallback']:>7}{t['realigned']:>7}")
    d = result["db"]
    print(f"\nDB 현재: posts={d['posts']} / reference_signals={d['reference_signals']}"
          f" / staff={d['staff']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
