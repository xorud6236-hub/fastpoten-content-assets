# -*- coding: utf-8 -*-
"""viewer.py — 로컬 품질 확인 뷰어 (읽기 전용, 2차)

추출된 글을 관리자 1인이 브라우저로 눈검수하는 도구. 파이썬 표준 http.server만 사용
(새 웹 프레임워크 없음). 화면 2개: 목록(/) · 글 1건 상세(/post?id=N).

★ 불변 1(마스킹) — 이 파일이 반드시 지키는 것:
  - 왼쪽 '가공 전 원문' 패널: body_raw 파일을 서버가 읽어 masking.mask_text로 개인정보만
    가린 텍스트만 화면에 낸다(masked_raw_flow). 원본 문자열(전화번호·이름 등)은 어떤 경로로도
    브라우저에 나가지 않는다. 원본 줄바꿈/흐름은 유지하되 HTML 이스케이프 후 가림 자리만 하이라이트.
  - 오른쪽 '정리 결과' 패널: 오직 마스킹본만 쓴다 — post_paragraphs.clean_text (intake가 가려 저장한 문단).
  - raw_text / body_raw / body_clean 의 '원본 문자열'은 마스킹 통과분 외에는 화면에 절대 출력하지 않는다.
  - "가림 종류·건수"는 서버 내부에서만 계산한다: body_clean(개인정보 포함)을 서버가 읽어
    masking.py로 다시 가려 hit의 '종류'만 세고, 원본 문자열은 화면으로 내보내지 않는다.
  - 이미지: 이 뷰어는 localhost 읽기전용·관리자 1인 검수용이라 추출 이미지를 실제로 보여준다
    (불변 1은 '발행·재사용 금지'지 '검수 보기 금지'가 아님 — 사람이 개인정보 유무·재사용 가부를
    판단하려면 봐야 함). 단 reuse_scope 배지(재사용 가능/권리 확인 필요/원본 재사용 금지)는 그대로
    표시해 재사용 전 검토가 필요함을 남긴다. 파일 서빙은 corpus 하위 경로만(traversal 차단).
    이미지 '텍스트' 마스킹과는 무관 — 이미지 노출은 검수 화면 한정, 원문 텍스트 누출은 여전히 0.

사용:
  python src/viewer.py                # 기본 포트 8765로 켜기 → http://localhost:8765/
  python src/viewer.py 9000           # 포트 지정
"""
import datetime
import html as html_mod
import mimetypes
import os
import re
import sqlite3
import statistics
import sys
import urllib.parse
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import DEFAULT_DB_PATH, ROOT_DIR, get_connection  # noqa: E402
import masking  # noqa: E402
import trends  # noqa: E402  (시기별 주제 트렌드·주제별 조회수 — 정규화는 keyword_normalize 단일 출처)

TOKENS_PATH = os.path.join(ROOT_DIR, "templates", "tokens.css")
CORPUS_DIR = os.path.join(ROOT_DIR, "corpus")   # 이미지 서빙 허용 루트(밖은 차단)
AUTO_VIEW_MARK = "자동추출:조회수"  # extract_cafe가 조회수 행에 남기는 표식(참고 신호)


def safe_image_path(local_path):
    """DB의 local_path → corpus 하위의 실제 파일 절대경로. 벗어나거나 없으면 None.

    ★ 경로안전(traversal 차단): local_path는 DB에서만 오고, 절대/`..` 경로로 corpus 밖을
    가리키면 거부한다. realpath로 정규화 후 corpus 하위인지 commonpath로 검증(다른 드라이브면 ValueError→None).
    로컬 검수 화면이라 reuse_scope·contains_person으로는 막지 않지만, 파일 위치는 반드시 corpus 안이어야 한다.
    """
    if not local_path:
        return None
    try:
        fp = os.path.realpath(os.path.join(ROOT_DIR, local_path))
        corpus = os.path.realpath(CORPUS_DIR)
        if os.path.commonpath([fp, corpus]) != corpus:
            return None
    except ValueError:      # 다른 드라이브 등 공통경로 없음 → 밖으로 간주
        return None
    return fp if os.path.isfile(fp) else None

# 화면 문구(설계안 그대로 — 코드 용어·등급 용어 금지)
FAIL_MESSAGES = {
    "실패-삭제된글": "이 글은 삭제된 글이라 가져오지 못했습니다.",
    "실패-로그인필요": "로그인이 필요한 글이라 가져오지 못했습니다.",
    "실패-비공개게시판": "비공개 게시판이라 접근할 수 없었습니다.",
    "실패-접근불가(기타)": "접근할 수 없었습니다(기타).",
    "링크오류": "접근할 수 없었습니다(기타).",
}
LENGTH_LABEL = {"short": "짧음", "medium": "보통", "long": "긺"}
# reuse_scope → (라벨, 의미색 클래스) — 색만이 아니라 글자로도 구분(접근성)
REUSE_LABEL = {
    "image_reuse_allowed": ("재사용 가능", "ok"),
    "image_rights_review": ("권리 확인 필요", "warn"),
    "image_pattern_only": ("원본 재사용 금지", "danger"),
}

esc = html_mod.escape


# ---------------------------------------------------------------------------
# 서버 내부 계산 (원본은 화면으로 내보내지 않음)
# ---------------------------------------------------------------------------
def mask_type_counts(conn, body_clean_path):
    """body_clean(개인정보 포함)을 서버가 읽어 다시 가려 '종류별 건수'만 센다.
    반환: Counter{종류: 건수}. 원본 문자열(전화번호·이름)은 절대 반환/출력하지 않음."""
    counts = Counter()
    if not body_clean_path:
        return counts
    fp = os.path.join(ROOT_DIR, body_clean_path)
    if not os.path.exists(fp):
        return counts
    with open(fp, encoding="utf-8") as f:
        text = f.read()
    pats = masking.load_regex_patterns(conn)
    names = masking.load_staff_names(conn)
    _, hits = masking.mask_text(text, pats, names)  # hits[i]['original']은 사용하지 않음
    for h in hits:
        counts[h["type"]] += 1
    return counts


def highlight_masked(clean_text):
    """마스킹본(clean_text)에서 가려진 자리([담당자]·[가림] 등 대괄호 토큰)를 노랑 하이라이트.
    clean_text에는 개인정보가 없다(이미 가려짐). 먼저 이스케이프 후 대괄호 토큰만 감싼다."""
    escaped = esc(clean_text or "")
    return re.sub(r"(\[[^\[\]\n]{1,20}\])",
                  r'<mark class="masked">\1</mark>', escaped)


def masked_raw_flow(conn, body_raw_path):
    """왼쪽 '가공 전 원문' 패널용 — 원문 파일(body_raw)을 읽어 개인정보만 가린 '원본 흐름' HTML.
    ★ 불변 1: 원본 문자열은 반환/출력하지 않는다. mask_text 통과분만 이스케이프+하이라이트해 낸다.
    오른쪽과 같은 패턴·직원 이름 목록 사용. 원본 줄바꿈은 highlight_masked(esc)로 보존
    (CSS white-space: pre-wrap). 원문 파일이 없으면 None(호출부에서 안내 문구 표시)."""
    if not body_raw_path:
        return None
    fp = os.path.join(ROOT_DIR, body_raw_path)
    if not os.path.exists(fp):
        return None
    with open(fp, encoding="utf-8") as f:
        text = f.read()
    pats = masking.load_regex_patterns(conn)
    names = masking.load_staff_names(conn)
    masked, _ = masking.mask_text(text, pats, names)  # 원본 text는 여기서 버려짐
    return highlight_masked(masked)


def view_count_of(conn, post_id):
    row = conn.execute(
        "SELECT view_count FROM reference_signals "
        "WHERE post_id=? AND collected_from_sheet=?",
        (post_id, AUTO_VIEW_MARK)).fetchone()
    if row and row["view_count"] is not None:
        return row["view_count"]
    row = conn.execute(
        "SELECT view_count FROM reference_signals "
        "WHERE post_id=? AND view_count IS NOT NULL ORDER BY signal_id LIMIT 1",
        (post_id,)).fetchone()
    return row["view_count"] if row else None


def is_success(status):
    return bool(status) and status.startswith("성공")


# ---------------------------------------------------------------------------
# 분석(참고 신호) 집계 — reference_signals를 한 번의 JOIN으로 가져와 N+1 회피
# ---------------------------------------------------------------------------
def _parse_days(publish_date, today):
    """publish_date(YYYY-MM-DD…) → 오늘까지 경과일. 없거나 파싱불가면 None."""
    try:
        y, m, d = map(int, str(publish_date)[:10].split("-"))
        return (today - datetime.date(y, m, d)).days
    except Exception:
        return None


