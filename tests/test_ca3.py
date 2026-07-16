# -*- coding: utf-8 -*-
"""CA-3 자체 테스트 — 마스킹·문단·파이프라인 검증.

핵심(CLAUDE.md 검증방법): 테스트 문장(전화번호·OO쌤 포함)이 반드시 가려지는지 자동 확인.
사용: python tests/test_ca3.py
"""
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
import db  # noqa: E402
import masking  # noqa: E402
import load_rulebook  # noqa: E402
import intake_manual as im  # noqa: E402


def _patterns_db():
    """임시 DB에 룰북 패턴 적재 후 (db_path) 반환."""
    tmp = tempfile.mkdtemp()
    dbp = os.path.join(tmp, "t.sqlite3")
    load_rulebook.run(db_path=dbp)
    return dbp


class TestMasking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dbp = _patterns_db()

    def setUp(self):
        self.conn = db.get_connection(self.dbp)
        self.pats = masking.load_regex_patterns(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_phone_masked(self):
        out, hits = masking.mask_text("문의는 010-1234-5678로 주세요", self.pats)
        self.assertNotIn("010-1234-5678", out)
        self.assertIn("[전화번호]", out)

    def test_honorific_masked(self):
        out, _ = masking.mask_text("담당 가상인쌤이 안내해요", self.pats)
        self.assertNotIn("가상인쌤", out)
        self.assertIn("[담당자]", out)

    def test_openchat_masked_no_overrun(self):
        out, _ = masking.mask_text("오픈채팅(https://open.kakao.com/o/sABCdef)으로 연락", self.pats)
        self.assertNotIn("open.kakao.com", out)
        self.assertIn(")으로 연락", out)  # 괄호·뒤 한글은 보존

    def test_name_list_masks_bare_name(self):
        out, _ = masking.mask_text("가상인 선생님께", self.pats, name_list=["가상인"])
        self.assertNotIn("가상인", out)

    def test_no_pii_no_change(self):
        text = "임상심리사 2급은 학사학위와 1년 수련이 필요해요."
        out, hits = masking.mask_text(text, self.pats, name_list=["가상인"])
        self.assertEqual(out, text)
        self.assertEqual(hits, [])


class TestRulesFingerprint(unittest.TestCase):
    """규칙 지문 — 저장해 둔 가림 건수가 아직 유효한지 판단하는 근거."""

    @classmethod
    def setUpClass(cls):
        cls.dbp = _patterns_db()

    def setUp(self):
        self.conn = db.get_connection(self.dbp)

    def tearDown(self):
        self.conn.close()

    def _fp(self, names):
        """staff 이름 목록을 그 순서대로 넣고 지문 계산."""
        self.conn.execute("DELETE FROM staff")
        for n in names:
            self.conn.execute("INSERT INTO staff (staff_name) VALUES (?)", (n,))
        self.conn.commit()
        return masking.rules_fingerprint(self.conn)

    def test_same_rules_same_fingerprint(self):
        self.assertEqual(self._fp(["가상인", "나철수"]), self._fp(["가상인", "나철수"]))

    def test_order_does_not_change_fingerprint(self):
        # 같은 규칙이면 행 순서가 흔들려도 같은 지문(정렬 후 계산) — 헛되이 다시 세지 않게
        self.assertEqual(self._fp(["가상인", "나철수"]), self._fp(["나철수", "가상인"]))

    def test_removing_a_name_changes_fingerprint(self):
        # BACKLOG 6 실제 사례: staff에서 '테스트'를 빼면 가림 결과가 달라진다 → 지문도 달라져야
        self.assertNotEqual(self._fp(["가상인", "테스트"]), self._fp(["가상인"]))

    def test_algo_version_changes_fingerprint(self):
        # 코드에 박힌 규칙(호칭 접미 목록·이름 최소 길이)은 DB에 없어 지문이 자동으로 못 잡는다.
        # 그 곁의 번호를 올리면 지문이 바뀌어야 옛 건수가 '다시 세기 필요'가 된다.
        before = self._fp(["가상인"])
        orig = masking.MASK_ALGO_VERSION
        try:
            masking.MASK_ALGO_VERSION = orig + 1
            self.assertNotEqual(before, masking.rules_fingerprint(self.conn))
        finally:
            masking.MASK_ALGO_VERSION = orig
        self.assertEqual(before, masking.rules_fingerprint(self.conn))  # 되돌리면 같은 지문

    def test_one_character_pattern_change_changes_fingerprint(self):
        before = self._fp(["가상인"])
        self.conn.execute(
            "UPDATE rulebook_pii_patterns SET pattern = pattern || '?' WHERE pattern_id="
            "(SELECT MIN(pattern_id) FROM rulebook_pii_patterns "
            " WHERE pattern_type='regex' AND pattern IS NOT NULL)")
        self.conn.commit()
        self.assertNotEqual(before, masking.rules_fingerprint(self.conn))


class TestParagraph(unittest.TestCase):
    def test_split(self):
        paras = im.split_paragraphs("가\n나\n\n다\n\n\n라")
        self.assertEqual(paras, ["가\n나", "다", "라"])  # 문단 내부 단일 개행 보존(빈 줄만 문단 분리)

    def test_intro_first(self):
        role, conf = im.tag_role("안녕하세요! 응시자격 알아볼게요", 0, 5)
        self.assertEqual(role, "도입")

    def test_closing(self):
        role, _ = im.tag_role("정리하면, 학사와 수련이 필요해요", 4, 5)
        self.assertEqual(role, "마무리")

    def test_cta(self):
        role, _ = im.tag_role("궁금하면 오픈채팅으로 문의주세요", 3, 5)
        self.assertEqual(role, "CTA")


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dbp = _patterns_db()
        # 담당자 이름을 staff에 넣어 name_list 마스킹 재료 확보
        conn = db.get_connection(cls.dbp)
        conn.execute("INSERT OR IGNORE INTO staff (staff_name) VALUES ('가상인')")
        conn.commit(); conn.close()
        folder = os.path.join(ROOT, "examples", "intake_sample", "임상심리사2급_응시자격")
        cls.res = im.run([folder], db_path=cls.dbp)[0]
        cls.res2 = im.run([folder], db_path=cls.dbp)[0]  # 멱등

    def setUp(self):
        self.conn = db.get_connection(self.dbp)

    def tearDown(self):
        self.conn.close()

    def test_three_versions_saved(self):
        for key in ("raw", "clean", "pub_ref"):
            path = os.path.join(ROOT, {
                "raw": "corpus/임상심리사2급_응시자격/body_raw.txt",
                "clean": "corpus/임상심리사2급_응시자격/body_clean.txt",
                "pub_ref": "corpus/임상심리사2급_응시자격/body_pub_ref.txt",
            }[key])
            self.assertTrue(os.path.exists(path))

    def test_pub_ref_has_no_pii(self):
        # ★ 핵심 불변: 참고용 본문에 개인정보가 남으면 안 됨
        path = os.path.join(ROOT, "corpus/임상심리사2급_응시자격/body_pub_ref.txt")
        text = open(path, encoding="utf-8").read()
        self.assertNotIn("010-1234-5678", text)
        self.assertNotIn("open.kakao.com", text)
        self.assertNotIn("가상인쌤", text)
        self.assertNotRegex(text, r"01[016-9]-?\d{3,4}-?\d{4}")

    def test_raw_is_verbatim(self):
        # 원문(body_raw)은 절대 수정 안 함(불변 4) — 개인정보 그대로 보존
        raw = open(os.path.join(ROOT, "corpus/임상심리사2급_응시자격/body_raw.txt"),
                   encoding="utf-8").read()
        self.assertIn("010-1234-5678", raw)
        self.assertIn("가상인쌤", raw)

    def test_db_no_body_text_column(self):
        # posts엔 경로만(불변 4)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(posts)")}
        self.assertIn("body_pub_ref_path", cols)
        self.assertNotIn("body_text", cols)

    def test_paragraphs_and_images(self):
        pid = self.res["post_id"]
        np = self.conn.execute("SELECT COUNT(*) c FROM post_paragraphs WHERE post_id=?", (pid,)).fetchone()["c"]
        ni = self.conn.execute("SELECT COUNT(*) c FROM post_images WHERE post_id=?", (pid,)).fetchone()["c"]
        self.assertEqual(np, 7)  # 제목 줄 제외 후 본문 7문단(부록 A와 동일)
        self.assertEqual(ni, 6)

    def test_paragraph_clean_text_masked(self):
        # 문단 clean_text(참고용)에도 개인정보 없어야
        pid = self.res["post_id"]
        rows = self.conn.execute(
            "SELECT clean_text FROM post_paragraphs WHERE post_id=?", (pid,)).fetchall()
        joined = " ".join(r["clean_text"] for r in rows)
        self.assertNotIn("010-1234-5678", joined)
        self.assertNotIn("가상인쌤", joined)

    def test_image_pattern_only_flagged(self):
        pid = self.res["post_id"]
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM post_images WHERE post_id=? AND reuse_scope='image_pattern_only'",
            (pid,)).fetchone()["c"]
        self.assertEqual(n, 2)  # 인물 분위기사진 + 전화번호 상담배너

    def test_mask_count_saved_at_intake(self):
        # 3차: 투입·추출 때 가림 건수+지문이 함께 저장 — 따로 세는 명령 없이도 숫자가 있어야
        pid = self.res["post_id"]
        row = self.conn.execute(
            "SELECT mask_count, mask_rules_fingerprint FROM posts WHERE post_id=?",
            (pid,)).fetchone()
        self.assertEqual(row["mask_count"], len(self.res["hits"]))
        self.assertGreater(row["mask_count"], 0)   # 이 샘플엔 전화번호·이름이 들어 있음
        self.assertEqual(row["mask_rules_fingerprint"], masking.rules_fingerprint(self.conn))

    def test_idempotent_no_duplicate(self):
        self.assertEqual(self.res["post_id"], self.res2["post_id"])
        pid = self.res["post_id"]
        np = self.conn.execute("SELECT COUNT(*) c FROM post_paragraphs WHERE post_id=?", (pid,)).fetchone()["c"]
        self.assertEqual(np, 7)  # 재실행해도 7개(중복 누적 없음)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
