# -*- coding: utf-8 -*-
"""extract_cafe.py — 카페 공개 글 본문 자동 추출기 (앞단, 1차 뼈대)

설계 원칙(계획 docs/plans/20260707-cafe-extractor-web-viewer.md):
  이 모듈은 "새 스크래퍼"가 아니라 **본문을 만들어 기존 파이프라인에 넘기는 앞단**이다.
  posts에서 아직 본문 없는 글 1건을 골라 → 공개 카페 페이지를 열어 제목·본문·이미지·조회수 확보
  → inbox/<슬러그>/body.txt + meta.json 저장 → intake_manual.process_article 재사용으로
  마스킹·문단분리·이미지·DB 저장. (마스킹/본문 파일화를 새로 짜지 않음 → 불변 1·4 자동 준수)

불변 준수 지점:
  - 불변 1(마스킹): 본문은 intake_manual → masking.py 통과분(body_pub_ref)만 참고. 패턴 재정의 안 함.
  - 불변 2(계정정보): 카페 글은 전체공개 → 로그인·비밀번호·계정정보 시트를 읽지도 저장하지도 않음.
  - 불변 3(성과 이원화): 조회수는 reference_signals(참고 신호)로만 저장. 성과 라벨 금지.
  - 불변 4(본문은 파일에만): posts에 본문 텍스트 안 넣음(process_article이 경로만 저장).
  - 불변 9(멱등): normalized_url로 기존 posts 행 갱신(새 행 금지). 재실행해도 중복 누적 없음.
  - 이미지: 보수값 기본(image_rights_review) — 재사용 허용값 자동 부여 금지(불변 1).

실패 분류(계획 결정 2): 실패-삭제된글 / 실패-비공개게시판 / 실패-로그인필요 / 실패-접근불가(기타).
  실패는 예외 없이 extraction_logs에 사유 기록(v9 "실패 사유 100%"). method='playwright'.

라이브 추출부(fetch_article_html)는 Playwright 필요(새 의존성). 파싱·분류·저장 로직은
  라이브 접속 없이 저장된 HTML 픽스처로 단위테스트 가능(fetch를 주입 seam으로 분리).

사용:
  python src/extract_cafe.py               # 본문 없는 글 1건 자동 선택 → 라이브 추출
  python src/extract_cafe.py <카페URL>      # 지정 글 1건(기존 posts 행이어야 함) 추출
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import ROOT_DIR, get_connection, init_db  # noqa: E402
import intake_manual as im  # noqa: E402

INBOX_DIR = os.path.join(ROOT_DIR, "inbox")
CORPUS_DIR = os.path.join(ROOT_DIR, "corpus")
METHOD = "playwright"
AUTO_VIEW_MARK = "자동추출:조회수"  # reference_signals 조회수 행 식별용(CA-2 순위행과 구분·멱등)
# 카페 이미지는 채팅캡처·닉네임 노출 가능 → 사람이 분류하기 전 보수 기본값(불변 1)
DEFAULT_REUSE_SCOPE = "image_rights_review"

# [정리] 예전 _SCOPE_END_RE(댓글/관련글 표식 절단)·_IMG_DENY(UI 이미지 deny-list)는 제거했다.
#   근거: 본문을 이제 div.article_viewer > div.se-main-container 서브트리로 "구조적으로" 스코프한다
#   (라이브=DOM query_selector, 오프라인=_slice_container의 balanced-div 스캔). 댓글·프로필·관련글·
#   편집기 UI는 컨테이너 바깥이라 애초에 들어오지 않고, 본문 이미지는 se-image 컴포넌트에서만 뽑으므로
#   자유텍스트 앵커나 src deny-list 없이도 새지 않는다. (중복 방어 제거 — 구조적 스코프가 대체)

# 접근 실패 감지어 → 상태값 (페이지 텍스트 기준, 계획 결정 2)
FAILURE_SIGNS = [
    ("실패-삭제된글", ["삭제된 게시글", "삭제된 게시물", "삭제되었", "존재하지 않는 게시"]),
    ("실패-로그인필요", ["로그인이 필요", "로그인 후 이용", "네이버 로그인"]),
    ("실패-비공개게시판", ["멤버만", "등급이 낮", "접근 권한", "가입된 회원", "비공개", "읽기 권한"]),
]


def classify_failure(page_text):
    """페이지 텍스트에서 접근 실패 유형을 판정. 실패 없으면 None.

    자체검증(보안/데이터 경로): 삭제/로그인/비공개 문구가 성공으로 새면 안 됨 → tests 참조.
    """
    if not page_text:
        return None
    for status, signs in FAILURE_SIGNS:
        if any(s in page_text for s in signs):
            return status
    return None


# ---------------------------------------------------------------------------
# HTML 파서 — SmartEditor 3(.se-main-container) 실제 구조 대상.
#   본문은 div.article_viewer > div.se-main-container 안의 se-component 블록이
#   위→아래로 쌓인 구조다(텍스트=se-text, 이미지=se-image, 표=se-table …).
#   ★ 문단 경계 = 이미지. 카페 글은 "이미지-텍스트-이미지-텍스트" 흐름이라
#     이미지가 사실상 문단 구분자다. 그래서 블록을 문서 순서대로 훑되:
#     텍스트 계열 컴포넌트는 버퍼에 계속 누적하고, 이미지를 만나면 그때까지
#     누적된 텍스트를 "한 문단"으로 끊는다(se-text 컴포넌트 개수로 쪼개지 않음 → 과분할 방지).
#     이미지 위치(nearby_paragraph_no)와 이미지↔텍스트 원본 흐름은 그대로 보존한다.
# simplified: 라이브 네이버 DOM 변경 시 셀렉터(se-* class 이름) 튜닝 필요할 수 있음(리뷰 주목).
# ---------------------------------------------------------------------------

# se-component 블록 경계(각 컴포넌트는 se-main-container의 최상위 형제 → 시작 태그로 분할).
# (?![-\w]): 'se-component' 정확 토큰만 — 내부 래퍼 div의 'se-component-content'를 블록으로 오인 금지.
_COMPONENT_RE = re.compile(r'<div[^>]*\bclass="([^"]*\bse-component(?![-\w])[^"]*)"[^>]*>')
# 텍스트 문단 조각(한 컴포넌트 내 여러 개 = 소프트 줄바꿈 → 같은 문단으로 이어붙임).
_PARA_RE = re.compile(r'<p[^>]*\bclass="[^"]*\bse-text-paragraph\b[^"]*"[^>]*>(.*?)</p>', re.DOTALL)
_IMG_TAG_RE = re.compile(r'<img\b[^>]*>')
# 텍스트 계열 컴포넌트(문단으로 취급). se-table은 best-effort로 셀 텍스트를 이어붙임.
_TEXT_FAMILY = ("se-text", "se-sectionTitle", "se-quotation", "se-table")


def parse_article_html(html, body_html=None):
    """HTML → {"title","view_count","publish_date","lines","images"}.

    제목·조회수·작성일은 se-main-container 바깥 헤더에 있으므로 항상 html(프레임 전체)에서 뽑고,
    본문(문단·이미지)은 body_html(se-main-container 서브트리)에서 뽑는다.
    body_html이 없으면(오프라인/폴백) html에서 컨테이너를 balanced-div 스캔으로 잘라 쓴다.
    라이브 접속 없이 저장된 픽스처로 검증 가능(tests/test_cafe_extract.py).
    """
    title = _extract_by_class(html, "title_text")
    count_raw = _extract_by_class(html, "count")
    date_raw = _extract_by_class(html, "date")
    view_count = None
    if count_raw:
        digits = re.sub(r"[^\d]", "", count_raw)
        view_count = int(digits) if digits else None
    publish_date = _normalize_date(date_raw)

    if body_html is None:
        body_html = _slice_container(html) or ""
    lines, images = parse_body_html(body_html)
    return {"title": (title or "").strip() or None,
            "view_count": view_count,
            "publish_date": publish_date,
            "lines": lines,
            "images": images}


def _extract_by_class(html, cls):
    """class에 cls 토큰을 가진 첫 요소의 내부 텍스트(태그 제거)."""
    # <tag ... class="... cls ..." ...> ... </tag> 중 첫 매치의 내부(비탐욕)
    m = re.search(
        r'<([a-zA-Z0-9]+)[^>]*\bclass="[^"]*\b' + re.escape(cls) + r'\b[^"]*"[^>]*>(.*?)</\1>',
        html, re.DOTALL)
    if not m:
        return None
    inner = re.sub(r"<[^>]+>", " ", m.group(2))
    inner = _unescape(inner)
    return re.sub(r"\s+", " ", inner).strip()


def _slice_container(html):
    """full html → div.se-main-container의 내부 HTML(서브트리)만 balanced-div 스캔으로 잘라 반환.

    라이브 경로는 fetch_article_html이 DOM(query_selector)으로 컨테이너를 정확히 떠서
    body_html로 넘기므로 여기 안 탄다. 이 함수는 오프라인/폴백 전용 — 예전 `(.*)$`가
    문서 끝까지(댓글·관련글) 삼키던 과다추출을, div 열림/닫힘 균형으로 컨테이너 끝에서 정확히 끊는다.
    자체검증(스코프 경계): 바깥 UI가 새면 tests의 'se-main-container 바깥 이미지 0' 케이스가 깨진다.
    """
    m = re.search(r'<div[^>]*\bclass="[^"]*\bse-main-container\b[^"]*"[^>]*>', html)
    if not m:
        return None
    start = m.end()
    depth = 1
    for t in re.finditer(r'<(/?)div\b[^>]*>', html[start:], re.IGNORECASE):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return html[start:start + t.start()]
    return html[start:]  # 닫는 태그를 못 찾으면 끝까지(백스톱)


def parse_body_html(body_html):
    """se-main-container 서브트리 → (lines, images). ★ 문단 경계 = 이미지.

    se-component 블록을 문서 순서대로 훑는다:
      - 텍스트 계열 컴포넌트(se-text/소제목/인용/표) = 문단 버퍼에 계속 누적
        (내부 여러 se-text-paragraph = 소프트 줄바꿈 → 공백으로 이어붙임).
      - 이미지 컴포넌트(se-image)를 만나면: (1) 누적된 텍스트 버퍼를 한 문단으로 flush,
        (2) 이미지 1장 기록(그룹이면 여러 장, src는 고해상 우선 data-lazy-src → src).
        nearby_paragraph_no = 그때까지 flush된 문단 수(1-based) → 이미지↔텍스트 원본 순서 보존.
      - 끝에 남은 텍스트 버퍼도 마지막 문단으로 flush.
      - 두 이미지 사이의 텍스트 컴포넌트들은 "한 문단"으로 병합(과분할 방지).
      - 연속 이미지(사이 텍스트 없음)는 빈 문단을 만들지 않음(각 이미지만 기록).
      - 이미지 0개 글 → 전체 텍스트가 1문단(사용자 규칙상 수용).
      - 맨 앞 '카페 운영진 허가를 받아 작성' 류 고지 문구는 본문이 아니므로 첫 블록이면 제거.
    최종 lines는 문단 사이를 정확히 빈 줄 1개(\\n\\n)로 구분 →
      intake.split_paragraphs(무수정)가 블록 1:1로 문단화한다.
    자체검증(문단 경계=이미지·병합·고지 제거): tests/test_cafe_extract.py 케이스가 깨지면 실패.
    """
    # 1) se-component 블록을 순서대로 (종류, 텍스트|이미지목록)로 파싱.
    blocks = []  # ("text", 컴포넌트문자열) | ("image", src)
    for cls, chunk in _iter_components(body_html):
        if "se-image" in cls:                       # 이미지 컴포넌트(그룹이면 여러 img)
            for src in _imgs_in(chunk):
                blocks.append(("image", src))
        elif any(t in cls for t in _TEXT_FAMILY):   # 텍스트/소제목/인용/표(best-effort)
            txt = _component_text(chunk)
            if txt:
                blocks.append(("text", txt))
        # 그 외 컴포넌트(동영상·링크카드·스티커 등)는 본문 산문 아님 → 건너뜀(보수)

    # 2) 맨 앞 고지 문구 블록 제거(첫 텍스트 블록만, 보수적으로 — 누적 시작 전에).
    if blocks and blocks[0][0] == "text" and _is_permission_notice(blocks[0][1]):
        blocks = blocks[1:]

    # 3) 순서 유지하며 문단(lines)·이미지(nearby) emit — 이미지가 문단 경계.
    lines, images = [], []
    para_count = 0
    buf = []  # 아직 flush 안 된 텍스트 컴포넌트들(이미지 경계/끝에서 한 문단으로 병합)

    def flush():
        nonlocal para_count
        if not buf:                  # 연속 이미지·맨앞 이미지 → 빈 문단 만들지 않음
            return
        if lines:
            lines.append("")         # 문단 사이 빈 줄 1개
        lines.append(" ".join(buf))
        buf.clear()
        para_count += 1

    for kind, val in blocks:
        if kind == "text":
            buf.append(val)          # 이미지 만날 때까지 계속 누적
        else:                        # image = 문단 경계
            flush()                  # 그때까지 누적 텍스트를 한 문단으로
            images.append({"src": val, "nearby_paragraph_no": max(1, para_count)})
    flush()                          # 끝에 남은 텍스트 버퍼 = 마지막 문단
    return lines, images


def _iter_components(body_html):
    """body_html을 se-component 시작 태그 경계로 분할해 (class, 블록HTML)을 순서대로 yield."""
    ms = list(_COMPONENT_RE.finditer(body_html))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(body_html)
        yield m.group(1), body_html[m.start():end]


def _component_text(chunk):
    """텍스트 계열 컴포넌트에서 문단 텍스트 추출(내부 se-text-paragraph를 공백으로 이어붙임)."""
    parts = _PARA_RE.findall(chunk)
    if not parts:
        parts = [chunk]  # se-sectionTitle 등 se-text-paragraph가 없으면 컴포넌트 전체 텍스트로(best-effort)
    out = []
    for part in parts:
        txt = re.sub(r"<[^>]+>", "", part)
        txt = _unescape(txt).replace("​", "").replace("﻿", "")  # zero-width/BOM 제거
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            out.append(txt)
    return " ".join(out).strip()


def _imgs_in(chunk):
    """이미지 컴포넌트 내 각 <img>의 src(고해상 우선: data-lazy-src → src)를 순서대로 반환."""
    out = []
    for tag in _IMG_TAG_RE.findall(chunk):
        m = re.search(r'\bdata-lazy-src="([^"]+)"', tag) or re.search(r'\bsrc="([^"]+)"', tag)
        if m:
            out.append(m.group(1))
    return out


def _is_permission_notice(text):
    """맨 앞 '카페 운영진 허가를 받아 작성' 류 고지 문구인지(본문 아님). 보수적으로 판정.

    형식(* * / " ' 따옴표)·표현 변형과 무관하게 내용 기준: '허가'+'작성'이 함께 있고
    '카페' 또는 '운영'이 있는 짧은 첫 블록만 고지로 본다(본문 오제거 방지).
    자체검증(보수성): 일반 본문 문장이 고지로 잘못 잡히면 tests의 고지-제거 케이스와 충돌해 드러난다.
    """
    t = re.sub(r'[\s*"\'“”]', "", text)
    return len(t) <= 80 and ("허가" in t and "작성" in t) and ("카페" in t or "운영" in t)


def _unescape(s):
    return (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
             .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))


def _normalize_date(raw):
    if not raw:
        return None
    m = re.search(r"(\d{4})[.\-/\s]+(\d{1,2})[.\-/\s]+(\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


# ---------------------------------------------------------------------------
# 저장 앞단 — inbox 작성 → intake 재사용
# ---------------------------------------------------------------------------
def slugify(url):
    """카페 URL → 안전한 폴더 슬러그(cafe_<카페>_<글ID>)."""
    m = re.search(r"cafe\.naver\.com/([A-Za-z0-9_-]+)/(\d+)", url or "")
    if m:
        return f"cafe_{m.group(1).lower()}_{m.group(2)}"
    safe = re.sub(r"[^A-Za-z0-9]+", "_", (url or "post")).strip("_")
    return f"cafe_{safe[:40]}" if safe else "cafe_unknown"


def build_meta(existing, parsed, image_metas):
    """기존 posts 행 값을 보존하며 추출값을 덮어쓴 meta(intake 입력)를 만든다.

    ★ upsert_post는 meta에 없는 필드를 NULL로 덮어쓰므로(데이터 손실 방지),
      기존 cafe_name/keyword/staff_name/account_id/publish_date를 반드시 넘겨준다.
    """
    def ex(k):
        try:
            return existing[k]
        except (KeyError, IndexError, TypeError):
            return None
    return {
        "normalized_url": ex("normalized_url"),
        "title": parsed.get("title") or ex("title"),
        "cafe_name": ex("cafe_name"),
        "keyword": ex("keyword"),
        "keyword_tier2": None,
        "category": None,
        "staff_name": ex("staff_name"),
        "account_id": ex("account_id"),  # 라벨 보존(계정 비번 아님 — 불변 2 무관)
        # 기존 CA-2 시트 날짜를 우선 보존(라벨 보존·데이터 손실 방지). 시트에 없을 때만 추출값으로 채움.
        "publish_date": ex("publish_date") or parsed.get("publish_date"),
        "images": image_metas,
    }


def _image_metas(parsed_images, local_paths):
    """추출 이미지 → intake용 이미지 meta. 재사용값은 보수 기본값(불변 1)."""
    out = []
    for i, img in enumerate(parsed_images, start=1):
        out.append({
            "image_order": i,
            "image_type": "본문이미지",
            "image_role": None,
            "image_source_type": "외부이미지",   # 카페 원본 → 외부
            "reuse_scope": DEFAULT_REUSE_SCOPE,  # 사람 검토 전까지 재사용 불가
            "contains_person": False,            # 자동 판별 못 함 → 사람이 확인(보수)
            "contains_logo": False,
            "contains_text": False,
            "nearby_paragraph_no": img.get("nearby_paragraph_no"),
            "image_url": img.get("src"),
            "local_path": local_paths.get(i),
        })
    return out


def download_images(images, dest_dir, referer):
    """이미지 best-effort 다운로드 → {order: 상대경로}. 실패는 건너뜀(추출 중단 안 함)."""
    os.makedirs(dest_dir, exist_ok=True)
    paths = {}
    for i, img in enumerate(images, start=1):
        src = img.get("src")
        if not src:
            continue
        try:
            ext = os.path.splitext(urllib.parse.urlparse(src).path)[1][:5] or ".jpg"
            fname = f"image_{i}{ext}"
            fpath = os.path.join(dest_dir, fname)
            req = urllib.request.Request(src, headers={
                "User-Agent": "Mozilla/5.0", "Referer": referer or "https://cafe.naver.com/"})
            with urllib.request.urlopen(req, timeout=20) as r, open(fpath, "wb") as f:
                f.write(r.read())
            paths[i] = os.path.relpath(fpath, ROOT_DIR)
        except Exception:
            continue  # 이미지 실패는 글 추출을 막지 않음
    return paths


def save_inbox(slug, body_text, meta):
    folder = os.path.join(INBOX_DIR, slug)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "body.txt"), "w", encoding="utf-8") as f:
        f.write(body_text)
    with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return folder


# ---------------------------------------------------------------------------
# 라이브 fetch (Playwright) — 유일한 외부 접속부. 나머지는 오프라인 테스트 가능.
# ---------------------------------------------------------------------------
def fetch_article_html(url, timeout=30000):
    """공개 카페 글을 열어 (html, page_text, body_html) 반환. Playwright 필요(새 의존성).

    - html/page_text: 본문 iframe(cafe_main) 프레임 전체 — 제목·조회수·작성일은 여기서(헤더).
    - body_html: 프레임 안 div.article_viewer > div.se-main-container 서브트리를 DOM으로 정확히 떠
      본문 전용으로 반환(바깥 댓글·프로필·관련글·UI가 구조적으로 배제됨). 못 찾으면 None
      → parse_article_html이 html에서 balanced-div 스캔으로 폴백.
    로그인/계정정보를 다루지 않음(불변 2) — 전체공개 페이지만 읽는다.
    라이브 셀렉터(iframe 이름·se-* class)는 네이버 구조 변경 시 튜닝 필요할 수 있음(리뷰 주목).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "Playwright 미설치 — 설치 필요: pip install playwright && playwright install chromium"
        ) from e
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            page_text = page.inner_text("body")
            html = page.content()
            body_html = None
            # 카페 본문은 iframe(id/name 'cafe_main') 안에 있는 경우가 많다.
            frame = None
            for f in page.frames:
                fu = f.url or ""
                if f.name == "cafe_main" or "/articles/" in fu or "ArticleRead" in fu:
                    frame = f
                    break
            scope = frame if frame is not None else page
            if frame is not None:
                try:
                    frame.wait_for_load_state("domcontentloaded", timeout=timeout)
                    html = frame.content()
                    page_text = frame.inner_text("body")
                except Exception:
                    scope = page  # iframe 접근 실패 → 상위 페이지 내용으로 진행
            # 진짜 본문 컨테이너만 DOM으로 정확히 뜬다(정규식으로 자르지 않음).
            try:
                el = (scope.query_selector("div.article_viewer div.se-main-container")
                      or scope.query_selector("div.se-main-container"))
                if el is not None:
                    body_html = el.inner_html()
            except Exception:
                body_html = None  # 실패 시 parse_article_html이 html에서 폴백 스코프
            return html, page_text, body_html
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# 한 건 처리 (체크포인트: 건마다 커밋) + 실패 기록
# ---------------------------------------------------------------------------
def _next_attempt_no(conn, post_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_no),0)+1 n FROM extraction_logs WHERE post_id=?",
        (post_id,)).fetchone()
    return row["n"]


