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
import html as html_mod
import mimetypes
import os
import re
import sqlite3
import sys
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import DEFAULT_DB_PATH, ROOT_DIR, get_connection  # noqa: E402
import masking  # noqa: E402

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
/* 목록 표 — 줄 전체가 클릭 영역(진짜 링크) */
.listhead, .listrow { display: grid;
    grid-template-columns: 3fr 1.2fr 1fr 0.8fr 0.8fr 0.8fr 1.1fr; gap: 12px;
    padding: 12px 16px; align-items: center; }
.listhead { color: var(--muted); font-size: 13px; font-weight: 700;
            border-bottom: 2px solid var(--line); }
.listrow { background: var(--paper); border-bottom: 1px solid var(--line);
           color: var(--ink); }
.listrow:hover { background: #eef4fb; text-decoration: none; }
.listrow .r-title { font-weight: 700; color: var(--brand); }
.num-dim { color: var(--muted); }
.filters { margin: 16px 0; font-size: 14px; }
.state { background: var(--paper); border: 1px solid var(--line);
         border-radius: 8px; padding: 32px; text-align: center; color: var(--muted); }
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
def render_list(conn, view="all"):
    try:
        rows = conn.execute(
            "SELECT post_id, title, cafe_name, extraction_status, publish_date, "
            "body_clean_path, "
            "(SELECT COUNT(*) FROM post_paragraphs pp WHERE pp.post_id=p.post_id) para_n, "
            "(SELECT COUNT(*) FROM post_images pi WHERE pi.post_id=p.post_id) img_n "
            "FROM posts p WHERE body_raw_path IS NOT NULL "
            "ORDER BY updated_at DESC, post_id DESC").fetchall()
    except sqlite3.Error:
        # 자산창고 파일/테이블을 못 열 때(코드 용어 노출 금지)
        topbar = "<div class='topbar'><span class='t-title'>추출 글 품질 확인</span></div>"
        body = ("<div class='wrap'><div class='state'>글 목록을 불러오지 못했습니다. "
                "자산창고 파일을 찾을 수 없어요. 자산창고를 먼저 만든 뒤 다시 열어주세요."
                "</div></div>")
        return page("추출 글 품질 확인", topbar, body)

    n_total = len(rows)
    n_ok = sum(1 for r in rows if is_success(r["extraction_status"]))
    n_fail = n_total - n_ok
    topbar = ("<div class='topbar'><span class='t-title'>추출 글 품질 확인</span>"
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
            "<div>제목</div><div>카페</div><div>상태</div>"
            "<div>가림</div><div>문단</div><div>이미지</div><div>작성일</div></div>")
    body_rows = []
    for r in rows:
        if not match(r):
            continue
        ok = is_success(r["extraction_status"])
        # simplified: 목록마다 body_clean을 다시 읽어 가림 건수를 센다(소량 파일럿엔 충분).
        #   글이 대량이 되면 건수를 저장해 두는 방식으로 바꿀 것.
        mask_n = sum(mask_type_counts(conn, r["body_clean_path"]).values())
        mask_cls = "" if mask_n else " num-dim"
        body_rows.append(
            f"<a class='listrow' href='/post?id={r['post_id']}'>"
            f"<div class='r-title'>{esc(r['title'] or '(제목 없음)')}</div>"
            f"<div>{esc(r['cafe_name'] or '-')}</div>"
            f"<div><span class='badge {'ok' if ok else 'danger'}'>"
            f"{esc(r['extraction_status'] or '상태 미상')}</span></div>"
            f"<div class='{mask_cls.strip()}'>{mask_n}건</div>"
            f"<div>{r['para_n']}</div><div>{r['img_n']}</div>"
            f"<div>{esc(r['publish_date'] or '-')}</div></a>")

    filters = ("<div class='filters'>보기: "
               "<a href='/'>전체</a> · <a href='/?view=ok'>성공만</a> · "
               "<a href='/?view=fail'>실패만</a></div>")
    body = (f"<div class='wrap'>{filters}{head}{''.join(body_rows)}</div>")
    return page("추출 글 품질 확인", topbar, body)


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
                    self._send_html(render_list(conn, qs.get("view", ["all"])[0]))
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
