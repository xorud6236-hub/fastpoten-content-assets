# -*- coding: utf-8 -*-
"""ai_client.py — 밖(AI)과 통신하는 유일한 출구 + 마스킹 게이트

계획: docs/plans/20260721-원고생성화면-재설계.md §4-1(마스킹 게이트)·§4-2(열쇠)·§7(비용)

★ 이 모듈의 존재 이유는 "AI를 부르는 것"이 아니라 **나가는 글자에 마스킹을 강제하는 것**이다.
   밖으로 나가는 경로는 ask() 하나뿐이고, 마스킹은 그 안에서 일어난다(우회 경로 없음).

불변 1(개인정보 마스킹): 프롬프트 전체를 보내기 직전 masking.mask_text에 통과시키고,
  그러고도 전화번호·오픈채팅 링크가 남아 있으면 **보내지 않고 예외**(이중 안전장치, fail-closed).
불변 2의 정신(비밀은 창고·저장소에 안 넣는다): 열쇠는 환경변수 ANTHROPIC_API_KEY에서만 읽고
  파일·로그·예외 메시지·창고 어디에도 적지 않는다(앞 4자·길이도 남기지 않는다).

정직한 한계: 게이트는 우리가 아는 패턴만 가린다. 처음 보는 형식의 개인정보는 못 잡는다.

사용(다음 차수의 화면이 부른다):
    conn = db.get_connection()
    r = ai_client.ask(conn, "…재료 꾸러미…")
    r.status  # ok / no_key / sdk_missing / over_budget / refusal / error
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

import db
import masking

MODEL = "claude-opus-4-8"          # 날짜 접미사 없음(정확히 이 문자열)
DEFAULT_MAX_TOKENS = 16000         # 한국어 2,000~2,500자 원고 + 생각 여유

# 가격: 100만 토큰당 USD (claude-opus-4-8). 시점마다 바뀔 수 있는 추정치.
PRICE_IN_USD_PER_MTOK = 5.0
PRICE_OUT_USD_PER_MTOK = 25.0
USD_KRW = 1400.0                   # 추정치(고정 환율. 실제 청구액으로 검증할 것)

# 사용자 결정(계획 §8 Q2): 편당 1,000원 · 월 3만원
CAP_PER_CALL_KRW = 1000.0
CAP_PER_MONTH_KRW = 30000.0

SPEND_PATH = os.path.join(db.ROOT_DIR, "data", "ai_spend.json")      # 깃 제외(data/)
CALL_LOG_DIR = os.path.join(db.ROOT_DIR, "data", "ai_calls")        # 무엇이 나갔나 증거

# 게이트 통과 후 남아 있으면 안 되는 것(이중 안전장치. 마스킹 패턴과 별개의 최종 검사)
_LEAK_CHECKS = [
    ("전화번호", re.compile(r"01[0-9][-.\s]?\d{3,4}[-.\s]?\d{4}")),
    ("오픈채팅", re.compile(r"open\.kakao\.com/\S+", re.I)),
]

_spend_lock = threading.Lock()


class MaskingGateError(Exception):
    """게이트를 통과했는데도 개인정보가 남아 있어 발송을 중단했다."""


class AiResult(object):
    """호출 결과. 화면이 그대로 보여줄 수 있게 '무엇이 가려졌나'까지 담는다."""

    def __init__(self, status, text="", message="", mask_hits=None, masked_prompt="",
                 usage=None, cost_krw=0.0, est_cost_krw=0.0, log_path=""):
        self.status = status            # ok / no_key / sdk_missing / over_budget / refusal / error
        self.text = text
        self.message = message          # 사람에게 보여줄 안내(열쇠 값은 절대 담지 않는다)
        self.mask_hits = mask_hits or {}   # {"전화번호": 2, "직원 실명/닉네임": 1}
        self.masked_prompt = masked_prompt  # 실제로 나간 글자 그대로(가려진 상태)
        self.usage = usage or {}        # {"input_tokens": n, "output_tokens": n}
        self.cost_krw = cost_krw        # 실제 사용량 기준(추정 단가)
        self.est_cost_krw = est_cost_krw  # 보내기 전 최악 기준 예상
        self.log_path = log_path

    @property
    def ok(self):
        return self.status == "ok"


def mask_outgoing(conn, prompt):
    """★ 마스킹 게이트 — 밖으로 나가는 글자는 반드시 여기를 지난다.

    (가려진 글자, {패턴명: 건수}) 반환. 통과 후에도 전화번호·오픈채팅이 남으면 MaskingGateError.
    """
    if prompt is None or not str(prompt).strip():
        raise ValueError("보낼 내용이 비었습니다.")
    masked, hits = masking.mask_text(
        str(prompt), masking.load_regex_patterns(conn), masking.load_staff_names(conn))
    counts = {}
    for h in hits:
        counts[h["type"]] = counts.get(h["type"], 0) + 1
    for label, rx in _LEAK_CHECKS:          # 이중 안전장치(fail-closed)
        if rx.search(masked):
            raise MaskingGateError(
                "가림 뒤에도 %s 형태가 남아 있어 밖으로 보내지 않았습니다. "
                "개인정보 패턴을 확인하세요." % label)
    return masked, counts


def _usd_to_krw(usd):
    return usd * USD_KRW


def cost_krw(input_tokens, output_tokens):
    """토큰 → 원(추정 단가·추정 환율)."""
    usd = (input_tokens * PRICE_IN_USD_PER_MTOK
           + output_tokens * PRICE_OUT_USD_PER_MTOK) / 1_000_000.0
    return _usd_to_krw(usd)


def _load_spend(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (IOError, OSError, ValueError):
        return {}                        # 기록이 깨져도 호출은 막지 않는다(월 상한만 느슨해짐)


def month_spent_krw(path=SPEND_PATH, month=None):
    """이번 달 누적 사용액(원)."""
    month = month or datetime.now().strftime("%Y-%m")
    return float(_load_spend(path).get(month, 0.0))


def _add_spend(path, krw, month=None):
    month = month or datetime.now().strftime("%Y-%m")
    with _spend_lock:
        data = _load_spend(path)
        data[month] = float(data.get(month, 0.0)) + float(krw)   # 지난 달 기록은 지우지 않는다
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        return data[month]


def _write_call_log(dirpath, masked_prompt, counts):
    """무엇이 나갔나를 사람이 눈으로 확인할 수 있게 남긴다(가려진 상태 그대로, 열쇠 없음)."""
    try:
        os.makedirs(dirpath, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + str(int(time.time() * 1000) % 1000)
        path = os.path.join(dirpath, name + ".txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 가림: %s\n\n" % (json.dumps(counts, ensure_ascii=False) or "{}"))
            f.write(masked_prompt)
        return path
    except (IOError, OSError):
        return ""                        # 기록 실패가 원고 작업을 막지는 않는다


def _make_client():
    """(client, 오류상태, 안내문). 열쇠 값은 어디에도 담지 않는다."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None, "no_key", ("AI 열쇠가 없습니다. 환경변수 ANTHROPIC_API_KEY를 설정한 뒤 "
                                "다시 시도하세요(설정 후 프로그램을 다시 켜야 합니다).")
    try:
        import anthropic
    except ImportError:
        return None, "sdk_missing", "AI 연결 도구가 설치돼 있지 않습니다. `pip install anthropic` 후 다시 시도하세요."
    return anthropic.Anthropic(api_key=key), None, ""


