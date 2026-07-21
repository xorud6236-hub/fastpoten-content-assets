# -*- coding: utf-8 -*-
"""staff_cleanup.py — 담당자(staff) 이름 목록 정제 + 잘못 가려진 참고용 본문 복구 (1회성 정비)

2026-07-21 실행분. BACKLOG 6의 실행 기록이자, 창고를 처음부터 다시 만든 PC에서
같은 정비를 다시 걸 수 있게 남긴다.

멱등은 **아니다**(정직하게 적어둔다). 이미 정비된 창고에 다시 돌리면 '글 건수 지문'이
안 맞아 **아무것도 바꾸지 않고 멈춘다**. 두 번 걸어도 망가지지 않는다는 뜻이지,
두 번째가 무해한 통과라는 뜻은 아니다. 정비 후 재실행 시 나오는 '지문 불일치 — 중단'은
정상 동작이다.

사용:
  python src/staff_cleanup.py           # 드라이런(아무것도 안 바꿈)
  python src/staff_cleanup.py --apply   # 실행

★ 실명을 코드에 적지 않는다 — 전부 staff_id로 지목하고 이름은 창고에서 읽는다.
  지목이 맞는지는 '글 건수 지문'으로 대조하고, 하나라도 어긋나면 즉시 멈춘다.
★ 불변 4: body_raw는 읽지도 쓰지도 않는다. body_clean → 다시 가림 → body_pub_ref만 덮어쓴다.

■ 왜 '고르는 칸'과 '가리는 재료'를 나눠야 했나 (2026-07-21 드라이런이 잡아낸 것)
  같은 사람의 이름이 엑셀에 두 철자로 들어와 있었다(띄어쓰기 차이). 잘못된 철자를
  지우려 했더니, **그 철자가 본문에 실제 사람 이름으로 쓰인 곳이 3건** 있었다
  (예: "평생교육 멘토 OOO입니다"). 지웠으면 참고용 본문에 실명이 드러난다(불변 1 위반).
  → posts.staff_name(=화면 고르는 칸의 재료)은 올바른 이름으로 고치되,
    staff 행(=가리는 재료)은 남긴다. 고르는 칸은 posts에서 뽑으므로 목록은 깨끗해진다.
  두 목적이 원래 다른 것인데 한 테이블을 같이 쓰고 있었다는 게 드러난 것이다.
"""
import io
import json
import os
import sys
import time

ROOT_DIR_LOCAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT_DIR_LOCAL, "src"))
from db import ROOT_DIR, get_connection  # noqa: E402
import masking  # noqa: E402

# 사용자 판정(2026-07-20~21) — staff_id로만 지목
DROP_IDS = [260, 187]                    # 담당자 아님(시험 입력·오타) → staff 제거, posts는 빈 값
MERGE = {214: 213, 186: 257, 249: 250}   # 잘못된 id → 올바른 id (동일인)
KEEP_AS_MASK = {249}                     # posts만 고치고 staff 행은 남긴다(위 ■ 참고)

# 확인용 지문 — 이 숫자가 안 맞으면 다른 창고이거나 이미 정비된 상태다
EXPECT_POSTS = {260: 4, 187: 1, 214: 2, 186: 2, 249: 2}
EXPECT_TARGET_POSTS = {213: 759, 257: 448, 250: 381}

LOG_NAME = "staff_cleanup_20260721.json"   # data/ 아래(깃 제외) — 되돌리기용