def _log(conn, post_id, status, detail):
    conn.execute(
        "INSERT INTO extraction_logs (post_id, attempt_no, status, error_detail, method) "
        "VALUES (?, ?, ?, ?, ?)",
        (post_id, _next_attempt_no(conn, post_id), status, detail, METHOD))


def _fail(conn, post_id, status, detail):
    """실패 확정 — posts 상태 + extraction_logs 사유 100% 기록 후 커밋(체크포인트)."""
    conn.execute(
        "UPDATE posts SET extraction_status=?, extraction_error=?, "
        "updated_at=datetime('now','localtime') WHERE post_id=?",
        (status, detail, post_id))
    _log(conn, post_id, status, detail)
    conn.commit()
    return {"post_id": post_id, "status": status, "detail": detail, "ok": False}


def process_one(conn, post_row, html=None, page_text=None, body_html=None, download=True):
    """글 1건 끝-끝. html/page_text(/body_html)를 주면 라이브 접속 없이 처리(테스트 seam).

    성공: inbox 작성 → intake_manual.process_article 재사용 → 상태를 '성공(자동추출)'로,
          조회수는 reference_signals(참고 신호)로, 이미지는 corpus/에 내려받아 보수 분류.
    body_html(se-main-container 서브트리)이 없으면 parse_article_html이 html에서 폴백 스코프.
    """
    post_id = post_row["post_id"]
    url = post_row["normalized_url"]
    try:
        if html is None:
            html, page_text, body_html = fetch_article_html(url)

        parsed = parse_article_html(html or "", body_html=body_html)
        if not parsed["lines"]:
            # 본문이 비었을 때만 실패 사유 판정 — 정상 본문에 '멤버만/비공개' 산문이 있어도 오탐 방지
            fail = classify_failure(page_text or "") or "실패-접근불가(기타)"
            return _fail(conn, post_id, fail, "본문 없음: " + fail)

        slug = slugify(url)
        local_paths = {}
        if download:
            local_paths = download_images(
                parsed["images"], os.path.join(CORPUS_DIR, slug, "images"), url)
        image_metas = _image_metas(parsed["images"], local_paths)
        meta = build_meta(post_row, parsed, image_metas)
        body_text = "\n".join(parsed["lines"])
        folder = save_inbox(slug, body_text, meta)

        # ★ 기존 파이프라인 재사용 — 마스킹·문단·이미지·본문파일·DB저장(불변 1·4 자동 준수)
        res = im.process_article(conn, folder)
        pid = res["post_id"]

        # 자동추출 표식으로 상태 덮어쓰기(process_article은 '성공(수동투입)'로 넣음)
        conn.execute(
            "UPDATE posts SET extraction_status='성공(자동추출)', extraction_error=NULL, "
            "updated_at=datetime('now','localtime') WHERE post_id=?", (pid,))
        # 조회수 = 참고 신호로만(불변 3). 멱등: 자동추출 조회수 행만 교체.
        conn.execute("DELETE FROM reference_signals WHERE post_id=? AND collected_from_sheet=?",
                     (pid, AUTO_VIEW_MARK))
        if parsed["view_count"] is not None:
            conn.execute(
                "INSERT INTO reference_signals (post_id, view_count, collected_from_sheet) "
                "VALUES (?, ?, ?)", (pid, parsed["view_count"], AUTO_VIEW_MARK))
        # 내려받은 이미지 경로/URL을 post_images에 보강(intake는 분류만 저장)
        for imeta in image_metas:
            conn.execute(
                "UPDATE post_images SET image_url=?, local_path=? "
                "WHERE post_id=? AND image_order=?",
                (imeta.get("image_url"), imeta.get("local_path"), pid, imeta["image_order"]))

        _log(conn, pid, "성공(자동추출)",
             f"문단 {len(res['paras'])} · 이미지 {len(parsed['images'])} · 마스킹 {len(res['hits'])}")
        conn.commit()
        return {"post_id": pid, "status": "성공(자동추출)", "ok": True,
                "slug": slug, "paras": len(res["paras"]), "images": len(parsed["images"]),
                "view_count": parsed["view_count"], "hits": res["hits"],
                "images_reuse": DEFAULT_REUSE_SCOPE}
    except Exception as e:  # 어떤 실패도 사유 남기고 예외 전파 안 함(멈추지 않음)
        return _fail(conn, post_id, "실패-접근불가(기타)", repr(e))


