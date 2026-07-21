# -*- coding: utf-8 -*-
"""ai_client(밖과 통신하는 층) 자체 테스트 — ★ 실제 API를 부르지 않는다(가짜 응답).

보는 것(계획 20260721-원고생성화면-재설계.md §4-1·4-2·§7):
  ① 전화번호·OO쌤·오픈채팅이 든 글을 보내면 나간 글자에 원본이 없다(불변 1)
  ② 마스킹을 우회하는 경로가 없다 — 밖으로 보내는 코드가 ask() 한 곳뿐
  ③ 열쇠가 없으면 죽지 않고 '없음'을 알린다
  ④ 열쇠 값이 안내문·기록 파일 어디에도 안 나온다(불변 2의 정신)
  ⑤ 편당/월 비용 상한을 넘으면 아예 보내지 않는다
  ⑥ stop_reason='refusal'이면 content를 읽기 전에 처리한다

★ 실제 창고(data/)는 건드리지 않는다 — 전부 tempfile.
사용: python tests/test_ai_client.py
"""
import json
import os
import shutil
from datetime import datetime
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
import ai_client  # noqa: E402
import db  # noqa: E402
import load_rulebook  # noqa: E402

PHONE = "010-1234-5678"
OPENCHAT = "https://open.kakao.com/o/gAbCdEfG"
FAKE_KEY = "sk-ant-test-DO-NOT-USE-0000"


class _Usage(object):
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


class _Block(object):
    type = "text"

    def __init__(self, text):
        self.text = text


class _Msg(object):
    def __init__(self, stop_reason="end_turn", text="초안입니다.", in_tok=100, out_tok=50):
        self.stop_reason = stop_reason
        self._text = text
        self.usage = _Usage(in_tok, out_tok)

    @property
    def content(self):
        return [_Block(self._text)]


class _RefusalMsg(object):
    """거절 응답 — content를 읽으면 테스트가 깨진다(읽기 전에 처리해야 한다)."""
    stop_reason = "refusal"

    def __init__(self):
        self.usage = _Usage(100, 0)

    @property
    def content(self):
        raise AssertionError("refusal인데 content를 읽었다")


class _Stream(object):
    def __init__(self, msg):
        self._msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._msg


class _Messages(object):
    def __init__(self, parent):
        self.p = parent

    def count_tokens(self, model=None, messages=None):
        self.p.counted = messages
        return _Usage(self.p.in_tokens, 0)

    def stream(self, model=None, max_tokens=None, thinking=None, messages=None):
        self.p.sent = messages
        self.p.thinking = thinking
        self.p.model = model
        if self.p.raise_text:
            raise RuntimeError(self.p.raise_text)
        return _Stream(self.p.msg)


class FakeClient(object):
    def __init__(self, msg=None, in_tokens=100, raise_text=""):
        self.msg = msg or _Msg()
        self.in_tokens = in_tokens
        self.raise_text = raise_text
        self.sent = None
        self.counted = None
        self.thinking = None
        self.model = None
        # 진짜 SDK와 같은 모양 — client.messages.count_tokens / .stream
        self.messages = _Messages(self)


class TestAiClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.dbp = os.path.join(cls.tmp, "t.sqlite3")
        load_rulebook.run(db_path=cls.dbp)          # 개인정보 패턴 6종 적재
        conn = db.get_connection(cls.dbp)
        db.init_db(conn)
        conn.execute("INSERT INTO staff (staff_name) VALUES ('가상인')")
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.conn = db.get_connection(self.dbp)
        self.spend = os.path.join(self.tmp, "spend_%s.json" % self.id().rsplit(".", 1)[-1])
        self.logs = os.path.join(self.tmp, "logs_%s" % self.id().rsplit(".", 1)[-1])
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY

    def tearDown(self):
        self.conn.close()
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def _ask(self, prompt, client, **kw):
        return ai_client.ask(self.conn, prompt, client=client,
                             spend_path=self.spend, log_dir=self.logs, **kw)

    # ① 나가는 글자에 원본이 없다
    def test_pii_never_leaves(self):
        c = FakeClient()
        prompt = "문의는 %s 로 주세요. 가상인쌤이 안내합니다. %s 참여하세요." % (PHONE, OPENCHAT)
        r = self._ask(prompt, c)
        self.assertTrue(r.ok)
        sent = c.sent[0]["content"]
        for original in (PHONE, "가상인", OPENCHAT):
            self.assertNotIn(original, sent)
        self.assertNotIn(PHONE, r.masked_prompt)
        self.assertTrue(sum(r.mask_hits.values()) >= 3, r.mask_hits)
        # 기록 파일에도 가려진 상태로만 남는다
        with open(r.log_path, encoding="utf-8") as f:
            self.assertNotIn(PHONE, f.read())

    # ①-b 게이트가 못 가린 게 남으면 보내지 않는다(fail-closed)
    def test_gate_blocks_when_patterns_missing(self):
        conn2 = db.get_connection(os.path.join(self.tmp, "empty.sqlite3"))
        db.init_db(conn2)
        c = FakeClient()
        with self.assertRaises(ai_client.MaskingGateError):
            ai_client.ask(conn2, "연락처 %s" % PHONE, client=c,
                          spend_path=self.spend, log_dir=self.logs)
        self.assertIsNone(c.sent)       # 한 글자도 안 나갔다
        conn2.close()

    # ② 우회 경로 없음 — 밖으로 보내는 코드가 한 곳뿐
    def test_single_outbound_path(self):
        with open(os.path.join(ROOT, "src", "ai_client.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("client.messages.stream"), 1)
        self.assertNotIn("messages.create", src)
        # 그 한 곳(ask)보다 마스킹이 먼저 일어난다
        body = src.split("def ask(")[1]
        self.assertLess(body.index("mask_outgoing("), body.index("client.messages.stream"))

    # ③ 열쇠가 없으면 죽지 않는다
    def test_no_key(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        r = self._ask("안녕", None)
        self.assertEqual(r.status, "no_key")
        self.assertIn("열쇠", r.message)
        self.assertTrue(r.mask_hits == {} or isinstance(r.mask_hits, dict))

    # ④ 열쇠 값은 안내문·기록 어디에도 안 나온다
    def test_key_never_printed(self):
        c = FakeClient(raise_text="401 unauthorized key=%s" % FAKE_KEY)
        r = self._ask("연락처 %s" % PHONE, c)
        self.assertEqual(r.status, "error")
        self.assertNotIn(FAKE_KEY, r.message)
        with open(r.log_path, encoding="utf-8") as f:
            self.assertNotIn(FAKE_KEY, f.read())

    # ⑤ 비용 상한 — 편당
    def test_over_budget_per_call(self):
        huge = int(ai_client.CAP_PER_CALL_KRW * 1_000_000 /
                   (ai_client.PRICE_IN_USD_PER_MTOK * ai_client.USD_KRW)) + 10_000
        c = FakeClient(in_tokens=huge)
        r = self._ask("긴 재료", c)
        self.assertEqual(r.status, "over_budget")
        self.assertIsNone(c.sent)                   # 보내지 않았다
        self.assertGreater(r.est_cost_krw, ai_client.CAP_PER_CALL_KRW)

    # ⑤-b 비용 상한 — 월 누적
    def test_over_budget_per_month(self):
        with open(self.spend, "w", encoding="utf-8") as f:
            json.dump({datetime.now().strftime("%Y-%m"): ai_client.CAP_PER_MONTH_KRW}, f)
        c = FakeClient()
        r = self._ask("짧은 재료", c)
        self.assertEqual(r.status, "over_budget")
        self.assertIsNone(c.sent)
        self.assertIn("월 상한", r.message)

    def test_spend_accumulates(self):
        c = FakeClient()
        r = self._ask("짧은 재료", c)
        self.assertGreater(r.cost_krw, 0)
        self._ask("짧은 재료", c)
        self.assertAlmostEqual(ai_client.month_spent_krw(self.spend), r.cost_krw * 2, places=6)

    # ⑥ refusal은 content를 읽기 전에 처리
    def test_refusal(self):
        c = FakeClient(msg=_RefusalMsg())
        r = self._ask("짧은 재료", c)
        self.assertEqual(r.status, "refusal")
        self.assertEqual(r.text, "")

    # 모델·thinking 설정이 실제로 넘어간다(생략하면 생각 없이 돈다)
    def test_model_and_thinking(self):
        c = FakeClient()
        self._ask("짧은 재료", c)
        self.assertEqual(c.model, "claude-opus-4-8")
        self.assertEqual(c.thinking, {"type": "adaptive"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
