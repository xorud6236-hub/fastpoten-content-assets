# -*- coding: utf-8 -*-
"""카페 추출기(extract_cafe) 자체 테스트 — 라이브 네이버 접속 없이 검증.

- 파서: 저장된 HTML 픽스처에서 제목·조회수·본문문단·이미지를 뽑는지.
- 상태분류: 삭제/로그인/비공개 문구를 실패 유형으로 정확히 판정하는지(성공으로 새면 안 됨).
- 파이프라인(오프라인): 픽스처 본문을 process_one에 주입 → 마스킹(전화번호·OO쌤)·문단·조회수(참고신호)
  ·이미지 보수분류가 DB에 저장되고, posts 건수 불변·멱등인지.

사용: python tests/test_cafe_extract.py
"""
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
import db  # noqa: E402
import load_rulebook  # noqa: E402
import extract_cafe as ex  # noqa: E402
import intake_manual as im  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "cafe_article_sample.html")
FIXTURE_URL = "https://cafe.naver.com/studentstudyhard/2712612"


def _fixture_html():
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


class TestParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = ex.parse_article_html(_fixture_html())

    def test_title(self):
        self.assertEqual(self.parsed["title"],
                         "임상심리사 2급, 응시자격부터 정확히 알아볼게요")

    def test_view_count_is_int(self):
        self.assertEqual(self.parsed["view_count"], 1234)  # "조회 1,234" → 1234

    def test_publish_date(self):
        self.assertEqual(self.parsed["publish_date"], "2026-01-06")

    def test_body_paragraphs_present(self):
        joined = "\n".join(self.parsed["lines"])
        self.assertIn("응시자격을 짚어드릴게요", joined)
        self.assertIn("정리하면", joined)
        # 원문에 개인정보가 그대로 들어옴(이후 마스킹 대상)
        self.assertIn("010-1234-5678", joined)
        self.assertIn("민지쌤", joined)

    def test_images_extracted_high_res(self):
        # 이미지 2장, 고해상 우선(data-lazy-src의 w800) 선택
        self.assertEqual(len(self.parsed["images"]), 2)
        self.assertTrue(all(i["src"].startswith("http") for i in self.parsed["images"]))
        self.assertTrue(all("type=w800" in i["src"] for i in self.parsed["images"]))

    def test_images_nearby_positions(self):
        # 이미지1은 문단1 뒤(nearby=1), 이미지2는 문단2 뒤(nearby=2) — 원본 순서 보존
        nearby = [i["nearby_paragraph_no"] for i in self.parsed["images"]]
        self.assertEqual(nearby, [1, 2])

    def test_leading_notice_removed(self):
        # 맨 앞 '카페 운영진 허가를 받아 작성' 고지 문구는 본문에서 제거
        joined = "\n".join(self.parsed["lines"])
        self.assertNotIn("허가를 받아 작성", joined)
        self.assertTrue(self.parsed["lines"][0].startswith("안녕하세요"))

    def test_internal_soft_linebreaks_preserved(self):
        # ★ 수정 A: 구간 내부 원본 줄 구조(\n)를 보존해야 함(공백 벽 텍스트 제거).
        #   같은 구간의 여러 se-text-paragraph(원본 줄)·여러 텍스트 컴포넌트가 \n으로 이어짐.
        body_text = "\n".join(self.parsed["lines"])
        # 한 컴포넌트 내 두 se-text-paragraph가 \n으로(공백 병합 아님)
        self.assertIn("응시자격은 학사학위와 1년 수련이 필요합니다.\n이건 꼭 확인하세요.", body_text)
        self.assertNotIn("필요합니다. 이건 꼭 확인하세요.", body_text)   # 공백으로 안 뭉침
        # 같은 구간의 다른 텍스트 컴포넌트도 \n으로 이어짐
        self.assertIn("이건 꼭 확인하세요.\n궁금하면 담당 민지쌤에게 문의주세요.", body_text)
        # 구간(이미지) 경계는 여전히 빈 줄 1개(\n\n) → 구간 수 = 이미지 경계 수
        segs = [s for s in re.split(r"\n\s*\n", body_text.strip()) if s.strip()]
        self.assertEqual(len(segs), 3)                       # 이미지 2개 → 텍스트 구간 3개
        self.assertEqual(len(self.parsed["images"]), 2)
        self.assertIn("\n", segs[1])                         # 구간 내부에 단일 \n 살아있음
        # clean_body(정제) 후에도 내부 단일 \n 보존(과분할 없이 벽 텍스트만 해소)
        cleaned = im.clean_body(body_text)
        self.assertIn("필요합니다.\n이건 꼭 확인하세요.", cleaned)

    def test_text_between_images_merged_into_one_paragraph(self):
        # ★ 문단 경계 = 이미지. 두 이미지 사이의 se-text 컴포넌트 여러 개가 한 문단으로 병합.
        #   텍스트 구간 3개(이미지 2개 사이) → 문단 3개(컴포넌트 개수만큼 쪼개지지 않음).
        paras = im.split_paragraphs(im.clean_body("\n".join(self.parsed["lines"])))
        self.assertEqual(len(paras), 3)
        # 구간1: 컴포넌트 2개가 같은 문단
        self.assertIn("짚어드릴게요", paras[0])
        self.assertIn("핵심만 정리", paras[0])
        # 구간2: 컴포넌트 2개 + 내부 소프트 줄바꿈이 모두 같은 문단
        self.assertIn("학사학위", paras[1])
        self.assertIn("이건 꼭 확인", paras[1])
        self.assertIn("민지쌤", paras[1])
        self.assertIn("010-1234-5678", paras[1])
        # 구간3: 컴포넌트 2개가 같은 문단
        self.assertIn("정리하면", paras[2])
        self.assertIn("감사합니다", paras[2])


