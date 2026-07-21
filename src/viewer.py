# -*- coding: utf-8 -*-
"""viewer.py — 로컬 품질 확인 뷰어

추출된 글을 관리자 1인이 브라우저로 눈검수하는 도구. 파이썬 표준 http.server만 사용
(새 웹 프레임워크 없음). 화면 2개: 목록(/) · 글 1건 상세(/post?id=N).

★ 창고에 쓰는 곳은 팩트 룰북 화면 한 곳뿐이다(POST /fact/save — 팩트 값 고치기·도장·되돌리기).
  글 목록·분석·트렌드·주제 검수·데이터는 읽기 전용이고, 다른 주소로 온 쓰기 요청은 거절한다.

★ 불변 1(마스킹) — 이 파일이 반드시 지키는 것:
  - 왼쪽 '가공 전 원문' 패널: body_raw 파일을 서버가 읽어 masking.mask_text로 개인정보만
    가린 텍스트만 화면에 낸다(masked_raw_flow). 원본 문자열(전화번호·이름 등)은 어떤 경로로도
    브라우저에 나가지 않는다. 원본 줄바꿈/흐름은 유지하되 HTML 이스케이프 후 가림 자리만 하이라이트.
  - 오른쪽 '정리 결과' 패널: 오직 마스킹본만 쓴다 — post_paragraphs.clean_text (intake가 가려 저장한 문단).
  - raw_text / body_raw / body_clean 의 '원본 문자열'은 마스킹 통과분 외에는 화면에 절대 출력하지 않는다.
  - "가림 종류·건수"는 서버 내부에서만 계산한다: body_clean(개인정보 포함)을 서버가 읽어
    masking.py로 다시 가려 hit의 '종류'만 세고, 원본 문자열은 화면으로 내보내지 않는다.
    (상세 화면 1건만 이렇게 센다. 목록은 창고에 저장된 건수(posts.mask_count)를 읽을 뿐
     본문 파일을 열지 않는다 — 저장된 지문이 지금 규칙과 다르면 숫자 대신 '다시 세기 필요'.)
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
import keyword_normalize as kn  # noqa: E402  (주제 묶기 단일 출처 — 트렌드·분석·목록이 같은 함수를 쓴다)
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
/* 상태 배지(pill) — 색만이 아니라 글자로도 구분.
   white-space: nowrap — '성공(자동추출)'처럼 긴 상태가 좁은 칸에서 두 줄로 접히지 않게(사용 피드백). */
.badge { display: inline-block; border-radius: 999px; padding: 4px 12px;
         font-size: 13px; font-weight: 700; border: 1px solid; white-space: nowrap; }
.badge.ok { color: var(--ok); background: var(--ok-bg); border-color: var(--ok); }
.badge.warn { color: var(--warn); background: var(--warn-bg); border-color: var(--warn); }
.badge.danger { color: var(--danger); background: var(--danger-bg); border-color: var(--danger); }
/* '아직 안 봄'(미확인) — 잘못이 아니므로 경고색을 쓰지 않는다. .chip dim과 같은 회색 값. */
.badge.dim { color: var(--muted); background: #eef1f5; border-color: var(--line); }
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
/* 값 그대로 보여주는 글칸 — 줄바꿈 유지(문단 카드·팩트 칸 공용) */
.ptext { margin-top: 8px; white-space: pre-wrap; word-break: break-word; }
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
/* 목록 표 — 줄 전체가 클릭 영역(진짜 링크). 9컬럼(제목·카페·담당자·상태·가림·문단·이미지·조회수·작성일)
   '가림' 칸은 '다시 세기 필요'가 들어갈 만큼, '상태' 칸은 '실패-접근불가(기타)' 배지가 한 줄로
   들어갈 만큼 넓힌다(그만큼 제목을 줄임 — 제목은 말줄임 처리라 안전). */
.listhead, .listrow { display: grid;
    grid-template-columns: 2fr 1fr 0.8fr 1.7fr 1.2fr 0.5fr 0.55fr 0.8fr 1fr; gap: 12px;
    padding: 12px 16px; align-items: center; }
/* 글 목록도 분석 표와 같은 규칙 — 최소 폭 아래로는 짜부러지지 않고 가로로 밀린다(.tablewrap) */
.postlist .listhead, .postlist .listrow { min-width: 1040px; }
.listhead { color: var(--muted); font-size: 13px; font-weight: 700;
            border-bottom: 2px solid var(--line); }
.listrow { background: var(--paper); border-bottom: 1px solid var(--line);
           color: var(--ink); }
.listrow:hover { background: #eef4fb; text-decoration: none; }
.listrow .r-title { font-weight: 700; color: var(--brand); }
/* 표의 칸은 접히지 않는다 — 넘치면 …로 줄이고, 표 전체는 .tablewrap으로 가로 스크롤.
   (글 목록·분석·트렌드·팩트 표 공통. 칸마다 두 줄로 접혀 줄 높이가 들쭉날쭉하던 문제) */
.listhead > div, .listrow > div { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.num-dim { color: var(--muted); }
.num { text-align: right; }
/* '가림' 칸에 숫자를 못 쓰는 줄(규칙이 바뀌었거나 아직 안 셈) — 회색 작은 글자.
   경고색을 쓰지 않는 이유: 규칙을 한 번 손보면 온 줄이 이 상태가 되는데 전부 경고색이면 경고가 아니게 된다.
   해야 할 일은 목록 위 안내 줄이 말한다. */
.recount { color: var(--muted); font-size: 12px; }
/* 쪽 이동 줄 — 훑는 동안 반복해서 누르는 주 동작이라 손가락 크기(44px) 확보.
   좁은 화면에선 가로로 삐져나가지 않고 두 줄로 넘어간다. 색은 기존 변수만 사용. */
.pager { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px;
         margin: 8px 0 24px; font-size: 15px; }
.pager a, .pager .cur, .pager .off { display: inline-flex; align-items: center;
         min-height: 44px; padding: 0 12px; }
.pager a { font-weight: 700; }
.pager .cur { font-weight: 800; }
/* 누를 수 없는 이동 — 자리는 지키되 흐린 글자만(화살표·밑줄 없음. 색만으로 구분하지 않음) */
.pager .off { color: var(--muted); }
.filters { margin: 16px 0; font-size: 14px; }
.filters a.on { font-weight: 800; text-decoration: underline; }
.filters .note { color: var(--muted); font-size: 13px; margin-left: 8px; }
/* 걸러 보기 줄 — 고르는 칸 + [적용] 한 벌(GET 폼, 자바스크립트 0).
   좁은 화면에서는 칸이 위아래로 접히고 손가락 크기(44px)를 지킨다. */
.filters.pick { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.filters.pick label { display: inline-flex; align-items: center; gap: 6px; }
.filters select, .filters button { font-family: var(--font); font-size: 15px;
         min-height: 44px; border-radius: 6px; }
.filters select { padding: 0 8px; border: 1px solid var(--line); background: var(--paper);
         color: var(--ink); max-width: 220px; }
.filters button { font-weight: 700; color: #fff; background: var(--brand);
         border: 1px solid var(--brand); padding: 0 20px; cursor: pointer; }
/* 걸러 놓은 조건 — 무엇 때문에 목록이 줄었는지 글자로 알린다(색만으로 구분하지 않음).
   ✕는 이 화면의 주 동작이라 최소 44×44. 조건이 많으면 두 줄로 접혀 내려간다. */
.fchips { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px;
          margin: 12px 0 4px; font-size: 14px; }
.fchip { display: inline-flex; align-items: center; border: 1px solid var(--line);
         border-radius: 999px; background: var(--paper); padding-left: 12px; }
.fchip .x { display: inline-flex; align-items: center; justify-content: center;
            min-width: 44px; min-height: 44px; font-weight: 800; }
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
/* 트렌드 표 — 계절(주제·총·최다월·쏠림%) / 월내(주제·초·중·말·총) */
.an7 .listhead, .an7 .listrow { grid-template-columns: 2fr 0.9fr 1fr 1fr; min-width: 520px; }
.an8 .listhead, .an8 .listrow { grid-template-columns: 2fr 0.8fr 0.8fr 0.8fr 0.8fr; min-width: 560px; }
/* 주제 검수 — near중복 후보(주제A·글수·주제B·글수·사유) / 주제 목록(주제·글수) */
.an9 .listhead, .an9 .listrow { grid-template-columns: 1.6fr 0.5fr 1.6fr 0.5fr 1.4fr;
            min-width: 700px; }
.an10 .listhead, .an10 .listrow { grid-template-columns: 3fr 1fr; min-width: 360px; }
/* 팩트 룰북 목록 — 항목명·종류·카테고리·상태·고친 칸·확인 날짜 */
.an11 .listhead, .an11 .listrow { grid-template-columns: 2.4fr 0.7fr 1.2fr 1fr 0.8fr 1fr;
            min-width: 720px; }
/* 분석(재료 찾기) 표 — 주제별 우리 글(an12) / 팩트 항목 맞춰보기·담당자별(an13, 둘 다 4열) */
.an12 .listhead, .an12 .listrow { grid-template-columns: 2.4fr 0.8fr 1.1fr 1fr 1fr;
            min-width: 640px; }
.an13 .listhead, .an13 .listrow { grid-template-columns: 2fr 1fr 1.2fr 1fr; min-width: 560px; }
/* 결론 난 참고표 접기 — 손가락 크기(44px)와 굵은 글자. 브라우저 기본 details/summary */
.foldsec { margin-top: 32px; }
.foldsec > summary { display: flex; align-items: center; min-height: 44px; cursor: pointer;
            font-size: 16px; font-weight: 700; color: var(--brand); }
.foldsec > summary + * { margin-top: 12px; }
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
/* 그 달에 이 주제 글이 한 건도 없음 — 빈 칸으로 두면 '데이터 없음'으로 오해되므로 가운뎃점을 찍는다 */
.hm-c.zero { color: var(--muted); }
/* 색 범례 — 색만으로 구분되지 않게 실제 몫(%)을 글자로 함께 적는다(접근성) */
.hm-legend { display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
             margin: 0 0 10px; font-size: 12px; color: var(--muted); }
.hm-legend .hm-c { width: 52px; }
/* 누를 수 없는 집계 줄(섹션 2·3·4·트렌드) — hover 강조·커서 없음 */
.listrow.static { cursor: default; }
.listrow.static:hover { background: var(--paper); }
/* ── 팩트 고치기(4차) — 뷰어에 입력 요소가 처음 생긴다. 브라우저 기본을 쓰되 글꼴·폭·높이만 맞춤 ── */
.fedit label { display: block; font-weight: 700; margin-bottom: 6px; }
.fedit textarea { width: 100%; min-height: 9em; font-family: var(--font); font-size: 15px;
        line-height: 1.6; padding: 10px; border: 1px solid var(--line); border-radius: 6px;
        color: var(--ink); background: var(--paper); }
.fedit .help { color: var(--muted); font-size: 13px; margin: 6px 0 10px; }
/* 누르는 것은 전부 손가락 크기(44px). 상태를 바꾸는 것은 버튼, 화면 이동은 링크(모양만 같게) */
.fedit .btn { display: inline-flex; align-items: center; min-height: 44px; padding: 0 18px;
        border-radius: 6px; font-family: var(--font); font-size: 15px; font-weight: 700;
        cursor: pointer; border: 1px solid var(--line); background: var(--paper); color: var(--ink); }
.fedit .btn:hover { text-decoration: none; }
.fedit .btn.go { background: var(--brand); border-color: var(--brand); color: #fff; }
/* 지금 상태인 도장은 눌린 모양(색만이 아니라 테두리 굵기로도 구분) */
.fedit .btn.on { border-color: var(--brand); border-width: 2px; font-weight: 800; }
/* 하단 고정 도장 바 — 칸이 7~8개라 상세가 길다. 다 읽고 위로 되돌아가는 왕복을 없앤다 */
.stampbar { position: sticky; bottom: 0; z-index: 9; background: var(--paper);
        border-top: 1px solid var(--line); padding: 12px 24px; }
.stampbar form { max-width: 1100px; margin: 0 auto;
        display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.stampbar details { flex: 1 1 100%; }
.stampbar summary { min-height: 44px; display: flex; align-items: center;
        font-weight: 700; color: var(--brand); cursor: pointer; }
/* 방금 한 행동의 결과 한 줄(.honest는 '항상 떠 있는 정직 경고'라 의미·크기가 다르다) */
.flash { border: 1px solid var(--ok); background: var(--ok-bg); color: var(--ok);
        border-radius: 8px; padding: 10px 14px; margin: 0 0 16px; font-weight: 700;
        display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.flash.bad { border-color: var(--danger); background: var(--danger-bg); color: var(--danger); }
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
            + item("/facts", "팩트 룰북", "facts") + " · "
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
PAGE_SIZE = 100   # 한 쪽에 보이는 글 수(사용자 확정). 바꾸려면 이 숫자 하나만.
# 걸러 보기 값(담당자·카페·주제)은 창고에 든 자유 문자열이라 목록으로 좁힐 수 없다.
# 대신 ①길이 상한 ②반드시 ? 바인딩(문자열 결합 금지) ③화면 출력은 esc() 로 막는다.
# 한계: 60자를 넘는 카페·담당자·주제 이름이 창고에 생기면 그 이름으로는 걸러지지 않는다
# (지금 값들은 훨씬 짧다. 확인: SELECT MAX(LENGTH(keyword)) FROM posts — 넘는 값이 나오면 상한을 올릴 것).
MAX_FILTER_LEN = 60


def _clip(v):
    return (v or "").strip()[:MAX_FILTER_LEN]


def topic_members(conn):
    """주제(자동으로 묶은 이름) → [(원본 키워드, 창고 글 수, 그중 본문 가져온 글 수)].
    글 목록과 주제 검수가 같은 함수를 쓴다 — 두 화면의 숫자가 어긋날 수 없다."""
    m = defaultdict(list)
    for r in conn.execute(
            "SELECT keyword, COUNT(*) n, "
            "SUM(CASE WHEN body_raw_path IS NOT NULL THEN 1 ELSE 0 END) nb "
            "FROM posts WHERE keyword IS NOT NULL GROUP BY keyword"):
        t = kn.normalize(r["keyword"])
        if t:
            m[t].append((r["keyword"], r["n"], r["nb"]))
    return m


def render_list(conn, view="all", sort="recent", page_no=1, cafe="", staff="", topic=""):
    # 입력검증: 쿼리값을 링크 href에 되비추므로 안전 리터럴로만 좁힌다(주입 차단)
    view = view if view in ("ok", "fail") else "all"
    sort = "views" if sort == "views" else "recent"
    cafe, staff, topic = _clip(cafe), _clip(staff), _clip(topic)
    # 쪽 번호도 같은 결 — 정수만 통과. 범위 밖은 아래에서 가장 가까운 쪽으로(오류 화면 없음)
    try:
        page_no = int(page_no)
    except (TypeError, ValueError):
        page_no = 1
    try:
        # ★ 조회수(참고 신호)를 한 번의 JOIN으로(행마다 재조회 없음 — N+1 회피).
        # ★ 가림 건수는 저장된 값(mask_count)을 그대로 읽는다 — 목록은 본문 파일을 열지 않는다.
        # ★ 걸러 보기(카페·담당자)는 SQL where + ? 바인딩. 값은 절대 문자열로 붙이지 않는다.
        where, params = ["p.body_raw_path IS NOT NULL"], [AUTO_VIEW_MARK]
        if cafe:
            where.append("p.cafe_name = ?")
            params.append(cafe)
        if staff:
            where.append("p.staff_name = ?")
            params.append(staff)
        rows = conn.execute(
            "SELECT p.post_id, p.title, p.keyword, p.cafe_name, p.staff_name, "
            "p.extraction_status, "
            "p.publish_date, p.mask_count, p.mask_rules_fingerprint, rs.view_count AS views, "
            "(SELECT COUNT(*) FROM post_paragraphs pp WHERE pp.post_id=p.post_id) para_n, "
            "(SELECT COUNT(*) FROM post_images pi WHERE pi.post_id=p.post_id) img_n "
            "FROM posts p "
            "LEFT JOIN reference_signals rs "
            "  ON rs.post_id=p.post_id AND rs.collected_from_sheet=? "
            "WHERE " + " AND ".join(where)
            + " ORDER BY p.updated_at DESC, p.post_id DESC", params).fetchall()
        # 지금의 가림 규칙 지문 — 요청당 딱 한 번(질의 2번). 저장된 지문과 다르면 옛 숫자다.
        now_fp = masking.rules_fingerprint(conn)
        # 상단 배지·안내 줄은 '창고 전체' 기준을 유지한다(걸러진 숫자는 범위 줄이 말한다 — 명세 §4-2).
        tot = conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN extraction_status LIKE '성공%' THEN 1 ELSE 0 END) ok "
            "FROM posts WHERE body_raw_path IS NOT NULL").fetchone()
        n_stale = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE body_raw_path IS NOT NULL "
            "AND (mask_count IS NULL OR mask_rules_fingerprint IS NOT ?)", (now_fp,)).fetchone()[0]
        # 고르는 칸의 선택지 — 목록에 실제로 나오는 글에서만 뽑는다(대부분 고르면 글이 나온다.
        # 다만 보기(성공만·실패만)나 다른 조건과 겹치면 0건이 될 수 있다)
        pick_sql = ("SELECT DISTINCT {c} v FROM posts WHERE body_raw_path IS NOT NULL "
                    "AND {c} IS NOT NULL AND {c} <> '' ORDER BY v")
        cafes = [r["v"] for r in conn.execute(pick_sql.format(c="cafe_name"))]
        staffs = [r["v"] for r in conn.execute(pick_sql.format(c="staff_name"))]
        # 주제 걸러 보기 — 트렌드·분석과 같은 함수(kn.normalize)로 원본 키워드를 묶어 대조한다.
        #   주제는 창고에 저장된 값이 아니라 '묶은 결과'라 SQL로 못 거른다 → 원본 키워드 목록으로 환원.
        members = topic_members(conn).get(topic, []) if topic else []
        if topic:
            kwset = {k for k, _, _ in members}
            rows = [r for r in rows if r["keyword"] in kwset]
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

    n_total, n_ok = tot["n"], (tot["ok"] or 0)
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

    def stale(r):
        """저장된 가림 건수를 못 믿는 줄 — 아직 안 셌거나, 센 뒤에 규칙이 바뀌었다.
        둘을 구별해 보여주지 않는다(사용자가 할 일이 '다시 세기'로 똑같다)."""
        return r["mask_count"] is None or r["mask_rules_fingerprint"] != now_fp

    def on(cond):
        return " class='on'" if cond else ""

    def href(**over):
        """지금 조건을 그대로 달고 다니는 목록 주소(바뀌는 것만 넘긴다). 쪽은 언제나 1쪽부터."""
        d = dict(view=view, sort=sort, page_no=1, cafe=cafe, staff=staff, topic=topic)
        d.update(over)
        return list_href(**d)
    filters = ("<div class='filters'>보기: "
               f"<a href='{href(view='all')}'{on(view=='all')}>전체</a> · "
               f"<a href='{href(view='ok')}'{on(view=='ok')}>성공만</a> · "
               f"<a href='{href(view='fail')}'{on(view=='fail')}>실패만</a>"
               "&nbsp;&nbsp;|&nbsp;&nbsp;정렬: "
               f"<a href='{href(sort='recent')}'{on(sort=='recent')}>최신순</a> · "
               f"<a href='{href(sort='views')}'{on(sort=='views')}>조회수 높은 순</a>"
               "<span class='note'>조회수는 참고 신호입니다.</span></div>")
    # 보기·정렬 링크에는 page를 달지 않는다 → 보기·정렬을 바꾸면 언제나 1쪽부터
    # (다른 목록이 됐는데 27쪽에 서 있으면 텅 빈 화면이 뜬다). 걸러 놓은 조건은 그대로 따라간다.

    # ①-a 걸러 보기 줄 — 고르는 칸 + [적용] 하나(GET 폼. 자바스크립트가 없어 고른 즉시 이동 못 함).
    #   게시판은 넣지 않는다(값이 대부분 비어 있어 넣으면 대부분 글이 사라져 보인다).
    def picker(name, cur, values, all_label):
        opts = [f"<option value=''>{all_label}</option>"]
        opts += [f"<option value='{esc(v)}'{' selected' if v == cur else ''}>{esc(v)}</option>"
                 for v in values]
        return f"<select name='{name}'>{''.join(opts)}</select>"
    keep = "".join(
        f"<input type='hidden' name='{k}' value='{esc(v)}'>"
        for k, v in (("view", view if view != "all" else ""),
                     ("sort", sort if sort != "recent" else ""),
                     ("topic", topic)) if v)
    pick = ("<form class='filters pick' method='get' action='/'>걸러 보기: "
            f"<label>카페 {picker('cafe', cafe, cafes, '카페 전체')}</label>"
            f"<label>담당자 {picker('staff', staff, staffs, '담당자 전체')}</label>"
            f"{keep}<button type='submit'>적용</button>"
            "<span class='note'>주제로 걸러 보려면 트렌드·분석 화면에서 주제 이름을 누르세요."
            "</span></form>")

    # ①-b 조건 줄 — 무엇 때문에 목록이 줄었는지. 0건이어도 이 줄은 지우지 않는다(명세 §4-3).
    conds = [(k, f, v) for k, f, v in
             (("주제", "topic", topic), ("담당자", "staff", staff), ("카페", "cafe", cafe)) if v]
    if conds:
        chip_parts = []
        for k, f, v in conds:
            drop = href(**{f: ""})                  # 그 조건만 빠진 목록으로
            chip_parts.append(f"<span class='fchip'>{k} ‘{esc(v)}’"
                              f"<a class='x' href='{drop}' title='{k} 조건 지우기'>✕</a></span>")
        chips = "".join(chip_parts)
        clear_href = href(cafe="", staff="", topic="")
        clear = (f"<a href='{clear_href}'>모두 지우기</a>" if len(conds) > 1 else "")
        cond_html = f"<div class='fchips'>걸러 보는 중: {chips}{clear}</div>"
    else:
        clear_href, cond_html = "/", ""
    # ★ 두 숫자가 다른 이유를 화면이 말한다 — 트렌드·분석의 주제 옆 숫자는 '창고 글 수'(본문 없는 글 포함),
    #   아래 목록은 '본문을 가져온 글'만. 숫자를 맞추지 않고 둘 다 보여준다(사용자 결정 2026-07-21).
    n_topic_all = sum(n for _, n, _ in members)
    n_topic_body = sum(nb for _, _, nb in members)
    if topic and members:
        more_cond = " 다른 조건도 함께 걸었다면 여기서 더 줄어듭니다." if len(conds) > 1 else ""
        cond_html += (
            f"<p class='intro sub'>창고에 주제 ‘{esc(topic)}’로 쓴 글은 <b>{_comma(n_topic_all)}건</b>, "
            f"그중 <b>본문을 가져온 글은 {_comma(n_topic_body)}건</b>입니다. "
            f"아래 목록에는 본문을 가져온 글만 나옵니다.{more_cond} "
            "트렌드·분석 화면에서 주제 이름 옆에 보이는 숫자는 창고 글 수 쪽이에요.</p>")
        # 주제는 자동으로 묶은 값이다 — 사람이 원본과 대조할 수 있게 묶인 원본 키워드를 같은 화면에 둔다
        # (헌장 디자인 규칙. /topics의 원본 키워드 표기와 같은 방식).
        ms = sorted(members, key=lambda x: -x[1])
        head_kw = ", ".join(f"{esc(k)}({n})" for k, n, _ in ms[:12])
        more_kw = f" 외 {len(ms) - 12}개" if len(ms) > 12 else ""
        cond_html += (f"<p class='intro sub'>주제 ‘{esc(topic)}’에는 원본 키워드 {len(ms)}종이 "
                      f"묶여 있습니다(괄호=창고 글 수): {head_kw}{more_kw}</p>")

    # ② 안내 줄 — 다시 세야 하는 글이 있을 때만. 건수는 이 쪽이 아니라 창고 전체 기준(할 일의 크기).
    notice = (f"<p class='intro sub'>가림 건수를 다시 세야 하는 글이 {_comma(n_stale)}건 있습니다"
              "(가림 규칙이 바뀌었거나, 아직 세지 않은 글입니다). "
              "클로드 코드에 ‘가림 건수 다시 세 줘’라고 말하면 정리됩니다.</p>") if n_stale else ""

    # 거르고 정렬한 '뒤에' 잘라낸다 — 거르기·정렬 규칙은 한 글자도 안 바뀐다(계획 §1)
    matched = [r for r in rows if match(r)]
    n_shown = len(matched)
    if n_shown == 0:
        # 조건이 창고에 아예 없는 값일 때는 그 사실부터 말한다(주소를 손으로 고친 경우 등)
        unknown = [(k, v) for k, v in (("카페", cafe if cafe not in cafes else ""),
                                       ("담당자", staff if staff not in staffs else ""),
                                       ("주제", topic if not members else "")) if v]
        if unknown:
            k, v = unknown[0]
            msg = f"‘{esc(v)}’(으)로 걸러 보려 했지만 그런 {k}가 창고에 없습니다."
        elif topic and n_topic_body == 0:
            # 주제는 창고에 있는데 본문을 하나도 안 가져온 경우 — '없는 주제'와 다른 상황이다
            msg = (f"창고에 주제 ‘{esc(topic)}’로 쓴 글은 {_comma(n_topic_all)}건 있지만, "
                   "그중 본문을 가져온 글이 아직 없습니다. "
                   "이 목록에는 본문을 가져온 글만 나오기 때문에 비어 있어요.")
        elif conds:
            msg = ("고른 조건에 맞는 글이 없습니다. 조건: "
                   + " · ".join(f"{k} ‘{esc(v)}’" for k, _, v in conds)
                   + ". 조건을 하나씩 지워보세요.")
        else:
            what = {"ok": "성공만", "fail": "실패만"}.get(view, "전체")
            msg = f"{what} 보기에 해당하는 글이 없습니다. 위에서 ‘전체’를 눌러 보세요."
        undo = (f"<div style='margin-top:12px'><a href='{clear_href}'>모두 지우기</a></div>"
                if conds else "")
        body = (f"<div class='wrap'>{filters}{pick}{cond_html}"
                f"<div class='state'>{msg}{undo}</div></div>")
        return page("추출 글 품질 확인", topbar, body)

    n_pages = -(-n_shown // PAGE_SIZE)                  # 올림 나눗셈
    page_no = min(max(page_no, 1), n_pages)             # 없는 쪽 → 가장 가까운 쪽
    start = (page_no - 1) * PAGE_SIZE
    chunk = matched[start:start + PAGE_SIZE]

    # ② 범위 줄 — 상단 배지(창고 전체)와 나란히 읽으면 이어지도록 같은 숫자를 다시 부른다
    prefix = {"ok": "성공만 ", "fail": "실패만 "}.get(view, "")
    if n_pages == 1:
        range_line = f"{prefix}{_comma(n_shown)}건 모두 보는 중"
    else:
        range_line = (f"{prefix}{_comma(n_shown)}건 중 {_comma(start + 1)}~"
                      f"{_comma(start + len(chunk))}번째 보는 중 · {page_no} / {n_pages}쪽")
    range_html = f"<p class='intro sub'>{range_line}</p>"

    head = ("<div class='listhead'>"
            "<div>제목</div><div>카페</div><div>담당자</div><div>상태</div>"
            "<div>가림</div><div>문단</div><div>이미지</div>"
            "<div class='num'>조회수</div><div>작성일</div></div>")
    body_rows = []
    for r in chunk:
        ok = is_success(r["extraction_status"])
        if stale(r):
            # 못 믿는 숫자는 아예 안 쓴다 — 옛 숫자가 조용히 보이는 일이 구조적으로 불가능해진다.
            mask_cell = "<div class='recount'>다시 세기 필요</div>"
        else:
            mask_n = r["mask_count"]
            dim = "" if mask_n else " class='num-dim'"   # 0건은 흐리게(지금과 동일)
            mask_cell = f"<div{dim}>{mask_n}건</div>"
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
            f"{mask_cell}"
            f"<div>{r['para_n']}</div><div>{r['img_n']}</div>"
            f"{view_cell}"
            f"<div>{esc(r['publish_date'] or '-')}</div></a>")

    body = (f"<div class='wrap'>{filters}{pick}{cond_html}{notice}{range_html}"
            f"<div class='postlist'><div class='tablewrap'>{head}{''.join(body_rows)}</div></div>"
            f"{pager_html(view, sort, page_no, n_pages, cafe, staff, topic)}</div>")
    return page("추출 글 품질 확인", topbar, body)


def list_href(view="all", sort="recent", page_no=1, cafe="", staff="", topic=""):
    """목록 링크 — 지금 보기·정렬·걸러 놓은 조건을 그대로 달고 다닌다(쪽을 옮겨도 유지).
    보기·정렬은 좁혀진 리터럴이고, 카페·담당자·주제는 자유 문자열이라 urlencode로 감싼다."""
    q = [(k, v) for k, v in (("view", view), ("sort", sort)) if v not in ("all", "recent")]
    q += [(k, v) for k, v in (("cafe", cafe), ("staff", staff), ("topic", topic)) if v]
    if page_no > 1:
        q.append(("page", page_no))
    return "/?" + urllib.parse.urlencode(q) if q else "/"


def topic_link(topic):
    """트렌드·분석의 주제 이름 → 그 주제로 걸러진 글 목록. 목록도 같은 kn.normalize 결과로 되거른다."""
    return f"<a class='r-title' href='{list_href(topic=topic)}'>{esc(topic)}</a>"


def staff_link(name):
    """담당자 이름 → 그 담당자 글 목록. 집계에서 만든 '(없음)'은 걸 수 없으니 글자만."""
    if not name or name == "(없음)":
        return esc(name or "-")
    return f"<a class='r-title' href='{list_href(staff=name)}'>{esc(name)}</a>"


def pager_html(view, sort, page_no, n_pages, cafe="", staff="", topic=""):
    """③ 쪽 이동 줄(목록 아래만). 한 쪽뿐이면 줄 자체가 없다.
    번호를 늘어놓지 않는다 — 이 화면의 훑기는 순서대로 넘기기다(사용자 확정).
    누를 수 없는 이동은 없애지 않고 흐리게 남긴다(쪽마다 단추 위치가 움직이면 연달아 누르기가 방해받음)."""
    if n_pages <= 1:
        return ""

    def item(label_on, label_off, target, enabled):
        if not enabled:      # 화살표·밑줄 없이 흐린 글자 — 흑백으로 봐도 링크가 아님이 보인다
            return f"<span class='off'>{label_off}</span>"
        return f"<a href='{list_href(view, sort, target, cafe, staff, topic)}'>{label_on}</a>"
    prev_ok, next_ok = page_no > 1, page_no < n_pages
    return ("<div class='pager'>"
            + item("← 처음", "처음", 1, prev_ok)
            + item("← 이전", "이전", page_no - 1, prev_ok)
            + f"<span class='cur'>{page_no} / {n_pages}쪽</span>"
            + item("다음 →", "다음", page_no + 1, next_ok)
            + item("끝 →", "끝", n_pages, next_ok)
            + "</div>")


# ---------------------------------------------------------------------------
# 화면 C — 분석 (창고에서 원고 재료를 찾는 입구, 읽기 전용)
#   역할 전환(2026-07-21): '조회수 순위표' → '주제로 우리 글 찾기'.
#   위 세 섹션(주제·팩트·담당자)은 조회수를 쓰지 않는다 — 조회수가 0건이어도 이 화면은 쓸모가 있다.
#   결론이 난 조회수 표들은 지우지 않고 아래 접기 안에 그대로 둔다.
# ---------------------------------------------------------------------------
SECTION1_TOP = 50   # 섹션1 표에 보일 상위 건수(결정 5)
SECTION2_TOP = 20   # 섹션2 키워드 상위 N
TOPIC_TOP = 50      # 섹션 A(주제별 우리 글)에 한 번에 보일 주제 수 — 주제는 1,300종이 넘는다
TOPIC_MORE = 200    # '더 보기'를 누르면 보일 주제 수(사용자 확정: 50 → 200)
FACT_GAP_TOP = 20   # 섹션 B(팩트 항목 이름 맞춰보기)에 보일 팩트 항목 수
ANALYSIS_TITLE = "주제로 우리 글 찾기"
TOPIC_SORTS = ("many", "few", "name")


def analysis_topic_rows(conn):
    """주제별 우리 글 — 주제 · 쓴 글 · 조회수 있는 글 · 평균 문단 · 평균 이미지.
    ★ 조회수가 한 건도 없어도 만들어진다(이 화면 역할 전환의 실질).
    주제 묶기는 topic_members()·트렌드와 같은 경로(kn.normalize) — 화면끼리 숫자가 어긋날 수 없다.
    본문 파일은 열지 않는다(문단·이미지는 창고에 든 행 수만 센다)."""
    rows = conn.execute(
        "SELECT p.keyword, COUNT(*) n, "
        "SUM(CASE WHEN p.body_raw_path IS NOT NULL THEN 1 ELSE 0 END) nb, "
        "SUM(CASE WHEN rs.v IS NOT NULL THEN 1 ELSE 0 END) nv, "
        "SUM(COALESCE(pp.c, 0)) np, SUM(COALESCE(pi.c, 0)) ni "
        "FROM posts p "
        "LEFT JOIN (SELECT post_id, MAX(view_count) v FROM reference_signals "
        "           WHERE collected_from_sheet=? AND view_count IS NOT NULL "
        "           GROUP BY post_id) rs ON rs.post_id=p.post_id "
        "LEFT JOIN (SELECT post_id, COUNT(*) c FROM post_paragraphs GROUP BY post_id) pp "
        "       ON pp.post_id=p.post_id "
        "LEFT JOIN (SELECT post_id, COUNT(*) c FROM post_images GROUP BY post_id) pi "
        "       ON pi.post_id=p.post_id "
        "WHERE p.keyword IS NOT NULL GROUP BY p.keyword", (AUTO_VIEW_MARK,)).fetchall()
    agg = {}
    for r in rows:
        t = kn.normalize(r["keyword"])
        if not t:
            continue
        a = agg.setdefault(t, dict(topic=t, n=0, nb=0, nv=0, np=0, ni=0))
        a["n"] += r["n"]
        a["nb"] += r["nb"] or 0
        a["nv"] += r["nv"] or 0
        a["np"] += r["np"] or 0
        a["ni"] += r["ni"] or 0
    return list(agg.values())


def analysis_staff_rows(conn):
    """담당자별 우리 글 — 담당자 · 쓴 글 · 조회수 있는 글 · 평균 조회수.
    조회수가 없어도 '쓴 글'은 나온다. 평균 조회수는 조회수를 확보한 글만의 참고 신호."""
    rows = conn.execute(
        "SELECT COALESCE(NULLIF(p.staff_name, ''), '(없음)') s, COUNT(*) n, "
        "SUM(CASE WHEN rs.v IS NOT NULL THEN 1 ELSE 0 END) nv, "
        "SUM(COALESCE(rs.v, 0)) sv "
        "FROM posts p "
        "LEFT JOIN (SELECT post_id, MAX(view_count) v FROM reference_signals "
        "           WHERE collected_from_sheet=? AND view_count IS NOT NULL "
        "           GROUP BY post_id) rs ON rs.post_id=p.post_id "
        "GROUP BY s ORDER BY n DESC, s", (AUTO_VIEW_MARK,)).fetchall()
    return [dict(staff=r["s"], n=r["n"], nv=r["nv"] or 0,
                 avg=((r["sv"] / r["nv"]) if r["nv"] else None)) for r in rows]


def fact_gap_rows(conn, topic_n):
    """룰북 팩트 항목 이름이 우리 글 주제 이름과 맞는지 — 안 맞은 것 먼저, 그다음 글 적은 것부터.
    팩트 항목 이름도 글 키워드와 같은 방법(kn.normalize)으로 묶어 맞춰본다.
    ★ 이 표가 아는 건 '이름이 맞았는지'뿐이다. 이름이 안 맞은 항목(matched=False)을 '0건'으로
      찍으면 '안 썼다'는 거짓말이 된다(실측: 이름만 다르고 83편을 쓴 주제가 있었다).
    점수·추천이 아니다 — 이름이 맞았는지와 글 수, 두 사실로만 줄을 세운다."""
    rows = conn.execute(
        "SELECT fact_id, item_name, category, review_status FROM rulebook_facts").fetchall()
    out = []
    for r in rows:
        t = kn.normalize(r["item_name"] or "")
        hit = bool(t) and t in topic_n      # 글 주제 목록에 그 이름이 실제로 있는가
        out.append(dict(fact_id=r["fact_id"], name=r["item_name"] or "(이름 없음)",
                        cat=r["category"] or "-", status=r["review_status"] or "미확인",
                        topic=(t if hit else ""), matched=hit,
                        n=(topic_n.get(t, 0) if hit else 0)))
    out.sort(key=lambda x: (x["matched"], x["n"], x["name"]))
    return out


def render_analysis(conn, sort="views", min_age=False, tsort="many", more=False):
    """창고에서 원고 재료를 찾는 입구. 위 세 섹션(주제·팩트·담당자)은 조회수를 쓰지 않고,
    결론이 난 조회수 표들은 아래 접기 안에 그대로 남는다(지우지 않음)."""
    # 입력검증: sort·tsort를 링크 href에 되비추므로 안전 리터럴로만 좁힌다(주입 차단)
    sort = "vpd" if sort == "vpd" else "views"
    tsort = tsort if tsort in TOPIC_SORTS else "many"
    more = bool(more)
    topbar_menu = nav_menu("analysis")
    try:
        recs = analysis_records(conn, datetime.date.today())
        trows = analysis_topic_rows(conn)
        srows = analysis_staff_rows(conn)
        n_posts = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        facts = fact_gap_rows(conn, {t["topic"]: t["n"] for t in trows})
    except sqlite3.Error:
        topbar = ("<div class='topbar'>" + topbar_menu
                  + f"<span class='t-title'>{ANALYSIS_TITLE}</span></div>")
        body = ("<div class='wrap'><div class='state'>분석을 불러오지 못했습니다. "
                "자산창고 파일을 찾을 수 없어요. 자산창고를 먼저 만든 뒤 다시 열어주세요."
                "</div></div>")
        return page(ANALYSIS_TITLE, topbar, body)

    n_target = len(recs)
    topbar = ("<div class='topbar'>" + topbar_menu
              + f"<span class='t-title'>{ANALYSIS_TITLE}</span>"
              f"<span class='badge ok'>창고 글 {_comma(n_posts)}건</span></div>")

    if n_posts == 0:
        body = ("<div class='wrap'><div class='state'>아직 창고에 글이 없습니다. "
                "글을 넣은 뒤 이 화면을 새로고침하세요.</div></div>")
        return page(ANALYSIS_TITLE, topbar, body)

    # --- 안내 두 줄 + 정직 박스(화면 맨 위) ---
    intro = (
        "<p class='intro'>이 화면은 창고에서 <b>원고 재료를 찾는 입구</b>입니다.</p>"
        "<p class='intro sub'>주제·담당자를 누르면 그 조건의 글 목록이 열립니다.</p>")
    pct = (n_target * 100 // n_posts) if n_posts else 0
    honest_top = (
        "<div class='honest'>여기 숫자로 <b>알 수 있는 것</b>: 어떤 주제로 몇 편을 썼는지, "
        "그 주제 글이 보통 몇 문단·몇 장짜리인지, 룰북 팩트 항목 이름이 글 주제와 맞는지.<br>"
        "여기 숫자로 <b>알 수 없는 것</b>: 어떤 글이 잘 됐는지. 카페 조회수는 참고 신호일 뿐이고, "
        f"조회수가 있는 글도 전체의 {pct}%({_comma(n_target)}건)뿐입니다. 잘 됐는지는 우리 "
        "홈페이지·워드프레스 실측이 쌓여야 답할 수 있습니다.</div>")

    def on(cond):
        return " class='on'" if cond else ""

    def alink(**over):
        """이 화면 주소 — 지금 고른 것(정렬·기간·더 보기)을 그대로 달고 다닌다(바뀌는 것만 넘긴다).
        한 곳에서만 만든다: 예전엔 조각을 손으로 두 벌 이어붙여 한쪽 상태가 링크마다 흘렸다."""
        d = dict(sort=sort, min_age=min_age, tsort=tsort, more=more)
        d.update(over)
        q = [(k, v) for k, v in (("sort", d["sort"]), ("tsort", d["tsort"]))
             if v not in ("views", "many")]      # 기본값은 주소에 적지 않는다(주소가 짧게)
        if d["min_age"]:
            q.append(("min_age", 30))
        if d["more"]:
            q.append(("more", TOPIC_MORE))
        return "/analysis?" + urllib.parse.urlencode(q) if q else "/analysis"

    # --- 섹션 A: 주제별 우리 글(조회수 없이도 나온다) ---
    # 동률이면 언제나 주제 이름 가나다순 — 열 때마다 순서가 흔들려 보이지 않게(명세 §2-3의 규칙)
    if tsort == "name":
        trows.sort(key=lambda t: t["topic"])
    elif tsort == "few":
        trows.sort(key=lambda t: (t["n"], t["topic"]))
    else:
        trows.sort(key=lambda t: (-t["n"], t["topic"]))
    limit = TOPIC_MORE if more else TOPIC_TOP
    a_shown = trows[:limit]

    def _avg_cell(total, denom):
        return f"{total / denom:.1f}" if denom else "-"
    a_head = ("<div class='listhead'><div>주제</div><div class='num'>쓴 글</div>"
              "<div class='num'>조회수 있는 글</div><div class='num'>평균 문단</div>"
              "<div class='num'>평균 이미지</div></div>")
    a_rows = "".join(
        "<div class='listrow static'>"
        f"<div>{topic_link(t['topic'])}</div>"
        f"<div class='num'>{_comma(t['n'])}</div>"
        f"<div class='num{'' if t['nv'] else ' num-dim'}'>{_comma(t['nv'])}</div>"
        f"<div class='num'>{_avg_cell(t['np'], t['nb'])}</div>"
        f"<div class='num'>{_avg_cell(t['ni'], t['nb'])}</div></div>"
        for t in a_shown)
    a_sortbar = ("<div class='filters'>정렬: "
                 f"<a href='{alink(tsort='many')}'{on(tsort == 'many')}>많이 쓴 순</a> · "
                 f"<a href='{alink(tsort='few')}'{on(tsort == 'few')}>적게 쓴 순</a> · "
                 f"<a href='{alink(tsort='name')}'{on(tsort == 'name')}>가나다순</a></div>")
    if a_shown:
        a_table = f"<div class='an12'><div class='tablewrap'>{a_head}{a_rows}</div></div>"
    else:
        a_table = ("<div class='state'>주제로 묶을 글이 아직 없습니다. "
                   "글에 키워드가 적혀 있어야 주제로 묶입니다.</div>")
    # '더 보기'는 주소 링크로만(자바스크립트 0) — 목록 화면의 쪽 이동과 같은 방식
    a_more = (f"<div class='filters'><a href='{alink(more=not more)}'>"
              + (f"처음 {_comma(TOPIC_TOP)}개만 보기" if more
                 else f"더 보기({_comma(TOPIC_MORE)}개까지) →")
              + "</a></div>") if len(trows) > TOPIC_TOP else ""
    secA = ("<h2 class='sec'>주제별 우리 글</h2>"
            "<p class='intro sub'>비슷한 키워드를 하나의 주제로 묶어 센 것입니다. "
            f"‘쓴 글’은 창고에 든 글 수예요. 주제 {_comma(len(trows))}개 중 "
            f"{_comma(len(a_shown))}개가 보입니다.</p>"
            "<p class='intro sub'>‘조회수 있는 글’은 우리가 카페에서 직접 가져와 조회수를 "
            "확보한 글이에요 — 엑셀에 적혀 온 숫자는 여기 세지 않습니다.</p>"
            f"{a_sortbar}{a_table}{a_more}"
            "<p class='intro sub'>‘평균 문단·이미지’는 그 주제 글이 보통 어떻게 생겼는지입니다"
            "(본문을 가져온 글만으로 계산). 잘 된 글의 기준이 아니라 원고 틀을 잡을 때 쓰는 "
            "숫자예요.</p>")

    # --- 섹션 B: 팩트 항목 이름이 우리 글 주제와 맞나 ---
    #   ★ 이름이 안 맞은 항목을 '0건'으로 찍지 않는다 — '안 썼다'가 아니라 '이름이 안 맞는다'가 사실.
    if facts:
        n_un = sum(1 for f in facts if f["status"] == "미확인")
        n_hit = sum(1 for f in facts if f["matched"])
        b_head = ("<div class='listhead'><div>팩트 항목</div><div>카테고리</div>"
                  "<div>맞은 주제</div><div class='num'>그 주제로 쓴 글</div></div>")
        b_rows = []
        for f in facts[:FACT_GAP_TOP]:
            if f["matched"]:
                c3 = (f"주제 ‘<a href='{list_href(topic=f['topic'])}'>{esc(f['topic'])}</a>’")
                c4 = f"<div class='num'>{_comma(f['n'])}편</div>"
            else:
                c3 = "이름이 안 맞음 — 직접 찾아보세요"
                c4 = "<div class='num'><a href='/'>글 목록에서 찾기 →</a></div>"
            b_rows.append(
                "<div class='listrow static'>"
                f"<div><a class='r-title' href='/fact?id={f['fact_id']}'>{esc(f['name'])}</a> "
                f"{fact_badge(f['status'])}</div>"
                f"<div>{esc(f['cat'])}</div>"
                f"<div>{c3}</div>{c4}</div>")
        secB = ("<h2 class='sec'>팩트 항목 이름, 우리 글 주제와 맞나</h2>"
                f"<p class='intro sub'>룰북에 정리해 둔 팩트 항목 {_comma(len(facts))}개의 이름을 "
                "우리 글 주제 이름과 그대로 맞춰 본 것입니다 — "
                f"<b>맞은 것 {_comma(n_hit)}개 · 안 맞은 것 {_comma(len(facts) - n_hit)}개</b>. "
                "안 맞은 것을 먼저, 그다음 글이 적은 것부터 "
                f"{_comma(min(len(facts), FACT_GAP_TOP))}개가 보입니다.</p>"
                "<div class='honest'>이 표가 아는 것은 <b>이름이 맞았는지</b>뿐이고, 그 주제로 글을 "
                "썼는지 안 썼는지가 아닙니다. 이름이 안 맞아도 <b>다른 말로 이미 쓴 글이 있을 수 "
                "있어요</b> — 그럴 땐 글 목록에서 직접 찾아보고 판단해 주세요.<br>"
                "이름이 맞은 줄의 숫자는 <b>그 주제 전체의 글 수</b>입니다. 그래서 이름이 다른 "
                "항목이라도 같은 주제에 붙으면 같은 숫자가 나옵니다.<br>"
                f"룰북 팩트는 AI가 만든 초안이라 아직 사람이 확인하지 않았습니다 — "
                f"{_comma(len(facts))}건 중 <b>{_comma(n_un)}건이 ‘미확인’</b>입니다. "
                "여기 이름을 보고 원고를 쓰기 전에 팩트 화면에서 내용을 먼저 확인하세요.</div>"
                f"<div class='an13'><div class='tablewrap'>{b_head}{''.join(b_rows)}</div></div>")
    else:
        secB = ("<h2 class='sec'>팩트 항목 이름, 우리 글 주제와 맞나</h2>"
                "<div class='state'>룰북 팩트가 아직 창고에 없습니다. 팩트를 넣으면 여기서 "
                "팩트 항목 이름이 우리 글 주제와 맞는지 볼 수 있어요."
                "<div style='margin-top:12px'><a href='/facts'>팩트 룰북 →</a></div></div>")

    # --- 섹션 C: 담당자별 우리 글(쓴 글 + 평균 조회수) ---
    c_head = ("<div class='listhead'><div>담당자</div><div class='num'>쓴 글</div>"
              "<div class='num'>조회수 있는 글</div><div class='num'>평균 조회수</div></div>")
    c_rows = "".join(
        "<div class='listrow static'>"
        f"<div>{staff_link(s['staff'])}</div>"
        f"<div class='num'>{_comma(s['n'])}</div>"
        f"<div class='num{'' if s['nv'] else ' num-dim'}'>{_comma(s['nv'])}</div>"
        + (f"<div class='num'>{_comma(round(s['avg']))}</div>" if s["avg"] is not None
           else "<div class='num num-dim'>-</div>")
        + "</div>"
        for s in srows)
    secC = ("<h2 class='sec'>담당자별 우리 글<span class='secsub'>쓴 글 많은 순</span></h2>"
            "<p class='intro sub'>담당자 이름을 누르면 그 담당자가 쓴 글 목록이 열립니다. "
            "‘조회수 있는 글’은 우리가 카페에서 직접 가져와 조회수를 확보한 글이고, "
            "‘평균 조회수’는 그 글들만으로 낸 참고 신호예요 — 사람을 견주는 "
            "숫자가 아닙니다.</p>"
            f"<div class='an13'><div class='tablewrap'>{c_head}{c_rows}</div></div>"
            "<p class='intro sub'>위 세 표(주제별·팩트 항목·담당자별)는 아래 접기 안의 정렬·기간을 "
            "바꿔도 달라지지 않습니다 — 언제나 창고 전체 기준이에요.</p>")

    upper = f"<div class='wrap'>{intro}{honest_top}{secA}" \
            f"<div style='margin-top:32px'>{secB}</div>" \
            f"<div style='margin-top:32px'>{secC}</div>"
    fold_open = " open" if (sort != "views" or min_age) else ""
    fold_head = ("<details class='foldsec'" + fold_open + ">"
                 "<summary>▸ 조회수로 본 참고 신호 — 결론: 문단 수·이미지 수·글자 수로는 "
                 "많이 본 글과 아닌 글이 갈리지 않았습니다. 조회수 표는 여기 접어 두었어요."
                 "</summary>")

    # 접기 안 — 조회수가 없으면 표 대신 안내만(위쪽 세 섹션은 그대로 보인다)
    if n_target == 0:
        return page(ANALYSIS_TITLE, topbar,
                    upper + fold_head
                    + "<div class='state'>조회수를 확보한 글이 아직 없습니다. "
                      "글을 추출하면 여기 표가 생깁니다. 위쪽 표는 조회수 없이도 볼 수 있어요."
                      "</div></details></div>")

    # 30일+ 필터: 켜면 경과일<30(또는 작성일 불명) 글 제외
    used = [r for r in recs if r["dg"] is not None and r["dg"] >= 30] if min_age else recs

    # --- 조절 바(주소 링크로만, JS 없음) ---
    controls = (
        "<div class='filters'>정렬: "
        f"<a href='{alink(sort='views')}'{on(sort=='views')}>조회수 순</a> · "
        f"<a href='{alink(sort='vpd')}'{on(sort=='vpd')}>하루당 조회수 순</a>"
        "&nbsp;&nbsp;|&nbsp;&nbsp;기간: "
        f"<a href='{alink(min_age=False)}'{on(not min_age)}>전체</a> · "
        f"<a href='{alink(min_age=True)}'{on(min_age)}>올린 지 30일 지난 글만</a>"
        "<div class='note'>하루당 조회수는 최근에 올린 글이 높게 나오는 경향이 있어요"
        "(조회가 초반에 몰림). 오래된 글과 견줄 땐 '조회수 순'도 함께 보세요.</div></div>")

    # 30일+ 필터가 전부 걸러낸 경우(빈 집합) — 평균 계산 크래시 방지, 안내만
    if not used:
        return page(ANALYSIS_TITLE, topbar,
                    upper + fold_head + controls
                    + "<div class='state'>고른 조건(올린 지 30일 지난 글)에 맞는 글이 없습니다. "
                      "‘전체’로 바꾸면 모든 글을 볼 수 있어요.</div></details></div>")

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
                f"<div>{topic_link(it['topic'])}</div>"
                f"<div class='num'>{_comma(it['published'])}</div>"
                f"<div class='num'>{it['extracted']}</div>"
                f"<div class='num'>{_comma(round(it['avg_views']))}</div>"
                f"<div class='num'>{_comma(it['sum_views'])}</div>"
                f"<div class='num'>{avpd}</div></div>")
        sect = ("<h2 class='sec'>주제별 조회수 <span class='secsub'>변형 키워드를 하나의 주제로 "
                "묶어 봄 · 추출글 2건+ · 평균 조회수 높은 순</span></h2>"
                "<p class='intro sub'>‘발행 글수’는 우리가 그 주제로 쓴 전체 글, ‘추출·조회 글수’는 "
                "그중 조회수를 확보한 글입니다. <b>발행은 적은데 평균 조회수가 높은 주제</b>가 "
                "‘더 써볼 후보’예요. (조회수는 참고 신호 — 자사 채널에서 재검증 필요)<br>"
                "주제 이름을 누르면 그 주제로 쓴 우리 글 목록이 열립니다.</p>"
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

    # 담당자별 평균 조회수는 접기 안으로 내리지 않고 섹션 C(담당자별 우리 글)에 열로 살아 있다
    # (사용자 결정 2026-07-21: 펼쳐 둔다). 같은 표를 두 번 그리지 않으려고 여기서는 다시 만들지 않는다.

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

    body = (upper + fold_head + controls + sec1
            + f"<div style='margin-top:32px'>{sect}</div>"
            + f"<div style='margin-top:32px'>{sec2}</div>"
            + f"<div style='margin-top:32px'>{sec4}</div></details></div>")
    return page(ANALYSIS_TITLE, topbar, body)


# ---------------------------------------------------------------------------
# 화면 D — 주제·시기 트렌드 (전체 글의 작성일 기준, 읽기 전용)
# ---------------------------------------------------------------------------
# 히트맵 범위 — 화면에 적는 설명과 실제 집계가 어긋나지 않도록 여기서 한 번 정하고 둘 다 이 값을 쓴다.
HEAT_TOP_N, HEAT_MONTHS, HEAT_MIN_MONTH = 15, 12, 30
SEAS_MIN_TOPIC = 40   # 계절성·월초중순말 표에 넣을 최소 글 수(trends 기본값과 같은 값을 화면에도 적는다)


def render_trends(conn):
    """월별 비중 히트맵(뜨는/식는 주제) + 월별 계절성 + 월초·중순·말 분포.
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
        "원시 건수는 대부분 증가하기 때문. 비중을 보면 ‘상대적으로’ 뜨는/식는 주제만 남습니다.</p>"
        "<p class='intro sub'>이 화면의 <b>주제 이름을 누르면 그 주제로 쓴 우리 글 목록</b>이 "
        "열립니다. 돌아올 때는 브라우저 뒤로 가기를 쓰세요.</p>")

    # --- 월별 비중 히트맵 (분기 '시작 vs 최근' 기울기의 함정 대체) ---
    hm = trends.monthly_share_heatmap(recs, top_n=HEAT_TOP_N, months_back=HEAT_MONTHS,
                                      min_month_total=HEAT_MIN_MONTH)
    if hm["months"] and hm["rows"]:
        mx = hm["max_share"] or 1.0

        def hm_num(sh):
            """칸에 찍는 숫자 문자열(범례 설명도 같은 규칙을 써서 표와 어긋나지 않게)."""
            return f"{sh:.1f}" if sh < 10 else f"{sh:.0f}"

        def hm_cell(sh):
            """한 칸 — 배경 농도 = 그 달 몫. 0은 빈 칸이 아니라 가운뎃점(데이터 없음과 구분)."""
            alpha = min(sh / mx, 1.0) * 0.92
            zero = " zero" if sh <= 0 else ""
            txt = "·" if sh <= 0 else hm_num(sh)
            return (f"<div class='hm-c{zero}' "
                    f"style='background:rgba(var(--heat),{alpha:.2f})'>{txt}</div>")

        gcols = f"grid-template-columns:150px repeat({len(hm['months'])},minmax(34px,1fr));"
        hm_head = (f"<div class='hm-row hm-head' style='{gcols}'><div class='hm-t'>주제</div>"
                   + "".join(f"<div class='hm-mh'>{esc(m[2:4])}.{esc(m[5:7])}</div>"
                             for m in hm["months"]) + "</div>")
        hm_rows = []
        for r in hm["rows"]:
            cells = "".join(hm_cell(sh) for sh in r["cells"])
            hm_rows.append(
                f"<div class='hm-row' style='{gcols}'>"
                f"<div class='hm-t'>{topic_link(r['topic'])}"
                f"<span class='hm-tot'>{_comma(r['total'])}</span></div>"
                f"{cells}</div>")
        # 범례 — 색만으로 구분되지 않게 실제 몫(%)을 숫자로 함께
        legend = ("<div class='hm-legend'><span>옅을수록 적게 쓴 달 →</span>"
                  + "".join(hm_cell(mx * f) for f in (0.0, 0.25, 0.5, 0.75, 1.0))
                  + f"<span>← 진할수록 많이 쓴 달 (이 표에서 가장 큰 몫 {hm_num(mx)}%)</span></div>")
        span = (f"{hm['months'][0][:4]}년 {int(hm['months'][0][5:7])}월"
                f"~{hm['months'][-1][:4]}년 {int(hm['months'][-1][5:7])}월")
        sec_h = ("<h2 class='sec'>월별 비중 히트맵<span class='secsub'>주제(행)×월(열)</span></h2>"
                 "<p class='intro'>칸 안의 숫자는 <b>그 달에 우리가 올린 글 전체 중 이 주제가 "
                 "차지한 몫(%)</b>입니다. <b>순위가 아닙니다.</b> 예를 들어 ‘5.0’은 그 달 우리 글 "
                 "100건 중 5건이 이 주제였다는 뜻이고, ‘·’는 그 달에 이 주제 글이 한 건도 "
                 "없었다는 뜻입니다.</p>"
                 f"<p class='intro sub'>지금 보고 있는 범위: 글이 많은 <b>주제 {len(hm['rows'])}개</b>"
                 f"(글 수 많은 순 상위 {HEAT_TOP_N}개까지) × <b>최근 {len(hm['months'])}개월</b>"
                 f"({span}). 전체 글이 {HEAT_MIN_MONTH}건이 안 되는 달은 몫이 크게 튀어서 뺐습니다. "
                 f"주제 이름 옆 작은 숫자는 그 주제의 전체 글 수예요.</p>"
                 f"{legend}"
                 "<p class='intro sub'>매 달의 몫을 그대로 보여줍니다 — ‘시작 대비 최근’ 방식은 "
                 "뒤늦게 생긴 주제가 늘 상승처럼 보이는 함정이 있어, 달별 색으로 추세를 직접 보게 했어요. "
                 "왼→오른쪽으로 <b>색이 짙어지면 뜨는 주제, 옅어지면 식는 주제</b>입니다.</p>"
                 f"<div class='tablewrap'><div class='heatmap'>{hm_head}{''.join(hm_rows)}</div></div>")
    else:
        sec_h = ("<h2 class='sec'>월별 비중 히트맵</h2><div class='state'>"
                 "히트맵에 쓸 월별 물량이 아직 부족합니다.</div>")

    # --- 월별 계절성 ---
    seas = trends.seasonality(recs, min_topic=SEAS_MIN_TOPIC)
    if seas:
        s_head = ("<div class='listhead'><div>주제</div><div class='num'>총 글수</div>"
                  "<div class='num'>가장 많이 쓴 달</div><div class='num'>그 달 비중</div></div>")
        s_rows = "".join(
            "<div class='listrow static'>"
            f"<div>{topic_link(it['topic'])}</div><div class='num'>{_comma(it['total'])}</div>"
            f"<div class='num'>{it['peak_month']}월</div>"
            f"<div class='num'>{it['peak_pct']:.0f}%</div></div>"
            for it in seas)
        sec_s = ("<h2 class='sec'>월별 계절성<span class='secsub'>특정 달에 쏠린 주제 · "
                 "쏠림 큰 순</span></h2>"
                 "<p class='intro'>‘그 달 비중’은 <b>그 주제로 쓴 글 전체 중 그 달에 쓴 몫(%)</b>"
                 "입니다(순위 아님). 12달에 고르게 썼다면 8%쯤이니, 이보다 크면 그 달에 몰아 쓴 "
                 "것입니다.</p>"
                 f"<p class='intro sub'>글이 {SEAS_MIN_TOPIC}건 이상인 주제만, 쏠림 큰 순으로 "
                 f"{len(seas)}개 보여줍니다(연도 구분 없이 1~12월로 합산). "
                 "자격증은 시험·접수 일정이 있어 특정 달에 발행이 몰립니다. "
                 "수요 정점보다 앞서 쓰려면 이 달 <b>한두 달 전</b>이 후보예요.</p>"
                 f"<div class='an7'><div class='tablewrap'>{s_head}{s_rows}</div></div>")
    else:
        sec_s = ""

    # --- 월초/중순/말 ---
    im = trends.intramonth(recs, min_topic=SEAS_MIN_TOPIC)
    base = im["baseline"]

    def dom_table(items):
        head = ("<div class='listhead'><div>주제</div><div class='num'>월초(1-10)</div>"
                "<div class='num'>중순(11-20)</div><div class='num'>월말(21-31)</div>"
                "<div class='num'>총 글수</div></div>")
        rows = "".join(
            "<div class='listrow static'>"
            f"<div>{topic_link(it['topic'])}</div>"
            f"<div class='num'>{it['early_pct']:.0f}%</div>"
            f"<div class='num'>{it['mid_pct']:.0f}%</div>"
            f"<div class='num'>{it['late_pct']:.0f}%</div>"
            f"<div class='num'>{_comma(it['total'])}</div></div>"
            for it in items)
        return f"<div class='an8'><div class='tablewrap'>{head}{rows}</div></div>"

    sec_d = ("<h2 class='sec'>월초·중순·말 분포<span class='secsub'>주제를 월내 어느 시기에 "
             "발행했나</span></h2>"
             "<p class='intro'>표의 %는 <b>그 주제로 쓴 글 전체 중 월초·중순·월말에 각각 쓴 "
             "몫</b>입니다(순위 아님). 한 줄의 세 숫자를 더하면 100%가 됩니다.</p>"
             f"<p class='intro sub'>전체 기준선: 월초 {base[0]:.0f}% · 중순 {base[1]:.0f}% · "
             f"월말 {base[2]:.0f}%. 아래는 글이 {SEAS_MIN_TOPIC}건 이상인 주제 중 기준선보다 "
             "한쪽으로 치우친 주제입니다.</p>"
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
        n_fact = one("SELECT COUNT(*) FROM rulebook_facts")
        n_fact_todo = one("SELECT COUNT(*) FROM rulebook_facts WHERE review_status='미확인'")
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
                + card(n_fact, "팩트")
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
                f"<p class='intro sub'>‘팩트’(학점·응시자격 등 제도 수치) {_comma(n_fact)}건이 "
                f"창고에 있고, 그중 {_comma(n_fact_todo)}건이 아직 확인 전입니다 — "
                "AI가 만든 초안이라 사람이 하나씩 확인해야 원고에 쓸 수 있습니다. "
                "<a href='/facts'>팩트 룰북에서 확인하기 →</a></p>")

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
    members = topic_members(conn)   # 글 목록 화면과 같은 함수 → 같은 숫자

    def member_str(topic, top=6):
        ms = sorted(members.get(topic, []), key=lambda x: -x[1])
        shown = ", ".join(f"{esc(k)}({n})" for k, n, _ in ms[:top])
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
# 화면 G — 팩트 룰북 (목록 /facts · 항목 상세 /fact?id=N · 저장 POST /fact/save)
#   ★ 뷰어에서 유일하게 창고에 쓰는 화면이다(4차). 다른 화면은 전부 읽기 전용.
#   ★ 불변 1: 화면에서 저장하는 값도 masking.mask_text를 거쳐 '가려진 값'으로만 저장한다.
#     화면 출력은 esc()만 통과시킨다.
#   ★ 데이터 손실 방지: 모든 수정은 rulebook_fact_edits에 남고(이전 값 보존) 칸마다 되돌리기가 있다.
#     항목 삭제 기능은 만들지 않는다.
# ---------------------------------------------------------------------------
# 칸 이름은 엑셀·화면명세 §4 문구 그대로. 순서는 D-B — '주의메모'를 요건(개별=핵심 팩트) 바로
# 아래로 끌어올린다(이번 모순이 요건↔주의메모↔FAQ 사이에서 났다).
FACT_FIELDS_BY_KIND = {
    "공통": [("requirement", "응시/취득 요건"),
             ("caution_memo", "주의메모 (시점/예외)"),
             ("credits", "필요 학점"),
             ("duration", "예상 소요 기간"),
             ("shortcut", "기간 단축 방법"),
             ("faq_top3", "자주 묻는 질문 TOP3"),
             ("cautions", "주의사항 / 흔한 오해")],
    "개별": [("core_fact", "핵심 팩트"),
             ("caution_memo", "주의메모"),
             ("path_by_education", "학력별 경로 요약"),
             ("emphasis", "글 작성 시 강조포인트"),
             ("use_priority", "사용 우선순위"),
             ("remarks", "비고")],
}
# 주소의 필터값(영문) → 창고의 상태·종류(한글). 정해진 목록 밖은 '전체'로 떨어진다(입력검증).
FACT_VIEWS = {"unreviewed": "미확인", "reviewed": "확인함", "hold": "보류"}
FACT_KINDS = {"common": "공통", "individual": "개별"}
# 목록 순서(D-A): 공통 먼저 → 개별, 카테고리로 묶고 그 안은 항목명순
FACT_ORDER = ("ORDER BY CASE fact_kind WHEN '공통' THEN 0 ELSE 1 END, "
              "COALESCE(category,''), item_name, fact_id")

