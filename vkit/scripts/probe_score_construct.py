# -*- coding: utf-8 -*-
"""构造效度探针：判断一个复合打分器的 score 能不能当尺子排序。

回答三个问题：
  1. 每组内部，score 是否被某个单一取值吃掉（封顶 / 托底）
  2. score >= 门槛的样本由哪些组构成（排序标签 vs 分类标签）
  3. 每日候选只数分布（门槛在极端日是否失效）

用法：
  python probe_score_construct.py --db audit.db
  python probe_score_construct.py --db audit.db --group cat --threshold 85 --top 20 --fwd fwd5
  python probe_score_construct.py --db audit.db --group regime --threshold 80 --json out.json

只读。纯标准库。表需含 date / score 两列，其余可指定。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def q(name: str, cols: list[str]) -> str:
    """标识符只有存在且形如标识符时才拼进 SQL——别无他法，sqlite 不支持参数化列名。"""
    if not IDENT.match(name) or name not in cols:
        raise SystemExit(f"列不存在或不合法：{name!r}（现有列：{cols}）")
    return name


def pct(a: float, b: float) -> str:
    return f"{a / b * 100:5.1f}%" if b else "    —"


def f1(v) -> str:
    """空组 / 全 NULL 列返回 NA，不报错也不写成 0。"""
    return f"{v:.1f}" if v is not None else "NA"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--table", default="rows")
    ap.add_argument("--score", default="score")
    ap.add_argument("--group", default="cat")
    ap.add_argument("--date", default="date")
    ap.add_argument("--threshold", type=float, default=85.0)
    ap.add_argument("--top", type=int, default=20, help="模拟每日 top-N 的入选构成")
    ap.add_argument("--fwd", default="", help="前向收益列，给出则附各组均值与全样本差值")
    ap.add_argument("--json", default="", help="把结果另存为 JSON")
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({a.table})")]
    if not cols:
        raise SystemExit(f"表不存在：{a.table}")
    S, G = q(a.score, cols), q(a.group, cols)
    D = q(a.date, cols) if a.date in cols else None
    F = q(a.fwd, cols) if a.fwd else None

    out: dict = {"db": a.db, "table": a.table, "threshold": a.threshold}
    print(f"# 构造效度探针 · {a.db} · 表 {a.table} · 门槛 {a.threshold:g}\n")

    # ---------- 1. 分组分布 + 单一取值聚集
    rows = list(con.execute(
        f"SELECT {G}, COUNT(*), MIN({S}), AVG({S}), MAX({S}), SUM({S} >= ?), SUM({S} IS NULL) "
        f"FROM {a.table} GROUP BY {G} ORDER BY 2 DESC", (a.threshold,)))
    modal = {}
    for g, s, c in con.execute(
            f"SELECT {G}, {S}, COUNT(*) FROM {a.table} GROUP BY {G}, {S} "
            f"ORDER BY {G}, 3 DESC"):
        if g not in modal:
            modal[g] = (s, c)

    print("## 1. 分组分布与单值聚集\n")
    print(f"| {a.group} | n | 占比 | min | avg | max | >=门槛 | 占该类 | 最高频分值 | 占比 | 判读 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    total = sum(r[1] for r in rows)
    group_info = []
    for g, n, mn, av, mx, hi, nul in rows:
        ms, mc = modal.get(g, (None, 0))
        hi = hi or 0
        share = mc / n if n else 0
        flag = "**封顶/托底**" if share >= 0.80 else ("单值偏集" if share >= 0.50 else "")
        print(f"| {g} | {n:,} | {pct(n, total)} | {f1(mn)} | {f1(av)} | {f1(mx)} | {hi:,} | "
              f"{pct(hi, n)} | {ms} | {share * 100:.1f}% | {flag} |")
        group_info.append({"group": g, "n": n, "min": mn, "avg": av, "max": mx,
                           "gte": hi, "modal": ms, "modal_share": share})
    print(f"\n合计 {total:,} 行；score 为 NULL {sum(r[6] for r in rows):,} 行")
    out["groups"] = group_info

    # ---------- 2. 阈值构成
    comp = list(con.execute(
        f"SELECT {G}, COUNT(*) FROM {a.table} WHERE {S} >= ? GROUP BY {G} ORDER BY 2 DESC",
        (a.threshold,)))
    hit = sum(c for _, c in comp)
    print(f"\n## 2. score >= {a.threshold:g} 的构成（排序真正会买到的东西）\n")
    print(f"| {a.group} | 条数 | 占门槛之上 |")
    print("|---|---:|---:|")
    for g, c in comp:
        print(f"| {g} | {c:,} | {pct(c, hit)} |")
    if hit and comp and comp[0][1] / hit >= 0.60:
        print(f"\n> ⚠️ 门槛之上 {comp[0][1] / hit * 100:.1f}% 集中在「{comp[0][0]}」一类。"
              "核对这是不是策略声称要买的那个类；不是就是分类标签≠排序标签。")
    out["threshold_composition"] = {g: c for g, c in comp}

    # ---------- 3. 每日候选只数
    if D:
        cnts = sorted(c for _, c in con.execute(
            f"SELECT {D}, COUNT(*) FROM {a.table} WHERE {S} >= ? GROUP BY {D}", (a.threshold,)))
        if cnts:
            med = statistics.median(cnts)
            hot = sum(1 for c in cnts if c > max(3 * med, med + 20))
            print("\n## 3. 每日 >=门槛 的只数\n")
            print(f"- 交易日 {len(cnts)}　中位 {med:g}　均值 {statistics.mean(cnts):.1f}　"
                  f"最小 {cnts[0]}　最大 {cnts[-1]}")
            print(f"- 异常日（超过中位 3 倍或 +20）：{hot} 天（{pct(hot, len(cnts))}）"
                  "——这些日子 top-N 近似随机抽样，组合模拟要单独报占比")
            out["per_day"] = {"days": len(cnts), "median": med,
                              "mean": statistics.mean(cnts), "min": cnts[0],
                              "max": cnts[-1], "hot_days": hot}

        # ---------- 4. top-N 入选构成
        try:
            tn = list(con.execute(
                f"SELECT {G}, COUNT(*) FROM (SELECT {G} AS {G}, "
                f"ROW_NUMBER() OVER (PARTITION BY {D} ORDER BY {S} DESC) rn "
                f"FROM {a.table} WHERE {S} IS NOT NULL) WHERE rn <= ? GROUP BY {G} ORDER BY 2 DESC",
                (a.top,)))
            tot = sum(c for _, c in tn)
            print(f"\n## 4. 每日 top-{a.top} 实际由哪些类构成\n")
            print(f"| {a.group} | 条数 | 占比 |")
            print("|---|---:|---:|")
            for g, c in tn:
                print(f"| {g} | {c:,} | {pct(c, tot)} |")
            out["topn_composition"] = {g: c for g, c in tn}
        except sqlite3.OperationalError as e:
            print(f"\n## 4. top-N 构成：算不了（{e}）—— sqlite 版本不支持窗口函数，升级或改用 Python 端排序")
    else:
        print(f"\n（表里没有 {a.date} 列，跳过每日与 top-N 两项）")

    # ---------- 5. 可选：各组收益 vs 全样本
    if F:
        base = con.execute(f"SELECT AVG({F}) FROM {a.table} WHERE {F} IS NOT NULL").fetchone()[0]
        if base is None:
            print(f"\n## 5. 各组平均前向收益（{a.fwd}）：该列全为空，算不了——不是 0，是不可估")
            base = None
        if base is not None:
            print(f"\n## 5. 各组平均前向收益（{a.fwd}）　全样本均值 {base * 100:+.2f}%\n")
            print(f"| {a.group} | n | 均值 | 相对全样本 |")
            print("|---|---:|---:|---:|")
            recs = []
            for g, n, av in con.execute(
                    f"SELECT {G}, COUNT(*), AVG({F}) FROM {a.table} WHERE {F} IS NOT NULL "
                    f"GROUP BY {G} ORDER BY 2 DESC"):
                print(f"| {g} | {n:,} | {av * 100:+.2f}% | {(av - base) * 100:+.2f}pp |")
                recs.append({"group": g, "n": n, "mean": av, "excess": av - base})
            out["group_returns"] = recs
            print("\n> 组间只比均值差（事件式），不倒过来排名次：量纲不同的分数排出来的名次没有含义。")

    print("\n## 判读清单\n")
    print("1. 有类别的单值聚集 >= 80% → 该类 rank-IC 一律不得引用，只做成败率/均值差。")
    print("2. 门槛之上集中在「未满条件 / 观察」类 → 分类标签≠排序标签，按文档与实现不一致报。")
    print("3. 异常日占比高 → 组合模拟要剔除或单独报占比，否则净值是随机抽样的结果。")
    print("4. 分组数 < 几百个截面日的 → 直接标 L0，不给判决。")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2, default=float)
        print(f"\n已写入 {a.json}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
