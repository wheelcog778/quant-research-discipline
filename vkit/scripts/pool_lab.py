# -*- coding: utf-8 -*-
"""池子层分解 + 选股规则消融 —— 回答「亏钱是选股不行，还是池子选错」。

为什么需要这个：
  分组 IC（按 cat/regime 分别算）能告诉你在哪一组里排序有效，但回答不了更前面的问题：
  **这套策略选的池子本身，未来是不是就在跌。** 组内 alpha 为正、组合净值为负，完全可能同时成立——
  因为 alpha 的分母是「同池等权」，而池子在跌时，选得好的 20 只也只是跌得少一点。
  把它误读成「选股能力为负」，就会去优化选股，而问题在门槛。

两类输出（一次扫描全库，峰值内存只占单日截面）：

  1. 池子表 `pool_table`：每个池子（全池 / 各类 / 「被剔除的」）各自等权持有的毛收益与净收益。
     过滤器成本 = 被剔除池的收益 − 被保留池的收益。这是「门槛立对了没有」的唯一直接证据。
  2. 选股消融 `selection_ablation`：同一批分数、同一批收益，只换「选谁」这一层
     （跨类取绝对分 / 类内取 top-N / 类内百分位），比较 cohort alpha 与净累计。
     若所有规则的净累计都负而 alpha 都正 → 问题不在选股规则，在池子与换手成本。

口径（与 factor-verdict 其余部分一致）：
  fwdH 视为「T 日收盘出信号 → T+1 收盘买入 → 持有 H 日」的收益，不再 shift。
  成本按 round trip 2c 从每个 cohort 扣；净值按「资金分 H 份、每份走一个相位」链式，等价每日 1/H 换仓。
  这是简化模型：无止损、无仓位上限、无容量约束，结果只能用来比大小，不能当上线证据。

⚠️ 并列（这个坑会让结果假到离谱）：
  同分是常态——封顶组、托底组、离散打分器的整数档都大量同分。破平**只能用确定性键**
  （默认 sym 代码序），**绝不能拿未来收益当第二排序键**：那等于在同分组里挑出「后来涨得最多的」，
  alpha 会被抬到 +5%/期、净值抬到几百万 %（实测踩过）。
  按代码序破平本身也是任意的，所以关键结论要用 `--tie hash` 再跑一遍（关 6 并列敏感性）。

用法：
  python scripts/pool_lab.py --db audit.db --fwd 5 --tops 5,20 --cost 0.002 --drop eliminated
  python scripts/pool_lab.py --db audit.db --fwd 5 --drop eliminated,risk --tie hash

表结构要求：date, sym, score, <cat 列>, fwd<h>。cat 取值通过 --drop 指定要剔的组名。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import factor_lab as fl  # noqa: E402

BATCH = 200_000
TD = 242
IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _tie_key(sym: str, mode: str):
    """确定性破平键。code=代码序；hash=稳定哈希（用于复核并列敏感性）。"""
    if mode == "hash":
        return hashlib.md5(str(sym).encode()).hexdigest()
    return str(sym)


def _validate_horizons(horizons):
    horizons = tuple(horizons)
    if not horizons or any(isinstance(h, bool) or not isinstance(h, int) or h <= 0 for h in horizons):
        raise ValueError("horizons 必须是非空正整数")
    if len(set(horizons)) != len(horizons):
        raise ValueError("horizons 不得重复")
    return horizons


def iter_days(db, horizons, cat_col, table, tie_mode):
    """按日期产出 (date, [(score, tiekey, cat, {h: fwd})])，内存只占一个截面。"""
    horizons = _validate_horizons(horizons)
    if not IDENT.match(str(table)) or not IDENT.match(str(cat_col)):
        raise ValueError(f"非法表名或列名: table={table!r}, cat_col={cat_col!r}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    db_cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    need = {"date", "sym", "score", cat_col} | {f"fwd{h}" for h in horizons}
    if not db_cols or not (need <= db_cols):
        con.close()
        raise ValueError(f"表 {table} 缺失必需列: {sorted(need - db_cols)}")
    cols = "date, sym, score, " + cat_col + ", " + ", ".join(f"fwd{h}" for h in horizons)
    cur = con.execute(f"SELECT {cols} FROM {table} ORDER BY date")
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
            fwd = {h: float(r[4 + i]) for i, h in enumerate(horizons) if r[4 + i] is not None}
            if fwd:
                bucket.append((float(r[2]), _tie_key(r[1], tie_mode), r[3], fwd))
    if bucket and cur_date is not None:
        yield cur_date, bucket
    con.close()


def _mean(xs):
    return statistics.mean(xs) if xs else None


def run(db, horizon, tops, cost, cat_col, drop, table, min_n, tie_mode="code"):
    _validate_horizons((horizon,))
    tops = tuple(tops)
    if not tops or any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in tops):
        raise ValueError("tops 必须是非空正整数")
    if cost < 0 or not math.isfinite(cost):
        raise ValueError("cost 必须是非负有限数")
    pools = {}
    cohorts = {}
    n_days = 0

    for date, sec in iter_days(db, (horizon,), cat_col, table, tie_mode):
        n_days += 1
        rows = [(s, k, c, f[horizon]) for s, k, c, f in sec if horizon in f]
        if len(rows) < min_n:
            continue

        # ---- 池子表：每个 cat + 全部 + 被剔除的 + 被保留的
        by_cat = {}
        for _s, _k, c, r in rows:
            by_cat.setdefault(c, []).append(r)
        for c, rs in by_cat.items():
            pools.setdefault(f"cat:{c}", []).append(_mean(rs))
        pools.setdefault("ALL", []).append(_mean([r for _, _, _, r in rows]))
        kept = [r for _, _, c, r in rows if c not in drop] if drop else [r for _, _, _, r in rows]
        dropped = [r for _, _, c, r in rows if c in drop] if drop else []
        if kept:
            pools.setdefault("KEPT", []).append(_mean(kept))
        if dropped:
            pools.setdefault("DROPPED", []).append(_mean(dropped))

        # ---- 选股消融：排序键只允许 (分数/百分位, 确定性 tiekey)，不许碰收益
        scored = [(s, k, c, r) for s, k, c, r in rows if c not in drop] if drop else rows
        if not scored:
            continue
        pool_mean = _mean([r for _, _, _, r in scored])

        for n in tops:
            # ① 跨类取绝对分
            pairs = sorted(((s, k, r) for s, k, _c, r in scored),
                           key=lambda x: (x[0], x[1]), reverse=True)[:n]
            if pairs:
                cohorts.setdefault(f"abs_top{n}", []).append(
                    (date, _mean([p[2] for p in pairs]), pool_mean))
            # ② 类内取 top-N（每类各取 N 只）
            vals = []
            for c in {c for _, _, c, _ in scored}:
                sub = sorted(((s, k, r) for s, k, cc, r in scored if cc == c),
                             key=lambda x: (x[0], x[1]), reverse=True)[:n]
                vals.extend(r for _, _, r in sub)
            if vals:
                cohorts.setdefault(f"percat_top{n}", []).append(
                    (date, _mean(vals), pool_mean))
            # ③ 类内百分位后跨类比
            pct = []
            for c in {c for _, _, c, _ in scored}:
                sub = sorted(((s, k, r) for s, k, cc, r in scored if cc == c),
                             key=lambda x: (x[0], x[1]))
                k = len(sub)
                for i, (_s, kk, r) in enumerate(sub):
                    pct.append(((i + 1) / k, kk, r))
            if pct:
                pct.sort(key=lambda x: (x[0], x[1]), reverse=True)
                cohorts.setdefault(f"pct_top{n}", []).append(
                    (date, _mean([p[2] for p in pct[:n]]), pool_mean))
                hi = [p[2] for p in pct if p[0] >= 0.95]
                if hi:
                    cohorts.setdefault("pct_top5pct", []).append(
                        (date, _mean(hi), pool_mean))

    # ---- 净值：相位链式
    def chain(series, H, c):
        """series: [(date, ret, ...)] → (毛累计, 净累计, 年化)。"""
        gross, net = [], []
        for p in range(H):
            g = n = 1.0
            k = 0
            for i in range(p, len(series), H):
                g *= (1 + series[i][1])
                n *= (1 + series[i][1] - 2 * c)
                k += 1
            if k:
                gross.append(g)
                net.append(n)
        if not net:
            return None, None, None
        gm = sum(gross) / len(gross) - 1
        nm = sum(net) / len(net) - 1
        yrs = len(series) / (TD / H)
        ann = (1 + nm) ** (1 / yrs) - 1 if yrs > 0 and nm > -1 else None
        return gm, nm, ann

    pool_out = {}
    for name, daily in pools.items():
        s = [(f"d{i}", x) for i, x in enumerate(daily) if x is not None]
        gm, nm, ann = chain(s, horizon, cost)
        pool_out[name] = dict(days=len(s), gross_cum=gm, net_cum=nm, ann=ann,
                              mean_daily=_mean([x for x in daily if x is not None]))

    abl_out = {}
    for key, coh in cohorts.items():
        gm, nm, ann = chain([(d, r) for d, r, _ in coh], horizon, cost)
        alphas = [r - p for _, r, p in coh]
        mu = _mean(alphas)
        # 原先用未校正的朴素 t，逐日生成的 H 日 cohort 存在 H-1 日重叠；统一改用 hac_t_overlap
        sd = statistics.pstdev(alphas) if len(alphas) > 2 else 0.0
        deg = len(alphas) < 3 or sd < 1e-12 or len(set(round(x, 12) for x in alphas)) == 1
        t = None if deg else fl.hac_t_overlap(alphas, max(0, horizon - 1))[0]
        abl_out[key] = dict(cohorts=len(coh), gross_cum=gm, net_cum=nm, ann=ann,
                            alpha=mu, alpha_t=t, pool_ret=_mean([p for _, _, p in coh]))
    return dict(db=db, horizon=horizon, tops=tops, cost=cost, drop=list(drop),
                tie_mode=tie_mode, n_days=n_days,
                pool_table=pool_out, selection_ablation=abl_out)


def render(r):
    H, c = r["horizon"], r["cost"]
    md = [f"# 池子分解 / 选股消融　·　持有 {H} 日 · 单边成本 {c*100:.2f}%",
          f"\n- 库：`{r['db']}`　扫描 {r['n_days']} 个交易日"
          + (f"　剔除组：{r['drop']}" if r["drop"] else "")
          + f"　并列破平键：{r['tie_mode']}(sym)",
          "\n## 1. 池子表（各池等权，相位链式净值）\n",
          "| 池子 | 有效天数 | 日均收益 | 毛累计 | 净累计 | 年化 |",
          "|---|---:|---:|---:|---:|---:|"]
    for k in sorted(r["pool_table"], key=lambda x: -(r["pool_table"][x]["gross_cum"] or -9)):
        v = r["pool_table"][k]
        md.append(f"| {k} | {v['days']} | {(v['mean_daily'] or 0)*100:+.3f}% | "
                  f"{(v['gross_cum'] or 0)*100:+.1f}% | {(v['net_cum'] or 0)*100:+.1f}% | "
                  f"{(v['ann'] or 0)*100:+.2f}% |")
    if "KEPT" in r["pool_table"] and "DROPPED" in r["pool_table"]:
        gk = r["pool_table"]["KEPT"]["gross_cum"] or 0
        gd = r["pool_table"]["DROPPED"]["gross_cum"] or 0
        md.append(f"\n> **过滤器成本 = 被剔除池 {gd*100:+.1f}% − 被保留池 {gk*100:+.1f}% = "
                  f"{(gd-gk)*100:+.1f} 个百分点**（毛口径，未扣成本）。")
    md += ["\n## 2. 选股规则消融（同一批分数，只换选谁）\n",
           "| 规则 | cohort 数 | 池均收益 | 毛累计 | 净累计 | 年化 | alpha/期 | t |",
           "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for k in sorted(r["selection_ablation"],
                    key=lambda x: -(r["selection_ablation"][x]["gross_cum"] or -9)):
        v = r["selection_ablation"][k]
        t_str = "不可估" if v["alpha_t"] is None else f"{v['alpha_t']:+.2f}"
        md.append(f"| {k} | {v['cohorts']} | {(v['pool_ret'] or 0)*100:+.3f}% | "
                  f"{(v['gross_cum'] or 0)*100:+.1f}% | {(v['net_cum'] or 0)*100:+.1f}% | "
                  f"{(v['ann'] or 0)*100:+.2f}% | {(v['alpha'] or 0)*100:+.3f}% | "
                  f"{t_str} |")
    md += ["\n## 3. 怎么读这张表\n",
           "- **alpha 全正、净累计全负** → 问题不在选股规则，在池子整体在跌 + 换手成本。去优化选股是白费。",
           "- **alpha 为负** → 选股规则本身有害（常见于跨类取绝对分，分数不可比时必然如此）。",
           "- **净累计为正** → 继续看关 6 并列敏感性与关 7 组合层，别在这张表就下结论。",
           "- 池子表里 `KEPT` 与 `DROPPED` 的差，就是「门槛收的过路费」。它为正且很大时，",
           "  说明过滤器把好样本剔掉了——这时该重设门槛，不是调参数。",
           f"- 本表破平键为 `{r['tie_mode']}(sym)`。**换 `--tie hash` 再跑一次**，若 alpha 大幅变化，",
           "  说明结果由并列切分决定，不能引用（关 6 并列敏感性）。"]
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--fwd", type=int, default=5)
    ap.add_argument("--tops", default="5,20")
    ap.add_argument("--cost", type=float, default=0.002)
    ap.add_argument("--cat-col", default="cat")
    ap.add_argument("--drop", default="", help="逗号分隔，如 eliminated 或 eliminated,risk")
    ap.add_argument("--tie", default="code", choices=["code", "hash"])
    ap.add_argument("--table", default="rows")
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--json")
    a = ap.parse_args()
    tops = [int(x) for x in a.tops.split(",") if x.strip()]
    drop = tuple(x.strip() for x in a.drop.split(",") if x.strip())
    r = run(a.db, a.fwd, tops, a.cost, a.cat_col, drop, a.table, a.min_n, a.tie)
    print(render(r))
    if a.json:
        json.dump(r, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n已写 {a.json}", file=sys.stderr)


if __name__ == "__main__":
    main()