# ── 4차(쓰기) — 입력검증에 쓰는 정해진 목록 ──────────────────────────────────
# 고칠 수 있는 칸은 아래 합집합 안에서만(주소·보내온 값으로 다른 칸을 건드릴 수 없다).
FACT_ALL_FIELDS = {c: label for fs in FACT_FIELDS_BY_KIND.values() for c, label in fs}
FACT_STATUSES = ("확인함", "보류", "미확인")   # D3 — 상태 3종
FIELD_STATUS = "review_status"                # 상태 변경도 이력에 남긴다(창고 칸 이름 그대로)
FIELD_NOTE = "review_note"                    # 검수 메모도 덮어쓰기 전 값을 이력에 남긴다
FACT_HISTORY_LABEL = {FIELD_STATUS: "상태", FIELD_NOTE: "검수 메모"}
FACT_VALUE_MAX = 5000        # 한 칸 글자 수 상한
FACT_POST_MAX = 256 * 1024   # 한 번에 받는 내용 전체 크기 상한(그보다 크면 읽지 않는다)
FACT_SAVE_PATH = "/fact/save"


def fact_origins(conn, fact_id=None):
    """칸별 '엑셀에서 온 원래 값' = 그 칸의 가장 이른 이력의 이전 값. {(항목번호, 칸): 원래값}"""
    sql = ("SELECT fact_id, field_name, old_value FROM rulebook_fact_edits "
           "WHERE edit_id IN (SELECT MIN(edit_id) FROM rulebook_fact_edits "
           "                  GROUP BY fact_id, field_name)")
    args = ()
    if fact_id is not None:
        sql += " AND fact_id=?"
        args = (fact_id,)
    return {(r["fact_id"], r["field_name"]): r["old_value"] for r in conn.execute(sql, args)}


