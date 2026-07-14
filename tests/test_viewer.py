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
import viewer  # noqa: E402

# 개인정보 — 어떤 화면에도 원본 그대로 나오면 안 됨(마스킹돼 사라져야 함)
PII_PHONE = "010-1234-5678"
PII_NAME = "김민지쌤"          # 직원 실명+호칭(마스킹 대상)
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
        conn.execute("INSERT INTO staff (staff_name) VALUES ('김민지')")

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
            ("임상심리사 2급 응시자격", "임상심리사2급 응시자격", "공준모", "질문게시판", "김민지",
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
        self.assertIn("김민지", h)                           # 담당자 실명(내부 검수 허용)

    # ---- 분석 화면 렌더 + 개인정보 누출 0(불변 1·3) ----
    def test_analysis_renders_sections(self):
        h = self._get("/analysis")
        self.assertIn("참고 신호 분석", h)
        self.assertIn("조회수 높은 글", h)
        self.assertIn("키워드별 조회수", h)
        self.assertIn("담당자별 조회수", h)
        self.assertIn("형식과 조회수, 관계가 있을까?", h)
        self.assertIn("분석 대상 1건", h)
        self.assertIn("1,234", h)                           # 조회수 천단위
        # 불변 3 — 성과로 단정하지 않고 참고 신호로 표기
        self.assertIn("참고 신호", h)
        self.assertNotIn("성과 등급", h)

    def test_analysis_no_pii_leak(self):
        h = self._get("/analysis")
        self.assertNotIn(PII_PHONE, h)
        self.assertNotIn(PII_NAME, h)                       # '김민지쌤'(호칭 포함) 누출 없음
        self.assertNotIn(OPENCHAT, h)
        self.assertNotIn(PARA_RAW_SENTINEL, h)

    def test_analysis_sort_and_filter_urls_ok(self):
        # 정렬/기간 URL 모두 500 없이 렌더(urlopen은 500이면 예외)
        for path in ("/analysis?sort=views", "/analysis?sort=vpd",
                     "/analysis?min_age=30", "/analysis?sort=vpd&min_age=30"):
            h = self._get(path)
            self.assertIn("참고 신호 분석", h)

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

    def test_intramonth_late_skew(self):
        import trends
        recs = ([self._rec("L", 2026, 4, 25) for _ in range(8)]
                + [self._rec("L", 2026, 4, 5) for _ in range(2)])
        im = trends.intramonth(recs, min_topic=2, top=3)
        self.assertEqual(im["late"][0]["topic"], "L")
        self.assertAlmostEqual(im["late"][0]["late_pct"], 80.0, places=1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