def analysis_records(conn, today):
    """분석 대상(추출완료 + 조회수 있음)을 한 번의 쿼리로. 행마다 재조회 없음(N+1 회피).
    조회수는 reference_signals의 '자동추출:조회수' 행. 하루당조회 vpd=views/max(경과일,1)."""
    rows = conn.execute(
        "SELECT p.post_id, p.title, p.keyword, p.staff_name, p.publish_date, "
        "rs.view_count AS views, "
        "(SELECT COUNT(*) FROM post_paragraphs pp WHERE pp.post_id=p.post_id) np, "
        "(SELECT COUNT(*) FROM post_images pi WHERE pi.post_id=p.post_id) ni, "
        "(SELECT COALESCE(SUM(LENGTH(clean_text)),0) FROM post_paragraphs pp "
        "   WHERE pp.post_id=p.post_id) chars "
        "FROM posts p "
        "LEFT JOIN reference_signals rs "
        "  ON rs.post_id=p.post_id AND rs.collected_from_sheet=? "
        "WHERE p.body_raw_path IS NOT NULL AND rs.view_count IS NOT NULL",
        (AUTO_VIEW_MARK,)).fetchall()
    recs = []
    for r in rows:
        dg = _parse_days(r["publish_date"], today)
        v = r["views"]
        vpd = (v / max(dg, 1)) if dg is not None else None
        recs.append(dict(pid=r["post_id"], title=r["title"], kw=r["keyword"],
                         staff=r["staff_name"], pd=r["publish_date"], dg=dg,
                         v=v, vpd=vpd, np=r["np"], ni=r["ni"], chars=r["chars"] or 0))
    return recs


def pearson(xs, ys):
    """짝지은 값의 피어슨 상관계수. 표본<3이면 None."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
    return num / den if den else None


def rel_label(r):
    """상관 세기 일상어(설계 규칙). |r|<0.1 거의 없음 … 0.5+ 뚜렷."""
    a = abs(r)
    if a < 0.1:
        return "거의 관계 없음"
    if a < 0.3:
        return "약한 관계"
    if a < 0.5:
        return "어느 정도 관계"
    return "뚜렷한 관계"


# ---------------------------------------------------------------------------
# 공통 HTML 뼈대 + 씨앗 스타일 (색·글꼴은 tokens.css 변수만 참조)
# ---------------------------------------------------------------------------
PAGE_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: var(--font); color: var(--ink); background: #f4f6fa;
       font-size: 16px; line-height: 1.6; }
a { color: var(--brand); text-decoration: none; }
a:hover { text-decoration: underline; }
.topbar { position: sticky; top: 0; z-index: 10;
          background: var(--brand); color: #fff;
          display: flex; align-items: center; gap: 16px;
          padding: 16px 24px; }
.topbar a { color: #fff; }
.topbar .t-title { flex: 1; font-size: 20px; font-weight: 700;
                   white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 24px; }
.meta { color: var(--muted); font-size: 13px; margin: 8px 0 24px; }
.meta b { color: var(--ink); font-weight: 700; }
h1.doc { font-size: 24px; font-weight: 800; line-height: 1.3; }
h2.sec { font-size: 20px; font-weight: 700; color: var(--brand);
         margin: 0 0 16px; }
/* 상태 배지(pill) — 색만이 아니라 글자로도 구분 */
.badge { display: inline-block; border-radius: 999px; padding: 4px 12px;
         font-size: 13px; font-weight: 700; border: 1px solid; }
.badge.ok { color: var(--ok); background: var(--ok-bg); border-color: var(--ok); }
.badge.warn { color: var(--warn); background: var(--warn-bg); border-color: var(--warn); }
.badge.danger { color: var(--danger); background: var(--danger-bg); border-color: var(--danger); }
/* 태그 chip — 문단 역할·이미지 분류 */
.chip { display: inline-block; border-radius: 6px; padding: 2px 8px;
        font-size: 13px; font-weight: 700; color: var(--accent);
        background: #eaf7f5; margin-right: 6px; }
.chip.dim { color: var(--muted); background: #eef1f5; }
.chip.mark { color: var(--note-ink); background: var(--note-bg); }
.needcheck { color: var(--muted); font-size: 13px; margin-left: 4px; }
/* 2단 배치 — 좁아지면 위아래로 쌓임 */
.cols { display: flex; flex-wrap: wrap; gap: 24px; align-items: flex-start; }
.col-main { flex: 1 1 60%; min-width: 320px; }
.col-side { flex: 1 1 320px; min-width: 280px; }
/* 좌우 원문 대조 — 각 ≈50%, 독립 스크롤. 좁아지면 위(원문)/아래(정리결과)로 쌓임 */
.compare-intro { color: var(--muted); font-size: 14px; margin: 0 0 16px; }
.compare { display: flex; flex-wrap: wrap; gap: 24px; align-items: flex-start;
           margin-bottom: 24px; }
.cmp-col { flex: 1 1 45%; min-width: 320px; }
.cmp-col > h2.sec { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.panel-help { color: var(--muted); font-size: 12px; margin: -8px 0 12px; }
/* 왼쪽 — 한 장짜리 문서(원본 흐름 유지, 문단 카드로 안 쪼갬) */
.rawdoc { background: var(--paper); border: 1px solid var(--line); border-radius: 8px;
          padding: 16px; white-space: pre-wrap; word-break: break-word;
          max-height: 70vh; overflow-y: auto; }
/* 오른쪽 — 문단 카드 스트림(독립 스크롤) */
.rightstream { max-height: 70vh; overflow-y: auto; padding-right: 4px; }
/* 대조 아래 — 부가 정보 두 블록을 가로 전체로 나란히 */
.belowcols { display: flex; flex-wrap: wrap; gap: 24px; align-items: flex-start; }
.belowcols > * { flex: 1 1 340px; min-width: 280px; }
/* 좁은 화면: 좌우 2단을 위아래로 쌓고 내부 스크롤 해제(이중 스크롤 방지) */
@media (max-width: 720px) {
  .cmp-col { flex: 1 1 100%; }
  .rawdoc, .rightstream { max-height: none; overflow: visible; }
}
.para { background: var(--paper); border: 1px solid var(--line);
        border-radius: 8px; padding: 16px; margin-bottom: 16px; }
.para .ptext { margin-top: 8px; white-space: pre-wrap; word-break: break-word; }
mark.masked { background: var(--note-bg); color: var(--note-ink);
              border-radius: 4px; padding: 0 3px; font-weight: 700; }
.panel { background: var(--paper); border: 1px solid var(--line);
         border-radius: 8px; padding: 16px; margin-bottom: 24px; }
.panel h2.sec { font-size: 16px; margin-bottom: 12px; }
.masklist { list-style: none; }
.masklist li { padding: 6px 0; border-bottom: 1px solid var(--line);
               display: flex; justify-content: space-between; }
.masklist li:last-child { border-bottom: 0; }
.note-empty { color: var(--muted); font-size: 13px; }
.imgcard { border: 1px solid var(--line); border-radius: 8px;
           padding: 12px; margin-bottom: 12px; }
.imgcard .cls { font-size: 13px; color: var(--muted); margin-bottom: 8px; }
.imgcard .badges > * { margin: 0 6px 6px 0; }
.thumb { max-width: 100%; border-radius: 6px; display: block; margin-top: 8px; }
.imgnote { color: var(--muted); font-size: 12px; margin-top: 6px; }
.placeholder { background: #eef1f5; color: var(--muted); border-radius: 6px;
               padding: 24px 12px; text-align: center; font-size: 13px; margin-top: 8px; }
/* 목록 표 — 줄 전체가 클릭 영역(진짜 링크). 9컬럼(제목·카페·담당자·상태·가림·문단·이미지·조회수·작성일) */
.listhead, .listrow { display: grid;
    grid-template-columns: 2.6fr 1fr 1fr 1.2fr 0.7fr 0.6fr 0.7fr 0.9fr 1.1fr; gap: 12px;
    padding: 12px 16px; align-items: center; }
.listhead { color: var(--muted); font-size: 13px; font-weight: 700;
            border-bottom: 2px solid var(--line); }
.listrow { background: var(--paper); border-bottom: 1px solid var(--line);
           color: var(--ink); }
.listrow:hover { background: #eef4fb; text-decoration: none; }
.listrow .r-title { font-weight: 700; color: var(--brand);
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.num-dim { color: var(--muted); }
.num { text-align: right; }
.filters { margin: 16px 0; font-size: 14px; }
.filters a.on { font-weight: 800; text-decoration: underline; }
.filters .note { color: var(--muted); font-size: 13px; margin-left: 8px; }
.state { background: var(--paper); border: 1px solid var(--line);
         border-radius: 8px; padding: 32px; text-align: center; color: var(--muted); }
/* 상단 띠 화면 이동 메뉴 — 현재 화면은 굵게·밑줄(색만이 아니라 글자로도 구분) */
.navmenu { font-size: 15px; font-weight: 700; white-space: nowrap; }
.navmenu a.here { font-weight: 800; text-decoration: underline; }
/* 분석 화면 — 안내·섹션 부제·정직 박스·표 가로 스크롤 */
.intro { font-size: 15px; margin: 6px 0 2px; }
.intro.sub { color: var(--muted); font-size: 13px; margin: 0 0 16px; }
.secsub { color: var(--muted); font-size: 13px; font-weight: 400; margin-left: 8px; }
.tablewrap { overflow-x: auto; margin-bottom: 8px; }
.honest { background: var(--note-bg); color: var(--note-ink);
          border: 1px solid var(--warn); border-radius: 8px;
          padding: 16px; font-weight: 700; margin-top: 16px; line-height: 1.6; }
.rel-line { padding: 4px 0; }
/* 분석 전용 표 그리드(그리드 비율만 변형 — .listrow 색·행높이·hover는 그대로) */
.an1 .listhead, .an1 .listrow { grid-template-columns: 2.6fr 1.3fr 1fr 0.9fr 1fr 0.9fr 1fr;
            min-width: 760px; }
.an2 .listhead, .an2 .listrow { grid-template-columns: 2fr 0.8fr 1.2fr 1.2fr 1.4fr;
            min-width: 620px; }
.an3 .listhead, .an3 .listrow { grid-template-columns: 2fr 1fr 1.3fr; min-width: 420px; }
.an4 .listhead, .an4 .listrow { grid-template-columns: 2fr 1fr 1fr 1fr; min-width: 480px; }
/* 주제별 조회수(정규화) — 주제·발행·추출·평균조회·합계·하루당 */
.an5 .listhead, .an5 .listrow { grid-template-columns: 2fr 0.8fr 0.8fr 1fr 1fr 1fr;
            min-width: 680px; }
/* 트렌드 표 — 분기(주제·총·시작%·최근%·추세) / 계절(주제·총·최다월·쏠림%) / 월내(주제·초·중·말·총) */
.an6 .listhead, .an6 .listrow { grid-template-columns: 2fr 0.8fr 1fr 1fr 1.1fr; min-width: 640px; }
.an7 .listhead, .an7 .listrow { grid-template-columns: 2fr 0.9fr 1fr 1fr; min-width: 520px; }
.an8 .listhead, .an8 .listrow { grid-template-columns: 2fr 0.8fr 0.8fr 0.8fr 0.8fr; min-width: 560px; }
/* 주제 검수 — near중복 후보(주제A·글수·주제B·글수·사유) / 주제 목록(주제·글수) */
.an9 .listhead, .an9 .listrow { grid-template-columns: 1.6fr 0.5fr 1.6fr 0.5fr 1.4fr;
            min-width: 700px; }
.an10 .listhead, .an10 .listrow { grid-template-columns: 3fr 1fr; min-width: 360px; }
/* 주제 검수 — near중복 후보 판단 카드(원본 키워드 함께 보기) */
.dupcard { background: var(--paper); border: 1px solid var(--line); border-radius: 8px;
           padding: 14px 16px; margin-bottom: 12px; }
.dupcard .why { font-size: 13px; color: var(--note-ink); background: var(--note-bg);
                display: inline-block; border-radius: 6px; padding: 2px 8px; margin-bottom: 10px; }
.dup-cols { display: flex; flex-wrap: wrap; gap: 16px; }
.dup-side { flex: 1 1 300px; min-width: 260px; }
.dup-side .th { font-weight: 800; color: var(--brand); }
.dup-side .kw { color: var(--muted); font-size: 13px; margin-top: 4px; word-break: break-word; }
/* 데이터 현황 숫자 카드 — 가로로 늘어놓기 */
.statcards { display: flex; flex-wrap: wrap; gap: 12px; margin: 8px 0 20px; }
.statcard { flex: 1 1 150px; min-width: 130px; background: var(--paper);
            border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; }
.statcard .n { font-size: 24px; font-weight: 800; color: var(--brand); }
.statcard .l { font-size: 13px; color: var(--muted); margin-top: 2px; }
/* 월별 비중 히트맵 — 주제(행)×월(열), 칸 배경 농도로 비중 표현 */
.heatmap { min-width: 560px; }
.hm-row { display: grid; gap: 2px; margin-bottom: 2px; align-items: stretch; }
.hm-head { margin-bottom: 4px; }
.hm-head .hm-mh { font-size: 11px; color: var(--muted); text-align: center; align-self: end;
                  padding-bottom: 3px; }
.hm-t { font-size: 13px; font-weight: 700; color: var(--ink); display: flex;
        justify-content: space-between; align-items: center; padding-right: 8px;
        overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.hm-t .hm-tot { color: var(--muted); font-weight: 400; font-size: 11px; margin-left: 6px; }
.hm-c { min-height: 26px; display: flex; align-items: center; justify-content: center;
        font-size: 11px; color: var(--ink); border-radius: 3px; border: 1px solid var(--line); }
/* 추세 방향 글자색(색만이 아니라 화살표·부호로도 구분) */
.trend-up { color: var(--ok); font-weight: 700; }
.trend-down { color: var(--danger); font-weight: 700; }
/* 누를 수 없는 집계 줄(섹션 2·3·4·트렌드) — hover 강조·커서 없음 */
.listrow.static { cursor: default; }
.listrow.static:hover { background: var(--paper); }
"""