def select_target(conn):
    """본문 없는 카페 글 1건(가장 오래된 post_id) 선택. 없으면 None."""
    return conn.execute(
        "SELECT * FROM posts WHERE body_raw_path IS NULL "
        "AND normalized_url LIKE '%cafe.naver.com/%' ORDER BY post_id LIMIT 1").fetchone()


def find_post(conn, url):
    return conn.execute("SELECT * FROM posts WHERE normalized_url=?", (url,)).fetchone()


def run(url=None, db_path=None):
    """1건 처리. 뼈대 단계 — 다건 확대는 사용자 승인 후 별도(계획 1차 후반)."""
    conn = get_connection(db_path) if db_path else get_connection()
    init_db(conn)
    post = find_post(conn, url) if url else select_target(conn)
    if post is None:
        conn.close()
        return None
    res = process_one(conn, post)
    conn.close()
    return res


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    url = sys.argv[1] if len(sys.argv) > 1 else None
    res = run(url)
    if res is None:
        print("대상 없음: 본문 없는 카페 글이 posts에 없습니다(또는 지정 URL이 posts에 없음).")
        return 1
    if res["ok"]:
        print(f"[성공(자동추출)] post_id={res['post_id']} · {res['slug']}")
        print(f"  문단 {res['paras']} · 이미지 {res['images']}(재사용:{res['images_reuse']}) "
              f"· 조회수 {res['view_count']}(참고신호) · 마스킹 {len(res['hits'])}건")
        for h in res["hits"]:
            print(f"    - {h['type']}: {h['original']} → 가림")
    else:
        print(f"[{res['status']}] post_id={res['post_id']} · 사유: {res['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
