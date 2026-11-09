# -*- coding: utf-8 -*-
"""审计库直读层：把 factor_lab 的统计口径搬到 SQL 流式上，绕开 CSV 全量载入。

存在的唯一理由：全池 410 万行 × 4 个持有期，`ic --factor factor.csv` 会把
factor.csv 和 3018 个 bars 一起读进内存（>1.2 GB），本机 2 GB 可用跑不动。
审计库里已经存了 fwd1/5/10/20，所以 IC 只需要 (date, sym, score, fwd_h)，
按日期流式读取即可，峰值内存只跟单日截面（约 2900 行）有关。

口径必须与 factor_lab 完全一致（同一套 spearman / hac_t_overlap / spread_summary），
否则同一份数据会出现两个 t 值。
"""
from __future__ import annotations

import os
import re
import sqlite3
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import factor_lab as fl  # noqa: E402

BATCH = 200_000
IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _validate_horizons(horizons) -> tuple[int, ...]:
    horizons = tuple(horizons)
    if not horizons or any(isinstance(h, bool) or not isinstance(h, int) or h <= 0 for h in horizons):
        raise ValueError('horizons 必须是非空正整数')
    if len(set(horizons)) != len(horizons):
        raise ValueError('horizons 不得重复')
    return horizons


def _connect(db: str):
    if not os.path.exists(db):
        raise SystemExit(f"找不到审计库：{db}")
    return sqlite3.connect(db)


def check_schema(db: str, table: str = "rows") -> list[str]:
    if not IDENT.match(str(table)):
        raise ValueError(f"非法表名：{table!r}")
    con = _connect(db)
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    con.close()
    if not cols:
        raise SystemExit(f"表 {table} 不存在于 {db}")
    need = {"date", "sym", "score"}
    miss = need - set(cols)
    if miss:
        raise SystemExit(f"表 {table} 缺列：{sorted(miss)}")
    return cols


def iter_sections(db: str, horizons, cat: str = "", table: str = "rows",
                  lo_score: float | None = None, hi_score: float | None = None,
                  date_from: str = "", date_to: str = ""):
    """按日期产出 (date, [(sym, score, {h: fwd})...])，内存只占一个截面。"""
    horizons = _validate_horizons(horizons)
    schema_cols = set(check_schema(db, table))
    miss_h = {f"fwd{h}" for h in horizons} - schema_cols
    if miss_h:
        raise ValueError(f"表 {table} 缺前向收益列：{sorted(miss_h)}")
    con = _connect(db)
    cols = "date, sym, score, " + ", ".join(f"fwd{h}" for h in horizons)
    # 原先用 f-string 拼接过滤条件，改为参数化查询避免注入与引号转义异常
    where, params = [], []
    if cat:
        where.append("cat = ?")
        params.append(cat)
    if lo_score is not None:
        where.append("score >= ?")
        params.append(float(lo_score))
    if hi_score is not None:
        where.append("score < ?")
        params.append(float(hi_score))
    if date_from:
        where.append("date >= ?")
        params.append(date_from)
    if date_to:
        where.append("date <= ?")
        params.append(date_to)
    sql = f"SELECT {cols} FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date, sym"

    cur = con.execute(sql, params)
    cur.arraysize = BATCH
    cur_date, bucket = None, []
    while True:
        chunk = cur.fetchmany(BATCH)
        if not chunk:
            break
        for r in chunk:
            d = r[0]
            if d != cur_date:
                if bucket and cur_date is not None:
                    yield cur_date, bucket
                cur_date, bucket = d, []
            if r[2] is None:
                continue
            fwd = {}
            for i, h in enumerate(horizons):
                v = r[3 + i]
                if v is not None:
                    fwd[h] = v
            if fwd:
                bucket.append((r[1], float(r[2]), fwd))
    if bucket and cur_date is not None:
        yield cur_date, bucket
    con.close()