def page(title, topbar_html, body_html):
    return (
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(title)}</title>"
        "<link rel='stylesheet' href='/tokens.css'>"
        f"<style>{PAGE_CSS}</style></head><body>"
        f"{topbar_html}{body_html}</body></html>"
    )


def nav_menu(current):
    """상단 띠 왼쪽 화면 이동 메뉴(글 목록 · 분석 · 주제·시기 트렌드). 현재 화면은 here(굵게·밑줄)."""
    def item(href, label, key):
        cls = " class='here'" if current == key else ""
        return f"<a href='{href}'{cls}>{label}</a>"
    return ("<span class='navmenu'>"
            + item("/", "글 목록", "list") + " · "
            + item("/analysis", "분석", "analysis") + " · "
            + item("/trends", "주제·시기 트렌드", "trends") + " · "
            + item("/topics", "주제 검수", "topics") + " · "
            + item("/data", "데이터", "data") + "</span>")


def _comma(n):
    return f"{n:,}"


# ---------------------------------------------------------------------------
# 화면 A — 상세
# ---------------------------------------------------------------------------
def render_detail(conn, id_raw):
    try:
        post_id = int(id_raw)
    except (TypeError, ValueError):
        return render_not_found()
    post = conn.execute(
        "SELECT post_id, title, cafe_name, board_name, staff_name, publish_date, "
        "content_length_type, extraction_status, body_clean_path, body_raw_path "
        "FROM posts WHERE post_id=?", (post_id,)).fetchone()
    if post is None:
        return render_not_found()

    title = post["title"] or "(제목 없음)"
    status = post["extraction_status"] or ""
    ok = is_success(status)
    badge_cls = "ok" if ok else "danger"
    topbar = (
        "<div class='topbar'>"
        "<a href='/'>← 목록으로</a>"
        f"<span class='t-title'>{esc(title)}</span>"
        f"<span class='badge {badge_cls}'>{esc(status or '상태 미상')}</span>"
        "</div>"
    )

    # 실패한 글: 본문 대신 안내(이 화면엔 버튼 없음 — 읽기 전용)
    if not ok:
        msg = FAIL_MESSAGES.get(status, "접근할 수 없었습니다(기타).")
        body = (
            "<div class='wrap'>"
            f"<h1 class='doc'>{esc(title)}</h1>"
            "<div class='state' style='margin-top:24px'>"
            f"<p style='font-size:16px;color:var(--ink)'>{esc(msg)}</p>"
            "<p style='margin-top:12px'>이 글은 다시 추출을 돌린 뒤 확인할 수 있어요.</p>"
            "</div></div>"
        )
        return page(title, topbar, body)

    # 메타 줄
    vc = view_count_of(conn, post_id)
    length = LENGTH_LABEL.get(post["content_length_type"], post["content_length_type"] or "-")
    meta_bits = [
        esc(post["cafe_name"] or "-"),
        esc(post["board_name"] or "-"),
        f"담당 {esc(post['staff_name'] or '-')}",
        f"작성일 {esc(post['publish_date'] or '-')}",
        (f"조회수 {vc} (참고 신호)" if vc is not None else "조회수 - (참고 신호)"),
        f"길이 {esc(length)}",
    ]
    meta = " · ".join(meta_bits)

    paras = conn.execute(
        "SELECT paragraph_no, clean_text, role, contains_fact, contains_cta "
        "FROM post_paragraphs WHERE post_id=? ORDER BY paragraph_no",
        (post_id,)).fetchall()

    # 왼쪽 — 가공 전 원문(개인정보만 가린 원본 흐름). ★ mask_text 통과분만.
    left_flow = masked_raw_flow(conn, post["body_raw_path"])
    if left_flow is not None:
        left = f"<div class='rawdoc'>{left_flow}</div>"
    else:
        left = ("<div class='state'>가공 전 원문 파일이 없어 원본 흐름을 보여줄 수 없습니다. "
                "오른쪽 정리 결과만 확인하세요.</div>")

    # 오른쪽 — 우리가 정리한 결과(문단·역할). 문단 번호만 표기(색 마커 없음 — 위치 대응 미저장).
    if paras:
        right_items = []
        for p in paras:
            role = p["role"] or ""
            need = (not role) or role == "배경설명"
            chip_cls = "chip dim" if need else "chip"
            no = p["paragraph_no"]
            chips = [f"<span class='chip dim'>{esc(str(no)) if no is not None else '?'}</span>",
                     f"<span class='{chip_cls}'>{esc(role or '미분류')}</span>"]
            if p["contains_fact"]:
                chips.append("<span class='chip mark'>📌 팩트</span>")
            if p["contains_cta"]:
                chips.append("<span class='chip mark'>📣 안내</span>")
            need_html = "<span class='needcheck'>자동 분류 실패 — 확인 필요</span>" if need else ""
            right_items.append(
                "<div class='para'>"
                f"<div>{''.join(chips)}{need_html}</div>"
                f"<div class='ptext'>{highlight_masked(p['clean_text'])}</div>"
                "</div>")
        right = "".join(right_items)
    else:
        right = ("<div class='state'>아직 문단으로 정리되지 않았습니다. "
                 "(추출은 됐지만 정리 결과가 비어 있어요.)</div>")

    # 대조 아래 — 개인정보 가림 결과(종류+건수만)
    counts = mask_type_counts(conn, post["body_clean_path"])
    total = sum(counts.values())
    if total:
        items = "".join(
            f"<li><span>{esc(t)}</span><span><b>{n}</b>건</span></li>"
            for t, n in counts.most_common())
        mask_panel = (
            "<div class='panel'><h2 class='sec'>개인정보 가림 결과 "
            f"<span class='badge ok'>총 {total}건</span></h2>"
            f"<ul class='masklist'>{items}</ul></div>")
    else:
        mask_panel = (
            "<div class='panel'><h2 class='sec'>개인정보 가림 결과</h2>"
            "<p class='note-empty'>가릴 개인정보가 발견되지 않았습니다. "
            "(원래 개인정보가 없던 글이거나, 못 잡았을 수 있으니 본문을 함께 확인하세요.)</p>"
            "</div>")

    # 오른쪽 아래 — 이미지 분류
    images = conn.execute(
        "SELECT image_id, image_order, image_type, image_role, image_source_type, "
        "reuse_scope, contains_person, local_path "
        "FROM post_images WHERE post_id=? ORDER BY image_order", (post_id,)).fetchall()
    if images:
        cards = []
        for im in images:
            label, cls = REUSE_LABEL.get(im["reuse_scope"], (im["reuse_scope"] or "분류 미상", "warn"))
            person = im["contains_person"]
            cls_line = " / ".join(x for x in [im["image_type"], im["image_role"],
                                              im["image_source_type"]] if x) or "분류 정보 없음"
            badges = [f"<span class='badge {cls}'>{esc(label)}</span>"]
            if person:
                badges.append("<span class='badge danger'>인물 포함</span>")
            # 로컬 검수 화면: 추출 이미지는 항상 실제로 표시(파일이 corpus 안에 있으면).
            #   재사용 가부는 위 배지로 명시 — '보이되 재사용 전 검토 필요'를 캡션으로 남긴다.
            if safe_image_path(im["local_path"]):
                media = ("<img class='thumb' src='/img?id="
                         f"{im['image_id']}' alt='추출 이미지 (재사용 전 검토 필요)'>"
                         "<div class='imgnote'>재사용 전 검토 필요 — 검수용 보기입니다.</div>")
            else:
                media = "<div class='placeholder'>이미지 파일을 찾을 수 없습니다.</div>"
            cards.append(
                "<div class='imgcard'>"
                f"<div class='cls'>{im['image_order'] or '?'}. {esc(cls_line)}</div>"
                f"<div class='badges'>{''.join(badges)}</div>"
                f"{media}</div>")
        img_panel = (f"<div class='panel'><h2 class='sec'>이미지 분류 ({len(images)}개)</h2>"
                     f"{''.join(cards)}</div>")
    else:
        img_panel = ("<div class='panel'><h2 class='sec'>이미지 분류</h2>"
                     "<p class='note-empty'>이 글에는 이미지가 없습니다.</p></div>")

    left_help = ("원본 파일을 그대로 띄운 게 아니라, "
                 "전화번호·이름 등 개인정보만 가린 사본입니다.")
    body = (
        "<div class='wrap'>"
        f"<h1 class='doc'>{esc(title)}</h1>"
        f"<div class='meta'>{meta}</div>"
        "<p class='compare-intro'>왼쪽은 가공 전 원문 그대로의 흐름(개인정보만 가림), "
        "오른쪽은 우리가 문단·역할로 정리한 결과입니다.</p>"
        "<div class='compare'>"
        "<div class='cmp-col'>"
        "<h2 class='sec'>가공 전 원문 (개인정보만 가림) "
        f"<span class='badge warn' title='{esc(left_help)}'>개인정보만 가린 원본 흐름</span>"
        "</h2>"
        f"<p class='panel-help'>{esc(left_help)}</p>"
        f"{left}</div>"
        "<div class='cmp-col'>"
        "<h2 class='sec'>우리가 정리한 결과 (문단·역할)</h2>"
        f"<div class='rightstream'>{right}</div></div>"
        "</div>"
        f"<div class='belowcols'>{mask_panel}{img_panel}</div>"
        "</div>"
    )
    return page(title, topbar, body)