def hide(name):
    """이름을 화면에 안 드러내기 — 첫 글자 + O"""
    name = name or ""
    return name[:1] + "O" * max(0, len(name) - 1)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    apply = "--apply" in sys.argv
    conn = get_connection()
    conn.execute("PRAGMA busy_timeout = 60000")

    rows = {r["staff_id"]: r["staff_name"]
            for r in conn.execute("SELECT staff_id, staff_name FROM staff")}
    n_staff = len(rows)
    all_ids = DROP_IDS + list(MERGE)

    print(f"[staff] 현재 {n_staff}건")
    ok = True
    for sid in all_ids + list(MERGE.values()):
        nm = rows.get(sid)
        if nm is None:
            print(f"  !! staff_id={sid} 없음 — 이미 정비됐거나 다른 창고")
            ok = False
            continue
        n = conn.execute("SELECT COUNT(*) c FROM posts WHERE staff_name=?", (nm,)).fetchone()["c"]
        exp = EXPECT_POSTS.get(sid, EXPECT_TARGET_POSTS.get(sid))
        print(f"  id={sid} 이름={hide(nm)}({len(nm)}자) 글 {n}건  "
              f"{'OK' if n == exp else '!! 예상 ' + str(exp)}")
        ok = ok and n == exp
    if not ok:
        print("지문 불일치 — 중단(아무것도 바꾸지 않음)")
        return 1

    drop_names = [rows[i] for i in all_ids if i not in KEEP_AS_MASK]
    old_names = masking.load_staff_names(conn)
    new_names = [n for n in old_names if n not in set(drop_names)]
    print(f"[가림 재료] {len(old_names)} -> {len(new_names)} "
          f"(가림 유지: {[hide(rows[i]) for i in sorted(KEEP_AS_MASK)]})")

    regex_pats = masking.load_regex_patterns(conn)

    # 1) 참고용 본문 다시 만들기 — 본문이 있는 글 전부 재계산하고 '달라진 것만' 쓴다(멱등)
    posts = conn.execute(
        "SELECT post_id, body_clean_path, body_pub_ref_path FROM posts "
        "WHERE body_clean_path IS NOT NULL ORDER BY post_id").fetchall()
    print(f"[본문] 대상 글 {len(posts)}건 — 다시 계산 중...")

    t0 = time.time()
    affected, file_rewrite, missing, samples = [], [], 0, []
    for i, p in enumerate(posts, 1):
        fp = os.path.join(ROOT_DIR, p["body_clean_path"])
        if not os.path.exists(fp):
            missing += 1
            continue
        with io.open(fp, encoding="utf-8") as f:
            clean = f.read()
        old_m, _ = masking.mask_text(clean, regex_pats, old_names)
        new_m, _ = masking.mask_text(clean, regex_pats, new_names)
        if old_m != new_m:
            affected.append(p["post_id"])
            for nm in drop_names:
                if nm in clean and len(samples) < 12:
                    j = clean.find(nm)
                    ctx = clean[max(0, j - 20): j + len(nm) + 20].replace("\n", " ")
                    samples.append((p["post_id"], hide(nm),
                                    ctx.replace(nm, "[그이름:%d자]" % len(nm))))
        rp = p["body_pub_ref_path"]
        if rp:
            rfp = os.path.join(ROOT_DIR, rp)
            cur = None
            if os.path.exists(rfp):
                with io.open(rfp, encoding="utf-8") as f:
                    cur = f.read()
            if cur != new_m:
                file_rewrite.append(p["post_id"])
                if apply:
                    os.makedirs(os.path.dirname(rfp), exist_ok=True)
                    with io.open(rfp, "w", encoding="utf-8") as f:
                        f.write(new_m)
        if i % 500 == 0:
            print(f"  [{i}/{len(posts)}] {time.time() - t0:.0f}s", flush=True)
    print(f"[본문] 계산 {time.time() - t0:.0f}s · 본문 파일 없음 {missing}건")
    print(f"  내용이 실제로 달라지는 글: {len(affected)}건")
    print(f"  참고용 본문을 다시 쓰는 글: {len(file_rewrite)}건")
    for pid, nm, ctx in samples[:6]:
        print(f"   - post {pid} ({nm}): ...{ctx}...")

    # 2) 문단의 정리본(clean_text)도 같은 규칙으로 다시
    paras = conn.execute(
        "SELECT paragraph_id, raw_text, clean_text FROM post_paragraphs").fetchall()
    para_fix = []
    for r in paras:
        if r["raw_text"] is None:
            continue
        new_c, _ = masking.mask_text(r["raw_text"], regex_pats, new_names)
        if new_c != r["clean_text"]:
            para_fix.append((r["paragraph_id"], new_c))
    print(f"[문단] {len(paras)}건 중 다시 쓸 문단 {len(para_fix)}건")

    # 3) staff / posts.staff_name 변경 계획
    plan = []
    for sid in DROP_IDS:
        nm = rows[sid]
        n = conn.execute("SELECT COUNT(*) c FROM posts WHERE staff_name=?", (nm,)).fetchone()["c"]
        plan.append({"staff_id": sid, "from": nm, "to": "", "posts": n, "action": "drop"})
    for bad, good in MERGE.items():
        nm = rows[bad]
        n = conn.execute("SELECT COUNT(*) c FROM posts WHERE staff_name=?", (nm,)).fetchone()["c"]
        plan.append({"staff_id": bad, "from": nm, "to": rows[good], "posts": n,
                     "action": "merge_keep_mask" if bad in KEEP_AS_MASK else "merge"})
    n_removed = len([p for p in plan if p["staff_id"] not in KEEP_AS_MASK])
    print(f"[staff] {n_staff} -> {n_staff - n_removed}건 · posts.staff_name "
          f"{sum(p['posts'] for p in plan)}건 정정")

    if not apply:
        print("\n(드라이런 — 아무것도 바꾸지 않았습니다)")
        conn.close()
        return 0

    for item in plan:
        conn.execute("UPDATE posts SET staff_name=? WHERE staff_name=?",
                     (item["to"], item["from"]))
        if item["staff_id"] not in KEEP_AS_MASK:
            conn.execute("DELETE FROM staff WHERE staff_id=?", (item["staff_id"],))
    for pid, txt in para_fix:
        conn.execute("UPDATE post_paragraphs SET clean_text=? WHERE paragraph_id=?", (txt, pid))
    conn.commit()

    log_path = os.path.join(ROOT_DIR, "data", LOG_NAME)
    with io.open(log_path, "w", encoding="utf-8") as f:
        json.dump({"when": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "staff_before": n_staff, "staff_after": n_staff - n_removed,
                   "staff_name_changes": plan,
                   "pub_ref_rewritten": file_rewrite,
                   "paragraphs_rewritten": [p[0] for p in para_fix],
                   "content_changed_posts": affected}, f, ensure_ascii=False, indent=1)
    print(f"\n실행 완료. 되돌리기용 기록: {log_path}")
    print("다음: python src/count_masks.py (가림 규칙이 바뀌었으므로 건수 다시 세기)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
