# -*- coding: utf-8 -*-
"""viewer 자체 테스트 — 불변 1(마스킹) 회귀. 실제 서버를 띄워 HTTP로 받아 검증.

핵심(★ 차단 사유): 상세/목록 HTML에 개인정보(전화번호·미마스킹 이름·오픈채팅 링크·OO쌤)가
절대 새어나오지 않아야 한다.
- 좌우 대조 개편(v2): 왼쪽 '가공 전 원문' 패널은 body_raw를 masking.mask_text로 개인정보만
  가린 '원본 흐름'을 **의도적으로** 표시한다(문단 재분할 없음). 따라서 원문 흐름의 비-개인정보
  문장은 화면에 나오되(FLOW_MARK 검증), 개인정보(PII_*)는 마스킹돼 사라져야 한다.
- 오른쪽 '정리 결과' 패널은 마스킹본(clean_text)만 쓰고 문단 raw_text는 화면에 내지 않는다
  (PARA_RAW_SENTINEL 검증).

사용: python tests/test_viewer.py
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
import db  # noqa: E402
import load_rulebook  # noqa: E402
import masking  # noqa: E402
import viewer  # noqa: E402

# 개인정보 — 어떤 화면에도 원본 그대로 나오면 안 됨(마스킹돼 사라져야 함)
PII_PHONE = "010-1234-5678"
PII_NAME = "가상인쌤"          # 직원 실명+호칭(마스킹 대상)
OPENCHAT = "https://open.kakao.com/o/secret123"
# 비-개인정보 원문 흐름 표식 — 왼쪽 패널에 마스킹 후 그대로 보여야 함(대조의 목적)
FLOW_MARK = "가공전원문흐름표식OK"
# 문단 raw_text에만 있는 표식 — 오른쪽은 clean_text만 쓰므로 화면에 절대 나오면 안 됨
PARA_RAW_SENTINEL = "문단원문에만있는표식NG"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestViewerInvariant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.dbp = os.path.join(cls.tmp, "t.sqlite3")
        load_rulebook.run(db_path=cls.dbp)          # 마스킹 패턴 적재
        conn = db.get_connection(cls.dbp)
        db.init_db(conn)
        conn.execute("INSERT INTO staff (staff_name) VALUES ('가상인')")

        # body_clean(개인정보 포함) — 서버가 종류·건수 계산에만 쓰고 화면엔 안 냄
        clean_path = os.path.join(cls.tmp, "corpus", "post", "body_clean.txt")
        raw_path = os.path.join(cls.tmp, "corpus", "post", "body_raw.txt")
        _write(clean_path, f"문의는 {PII_PHONE} 로 주세요. {PII_NAME}이 안내드려요. {OPENCHAT}")
        # 원문(body_raw): 비-개인정보 흐름(FLOW_MARK)은 남고, 개인정보(PII_*)는 마스킹돼야 함
        _write(raw_path, f"{FLOW_MARK}\n상담 문의는 {PII_PHONE} 로 주세요.\n"
                         f"{PII_NAME}이 안내드립니다.\n{OPENCHAT}")
        # 경로는 ROOT 기준 join되므로 절대경로 저장(윈도우: 절대경로면 그대로 사용됨)
        conn.execute(
            "INSERT INTO posts (post_id, title, keyword, cafe_name, board_name, staff_name, "
            "publish_date, content_length_type, extraction_status, "
            "body_raw_path, body_clean_path, body_pub_ref_path) "
            "VALUES (21512, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("임상심리사 2급 응시자격", "임상심리사2급 응시자격", "공준모", "질문게시판", "가상인",
             "2026-01-06", "medium", "성공(자동추출)",
             raw_path, clean_path, clean_path))
        # 문단: clean_text는 마스킹본(개인정보 없음), raw_text엔 원문 흔적(화면에 나오면 안 됨)
        conn.execute(
            "INSERT INTO post_paragraphs (post_id, paragraph_no, raw_text, clean_text, "
            "role, contains_fact, contains_cta) VALUES (21512, 1, ?, ?, '조건설명', 1, 0)",
            (f"{PARA_RAW_SENTINEL} {PII_PHONE}", "응시자격은 [담당자]에게 문의하세요. 전화 [가림]."))
        conn.execute(
            "INSERT INTO post_paragraphs (post_id, paragraph_no, raw_text, clean_text, "
            "role, contains_fact, contains_cta) VALUES (21512, 2, ?, ?, '배경설명', 0, 0)",
            (PARA_RAW_SENTINEL, "그 밖의 안내 문단입니다."))
        # 이미지1: 실제 추출 이미지(corpus 하위 실파일) — 로컬 검수 화면이라 인물포함/원본금지여도 표시.
        cls.img_dir = os.path.join(viewer.CORPUS_DIR, "_test_viewer_tmp")
        os.makedirs(cls.img_dir, exist_ok=True)
        img_file = os.path.join(cls.img_dir, "img.png")
        with open(img_file, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")   # PNG 시그니처(서빙 검증용 더미)
        img_rel = os.path.relpath(img_file, viewer.ROOT_DIR)
        conn.execute(
            "INSERT INTO post_images (image_id, post_id, image_order, image_type, reuse_scope, "
            "contains_person, local_path) VALUES (7001, 21512, 1, '본문이미지', "
            "'image_pattern_only', 1, ?)", (img_rel,))
        # 이미지2: local_path가 corpus 밖을 가리킴(traversal) — 실파일이 있어도 서빙 거부돼야 함
        conn.execute(
            "INSERT INTO post_images (image_id, post_id, image_order, image_type, reuse_scope, "
            "contains_person, local_path) VALUES (7002, 21512, 2, '본문이미지', "
            "'image_rights_review', 0, 'corpus/../CLAUDE.md')")
        # 조회수 = 참고 신호
        conn.execute(
            "INSERT INTO reference_signals (post_id, view_count, collected_from_sheet) "
            "VALUES (21512, 1234, ?)", (viewer.AUTO_VIEW_MARK,))
        conn.commit()
        conn.close()

        cls.httpd = viewer.make_server(db_path=cls.dbp, port=0)
        cls.port = cls.httpd.server_address[1]
        cls.th = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.img_dir, ignore_errors=True)

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.read().decode("utf-8")

    def _status(self, path):
        """상태 코드만(경로안전 검증용). 404 등은 HTTPError로 옴."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    # ---- ★ 불변 1: 개인정보 누출 없음(왼쪽 원문 흐름은 마스킹돼 표시됨) ----
    def test_detail_no_pii_leak(self):
        h = self._get("/post?id=21512")
        self.assertNotIn(PII_PHONE, h)            # 전화번호 원본 없음(마스킹)
        self.assertNotIn(PII_NAME, h)             # 미마스킹 이름 없음(마스킹)
        self.assertNotIn(OPENCHAT, h)             # 오픈채팅 링크 없음(마스킹)
        self.assertNotIn(PARA_RAW_SENTINEL, h)    # 문단 raw_text는 화면에 안 나옴
        self.assertIn(FLOW_MARK, h)               # 왼쪽 원문 흐름은 마스킹 후 표시됨

    def test_list_no_pii_leak(self):
        h = self._get("/")
        self.assertNotIn(PII_PHONE, h)
        self.assertNotIn(PII_NAME, h)
        self.assertNotIn(PARA_RAW_SENTINEL, h)

    # ---- 좌우 대조 레이아웃(개편) ----
    def test_detail_compare_two_panels_and_badge(self):
        h = self._get("/post?id=21512")
        self.assertIn("가공 전 원문 (개인정보만 가림)", h)     # 왼쪽 패널 제목
        self.assertIn("우리가 정리한 결과 (문단·역할)", h)     # 오른쪽 패널 제목
        self.assertIn("개인정보만 가린 원본 흐름", h)          # 왼쪽 안내 배지
        self.assertNotIn("참고용 본문 (개인정보 가림 완료)", h)  # v1 옛 제목 폐기

    # ---- 화면이 제대로 보이는지 ----
    def test_detail_shows_masked_body(self):
        h = self._get("/post?id=21512")
        self.assertIn("우리가 정리한 결과 (문단·역할)", h)
        self.assertIn("<mark class=\"masked\">", h)     # 가림 자리 하이라이트
        self.assertIn("(참고 신호)", h)                  # 조회수 라벨(불변 3)
        self.assertIn("1234", h)
        self.assertIn("자동 분류 실패 — 확인 필요", h)    # 배경설명 문단 강조

    def test_detail_mask_count_types_only(self):
        # 가림 결과 패널: 종류·건수만(원본 없이). 전화번호가 잡혀 총 건수 > 0.
        h = self._get("/post?id=21512")
        self.assertIn("개인정보 가림 결과", h)
        self.assertIn("총 ", h)
        self.assertIn("건", h)

    def test_extracted_image_is_shown_for_review(self):
        # 로컬 검수 화면: 추출 이미지(인물포함/원본금지여도)를 실제 <img>로 표시.
        #   단 reuse_scope 배지와 '재사용 전 검토 필요'는 그대로 남는다.
        h = self._get("/post?id=21512")
        self.assertIn("<img class='thumb'", h)         # 자리표시 아님 — 실제 그림
        self.assertIn("/img?id=7001", h)               # 서빙 링크
        self.assertIn("원본 재사용 금지", h)             # reuse 배지 유지
        self.assertIn("재사용 전 검토 필요", h)           # 검토 필요 표시 유지

    def test_img_serves_corpus_file(self):
        # corpus 하위 실파일은 200으로 서빙
        self.assertEqual(self._status("/img?id=7001"), 200)

    def test_img_rejects_path_outside_corpus(self):
        # ★ 경로안전: local_path가 corpus 밖(traversal)을 가리키면 실파일이어도 404
        self.assertEqual(self._status("/img?id=7002"), 404)

    def test_img_rejects_non_integer_id(self):
        # ★ 경로안전: image_id 정수 강제 — 비정수는 404
        self.assertEqual(self._status("/img?id=abc"), 404)
        self.assertEqual(self._status("/img?id=1%20OR%201"), 404)

    def test_img_display_does_not_leak_text_pii(self):
        # 이미지 표시를 켜도 텍스트 PII 누출은 여전히 0
        h = self._get("/post?id=21512")
        self.assertNotIn(PII_PHONE, h)
        self.assertNotIn(PII_NAME, h)
        self.assertNotIn(OPENCHAT, h)
        self.assertNotIn(PARA_RAW_SENTINEL, h)

    def test_list_shows_post(self):
        h = self._get("/")
        self.assertIn("추출 글 품질 확인", h)
        self.assertIn("임상심리사 2급 응시자격", h)

    def test_unknown_post_is_friendly(self):
        h = self._get("/post?id=99999")
        self.assertIn("그런 글이 없습니다.", h)

    # ---- 목록 화면 새 컬럼(담당자·조회수) ----
    def test_list_has_staff_and_view_columns(self):
        h = self._get("/")
        self.assertIn("<div>담당자</div>", h)              # 새 헤더 컬럼
        self.assertIn(">조회수</div>", h)                   # 새 헤더 컬럼(num 정렬)
        self.assertIn("조회수는 참고 신호입니다.", h)        # 참고 신호 병기(불변 3)
        self.assertIn("1,234", h)                           # 천단위 쉼표 조회수
        self.assertIn("가상인", h)                           # 담당자 실명(내부 검수 허용)

    def test_list_page_query_is_integer_only(self):
        # ★ 입력검증: 주소의 쪽 번호가 글자·범위 밖이어도 오류 화면 없이 가장 가까운 쪽을 보여준다
        for q in ("?page=abc", "?page=0", "?page=-5", "?page=99", "?page=1%20OR%201"):
            h = self._get("/" + q)
            self.assertIn("임상심리사 2급 응시자격", h)
            self.assertNotIn(PII_PHONE, h)

    # ---- 분석 화면 렌더 + 개인정보 누출 0(불변 1·3) ----
    def test_analysis_renders_sections(self):
        h = self._get("/analysis")
        self.assertIn("주제로 우리 글 찾기", h)              # 화면 제목(메뉴 이름은 '분석' 그대로)
        self.assertIn("주제별 우리 글", h)                    # 섹션 A
        self.assertIn("팩트 항목 이름, 우리 글 주제와 맞나", h)   # 섹션 B
        self.assertIn("담당자별 우리 글", h)                  # 섹션 C
        # 결론 난 조회수 표들은 지워지지 않고 접기 안에 남아 있다
        self.assertIn("조회수 높은 글", h)
        self.assertIn("키워드별 조회수", h)
        self.assertIn("형식과 조회수, 관계가 있을까?", h)
        self.assertIn("창고 글 1건", h)
        self.assertIn("1,234", h)                           # 조회수 천단위
        # 불변 3 — 성과로 단정하지 않고 참고 신호로 표기
        self.assertIn("참고 신호", h)
        self.assertNotIn("성과 등급", h)

    def test_analysis_menu_label_stays(self):
        # 메뉴 이름은 '분석' 그대로(손에 익은 자리) — 바뀌는 건 화면 제목뿐
        h = self._get("/analysis")
        self.assertIn(">분석</a>", h)

    def test_analysis_folds_concluded_sections_without_deleting(self):
        h = self._get("/analysis")
        self.assertIn("<details class='foldsec'", h)
        self.assertIn("문단 수·이미지 수·글자 수로는", h)     # 접기 제목의 결론 한 줄
        self.assertNotIn("<details class='foldsec' open>", h)  # 기본은 접힘

    def test_analysis_topic_and_staff_are_entrances_to_list(self):
        h = self._get("/analysis")
        self.assertIn("topic=", h)
        self.assertIn("staff=", h)
        self.assertIn("주제·담당자를 누르면 그 조건의 글 목록이 열립니다", h)

    def test_analysis_says_what_it_can_and_cannot_tell(self):
        # 정직 박스가 화면 맨 위(섹션 A 표보다 먼저)에 온다
        h = self._get("/analysis")
        self.assertIn("알 수 있는 것", h)
        self.assertIn("알 수 없는 것", h)
        # 순서는 <body> 안에서 잰다 — 화면 위쪽 <style>의 주석에도 같은 말이 들어 있어
        # 전체 HTML에서 재면 주석을 짚는다(2026-07-21에 실제로 그렇게 헛짚었다).
        body = h.split("<body>", 1)[1]
        self.assertLess(body.index("알 수 없는 것"), body.index("주제별 우리 글"))

    def test_analysis_fact_gap_says_facts_are_unreviewed(self):
        # 섹션 B가 근거로 쓰는 룰북 팩트는 아직 사람이 확인 안 한 AI 초안이라는 사실을 밝힌다
        h = self._get("/analysis")
        self.assertIn("AI가 만든 초안이라 아직 사람이 확인하지 않았습니다", h)
        self.assertIn("건이 ‘미확인’", h)
        self.assertIn("href='/fact?id=", h)

    def test_analysis_topic_sort_urls_ok(self):
        for path in ("/analysis?tsort=many", "/analysis?tsort=few", "/analysis?tsort=name",
                     "/analysis?tsort=%27%20OR%201"):     # 정해진 값 밖은 기본값으로
            h = self._get(path)
            self.assertIn("주제별 우리 글", h)
            self.assertNotIn("OR 1", h)

    def test_analysis_no_pii_leak(self):
        h = self._get("/analysis")
        self.assertNotIn(PII_PHONE, h)
        self.assertNotIn(PII_NAME, h)                       # '가상인쌤'(호칭 포함) 누출 없음
        self.assertNotIn(OPENCHAT, h)
        self.assertNotIn(PARA_RAW_SENTINEL, h)

    def test_analysis_sort_and_filter_urls_ok(self):
        # 정렬/기간 URL 모두 500 없이 렌더(urlopen은 500이면 예외)
        for path in ("/analysis?sort=views", "/analysis?sort=vpd",
                     "/analysis?min_age=30", "/analysis?sort=vpd&min_age=30"):
            h = self._get(path)
            self.assertIn("주제로 우리 글 찾기", h)

    # ---- 주제별 조회수(정규화) 섹션 + 트렌드 화면(추가분) ----
    def test_analysis_has_topic_section(self):
        # 변형 키워드를 주제로 묶는 '주제별 조회수' 섹션이 분석 화면에 있다
        # (표본 1건이라 표는 비어도 섹션 제목은 항상 나온다)
        self.assertIn("주제별 조회수", self._get("/analysis"))

    def test_nav_has_trends_link(self):
        # 상단 메뉴에 트렌드 화면 링크가 있다
        self.assertIn("/trends", self._get("/analysis"))

    def test_trends_renders_and_no_pii(self):
        h = self._get("/trends")
        self.assertIn("주제·시기 트렌드", h)
        self.assertIn("발행 습관", h)              # 정직 박스(발행량≠검색수요)
        self.assertNotIn("성과 등급", h)
        # 불변 1 — 본문을 안 쓰는 화면이지만 회귀 방지로 PII 부재 확인
        self.assertNotIn(PII_PHONE, h)
        self.assertNotIn(PII_NAME, h)
        self.assertNotIn(OPENCHAT, h)

    def test_nav_has_all_menus(self):
        # 한 사이트에서 5개 화면을 메뉴로 오간다
        h = self._get("/analysis")
        for path in ("/", "/analysis", "/trends", "/topics", "/data"):
            self.assertIn(f"href='{path}'", h)

    def test_data_renders_and_no_pii(self):
        h = self._get("/data")
        self.assertIn("창고 현황", h)
        self.assertIn("룰북 열람", h)
        self.assertNotIn(PII_PHONE, h)
        self.assertNotIn(PII_NAME, h)
        self.assertNotIn(OPENCHAT, h)
        self.assertNotIn(PARA_RAW_SENTINEL, h)

    def test_data_tells_truth_about_facts(self):
        # '데이터' 화면이 팩트를 '미적재'라고 말하던 거짓 안내 회귀 방지(2026-07-20)
        conn = db.get_connection(self.dbp)
        try:
            n = conn.execute("SELECT COUNT(*) c FROM rulebook_facts").fetchone()["c"]
        finally:
            conn.close()
        self.assertGreater(n, 0, "룰북 적재가 팩트를 넣지 않았다 — 이 테스트의 전제가 깨짐")
        h = self._get("/data")
        self.assertNotIn("팩트(미적재)", h)
        self.assertNotIn("아직 창고에 없습니다", h)
        self.assertIn(f"{n:,}건이 창고에 있고", h)
        self.assertIn("href='/facts'", h)

    def test_facts_screen_serves_over_http(self):
        # 이 창고는 load_rulebook으로 만들어져 팩트도 함께 들어온다(2차 적재가 같은 실행).
        # 여기서는 라우팅·개인정보 누출만 본다. 빈 화면은 TestFactsScreens에서 따로 검증.
        h = self._get("/facts")
        self.assertIn("팩트 룰북", h)
        self.assertNotIn(PII_PHONE, h)
        self.assertNotIn(PII_NAME, h)
        self.assertNotIn(OPENCHAT, h)

    def test_fact_detail_serves_over_http(self):
        # 상세도 주소로 실제로 열려야 한다(라우팅 오타를 잡는 유일한 테스트)
        conn = db.get_connection(self.dbp)
        try:
            row = conn.execute("SELECT fact_id, item_name FROM rulebook_facts "
                               "ORDER BY fact_id LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "룰북 적재가 팩트를 넣지 않았다 — 이 테스트의 전제가 깨짐")
        h = self._get(f"/fact?id={row['fact_id']}")
        self.assertIn(row["item_name"], h)
        self.assertNotIn(PII_PHONE, h)
        self.assertNotIn(PII_NAME, h)
        self.assertNotIn(OPENCHAT, h)

    def test_nav_has_facts_link(self):
        self.assertIn("href='/facts'", self._get("/data"))

    def test_viewer_is_read_only_no_post(self):
        # ★ 3차는 읽기 전용 — 뷰어에 쓰기(POST) 경로가 없다(고치기·도장은 4차)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/facts", data=b"x=1")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertIn(cm.exception.code, (405, 501))

    def test_tables_scroll_instead_of_wrapping(self):
        # 사용 피드백 1차 — 좁아지면 칸이 접히는 게 아니라 표가 가로로 밀린다(글 목록·분석 공통)
        for path in ("/", "/analysis"):
            h = self._get(path)
            self.assertIn("class='tablewrap'", h)
            self.assertIn(".listhead > div, .listrow > div { overflow: hidden;", h)

    def test_topics_renders_and_no_pii(self):
        h = self._get("/topics")
        self.assertIn("주제 검수", h)
        self.assertIn("주제 목록", h)
        self.assertNotIn(PII_PHONE, h)
        self.assertNotIn(PII_NAME, h)
        self.assertNotIn(OPENCHAT, h)


class TestListPagingAndMaskCount(unittest.TestCase):
    """목록 4차 — 저장된 가림 건수 사용 + 쪽 나누기. 임시 창고만 사용(실제 창고에 쓰지 않음).

    ★ 이 화면은 읽기 전용이다: render_list는 창고에 아무것도 쓰지 않는다.
    글 250건(성공 150·실패 100), 최신순은 post_id 내림차순(updated_at 동일)."""

    N_POSTS = 250
    N_OK = 150

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.dbp = os.path.join(cls.tmp, "list.sqlite3")
        load_rulebook.run(db_path=cls.dbp)              # 가림 규칙(지문의 재료)
        cls.conn = db.get_connection(cls.dbp)
        db.init_db(cls.conn)
        cls.fp = masking.rules_fingerprint(cls.conn)
        raw = os.path.join(cls.tmp, "body_raw.txt")     # 목록 대상 조건(body_raw_path IS NOT NULL)
        _write(raw, "본문")
        rows = [(i, f"글{i:03d}", "성공(자동추출)" if i <= cls.N_OK else "실패-삭제된글", raw)
                for i in range(1, cls.N_POSTS + 1)]
        cls.conn.executemany(
            "INSERT INTO posts (post_id, title, extraction_status, body_raw_path) "
            "VALUES (?,?,?,?)", rows)
        cls.conn.executemany(
            "INSERT INTO reference_signals (post_id, view_count, collected_from_sheet) "
            "VALUES (?,?,?)",
            [(i, i, viewer.AUTO_VIEW_MARK) for i in range(1, cls.N_POSTS + 1)])
        # 가림 건수 3가지 상태(나머지 247건은 mask_count 없음 = 아직 안 셈)
        cls.conn.execute(                       # 지금 규칙으로 셈 → 숫자 그대로
            "UPDATE posts SET mask_count=3, mask_rules_fingerprint=? WHERE post_id=250", (cls.fp,))
        cls.conn.execute(                       # 센 뒤 규칙이 바뀜 → 옛 숫자(5)는 화면에 못 나옴
            "UPDATE posts SET mask_count=5, mask_rules_fingerprint='옛날지문' WHERE post_id=249")
        cls.conn.execute(                       # 세어 봤는데 0건 → 흐린 '0건'
            "UPDATE posts SET mask_count=0, mask_rules_fingerprint=? WHERE post_id=248", (cls.fp,))
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- 쪽 나누기 ----
    def test_first_page_shows_100_and_range_line(self):
        h = viewer.render_list(self.conn)
        self.assertEqual(h.count("class='listrow'"), 100)
        self.assertIn("250건 중 1~100번째 보는 중 · 1 / 3쪽", h)
        self.assertIn("글250", h)          # 최신순 첫 줄
        self.assertNotIn("글150", h)       # 다음 쪽
        # 첫 쪽에선 '처음·이전'이 흐림(없애지 않고 남김), '다음·끝'만 링크
        self.assertIn("<span class='off'>처음</span>", h)
        self.assertIn("<span class='off'>이전</span>", h)
        self.assertIn("다음 →", h)
        self.assertIn("<span class='cur'>1 / 3쪽</span>", h)

    def test_last_page_partial_and_disabled_next(self):
        h = viewer.render_list(self.conn, page_no=3)
        self.assertEqual(h.count("class='listrow'"), 50)
        self.assertIn("250건 중 201~250번째 보는 중 · 3 / 3쪽", h)
        self.assertIn("<span class='off'>다음</span>", h)
        self.assertIn("<span class='off'>끝</span>", h)
        self.assertIn("← 처음", h)

    def test_out_of_range_page_falls_back_to_nearest(self):
        # 오류 화면 대신 가장 가까운 쪽(기존 view/sort와 같은 결 — 허용된 것만 통과)
        self.assertIn("3 / 3쪽", viewer.render_list(self.conn, page_no=99))
        self.assertIn("1 / 3쪽", viewer.render_list(self.conn, page_no=0))
        self.assertIn("1 / 3쪽", viewer.render_list(self.conn, page_no=-7))
        self.assertIn("1 / 3쪽", viewer.render_list(self.conn, page_no="abc"))   # 정수 강제
        self.assertIn("1 / 3쪽", viewer.render_list(self.conn, page_no=None))
        self.assertIn("2 / 3쪽", viewer.render_list(self.conn, page_no="2"))     # 주소는 문자열

    def test_single_page_hides_pager(self):
        h = viewer.render_list(self.conn, view="fail", page_no=1)   # 실패 100건 = 딱 한 쪽
        self.assertIn("실패만 100건 모두 보는 중", h)
        self.assertNotIn("class='pager'", h)

    # ---- 필터·정렬이 쪽을 넘어도 유지 ----
    def test_filter_survives_paging(self):
        h = viewer.render_list(self.conn, view="ok", page_no=2)
        self.assertIn("성공만 150건 중 101~150번째 보는 중 · 2 / 2쪽", h)
        self.assertNotIn("실패-삭제된글", h)                  # 거르기 규칙 그대로
        self.assertIn("href='/?view=ok'", h)                  # 쪽 이동이 보기를 달고 다님(1쪽)
        self.assertEqual(h.count("class='listrow'"), 50)

    def test_sort_survives_paging(self):
        h = viewer.render_list(self.conn, sort="views", page_no=2)
        self.assertIn("href='/?sort=views'", h)               # 쪽 이동이 정렬을 달고 다님
        self.assertIn("href='/?sort=views&page=3'", h)
        self.assertIn("글150", h)          # 조회수=post_id → 2쪽은 150~51위
        self.assertNotIn("글250", h)

    def test_filter_and_sort_together_in_page_links(self):
        h = viewer.render_list(self.conn, view="ok", sort="views", page_no=1)
        self.assertIn("href='/?view=ok&sort=views&page=2'", h)

    def test_changing_filter_starts_at_page_one(self):
        # 보기·정렬 링크에는 page가 붙지 않는다(다른 목록이 됐는데 뒷쪽에 서 있으면 빈 화면)
        h = viewer.render_list(self.conn, view="ok", page_no=2)
        self.assertIn("href='/?view=fail'", h)
        self.assertNotIn("href='/?view=fail&page=2'", h)

    # ---- 저장된 가림 건수(지문 대조) ----
    def test_saved_count_is_shown_when_fingerprint_matches(self):
        h = viewer.render_list(self.conn)
        self.assertIn(">3건</div>", h)                        # 지금 규칙으로 센 글 → 숫자
        self.assertIn("<div class='num-dim'>0건</div>", h)    # 세어 봤는데 0건 → 흐린 숫자

    def test_stale_fingerprint_never_shows_old_number(self):
        h = viewer.render_list(self.conn)
        self.assertIn("<div class='recount'>다시 세기 필요</div>", h)
        self.assertNotIn(">5건</div>", h)                     # 옛 숫자는 화면에 못 나온다
        # 안내 줄: 아직 안 센 글 + 규칙이 바뀐 글을 한 문구로, 창고 전체 기준 건수
        self.assertIn(f"다시 세야 하는 글이 {self.N_POSTS - 2}건 있습니다", h)
        self.assertNotIn("count_masks", h)                    # 화면에 명령줄·파일명 노출 금지

    def test_no_notice_when_all_counted(self):
        conn = db.get_connection(os.path.join(self.tmp, "all.sqlite3"))
        db.init_db(conn)
        raw = os.path.join(self.tmp, "body_raw.txt")
        conn.execute("INSERT INTO posts (post_id, title, extraction_status, body_raw_path, "
                     "mask_count, mask_rules_fingerprint) VALUES (1,'글','성공(자동추출)',?,2,?)",
                     (raw, masking.rules_fingerprint(conn)))
        conn.commit()
        h = viewer.render_list(conn)
        self.assertNotIn("다시 세야 하는 글이", h)
        self.assertIn(">2건</div>", h)
        self.assertIn("1건 모두 보는 중", h)
        conn.close()

    # ---- 표 폭·줄바꿈(사용 피드백 1차) ----
    def test_list_is_wrapped_and_status_badge_never_wraps(self):
        h = viewer.render_list(self.conn)
        # 목록도 분석 표처럼 가로 스크롤로 감싸고 최소 폭을 준다(칸이 짜부러지지 않음)
        self.assertIn("<div class='postlist'><div class='tablewrap'>", h)
        self.assertIn(".postlist .listhead, .postlist .listrow { min-width:", h)
        # '성공(자동추출)' 배지가 두 줄로 접히던 문제 — 줄바꿈 방지
        self.assertRegex(h, r"\.badge \{[^}]*white-space: nowrap")
        self.assertIn("성공(자동추출)", h)

    # ---- 성능의 본체: 목록은 본문 파일을 열지 않는다 ----
    def test_list_does_not_read_body_files(self):
        called = []
        orig = viewer.mask_type_counts

        def spy(*a, **k):
            called.append(a)
            return orig(*a, **k)
        viewer.mask_type_counts = spy
        try:
            viewer.render_list(self.conn)
        finally:
            viewer.mask_type_counts = orig
        self.assertEqual(called, [])   # 글마다 다시 세지 않음(36.3초의 원인이었다)


class TestListFilters(unittest.TestCase):
    """목록 걸러 보기(2차) — 담당자·카페·주제. 임시 창고만 사용(실제 창고에 쓰지 않음).

    ★ 이름은 전부 가짜다(저장소 공개 이력 — 실제 명단을 테스트에 옮겨 적지 않는다).
    글 10건: 카페하나 6(담당가 4·담당나 2, 전부 주제 '플래너') / 카페둘 4(담당나, 주제 '보육교사2급')."""

    CAFE1, CAFE2 = "가상카페하나", "가상카페둘"
    STAFF1, STAFF2 = "가상담당가", "가상담당나"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.conn = db.get_connection(os.path.join(cls.tmp, "filter.sqlite3"))
        db.init_db(cls.conn)
        raw = os.path.join(cls.tmp, "body_raw.txt")      # 목록 대상 조건(body_raw_path IS NOT NULL)
        _write(raw, "본문")
        rows = []
        for i in range(1, 5):        # 1~4: 카페하나 · 담당가 · '플래너 자격증'
            rows.append((i, f"글{i}", "플래너 자격증", cls.CAFE1, cls.STAFF1))
        for i in range(5, 7):        # 5~6: 카페하나 · 담당나 · '플래너비용'(같은 주제, 다른 원본 키워드)
            rows.append((i, f"글{i}", "플래너비용", cls.CAFE1, cls.STAFF2))
        for i in range(7, 11):       # 7~10: 카페둘 · 담당나 · 다른 주제
            rows.append((i, f"글{i}", "보육교사2급 취업", cls.CAFE2, cls.STAFF2))
        cls.conn.executemany(
            "INSERT INTO posts (post_id, title, keyword, cafe_name, staff_name, "
            "extraction_status, body_raw_path) VALUES (?,?,?,?,?,'성공(자동추출)',?)",
            [r + (raw,) for r in rows])
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_filter_by_staff(self):
        h = viewer.render_list(self.conn, staff=self.STAFF1)
        self.assertEqual(h.count("class='listrow'"), 4)
        self.assertIn("4건 모두 보는 중", h)
        self.assertIn("총 10건", h)            # 상단 배지는 창고 전체 기준 그대로

    def test_filter_by_cafe(self):
        h = viewer.render_list(self.conn, cafe=self.CAFE1)
        self.assertEqual(h.count("class='listrow'"), 6)
        self.assertIn("6건 모두 보는 중", h)

    def test_filter_by_topic_uses_same_normalize(self):
        # 주제 '플래너'에는 원본 키워드 2종('플래너 자격증'·'플래너비용')이 묶인다 → 6건
        import keyword_normalize as kn
        self.assertEqual(kn.normalize("플래너 자격증"), "플래너")
        self.assertEqual(kn.normalize("플래너비용"), "플래너")
        h = viewer.render_list(self.conn, topic="플래너")
        self.assertEqual(h.count("class='listrow'"), 6)
        self.assertIn("6건 모두 보는 중", h)

    def test_topic_filter_shows_member_keywords(self):
        # ★ 헌장 디자인 규칙 — 자동으로 묶은 값은 원본과 대조할 길을 같은 화면에 둔다
        h = viewer.render_list(self.conn, topic="플래너")
        self.assertIn("원본 키워드 2종이 묶여 있습니다", h)
        self.assertIn("플래너 자격증(4)", h)
        self.assertIn("플래너비용(2)", h)

    def test_two_conditions_are_all_or_nothing(self):
        # 두 조건은 '모두 만족'(그리고) — 주제 플래너 ∩ 담당나 = 2건
        h = viewer.render_list(self.conn, topic="플래너", staff=self.STAFF2)
        self.assertEqual(h.count("class='listrow'"), 2)
        self.assertIn("2건 모두 보는 중", h)

    def test_condition_chip_shows_kind_before_value(self):
        h = viewer.render_list(self.conn, topic="플래너", staff=self.STAFF2)
        self.assertIn("걸러 보는 중:", h)
        self.assertIn("주제 ‘플래너’", h)                  # 종류가 값 앞에
        self.assertIn(f"담당자 ‘{self.STAFF2}’", h)
        self.assertIn("모두 지우기", h)                     # 조건 2개 이상일 때만
        self.assertIn("class='fchip'", h)

    def test_single_condition_has_no_clear_all(self):
        h = viewer.render_list(self.conn, cafe=self.CAFE1)
        self.assertIn(f"카페 ‘{self.CAFE1}’", h)
        self.assertNotIn("모두 지우기", h)

    def test_zero_result_keeps_condition_row(self):
        # 걸러서 0건이어도 조건 줄은 남는다 — 무엇 때문에 비었는지 안 보이면 되돌릴 수 없다
        h = viewer.render_list(self.conn, cafe=self.CAFE2, staff=self.STAFF1)
        self.assertEqual(h.count("class='listrow'"), 0)
        self.assertIn("걸러 보는 중:", h)
        self.assertIn("고른 조건에 맞는 글이 없습니다.", h)
        self.assertIn("모두 지우기", h)

    def test_unknown_condition_says_so(self):
        h = viewer.render_list(self.conn, topic="창고에없는주제")
        self.assertIn("그런 주제가 창고에 없습니다", h)
        self.assertIn("걸러 보는 중:", h)

    def test_filters_are_bound_parameters_not_string_sql(self):
        # ★ 입력검증 — 따옴표·SQL 조각을 넣어도 그대로 '없는 값'일 뿐(주입되지 않는다)
        for bad in ("' OR 1=1 --", "'; DROP TABLE posts; --", "%"):
            h = viewer.render_list(self.conn, staff=bad)
            self.assertEqual(h.count("class='listrow'"), 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"], 10)

    def test_filter_value_length_is_capped(self):
        long = "가" * 200
        h = viewer.render_list(self.conn, staff=long)
        self.assertEqual(h.count("class='listrow'"), 0)
        self.assertNotIn("가" * (viewer.MAX_FILTER_LEN + 1), h)   # 상한을 넘겨 되비추지 않음

    def test_filter_is_carried_by_view_sort_and_paging_links(self):
        h = viewer.render_list(self.conn, topic="플래너")
        self.assertIn("topic=", h)                 # 보기·정렬 링크가 조건을 달고 다닌다
        self.assertIn("view=fail&", h)             # 조건이 뒤에 붙은 형태

    def test_apply_is_a_get_form_never_post(self):
        # ★ 읽기 전용 — [적용]은 GET 폼(주소가 바뀔 뿐 창고에 쓰지 않는다)
        h = viewer.render_list(self.conn)
        self.assertIn("method='get'", h)
        self.assertNotIn("method='post'", h)
        self.assertIn("<button type='submit'>적용</button>", h)

    def test_board_is_not_offered_as_a_filter(self):
        # 게시판은 값이 대부분 비어 있어 넣지 않기로 했다(넣으면 대부분 글이 사라져 보인다)
        h = viewer.render_list(self.conn)
        self.assertIn("name='cafe'", h)
        self.assertIn("name='staff'", h)
        self.assertNotIn("name='board", h)

    def test_filtered_list_does_not_read_body_files_or_write(self):
        # 성능(36초→0.06초의 본체)과 읽기 전용을 함께 지킨다
        called = []
        orig = viewer.mask_type_counts
        viewer.mask_type_counts = lambda *a, **k: called.append(a)
        try:
            viewer.render_list(self.conn, topic="플래너", staff=self.STAFF2)
        finally:
            viewer.mask_type_counts = orig
        self.assertEqual(called, [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"], 10)

    def test_filtered_list_has_no_pii(self):
        for h in (viewer.render_list(self.conn, topic="플래너"),
                  viewer.render_list(self.conn, cafe=self.CAFE1),
                  viewer.render_list(self.conn, staff=self.STAFF1)):
            self.assertNotIn(PII_PHONE, h)
            self.assertNotIn(PII_NAME, h)
            self.assertNotIn(OPENCHAT, h)

    def test_analysis_staff_name_links_to_filtered_list(self):
        # 분석 담당자별 표의 이름이 그 담당자 글 목록으로 가는 입구가 된다
        self.conn.execute(
            "INSERT INTO reference_signals (post_id, view_count, collected_from_sheet) "
            "VALUES (1, 100, ?)", (viewer.AUTO_VIEW_MARK,))
        self.conn.commit()
        try:
            h = viewer.render_analysis(self.conn)
            self.assertIn("staff=", h)
            self.assertIn("담당자 이름을 누르면", h)
        finally:
            self.conn.execute("DELETE FROM reference_signals WHERE post_id=1")
            self.conn.commit()


class TestTopicCountGap(unittest.TestCase):
    """주제 옆 숫자(창고 글 수)와 목록 건수(본문 가져온 글)가 어긋나는 것을 화면이 설명하는가.

    ★ 기존 표본은 10건 모두 본문이 있어 두 숫자가 같아져 회귀를 못 잡았다 →
      여기서는 **본문 없는 글을 일부러 넣는다.**
    글 7건: 주제 '플래너' 5건(본문 3 · 본문없음 2) / 주제 '요양보호사' 2건(전부 본문 없음)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.conn = db.get_connection(os.path.join(cls.tmp, "gap.sqlite3"))
        db.init_db(cls.conn)
        raw = os.path.join(cls.tmp, "body_raw.txt")
        _write(raw, "본문")
        rows = [(1, "플래너 자격증", raw), (2, "플래너 자격증", raw), (3, "플래너 자격증", raw),
                (4, "플래너비용", None), (5, "플래너비용", None),          # 창고엔 있고 본문 없음
                (6, "요양보호사 자격증", None), (7, "요양보호사 자격증", None)]  # 주제 전체가 본문 없음
        cls.conn.executemany(
            "INSERT INTO posts (post_id, title, keyword, extraction_status, body_raw_path) "
            "VALUES (?,?,?,'성공(자동추출)',?)",
            [(i, f"글{i}", k, p) for i, k, p in rows])
        cls.conn.commit()
        import keyword_normalize as kn
        assert kn.normalize("플래너 자격증") == "플래너"
        assert kn.normalize("요양보호사 자격증") == "요양보호사"

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_screen_explains_what_each_number_counts(self):
        h = viewer.render_list(self.conn, topic="플래너")
        self.assertEqual(h.count("class='listrow'"), 3)      # 목록은 본문 가져온 3건
        self.assertIn("창고에 주제 ‘플래너’로 쓴 글은 <b>5건</b>", h)
        self.assertIn("본문을 가져온 글은 3건</b>", h)
        self.assertIn("아래 목록에는 본문을 가져온 글만 나옵니다", h)
        self.assertIn("트렌드·분석 화면에서 주제 이름 옆에 보이는 숫자는 창고 글 수", h)

    def test_topic_with_no_body_says_why_it_is_empty(self):
        h = viewer.render_list(self.conn, topic="요양보호사")
        self.assertEqual(h.count("class='listrow'"), 0)
        self.assertIn("쓴 글은 2건 있지만", h)
        self.assertIn("본문을 가져온 글이 아직 없습니다", h)
        self.assertNotIn("그런 주제가 창고에 없습니다", h)     # ★ '없는 주제'와 다른 상황
        self.assertIn("걸러 보는 중:", h)

    def test_unknown_topic_message_is_different(self):
        h = viewer.render_list(self.conn, topic="창고에없는주제")
        self.assertIn("그런 주제가 창고에 없습니다", h)
        self.assertNotIn("본문을 가져온 글이 아직 없습니다", h)

    def test_two_screens_use_one_helper_and_agree(self):
        m = viewer.topic_members(self.conn)
        self.assertEqual(sum(n for _, n, _ in m["플래너"]), 5)
        self.assertEqual(sum(nb for _, _, nb in m["플래너"]), 3)
        h_list = viewer.render_list(self.conn, topic="플래너")
        h_topics = viewer.render_topics(self.conn)
        self.assertIn("창고에 주제 ‘플래너’로 쓴 글은 <b>5건</b>", h_list)
        self.assertIn("<div>플래너</div><div class='num'>5</div>", h_topics)   # 주제 검수도 같은 5
        self.assertIn("플래너 자격증(3)", h_list)              # 원본 키워드 대조(창고 글 수)


class TestTrendsHeatmapExplain(unittest.TestCase):
    """히트맵 숫자 설명(사용 피드백 3차) — 사용자가 칸의 몫(%)을 '순위'로 오해했던 회귀 방지.

    글이 30건 넘는 달이 두 달 있어야 히트맵이 그려지므로 80건을 넣는다(임시 창고, 읽기 전용).
    2026-06: 흔한 주제 39건 + 드문 주제 1건(2.5%) / 2026-07: 다른 주제 40건."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.conn = db.get_connection(os.path.join(cls.tmp, "heat.sqlite3"))
        db.init_db(cls.conn)
        rows, pid = [], 1
        for i in range(39):
            rows.append((pid, f"글{pid}", "사회복지사2급", f"2026-06-{i % 28 + 1:02d}"))
            pid += 1
        rows.append((pid, f"글{pid}", "한국사능력검정", "2026-06-15"))   # 그 달 1/40 = 2.5%
        pid += 1
        for i in range(40):
            rows.append((pid, f"글{pid}", "보육교사2급", f"2026-07-{i % 28 + 1:02d}"))
            pid += 1
        cls.conn.executemany(
            "INSERT INTO posts (post_id, title, keyword, publish_date) VALUES (?,?,?,?)", rows)
        cls.conn.commit()
        cls.html = viewer.render_trends(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_says_the_number_is_not_a_rank(self):
        # ★ 실제 오해가 '순위'였다 — 이 말이 화면에서 사라지면 실패
        self.assertIn("순위가 아닙니다", self.html)
        self.assertIn("차지한 몫(%)", self.html)

    def test_says_how_many_topics_and_months_are_shown(self):
        # 몇 개 주제 · 몇 개월치를 보고 있는지 화면에 적혀 있다
        self.assertIn("주제 3개", self.html)
        self.assertIn("상위 15개까지", self.html)
        self.assertIn("최근 2개월", self.html)
        self.assertIn("2026년 6월~2026년 7월", self.html)
        self.assertIn("30건이 안 되는 달은", self.html)

    def test_small_share_shows_number_and_zero_shows_dot(self):
        # 3% 미만이라 비어 보이던 칸에도 숫자가 찍히고, 0은 빈 칸이 아니라 가운뎃점
        self.assertIn(">2.5</div>", self.html)
        self.assertIn("class='hm-c zero'", self.html)
        self.assertIn(">·</div>", self.html)

    def test_has_color_legend_with_numbers(self):
        # 색만으로 구분되지 않게 범례에 실제 몫(%)을 함께 적는다
        self.assertIn("hm-legend", self.html)
        self.assertIn("진할수록 많이 쓴 달", self.html)
        # 칸의 숫자는 10 이상이면 반올림한 정수(39/40 = 97.5% → 98)
        self.assertIn(">98</div>", self.html)
        # 범례 캡션은 이 표의 실제 최댓값을 말한다(7월 40/40 = 100%)
        self.assertIn("가장 큰 몫 100%", self.html)

    def test_topic_names_link_to_filtered_post_list(self):
        # 2차 — 히트맵의 주제 이름이 그 주제로 걸러진 글 목록으로 가는 입구가 된다
        self.assertIn("href='/?topic=", self.html)
        self.assertIn("주제 이름을 누르면", self.html)

    def test_other_trend_numbers_are_explained(self):
        # 같은 화면의 다른 숫자도 무슨 숫자인지 한 줄씩 있다 + 불변 3(성과로 단정 안 함)
        self.assertIn("월초·중순·월말에 각각 쓴", self.html)
        self.assertIn("발행 습관", self.html)
        self.assertNotIn("성과 등급", self.html)


class TestFactsScreens(unittest.TestCase):
    """팩트 룰북 화면(3차) — 읽기 전용 목록·상세. 임시 창고만 사용(실제 창고에 쓰지 않음).

    실물과 같은 규모(공통 16 + 개별 35 = 51건, 전부 '미확인')로 만들어 목록 숫자를 검증한다.
    값은 전부 가상 — 실제 명단·개인정보는 테스트에 넣지 않는다(불변 1)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.conn = db.get_connection(os.path.join(cls.tmp, "facts.sqlite3"))
        db.init_db(cls.conn)
        # 1번: 실제로 모순이 발견된 항목의 재현(요건 160 ↔ 주의메모 120/160 ↔ FAQ 120)
        cls.conn.execute(
            "INSERT INTO rulebook_facts (fact_id, fact_kind, excel_no, category, item_name, "
            "requirement, caution_memo, faq_top3, credits, source_version) "
            "VALUES (1,'공통',27,'사회복지','사회복지사2급',?,?,?,?,'테스트')",
            ("실습 160시간 필수", "2020.1.1 이전 입학자는 실습 120시간",
             "Q1 실습은 몇 시간인가요? → 120시간 필수", ""))
        rows = [(i, "공통" if i <= 16 else "개별", f"카테고리{i % 5}", f"가상항목{i:02d}")
                for i in range(2, 52)]
        cls.conn.executemany(
            "INSERT INTO rulebook_facts (fact_id, fact_kind, category, item_name, "
            "core_fact, source_version) VALUES (?,?,?,?,'가상 내용','테스트')", rows)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_empty_state_guides_to_ingest(self):
        # 팩트가 0건인 창고 — 화면에서는 넣을 수 없다는 것까지 알려준다
        empty = db.get_connection(os.path.join(self.tmp, "empty.sqlite3"))
        db.init_db(empty)
        try:
            h = viewer.render_facts(empty)
            self.assertIn("아직 팩트가 창고에 없습니다.", h)
            self.assertIn("화면에서는 넣을 수 없어요.", h)
        finally:
            empty.close()

    def test_list_shows_51_and_all_unreviewed(self):
        h = viewer.render_facts(self.conn)
        self.assertEqual(h.count("class='listrow'"), 51)
        self.assertIn("51건 중 0건 확인함", h)                  # 상단 띠 진행 배지
        self.assertIn("<div class='n'>51</div>", h)            # 숫자 카드
        self.assertIn("<div class='l'>미확인</div>", h)
        self.assertIn("51건 전부 ‘미확인’에서 시작합니다", h)     # 정직 박스
        self.assertIn("<span class='badge dim'>미확인</span>", h)  # 미확인은 회색(빨강 아님)
        self.assertNotIn("badge danger", h)

    def test_list_columns_and_edited_dash(self):
        h = viewer.render_facts(self.conn)
        for col in ("항목명", "종류", "카테고리", "상태", "고친 칸", "확인 날짜"):
            self.assertIn(f">{col}</div>", h)
        self.assertIn("<div class='num num-dim'>–</div>", h)   # 고친 칸 0 → 회색 –

    def test_list_order_is_common_first(self):
        # D-A: 공통 먼저 → 개별. 개별 첫 항목보다 공통 마지막 항목이 앞에 있다
        h = viewer.render_facts(self.conn)
        self.assertLess(h.index("가상항목16"), h.index("가상항목17"))
        self.assertLess(h.index("사회복지사2급"), h.index("가상항목17"))

    def test_list_filters_narrow_to_known_values(self):
        # ★ 입력검증: 정해진 목록 밖의 필터값은 '전체'로 떨어진다(주입 차단)
        self.assertEqual(viewer.render_facts(self.conn, view="'; DROP--").count("class='listrow'"), 51)
        self.assertEqual(viewer.render_facts(self.conn, kind="abc").count("class='listrow'"), 51)
        self.assertEqual(viewer.render_facts(self.conn, kind="common").count("class='listrow'"), 16)
        self.assertEqual(
            viewer.render_facts(self.conn, kind="individual").count("class='listrow'"), 35)
        self.assertEqual(
            viewer.render_facts(self.conn, view="unreviewed").count("class='listrow'"), 51)
        h = viewer.render_facts(self.conn, view="reviewed")     # 확인함 0건
        self.assertIn("이 보기에 해당하는 항목이 없습니다.", h)

    def test_detail_shows_conflicting_fields_together(self):
        # 이 화면의 존재 이유 — 요건·주의메모·FAQ가 한 화면에 함께 보여야 모순이 보인다
        h = viewer.render_fact(self.conn, 1)
        self.assertIn("응시/취득 요건", h)
        self.assertIn("주의메모 (시점/예외)", h)
        self.assertIn("자주 묻는 질문 TOP3", h)
        self.assertIn("실습 160시간 필수", h)
        self.assertIn("2020.1.1 이전 입학자는 실습 120시간", h)
        self.assertIn("120시간 필수", h)
        # D-B: 주의메모가 요건 바로 아래(FAQ보다 위)
        self.assertLess(h.index("주의메모 (시점/예외)"), h.index("자주 묻는 질문 TOP3"))
        self.assertGreater(h.index("주의메모 (시점/예외)"), h.index("응시/취득 요건"))

    def test_detail_empty_field_is_kept_with_notice(self):
        h = viewer.render_fact(self.conn, 1)
        self.assertIn("필요 학점", h)                    # 빈 칸도 지우지 않는다
        self.assertIn("이 칸은 비어 있습니다.", h)

    def test_detail_has_next_unreviewed_link_and_no_history(self):
        h = viewer.render_fact(self.conn, 1)
        self.assertIn("다음 미확인 →", h)
        self.assertNotIn("수정 이력", h)                 # 이력 0건이면 아예 안 보인다

    def test_detail_individual_uses_its_own_fields(self):
        h = viewer.render_fact(self.conn, 20)            # 개별 팩트
        self.assertIn("핵심 팩트", h)
        self.assertIn("사용 우선순위", h)
        self.assertNotIn("자주 묻는 질문 TOP3", h)

    def test_unknown_or_non_integer_id_is_friendly(self):
        for bad in (99999, "abc", None, "1 OR 1"):
            self.assertIn("그런 팩트 항목이 없습니다.", viewer.render_fact(self.conn, bad))

    def test_facts_screens_have_no_pii(self):
        # ★ 불변 1 회귀 — 팩트 값은 적재 때 이미 가려지지만 화면에서도 새지 않는지 확인
        for h in (viewer.render_facts(self.conn), viewer.render_fact(self.conn, 1)):
            self.assertNotIn(PII_PHONE, h)
            self.assertNotIn(PII_NAME, h)
            self.assertNotIn(OPENCHAT, h)

    def test_facts_screens_do_not_write(self):
        # ★ 읽기 전용 — 화면을 그려도 창고 내용이 그대로다(건수·상태 불변)
        before = self.conn.execute(
            "SELECT COUNT(*), SUM(review_status='미확인') FROM rulebook_facts").fetchone()
        viewer.render_facts(self.conn)
        viewer.render_fact(self.conn, 1)
        after = self.conn.execute(
            "SELECT COUNT(*), SUM(review_status='미확인') FROM rulebook_facts").fetchone()
        self.assertEqual(tuple(before), tuple(after))


class TestAnalysisWithoutViewCounts(unittest.TestCase):
    """분석 탭 역할 전환(6차) — ★ 조회수가 한 건도 없는 창고에서도 쓸모가 있어야 한다.

    글 7건: 주제 '플래너' 5건(본문 3) · 주제 '요양보호사' 2건(본문 0). 참고 신호(조회수) 0건.
    팩트 2건(둘 다 '미확인') — 하나는 글이 많은 주제, 하나는 글이 0건인 주제."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.conn = db.get_connection(os.path.join(cls.tmp, "noviews.sqlite3"))
        db.init_db(cls.conn)
        raw = os.path.join(cls.tmp, "body_raw.txt")
        _write(raw, "본문")
        rows = [(1, "플래너 자격증", raw, "담당가"), (2, "플래너 자격증", raw, "담당가"),
                (3, "플래너비용", raw, "담당나"), (4, "플래너비용", None, "담당나"),
                (5, "플래너 자격증", None, "담당나"),
                (6, "요양보호사 자격증", None, "담당가"), (7, "요양보호사 자격증", None, "담당가")]
        cls.conn.executemany(
            "INSERT INTO posts (post_id, title, keyword, extraction_status, body_raw_path, "
            "staff_name) VALUES (?,?,?,'성공(자동추출)',?,?)",
            [(i, f"글{i}", k, p, s) for i, k, p, s in rows])
        # 본문을 가져온 3건에만 문단·이미지 — 평균의 분모가 '본문 가져온 글'임을 검증하려고
        cls.conn.executemany(
            "INSERT INTO post_paragraphs (post_id, paragraph_no, clean_text) VALUES (?,?,'문단')",
            [(p, i) for p in (1, 2, 3) for i in range(1, 5)])      # 글마다 4문단
        cls.conn.executemany(
            "INSERT INTO post_images (post_id, image_order, local_path) VALUES (?,?,'x.png')",
            [(p, i) for p in (1, 2, 3) for i in range(1, 3)])      # 글마다 2장
        cls.conn.executemany(
            "INSERT INTO rulebook_facts (fact_kind, category, item_name, source_version) "
            "VALUES ('공통',?,?,'테스트')",
            [("복지", "플래너"), ("복지", "한번도안쓴자격증")])
        cls.conn.commit()
        cls.html = viewer.render_analysis(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_sections_a_b_c_render_without_any_view_count(self):
        # ★ 역할 전환의 실질 — 조회수 0건이어도 세 섹션이 다 나온다
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM reference_signals").fetchone()["c"], 0)
        for sec in ("주제별 우리 글", "팩트 항목 이름, 우리 글 주제와 맞나", "담당자별 우리 글"):
            self.assertIn(sec, self.html)
        self.assertIn("창고 글 7건", self.html)
        self.assertNotIn("아직 분석할 글이 없습니다", self.html)   # 예전엔 여기서 화면이 끝났다

    def test_topic_row_counts_and_shape_numbers(self):
        # 주제 '플래너' = 창고 글 5건, 본문 3건 → 평균 문단 4.0 · 평균 이미지 2.0
        self.assertIn("<div class='num'>5</div>", self.html)
        self.assertIn("<div class='num'>4.0</div>", self.html)
        self.assertIn("<div class='num'>2.0</div>", self.html)
        # 본문이 하나도 없는 주제는 평균을 지어내지 않는다
        self.assertIn("<div class='num'>-</div>", self.html)
        self.assertIn("원고 틀을 잡을 때 쓰는 숫자", self.html)

    def test_topic_and_staff_link_to_filtered_list(self):
        self.assertIn("href='/?topic=", self.html)
        self.assertIn("href='/?staff=", self.html)

    def test_topic_numbers_match_the_post_list_screen(self):
        # 같은 경로(kn.normalize)를 쓰므로 목록 화면이 말하는 창고 글 수와 어긋나지 않는다
        self.assertIn("창고에 주제 ‘플래너’로 쓴 글은 <b>5건</b>",
                      viewer.render_list(self.conn, topic="플래너"))

    def test_fact_gap_is_sorted_by_fewest_and_marked_unreviewed(self):
        # 글이 0건인 팩트 항목이 먼저 오고, 미확인 상태임을 화면이 밝힌다
        self.assertLess(self.html.index("한번도안쓴자격증"), self.html.index("복지"))
        self.assertIn("2건 중 <b>2건이 ‘미확인’</b>", self.html)
        self.assertIn("<span class='badge dim'>미확인</span>", self.html)
        self.assertNotIn("추천 주제", self.html)         # 점수·추천을 새로 만들지 않는다

    def test_unmatched_fact_says_name_mismatch_not_zero(self):
        # ★ 이번 수정의 핵심 — 이름이 안 맞은 항목을 '0건'(=안 썼다)으로 찍지 않는다.
        #   실제 창고에서 '편입학 — 사이버대·방통대'가 0건으로 뜨는데 방통대 글이 83편 있었다.
        secb = self.html.split("팩트 항목 이름, 우리 글 주제와 맞나")[1].split("담당자별 우리 글")[0]
        self.assertIn("이름이 안 맞음 — 직접 찾아보세요", secb)
        self.assertNotIn("0건", secb)
        self.assertIn("글 목록에서 찾기 →", secb)          # 직접 찾아볼 길이 화면에 있다

    def test_matched_fact_shows_which_topic_it_landed_on(self):
        # 이름이 맞은 줄은 '어느 주제에 붙어 몇 편인지'를 화면이 말한다(같은 숫자가 나오는 이유)
        self.assertIn("맞은 주제", self.html)                 # 표 머리
        self.assertIn("주제 ‘<a href='/?topic=", self.html)
        self.assertIn(">5편</div>", self.html)                # 주제 '플래너' 5편

    def test_fact_match_counts_are_on_screen(self):
        # 이 표를 얼마나 믿을지 스스로 판단할 수 있게 맞은 수·안 맞은 수를 적는다
        self.assertIn("맞은 것 1개 · 안 맞은 것 1개", self.html)

    def test_upper_tables_say_they_ignore_the_folded_filters(self):
        self.assertIn("언제나 창고 전체 기준", self.html)
        self.assertIn("우리가 카페에서 직접 가져와 조회수를 확보한 글", self.html)

    def test_concluded_view_tables_are_folded_not_deleted(self):
        self.assertIn("<details class='foldsec'", self.html)
        self.assertIn("조회수를 확보한 글이 아직 없습니다", self.html)
        self.assertIn("위쪽 표는 조회수 없이도 볼 수 있어요", self.html)

    def test_empty_fact_table_points_to_facts_screen(self):
        empty = db.get_connection(os.path.join(self.tmp, "nofacts.sqlite3"))
        db.init_db(empty)
        try:
            h = viewer.render_analysis(empty)
            self.assertIn("아직 창고에 글이 없습니다", h)      # 글도 0건인 창고
            empty.execute("INSERT INTO posts (post_id, title, keyword) VALUES (1,'글','플래너')")
            empty.commit()
            h = viewer.render_analysis(empty)
            self.assertIn("룰북 팩트가 아직 창고에 없습니다", h)
            self.assertIn("href='/facts'", h)
        finally:
            empty.close()

    def test_analysis_tables_scroll_instead_of_wrapping(self):
        # 좁은 화면에서 칸이 접히지 않고 표가 가로로 밀린다(사용 피드백 6번 후반)
        for cls_name in ("an12", "an13"):        # 팩트·담당자 표는 같은 4열 틀을 함께 쓴다
            self.assertIn(f"class='{cls_name}'", self.html)
        self.assertEqual(self.html.count("class='tablewrap'"), 3)   # 세 표 모두 감싸짐

    def test_screen_is_read_only(self):
        before = self.conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        viewer.render_analysis(self.conn, tsort="few")
        viewer.render_analysis(self.conn, tsort="name")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"], before)
        self.assertNotIn("method='post'", self.html)

    def test_no_pii_leak(self):
        for bad in (PII_PHONE, PII_NAME, OPENCHAT, PARA_RAW_SENTINEL):
            self.assertNotIn(bad, self.html)


class TestAnalysisTopicMore(unittest.TestCase):
    """주제 표의 '더 보기' — 기본 50개, 누르면 200개까지(자바스크립트 없이 주소로만)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.conn = db.get_connection(os.path.join(cls.tmp, "many.sqlite3"))
        db.init_db(cls.conn)
        # 주제 260개 — 기본(50)·더 보기(200) 두 상한을 모두 넘겨 잘림을 실제로 잰다
        cls.conn.executemany(
            "INSERT INTO posts (post_id, title, keyword) VALUES (?,?,?)",
            [(i, f"글{i}", f"주제{i:04d}자격증") for i in range(1, 261)])
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _topic_rows(self, html):
        return html.count("<div class='listrow static'><div><a class='r-title' href='/?topic=")

    def test_default_shows_50_and_offers_more(self):
        h = viewer.render_analysis(self.conn)
        self.assertEqual(self._topic_rows(h), viewer.TOPIC_TOP)
        self.assertIn("260개 중 50개가 보입니다", h)
        self.assertIn("더 보기(200개까지) →", h)

    def test_more_shows_200(self):
        h = viewer.render_analysis(self.conn, more=True)
        self.assertEqual(self._topic_rows(h), viewer.TOPIC_MORE)
        self.assertIn("260개 중 200개가 보입니다", h)
        self.assertIn("처음 50개만 보기", h)          # 되돌아갈 길도 있다

    def test_more_travels_with_sort_links(self):
        # 정렬을 바꿔도 '더 보기'가 풀리지 않는다(링크를 한 곳에서 만든 덕)
        h = viewer.render_analysis(self.conn, more=True, tsort="few")
        self.assertIn("tsort=name&more=200", h)

    def test_read_only(self):
        before = self.conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        h = viewer.render_analysis(self.conn, more=True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"], before)
        self.assertNotIn("method='post'", h)
        self.assertNotIn("<script", h)               # 자바스크립트 0


class TestAnalysisMath(unittest.TestCase):
    """분석 집계의 비자명 수치 로직(상관·세기 라벨) 자체검증 — 깨지면 실패."""

    def test_pearson_perfect_and_none(self):
        self.assertAlmostEqual(viewer.pearson([1, 2, 3, 4], [2, 4, 6, 8]), 1.0, places=6)
        self.assertAlmostEqual(viewer.pearson([1, 2, 3, 4], [8, 6, 4, 2]), -1.0, places=6)
        self.assertIsNone(viewer.pearson([1, 2], [1, 2]))       # 표본<3
        self.assertIsNone(viewer.pearson([5, 5, 5], [1, 2, 3]))  # 분산 0 → None

    def test_rel_label_bands(self):
        self.assertEqual(viewer.rel_label(-0.08), "거의 관계 없음")
        self.assertEqual(viewer.rel_label(0.2), "약한 관계")
        self.assertEqual(viewer.rel_label(-0.4), "어느 정도 관계")
        self.assertEqual(viewer.rel_label(0.7), "뚜렷한 관계")


class TestKeywordNormalize(unittest.TestCase):
    """정규화 단일 출처 — 변형이 한 주제로 묶이고, 급은 갈리고, 일반어는 None."""

    def test_variants_merge_to_one_topic(self):
        import keyword_normalize as kn
        self.assertEqual(kn.normalize("사회복지사2급 취업"), "사회복지사2급")
        self.assertEqual(kn.normalize("사회복지사2급자격증취득방법"), "사회복지사2급")
        # 급(1급/2급)은 다른 주제로 유지 — 과병합 방지
        self.assertNotEqual(kn.normalize("사회복지사2급"), kn.normalize("사회복지사1급"))

    def test_alias_and_generic(self):
        import keyword_normalize as kn
        self.assertEqual(kn.normalize("미용종합면허증"), "종합미용면허증")  # 어순 동의어
        self.assertEqual(kn.normalize("사이버대학교 학점은행제"), "사이버대학")  # 표기+꼬리말
        self.assertIsNone(kn.normalize("학점은행제"))   # 일반어만 → 주제 없음
        self.assertIsNone(kn.normalize(""))

    def test_near_duplicate_candidates(self):
        import keyword_normalize as kn
        tc = [("사이버대학", 188), ("사이버대", 2), ("사회복지사", 130),
              ("사회복지사2급", 417), ("종합미용면허증", 100), ("미용종합면허증", 90)]
        flat = {tuple(sorted((a, b)))
                for a, b, ac, bc, why in kn.near_duplicate_candidates(tc, min_count=2)}
        self.assertIn(tuple(sorted(("종합미용면허증", "미용종합면허증"))), flat)  # 어순
        self.assertIn(tuple(sorted(("사이버대", "사이버대학"))), flat)          # 1글자 차
        # 급 차이는 병합 후보 아님(과병합 방지)
        self.assertNotIn(tuple(sorted(("사회복지사", "사회복지사2급"))), flat)


class TestTrendsMath(unittest.TestCase):
    """시기 트렌드 집계(비중 기반 뜨는/식는·계절성·월내) 자체검증 — 순수 함수, DB 불필요."""

    @staticmethod
    def _rec(topic, y, m, d):
        q = f"{y}-Q{(m - 1) // 3 + 1}"
        dom = "early" if d <= 10 else ("mid" if d <= 20 else "late")
        return dict(topic=topic, y=y, m=m, d=d, q=q, dom=dom)

    def test_quarter_trend_rising_and_falling_by_share(self):
        import trends
        recs = []
        # Q1: A 2건(20%) B 8건 / Q2: A 8건(80%) B 2건 → A는 비중↑, B는 비중↓
        recs += [self._rec("A", 2025, 1, 5) for _ in range(2)]
        recs += [self._rec("B", 2025, 1, 5) for _ in range(8)]
        recs += [self._rec("A", 2025, 4, 5) for _ in range(8)]
        recs += [self._rec("B", 2025, 4, 5) for _ in range(2)]
        qt = trends.quarter_trends(recs, min_topic=2, min_quarter=2, top=5)
        self.assertEqual(qt["rising"][0]["topic"], "A")
        self.assertEqual(qt["falling"][0]["topic"], "B")
        self.assertGreater(qt["rising"][0]["slope"], 0)
        self.assertLess(qt["falling"][0]["slope"], 0)

    def test_seasonality_peak_month(self):
        import trends
        recs = ([self._rec("S", 2026, 4, 5) for _ in range(9)]
                + [self._rec("S", 2026, 7, 5) for _ in range(1)])
        top = trends.seasonality(recs, min_topic=2, top=3)
        self.assertEqual(top[0]["topic"], "S")
        self.assertEqual(top[0]["peak_month"], 4)
        self.assertAlmostEqual(top[0]["peak_pct"], 90.0, places=1)

    def test_monthly_share_heatmap(self):
        import trends
        recs = ([self._rec("H", 2026, 4, 5) for _ in range(2)]      # 4월 H 2 / 전체 10 = 20%
                + [self._rec("X", 2026, 4, 5) for _ in range(8)]
                + [self._rec("H", 2026, 5, 5) for _ in range(8)]     # 5월 H 8 / 전체 10 = 80%
                + [self._rec("X", 2026, 5, 5) for _ in range(2)])
        hm = trends.monthly_share_heatmap(recs, top_n=5, min_month_total=5)
        self.assertEqual(hm["months"], ["2026-04", "2026-05"])
        hrow = next(r for r in hm["rows"] if r["topic"] == "H")
        self.assertAlmostEqual(hrow["cells"][0], 20.0, places=1)
        self.assertAlmostEqual(hrow["cells"][1], 80.0, places=1)

    def test_intramonth_late_skew(self):
        import trends
        recs = ([self._rec("L", 2026, 4, 25) for _ in range(8)]
                + [self._rec("L", 2026, 4, 5) for _ in range(2)])
        im = trends.intramonth(recs, min_topic=2, top=3)
        self.assertEqual(im["late"][0]["topic"], "L")
        self.assertAlmostEqual(im["late"][0]["late_pct"], 80.0, places=1)


class TestTrendsEmptyState(unittest.TestCase):
    """빈 트렌드 화면이 원인별로 다른 안내를 준다 — 창고 비면 '엑셀 적재 먼저'."""

    def test_empty_posts_guides_to_ingest(self):
        tmp = tempfile.mkdtemp()
        try:
            conn = db.get_connection(os.path.join(tmp, "e.sqlite3"))
            db.init_db(conn)                 # 테이블만 있고 posts 0건
            conn.commit()
            h = viewer.render_trends(conn)
            self.assertIn("ingest_excel", h)          # 적재 안내 문구
            self.assertNotIn("작성일이 있는 글이 아직 없습니다", h)  # 옛 뭉뚱그린 문구 아님
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ymd_year_cap_allows_future(self):
        import trends
        self.assertEqual(trends._ymd("2027-03-15"), (2027, 3, 15))  # 2027 시한폭탄 방지
        self.assertIsNone(trends._ymd("2323-01-01"))                # 명백한 오타는 계속 배제


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