# --- se-component 블록 헬퍼(실제 SmartEditor 3 구조 모사) ---
def _text_comp(*paragraphs):
    ps = "".join(f'<p class="se-text-paragraph"><span>{t}</span></p>' for t in paragraphs)
    return ('<div class="se-component se-text se-l-default"><div class="se-component-content">'
            '<div class="se-section se-section-text"><div class="se-module se-module-text">'
            + ps + "</div></div></div></div>")


def _img_comp(src, lazy=None):
    lazy_attr = f' data-lazy-src="{lazy}"' if lazy else ""
    return ('<div class="se-component se-image se-l-default"><div class="se-component-content">'
            '<div class="se-section se-section-image"><div class="se-module se-module-image">'
            f'<a><img src="{src}"{lazy_attr} alt=""></a>'
            "</div></div></div></div>")


def _container(*blocks):
    return ('<div class="article_viewer"><div class="se-main-container">'
            + "".join(blocks) + "</div></div>")


class TestBlockParagraphs(unittest.TestCase):
    """★ 문단 경계 = 이미지. 두 이미지 사이 텍스트 컴포넌트들을 한 문단으로 병합(과분할 방지),
    블록 순서·이미지 위치 보존, 엣지(이미지 0개·연속 이미지) 동작."""

    def test_images_are_paragraph_boundaries(self):
        # 텍스트 구간마다 se-text 컴포넌트 여러 개 → 이미지 사이 컴포넌트들이 한 문단으로 병합.
        body = _container(
            _text_comp("A 첫째 컴포넌트"),
            _text_comp("A 둘째 컴포넌트"),
            _img_comp("https://ex.com/a.jpg"),
            _text_comp("B 첫째 컴포넌트"),
            _text_comp("B 둘째 컴포넌트"),
            _img_comp("https://ex.com/b.jpg"),
            _text_comp("C 컴포넌트"),
        )
        parsed = ex.parse_article_html(body)
        paras = im.split_paragraphs(im.clean_body("\n".join(parsed["lines"])))
        self.assertEqual(len(paras), 3)                 # 텍스트 구간 3개 = 문단 3개
        self.assertIn("A 첫째 컴포넌트", paras[0])
        self.assertIn("A 둘째 컴포넌트", paras[0])        # 같은 구간 다른 컴포넌트 → 같은 문단
        self.assertIn("B 첫째 컴포넌트", paras[1])
        self.assertIn("B 둘째 컴포넌트", paras[1])        # 병합
        self.assertIn("C 컴포넌트", paras[2])

    def test_block_order_preserved_with_nearby(self):
        # 이미지↔텍스트 교차 순서·이미지 위치·고해상 우선 보존
        body = _container(
            _text_comp("문단 A"),
            _img_comp("https://ex.com/a.jpg", lazy="https://ex.com/a_big.jpg"),
            _text_comp("문단 B"),
            _img_comp("https://ex.com/b.jpg"),
            _text_comp("문단 C"),
        )
        parsed = ex.parse_article_html(body)
        paras = im.split_paragraphs(im.clean_body("\n".join(parsed["lines"])))
        self.assertEqual(len(paras), 3)
        imgs = parsed["images"]
        self.assertEqual(len(imgs), 2)
        self.assertEqual(imgs[0]["src"], "https://ex.com/a_big.jpg")  # 고해상(data-lazy-src) 우선
        self.assertEqual(imgs[1]["src"], "https://ex.com/b.jpg")      # lazy 없으면 src
        self.assertEqual(imgs[0]["nearby_paragraph_no"], 1)          # 문단A 뒤
        self.assertEqual(imgs[1]["nearby_paragraph_no"], 2)          # 문단B 뒤

    def test_zero_images_is_one_paragraph(self):
        # 이미지 0개 글 → 전체 텍스트가 1문단(사용자 규칙상 수용).
        body = _container(
            _text_comp("첫 컴포넌트"),
            _text_comp("둘째 컴포넌트"),
            _text_comp("셋째 컴포넌트"),
        )
        parsed = ex.parse_article_html(body)
        self.assertEqual(len(parsed["images"]), 0)
        paras = im.split_paragraphs(im.clean_body("\n".join(parsed["lines"])))
        self.assertEqual(len(paras), 1)
        self.assertIn("첫 컴포넌트", paras[0])
        self.assertIn("셋째 컴포넌트", paras[0])

    def test_consecutive_images_make_no_empty_paragraph(self):
        # 연속 이미지(사이 텍스트 없음) → 빈 문단 만들지 않음. 둘 다 앞 문단을 가리킴.
        body = _container(
            _text_comp("문단 A"),
            _img_comp("https://ex.com/1.jpg"),
            _img_comp("https://ex.com/2.jpg"),
            _text_comp("문단 B"),
        )
        parsed = ex.parse_article_html(body)
        paras = im.split_paragraphs(im.clean_body("\n".join(parsed["lines"])))
        self.assertEqual(len(paras), 2)                 # A, B — 사이 빈 문단 없음
        imgs = parsed["images"]
        self.assertEqual(len(imgs), 2)
        self.assertEqual(imgs[0]["nearby_paragraph_no"], 1)  # 둘 다 문단A 뒤
        self.assertEqual(imgs[1]["nearby_paragraph_no"], 1)

    def test_leading_permission_notice_removed(self):
        body = _container(
            _text_comp("이 글은 카페 운영진의 허가를 받아 작성되었습니다."),
            _text_comp("본문 첫 문단입니다."),
            _img_comp("https://ex.com/x.jpg"),
            _text_comp("본문 둘째 문단입니다."),
        )
        parsed = ex.parse_article_html(body)
        joined = "\n".join(parsed["lines"])
        self.assertNotIn("허가를 받아 작성", joined)
        paras = im.split_paragraphs(im.clean_body(joined))
        self.assertEqual(len(paras), 2)                 # 이미지 경계로 첫/둘째 문단 분리
        self.assertIn("본문 첫 문단", paras[0])
        self.assertIn("본문 둘째 문단", paras[1])

    def test_notice_removal_is_conservative(self):
        # 첫 블록이 아니면 고지어를 포함해도 제거하지 않음(본문 오제거 방지).
        body = _container(
            _text_comp("본문 첫 문단입니다."),
            _img_comp("https://ex.com/x.jpg"),
            _text_comp("이 글은 카페 운영진의 허가를 받아 작성되었습니다."),
        )
        parsed = ex.parse_article_html(body)
        joined = "\n".join(parsed["lines"])
        self.assertIn("허가를 받아 작성", joined)         # 첫 블록 아님 → 유지
        paras = im.split_paragraphs(im.clean_body(joined))
        self.assertEqual(len(paras), 2)