def ask(conn, prompt, max_tokens=DEFAULT_MAX_TOKENS, client=None,
        spend_path=SPEND_PATH, log_dir=CALL_LOG_DIR):
    """★ 밖으로 나가는 유일한 경로. prompt는 이 함수 안에서 반드시 가려진다.

    client는 테스트에서 가짜를 넣기 위한 자리(실사용에서는 None → 환경변수 열쇠로 만든다).
    """
    masked, counts = mask_outgoing(conn, prompt)     # ← 우회 불가: 어떤 분기보다 먼저
    log_path = _write_call_log(log_dir, masked, counts)

    def _res(status, **kw):
        kw.setdefault("mask_hits", counts)
        kw.setdefault("masked_prompt", masked)
        kw.setdefault("log_path", log_path)
        return AiResult(status, **kw)

    if client is None:
        client, err, msg = _make_client()
        if err:
            return _res(err, message=msg)

    messages = [{"role": "user", "content": masked}]
    try:
        # 보내기 전에 값부터 — 상한을 넘으면 아예 보내지 않는다
        counted = client.messages.count_tokens(model=MODEL, messages=messages)
        in_tokens = int(getattr(counted, "input_tokens", 0))
    except Exception as e:                            # noqa: BLE001 — 통신 실패 종류를 가리지 않는다
        return _res("error", message="토큰 계산에 실패했습니다: %s" % _safe(e))

    est = cost_krw(in_tokens, max_tokens)             # 출력은 최악(max_tokens) 기준
    if est > CAP_PER_CALL_KRW:
        return _res("over_budget", est_cost_krw=est, message=(
            "이번 초안의 예상 비용이 약 %d원으로 한 편 상한(%d원)을 넘어 보내지 않았습니다. "
            "재료를 줄여 주세요." % (round(est), round(CAP_PER_CALL_KRW))))
    spent = month_spent_krw(spend_path)
    if spent + est > CAP_PER_MONTH_KRW:
        return _res("over_budget", est_cost_krw=est, message=(
            "이번 달 사용액 약 %d원 + 이번 예상 %d원이 월 상한(%d원)을 넘어 보내지 않았습니다."
            % (round(spent), round(est), round(CAP_PER_MONTH_KRW))))

    try:
        with client.messages.stream(model=MODEL, max_tokens=max_tokens,
                                    thinking={"type": "adaptive"},   # 생략하면 생각 없이 돈다
                                    messages=messages) as stream:
            msg = stream.get_final_message()
    except Exception as e:                            # noqa: BLE001
        return _res("error", est_cost_krw=est, message="AI 호출이 실패했습니다: %s" % _safe(e))

    usage = {"input_tokens": int(getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0),
             "output_tokens": int(getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0)}
    used = cost_krw(usage["input_tokens"], usage["output_tokens"])
    _add_spend(spend_path, used)                      # 실제로 쓴 만큼만 누적

    if getattr(msg, "stop_reason", None) == "refusal":   # content를 읽기 전에 처리(빈 배열일 수 있다)
        return _res("refusal", usage=usage, cost_krw=used, est_cost_krw=est,
                    message="AI가 이 요청에 답하기를 거절했습니다. 요청 내용을 바꿔 다시 시도하세요.")

    text = "".join(getattr(b, "text", "") for b in (getattr(msg, "content", None) or [])
                   if getattr(b, "type", "") == "text")
    return _res("ok", text=text, usage=usage, cost_krw=used, est_cost_krw=est)


def _safe(exc):
    """예외 메시지에 열쇠가 섞여 나갈 여지를 없앤다(값이 든 것은 지우고 종류만 남긴다)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    text = "%s: %s" % (type(exc).__name__, exc)
    if key and key in text:
        text = text.replace(key, "[가림]")
    return text
