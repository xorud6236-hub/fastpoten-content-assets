# -*- coding: utf-8 -*-
"""cardnews.py — 카드뉴스 렌더러 (CB-1, 트랙 B 2층)

설계(CLAUDE.md 현재 결정 5): **AI는 내용 JSON만, 렌더링은 템플릿**.
- 입력: 콘텐츠 JSON 1건(제목·조건 행들·주의문 등) — 지금은 사람이 룰북 팩트로 작성,
  나중에 생성 엔진(트랙 C)이 같은 형식으로 뽑는다.
- 처리: templates/의 HTML 템플릿에 값 치환 → 임시 HTML → **헤드리스 Chrome**으로 PNG 캡처.
- 새 파이썬 의존성 없음: 이미 깔린 Chrome을 --headless=new --screenshot로 사용.

불변 6(팩트는 룰북에서만): JSON의 각 값은 룰북 출처를 함께 적는다(source 필드).
개인정보·CTA·전화번호는 카드에 넣지 않는다(불변 1·8: 발행 채널 유도는 사람이).

콘텐츠 JSON 형식(examples/cardnews/*.json):
{
  "template": "card_condition",
  "chip": "학점은행제 · 사회복지",
  "title": "사회복지사 2급\n응시 조건",
  "subtitle": "고졸부터 시작할 수 있어요",
  "rows": [ {"key": "필요 학위", "value": "전문학사 이상"}, ... ],
  "note": "기간·비용은 <b>개인 학력·보유학점</b>에 따라 달라져요.",
  "brand": "패스트포텐",
  "source": "팩트 출처 · 통합룰북 V4.2"
}

사용:
  python src/cardnews.py                      # examples/cardnews/*.json 전부 렌더 → out/cardnews/
  python src/cardnews.py 파일.json [출력.png]  # 1건 렌더
"""
import glob
import html as html_mod
import json
import os
import subprocess
import sys
import tempfile

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(ROOT_DIR, "templates")
EXAMPLES_DIR = os.path.join(ROOT_DIR, "examples", "cardnews")
OUT_DIR = os.path.join(ROOT_DIR, "out", "cardnews")

CARD_W, CARD_H = 1080, 1350

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        "Chrome/Edge를 찾지 못했습니다. 카드 렌더링에는 둘 중 하나가 필요합니다.")


def _esc(v) -> str:
    """사용자 텍스트를 HTML 이스케이프하되 줄바꿈은 <br>로. (<b> 등 태그는 note에서만 허용)"""
    return html_mod.escape(str(v)).replace("\n", "<br>")


def build_rows_html(rows) -> str:
    parts = []
    for r in rows:
        parts.append(
            f'<div class="row"><div class="row__key">{_esc(r["key"])}</div>'
            f'<div class="row__val">{_esc(r["value"])}</div></div>')
    return "\n".join(parts)


def render_html(content: dict) -> str:
    """콘텐츠 JSON → 완성 HTML 문자열."""
    tpl_name = content.get("template", "card_condition")
    tpl_path = os.path.join(TEMPLATES_DIR, f"{tpl_name}.html")
    with open(tpl_path, encoding="utf-8") as f:
        tpl = f.read()
    fields = {
        "chip": _esc(content.get("chip", "")),
        "title": _esc(content.get("title", "")),
        "subtitle": _esc(content.get("subtitle", "")),
        "rows": build_rows_html(content.get("rows", [])),
        "note": content.get("note", ""),  # note는 <b> 허용 → 이스케이프 안 함(작성자 신뢰 입력)
        "brand": _esc(content.get("brand", "")),
        "source": _esc(content.get("source", "")),
    }
    out = tpl
    for key, val in fields.items():
        out = out.replace("{{" + key + "}}", val)
    if "{{" in out:
        leftover = out[out.index("{{"): out.index("{{") + 30]
        raise ValueError(f"템플릿에 채워지지 않은 자리 있음: {leftover}")
    return out


def render_png(content: dict, out_path: str, browser: str = None) -> str:
    """콘텐츠 JSON → PNG 파일. templates/를 base로 두어 brand.css 상대경로가 먹게 함."""
    browser = browser or find_browser()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    doc = render_html(content)
    # brand.css를 <link>로 부르므로 임시 HTML을 templates/ 안에 둔다(상대경로 해결)
    fd, tmp_html = tempfile.mkstemp(suffix=".html", dir=TEMPLATES_DIR)
    os.close(fd)
    try:
        with open(tmp_html, "w", encoding="utf-8") as f:
            f.write(doc)
        file_url = "file:///" + tmp_html.replace("\\", "/")
        cmd = [
            browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--screenshot={out_path}",
            f"--window-size={CARD_W},{CARD_H}",
            "--default-background-color=00000000",
            file_url,
        ]
        subprocess.run(cmd, capture_output=True, timeout=60)
    finally:
        os.remove(tmp_html)
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
        raise RuntimeError(f"PNG 렌더 실패: {out_path}")
    return out_path


def render_file(json_path: str, out_path: str = None, browser: str = None) -> str:
    with open(json_path, encoding="utf-8") as f:
        content = json.load(f)
    if out_path is None:
        base = os.path.splitext(os.path.basename(json_path))[0]
        out_path = os.path.join(OUT_DIR, base + ".png")
    return render_png(content, out_path, browser)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    browser = find_browser()
    if len(sys.argv) > 1:
        out = sys.argv[2] if len(sys.argv) > 2 else None
        path = render_file(sys.argv[1], out, browser)
        print(f"렌더 완료: {path}")
        return 0
    files = sorted(glob.glob(os.path.join(EXAMPLES_DIR, "*.json")))
    if not files:
        print(f"샘플 JSON이 없습니다: {EXAMPLES_DIR}")
        return 1
    print(f"브라우저: {os.path.basename(browser)}")
    for jp in files:
        path = render_file(jp, None, browser)
        print(f"  - {os.path.basename(jp)} → {path} ({os.path.getsize(path)//1024}KB)")
    print(f"\n{len(files)}장 렌더 완료 → {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