def fact_save_field(conn, fact_id, field, value):
    """한 칸 저장 — 이전 값을 이력에 남기고(되돌리기 재료) 새 값으로 바꾼다.

    ★ value는 이미 가려진 값이어야 한다(불변 1은 부르는 쪽 do_POST에서 통과시킨다).
    바뀐 게 없으면 아무것도 쓰지 않는다(빈 이력이 쌓이지 않게).
    """
    # 자체검증: 정해진 칸 밖의 이름이 오면 즉시 실패(칸 이름이 SQL에 들어가는 유일한 자리)
    assert field in FACT_ALL_FIELDS or field == FIELD_NOTE, "정해진 칸 밖 — 저장하지 않는다"
    row = conn.execute("SELECT * FROM rulebook_facts WHERE fact_id=?", (fact_id,)).fetchone()
    if row is None:
        return False
    old = row[field]
    if (old or "") == (value or ""):
        return False
    conn.execute(f"UPDATE rulebook_facts SET {field}=?, "
                 "updated_at=datetime('now','localtime') WHERE fact_id=?", (value, fact_id))
    conn.execute("INSERT INTO rulebook_fact_edits (fact_id, field_name, old_value, new_value) "
                 "VALUES (?,?,?,?)", (fact_id, field, old, value))
    conn.commit()
    return True