def render_not_found():
    topbar = ("<div class='topbar'><a href='/'>← 목록으로</a>"
              "<span class='t-title'>글을 찾을 수 없음</span></div>")
    body = ("<div class='wrap'><div class='state'>그런 글이 없습니다."
            "<div style='margin-top:12px'><a href='/'>← 목록으로</a></div></div></div>")
    return page("글을 찾을 수 없음", topbar, body)


# ---------------------------------------------------------------------------
# 화면 B — 목록
# ---------------------------------------------------------------------------
def render_list(conn, view="all", sort="recent"):
    # 입력검증: 쿼리값을 링크 href에 되비추므로 안전 리터럴로만 좁힌다(주입 차단)
    view = view if view in ("ok", "fail") else "all"
    sort = "views" if sort == "views" else "recent"
    try:
        # ★ 조회수(참고 신호)를 한 번의 JOIN으로(행마다 재조회 없음 — N+1 회피).
        rows = conn.execute(
            "SELECT p.post_id, p.title, p.cafe_name, p.staff_name, p.extraction_status, "
            "p.publish_date, p.body_clean_path, rs.view_count AS views, "
            "(SELECT COUNT(*) FROM post_paragraphs pp WHERE pp.post_id=p.post_id) para_n, "
            "(SELECT COUNT(*) FROM post_images pi WHERE pi.post_id=p.post_id) img_n "
            "FROM posts p "
            "LEFT JOIN reference_signals rs "
            "  ON rs.post_id=p.post_id AND rs.collected_from_sheet=? "
            "WHERE p.body_raw_path IS NOT NULL "
            "ORDER BY p.updated_at DESC, p.post_id DESC",
            (AUTO_VIEW_MARK,)).fetchall()
    except sqlite3.Error:
        # 자산창고 파일/테이블을 못 열 때(코드 용어 노출 금지)
        topbar = "<div class='topbar'><span class='t-title'>추출 글 품질 확인</span></div>"
        body = ("<div class='wrap'><div class='state'>글 목록을 불러오지 못했습니다. "
                "자산창고 파일을 찾을 수 없어요. 자산창고를 먼저 만든 뒤 다시 열어주세요."
                "</div></div>")
        return page("추출 글 품질 확인", topbar, body)

    rows = list(rows)
    if sort == "views":  # 조회수 높은 순(없는 글은 뒤로)
        rows.sort(key=lambda r: (r["views"] is not None, r["views"] or 0), reverse=True)

    n_total = len(rows)
    n_ok = sum(1 for r in rows if is_success(r["extraction_status"]))
    n_fail = n_total - n_ok
    topbar = ("<div class='topbar'>" + nav_menu("list")
              + "<span class='t-title'>추출 글 품질 확인</span>"
              f"<span class='badge ok'>총 {n_total}건 · 성공 {n_ok} · 실패 {n_fail}</span></div>")

    if n_total == 0:
        body = ("<div class='wrap'><div class='state'>아직 확인할 글이 없습니다. "
                "추출을 먼저 돌린 뒤 이 화면을 새로고침하세요.</div></div>")
        return page("추출 글 품질 확인", topbar, body)

    def match(r):
        if view == "ok":
            return is_success(r["extraction_status"])
        if view == "fail":
            return not is_success(r["extraction_status"])
        return True

    head = ("<div class='listhead'>"
            "<div>제목</div><div>카페</div><div>담당자</div><div>상태</div>"
            "<div>가림</div><div>문단</div><div>이미지</div>"
            "<div class='num'>조회수</div><div>작성일</div></div>")
    body_rows = []
    for r in rows:
        if not match(r):
            continue
        ok = is_success(r["extraction_status"])
        # simplified: 목록마다 body_clean을 다시 읽어 가림 건수를 센다(소량 파일럿엔 충분).
        #   글이 대량이 되면 건수를 저장해 두는 방식으로 바꿀 것.
        mask_n = sum(mask_type_counts(conn, r["body_clean_path"]).values())
        mask_cls = "" if mask_n else " num-dim"
        v = r["views"]
        view_cell = (f"<div class='num'>{_comma(v)}</div>" if v is not None
                     else "<div class='num num-dim'>-</div>")
        body_rows.append(
            f"<a class='listrow' href='/post?id={r['post_id']}'>"
            f"<div class='r-title'>{esc(r['title'] or '(제목 없음)')}</div>"
            f"<div>{esc(r['cafe_name'] or '-')}</div>"
            f"<div>{esc(r['staff_name'] or '-')}</div>"
            f"<div><span class='badge {'ok' if ok else 'danger'}'>"
            f"{esc(r['extraction_status'] or '상태 미상')}</span></div>"
            f"<div class='{mask_cls.strip()}'>{mask_n}건</div>"
            f"<div>{r['para_n']}</div><div>{r['img_n']}</div>"
            f"{view_cell}"
            f"<div>{esc(r['publish_date'] or '-')}</div></a>")

    def on(cond):
        return " class='on'" if cond else ""
    vq = f"&view={view}" if view != "all" else ""
    filters = ("<div class='filters'>보기: "
               f"<a href='/'{on(view=='all' and sort=='recent')}>전체</a> · "
               f"<a href='/?view=ok'{on(view=='ok')}>성공만</a> · "
               f"<a href='/?view=fail'{on(view=='fail')}>실패만</a>"
               "&nbsp;&nbsp;|&nbsp;&nbsp;정렬: "
               f"<a href='/?sort=recent{vq}'{on(sort=='recent')}>최신순</a> · "
               f"<a href='/?sort=views{vq}'{on(sort=='views')}>조회수 높은 순</a>"
               "<span class='note'>조회수는 참고 신호입니다.</span></div>")
    body = (f"<div class='wrap'>{filters}{head}{''.join(body_rows)}</div>")
    return page("추출 글 품질 확인", topbar, body)


