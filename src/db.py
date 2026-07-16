# -*- coding: utf-8 -*-
"""db.py — 콘텐츠 자산 DB 연결·스키마 생성 (CA-1)

기준 문서: docs/서비스기획서-v9.md §3(데이터 모델) + 부록 A.
원칙(CLAUDE.md 불변):
  - 본문 텍스트는 DB에 저장하지 않는다 — posts에는 파일 경로(body_*_path)만.
    (post_paragraphs의 문단 텍스트는 v9 §3-4가 정의한 분석용 필드로 예외)
  - 순위·조회수는 "참고 신호" — 등급(P1/P2/P3)·감점 컬럼을 만들지 않는다.
  - account_id는 업로드 계정 식별 메타 전용. `계정 정보` 시트 내용은 미반입.
  - 마이그레이션은 멱등: CREATE TABLE IF NOT EXISTS + ensure_column. 컬럼 삭제 금지.

사용: python src/db.py  →  data/content_assets.sqlite3 생성 후 테이블 목록 출력
"""
import os
import sqlite3
import sys

# 저장소 루트 기준 기본 DB 경로 (CRM과 완전 별개 파일)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(ROOT_DIR, "data", "content_assets.sqlite3")


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """DB 연결을 만든다. data/ 폴더가 없으면 생성."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """테이블에 컬럼이 없으면 추가한다(멱등). 이미 있으면 아무것도 안 함."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# ---------------------------------------------------------------------------
# 스키마 정의 — v9 §3-2 ~ §3-6 그대로
# ---------------------------------------------------------------------------
SCHEMA_STATEMENTS = [
    # 담당직원 = 작성자 (동일 간주, v9 설계노트 1)
    """CREATE TABLE IF NOT EXISTS staff (
        staff_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        staff_name    TEXT NOT NULL UNIQUE,
        team          TEXT,
        active        INTEGER NOT NULL DEFAULT 1,
        notes         TEXT
    )""",

    # 유사도 군집 (posts.cluster_id가 참조)
    """CREATE TABLE IF NOT EXISTS content_clusters (
        cluster_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        category              TEXT,
        cluster_type          TEXT,   -- near_duplicate / variation / template
        representative_post_id INTEGER,
        member_count          INTEGER,
        similarity_threshold  REAL,
        created_at            TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",

    # 게시글 메타 — 본문 텍스트 컬럼 없음(파일 경로만)
    """CREATE TABLE IF NOT EXISTS posts (
        post_id                INTEGER PRIMARY KEY AUTOINCREMENT,
        original_url           TEXT,
        normalized_url         TEXT UNIQUE,
        cafe_name              TEXT,
        board_name             TEXT,
        keyword                TEXT,   -- 대표 키워드(상세 다대다는 post_keywords)
        staff_name             TEXT,   -- 담당직원=작성자, 문체 집계 기준
        account_id             TEXT,   -- 업로드 계정 라벨(현황 시트 출처, 메타 전용)
        publish_date           TEXT,
        source_sheet           TEXT,
        source_row_no          INTEGER,
        title                  TEXT,
        title_length           INTEGER,
        title_form             TEXT,   -- 질문형/단정형/리스트형
        title_has_number       INTEGER,
        title_keyword_position TEXT,   -- 앞/중/뒤
        cluster_id             INTEGER REFERENCES content_clusters(cluster_id),
        body_raw_path          TEXT,   -- 본문은 폴더 코퍼스 파일로만, DB엔 경로만
        body_clean_path        TEXT,
        body_pub_ref_path      TEXT,
        content_length_type    TEXT,   -- short/medium/long (분류값, 감점 아님)
        content_use_case       TEXT,   -- 분류 기준 확정 후 부여(Phase 1 보류)
        extraction_status      TEXT,
        extraction_error       TEXT,
        usage_tags             TEXT,   -- JSON 배열
        risk_tags              TEXT,   -- JSON 배열
        created_at             TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        updated_at             TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",

    # 순위·조회수 = 참고 신호 (성과 지표 아님, v9 §3-3·§5)
    """CREATE TABLE IF NOT EXISTS reference_signals (
        signal_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id              INTEGER NOT NULL REFERENCES posts(post_id),
        rank_source          TEXT,
        rank_value           INTEGER,
        rank_bucket          TEXT,   -- Top3/Top10/Top30/Other/Not Exposed/Unknown
        exposure_status      TEXT,
        view_count           INTEGER,
        view_bucket          TEXT,   -- 분위수 버킷(상위10%/상위30%/중간/하위/Unknown)
        collected_from_sheet TEXT,
        collected_at         TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",

    # 문단 (raw/clean 텍스트는 v9 §3-4 정의 필드 — 분석 단위라 DB 보관)
    """CREATE TABLE IF NOT EXISTS post_paragraphs (
        paragraph_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id          INTEGER NOT NULL REFERENCES posts(post_id),
        paragraph_no     INTEGER NOT NULL,
        raw_text         TEXT,
        clean_text       TEXT,
        role             TEXT,   -- 도입/문제제기/공감/조건설명/절차안내/비교/사례/주의사항/CTA/마무리
        summary          TEXT,
        contains_cta     INTEGER,
        contains_fact    INTEGER,
        nearby_image_ids TEXT,   -- JSON 배열
        usage_tags       TEXT,
        risk_tags        TEXT
    )""",

    # 이미지 메타 (v9 §3-5·§7)
    """CREATE TABLE IF NOT EXISTS post_images (
        image_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id             INTEGER NOT NULL REFERENCES posts(post_id),
        image_order         INTEGER,
        image_url           TEXT,
        local_path          TEXT,
        image_hash          TEXT,
        image_source_type   TEXT,   -- 내부제작/AI제작/외부이미지/캡처/출처불명
        reuse_scope         TEXT,   -- image_reuse_allowed / image_pattern_only / image_rights_review
        contains_person     INTEGER,
        contains_logo       INTEGER,
        contains_text       INTEGER,
        image_role          TEXT,
        image_type          TEXT,
        nearby_paragraph_no INTEGER,
        nearby_text         TEXT,
        width               INTEGER,
        height              INTEGER,
        usage_tags          TEXT,
        risk_tags           TEXT
    )""",

    # 키워드 다계층 (v9 설계노트 2)
    """CREATE TABLE IF NOT EXISTS keywords (
        keyword_id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword    TEXT NOT NULL UNIQUE,
        tier       TEXT,   -- 1차/2차/3차
        category   TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS post_keywords (
        post_id    INTEGER NOT NULL REFERENCES posts(post_id),
        keyword_id INTEGER NOT NULL REFERENCES keywords(keyword_id),
        tier       TEXT,
        PRIMARY KEY (post_id, keyword_id)
    )""",

    # 추출 로그 — 실패 사유 100% 기록 (v9 §4)
    """CREATE TABLE IF NOT EXISTS extraction_logs (
        log_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id      INTEGER REFERENCES posts(post_id),
        attempt_no   INTEGER,
        status       TEXT,
        error_detail TEXT,
        method       TEXT,   -- 추출 수단(예: playwright, browser, 수동투입)
        attempted_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",

    # 게시글 임베딩 (외부 API 생성 벡터를 BLOB로 저장, v9 §3-6)
    """CREATE TABLE IF NOT EXISTS post_embeddings (
        post_id      INTEGER PRIMARY KEY REFERENCES posts(post_id),
        vector       BLOB,
        model_name   TEXT,
        dim          INTEGER,
        source_field TEXT,   -- 예: body_clean
        created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",

    # Before/After 학습 데이터 — Phase 1에서는 구조만 (v9 §9)
    """CREATE TABLE IF NOT EXISTS review_pairs (
        review_pair_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        source_post_id  INTEGER REFERENCES posts(post_id),
        draft_ai        TEXT,
        draft_human     TEXT,
        diff_summary    TEXT,
        edit_type       TEXT,   -- 팩트수정/문체수정/CTA수정/길이수정/표현삭제/중복문장정리/이미지요청수정/구조수정
        reviewer        TEXT,
        approved_at     TEXT,
        reuse_as_fewshot INTEGER,
        created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",

    # ------------------------------------------------------------------
    # 룰북 적재 테이블 (CA-1 최소: 카테고리·금지어·개인정보 패턴)
    # ------------------------------------------------------------------
    """CREATE TABLE IF NOT EXISTS rulebook_categories (
        category_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        no                   INTEGER,
        top_category         TEXT,           -- 상위 카테고리(자격증 취득 등)
        category_name        TEXT NOT NULL,  -- 하위 카테고리(=카테고리명)
        keyword_examples     TEXT,
        credit_bank_link     TEXT,           -- 학점은행제 연결 방식
        unique_keyword_count INTEGER,
        total_post_frequency INTEGER,
        active               INTEGER NOT NULL DEFAULT 1,
        source_version       TEXT NOT NULL   -- 예: V4.2
    )""",

    """CREATE TABLE IF NOT EXISTS rulebook_banned_words (
        word_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        no             INTEGER,
        word           TEXT NOT NULL,
        reason         TEXT,
        replacement    TEXT,
        source_version TEXT NOT NULL
    )""",

    # ------------------------------------------------------------------
    # 팩트 룰북 (CA-6) — `② 팩트 룰북` 시트: 공통 팩트 + 개별 팩트
    #   - 한 테이블 + fact_kind 구분 + 열 합집합 (계획 §4: 검수·화면·프롬프트 주입이 한 경로)
    #   - ★ 식별키는 엑셀 No.가 아니라 사람이 부르는 이름:
    #       공통 = 카테고리명 / 개별 = 상품·키워드명  → UNIQUE(fact_kind, item_name)
    #     (No.는 재번호되면 어긋나므로 참고용 컬럼일 뿐)
    #   - 내용 칸은 엑셀 열 그대로 텍스트 보관. FAQ TOP3처럼 한 칸에 여러 개가 든 셀도 쪼개지 않음.
    #   - review_status 기본값 '미확인' (계획 전제: AI 초안이라 아직 아무도 확인하지 않음)
    # ------------------------------------------------------------------
    """CREATE TABLE IF NOT EXISTS rulebook_facts (
        fact_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        fact_kind          TEXT NOT NULL CHECK (fact_kind IN ('공통','개별')),
        excel_no           INTEGER,   -- 엑셀 No.(참고용 — 식별키 아님)
        division           TEXT,      -- 엑셀 '구분'
        category           TEXT,      -- 공통: 카테고리 / 개별: 연결 카테고리
        item_name          TEXT NOT NULL,  -- ★ 식별키: 공통=카테고리명 / 개별=상품·키워드명
        -- 공통 팩트 칸 (SECTION A 열 그대로)
        requirement        TEXT,      -- 응시/취득 요건
        credits            TEXT,      -- 필요 학점
        duration           TEXT,      -- 예상 소요 기간
        shortcut           TEXT,      -- 기간 단축 방법
        faq_top3           TEXT,      -- 자주 묻는 질문 TOP3 (한 칸에 3개 — 쪼개지 않음)
        cautions           TEXT,      -- 주의사항 / 흔한 오해
        -- 공통·개별 공용
        caution_memo       TEXT,      -- 주의메모(시점/예외)
        -- 개별 팩트 칸 (SECTION B 열 그대로)
        core_fact          TEXT,      -- 핵심 팩트
        path_by_education  TEXT,      -- 학력별 경로 요약
        emphasis           TEXT,      -- 글 작성 시 강조포인트
        use_priority       TEXT,      -- 사용 우선순위
        remarks            TEXT,      -- 비고
        -- 검수 (계획 D2: 항목 통째 / D3: 상태 3개)
        review_status      TEXT NOT NULL DEFAULT '미확인'
                           CHECK (review_status IN ('미확인','확인함','보류')),
        reviewed_at        TEXT,
        review_note        TEXT,
        -- 엑셀 원본 지문 — 재적재 때 '엑셀이 바뀐 것 같은 항목' 감지용(덮어쓰기 안 함)
        source_fingerprint TEXT,
        source_version     TEXT NOT NULL,
        created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        updated_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE (fact_kind, item_name)
    )""",

    # 팩트 수정 이력 — 되돌리기 + '엑셀 원본이 뭐였지'(첫 수정의 old_value가 엑셀 원본)
    """CREATE TABLE IF NOT EXISTS rulebook_fact_edits (
        edit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        fact_id    INTEGER NOT NULL REFERENCES rulebook_facts(fact_id),
        field_name TEXT NOT NULL,   -- 고친 칸(rulebook_facts 컬럼명)
        old_value  TEXT,
        new_value  TEXT,
        edited_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",

    # 개인정보·직원 식별정보 탐지 패턴 (v9 §8·§8-2 정의 — 정제 마스킹·린터 공용)
    """CREATE TABLE IF NOT EXISTS rulebook_pii_patterns (
        pattern_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        pattern_type TEXT NOT NULL,  -- regex(정규식) / name_list(이름 목록 — staff·닉네임 확보 후 채움)
        pattern      TEXT,           -- regex일 때 정규식
        replacement  TEXT,           -- 마스킹 치환 문구
        description  TEXT,
        source       TEXT NOT NULL   -- 근거 문서(예: 서비스기획서 v9 §8)
    )""",
]

# 나중에 붙인 칸 — 기존 DB에도 멱등하게 추가(불변 9: 추가만. 삭제·변경 없음).
# CREATE TABLE에 넣지 않고 여기 모으는 이유: 새 DB·기존 DB가 같은 경로로 같은 결과가 된다.
ENSURE_COLUMNS = [
    # 글목록이 글마다 본문 파일을 열어 개인정보를 다시 세지 않도록 '가림 건수'를 저장한다.
    #   ★ 불변 1: 저장하는 건 건수(정수)와 규칙 지문뿐 — 원본 문자열(전화번호·이름)은 넣지 않는다.
    #   ★ 불변 4: 본문 텍스트 칸이 아니다(본문은 여전히 파일에만, posts엔 경로만).
    ("posts", "mask_count", "INTEGER"),
    # 그 건수를 '어떤 가림 규칙으로 셌는지'(masking.rules_fingerprint).
    # 지금 규칙과 다르면 저장된 건수는 옛 숫자 → 화면은 숫자를 쓰지 않고 '다시 세기 필요'로 둔다
    # (자동 재계산 안 함 — 뷰어는 읽기 전용. 다시 세기는 src/count_masks.py 한 번).
    ("posts", "mask_rules_fingerprint", "TEXT"),
]

INDEX_STATEMENTS = [
    # v9 설계노트 5: 생성 단계 RAG 대비 — category·usage_tags·staff_name 인덱스 선확보
    "CREATE INDEX IF NOT EXISTS idx_posts_staff_name ON posts(staff_name)",
    "CREATE INDEX IF NOT EXISTS idx_posts_cafe_name ON posts(cafe_name)",
    "CREATE INDEX IF NOT EXISTS idx_posts_cluster_id ON posts(cluster_id)",
    "CREATE INDEX IF NOT EXISTS idx_posts_extraction_status ON posts(extraction_status)",
    "CREATE INDEX IF NOT EXISTS idx_reference_signals_post_id ON reference_signals(post_id)",
    "CREATE INDEX IF NOT EXISTS idx_post_paragraphs_post_id ON post_paragraphs(post_id)",
    "CREATE INDEX IF NOT EXISTS idx_post_images_post_id ON post_images(post_id)",
    "CREATE INDEX IF NOT EXISTS idx_post_images_hash ON post_images(image_hash)",
    "CREATE INDEX IF NOT EXISTS idx_extraction_logs_post_id ON extraction_logs(post_id)",
    "CREATE INDEX IF NOT EXISTS idx_keywords_category ON keywords(category)",
    "CREATE INDEX IF NOT EXISTS idx_rulebook_fact_edits_fact_id ON rulebook_fact_edits(fact_id)",
]

# 자산 테이블 + 룰북 테이블 전체 이름 (테스트·검증용)
EXPECTED_TABLES = [
    "staff", "content_clusters", "posts", "reference_signals",
    "post_paragraphs", "post_images", "keywords", "post_keywords",
    "extraction_logs", "post_embeddings", "review_pairs",
    "rulebook_categories", "rulebook_banned_words", "rulebook_pii_patterns",
    "rulebook_facts", "rulebook_fact_edits",
]


def init_db(conn: sqlite3.Connection) -> None:
    """전체 스키마 생성(멱등). 여러 번 실행해도 안전."""
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    for table, column, decl in ENSURE_COLUMNS:
        ensure_column(conn, table, column, decl)
    for stmt in INDEX_STATEMENTS:
        conn.execute(stmt)
    conn.commit()


def list_tables(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [r["name"] for r in rows]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    conn = get_connection()
    init_db(conn)
    tables = list_tables(conn)
    print(f"DB 파일: {DEFAULT_DB_PATH}")
    print(f"테이블 {len(tables)}개 생성 확인:")
    for t in tables:
        print(f"  - {t}")
    missing = [t for t in EXPECTED_TABLES if t not in tables]
    if missing:
        print(f"[오류] 누락 테이블: {missing}")
        return 1
    print("모든 필수 테이블 OK")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
