# -*- coding: utf-8 -*-
"""CB-1 자체 테스트 — 카드뉴스 템플릿 채움·디자인 규칙·렌더 검증.

사용: python tests/test_cb1.py
"""
import glob
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
import cardnews  # noqa: E402


class TestRenderHtml(unittest.TestCase):
    def sample(self):
        return {
            "template": "card_condition",
            "chip": "학점은행제 · 사회복지",
            "title": "사회복지사 2급\n응시 조건",
            "subtitle": "테스트",
            "rows": [{"key": "필요 학위", "value": "전문학사 이상"}],
            "note": "기간은 <b>개인차</b>가 있어요.",
            "brand": "패스트포텐",
            "source": "룰북 V4.2",
        }

    def test_no_leftover_placeholder(self):
        html = cardnews.render_html(self.sample())
        self.assertNotIn("{{", html)
        self.assertNotIn("}}", html)

    def test_newline_becomes_br(self):
        html = cardnews.render_html(self.sample())
        self.assertIn("사회복지사 2급<br>응시 조건", html)

    def test_rows_rendered(self):
        html = cardnews.render_html(self.sample())
        self.assertIn("필요 학위", html)
        self.assertIn("전문학사 이상", html)

    def test_note_allows_bold_but_value_escaped(self):
        c = self.sample()
        c["rows"] = [{"key": "x", "value": "<script>bad</script>"}]
        html = cardnews.render_html(c)
        self.assertIn("<b>개인차</b>", html)          # note의 <b>는 유지
        self.assertNotIn("<script>", html)             # 행 값의 태그는 이스케이프
        self.assertIn("&lt;script&gt;", html)

    def test_missing_template_raises(self):
        c = self.sample()
        c["template"] = "존재하지않는템플릿"
        with self.assertRaises(FileNotFoundError):
            cardnews.render_html(c)


class TestDesignRule(unittest.TestCase):
    def test_template_has_no_hardcoded_color(self):
        # CLAUDE.md 디자인 규칙: 색은 brand.css에서만. 템플릿에 hex/rgb 색 금지.
        for tpl in glob.glob(os.path.join(ROOT, "templates", "*.html")):
            text = open(tpl, encoding="utf-8").read()
            self.assertFalse(re.search(r"#[0-9a-fA-F]{3,6}\b", text),
                             f"{os.path.basename(tpl)}에 하드코딩 색 있음")
            self.assertNotIn("rgb(", text)

    def test_brand_css_defines_tokens(self):
        css = open(os.path.join(ROOT, "templates", "brand.css"), encoding="utf-8").read()
        for token in ("--brand", "--accent", "--ink", "--font"):
            self.assertIn(token, css)


class TestExamplesRender(unittest.TestCase):
    def test_all_examples_have_required_fields(self):
        import json
        files = glob.glob(os.path.join(ROOT, "examples", "cardnews", "*.json"))
        self.assertGreaterEqual(len(files), 3)
        for jp in files:
            c = json.load(open(jp, encoding="utf-8"))
            for field in ("title", "rows", "source"):
                self.assertIn(field, c, f"{os.path.basename(jp)}에 {field} 없음")
            self.assertIn("룰북", c["source"], "팩트 출처(룰북) 표기 필수 — 불변 6")

    def test_render_png_smoke(self):
        # 브라우저가 있으면 실제 PNG 1장 렌더 확인. 없으면 스킵.
        try:
            browser = cardnews.find_browser()
        except FileNotFoundError:
            self.skipTest("Chrome/Edge 없음 — 렌더 테스트 스킵")
        files = sorted(glob.glob(os.path.join(ROOT, "examples", "cardnews", "*.json")))
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "smoke.png")
            cardnews.render_file(files[0], out, browser)
            self.assertTrue(os.path.exists(out))
            self.assertGreater(os.path.getsize(out), 10000)  # 유의미한 PNG


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