def fact_stamp(conn, fact_id, status, note=None, set_note=True):
    """도장(D2 — 항목 통째). 확인 날짜는 '미확인'으로 되돌리면 비운다. 상태·메모 변경도 이력에."""
    assert status in FACT_STATUSES, "정해진 상태 밖 — 저장하지 않는다"   # 자체검증
    row = conn.execute("SELECT * FROM rulebook_facts WHERE fact_id=?", (fact_id,)).fetchone()
    if row is None:
        return False
    old = row[FIELD_STATUS]
    at = None if status == "미확인" else datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if set_note and (row[FIELD_NOTE] or "") != (note or ""):
        conn.execute("INSERT INTO rulebook_fact_edits (fact_id, field_name, old_value, new_value) "
                     "VALUES (?,?,?,?)", (fact_id, FIELD_NOTE, row[FIELD_NOTE], note or None))
        conn.execute("UPDATE rulebook_facts SET review_note=? WHERE fact_id=?",
                     (note or None, fact_id))
    if old != status:
        conn.execute("INSERT INTO rulebook_fact_edits (fact_id, field_name, old_value, new_value) "
                     "VALUES (?,?,?,?)", (fact_id, FIELD_STATUS, old, status))
    conn.execute("UPDATE rulebook_facts SET review_status=?, reviewed_at=?, "
                 "updated_at=datetime('now','localtime') WHERE fact_id=?", (status, at, fact_id))
    conn.commit()
    return True


