# -*- coding: utf-8 -*-
"""intake_manual.py — 수동 투입 파이프라인 (CA-3, 파일럿 핵심)

inbox/<슬러그>/ 의 글 1건을 표준 양식(서비스기획서 v9 부록 A)으로 정리한다.
  입력: inbox/<슬러그>/body.txt (원문, 필수) + meta.json (제목·카페·담당·이미지 등, 선택)
  처리:
    1) 본문 3버전 파일 저장(corpus/): body_raw(원문 불변) / body_clean(정제) /
       body_pub_ref(개인정보 마스킹 — masking.py, 불변 1)
    2) 문단 분리 + 역할 1차 태깅(규칙 기반, 사람 검수 전제) + contains_fact/cta
    3) 이미지 메타 저장(meta.json의 반자동 입력 → post_images)
    4) posts(경로만·본문텍스트 없음, 불변 4) + post_paragraphs + post_images + staff + post_keywords 저장
    5) 사람용 요약(A안) 출력 + out/intake/<슬러그>_요약.md 저장

멱등: 같은 글(normalized_url 또는 슬러그) 재투입 시 기존 post를 갱신(문단·이미지 재생성).
사용: python src/intake_manual.py [inbox/<슬러그> ...]   (인자 없으면 inbox 전체)
"""
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import ROOT_DIR, get_connection, init_db  # noqa: E402
import masking  # noqa: E402

INBOX_DIR = os.path.join(ROOT_DIR, "inbox")
CORPUS_DIR = os.path.join(ROOT_DIR, "corpus")
OUT_DIR = os.path.join(ROOT_DIR, "out", "intake")

# 문단 역할 1차 태깅 규칙 (사람 검수 전제 — 규칙 기반이라 완벽하지 않음)
INTRO_CUES = ["안녕하세요", "짚어드릴", "알아볼게요", "막막"]
CLOSING_CUES = ["정리하면", "요약하면", "마지막으로", "결론", "정리해"]
ROLE_RULES = [
    ("CTA",     ["문의", "상담", "연락", "카톡", "오픈채팅", "신청하세요", "물어보세요", "물어보"]),
    ("주의사항", ["주의", "유의", "놓치면", "조심", "꼭 확인"]),
    ("절차안내", ["커리큘럼", "단계", "순서", "신청", "절차"]),
    ("조건설명", ["응시자격", "요건", "학위", "학점", "이수", "자격", "조건"]),
    ("비교",     ["차이", "비교", "vs", "반면"]),
    ("사례",     ["후기", "경험담"]),
]
FACT_HINTS = ["학위", "학점", "시간", "과목", "1년", "수련", "필기", "실기", "응시자격", "요건"]


def split_paragraphs(text):
    """빈 줄 기준 문단 분리. 문단 내부 원본 줄바꿈(단일 개행)은 보존, 좌우 공백만 정리.

    (분리 규칙은 빈 줄 기준 그대로 — 내부 개행 보존은 문단 수에 영향 없음.
     오른쪽 '정리결과' 패널이 원문처럼 줄 구조를 유지하도록 함.)
    """
    blocks = re.split(r"\n\s*\n", text.strip())
    paras = []
    for b in blocks:
        clean = re.sub(r"[ \t]*\n[ \t]*", "\n", b.strip())  # 개행 좌우 공백만 제거, 개행은 보존
        clean = re.sub(r"[ \t]{2,}", " ", clean)
        if clean:
            paras.append(clean)
    return paras


def tag_role(para, idx, total):
    """문단 → (role, confidence). 위치(도입·마무리) 규칙 우선, 그다음 키워드 규칙.
    규칙 기반이라 확신을 함께 반환 — '낮음'은 사람 검수 대상."""
    if idx == 0 and any(k in para for k in INTRO_CUES):
        return "도입", "높음"
    if any(k in para for k in CLOSING_CUES):
        return "마무리", "높음"
    for role, kws in ROLE_RULES:
        if any(k in para for k in kws):
            return role, "높음"
    if idx == 0:
        return "도입", "낮음"
    if idx == total - 1:
        return "마무리", "낮음"
    return "배경설명", "낮음"


def clean_body(text):
    """body_clean: 3줄 이상 연속 빈 줄 축소, 줄끝 공백·특수문자 반복 정리. 원문 훼손 최소."""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[!]{2,}", "!", t)
    t = re.sub(r"[~]{2,}", "~", t)
    return t.strip() + "\n"


