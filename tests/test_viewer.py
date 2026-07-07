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
import sys
import tempfile
import threading
import unittest
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
            "INSERT INTO posts (post_id, title, cafe_name, board_name, staff_name, "
            "publish_date, content_length_type, extraction_status, "
            "body_raw_path, body_clean_path, body_pub_ref_path) "
            "VALUES (21512, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("임상심리사 2급 응시자격", "공준모", "질문게시판", "김민지",
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
        # 이미지: 인물 포함/원본금지 → 썸네일 아님(자리표시)
        conn.execute(
            "INSERT INTO post_images (post_id, image_order, image_type, reuse_scope, "
            "contains_person, local_path) VALUES (21512, 1, '본문이미지', "
            "'image_pattern_only', 1, 'corpus/post/img.png')")
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

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.read().decode("utf-8")

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

    def test_image_is_placeholder_not_thumbnail(self):
        # 인물 포함/원본금지 → 실제 그림(img 태그·/img 링크) 대신 자리표시
        h = self._get("/post?id=21512")
        self.assertIn("미리보기 가림", h)
        self.assertNotIn("/img?id=", h)
        self.assertIn("원본 재사용 금지", h)

    def test_list_shows_post(self):
        h = self._get("/")
        self.assertIn("추출 글 품질 확인", h)
        self.assertIn("임상심리사 2급 응시자격", h)

    def test_unknown_post_is_friendly(self):
        h = self._get("/post?id=99999")
        self.assertIn("그런 글이 없습니다.", h)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