def fact_undo(conn, fact_id, field):
    """되돌리기 — 그 칸의 가장 최근 수정을 이전 값으로. 되돌린 것도 이력에 남는다
    (그래서 '되돌리기의 되돌리기'가 되고, 확인 대화상자가 필요 없다)."""
    last = conn.execute(
        "SELECT old_value FROM rulebook_fact_edits WHERE fact_id=? AND field_name=? "
        "ORDER BY edit_id DESC LIMIT 1", (fact_id, field)).fetchone()
    if last is None:
        return False
    if field == FIELD_STATUS:
        return fact_stamp(conn, fact_id, last["old_value"] or "미확인", set_note=False)
    return fact_save_field(conn, fact_id, field, last["old_value"])


def fact_badge(status):
    """상태 배지 — 색만이 아니라 글자로도 구분. 미확인은 회색(잘못이 아니라 '아직 안 봄')."""
    cls = {"확인함": "ok", "보류": "warn"}.get(status, "dim")
    label = "확인함 ✓" if status == "확인함" else (status or "미확인")
    return f"<span class='badge {cls}'>{esc(label)}</span>"


def fact_rows(conn):
    """목록·다음 항목 찾기가 함께 쓰는 한 벌(정렬 포함).

    '고친 칸' = 지금 값이 엑셀에서 온 원래 값과 다른 칸 수. 되돌리면 다시 0이 된다
    (이력 건수로 세면 되돌린 뒤에도 '고침'이 남아 목록이 사람을 속인다).
    항목 수십 건 규모라 파이썬에서 비교한다.
    """
    rows = conn.execute("SELECT * FROM rulebook_facts " + FACT_ORDER).fetchall()
    origins = fact_origins(conn)
    out = []
    for r in rows:
        d = dict(r)
        d["edited_n"] = sum(
            1 for (fid, fld), orig in origins.items()
            if fid == r["fact_id"] and fld in FACT_ALL_FIELDS and (r[fld] or "") != (orig or ""))
        out.append(d)
    return out