def _w(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def upsert_post(conn, meta, paths):
    """posts upsert — normalized_url 있으면 그 행 갱신, 없으면 슬러그로 신규."""
    url = meta.get("normalized_url")
    row = None
    if url:
        row = conn.execute("SELECT post_id FROM posts WHERE normalized_url=?", (url,)).fetchone()
    fields = {
        "cafe_name": meta.get("cafe_name"), "keyword": meta.get("keyword"),
        "staff_name": meta.get("staff_name"), "account_id": meta.get("account_id"),
        "publish_date": meta.get("publish_date"), "title": meta.get("title"),
        "body_raw_path": paths["raw"], "body_clean_path": paths["clean"],
        "body_pub_ref_path": paths["pub_ref"],
        "extraction_status": "성공(수동투입)",
        "content_length_type": paths["length_type"],
    }
    if row:
        post_id = row["post_id"]
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE posts SET {sets}, updated_at=datetime('now','localtime') "
                     f"WHERE post_id=?", (*fields.values(), post_id))
    else:
        cols = ", ".join(fields) + ", normalized_url, original_url"
        ph = ", ".join("?" for _ in range(len(fields) + 2))
        cur = conn.execute(f"INSERT INTO posts ({cols}) VALUES ({ph})",
                           (*fields.values(), url, url))
        post_id = cur.lastrowid
    return post_id


def process_article(conn, folder):
    slug = os.path.basename(folder.rstrip("/\\"))
    body_path = os.path.join(folder, "body.txt")
    if not os.path.exists(body_path):
        raise FileNotFoundError(f"{slug}: body.txt 없음")
    with open(body_path, encoding="utf-8") as f:
        raw = f.read()
    meta = {}
    mp = os.path.join(folder, "meta.json")
    if os.path.exists(mp):
        with open(mp, encoding="utf-8") as f:
            meta = json.load(f)

    # 1) 3버전 — 마스킹은 masking.py(DB 패턴 + staff 이름목록) 재사용
    regex_pats = masking.load_regex_patterns(conn)
    staff_names = masking.load_staff_names(conn)
    clean = clean_body(raw)
    pub_ref, hits = masking.mask_text(clean, regex_pats, staff_names)

    n_chars = len(re.sub(r"\s", "", clean))
    length_type = "short" if n_chars < 400 else ("long" if n_chars > 1200 else "medium")

    cdir = os.path.join(CORPUS_DIR, slug)
    paths = {
        "raw": os.path.relpath(os.path.join(cdir, "body_raw.txt"), ROOT_DIR),
        "clean": os.path.relpath(os.path.join(cdir, "body_clean.txt"), ROOT_DIR),
        "pub_ref": os.path.relpath(os.path.join(cdir, "body_pub_ref.txt"), ROOT_DIR),
        "length_type": length_type,
    }
    _w(os.path.join(ROOT_DIR, paths["raw"]), raw)          # 원문 불변(불변 4)
    _w(os.path.join(ROOT_DIR, paths["clean"]), clean)
    _w(os.path.join(ROOT_DIR, paths["pub_ref"]), pub_ref)

    # 2) DB upsert + 문단 재생성
    post_id = upsert_post(conn, meta, paths)
    # 가림 건수 저장 — 방금 위에서 나온 hits를 그대로 쓴다(파일을 다시 열어 다시 세지 않음).
    # 세는 재료가 body_clean(=clean)이라 상세 화면의 '총 N건'과 같은 숫자다.
    # ★ 불변 1: 넣는 건 개수(len)와 지문뿐 — hits[i]['original']은 쓰지 않는다.
    conn.execute(
        "UPDATE posts SET mask_count=?, mask_rules_fingerprint=? WHERE post_id=?",
        (len(hits), masking.rules_fingerprint(conn), post_id))
    conn.execute("DELETE FROM post_paragraphs WHERE post_id=?", (post_id,))
    conn.execute("DELETE FROM post_images WHERE post_id=?", (post_id,))

    paras = split_paragraphs(clean)
    # 본문 첫 줄이 제목과 같으면 문단에서 제외(제목은 posts.title에 이미 저장 — 문단 아님)
    title = (meta.get("title") or "").strip()
    if paras and title:
        norm = lambda s: re.sub(r"[\s\W]", "", s)
        if norm(paras[0]) == norm(title):
            paras = paras[1:]
    para_info = []
    for i, p in enumerate(paras):
        role, conf = tag_role(p, i, len(paras))
        contains_fact = 1 if any(h in p for h in FACT_HINTS) else 0
        contains_cta = 1 if role == "CTA" else 0
        # 문단 clean_text도 마스킹본으로(참고용 산출물엔 개인정보 없어야 — 불변 1)
        p_masked, _ = masking.mask_text(p, regex_pats, staff_names)
        conn.execute(
            """INSERT INTO post_paragraphs
               (post_id, paragraph_no, raw_text, clean_text, role, contains_cta, contains_fact)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (post_id, i + 1, p, p_masked, role, contains_cta, contains_fact))
        para_info.append({"no": i + 1, "role": role, "conf": conf,
                          "fact": contains_fact, "cta": contains_cta})

    # 3) 이미지 메타(반자동)
    for im in meta.get("images", []):
        conn.execute(
            """INSERT INTO post_images
               (post_id, image_order, image_type, image_role, image_source_type,
                reuse_scope, contains_person, contains_logo, contains_text, nearby_paragraph_no)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (post_id, im.get("image_order"), im.get("image_type"), im.get("image_role"),
             im.get("image_source_type"), im.get("reuse_scope"),
             int(bool(im.get("contains_person"))), int(bool(im.get("contains_logo"))),
             int(bool(im.get("contains_text"))), im.get("nearby_paragraph_no")))

    # 키워드(다대다)
    for kw, tier in [(meta.get("keyword"), "1차"), (meta.get("keyword_tier2"), "2차")]:
        if not kw:
            continue
        conn.execute("INSERT OR IGNORE INTO keywords (keyword, tier, category) VALUES (?, ?, ?)",
                     (kw, tier, meta.get("category")))
        kid = conn.execute("SELECT keyword_id FROM keywords WHERE keyword=?", (kw,)).fetchone()["keyword_id"]
        conn.execute("INSERT OR IGNORE INTO post_keywords (post_id, keyword_id, tier) VALUES (?, ?, ?)",
                     (post_id, kid, tier))
    if meta.get("staff_name"):
        conn.execute("INSERT OR IGNORE INTO staff (staff_name) VALUES (?)", (meta["staff_name"],))
    conn.commit()

    return {"slug": slug, "post_id": post_id, "meta": meta, "paras": para_info,
            "images": meta.get("images", []), "hits": hits, "length_type": length_type,
            "pub_ref": pub_ref}