def series(db: str, horizons, min_n: int = 20, cat: str = "", q: int = 10,
           **kw):
    """返回 (ic_series{遇h:[(date,ic,n)]}, spread_daily{h:[{date,n,excess,raw}]},
    diag{h:{...}}, n_obs, date_range)。

    IC 与分档都在同一次扫描里算，时间口径一致（同一个 fwd 列，同一个截面）。
    """
    horizons = _validate_horizons(horizons)
    ics = {h: [] for h in horizons}
    spd = {h: [] for h in horizons}
    diag = {h: {"days": 0, "too_few": 0, "tie_factor": 0, "tie_ret": 0} for h in horizons}
    n_obs, first, last = 0, None, None

    for d, sec in iter_sections(db, horizons, cat=cat, **kw):
        n_obs += len(sec)
        first = first or d
        last = d
        for h in horizons:
            pairs = [(s, f[h]) for _sym, s, f in sec if h in f]
            if len(pairs) < min_n:
                diag[h]["too_few"] += 1
                continue
            diag[h]["days"] += 1
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            ic = fl.spearman(xs, ys)
            if ic is None:
                rk = fl.rank(xs)
                if max(rk) == min(rk):
                    diag[h]["tie_factor"] += 1
                else:
                    diag[h]["tie_ret"] += 1
                continue
            ics[h].append((d, ic, len(pairs)))

            if q and len(pairs) >= max(min_n, q * 3):
                base = statistics.mean(ys)
                n = len(pairs)
                ranks = fl.rank(xs)
                buckets: list[list[float]] = [[] for _ in range(q)]
                for (_s, r), rk in zip(pairs, ranks):
                    buckets[min(q - 1, max(0, int((rk - 1) * q / n)))].append(r)
                spd[h].append({"date": d, "n": n,
                               "excess": [(statistics.mean(b) - base) if b else None
                                          for b in buckets],
                               "raw": [statistics.mean(b) if b else None for b in buckets]})
    return ics, spd, diag, n_obs, (first, last)

def _cost_line(h, ss, q, costs):
    """按价差符号定方向再扣成本（v1.3 口径，含持有期列）。"""
    if not ss:
        return f"| {h}日 | 样本不足 | — | — | — | — | — | — |"
    means = ss["q_mean"]
    lo, hi = means[0], means[-1]
    if hi >= lo:
        direction, leg = "多高档／空低档", hi - lo
        leg_ret = means[-1]
    else:
        direction, leg = "多低档／空高档", lo - hi
        leg_ret = means[0]
    rows = []
    for c in costs:
        net_long = leg_ret - c / 100.0
        net_ls = leg - 2 * c / 100.0
        verdict = "可实施（做多即可）" if net_long > 0 else ("仅多空可实施" if net_ls > 0 else "不可实施（L3）")
        rows.append(f"| {h}日 | {direction} | {leg*100:+.2f}% | {leg_ret*100:+.2f}% | "
                    f"{net_long*100:+.2f}% | {net_ls*100:+.2f}% | {c:.2f}% | {verdict} |")
    return "\n".join(rows)