def _fact_progress(rows):
    n_ok = sum(1 for r in rows if r["review_status"] == "확인함")
    return f"<span class='badge ok'>{_comma(len(rows))}건 중 {_comma(n_ok)}건 확인함</span>"


def _fact_load_failed(menu, title):
    topbar = f"<div class='topbar'>{menu}<span class='t-title'>{esc(title)}</span></div>"
    body = ("<div class='wrap'><div class='state'>팩트를 불러오지 못했습니다. "
            "자산창고 파일을 먼저 만든 뒤 다시 열어주세요.</div></div>")
    return page(title, topbar, body)


def render_facts(conn, view="all", kind="all"):
    """팩트 목록 — 51건을 끝까지 훑는 시작점. 읽기 전용."""
    menu = nav_menu("facts")
    # 입력검증: 쿼리값을 링크에 되비추므로 정해진 목록으로만 좁힌다(주입 차단)
    view = view if view in FACT_VIEWS else "all"
    kind = kind if kind in FACT_KINDS else "all"
    try:
        rows = fact_rows(conn)
    except sqlite3.Error:
        return _fact_load_failed(menu, "팩트 룰북")

    topbar = (f"<div class='topbar'>{menu}<span class='t-title'>팩트 룰북</span>"
              f"{_fact_progress(rows)}</div>")
    if not rows:
        body = ("<div class='wrap'><div class='state'>아직 팩트가 창고에 없습니다. "
                "룰북 엑셀의 ‘② 팩트 룰북’ 시트를 창고에 넣는 작업(적재)을 먼저 해야 합니다. "
                "화면에서는 넣을 수 없어요.</div></div>")
        return page("팩트 룰북", topbar, body)

    n_total = len(rows)
    n_ok = sum(1 for r in rows if r["review_status"] == "확인함")
    n_hold = sum(1 for r in rows if r["review_status"] == "보류")
    n_un = n_total - n_ok - n_hold

    intro = ("<p class='intro'>원고에 들어갈 사실을 사람이 하나씩 확인하는 화면입니다.</p>"
             "<p class='intro sub'>여기서 확인·수정한 내용이 최종본입니다. 엑셀은 새 팩트를 넣는 "
             "입구이고, 결과는 언제든 엑셀로 내보낼 수 있어요.</p>")

    def card(n, label):
        return (f"<div class='statcard'><div class='n'>{_comma(n)}</div>"
                f"<div class='l'>{esc(label)}</div></div>")
    cards = ("<div class='statcards'>" + card(n_total, "전체 항목") + card(n_ok, "확인함")
             + card(n_hold, "보류") + card(n_un, "미확인") + "</div>")

    def href(v, k):
        q = [f"{n}={x}" for n, x in (("view", v), ("kind", k)) if x != "all"]
        return "/facts?" + "&".join(q) if q else "/facts"

    def on(cond):
        return " class='on'" if cond else ""
    filters = ("<div class='filters'>보기: "
               f"<a href='{href('all', kind)}'{on(view == 'all')}>전체</a> · "
               f"<a href='{href('unreviewed', kind)}'{on(view == 'unreviewed')}>미확인만</a> · "
               f"<a href='{href('reviewed', kind)}'{on(view == 'reviewed')}>확인함</a> · "
               f"<a href='{href('hold', kind)}'{on(view == 'hold')}>보류</a>"
               "<br>종류: "
               f"<a href='{href(view, 'all')}'{on(kind == 'all')}>전체</a> · "
               f"<a href='{href(view, 'common')}'{on(kind == 'common')}>공통</a> · "
               f"<a href='{href(view, 'individual')}'{on(kind == 'individual')}>개별</a></div>")

    shown = [r for r in rows
             if (view == "all" or r["review_status"] == FACT_VIEWS[view])
             and (kind == "all" or r["fact_kind"] == FACT_KINDS[kind])]

    head = ("<div class='listhead'><div>항목명</div><div>종류</div><div>카테고리</div>"
            "<div>상태</div><div class='num'>고친 칸</div><div>확인 날짜</div></div>")
    if shown:
        body_rows = "".join(
            f"<a class='listrow' href='/fact?id={r['fact_id']}'>"
            f"<div class='r-title'>{esc(r['item_name'])}</div>"
            f"<div>{esc(r['fact_kind'])}</div>"
            f"<div>{esc(r['category'] or '-')}</div>"
            f"<div>{fact_badge(r['review_status'])}</div>"
            + (f"<div class='num'>{r['edited_n']}</div>" if r["edited_n"]
               else "<div class='num num-dim'>–</div>")
            + f"<div>{esc((r['reviewed_at'] or '')[5:10] or '–')}</div></a>"
            for r in shown)
        table = f"<div class='an11'><div class='tablewrap'>{head}{body_rows}</div></div>"
    else:
        table = ("<div class='state'>이 보기에 해당하는 항목이 없습니다. "
                 "위에서 ‘전체’를 눌러 보세요.</div>")

    honest = (f"<div class='honest'>{_comma(n_total)}건 전부 ‘미확인’에서 시작합니다 — AI가 만든 "
              "초안이라 아직 아무도 끝까지 확인하지 않았다는 뜻입니다. 한 항목을 통째로 읽고 "
              "도장을 찍어주세요.</div>")
    body = f"<div class='wrap'>{intro}{cards}{filters}{table}{honest}</div>"
    return page("팩트 룰북", topbar, body)