def build_summary(result):
    """사람용 요약(A안) — Markdown."""
    m = result["meta"]
    L = []
    L.append(f"# 📋 정리 요약 — {m.get('title', result['slug'])}")
    L.append("")
    L.append(f"- **카페/계정**: {m.get('cafe_name','?')} / {m.get('account_id','?')}(업로드 계정)")
    L.append(f"- **담당**: {m.get('staff_name','?')} · **키워드**: {m.get('keyword','?')}"
             f"{' / ' + m['keyword_tier2'] if m.get('keyword_tier2') else ''}")
    L.append(f"- **작성일**: {m.get('publish_date','?')} · **길이유형**: {result['length_type']}")
    L.append(f"- **저장 위치**: corpus/{result['slug']}/ (원문·정제·참고용 3버전) · post_id={result['post_id']}")
    L.append("")
    L.append(f"## 문단 {len(result['paras'])}개 (역할 1차 태깅 — 사람 검수 대상)")
    for p in result["paras"]:
        marks = []
        if p["fact"]:
            marks.append("📌팩트")
        if p["cta"]:
            marks.append("📣CTA")
        conf = "" if p["conf"] == "높음" else f" (확신 {p['conf']} — 확인要)"
        L.append(f"- {p['no']}. **{p['role']}**{conf} {' '.join(marks)}")
    L.append("")
    L.append(f"## 이미지 {len(result['images'])}개")
    for im in result["images"]:
        reuse = im.get("reuse_scope", "")
        flag = " ⚠️원본재사용금지" if reuse == "image_pattern_only" else ""
        person = " 👤인물" if im.get("contains_person") else ""
        L.append(f"- {im.get('image_order')}. {im.get('image_type')}/{im.get('image_role')} "
                 f"· {im.get('image_source_type')} · {reuse}{flag}{person}")
    L.append("")
    L.append("## 🔒 개인정보 마스킹 결과 (참고용 본문에서 가림)")
    if result["hits"]:
        for h in result["hits"]:
            L.append(f"- {h['type']}: `{h['original']}` → 가림")
    else:
        L.append("- (가릴 개인정보 없음)")
    L.append("")
    L.append("> 참고용 본문(body_pub_ref)에 위 항목이 모두 가려져 저장됨. 생성 단계는 이 버전만 참고.")
    return "\n".join(L)


def run(folders=None, db_path=None):
    conn = get_connection(db_path) if db_path else get_connection()
    init_db(conn)
    if not folders:
        folders = [os.path.dirname(p) for p in glob.glob(os.path.join(INBOX_DIR, "*", "body.txt"))]
    results = []
    for folder in folders:
        res = process_article(conn, folder)
        summary = build_summary(res)
        _w(os.path.join(OUT_DIR, f"{res['slug']}_요약.md"), summary)
        res["summary"] = summary
        results.append(res)
    conn.close()
    return results


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    folders = sys.argv[1:] or None
    results = run(folders)
    if not results:
        print(f"inbox에 처리할 글이 없습니다: {INBOX_DIR}/<슬러그>/body.txt")
        return 1
    for res in results:
        print(res["summary"])
        print("\n" + "=" * 70 + "\n")
    print(f"{len(results)}건 처리 완료 → 요약: out/intake/ · 본문: corpus/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
