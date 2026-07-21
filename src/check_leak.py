# -*- coding: utf-8 -*-
"""check_leak.py — 깃에 올라간 것에 직원 실명이 섞였는지 감사한다(불변 1).

왜 있나: 2026-07-15·16 이틀 연속으로 **테스트·작업노트에 실제 직원 이름을 그대로
베껴 넣는** 일이 있었다(가짜 이름을 지어내는 대신 실제 명단을 참고한 탓). 저장소가
공개였던 동안 7명의 실명이 노출됐다. 사람의 주의력에 기대면 반복되므로 명령으로 만든다.

무엇을 하나: staff 테이블의 실제 이름과 (1)깃에 커밋된 현재 파일 (2)지나간 이력 전체를
대조한다. 창고를 읽을 뿐 아무것도 고치지 않는다.

사용:
  python src/check_leak.py            # 커밋된 것 + 이력 감사
  python src/check_leak.py --working  # 아직 커밋 안 한 작업본까지 함께(커밋 전 확인용)

푸시 전에 돌려볼 것. 이력에서 지우려면 깃 이력 재작성이 필요하다(별건).
"""
import os
import re
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import masking  # noqa: E402
from db import ROOT_DIR, get_connection  # noqa: E402

# 사람 이름이 아닌 staff 값(오탐 유발) — BACKLOG 6에서 staff를 정제하면 지울 수 있다
NOT_A_PERSON = {"테스트", "에듀라이크"}


def find_git():
    """git 실행 파일 찾기. 이 PC엔 GitHub Desktop 번들만 있을 수 있다."""
    from shutil import which
    hit = which("git")
    if hit:
        return hit
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "GitHubDesktop")
    cands = []
    if os.path.isdir(base):
        for root, _dirs, files in os.walk(base):
            if "git.exe" in files and (os.sep + "cmd") in root:
                cands.append(os.path.join(root, "git.exe"))
    return sorted(cands)[-1] if cands else None  # 최신 app-x.y.z


def real_staff_names(conn):
    """마스킹이 실제로 쓰는 이름(2자 미만은 마스킹이 거른다) — 일반 단어 제외."""
    names = [n.strip() for n in masking.load_staff_names(conn) if len(n.strip()) >= 2]
    return [n for n in names if n not in NOT_A_PERSON]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    check_working = "--working" in sys.argv

    git = find_git()
    if not git:
        print("git을 찾지 못했습니다. GitHub Desktop이 설치돼 있는지 확인하세요.")
        return 2

    def run(args):
        return subprocess.run([git] + args, cwd=ROOT_DIR, capture_output=True,
                              encoding="utf-8", errors="replace").stdout

    conn = get_connection()
    conn.execute("PRAGMA busy_timeout = 60000")
    names = real_staff_names(conn)
    conn.close()
    print(f"대조 기준: 실제 직원 이름 {len(names)}건 (일반 단어·업체명 제외)\n")

    files = [f for f in run(["ls-files"]).splitlines() if f.strip()]

    # 어느 이름이 어디 있는지 '이름 자체'는 출력하지 않는다 — 이 출력이 또 새는 걸 막는다.
    print("① 깃에 올라간 현재 파일")
    cur = defaultdict(list)
    for f in files:
        txt = run(["show", f"HEAD:{f}"])
        for n in names:
            if n in txt:
                cur[n].append(f)
    if cur:
        for _n, fs in sorted(cur.items(), key=lambda x: x[1]):
            print(f"   ★ 실명 1건 — {', '.join(fs)}")
    else:
        print("   없음")

    if check_working:
        print("\n①-2 아직 커밋 안 한 작업본")
        work = defaultdict(list)
        for f in files:
            p = os.path.join(ROOT_DIR, f)
            if not os.path.exists(p):
                continue
            try:
                with open(p, encoding="utf-8") as fh:
                    txt = fh.read()
            except (UnicodeDecodeError, OSError):
                continue
            for n in names:
                if n in txt:
                    work[n].append(f)
        if work:
            for _n, fs in sorted(work.items(), key=lambda x: x[1]):
                print(f"   ★ 실명 1건 — {', '.join(fs)}")
        else:
            print("   없음")

    print("\n② 지나간 이력(모든 커밋)")
    hist = defaultdict(set)
    for n in names:
        for line in run(["log", "--all", "--oneline", "-S", n]).splitlines():
            if line.strip():
                hist[n].add(line.split()[0])
    if hist:
        print(f"   ★ {len(hist)}명의 실명이 커밋 이력에 남아 있음")
        print("   → 지우려면 깃 이력 재작성이 필요(별건). 저장소가 비공개면 노출은 멈춘다.")
    else:
        print("   없음")

    # ③ AI 열쇠 — 실명과 같은 종류의 사고(비밀이 저장소에 실림)를 막는다.
    #    진짜 열쇠는 ~/.fastpoten/api_key.txt에만 있어야 하고, 깃에 든 파일엔 적히면 안 된다.
    #    열쇠 값을 여기로 가져오지 않는다(비밀을 읽는 곳을 늘리지 않는다) — 길이로만 판정하고,
    #    테스트용 가짜는 짧아서 걸러진다(진짜는 100자 안팎).
    print("\n③ AI 열쇠가 파일에 적혔나")
    key_hits = []
    for line in run(["grep", "-n", "-I", "sk-ant-", "--", "."]).splitlines():
        # 형식: 경로:줄번호:내용 — 내용에서 열쇠 모양만 뽑아 길이로 판정(값은 절대 안 찍는다)
        m = re.search(r"sk-ant-[A-Za-z0-9_\-]+", line)
        if not m:
            continue
        found = m.group(0)
        where = ":".join(line.split(":", 2)[:2])
        if len(found) >= 60:
            key_hits.append((where, f"열쇠로 보이는 값 {len(found)}자"))
    if key_hits:
        for where, what in key_hits:
            print(f"   ★ {where} — {what}")
        print("   → 지금 폐기하고 새로 발급할 것. ~/.fastpoten/api_key.txt에만 두어야 한다.")
    else:
        print("   없음 (짧은 테스트용 가짜 값은 검사 대상이 아님)")

    # ④ 열쇠가 환경변수에 남아 있나 — 남아 있으면 클로드 코드가 발견해 제 대화에 쓴다.
    #    2026-07-21에 실제로 그렇게 요금이 나갔다. 원고 생성 전용 열쇠를 지키는 감시.
    print("\n④ AI 열쇠가 환경변수에 남아 있나  (※ 지금 이 창 기준)")
    env_key = int(bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()))
    if env_key:
        print("   ★ ANTHROPIC_API_KEY가 설정돼 있음 — 클로드 코드가 이 열쇠로 요금을 낼 수 있다")
        print('   → 지우기: powershell [Environment]::SetEnvironmentVariable('
              '"ANTHROPIC_API_KEY",$null,"User")  후 창을 새로 열 것')
    else:
        print("   없음 (원고 생성은 ~/.fastpoten/api_key.txt에서 읽는다)")
    print("   ※ 이 검사는 이 창이 켜질 때의 설정만 봅니다 — 설정을 바꿨다면 창을 새로 열고 다시 확인하세요.")

    print("\n" + "=" * 58)
    print(f"판정: 커밋된 현재 파일 {len(cur)}명 · 이력 {len(hist)}명 · "
          f"열쇠 {len(key_hits)}건 · 환경변수 잔류 {env_key}건")
    print("=" * 58)
    return 1 if (cur or key_hits) else 0


if __name__ == "__main__":
    raise SystemExit(main())