class TestContainerScope(unittest.TestCase):
    """본문은 div.article_viewer > div.se-main-container 서브트리로 구조적으로 스코프 →
    바깥 댓글·프로필·관련글 UI/이미지는 0(정규식 deny-list 없이 구조로 배제)."""

    def test_outside_ui_and_profile_excluded(self):
        html = (
            '<div class="ArticleTitle"><h3 class="title_text">제목</h3></div>'
            + _container(
                _text_comp("본문 첫 문단입니다."),
                _img_comp("https://cafeptthumb.example.com/body1.jpg?type=w800"),
                _text_comp("본문 둘째 문단입니다."),
                _img_comp("https://cafeptthumb.example.com/body2.jpg?type=w740"),
            )
            # se-main-container 바깥 UI — 구조적으로 제외돼야 함
            + '<div class="CommentBox">'
              '<img src="https://ssl.pstatic.net/static/cafe/default/cafe_profile.png" alt="프로필">'
              '<p class="se-text-paragraph"><span>댓글 내용입니다</span></p></div>'
            + '<div class="RelatedArticles">'
              '<img src="https://cafeptthumb.example.com/t.jpg?type=f100_100"></div>'
        )
        parsed = ex.parse_article_html(html)
        srcs = [i["src"] for i in parsed["images"]]
        self.assertEqual(len(srcs), 2)                       # 본문 이미지 2장만
        self.assertTrue(all("/body" in s for s in srcs))
        for frag in ("cafe_profile", "type=f100_100"):       # 프로필·관련글 썸네일 0
            self.assertFalse(any(frag in s for s in srcs), f"{frag} 이 새어들어옴")
        # 바깥 댓글 텍스트도 본문 문단에 안 들어옴
        self.assertNotIn("댓글 내용", "\n".join(parsed["lines"]))