def render_fact_not_found():
    topbar = ("<div class='topbar'><a href='/facts'>← 팩트 목록</a>"
              "<span class='t-title'>팩트 항목을 찾을 수 없음</span></div>")
    body = ("<div class='wrap'><div class='state'>그런 팩트 항목이 없습니다."
            "<div style='margin-top:12px'><a href='/facts'>← 팩트 목록</a></div></div></div>")
    return page("팩트 항목을 찾을 수 없음", topbar, body)


def render_fact_denied():
    """다른 사이트가 몰래 시킨 저장 등 — 처리하지 않았음을 알린다."""
    topbar = ("<div class='topbar'><a href='/facts'>← 팩트 목록</a>"
              "<span class='t-title'>처리하지 않았습니다</span></div>")
    body = ("<div class='wrap'><div class='state'>이 요청은 처리하지 않았습니다. "
            "팩트 룰북 화면에서 직접 눌러주세요."
            "<div style='margin-top:12px'><a href='/facts'>← 팩트 목록</a></div></div></div>")
    return page("처리하지 않았습니다", topbar, body)


def _fact_form(fact_id, action, field=None, inner=""):
    hidden = (f"<input type='hidden' name='action' value='{action}'>"
              f"<input type='hidden' name='id' value='{int(fact_id)}'>")
    if field:
        hidden += f"<input type='hidden' name='field' value='{esc(field)}'>"
    return (f"<form class='fedit' method='post' action='{FACT_SAVE_PATH}'>{hidden}{inner}</form>")


def _fact_undo_btn(fact_id, field):
    return _fact_form(fact_id, "undo", field,
                      "<button class='btn' type='submit'>되돌리기</button>")


def _fact_stampbar(row, draft_note=None):
    """하단 고정 도장 바 — 상태 3개 + 검수 메모(선택). 확인 대화상자 없음(§3-5)."""
    cur = row["review_status"]
    btns = "".join(
        f"<button class='btn{' on' if s == cur else ''}' type='submit' "
        f"name='status' value='{s}'>{label}</button>"
        for s, label in (("확인함", "확인함"), ("보류", "보류"), ("미확인", "미확인으로")))
    note = draft_note if draft_note is not None else (row["review_note"] or "")
    inner = ("<span>이 항목을 다 읽으셨나요?</span>" + btns
             + "<details><summary>검수 메모(선택)</summary>"
             "<p class='help'>왜 보류인지 적어두면 다음에 볼 때 도움이 됩니다 — "
             "예: 2020년 기준이 맞는지 협회에 확인 필요.</p>"
             "<label for='note'>검수 메모</label>"
             f"<textarea id='note' name='note' rows='2'>{esc(note)}</textarea>"
             "<p class='help'>메모는 [확인함]·[보류]·[미확인으로] 중 하나를 눌러야 함께 "
             "저장됩니다.</p></details>")
    return f"<div class='stampbar'>{_fact_form(row['fact_id'], 'stamp', None, inner)}</div>"


