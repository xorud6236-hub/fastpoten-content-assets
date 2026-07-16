# -*- coding: utf-8 -*-
"""count_masks(가림 건수 채우기 명령) 자체 테스트.

새 파일인 이유: 기존 테스트는 차수·모듈 단위로 나뉘어 있다(test_ca1=스키마 / test_ca3=마스킹·투입 /
test_viewer=화면 / test_cafe_extract=추출기). count_masks는 새로 생긴 '정비 명령'이라 붙을 자리가
없고, 이 파일은 화면(viewer)과 명령(count_masks)이 같은 숫자를 내는지도 함께 본다.

핵심으로 보는 것:
  - 저장된 건수 = 상세 화면의 '총 N건'(viewer.mask_type_counts 합계)와 일치
  - 다시 실행해도 안전(이미 지금 규칙으로 센 글은 건너뜀), 규칙이 바뀌면 다시 대상이 됨
  - 본문 파일이 없는 글은 조용히 넘기지 않고 건수로 보고

★ 실제 창고(data/content_assets.sqlite3)는 건드리지 않는다 — 전부 tempfile.
사용: python tests/test_count_masks.py
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
import count_masks  # noqa: E402
import db  # noqa: E402
import load_rulebook  # noqa: E402
import masking  # noqa: E402
import viewer  # noqa: E402

PHONE = "010-1234-5678"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestCountMasks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        # 본문 파일(개인정보 포함) — 절대경로로 저장(ROOT_DIR과 join해도 그대로 쓰임)
        cls.pii_path = os.path.join(cls.tmp, "corpus", "p1", "body_clean.txt")
        cls.plain_path = os.path.join(cls.tmp, "corpus", "p2", "body_clean.txt")
        cls.gone_path = os.path.join(cls.tmp, "corpus", "p3", "body_clean.txt")  # 일부러 안 만듦
        _write(cls.pii_path, f"문의는 {PHONE} 로 주세요. 김민지쌤이 안내드려요. 박철수 담당입니다.")
        _write(cls.plain_path, "임상심리사 2급은 학사학위와 1년 수련이 필요해요.")

        # 룰북 패턴 적재는 한 번만(엑셀 읽기가 느림) → 테스트마다 이 파일을 복사해 씀
        cls.master = os.path.join(cls.tmp, "master.sqlite3")
        load_rulebook.run(db_path=cls.master)
        conn = db.get_connection(cls.master)
        db.init_db(conn)
        conn.execute("INSERT INTO staff (staff_name) VALUES ('김민지')")
        for pid, path in ((1, cls.pii_path), (2, cls.plain_path), (3, cls.gone_path)):
            conn.execute(
                "INSERT INTO posts (post_id, title, extraction_status, body_clean_path) "
                "VALUES (?, ?, '성공(자동추출)', ?)", (pid, f"글{pid}", path))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.dbp = os.path.join(self.tmp, f"t_{self.id().rsplit('.', 1)[-1]}.sqlite3")
        shutil.copy(self.master, self.dbp)

    def _rows(self):
        conn = db.get_connection(self.dbp)
        rows = {r["post_id"]: r for r in conn.execute(
            "SELECT post_id, mask_count, mask_rules_fingerprint FROM posts")}
        conn.close()
        return rows

    def test_fills_counts_and_fingerprint(self):
        stats = count_masks.run(db_path=self.dbp)
        self.assertEqual(stats["counted"], 2)          # 본문 파일 있는 글 2건
        self.assertEqual(stats["missing_file"], 1)     # 파일 없는 글은 못 셈
        rows = self._rows()
        self.assertGreater(rows[1]["mask_count"], 0)
        self.assertEqual(rows[2]["mask_count"], 0)     # 개인정보 없는 글은 0건(NULL 아님)
        self.assertIsNone(rows[3]["mask_count"])       # 못 센 글은 비워둔다 → 화면은 '다시 세기 필요'
        conn = db.get_connection(self.dbp)
        self.assertEqual(rows[1]["mask_rules_fingerprint"], masking.rules_fingerprint(conn))
        conn.close()

    def test_saved_count_matches_viewer_total(self):
        # ★ 저장된 숫자 = 상세 화면이 세는 '총 N건'과 같아야 한다(같은 재료·같은 방식)
        count_masks.run(db_path=self.dbp)
        conn = db.get_connection(self.dbp)
        for pid, path in ((1, self.pii_path), (2, self.plain_path)):
            saved = conn.execute("SELECT mask_count FROM posts WHERE post_id=?",
                                 (pid,)).fetchone()["mask_count"]
            self.assertEqual(saved, sum(viewer.mask_type_counts(conn, path).values()), f"post {pid}")
        conn.close()

    def test_rerun_skips_already_counted(self):
        count_masks.run(db_path=self.dbp)
        before = self._rows()
        stats = count_masks.run(db_path=self.dbp)      # 재실행 안전
        self.assertEqual(stats["counted"], 0)
        self.assertEqual(stats["skipped"], 2)          # 이미 센 2건은 건너뜀
        self.assertEqual(stats["missing_file"], 1)     # 파일 없는 글은 계속 대상(보고됨)
        self.assertEqual(self._rows()[1]["mask_count"], before[1]["mask_count"])

    def test_all_recounts_everything(self):
        count_masks.run(db_path=self.dbp)
        stats = count_masks.run(recount_all=True, db_path=self.dbp)
        self.assertEqual(stats["counted"], 2)
        self.assertEqual(stats["skipped"], 0)

    def test_rule_change_makes_posts_targets_again(self):
        count_masks.run(db_path=self.dbp)
        before = self._rows()[1]["mask_count"]
        conn = db.get_connection(self.dbp)             # 직원 이름 추가 = 가림 규칙 변경
        conn.execute("INSERT INTO staff (staff_name) VALUES ('박철수')")
        conn.commit()
        new_fp = masking.rules_fingerprint(conn)
        conn.close()
        self.assertNotEqual(before, None)
        self.assertNotEqual(new_fp, self._rows()[1]["mask_rules_fingerprint"])  # 저장값은 이제 옛 규칙

        stats = count_masks.run(db_path=self.dbp)      # 규칙이 바뀌었으니 다시 대상
        self.assertEqual(stats["counted"], 2)
        rows = self._rows()
        self.assertEqual(rows[1]["mask_count"], before + 1)   # '박철수' 1건이 늘어남
        self.assertEqual(rows[1]["mask_rules_fingerprint"], new_fp)

    def test_body_file_is_never_written(self):
        # ★ 불변 4: 이 명령은 본문 파일을 읽기만 한다
        before = open(self.pii_path, encoding="utf-8").read()
        count_masks.run(db_path=self.dbp)
        self.assertEqual(open(self.pii_path, encoding="utf-8").read(), before)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