def report(ics, spd, diag, n_obs, date_range, horizons, trials: int,
           q: int = 10, costs=(0.10, 0.20, 0.30), slices: int = 4,
           title: str = "审计库直读 IC 报告") -> str:
    th = fl.t_threshold(trials)
    md = [f"# {title}\n",
          f"- 观测：{n_obs:,} 条（date×sym，只含有前向收益的行）",
          f"- 日期范围：{date_range[0]} → {date_range[1]}",
          f"- 分档：{q} 档，自低到高（Q1 = 因子最低）；超额相对当日全池均值",
          f"- 多重检验：{trials} 次 → Bonferroni |t| 门槛 {th:.2f}",
          f"- 成本口径：先由价差符号决定做多哪一档，再扣；A 股零售只能走多头腿\n"]

    md.append("## 1. IC / IR\n")
    md.append("| 持有期 | 交易日 | 平均标的 | IC均值 | IC标准差 | IR | t朴素 | t_HAC | IC>0占比 | 过门槛 |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for h in horizons:
        st = fl.ic_summary(ics[h], h)
        md.append(st.line(th) if st else f"| {h}日 | — | — | 样本不足 | | | | | | ❌ |")

    drops = [(h, dg) for h, dg in diag.items() if dg["tie_factor"] or dg["tie_ret"]]
    if drops:
        md.append("\n> ⚠️ 无效截面已剔除（相关系数无定义）：" +
                  "；".join(f"{h}日 因子全同分 {dg['tie_factor']} 天 / 收益全同 {dg['tie_ret']} 天"
                           for h, dg in drops))

    md.append("\n## 2. IC 衰减\n")
    md.append("| 持有期 | IC均值 | 相对 1 日 |")
    md.append("|---|---:|---:|")
    base = None
    for h in horizons:
        st = fl.ic_summary(ics[h], h)
        if not st:
            continue
        base = st.mean if base is None else base
        md.append(f"| {h}日 | {st.mean:+.4f} | {(st.mean/base*100 if base else 0):.0f}% |")

    md.append(f"\n## 3. 分层（{q} 档，自低到高，超额 %）\n")
    md.append("| 持有期 | " + " | ".join(f"Q{i+1}" for i in range(q)) +
              " | 高−低 | 低−高 | 高档胜率 | t朴素 | t_HAC | 单调档数 | 空档剔除日 |")
    md.append("|---|" + "---:|" * (q + 7))
    for h in horizons:
        ss = fl.spread_summary(spd[h], hac_lags=h)
        if not ss:
            md.append(f"| {h}日 | 样本不足 |" + " |" * (q + 7))
            continue
        cells = " | ".join(f"{x*100:+.2f}" for x in ss["q_mean"])
        t0 = "不可估" if ss["t"] is None else f"{ss['t']:+.2f}"
        thac = "不可估" if ss["t_hac"] is None else f"{ss['t_hac']:+.2f}"
        md.append(f"| {h}日 | {cells} | {ss['high_minus_low']*100:+.2f} | "
                  f"{ss['low_minus_high']*100:+.2f} | {ss['top_win']*100:.0f}% | "
                  f"{t0} | {thac} | {ss['monotonic']}/{q-1} | {ss['dropped_empty']} |")

    md.append("\n## 4. 成本（按价差方向）\n")
    md.append("| 持有期 | 方向 | 毛价差 | 多头腿收益 | 多头腿净 | 多空净 | 单边成本 | 判定 |")
    md.append("|---|---|---:|---:|---:|---:|---:|---|")
    for h in horizons:
        ss = fl.spread_summary(spd[h], hac_lags=h)
        md.append(_cost_line(h, ss, q, costs))

    md.append("\n## 5. 分段稳定性\n")
    for h in horizons:
        segs = fl.rolling_ic(ics[h], slices)
        if not segs:
            continue
        md.append(f"\n**{h} 日**：" + " / ".join(f"{s['mean']:+.4f}" for s in segs))
        pos = sum(1 for s in segs if s["mean"] > 0)
        md.append(f"（{pos}/{len(segs)} 段为正；{segs[0]['from']} → {segs[-1]['to']}）")

    md.append("\n## 6. 判读顺序\n")
    md.append("1. t_HAC 过门槛吗（不是 t朴素）；2. 衰减是单调还是反转；"
              "3. 分段是否一致；4. 分档价差扣成本后还剩多少；5. 分年（另跑 yearly）。")
    return "\n".join(md)


def walkforward(db: str, band=(0, 200), horizons=(5,), min_n: int = 20,
                first_test_year: int = 2022, trials: int = 1, table: str = "rows"):
    """走样本外：训练段决定「做多还是做空这个分数带」，测试段验证。

    与 yearly 的区别：yearly 是逐年描述，允许事后挑年份；
    这里每一年都只能用**此前**的数据定方向，属于真外推。
    """
    years = []
    con = _connect(db)
    for (d,) in con.execute(f"SELECT DISTINCT date FROM {table} ORDER BY date"):
        y = int(d[:4])
        if not years or years[-1] != y:
            years.append(y)
    con.close()

    out = []
    for ty in years:
        if ty < first_test_year:
            continue
        ics, spd, _dg, _n, _r = series(
            db, horizons, min_n=min_n, q=0,
            lo_score=band[0], hi_score=band[1],
            date_to=f"{ty-1}-12-31")
        if not ics[horizons[0]]:
            continue
        train_ic = fl.ic_summary(ics[horizons[0]], horizons[0])
        # 训练段的收益方向：IC>0 → 高分好；IC<0 → 低分好
        bet = 1.0 if train_ic.mean >= 0 else -1.0

        # 测试年：同一分数带的日均超额（相对当年全池均值）
        ics_t, spd_t, _d, _n2, _r2 = series(
            db, horizons, min_n=min_n, q=0,
            lo_score=band[0], hi_score=band[1],
            date_from=f"{ty}-01-01", date_to=f"{ty}-12-31")
        if not ics_t[horizons[0]]:
            continue
        test_ic = fl.ic_summary(ics_t[horizons[0]], horizons[0])
        if not test_ic:
            continue
        th = fl.t_threshold(trials)
        need = max(th, test_ic.t_crit)
        out.append({"year": ty,
                    "train_ic": train_ic.mean, "train_t": train_ic.t_hac,
                    "train_days": train_ic.n_days,
                    "bet": bet,
                    "test_ic": test_ic.mean, "test_t": test_ic.t_hac,
                    "test_days": test_ic.n_days,
                    "pnl": bet * test_ic.mean,
                    "threshold": need,
                    "pass": (bet * test_ic.t_hac) >= need})
    return out


def render_walkforward(wf_rows: list[dict], horizon: int, trials: int, band=(0, 200)) -> str:
    th = fl.t_threshold(trials)
    md = [f"# 样本外滚动外推（Walk-Forward） · 持有 {horizon} 日 · 分数带 [{band[0]}, {band[1]})",
          f"- 多重检验：{trials} 次 → 基础 Bonferroni 门槛 |t| ≥ {th:.2f}（测试年与有效自由度临界值取大者）\n",
          "| 测试年 | 训练天数 | 训练IC | 训练t_HAC | 押注方向 | 测试天数 | 测试IC | 测试t_HAC | 门槛 | 同向收益 | 判定 |",
          "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---|"]
    for r in wf_rows:
        b_str = "做多高分" if r["bet"] > 0 else "做空高分"
        mark = "✅ 样本外通过" if r["pass"] else ("⚠️ 同向未过线" if r["pnl"] > 0 else "❌ 样本外反号")
        md.append(f"| {r['year']} | {r['train_days']} | {r['train_ic']:+.4f} | {r['train_t']:+.2f} | "
                  f"{b_str} | {r['test_days']} | {r['test_ic']:+.4f} | {r['test_t']:+.2f} | "
                  f"{r['threshold']:.2f} | {r['pnl']:+.4f} | {mark} |")
    return "\n".join(md)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="db_lab —— 审计库流式截面检验与 Walk-Forward 外推")
    ap.add_argument("--db", required=True, help="SQLite 审计库路径")
    ap.add_argument("--table", default="rows", help="表名（默认 rows）")
    ap.add_argument("--horizons", default="1,5,10,20", help="逗号分隔的正整数持有期")
    ap.add_argument("--trials", type=int, default=1, help="多重检验尝试次数")
    ap.add_argument("--cat", default="", help="按类别过滤")
    ap.add_argument("--lo-score", type=float, default=None)
    ap.add_argument("--hi-score", type=float, default=None)
    ap.add_argument("--date-from", default="")
    ap.add_argument("--date-to", default="")
    ap.add_argument("--q", type=int, default=10, help="分档数（默认 10）")
    ap.add_argument("--slices", type=int, default=4, help="滚动分段数")
    ap.add_argument("--walkforward", action="store_true", help="运行逐年样本外 Walk-Forward")
    ap.add_argument("--first-test-year", type=int, default=2022)
    ap.add_argument("--out", default="", help="输出报告文件路径")
    a = ap.parse_args(argv)
    if a.trials <= 0:
        raise SystemExit("--trials 必须是正整数")
    hs = _validate_horizons([int(x.strip()) for x in a.horizons.split(",") if x.strip()])
    if a.walkforward:
        band = (a.lo_score if a.lo_score is not None else -1e9,
                a.hi_score if a.hi_score is not None else 1e9)
        wf = walkforward(a.db, band=band, horizons=(hs[0],), first_test_year=a.first_test_year,
                         trials=a.trials, table=a.table)
        md = render_walkforward(wf, hs[0], a.trials, band=band)
    else:
        ics, spd, diag, n_obs, dr = series(
            a.db, hs, q=a.q, cat=a.cat, table=a.table,
            lo_score=a.lo_score, hi_score=a.hi_score,
            date_from=a.date_from, date_to=a.date_to)
        md = report(ics, spd, diag, n_obs, dr, hs, trials=a.trials, q=a.q, slices=a.slices)
    print(md)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