def render_fact(conn, id_raw, edit=None, done=None, f=None, s=None,
                draft=None, error=None, draft_note=None):
    """항목 상세 — 한 항목의 모든 칸을 한 열로 펼친다(칸끼리 어긋나는 곳을 사람 눈으로 찾는 화면).

    edit: 지금 편집 상태인 칸 이름(나머지 칸은 그대로 보인다 — 보면서 고치는 게 이 작업의 본질).
    done/f/s: 방금 한 저장의 결과 알림(303 재이동 뒤 주소로 넘어온다).
    draft/error/draft_note: 저장 실패 시 — 고치던 내용을 그대로 되돌려 그린다(입력 내용 안 날림).
    """
    try:
        fact_id = int(id_raw)   # 입력검증: 정수만
    except (TypeError, ValueError):
        return render_fact_not_found()
    try:
        row = conn.execute("SELECT * FROM rulebook_facts WHERE fact_id=?", (fact_id,)).fetchone()
        if row is None:
            return render_fact_not_found()
        all_rows = fact_rows(conn)
        edits = conn.execute(
            "SELECT field_name, edited_at FROM rulebook_fact_edits "
            "WHERE fact_id=? ORDER BY edit_id DESC", (fact_id,)).fetchall()
        origins = fact_origins(conn, fact_id)
    except sqlite3.Error:
        return _fact_load_failed(nav_menu("facts"), "팩트 룰북")

    kind = row["fact_kind"]
    fields = FACT_FIELDS_BY_KIND[kind]   # fact_kind는 창고에서 '공통'/'개별'로 제한됨
    labels = dict(fields)
    # 입력검증: 편집할 칸은 이 항목의 정해진 칸 목록 안에서만(주소로 다른 칸을 열 수 없다)
    edit = edit if edit in labels else None

    # 다음 미확인 → (D-C: 자동 이동은 안 하고 링크만) — 목록과 같은 순서에서 나보다 뒤인 첫 미확인
    order = [r["fact_id"] for r in all_rows]
    pos = order.index(fact_id) if fact_id in order else -1
    nxt = next((r for r in all_rows[pos + 1:] if r["review_status"] == "미확인"), None)
    next_link = (f"<a href='/fact?id={nxt['fact_id']}'>다음 미확인 →</a>" if nxt else "")

    topbar = ("<div class='topbar'><a href='/facts'>← 팩트 목록</a>"
              f"<span class='t-title'>{esc(row['item_name'])}</span>"
              f"{fact_badge(row['review_status'])}{_fact_progress(all_rows)}{next_link}</div>")

    meta_bits = [f"{esc(kind)} 팩트"]
    if row["division"]:      # 엑셀 '구분' — 아래 칸 카드에 없으므로 여기서 보여준다
        meta_bits.append(f"구분 {esc(row['division'])}")
    if row["category"]:
        meta_bits.append(f"카테고리 {esc(row['category'])}")
    if row["excel_no"]:
        meta_bits.append(f"엑셀 {row['excel_no']}행")
    if row["updated_at"]:
        meta_bits.append(f"마지막 수정 {esc(row['updated_at'][:16])}")
    if row["review_note"]:
        meta_bits.append(f"검수 메모 {esc(row['review_note'])}")

    panels = []
    for col, label in fields:
        origin = origins.get((fact_id, col))
        changed = origin is not None and (row[col] or "") != (origin or "")
        chip = " <span class='chip mark'>고침</span>" if changed else ""
        if col == edit:
            # 이 칸만 입력 상자로. 저장 실패면 고치던 내용(draft)을 그대로 되돌려 넣는다.
            val = draft if draft is not None else (row[col] or "")
            err = f"<div class='flash bad'>{esc(error)}</div>" if error else ""
            inner = err + _fact_form(
                fact_id, "edit", col,
                f"<label for='v'>{esc(label)}</label>"
                f"<textarea id='v' name='value' rows='8'>{esc(val)}</textarea>"
                "<p class='help'>여기 적은 문장이 그대로 원고에 들어갑니다. 학점·기간 같은 "
                "수치는 조건(시점·학력)까지 함께 적어주세요.</p>"
                "<button class='btn go' type='submit'>저장</button> "
                f"<a class='btn' href='/fact?id={fact_id}'>취소</a>")
        else:
            val = row[col]
            inner = (f"<div class='ptext'>{esc(val)}</div>" if val and val.strip()
                     else "<p class='note-empty'>이 칸은 비어 있습니다.</p>")
            inner += ("<p class='fedit' style='margin-top:12px'>"
                      f"<a class='btn' href='/fact?id={fact_id}&edit={col}'>고치기</a></p>")
            if changed:   # 엑셀 원본에 닿는 길 — 고친 칸에만, 접어서(훑는 손가락에 안 닿게)
                inner += ("<details><summary>▸ 엑셀에서 온 원래 값 보기</summary>"
                          f"<div class='ptext placeholder' style='text-align:left'>"
                          f"{esc(origin or '(비어 있었음)')}</div>"
                          f"{_fact_undo_btn(fact_id, col)}</details>")
        panels.append(f"<div class='panel'><h2 class='sec'>{esc(label)}{chip}</h2>{inner}</div>")

    if edits:
        def edit_label(name):   # 값 칸이면 칸 이름, 상태·메모면 그 이름
            return FACT_HISTORY_LABEL.get(name) or labels.get(name) or name
        items = "".join(
            f"<li><span>{esc(edit_label(e['field_name']))}</span>"
            f"<span class='secsub'>{esc((e['edited_at'] or '')[:16])}</span></li>"
            for e in edits)
        history = (f"<details><summary>▸ 수정 이력 {len(edits)}건 보기</summary>"
                   f"<ul class='masklist'>{items}</ul></details>")
    else:
        history = ""

    # 방금 한 행동의 결과 한 줄(+ 되돌리기) — 조용히 바뀌는 것이 실수 클릭보다 위험하다
    flash = ""
    if error and edit is None:
        flash = f"<div class='flash bad'>{esc(error)}</div>"
    elif done in ("edit", "undo") and f in labels:
        word = "고쳤습니다" if done == "edit" else "되돌렸습니다"
        flash = (f"<div class='flash'>✓ ‘{esc(labels[f])}’을(를) {word}."
                 f"{_fact_undo_btn(fact_id, f)}</div>")
    elif done == "stamp" and s in FACT_STATUSES:
        flash = (f"<div class='flash'>✓ ‘{esc(s)}’(으)로 바꿨습니다."
                 f"{_fact_undo_btn(fact_id, FIELD_STATUS)}</div>")

    body = ("<div class='wrap'>"
            f"{flash}<h1 class='doc'>{esc(row['item_name'])}</h1>"
            f"<div class='meta'>{' · '.join(meta_bits)}</div>"
            "<p class='intro'>이 항목의 모든 칸을 아래에 한 번에 폈습니다. "
            "칸끼리 말이 어긋나는 곳을 찾는 게 이 화면의 목적입니다.</p>"
            "<p class='intro sub'>(예: 요건은 160시간인데 FAQ는 120시간이라고 적힌 경우)</p>"
            f"{''.join(panels)}{history}</div>"
            + _fact_stampbar(row, draft_note))
    return page(f"팩트 — {row['item_name']}", topbar, body)


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
                        conn, qs.get("view", ["all"])[0], qs.get("sort", ["recent"])[0],
                        qs.get("page", ["1"])[0], qs.get("cafe", [""])[0],
                        qs.get("staff", [""])[0], qs.get("topic", [""])[0]))
                elif u.path == "/analysis":
                    self._send_html(render_analysis(
                        conn, qs.get("sort", ["views"])[0],
                        qs.get("min_age", [None])[0] == "30",
                        qs.get("tsort", ["many"])[0],
                        qs.get("more", [None])[0] == str(TOPIC_MORE)))
                elif u.path == "/trends":
                    self._send_html(render_trends(conn))
                elif u.path == "/topics":
                    self._send_html(render_topics(conn))
                elif u.path == "/facts":
                    self._send_html(render_facts(
                        conn, qs.get("view", ["all"])[0], qs.get("kind", ["all"])[0]))
                elif u.path == "/fact":
                    self._send_html(render_fact(
                        conn, qs.get("id", [None])[0],
                        edit=qs.get("edit", [None])[0], done=qs.get("done", [None])[0],
                        f=qs.get("f", [None])[0], s=qs.get("s", [None])[0]))
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

        # ---- 쓰기(4차) — 팩트 화면 한 곳에서만. 다른 주소는 전부 거절 ----
        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != FACT_SAVE_PATH:
                # 다른 화면들은 여전히 읽기 전용이다(글목록·분석·트렌드·주제검수·데이터)
                return self.send_error(405, "Method Not Allowed")
            if not self._from_this_viewer():
                # 브라우저에 열어둔 다른 사이트가 몰래 저장을 밀어 넣는 것 차단
                return self._send_html(render_fact_denied(), code=403)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send_html(render_fact_denied(), code=403)
            if length <= 0 or length > FACT_POST_MAX:
                return self._send_html(render_fact_denied(), code=403)
            form = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)

            def one(key):
                v = form.get(key) or [""]
                return v[0]

            try:
                fact_id = int(one("id"))          # 입력검증: 항목 번호는 정수만
            except ValueError:
                return self._send_html(render_fact_not_found(), code=404)

            conn = get_connection(db_path)
            try:
                # 추출 배치가 같은 창고 파일에 쓰는 중일 수 있다 — 기다렸다 쓴다(선례 bulk_extract)
                conn.execute("PRAGMA busy_timeout = 60000")
                row = conn.execute("SELECT * FROM rulebook_facts WHERE fact_id=?",
                                   (fact_id,)).fetchone()
                if row is None:
                    return self._send_html(render_fact_not_found(), code=404)
                allowed = dict(FACT_FIELDS_BY_KIND[row["fact_kind"]])
                action = one("action")
                pats = masking.load_regex_patterns(conn)
                names = masking.load_staff_names(conn)

                if action == "edit":
                    field = one("field")
                    if field not in allowed:      # 입력검증: 정해진 칸 밖은 저장하지 않는다
                        return self._send_html(render_fact_denied(), code=403)
                    # ★ 불변 1: 화면에서 들어온 값도 가림을 통과시켜 '가려진 값'으로 저장한다
                    value, _ = masking.mask_text(one("value").replace("\r\n", "\n"), pats, names)
                    if len(value) > FACT_VALUE_MAX:
                        return self._send_html(render_fact(
                            conn, fact_id, edit=field, draft=value,
                            error=f"내용이 너무 깁니다(최대 {FACT_VALUE_MAX:,}자). "
                                  f"지금 {len(value):,}자예요. 줄여서 다시 저장해주세요."))
                    try:
                        fact_save_field(conn, fact_id, field, value)
                    except sqlite3.Error:
                        # 저장 실패 — 고치던 내용은 화면에 그대로 남긴다(절대 날리지 않는다)
                        return self._send_html(render_fact(
                            conn, fact_id, edit=field, draft=value,
                            error="저장하지 못했습니다. 지금 창고에 다른 작업(글 추출)이 쓰고 "
                                  "있는 것 같습니다. 잠시 뒤 [저장]을 다시 눌러주세요. "
                                  "고치던 내용은 아래에 그대로 있습니다."))
                    return self._see_other(f"/fact?id={fact_id}&done=edit&f={field}")

                if action == "stamp":
                    status = one("status")
                    if status not in FACT_STATUSES:
                        return self._send_html(render_fact_denied(), code=403)
                    note, _ = masking.mask_text(one("note").replace("\r\n", "\n"), pats, names)
                    if len(note) > FACT_VALUE_MAX:
                        return self._send_html(render_fact(
                            conn, fact_id, draft_note=note,
                            error=f"검수 메모가 너무 깁니다(최대 {FACT_VALUE_MAX:,}자). "
                                  f"지금 {len(note):,}자예요. 줄여서 다시 눌러주세요."))
                    try:
                        fact_stamp(conn, fact_id, status, note.strip() or None)
                    except sqlite3.Error:
                        return self._send_html(render_fact(
                            conn, fact_id, draft_note=note,
                            error="저장하지 못했습니다. 지금 창고에 다른 작업(글 추출)이 쓰고 "
                                  "있는 것 같습니다. 잠시 뒤 다시 눌러주세요. "
                                  "적어두신 메모는 아래에 그대로 있습니다."))
                    return self._see_other(
                        f"/fact?id={fact_id}&done=stamp&s={urllib.parse.quote(status)}")

                if action == "undo":
                    field = one("field")
                    if field not in allowed and field != FIELD_STATUS:
                        return self._send_html(render_fact_denied(), code=403)
                    try:
                        fact_undo(conn, fact_id, field)
                    except sqlite3.Error:
                        return self._send_html(render_fact(
                            conn, fact_id,
                            error="되돌리지 못했습니다. 지금 창고에 다른 작업(글 추출)이 쓰고 "
                                  "있는 것 같습니다. 잠시 뒤 다시 눌러주세요."))
                    if field == FIELD_STATUS:
                        return self._see_other(f"/fact?id={fact_id}")
                    return self._see_other(f"/fact?id={fact_id}&done=undo&f={field}")

                return self._send_html(render_fact_denied(), code=403)
            finally:
                conn.close()

        def _from_this_viewer(self):
            """이 뷰어 화면에서 직접 누른 저장인지 확인(다른 사이트가 시킨 요청 차단).

            127.0.0.1 바인딩과 함께 쓰는 두 번째 겹 — Host가 이 뷰어여야 하고,
            요청을 보낸 화면(Origin/Referer)도 같은 뷰어 주소여야 한다.
            """
            host = self.headers.get("Host", "")
            name = host.rsplit(":", 1)[0].strip("[]").lower()
            if name not in ("127.0.0.1", "localhost", "::1"):
                return False
            src = self.headers.get("Origin") or self.headers.get("Referer") or ""
            if not src:
                return False        # 출처를 알 수 없는 요청은 처리하지 않는다
            p = urllib.parse.urlparse(src)
            return p.scheme in ("http", "https") and p.netloc.lower() == host.lower()

        def _see_other(self, location):
            """저장 후 주소 재이동(303) — 새로고침으로 같은 저장이 두 번 들어가지 않게."""
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

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
