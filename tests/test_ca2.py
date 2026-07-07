# -*- coding: utf-8 -*-
"""CA-2 자체 테스트 — URL 정규화·순위 파싱·엑셀 적재 검증.

사용: python tests/test_ca2.py
"""
import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import db  # noqa: E402
import ingest_excel as ie  # noqa: E402


class TestNormalizeUrl(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(
            ie.normalize_url("https://cafe.naver.com/studentstudyhard/2712612"),
            ("https://cafe.naver.com/studentstudyhard/2712612", None))

    def test_query_stripped(self):
        u, err = ie.normalize_url("https://cafe.naver.com/studentstudyhard/2106096?boardType=L")
        self.assertEqual(u, "https://cafe.naver.com/studentstudyhard/2106096")
        self.assertIsNone(err)

    def test_mobile(self):
        u, _ = ie.normalize_url("https://m.cafe.naver.com/studentstudyhard/2299135")
        self.assertEqual(u, "https://cafe.naver.com/studentstudyhard/2299135")

    def test_cafe_web_form(self):
        u, _ = ie.normalize_url(
            "https://m.cafe.naver.com/ca-fe/web/cafes/dgmom365/articles/7263977?tc=x")
        self.assertEqual(u, "https://cafe.naver.com/dgmom365/7263977")

    def test_iframe_form(self):
        raw = ("https://cafe.naver.com/studentstudyhard?iframe_url_utf8="
               "%2FArticleRead.nhn%253Fclubid%3D21737991%2526articleid%3D2171366")
        u, _ = ie.normalize_url(raw)
        self.assertEqual(u, "https://cafe.naver.com/studentstudyhard/2171366")

    def test_cafe_home_is_error(self):
        u, err = ie.normalize_url("https://cafe.naver.com/studentstudyhard")
        self.assertIsNone(u)
        self.assertEqual(err, "링크오류")

    def test_http_and_case(self):
        u, _ = ie.normalize_url("http://cafe.naver.com/StudentStudyHard/2751429")
        self.assertEqual(u, "https://cafe.naver.com/studentstudyhard/2751429")


class TestParseRank(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(ie.parse_rank("1p 1등"), (1, "Top3"))
        self.assertEqual(ie.parse_rank("1p 5등"), (5, "Top10"))
        self.assertEqual(ie.parse_rank("2p 6등"), (16, "Top30"))
        self.assertEqual(ie.parse_rank("4p 7등"), (37, "Other"))

    def test_combined_uses_tonghap_only(self):
        # 통합탭 구간만 판단 — 카페탭 1p 1등에 속으면 안 됨
        self.assertEqual(ie.parse_rank("통합탭 그외 카페탭 1p 1등"), (None, "Other"))
        self.assertEqual(ie.parse_rank("통합탭 1p 4등 카페탭 1p 1등"), (4, "Top10"))

    def test_text_values(self):
        self.assertEqual(ie.parse_rank("그외"), (None, "Other"))
        self.assertEqual(ie.parse_rank("그 외"), (None, "Other"))
        self.assertEqual(ie.parse_rank("미반영"), (None, "Not Exposed"))
        self.assertEqual(ie.parse_rank("누락"), (None, "Not Exposed"))
        self.assertEqual(ie.parse_rank("변동없음"), (None, "Unknown"))
        self.assertEqual(ie.parse_rank(None), (None, "Unknown"))
        self.assertEqual(ie.parse_rank(""), (None, "Unknown"))


class TestNormalizeDate(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(ie.normalize_date("2026.01.01"), "2026-01-01")
        self.assertEqual(ie.normalize_date("2024,04.02"), "2024-04-02")
        self.assertEqual(ie.normalize_date(datetime.datetime(2022, 2, 17)), "2022-02-17")
        self.assertEqual(ie.normalize_date("2022-02-17 00:00:00"), "2022-02-17")
        self.assertIsNone(ie.normalize_date(None))

    def test_unparseable_kept_raw(self):
        self.assertEqual(ie.normalize_date("작성일 미상"), "작성일 미상")


class TestSheetCafeName(unittest.TestCase):
    def test_names(self):
        self.assertEqual(ie.sheet_cafe_name("공준모 현황"), "공준모")
        self.assertEqual(ie.sheet_cafe_name("공준모 현황(예전꺼)"), "공준모")
        self.assertEqual(ie.sheet_cafe_name("추가턴 카페 현황"), "추가턴 카페")


class TestIngestIntegration(unittest.TestCase):
    """실제 엑셀 파일로 통합 검증(수 분 내)."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db_path = os.path.join(cls.tmp, "test.sqlite3")
        cls.xlsx = ie.find_status_xlsx()
        cls.result1 = ie.ingest(cls.xlsx, cls.db_path)
        cls.result2 = ie.ingest(cls.xlsx, cls.db_path)  # 멱등 확인

    def setUp(self):
        self.conn = db.get_connection(self.db_path)

    def tearDown(self):
        self.conn.close()

    def test_scale_matches_expectation(self):
        t = self.result1["totals"]
        self.assertGreater(t["url_cells"], 11000)   # 관측치 11,821
        self.assertGreater(t["loaded"], 10000)      # 약 11,000건 규모

    def test_posts_equal_loaded_plus_errors(self):
        t = self.result1["totals"]
        n = self.conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        self.assertEqual(n, t["loaded"] + t["link_error"])

    def test_rerun_adds_nothing(self):
        t2 = self.result2["totals"]
        self.assertEqual(t2["loaded"], 0)
        self.assertEqual(t2["link_error"], 0)
        n = self.conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        self.assertEqual(n, self.result1["totals"]["loaded"]
                         + self.result1["totals"]["link_error"])

    def test_no_excluded_tabs(self):
        # 제외 탭(마케팅표·키워드·계정정보·휴일선정표)이 안 섞였는지
        rows = self.conn.execute("SELECT DISTINCT source_sheet s FROM posts").fetchall()
        for r in rows:
            self.assertIn("현황", r["s"])
            for banned in ("마케팅표", "계정", "휴일", "키워드(대기)"):
                self.assertNotIn(banned, r["s"])

    def test_normalized_urls_unique_and_wellformed(self):
        rows = self.conn.execute(
            "SELECT normalized_url u, COUNT(*) c FROM posts WHERE u IS NOT NULL "
            "GROUP BY u HAVING c > 1").fetchall()
        self.assertEqual(len(rows), 0, "정규화 URL 중복 존재")
        bad = self.conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE normalized_url IS NOT NULL "
            "AND normalized_url NOT LIKE 'https://cafe.naver.com/%'").fetchone()["c"]
        self.assertEqual(bad, 0)

    def test_link_errors_have_reason(self):
        # 실패 사유 기록률 100% (v9 KPI)
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE normalized_url IS NULL "
            "AND (extraction_error IS NULL OR extraction_status != '링크오류')").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_signals_buckets_valid(self):
        allowed = {"Top3", "Top10", "Top30", "Other", "Not Exposed", "Unknown"}
        rows = self.conn.execute(
            "SELECT DISTINCT rank_bucket b FROM reference_signals").fetchall()
        for r in rows:
            self.assertIn(r["b"], allowed)

    def test_core_meta_coverage(self):
        # 카페명은 시트에서 유도되므로 100%여야 함
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE cafe_name IS NULL OR cafe_name=''").fetchone()["c"]
        self.assertEqual(n, 0)
        # 키워드·담당자는 원본 빈칸이 있을 수 있으나 90% 이상은 채워져야 정상
        total = self.conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        kw = self.conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE keyword IS NOT NULL").fetchone()["c"]
        st = self.conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE staff_name IS NOT NULL").fetchone()["c"]
        self.assertGreater(kw / total, 0.9, f"키워드 채움률 {kw}/{total}")
        self.assertGreater(st / total, 0.9, f"담당자 채움률 {st}/{total}")

    def test_staff_table_populated(self):
        n = self.conn.execute("SELECT COUNT(*) c FROM staff").fetchone()["c"]
        self.assertGreater(n, 5)

    def test_dates_mostly_iso(self):
        # 컬럼 밀림 보정 후: 비정상 날짜(이름·순위 등이 날짜 자리에)는 0.5% 미만
        total = self.conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE publish_date IS NOT NULL").fetchone()["c"]
        bad = self.conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE publish_date IS NOT NULL AND "
            "publish_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
        ).fetchone()["c"]
        self.assertLess(bad / total, 0.005, f"비정상 날짜 {bad}/{total}")

    def test_staff_not_datelike(self):
        # 담당자 자리에 날짜가 들어간 행(밀림 잔재)이 극소수여야 함
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE staff_name GLOB '[0-9][0-9][0-9][0-9]*'"
        ).fetchone()["c"]
        self.assertLess(n, 20, f"담당자=날짜형 {n}건")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
