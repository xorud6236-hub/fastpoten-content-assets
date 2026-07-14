# -*- coding: utf-8 -*-
"""trends.py — 시기별 주제 트렌드 + 주제별 조회수(정규화) 집계. (B)

두 데이터 범위를 나눠 쓴다(진짜 불변 3·본문파일 원칙과 무관, 단지 가진 데이터가 다름):
  - 시기 트렌드      : posts 전체(1만여 건)의 keyword + publish_date만으로. 추출 불필요.
  - 주제별 조회수    : 추출완료 + 조회수 있는 글만(reference_signals). 조회수는 참고 신호.

핵심 설계 — '총량 착시' 제거:
  최근일수록 전체 발행량 자체가 늘어(2026 집중) 원시 건수는 거의 다 증가한다. 그래서 트렌드는
  '그 분기 전체 글 중 이 주제의 비중(%)'으로 본다 → 전체가 늘어도 '상대적으로 뜨는/식는' 주제만 잡힘.

주제 묶기는 keyword_normalize.normalize()(단일 출처)만 사용 — 여기서 주제를 새로 만들지 않는다.

자체 검증:  python src/trends.py    (요약 출력, DB 필요)
"""
import datetime
import re
from collections import Counter, defaultdict

from keyword_normalize import normalize

AUTO_VIEW_MARK = "자동추출:조회수"  # extract_cafe가 조회수 행에 남기는 표식(참고 신호)
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _ymd(publish_date):
    """publish_date(YYYY-MM-DD…) → (year, month, day) 또는 None. 오타연도(2323 등) 걸러냄.
    ★ 연도 상한은 넉넉히(2035) — 특정 연도 하드코딩(2026)은 다음 해 글을 조용히 버린다.
      명백한 오타(2323)만 거르고 실제 미래 글은 통과시킨다. ingest_excel.normalize_date와 동기화."""
    m = _DATE_RE.match(str(publish_date) or "")
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (2015 <= y <= 2035 and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return y, mo, d


def _dom_bucket(day):
    """일(day) → 월초/중순/말. 1-10 초 · 11-20 중순 · 21+ 말."""
    if day <= 10:
        return "early"
    if day <= 20:
        return "mid"
    return "late"


def load_topic_dates(conn):
    """posts 전체에서 (주제, y, m, d, 분기키, 월내버킷) 목록. keyword 없거나 일반어면 제외."""
    rows = conn.execute(
        "SELECT keyword, publish_date FROM posts WHERE keyword IS NOT NULL").fetchall()
    recs = []
    for r in rows:
        topic = normalize(r["keyword"])
        if not topic:
            continue
        ymd = _ymd(r["publish_date"])
        if not ymd:
            continue
        y, mo, d = ymd
        recs.append(dict(topic=topic, y=y, m=mo, d=d,
                         q=f"{y}-Q{(mo - 1) // 3 + 1}", dom=_dom_bucket(d)))
    return recs


def _slope(pairs):
    """최소제곱 기울기(단위: y값/x1). 점<2면 0."""
    n = len(pairs)
    if n < 2:
        return 0.0
    sx = sum(p[0] for p in pairs)
    sy = sum(p[1] for p in pairs)
    sxx = sum(p[0] ** 2 for p in pairs)
    sxy = sum(p[0] * p[1] for p in pairs)
    den = n * sxx - sx * sx
    return (n * sxy - sx * sy) / den if den else 0.0


def quarter_trends(recs, min_topic=40, min_quarter=150, top=10):
    """분기별 '비중' 기울기로 뜨는/식는 주제. 물량 충분한 분기(min_quarter+)만, 40건+ 주제만.
    반환: dict(quarters=[...], rising=[item], falling=[item]).
      item = dict(topic, total, first_pct, last_pct, slope)  (pct=분기 내 비중 %)."""
    topic_total = Counter(r["topic"] for r in recs)
    big = {t for t, n in topic_total.items() if n >= min_topic}
    q_total = Counter(r["q"] for r in recs)
    quarters = [q for q in sorted(q_total) if q_total[q] >= min_quarter]
    if len(quarters) < 2 or not big:
        return dict(quarters=quarters, rising=[], falling=[])
    qidx = {q: i for i, q in enumerate(quarters)}
    tq = defaultdict(Counter)  # topic -> quarter -> count
    for r in recs:
        if r["topic"] in big and r["q"] in quarters:
            tq[r["topic"]][r["q"]] += 1
    items = []
    for t in big:
        pts = [(qidx[q], tq[t].get(q, 0) / q_total[q] * 100) for q in quarters]
        items.append(dict(topic=t, total=topic_total[t],
                          first_pct=pts[0][1], last_pct=pts[-1][1],
                          slope=_slope(pts)))
    rising = sorted(items, key=lambda x: -x["slope"])[:top]
    falling = sorted(items, key=lambda x: x["slope"])[:top]
    return dict(quarters=quarters, rising=rising, falling=falling)


def seasonality(recs, min_topic=40, top=10):
    """달력 월(1-12)에 쏠린 주제. 반환: [dict(topic, total, peak_month, peak_pct)] 쏠림 큰 순."""
    topic_total = Counter(r["topic"] for r in recs)
    big = {t for t, n in topic_total.items() if n >= min_topic}
    tm = defaultdict(Counter)
    for r in recs:
        if r["topic"] in big:
            tm[r["topic"]][r["m"]] += 1
    out = []
    for t in big:
        tot = sum(tm[t].values())
        pk_m, pk_n = tm[t].most_common(1)[0]
        out.append(dict(topic=t, total=tot, peak_month=pk_m, peak_pct=pk_n / tot * 100))
    return sorted(out, key=lambda x: -x["peak_pct"])[:top]


def intramonth(recs, min_topic=40, top=6):
    """월초/중순/말 분포. 반환: dict(baseline=(초,중,말)%, early=[...], late=[...]).
      item = dict(topic, total, early_pct, mid_pct, late_pct)."""
    topic_total = Counter(r["topic"] for r in recs)
    big = {t for t, n in topic_total.items() if n >= min_topic}
    td = defaultdict(Counter)
    for r in recs:
        if r["topic"] in big:
            td[r["topic"]][r["dom"]] += 1
    items = []
    for t in big:
        tot = sum(td[t].values())
        items.append(dict(topic=t, total=tot,
                          early_pct=td[t].get("early", 0) / tot * 100,
                          mid_pct=td[t].get("mid", 0) / tot * 100,
                          late_pct=td[t].get("late", 0) / tot * 100))
    alld = Counter(r["dom"] for r in recs)
    allt = sum(alld.values()) or 1
    baseline = (alld["early"] / allt * 100, alld["mid"] / allt * 100, alld["late"] / allt * 100)
    return dict(baseline=baseline,
                early=sorted(items, key=lambda x: -x["early_pct"])[:top],
                late=sorted(items, key=lambda x: -x["late_pct"])[:top])


def topic_performance(conn, today, min_extracted=2, top=25):
    """주제별(정규화) 조회수 — 추출+조회수 있는 글을 주제로 묶어 평균조회/합계/하루당.
    발행량(published)은 posts 전체에서 같은 주제의 글 수(성과 대비 물량 비교용).
    반환: [dict(topic, published, extracted, avg_views, sum_views, avg_vpd)] 평균조회 높은 순.
      추출글이 min_extracted 미만인 주제는 평균의 신뢰가 낮아 제외."""
    # 1) 발행량: posts 전체를 주제로 집계
    published = Counter()
    for r in conn.execute("SELECT keyword FROM posts WHERE keyword IS NOT NULL"):
        t = normalize(r["keyword"])
        if t:
            published[t] += 1
    # 2) 추출+조회수: 주제로 묶어 평균/합계/하루당
    rows = conn.execute(
        "SELECT p.keyword, p.publish_date, rs.view_count AS views "
        "FROM posts p JOIN reference_signals rs "
        "  ON rs.post_id=p.post_id AND rs.collected_from_sheet=? "
        "WHERE p.body_raw_path IS NOT NULL AND rs.view_count IS NOT NULL",
        (AUTO_VIEW_MARK,)).fetchall()
    g = defaultdict(list)
    for r in rows:
        t = normalize(r["keyword"])
        if not t:
            continue
        ymd = _ymd(r["publish_date"])
        dg = None
        if ymd:
            dg = (today - datetime.date(*ymd)).days
        vpd = (r["views"] / max(dg, 1)) if dg is not None else None
        g[t].append((r["views"], vpd))
    out = []
    for t, vals in g.items():
        if len(vals) < min_extracted:
            continue
        views = [v for v, _ in vals]
        vpds = [vp for _, vp in vals if vp is not None]
        out.append(dict(topic=t, published=published.get(t, len(vals)),
                        extracted=len(vals),
                        avg_views=sum(views) / len(views), sum_views=sum(views),
                        avg_vpd=(sum(vpds) / len(vpds)) if vpds else None))
    return sorted(out, key=lambda x: -x["avg_views"])[:top]


def topic_counts(conn):
    """posts 전체를 주제로 묶은 (topic -> 글수) Counter. 주제 검수·데이터 화면 공용."""
    cnt = Counter()
    for r in conn.execute("SELECT keyword FROM posts WHERE keyword IS NOT NULL"):
        t = normalize(r["keyword"])
        if t:
            cnt[t] += 1
    return cnt


def topic_sample_skew(conn):
    """주제별 조회수 표본(추출+조회수 있는 글)이 어느 출처 시트에 쏠렸는지.
    반환: (dominant_sheet, dominant_pct, total). 표본 없으면 (None, 0.0, 0).
    ★ 이 표본은 대부분 공준모라 '주제 간 공정 비교'가 아직 아님을 화면이 경고하게 하는 근거."""
    rows = conn.execute(
        "SELECT p.source_sheet AS s, COUNT(*) AS n FROM posts p JOIN reference_signals rs "
        "  ON rs.post_id=p.post_id AND rs.collected_from_sheet=? "
        "WHERE p.body_raw_path IS NOT NULL AND rs.view_count IS NOT NULL "
        "GROUP BY p.source_sheet ORDER BY n DESC",
        (AUTO_VIEW_MARK,)).fetchall()
    total = sum(r["n"] for r in rows)
    if not total:
        return (None, 0.0, 0)
    return (rows[0]["s"], rows[0]["n"] / total * 100, total)


def _summary():
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from db import get_connection, DEFAULT_DB_PATH
    conn = get_connection(DEFAULT_DB_PATH)
    recs = load_topic_dates(conn)
    print(f"정규화 대상 행 {len(recs)}")
    qt = quarter_trends(recs)
    print(f"\n[분기 {qt['quarters'][:1]}..{qt['quarters'][-1:]}] 뜨는 주제:")
    for it in qt["rising"][:5]:
        print(f"  ▲ {it['topic'][:16]:16} {it['first_pct']:.1f}%→{it['last_pct']:.1f}% "
              f"(총{it['total']}, 기울기{it['slope']:+.2f})")
    for it in qt["falling"][:3]:
        print(f"  ▼ {it['topic'][:16]:16} {it['first_pct']:.1f}%→{it['last_pct']:.1f}%")
    print("\n[계절성] 특정 월 쏠림:")
    for it in seasonality(recs)[:5]:
        print(f"  {it['topic'][:16]:16} 최다 {it['peak_month']}월 ({it['peak_pct']:.0f}%)")
    im = intramonth(recs)
    print(f"\n[월내] 기준선 초{im['baseline'][0]:.0f}/중{im['baseline'][1]:.0f}/말{im['baseline'][2]:.0f}%")
    for it in im["late"][:3]:
        print(f"  말쏠림 {it['topic'][:14]:14} 말{it['late_pct']:.0f}%")
    print("\n[주제별 조회수] 평균조회 상위:")
    for it in topic_performance(conn, datetime.date.today())[:6]:
        vpd = f"{it['avg_vpd']:.1f}" if it["avg_vpd"] is not None else "-"
        print(f"  {it['topic'][:16]:16} 발행{it['published']:>4} 추출{it['extracted']:>2} "
              f"평균조회{it['avg_views']:>7.0f} 하루당{vpd}")
    conn.close()


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _summary()