class TestFailureClassify(unittest.TestCase):
    def test_deleted(self):
        self.assertEqual(ex.classify_failure("삭제된 게시글입니다"), "실패-삭제된글")

    def test_login(self):
        self.assertEqual(ex.classify_failure("로그인이 필요한 서비스입니다"), "실패-로그인필요")

    def test_private(self):
        self.assertEqual(ex.classify_failure("멤버만 볼 수 있는 게시판입니다"), "실패-비공개게시판")

    def test_normal_is_none(self):
        self.assertIsNone(ex.classify_failure("안녕하세요 응시자격 안내드립니다"))


class TestOfflinePipeline(unittest.TestCase):
    """process_one에 픽스처 HTML을 주입(라이브 fetch 우회)해 끝-끝 저장을 검증."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        cls.dbp = os.path.join(tmp, "t.sqlite3")
        load_rulebook.run(db_path=cls.dbp)
        conn = db.get_connection(cls.dbp)
        db.init_db(conn)
        # 본문 없는 기존 posts 행(추출 대상) 1건 + 기존 account_id 라벨(보존 검증용)
        conn.execute(
            "INSERT INTO posts (normalized_url, original_url, cafe_name, keyword, "
            "staff_name, publish_date, account_id, extraction_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (FIXTURE_URL, FIXTURE_URL, "공준모", "임상심리사2급 응시자격",
             "가상인", "2024-03-15", "평생교육기관67", "링크오류"))
        conn.commit()
        cls.n_before = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        post = ex.find_post(conn, FIXTURE_URL)
        html = _fixture_html()
        # download=False: 이미지 실제 다운로드 없이(오프라인) 분류만 저장
        cls.res = ex.process_one(conn, post, html=html, page_text=html, download=False)
        # 멱등 재실행
        post2 = ex.find_post(conn, FIXTURE_URL)
        cls.res2 = ex.process_one(conn, post2, html=html, page_text=html, download=False)
        conn.close()

    def setUp(self):
        self.conn = db.get_connection(self.dbp)

    def tearDown(self):
        self.conn.close()

    def test_success_status(self):
        self.assertTrue(self.res["ok"])
        self.assertEqual(self.res["status"], "성공(자동추출)")
        row = self.conn.execute(
            "SELECT extraction_status FROM posts WHERE post_id=?",
            (self.res["post_id"],)).fetchone()
        self.assertEqual(row["extraction_status"], "성공(자동추출)")

    def test_pub_ref_masks_pii(self):
        # ★ 핵심 불변 1: 참고용 본문에서 전화번호·OO쌤이 가려져야 함
        path = self.conn.execute(
            "SELECT body_pub_ref_path FROM posts WHERE post_id=?",
            (self.res["post_id"],)).fetchone()["body_pub_ref_path"]
        text = open(os.path.join(ROOT, path), encoding="utf-8").read()
        self.assertNotIn("010-1234-5678", text)
        self.assertNotIn("민지쌤", text)

    def test_raw_keeps_pii(self):
        # 원문(body_raw)은 불변 4 — 그대로 보존
        path = self.conn.execute(
            "SELECT body_raw_path FROM posts WHERE post_id=?",
            (self.res["post_id"],)).fetchone()["body_raw_path"]
        raw = open(os.path.join(ROOT, path), encoding="utf-8").read()
        self.assertIn("010-1234-5678", raw)

    def test_view_count_is_reference_signal(self):
        # 불변 3: 조회수는 reference_signals에만(성과 컬럼 없음)
        row = self.conn.execute(
            "SELECT view_count FROM reference_signals WHERE post_id=? AND collected_from_sheet=?",
            (self.res["post_id"], ex.AUTO_VIEW_MARK)).fetchone()
        self.assertEqual(row["view_count"], 1234)

    def test_images_conservative_reuse(self):
        rows = self.conn.execute(
            "SELECT reuse_scope FROM post_images WHERE post_id=?",
            (self.res["post_id"],)).fetchall()
        self.assertEqual(len(rows), 2)
        # 재사용 허용값을 자동 부여하지 않음(불변 1) — 보수값만
        self.assertTrue(all(r["reuse_scope"] == "image_rights_review" for r in rows))

    def test_existing_label_preserved(self):
        # upsert가 기존 라벨 5개를 NULL로 덮어쓰지 않아야(데이터 손실 방지 — CA-2 라벨 보존)
        row = self.conn.execute(
            "SELECT account_id, cafe_name, keyword, staff_name, publish_date "
            "FROM posts WHERE post_id=?",
            (self.res["post_id"],)).fetchone()
        self.assertEqual(row["account_id"], "평생교육기관67")
        self.assertEqual(row["cafe_name"], "공준모")
        self.assertEqual(row["keyword"], "임상심리사2급 응시자격")
        self.assertEqual(row["staff_name"], "가상인")
        self.assertEqual(row["publish_date"], "2024-03-15")

    def test_no_new_rows_and_idempotent(self):
        # 새 posts 행 안 생김(기존 행 갱신) + 재실행해도 문단·조회수 중복 없음
        n_after = self.conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        self.assertEqual(n_after, self.n_before)
        self.assertEqual(self.res["post_id"], self.res2["post_id"])
        pid = self.res["post_id"]
        n_sig = self.conn.execute(
            "SELECT COUNT(*) c FROM reference_signals WHERE post_id=? AND collected_from_sheet=?",
            (pid, ex.AUTO_VIEW_MARK)).fetchone()["c"]
        self.assertEqual(n_sig, 1)


class TestFailurePath(unittest.TestCase):
    """실패 경로 — 별도 DB(위 파이프라인 클래스의 건수 검증을 오염시키지 않도록 분리)."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.dbp = os.path.join(tmp, "t.sqlite3")
        load_rulebook.run(db_path=self.dbp)
        self.conn = db.get_connection(self.dbp)
        db.init_db(self.conn)
        self.conn.execute(
            "INSERT INTO posts (normalized_url, extraction_status) VALUES (?, ?)",
            ("https://cafe.naver.com/studentstudyhard/9999999", "링크오류"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_failure_logged_with_reason(self):
        # 삭제 문구 주입 → 실패 상태 + 로그 사유 100%(v9), method='playwright'
        post = ex.find_post(self.conn, "https://cafe.naver.com/studentstudyhard/9999999")
        r = ex.process_one(self.conn, post, html="<html></html>",
                           page_text="삭제된 게시글입니다", download=False)
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "실패-삭제된글")
        log = self.conn.execute(
            "SELECT status, error_detail, method FROM extraction_logs WHERE post_id=?",
            (post["post_id"],)).fetchone()
        self.assertEqual(log["status"], "실패-삭제된글")
        self.assertTrue(log["error_detail"])       # 사유 존재
        self.assertEqual(log["method"], "playwright")
        # posts.extraction_status도 실패로 갱신
        st = self.conn.execute(
            "SELECT extraction_status FROM posts WHERE post_id=?",
            (post["post_id"],)).fetchone()["extraction_status"]
        self.assertEqual(st, "실패-삭제된글")

    def test_empty_body_is_access_failure(self):
        # 정상 문구지만 본문 없음 → 실패-접근불가(기타)
        post = ex.find_post(self.conn, "https://cafe.naver.com/studentstudyhard/9999999")
        r = ex.process_one(self.conn, post, html="<html><body></body></html>",
                           page_text="정상 페이지지만 본문 구조 없음", download=False)
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "실패-접근불가(기타)")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
