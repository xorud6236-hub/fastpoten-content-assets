# -*- coding: utf-8 -*-
"""CA-1 자체 테스트 — 스키마 생성 + 룰북 최소 적재 검증.

사용: python tests/test_ca1.py   (표준 unittest, 추가 의존성 없음)
"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import db  # noqa: E402
import load_rulebook  # noqa: E402


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.sqlite3")
        self.conn = db.get_connection(self.db_path)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_db_file_created(self):
        self.assertTrue(os.path.exists(self.db_path))

    def test_all_expected_tables_exist(self):
        tables = set(db.list_tables(self.conn))
        for t in db.EXPECTED_TABLES:
            self.assertIn(t, tables, f"필수 테이블 누락: {t}")

    def test_init_is_idempotent(self):
        # 두 번 실행해도 오류 없어야 함(멱등)
        db.init_db(self.conn)
        db.init_db(self.conn)

    def test_posts_has_no_body_text_column(self):
        # 불변 4: 본문 텍스트는 파일에만 — posts엔 경로 컬럼만 있어야 함
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(posts)")}
        self.assertIn("body_raw_path", cols)
        self.assertIn("body_clean_path", cols)
        self.assertIn("body_pub_ref_path", cols)
        for banned in ("body_raw", "body_clean", "body_text", "body"):
            self.assertNotIn(banned, cols, f"posts에 본문 텍스트 컬럼 금지: {banned}")

    def test_no_performance_grade_anywhere(self):
        # 불변 3: 등급제(P1/P2/P3)·length_penalty 금지
        for table in db.EXPECTED_TABLES:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            self.assertNotIn("performance_grade", cols, table)
            self.assertNotIn("length_penalty", cols, table)

    def test_posts_has_mask_count_columns(self):
        # 가림 건수 저장 자리(ensure 패턴으로 추가) — 건수와 '어떤 규칙으로 셌는지'(지문)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(posts)")}
        self.assertIn("mask_count", cols)
        self.assertIn("mask_rules_fingerprint", cols)

    def test_ensure_columns_survive_rerun(self):
        # 불변 9: 다시 돌려도 칸이 사라지거나 중복되지 않음
        db.init_db(self.conn)
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(posts)")]
        self.assertEqual(cols.count("mask_count"), 1)
        self.assertEqual(cols.count("mask_rules_fingerprint"), 1)

    def test_ensure_column_idempotent(self):
        db.ensure_column(self.conn, "staff", "test_col", "TEXT")
        db.ensure_column(self.conn, "staff", "test_col", "TEXT")  # 재실행 안전
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(staff)")}
        self.assertIn("test_col", cols)


class TestRulebookLoad(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db_path = os.path.join(cls.tmp, "test.sqlite3")
        cls.xlsx = load_rulebook.find_rulebook_path()
        # 두 번 실행 → 멱등 확인(건수 동일해야 함)
        cls.counts1 = load_rulebook.run(cls.xlsx, cls.db_path)
        cls.counts2 = load_rulebook.run(cls.xlsx, cls.db_path)

    def setUp(self):
        self.conn = db.get_connection(self.db_path)

    def tearDown(self):
        self.conn.close()

    def test_category_count(self):
        # 룰북 V4.2 ①시트 = 15개 카테고리
        self.assertEqual(self.counts1["categories"], 15)

    def test_banned_word_count(self):
        # ④시트 금지어 14행 (CTA 문구는 미포함이어야 함)
        self.assertEqual(self.counts1["banned_words"], 14)
        rows = self.conn.execute("SELECT word FROM rulebook_banned_words").fetchall()
        words = [r["word"] for r in rows]
        self.assertIn("무조건", words)
        self.assertIn("합격 보장", words)
        for w in words:  # CTA 문구가 섞이면 안 됨
            self.assertNotIn("상담", w)

    def test_pii_patterns_seeded_and_valid(self):
        self.assertGreaterEqual(self.counts1["pii_patterns"], 6)
        rows = self.conn.execute(
            "SELECT name, pattern_type, pattern FROM rulebook_pii_patterns").fetchall()
        for r in rows:
            if r["pattern_type"] == "regex":
                re.compile(r["pattern"])  # 정규식이 유효해야 함

    def test_pii_regex_catches_examples(self):
        pats = {r["name"]: r["pattern"] for r in self.conn.execute(
            "SELECT name, pattern FROM rulebook_pii_patterns WHERE pattern_type='regex'")}
        self.assertTrue(re.search(pats["전화번호(일반)"], "문의는 010-1234-5678로 주세요"))
        self.assertTrue(re.search(pats["전화번호(대표번호)"], "대표번호 1588-0000"))
        self.assertTrue(re.search(pats["오픈채팅 링크"], "https://open.kakao.com/o/abc123"))
        self.assertTrue(re.search(pats["직원 호칭(쌤/멘토/팀장)"], "김철수쌤에게 문의주세요"))
        self.assertTrue(re.search(pats["직원 호칭(쌤/멘토/팀장)"], "박 멘토가 안내드립니다"))

    def test_rerun_is_idempotent(self):
        self.assertEqual(self.counts1, self.counts2)
        n = self.conn.execute("SELECT COUNT(*) c FROM rulebook_categories").fetchone()["c"]
        self.assertEqual(n, 15)  # 중복 누적 없어야 함

    def test_no_account_info_anywhere(self):
        # 불변 2: 계정 정보(비밀번호 등)는 어떤 형태로도 미반입
        tables = db.list_tables(self.conn)
        self.assertNotIn("account_info", tables)
        for t in tables:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({t})")}
            self.assertNotIn("password", cols, t)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