# ---------------------------------------------------------------------------
# 화면 C — 분석 (참고 신호 대시보드, 읽기 전용)
# ---------------------------------------------------------------------------
SECTION1_TOP = 50   # 섹션1 표에 보일 상위 건수(결정 5)
SECTION2_TOP = 20   # 섹션2 키워드 상위 N


def render_analysis(conn, sort="views", min_age=False):
    # 입력검증: sort를 링크 href에 되비추므로 안전 리터럴로만 좁힌다(주입 차단)
    sort = "vpd" if sort == "vpd" else "views"
    topbar_menu = nav_menu("analysis")
    try:
        recs = analysis_records(conn, datetime.date.today())
    except sqlite3.Error:
        topbar = ("<div class='topbar'>" + topbar_menu
                  + "<span class='t-title'>참고 신호 분석</span></div>")
        body = ("<div class='wrap'><div class='state'>분석을 불러오지 못했습니다. "
                "자산창고 파일을 찾을 수 없어요. 자산창고를 먼저 만든 뒤 다시 열어주세요."
                "</div></div>")
        return page("참고 신호 분석", topbar, body)

    n_target = len(recs)
    topbar = ("<div class='topbar'>" + topbar_menu
              + "<span class='t-title'>참고 신호 분석</span>"
              f"<span class='badge ok'>분석 대상 {n_target}건</span></div>")

    if n_target == 0:
        body = ("<div class='wrap'><div class='state'>아직 분석할 글이 없습니다. "
                "글을 추출해서 조회수를 확보한 뒤 이 화면을 새로고침하세요.</div></div>")
        return page("참고 신호 분석", topbar, body)

    # 30일+ 필터: 켜면 경과일<30(또는 작성일 불명) 글 제외
    used = [r for r in recs if r["dg"] is not None and r["dg"] >= 30] if min_age else recs

    # --- 안내 두 줄 ---
    intro = (
        "<p class='intro'>추출된 우리 카페 글의 조회수(참고 신호)로 "
        "\"어떤 주제·누가 많이 읽혔나\"를 봅니다.</p>"
        "<p class='intro sub'>조회수는 성과가 아니라 참고 신호입니다. "
        "카페 순위는 실제 조회수와 거의 맞지 않아 이 화면에서 뺐습니다.</p>")

    # --- 조절 바(주소 링크로만, JS 없음) ---
    def on(cond):
        return " class='on'" if cond else ""
    ageq = "&min_age=30" if min_age else ""
    controls = (
        "<div class='filters'>정렬: "
        f"<a href='/analysis?sort=views{ageq}'{on(sort=='views')}>조회수 순</a> · "
        f"<a href='/analysis?sort=vpd{ageq}'{on(sort=='vpd')}>하루당 조회수 순</a>"
        "&nbsp;&nbsp;|&nbsp;&nbsp;기간: "
        f"<a href='/analysis?sort={sort}'{on(not min_age)}>전체</a> · "
        f"<a href='/analysis?sort={sort}&min_age=30'{on(min_age)}>올린 지 30일 지난 글만</a>"
        "<div class='note'>하루당 조회수는 최근에 올린 글이 높게 나오는 경향이 있어요"
        "(조회가 초반에 몰림). 오래된 글과 견줄 땐 '조회수 순'도 함께 보세요.</div></div>")

    # 30일+ 필터가 전부 걸러낸 경우(빈 집합) — 평균 계산 크래시 방지, 안내만
    if not used:
        body = (f"<div class='wrap'>{intro}{controls}"
                "<div class='state'>고른 조건(올린 지 30일 지난 글)에 맞는 글이 없습니다. "
                "‘전체’로 바꾸면 모든 글을 볼 수 있어요.</div></div>")
        return page("참고 신호 분석", topbar, body)

    # --- 섹션 1: 글별 조회수 ---
    if sort == "vpd":
        s1 = sorted(used, key=lambda r: (r["vpd"] is not None, r["vpd"] or 0), reverse=True)
    else:
        s1 = sorted(used, key=lambda r: r["v"], reverse=True)
    shown = s1[:SECTION1_TOP]
    s1_head = ("<div class='listhead'><div>제목</div><div>키워드</div><div>담당자</div>"
               "<div class='num'>조회수</div><div class='num'>하루당 조회수</div>"
               "<div>형식</div><div>작성일</div></div>")
    s1_rows = []
    for r in shown:
        vpd_cell = (f"{r['vpd']:.1f}" if r["vpd"] is not None else "-")
        s1_rows.append(
            f"<a class='listrow' href='/post?id={r['pid']}'>"
            f"<div class='r-title'>{esc(r['title'] or '(제목 없음)')}</div>"
            f"<div>{esc(r['kw'] or '-')}</div>"
            f"<div>{esc(r['staff'] or '-')}</div>"
            f"<div class='num'>{_comma(r['v'])}</div>"
            f"<div class='num'>{vpd_cell}</div>"
            f"<div>{r['np']}문단·{r['ni']}장</div>"
            f"<div>{esc(str(r['pd'] or '-')[:10])}</div></a>")
    s1_more = f"<p class='intro sub'>상위 {SECTION1_TOP}건만 보입니다 (전체 {len(used)}건).</p>" \
        if len(used) > SECTION1_TOP else f"<p class='intro sub'>전체 {len(used)}건.</p>"
    sec1 = ("<h2 class='sec'>조회수 높은 글"
            "<span class='secsub'>정렬: 조회수 순 / 하루당 조회수 순 (위 조절 바)</span></h2>"
            f"<div class='an1'><div class='tablewrap'>{s1_head}{''.join(s1_rows)}</div></div>"
            f"{s1_more}")

    # --- 섹션 1.5: 주제별 조회수(정규화) — 변형 키워드를 주제로 묶어 과소집계 해소 ---
    #   위 기간 필터와 별개로, 추출·조회수 있는 글 전체를 주제로 묶는다(keyword_normalize 단일 출처).
    #   '발행'은 posts 전체 같은 주제 글 수 → 적게 썼는데 잘 된/많이 썼는데 안 된 주제가 보인다.
    try:
        tperf = trends.topic_performance(conn, datetime.date.today())
    except sqlite3.Error:
        tperf = []
    if tperf:
        # ★ 표본 편중 경고(정직) — 이 조회수 표본은 대부분 한 출처(공준모)라 주제 간 공정 비교 아님.
        #   로드맵 §2·§4가 필수로 지정한 표기. 편중이 뚜렷할 때(60%+)만 띄운다.
        skew_sheet, skew_pct, skew_tot = trends.topic_sample_skew(conn)
        skew_warn = ""
        if skew_pct >= 60 and skew_sheet:
            skew_warn = (
                "<div class='honest'>⚠ 이 표의 조회수는 표본 "
                f"{_comma(skew_tot)}건 중 <b>{skew_pct:.0f}%가 ‘{esc(skew_sheet)}’ 한 곳</b>에서 "
                "나온 글입니다. 다른 분류는 추출글이 적어 거의 안 잡혀요. <b>서로 다른 주제를 "
                "공정하게 비교하는 표가 아직 아닙니다</b> — ‘더 써볼 후보’는 참고로만 보고, "
                "주제별 표본이 고르게 쌓인 뒤 판단하세요.</div>")
        t_head = ("<div class='listhead'><div>주제</div><div class='num'>발행 글수</div>"
                  "<div class='num'>추출·조회 글수</div><div class='num'>평균 조회수</div>"
                  "<div class='num'>합계 조회수</div><div class='num'>평균 하루당</div></div>")
        t_rows = []
        for it in tperf:
            avpd = f"{it['avg_vpd']:.1f}" if it["avg_vpd"] is not None else "-"
            t_rows.append(
                "<div class='listrow static'>"
                f"<div>{esc(it['topic'])}</div>"
                f"<div class='num'>{_comma(it['published'])}</div>"
                f"<div class='num'>{it['extracted']}</div>"
                f"<div class='num'>{_comma(round(it['avg_views']))}</div>"
                f"<div class='num'>{_comma(it['sum_views'])}</div>"
                f"<div class='num'>{avpd}</div></div>")
        sect = ("<h2 class='sec'>주제별 조회수 <span class='secsub'>변형 키워드를 하나의 주제로 "
                "묶어 봄 · 추출글 2건+ · 평균 조회수 높은 순</span></h2>"
                "<p class='intro sub'>‘발행 글수’는 우리가 그 주제로 쓴 전체 글, ‘추출·조회 글수’는 "
                "그중 조회수를 확보한 글입니다. <b>발행은 적은데 평균 조회수가 높은 주제</b>가 "
                "‘더 써볼 후보’예요. (조회수는 참고 신호 — 자사 채널에서 재검증 필요)</p>"
                f"{skew_warn}"
                f"<div class='an5'><div class='tablewrap'>{t_head}{''.join(t_rows)}</div></div>")
    else:
        sect = ("<h2 class='sec'>주제별 조회수</h2><div class='state'>"
                "주제로 묶어 비교할 글이 아직 부족합니다.</div>")

    # --- 섹션 2: 키워드별 조회수(2건+) ---
    gk = defaultdict(list)
    for r in used:
        gk[r["kw"] or "(없음)"].append(r)
    kstats = []
    for k, g in gk.items():
        if len(g) < 2:
            continue
        vpds = [x["vpd"] for x in g if x["vpd"] is not None]
        kstats.append((k, len(g), statistics.mean(x["v"] for x in g),
                       sum(x["v"] for x in g),
                       statistics.mean(vpds) if vpds else None))
    kstats.sort(key=lambda x: x[2], reverse=True)
    if kstats:
        k_head = ("<div class='listhead'><div>키워드</div><div class='num'>글 수</div>"
                  "<div class='num'>평균 조회수</div><div class='num'>합계 조회수</div>"
                  "<div class='num'>평균 하루당 조회수</div></div>")
        k_rows = []
        for k, n, av, sm, avpd in kstats[:SECTION2_TOP]:
            avpd_cell = f"{avpd:.1f}" if avpd is not None else "-"
            k_rows.append(
                "<div class='listrow static'>"
                f"<div>{esc(k)}</div><div class='num'>{n}</div>"
                f"<div class='num'>{_comma(round(av))}</div>"
                f"<div class='num'>{_comma(sm)}</div>"
                f"<div class='num'>{avpd_cell}</div></div>")
        sec2 = (f"<h2 class='sec'>원본 키워드별 조회수 <span class='secsub'>묶기 전 원본 키워드 "
                f"그대로(참고) · 2건+ · 평균 조회수 높은 순 상위 {SECTION2_TOP}</span></h2>"
                f"<div class='an2'><div class='tablewrap'>{k_head}{''.join(k_rows)}</div></div>")
    else:
        sec2 = ("<h2 class='sec'>키워드별 조회수</h2><div class='state'>"
                "아직 여러 번 쓴 키워드가 없어 키워드별 비교를 만들 수 없습니다.</div>")

    # --- 섹션 3: 담당자별 조회수 ---
    gs = defaultdict(list)
    for r in used:
        gs[r["staff"] or "(없음)"].append(r)
    sstats = sorted(
        ((s, len(g), statistics.mean(x["v"] for x in g)) for s, g in gs.items()),
        key=lambda x: x[2], reverse=True)
    s_head = ("<div class='listhead'><div>담당자</div><div class='num'>글 수</div>"
              "<div class='num'>평균 조회수</div></div>")
    s_rows = "".join(
        "<div class='listrow static'>"
        f"<div>{esc(s)}</div><div class='num'>{n}</div>"
        f"<div class='num'>{_comma(round(av))}</div></div>"
        for s, n, av in sstats)
    sec3 = ("<h2 class='sec'>담당자별 조회수<span class='secsub'>평균 조회수 높은 순</span></h2>"
            f"<div class='an3'><div class='tablewrap'>{s_head}{s_rows}</div></div>")

    # --- 섹션 4: 형식과 조회수의 관계(정직 섹션) ---
    vs = [r["v"] for r in used]
    rel_rows = []
    for label, xs in [("문단 수", [r["np"] for r in used]),
                      ("이미지 수", [r["ni"] for r in used]),
                      ("글자 수", [r["chars"] for r in used])]:
        r_val = pearson(xs, vs)
        if r_val is None:
            rel_rows.append(f"<div class='rel-line'>{label} ↔ 조회수 : (표본 부족)</div>")
        else:
            rel_rows.append(f"<div class='rel-line'>{label} ↔ 조회수 : "
                            f"{rel_label(r_val)} (r={r_val:+.2f})</div>")
    # 상위25% vs 하위25%(조회수 기준)
    by_v = sorted(used, key=lambda r: r["v"], reverse=True)
    q = max(len(by_v) // 4, 1)
    top, bot = by_v[:q], by_v[-q:]

    def _avg(g, k):
        return statistics.mean(r[k] for r in g)
    cmp_head = ("<div class='listhead'><div>구분</div><div class='num'>평균 문단</div>"
                "<div class='num'>평균 이미지</div><div class='num'>평균 글자수</div></div>")
    cmp_rows = "".join(
        f"<div class='listrow static'><div>{lbl}</div>"
        f"<div class='num'>{_avg(g,'np'):.1f}</div>"
        f"<div class='num'>{_avg(g,'ni'):.1f}</div>"
        f"<div class='num'>{_comma(round(_avg(g,'chars')))}</div></div>"
        for lbl, g in [("조회수 상위 25% 글", top), ("조회수 하위 25% 글", bot)])
    honest = ("<div class='honest'>글의 형식(문단 수·이미지 수·글자 수)만으로는 많이 본 글과 "
              "아닌 글을 가를 수 없습니다. 조회수를 가르는 건 형식보다 "
              "\"무엇을 다뤘나(주제·시의성)\"에 더 가깝습니다.</div>")
    sec4 = ("<h2 class='sec'>형식과 조회수, 관계가 있을까?</h2>"
            f"{''.join(rel_rows)}"
            f"<div class='an4'><div class='tablewrap' style='margin-top:12px'>"
            f"{cmp_head}{cmp_rows}</div></div>{honest}")

    body = (f"<div class='wrap'>{intro}{controls}{sec1}"
            f"<div style='margin-top:32px'>{sect}</div>"
            f"<div style='margin-top:32px'>{sec2}</div>"
            f"<div style='margin-top:32px'>{sec3}</div>"
            f"<div style='margin-top:32px'>{sec4}</div></div>")
    return page("참고 신호 분석", topbar, body)


# ---------------------------------------------------------------------------
# 화면 D — 주제·시기 트렌드 (전체 글의 작성일 기준, 읽기 전용)
# ---------------------------------------------------------------------------
def render_trends(conn):
    """시기별로 발행 '비중'이 뜨는/식는 주제 + 월별 계절성 + 월초·중순·말 분포.
    전체 posts의 keyword+publish_date만 사용(추출 불필요). 조회수 아님 — 발행량 기준."""
    topbar_menu = nav_menu("trends")
    try:
        recs = trends.load_topic_dates(conn)
    except sqlite3.Error:
        topbar = ("<div class='topbar'>" + topbar_menu
                  + "<span class='t-title'>주제·시기 트렌드</span></div>")
        body = ("<div class='wrap'><div class='state'>트렌드를 불러오지 못했습니다. "
                "자산창고 파일을 먼저 만든 뒤 다시 열어주세요.</div></div>")
        return page("주제·시기 트렌드", topbar, body)

    topbar = ("<div class='topbar'>" + topbar_menu
              + "<span class='t-title'>주제·시기 트렌드</span>"
              f"<span class='badge ok'>대상 {_comma(len(recs))}건</span></div>")
    if not recs:
        # 빈 원인을 구분해 안내(비개발자 자가진단) — 창고가 통째로 비었나 vs 데이터 형태 문제.
        try:
            nposts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        except sqlite3.Error:
            nposts = 0
        if nposts == 0:
            msg = ("창고에 글이 아직 없습니다. 엑셀 원자료를 먼저 불러오세요 — 프로젝트 폴더에서 "
                   "<b>db.py → load_rulebook.py → ingest_excel.py</b> 순서로 실행한 뒤 이 화면을 "
                   "새로고침하면 됩니다. (창고 파일은 PC마다 다시 만들어야 해요.)")
        else:
            msg = ("글은 있는데 ‘작성일·키워드’가 트렌드에 쓸 형태가 아닙니다. 작성일이 "
                   "‘2026-03-15’ 형식인지, 키워드가 채워졌는지 확인이 필요합니다.")
        body = f"<div class='wrap'><div class='state'>{msg}</div></div>"
        return page("주제·시기 트렌드", topbar, body)

    intro = (
        "<p class='intro'>우리 카페 글을 주제로 묶어, <b>시기별로 발행 비중이 뜨거나 식는 "
        "주제</b>와 계절성·월내 분포를 봅니다.</p>"
        "<p class='intro sub'>‘비중’으로 봅니다 — 최근일수록 전체 발행량 자체가 늘어(2026 집중), "
        "원시 건수는 대부분 증가하기 때문. 비중을 보면 ‘상대적으로’ 뜨는/식는 주제만 남습니다.</p>")

    # --- 월별 비중 히트맵 (분기 '시작 vs 최근' 기울기의 함정 대체) ---
    hm = trends.monthly_share_heatmap(recs)
    if hm["months"] and hm["rows"]:
        mx = hm["max_share"] or 1.0
        gcols = f"grid-template-columns:150px repeat({len(hm['months'])},minmax(30px,1fr));"
        hm_head = (f"<div class='hm-row hm-head' style='{gcols}'><div class='hm-t'>주제</div>"
                   + "".join(f"<div class='hm-mh'>{esc(m[2:4])}.{esc(m[5:7])}</div>"
                             for m in hm["months"]) + "</div>")
        hm_rows = []
        for r in hm["rows"]:
            cells = []
            for sh in r["cells"]:
                alpha = (min(sh / mx, 1.0) * 0.92) if mx else 0.0
                txt = f"{sh:.0f}" if sh >= 3 else ""
                cells.append(f"<div class='hm-c' "
                             f"style='background:rgba(var(--heat),{alpha:.2f})'>{txt}</div>")
            hm_rows.append(
                f"<div class='hm-row' style='{gcols}'>"
                f"<div class='hm-t'>{esc(r['topic'])}"
                f"<span class='hm-tot'>{_comma(r['total'])}</span></div>"
                f"{''.join(cells)}</div>")
        sec_h = ("<h2 class='sec'>월별 비중 히트맵<span class='secsub'>주제(행)×월(열) · 상위 15개 "
                 "· 칸이 진할수록 그 달 전체 글 중 그 주제 비중 높음</span></h2>"
                 "<p class='intro sub'>매 달의 비중을 그대로 보여줍니다 — ‘시작 대비 최근’ 방식은 "
                 "뒤늦게 생긴 주제가 늘 상승처럼 보이는 함정이 있어, 달별 색으로 추세를 직접 보게 했어요. "
                 "왼→오른쪽으로 <b>색이 짙어지면 뜨는 주제, 옅어지면 식는 주제</b>입니다.</p>"
                 f"<div class='tablewrap'><div class='heatmap'>{hm_head}{''.join(hm_rows)}</div></div>")
    else:
        sec_h = ("<h2 class='sec'>월별 비중 히트맵</h2><div class='state'>"
                 "히트맵에 쓸 월별 물량이 아직 부족합니다.</div>")

    # --- 월별 계절성 ---
    seas = trends.seasonality(recs)
    if seas:
        s_head = ("<div class='listhead'><div>주제</div><div class='num'>총 글수</div>"
                  "<div class='num'>가장 많이 쓴 달</div><div class='num'>그 달 비중</div></div>")
        s_rows = "".join(
            "<div class='listrow static'>"
            f"<div>{esc(it['topic'])}</div><div class='num'>{_comma(it['total'])}</div>"
            f"<div class='num'>{it['peak_month']}월</div>"
            f"<div class='num'>{it['peak_pct']:.0f}%</div></div>"
            for it in seas)
        sec_s = ("<h2 class='sec'>월별 계절성<span class='secsub'>특정 달에 쏠린 주제 · "
                 "쏠림 큰 순 상위 10</span></h2>"
                 "<p class='intro sub'>자격증은 시험·접수 일정이 있어 특정 달에 발행이 몰립니다. "
                 "수요 정점보다 앞서 쓰려면 이 달 <b>한두 달 전</b>이 후보예요.</p>"
                 f"<div class='an7'><div class='tablewrap'>{s_head}{s_rows}</div></div>")
    else:
        sec_s = ""

    # --- 월초/중순/말 ---
    im = trends.intramonth(recs)
    base = im["baseline"]

    def dom_table(items):
        head = ("<div class='listhead'><div>주제</div><div class='num'>월초(1-10)</div>"
                "<div class='num'>중순(11-20)</div><div class='num'>월말(21-31)</div>"
                "<div class='num'>총 글수</div></div>")
        rows = "".join(
            "<div class='listrow static'>"
            f"<div>{esc(it['topic'])}</div>"
            f"<div class='num'>{it['early_pct']:.0f}%</div>"
            f"<div class='num'>{it['mid_pct']:.0f}%</div>"
            f"<div class='num'>{it['late_pct']:.0f}%</div>"
            f"<div class='num'>{_comma(it['total'])}</div></div>"
            for it in items)
        return f"<div class='an8'><div class='tablewrap'>{head}{rows}</div></div>"

    sec_d = ("<h2 class='sec'>월초·중순·말 분포<span class='secsub'>주제를 월내 어느 시기에 "
             "발행했나</span></h2>"
             f"<p class='intro sub'>전체 기준선: 월초 {base[0]:.0f}% · 중순 {base[1]:.0f}% · "
             f"월말 {base[2]:.0f}%. 아래는 기준선보다 한쪽으로 치우친 주제입니다.</p>"
             "<p class='intro sub' style='margin-top:8px'>월초에 몰아 쓴 주제</p>"
             f"{dom_table(im['early'])}"
             "<p class='intro sub' style='margin-top:12px'>월말에 몰아 쓴 주제</p>"
             f"{dom_table(im['late'])}")

    honest = ("<div class='honest'>이 화면은 <b>“우리가 언제 얼마나 발행했나”(발행 습관)</b>입니다. "
              "사람들이 언제 더 <b>검색</b>하는지(수요 타이밍)가 아닙니다. 수요 정점은 네이버 "
              "데이터랩 같은 외부 트렌드로 겹쳐 봐야 확정할 수 있고, 진짜 성과는 자사 채널 실측입니다. "
              "표본도 최근(2025~2026)에 치우쳐 있어 ‘발견’이지 ‘확정’이 아닙니다.</div>")

    body = (f"<div class='wrap'>{intro}{sec_h}"
            f"<div style='margin-top:32px'>{sec_s}</div>"
            f"<div style='margin-top:32px'>{sec_d}</div>"
            f"{honest}</div>")
    return page("주제·시기 트렌드", topbar, body)


# ---------------------------------------------------------------------------
# 화면 E — 데이터 (창고 현황 + 룰북 열람, 읽기 전용)
# ---------------------------------------------------------------------------
def render_data(conn):
    """창고에 뭐가 얼마나 들었나(건강검진) + 우리 규칙(룰북) 열람. 원본 셀 재노출 안 함(불변2)."""
    menu = nav_menu("data")
    try:
        one = lambda sql, p=(): conn.execute(sql, p).fetchone()[0]
        n_total = one("SELECT COUNT(*) FROM posts")
        n_kw = one("SELECT COUNT(*) FROM posts WHERE keyword IS NOT NULL")
        n_ext = one("SELECT COUNT(*) FROM posts WHERE body_raw_path IS NOT NULL")
        n_view = one("SELECT COUNT(*) FROM posts p JOIN reference_signals rs "
                     "ON rs.post_id=p.post_id AND rs.collected_from_sheet=? "
                     "WHERE rs.view_count IS NOT NULL", (AUTO_VIEW_MARK,))
        sheets = conn.execute(
            "SELECT COALESCE(source_sheet,'(미상)') s, COUNT(*) n FROM posts "
            "GROUP BY source_sheet ORDER BY n DESC").fetchall()
        n_cat = one("SELECT COUNT(*) FROM rulebook_categories")
        n_ban = one("SELECT COUNT(*) FROM rulebook_banned_words")
        n_pii = one("SELECT COUNT(*) FROM rulebook_pii_patterns")
        cats = conn.execute(
            "SELECT category_name, COALESCE(top_category,'-') top, "
            "COALESCE(total_post_frequency,0) freq FROM rulebook_categories "
            "ORDER BY freq DESC").fetchall()
        bans = conn.execute(
            "SELECT word, COALESCE(replacement,'-') rep FROM rulebook_banned_words "
            "ORDER BY word").fetchall()
        piis = conn.execute(
            "SELECT name, COALESCE(description,'') d FROM rulebook_pii_patterns "
            "ORDER BY name").fetchall()
    except sqlite3.Error:
        topbar = ("<div class='topbar'>" + menu + "<span class='t-title'>데이터</span></div>")
        body = ("<div class='wrap'><div class='state'>데이터를 불러오지 못했습니다. "
                "자산창고 파일을 먼저 만든 뒤 다시 열어주세요.</div></div>")
        return page("데이터", topbar, body)

    topbar = ("<div class='topbar'>" + menu + "<span class='t-title'>데이터</span>"
              f"<span class='badge ok'>글 {_comma(n_total)}건</span></div>")

    # 창고 현황 카드
    def card(n, label):
        return f"<div class='statcard'><div class='n'>{_comma(n)}</div><div class='l'>{esc(label)}</div></div>"
    cards = ("<div class='statcards'>"
             + card(n_total, "전체 글")
             + card(n_kw, "주제(키워드) 있음")
             + card(n_ext, "본문 추출완료")
             + card(n_view, "조회수 확보(참고 신호)")
             + "</div>")

    # 출처별
    sheet_rows = "".join(
        "<div class='listrow static'>"
        f"<div>{esc(s['s'])}</div><div class='num'>{_comma(s['n'])}</div></div>"
        for s in sheets)
    sheet_tbl = ("<div class='an10'><div class='tablewrap'>"
                 "<div class='listhead'><div>출처(엑셀 시트)</div><div class='num'>글 수</div></div>"
                 f"{sheet_rows}</div></div>")

    warehouse = ("<h2 class='sec'>창고 현황<span class='secsub'>지금 창고에 든 것</span></h2>"
                 f"{cards}{sheet_tbl}"
                 "<p class='intro sub'>‘조회수 확보’는 추출로 조회수까지 받은 글입니다. "
                 "조회수는 참고 신호이고, 대부분 공준모에서 나왔습니다.</p>")

    # 룰북 열람
    rb_cards = ("<div class='statcards'>"
                + card(n_cat, "카테고리") + card(n_ban, "금지어")
                + card(n_pii, "개인정보 패턴")
                + "<div class='statcard'><div class='n'>–</div><div class='l'>팩트(미적재)</div></div>"
                + "</div>")

    def bullet_list(items, empty):
        if not items:
            return f"<p class='note-empty'>{esc(empty)}</p>"
        return "<ul class='masklist'>" + "".join(items) + "</ul>"

    cat_items = [f"<li><span>{esc(r['category_name'])}"
                 f"<span class='secsub'>{esc(r['top'])}</span></span>"
                 f"<span>글 {_comma(r['freq'])}</span></li>" for r in cats]
    ban_items = [f"<li><span>{esc(r['word'])}</span><span>→ {esc(r['rep'])}</span></li>"
                 for r in bans]
    pii_items = [f"<li><span>{esc(r['name'])}</span>"
                 f"<span class='secsub'>{esc(r['d'][:40])}</span></li>" for r in piis]

    rulebook = ("<h2 class='sec'>룰북 열람<span class='secsub'>원고·마스킹이 따르는 우리 규칙"
                "</span></h2>"
                f"{rb_cards}"
                "<div class='belowcols'>"
                f"<div class='panel'><h2 class='sec'>카테고리 {n_cat}</h2>"
                f"{bullet_list(cat_items, '카테고리가 없습니다.')}</div>"
                f"<div class='panel'><h2 class='sec'>금지어 {n_ban}</h2>"
                f"{bullet_list(ban_items, '금지어가 없습니다.')}</div>"
                f"<div class='panel'><h2 class='sec'>개인정보 패턴 {n_pii}</h2>"
                f"{bullet_list(pii_items, '패턴이 없습니다.')}</div>"
                "</div>"
                "<p class='intro sub'>‘팩트’(학점·응시자격 등 제도 수치)는 아직 창고에 없습니다 — "
                "원고에 정확한 수치를 넣으려면 이 팩트 시트 적재가 다음 열쇠입니다.</p>")

    body = f"<div class='wrap'>{warehouse}<div style='margin-top:32px'>{rulebook}</div></div>"
    return page("데이터", topbar, body)


# ---------------------------------------------------------------------------
# 화면 F — 주제 묶음 검수 (D2, 읽기 전용: 후보만 보여주고 확정은 사람)
# ---------------------------------------------------------------------------
TOPIC_LIST_TOP = 60


def render_topics(conn):
    """정규화가 자동으로 묶은 주제를 사람이 확인하는 화면(D2). '같은 주제일 수 있는 후보'를
    보여주되 합치지 않는다 — 확정 병합은 사람이 정하면 규칙(ALIAS)에 반영."""
    menu = nav_menu("topics")
    try:
        counts = trends.topic_counts(conn)
    except sqlite3.Error:
        topbar = ("<div class='topbar'>" + menu + "<span class='t-title'>주제 검수</span></div>")
        body = ("<div class='wrap'><div class='state'>주제를 불러오지 못했습니다. "
                "자산창고 파일을 먼저 만든 뒤 다시 열어주세요.</div></div>")
        return page("주제 검수", topbar, body)

    topbar = ("<div class='topbar'>" + menu + "<span class='t-title'>주제 검수</span>"
              f"<span class='badge ok'>주제 {_comma(len(counts))}개</span></div>")
    if not counts:
        body = ("<div class='wrap'><div class='state'>묶을 주제가 아직 없습니다. "
                "창고에 글(키워드)이 있어야 합니다.</div></div>")
        return page("주제 검수", topbar, body)

    intro = (
        "<p class='intro'>변형 키워드를 규칙으로 묶은 <b>주제</b>를 사람이 확인하는 화면입니다.</p>"
        "<p class='intro sub'>아래 ‘같은 주제일 수 있는 후보’를 보고 <b>합쳐야 할 쌍</b>을 알려주시면 "
        "규칙에 한 줄로 반영합니다. 자동으로 합치지 않아요 — 예: ‘심리상담사↔상담심리사’는 "
        "글자만 비슷하지 다른 자격증이라 합치면 안 됩니다.</p>")

    # near-중복 후보 — 각 후보의 '원본 키워드'를 함께 보여줘 사람이 직접 판단(D2)
    import keyword_normalize as kn
    members = defaultdict(list)   # 주제 → [(원본키워드, 글수)]
    for r in conn.execute(
            "SELECT keyword, COUNT(*) n FROM posts WHERE keyword IS NOT NULL "
            "GROUP BY keyword"):
        t = kn.normalize(r["keyword"])
        if t:
            members[t].append((r["keyword"], r["n"]))

    def member_str(topic, top=6):
        ms = sorted(members.get(topic, []), key=lambda x: -x[1])
        shown = ", ".join(f"{esc(k)}({n})" for k, n in ms[:top])
        extra = f" 외 {len(ms) - top}개" if len(ms) > top else ""
        return shown + extra or "(없음)"

    cands = kn.near_duplicate_candidates(list(counts.items()))
    if cands:
        cards = "".join(
            "<div class='dupcard'>"
            f"<div class='why'>{esc(why)}</div>"
            "<div class='dup-cols'>"
            f"<div class='dup-side'><div class='th'>{esc(a)} · 글 {_comma(ac)}</div>"
            f"<div class='kw'>{member_str(a)}</div></div>"
            f"<div class='dup-side'><div class='th'>{esc(b)} · 글 {_comma(bc)}</div>"
            f"<div class='kw'>{member_str(b)}</div></div>"
            "</div></div>"
            for a, b, ac, bc, why in cands)
        sec_c = ("<h2 class='sec'>같은 주제일 수 있는 후보"
                 f"<span class='secsub'>합칠지는 사람이 판단 · {len(cands)}쌍</span></h2>"
                 "<p class='intro sub'>각 후보의 <b>원본 키워드(괄호=글 수)</b>를 보고 "
                 "같은 주제면 알려주세요 — 규칙에 반영합니다. 다른 자격증이면 그대로 둡니다.</p>"
                 f"{cards}")
    else:
        sec_c = ("<h2 class='sec'>같은 주제일 수 있는 후보</h2>"
                 "<div class='state'>눈에 띄는 중복 후보가 없습니다.</div>")

    # 전체 주제 목록(상위)
    ordered = sorted(counts.items(), key=lambda x: -x[1])
    shown = ordered[:TOPIC_LIST_TOP]
    l_head = "<div class='listhead'><div>주제</div><div class='num'>글 수</div></div>"
    l_rows = "".join(
        "<div class='listrow static'>"
        f"<div>{esc(t)}</div><div class='num'>{_comma(n)}</div></div>"
        for t, n in shown)
    more = (f"<p class='intro sub'>글 수 많은 순 상위 {TOPIC_LIST_TOP}개 (전체 {_comma(len(counts))}개).</p>"
            if len(counts) > TOPIC_LIST_TOP else f"<p class='intro sub'>전체 {_comma(len(counts))}개.</p>")
    sec_l = ("<h2 class='sec'>주제 목록<span class='secsub'>규칙이 묶은 결과</span></h2>"
             f"<div class='an10'><div class='tablewrap'>{l_head}{l_rows}</div></div>{more}")

    honest = ("<div class='honest'>이 화면은 읽기 전용입니다 — 여기서 바로 합치지 않습니다. "
              "합칠 쌍을 정해 알려주시면 규칙(단일 출처)에 반영해 분석·트렌드에 함께 적용됩니다. "
              "‘글 수 적은 변형이 큰 주제 옆에 붙어 있는지’를 위주로 보세요(예: 조사 ‘가’가 붙어 갈린 것).</div>")

    body = (f"<div class='wrap'>{intro}{sec_c}"
            f"<div style='margin-top:32px'>{sec_l}</div>{honest}</div>")
    return page("주제 검수", topbar, body)


# ---------------------------------------------------------------------------
# HTTP 서버
# ---------------------------------------------------------------------------
def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 콘솔 조용히

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if u.path == "/tokens.css":
                return self._send_file(TOKENS_PATH, "text/css; charset=utf-8")
            conn = get_connection(db_path)
            try:
                if u.path == "/":
                    self._send_html(render_list(
                        conn, qs.get("view", ["all"])[0], qs.get("sort", ["recent"])[0]))
                elif u.path == "/analysis":
                    self._send_html(render_analysis(
                        conn, qs.get("sort", ["views"])[0],
                        qs.get("min_age", [None])[0] == "30"))
                elif u.path == "/trends":
                    self._send_html(render_trends(conn))
                elif u.path == "/topics":
                    self._send_html(render_topics(conn))
                elif u.path == "/data":
                    self._send_html(render_data(conn))
                elif u.path == "/post":
                    self._send_html(render_detail(conn, qs.get("id", [None])[0]))
                elif u.path == "/img":
                    self._send_image(conn, qs.get("id", [None])[0])
                else:
                    self._send_html(render_not_found(), code=404)
            finally:
                conn.close()

        def _send_html(self, text, code=200):
            data = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, path, ctype):
            if not os.path.exists(path):
                return self._send_html(render_not_found(), code=404)
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_image(self, conn, id_raw):
            # 로컬 검수용: reuse_scope·contains_person 게이트는 표시 목적상 완화.
            #   단 경로안전은 유지 — image_id는 정수 강제, local_path는 DB에서만,
            #   corpus 하위 실제 파일만 서빙(safe_image_path). 아니면 404.
            try:
                image_id = int(id_raw)
            except (TypeError, ValueError):
                return self._deny()
            row = conn.execute(
                "SELECT local_path FROM post_images WHERE image_id=?", (image_id,)).fetchone()
            fp = safe_image_path(row["local_path"]) if row else None
            if not fp:
                return self._deny()
            ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
            self._send_file(fp, ctype)

        def _deny(self):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("미리보기 가림".encode("utf-8"))

    return Handler


def make_server(db_path=None, port=8765, host="127.0.0.1"):
    """서버를 만들어 반환(start 전). 테스트는 port=0으로 임시 포트 사용."""
    db_path = db_path or DEFAULT_DB_PATH
    return ThreadingHTTPServer((host, port), make_handler(db_path))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    port = 8765
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print("포트는 숫자여야 합니다. 예: python src/viewer.py 9000")
            return 1
    httpd = make_server(port=port)
    url = f"http://localhost:{httpd.server_address[1]}/"
    print(f"품질 확인 뷰어를 켰습니다. 브라우저에서 여세요:  {url}")
    print("끄려면 이 창에서 Ctrl+C 를 누르세요.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n뷰어를 껐습니다.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
