# -*- coding: utf-8 -*-
"""vkit — 否决器工具箱（Verdict Kit）。纯标准库，零依赖。

回答一个策略/因子「有没有信息」以及「信息值不值钱」，并在它该死的时候给出判死理由。

七个子命令：
  selftest                             自检（18 项，含假阳性率与重叠校正的校准、入口校验）
  ic      --bars DIR --factor CSV      单因子截面检验（IC/衰减/分层/滚动/Bonferroni/成本）
  sim     --bars DIR --signals CSV     组合模拟：同一批入场，比 7 种离场规则
  yearly  --bars DIR --factor CSV      分年 IC 与分档（关 4 / 样本外切分）
  conc    --rows CSV                   集中度：剔掉贡献最大的 N 天后还剩什么
  timing  --signals CSV --index CSV    择时：信号触发日 vs 其他日的前向收益
  db      --db SQLITE                  大样本审计库直读（IC/分层/成本/Walk-Forward）

数据格式（全部 CSV，UTF-8，第一行表头）
  bars/<code>.csv : date,open,high,low,close[,volume]
  factor.csv      : date,code,value
  signals.csv     : date,code
  rows.csv        : date,value          （每笔/每观测的收益，按日分组）
  index.csv       : date,close

统计约定（v1.3）：
  · 分层用平均秩定位档位 —— 同分同档，结果与输入顺序无关。
  · 前向收益重叠 → IC 的 t 用 Newey-West 校正；择时用 HAC-OLS。
  · 组合模拟的配对差同时报「日期聚类」和「持有期重叠校正」两个 t；后者按有效自由度取
    t 临界值。持有 H 日的交易共享 H-1 天行情，只做日期聚类仍会高估显著性。
  · 组合模拟与 IC 同口径：T+1 收盘买入、T+1+H 收盘卖出（--entry open 可改，但会多一段隔夜）。
  · 跳空开盘低于止损位时该笔**保留**并在开盘价离场，不得整笔删除。
  · 非法数值一律报错，不静默变成 0；零方差/空样本给明确提示，不抛异常。
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import random
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import factor_lab as _fl          # 同一个估计量，不许各写一套


# ============================================================ 数据
class Bar:
    __slots__ = ("date", "open", "high", "low", "close", "volume")

    def __init__(self, date, o, h, l, c, v=0.0):
        self.date, self.open, self.high, self.low, self.close, self.volume = date, o, h, l, c, v


def _num(x, where=""):
    """严格解析：空值、非数字、NaN/Inf 一律报错，绝不静默变成 0。"""
    if x is None or str(x).strip() == "":
        raise ValueError(f"{where}: 空值")
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: 非法数字 {x!r}")
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{where}: NaN/Inf")
    return v


def load_bars(path: str) -> list[Bar]:
    out = []
    with open(path, encoding="utf-8", newline="") as f:
        for i, r in enumerate(csv.DictReader(f), 2):
            try:
                o, h, l, c = (_num(r["open"], f"{path}:{i}"), _num(r["high"], f"{path}:{i}"),
                              _num(r["low"], f"{path}:{i}"), _num(r["close"], f"{path}:{i}"))
            except KeyError as ex:
                raise SystemExit(f"{path}: 缺少列 {ex}")
            v = _num(r["volume"], f"{path}:{i}") if r.get("volume") not in (None, "") else 0.0
            if min(o, h, l, c) <= 0:
                raise SystemExit(f"{path}:{i}: 价格必须为正（open={o} high={h} low={l} close={c}）")
            if v < 0:
                raise SystemExit(f"{path}:{i}: volume 不得为负（volume={v}）")
            if h < l or h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9:
                raise SystemExit(f"{path}:{i}: OHLC 不自洽（high={h} low={l} open={o} close={c}）")
            date = (r.get("date") or "").strip()
            if not date:
                raise SystemExit(f"{path}:{i}: 日期为空")
            out.append(Bar(date, o, h, l, c, v))
    if not out:
        raise SystemExit(f"{path}: 没有数据行")
    out.sort(key=lambda b: b.date)
    for a, b in zip(out, out[1:]):
        if a.date == b.date:
            raise SystemExit(f"{path}: 重复日期 {a.date}")
    return out


def load_bars_dir(d: str) -> dict[str, list[Bar]]:
    files = sorted(glob.glob(os.path.join(d, "*.csv")))
    if not files:
        raise SystemExit(f"{d} 下没有 csv")
    return {os.path.splitext(os.path.basename(p))[0]: load_bars(p) for p in files}


def load_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        raise SystemExit(f"找不到 {path}")
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"{path}: 没有数据行")
    return rows


def _is_untradable_bar(bar: Bar, prev_close: float | None = None) -> bool:
    """A 股不可成交日判定：停牌（volume == 0）或一字板（high == low，全天单一价格锁死）。"""
    if bar.volume == 0:
        return True
    if abs(bar.high - bar.low) < 1e-9:
        return True
    return False


def _is_limit_down_locked(bar: Bar, prev_close: float) -> bool:
    """跌停锁死或停牌无法卖出：volume==0，或一字跌停（high==low 且较前收盘下跌）。"""
    if bar.volume == 0:
        return True
    if abs(bar.high - bar.low) < 1e-9 and bar.close < prev_close * 0.995:
        return True
    return False


def untradable_dates(bars_by_code: dict[str, list[Bar]]) -> dict[str, set[str]]:
    """返回每只标的不可在当日买入的日期集合（停牌 volume==0 或一字板 high==low）。"""
    out: dict[str, set[str]] = {}
    for code, b_list in bars_by_code.items():
        bad = set()
        for i, b in enumerate(b_list):
            pc = b_list[i - 1].close if i > 0 else None
            if _is_untradable_bar(b, pc):
                bad.add(b.date)
        if bad:
            out[code] = bad
    return out


def _track_trials(target_path: str, param_sig: str, requested_trials: int) -> tuple[int, str]:
    """在同一数据集目录维护 .verdict_trials.json，自动累计不同参数组合的尝试次数。
    若用户传入的 --trials 小于历史已跑过的不同参数组合数，自动按历史次数抬高门槛并告警。
    """
    import hashlib
    import json
    base_dir = os.path.dirname(os.path.abspath(target_path))
    ledger_file = os.path.join(base_dir, ".verdict_trials.json")
    key = hashlib.md5(os.path.abspath(target_path).encode("utf-8")).hexdigest()[:12]
    data = {}
    if os.path.isfile(ledger_file):
        try:
            with open(ledger_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
    seen = list(data.get(key, []))
    if param_sig not in seen:
        seen.append(param_sig)
        data[key] = seen
        try:
            with open(ledger_file, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
    logged = len(seen)
    eff = max(requested_trials, logged)
    note = ""
    if logged > requested_trials:
        note = (f"> ⚠️ **多重检验账本自动校正**：该数据集已累计运行 {logged} 组不同参数，"
                f"高于手填 `--trials {requested_trials}`；已自动按 {eff} 次尝试抬高 Bonferroni 门槛。\n")
    return eff, note


# ============================================================ 指标
def ma(bars, n, end=None):
    end = len(bars) if end is None else end
    if end < n or n <= 0:
        return None
    return sum(b.close for b in bars[end - n:end]) / n


def atr(bars, n=14, end=None):
    end = len(bars) if end is None else end
    if end < n + 1:
        return 0.0
    trs = []
    for i in range(end - n, end):
        pc = bars[i - 1].close
        trs.append(max(bars[i].high - bars[i].low,
                       abs(bars[i].high - pc), abs(bars[i].low - pc)))
    return sum(trs) / n


def _rolling(b, n=20):
    out, q, s = [None] * len(b), [], 0.0
    for i, x in enumerate(b):
        q.append(x.close)
        s += x.close
        if len(q) > n:
            s -= q.pop(0)
        if len(q) == n:
            out[i] = s / n
    return out


# ============================================================ 稳健统计
def cluster_t(vals, groups):
    """按 groups 聚类估标准误的 t（同一日期的多笔交易不独立）。

    **只处理同一天内的相关，处理不了相邻信号日之间的重叠。** 持有 H 日的交易
    会共享 H-1 天的行情，日期聚类之后 t 仍然偏高。判定必须同时看 overlap_t。
    """
    n = len(vals)
    if n < 3:
        return 0.0
    m = sum(vals) / n
    sums = defaultdict(float)
    for v, g in zip(vals, groups):
        sums[g] += v - m
    var = sum(s * s for s in sums.values()) / (n * n)
    if var <= 0:
        return 0.0
    return m / math.sqrt(var)


def t_crit(df: float) -> float:
    """双侧 95% 的 t 临界值（实现在 factor_lab，全仓只此一份）。"""
    return _fl.t_crit(df)


def overlap_t(vals, dates, hold):
    """持有期重叠校正的 t：按日等权 → HAC(lag = hold-1) → 有效自由度。

    为什么不能只用日期聚类：持有 H 日的交易，相邻信号日共享 H-1 天行情，
    日度均值序列是 MA(H-1)，自相关最长到 H-1 阶。Bartlett 权在 lag=H-1 时
    把远端自协方差压低 20%–40%，仍然低估方差（合成数据上假阳性 12%–17%）。

    三处一起改：
      1. 按日等权（一天一票），不让样本多的日子主导；
      2. HAC 同时算平坦权与 Bartlett 权，取**较大**的方差——平坦权期望上无偏
         但可能算出负数（不是半正定），Bartlett 保正但偏低，取大者偏保守；
      3. 自由度按有效样本 n/(2·lag+1) 算，用 t 临界值而不是 1.96。
         自由度 < 3 时不给 t（样本短于约 6H 天，这个校正本身估不出来）。

    合成数据（纯噪声 + 20 日重叠）：日期聚类假阳性 46%–69%，这里 3%–7%。

    返回 dict：t / crit / df / lag / n_days / n / insufficient / degenerate。
    t is None 表示不可估——不许当成 0 报出去。
    """
    by = defaultdict(list)
    for v, d in zip(vals, dates):
        by[d].append(v)
    daily = [sum(by[k]) / len(by[k]) for k in sorted(by)]
    n = len(daily)
    lag = max(0, min(int(hold) - 1, n - 2)) if n >= 3 else 0
    need = 3 * (2 * lag + 1)
    base = {"df": max(1, n // (2 * lag + 1)), "lag": lag, "n_days": n,
            "n": len(vals), "need_days": need, "crit": None, "t": None}
    if n < need:
        return {**base, "insufficient": True, "degenerate": False}
    t, crit, df = _fl.hac_t_overlap(daily, lag)
    if t == 0.0 and crit <= 0:
        return {**base, "insufficient": False, "degenerate": True}
    return {**base, "t": t, "crit": crit, "df": df,
            "insufficient": False, "degenerate": False}


def hac_ols_diff(y, g, lag):
    """模型 y = a + b*g 的 HAC(Newey-West) 估计，返回 (b = 组1−组0 的均值差, t_b)。"""
    n = len(y)
    if n < 5 or len(set(g)) < 2:
        return 0.0, 0.0
    gb = sum(g) / n
    yb = sum(y) / n
    sxx = sum((x - gb) ** 2 for x in g)
    if sxx <= 0:
        return 0.0, 0.0
    b = sum((x - gb) * (v - yb) for x, v in zip(g, y)) / sxx
    a = yb - b * gb
    e = [v - a - b * x for x, v in zip(g, y)]
    mom = [(x - gb) * ei for x, ei in zip(g, e)]
    s = sum(m * m for m in mom) / n
    lag = max(1, min(lag, n - 2))
    for l in range(1, lag + 1):
        gl = sum(mom[i] * mom[i - l] for i in range(l, n)) / n
        s += 2 * (1 - l / (lag + 1)) * gl
    if s <= 0:
        return b, 0.0
    # Var(b) = n*S/sxx^2  →  SE = sqrt(n*S)/sxx
    return b, b * sxx / math.sqrt(n * s)


# ============================================================ IC 检验
def cmd_ic(args):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import factor_lab as fl

    bars = load_bars_dir(args.bars)
    close_by_code = {c: {b.date: b.close for b in v} for c, v in bars.items()}
    factor_by_date = defaultdict(dict)
    seen = set()
    for r in load_csv(args.factor):
        d, code = r["date"].strip(), r["code"].strip()
        if (d, code) in seen:
            raise SystemExit(f"factor.csv 出现重复记录：{d} {code}")
        seen.add((d, code))
        factor_by_date[d][code] = _num(r["value"], f"factor.csv:{d},{code}")

    horizons = _parse_horizons(args.horizons)
    untradable = untradable_dates(bars)
    rows_all = fl.build_rows(dict(factor_by_date), close_by_code, horizons=horizons)
    rows = fl.build_rows(dict(factor_by_date), close_by_code, horizons=horizons,
                         untradable_by_code=untradable)
    blocked_entry = len(rows_all) - len(rows)
    if not rows:
        if rows_all:
            raise SystemExit(f"没有可用观测：{len(rows_all):,} 条入场全部落在停牌/一字板日（不可成交剔除），"
                             "不是日期/代码对不上")
        raise SystemExit("没有可用观测：检查 factor.csv 的日期/代码是否与 bars 对得上")
    eff_trials, trial_note = _track_trials(
        args.factor, f"ic:{horizons}:{args.slices}:{args.cost}", args.trials)
    md = fl.report(rows, horizons=horizons, n_trials=eff_trials,
                   rolling_slices=args.slices, cost_pct=args.cost,
                   input_obs=len(seen))
    if blocked_entry > 0:
        md = md.replace("- 收益口径：", f"- 不可成交剔除：T+1 停牌或一字涨跌停拦截 {blocked_entry:,} 条\n- 收益口径：", 1)
    if trial_note:
        md = trial_note + "\n" + md
    print(md)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(md)
        print(f"\n→ {args.out}")


# ============================================================ 组合模拟
VARIANTS = {"A": "A 不动（持满）", "B": "B 仅止损1.15ATR", "C": "C 仅跌破MA20",
            "D": "D 止损+MA20（组合）", "E": "E 止损2ATR", "F": "F 止损3ATR",
            "G": "G MA20两日确认"}


def cmd_sim(args):
    bars = load_bars_dir(args.bars)
    sig = defaultdict(list)
    seen = set()
    dup = 0
    for r in load_csv(args.signals):
        d, code = r["date"].strip(), r["code"].strip()
        if not d or not code:
            raise SystemExit("signals.csv 有空行")
        if (d, code) in seen:
            dup += 1
            continue
        seen.add((d, code))
        sig[code].append(d)

    H = args.hold
    entry_at = getattr(args, "entry", "close")
    trades, gap_kept, no_bar, no_win, no_trade_entry, deferred_exit = [], 0, 0, 0, 0, 0
    for code, dates in sig.items():
        b = bars.get(code)
        if not b:
            no_bar += len(dates)
            continue
        idx = {x.date: i for i, x in enumerate(b)}
        ma20 = _rolling(b, 20)
        N = len(b)
        for d in dates:
            j = idx.get(d)
            if j is None:
                continue
            if j + 1 + H >= N:            # 窗口不足：跳过，不拿数据末尾截断冒充满期
                no_win += 1
                continue
            if _is_untradable_bar(b[j + 1], b[j].close):
                no_trade_entry += 1
                continue
            e = b[j + 1].close if entry_at == "close" else b[j + 1].open
            if e <= 0:
                continue
            low5 = min(x.low for x in b[max(0, j - 4): j + 1])
            a = atr(b, 14, j + 1)
            stop = min(low5 * 0.99, b[j].close - 1.15 * a) if a > 0 else low5 * 0.99
            if stop <= 0:
                continue
            if stop >= e:                 # 开盘已在止损线下方：保留这笔，_walk 按开盘价离场
                gap_kept += 1
            rec = {"date": d, "code": code, "entry": e, "stop": stop}
            for name in VARIANTS:
                sp = stop
                if name in ("E", "F") and a > 0:
                    sp = b[j].close - (2.0 if name == "E" else 3.0) * a
                wres = _walk(b, ma20, j, e, sp, name, H, entry_at)
                rec.update(wres)
            if rec.get("B_deferred"):
                deferred_exit += 1
            trades.append(rec)

    if not trades:
        raise SystemExit(f"没有可模拟的入场（T+1 停牌/一字板不可买 {no_trade_entry} 笔；"
                         f"重复信号 {dup}；缺行情 {no_bar}；窗口不足 {H} 日 {no_win}）")
    print(f"入场 {len(trades):,} 笔（跳空开盘已在止损线下仍计入：{gap_kept}；"
          f"T+1停牌/一字板不可买：{no_trade_entry}；一字跌停止损顺延：{deferred_exit}；"
          f"重复信号去重：{dup}；缺行情：{no_bar}；窗口不足 {H} 日：{no_win}）")
    if entry_at == "close":
        print(f"口径：T 日信号 → T+1 收盘买入 → T+{1 + H} 收盘卖出，"
              f"与 `ic --horizons {H}` 的收益口径一致（可直接对照）。")
    else:
        print(f"口径：T 日信号 → T+1 开盘买入 → T+{1 + H} 收盘卖出；"
              f"比 ic 的 {H} 日多一段隔夜，不能与 ic 的数字直接比较。")
    print()

    print("| 变体 | 平均收益 | 中位 | 胜率 | p5 | p10 | 波动 | 最差 | 平均天数 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k in VARIANTS:
        v = [t[f"{k}_ret"] for t in trades]
        if not v:
            continue
        print(f"| {VARIANTS[k]} | {st.mean(v):+.3f}% | {st.median(v):+.2f}% | "
              f"{100*sum(1 for x in v if x>0)/len(v):.1f}% | "
              f"{_q(v,0.05):+.2f}% | {_q(v,0.10):+.2f}% | "
              f"{(st.pstdev(v) if len(v)>1 else 0):.2f}% | {min(v):+.2f}% | "
              f"{st.mean(t[f'{k}_days'] for t in trades):.1f} |")

    print("\n配对差（同一笔 vs A）：")
    print("| 对比 | 平均差 | 中位差 | t(日期聚类) | t(重叠校正) | 门槛 | 按日等权差 | 天数/df |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    base = [t["A_ret"] for t in trades]
    dates = [t["date"] for t in trades]
    for k in "BCDEFG":
        d = [t[f"{k}_ret"] - b for t, b in zip(trades, base)]
        by_day = defaultdict(list)
        for x, dd in zip(d, dates):
            by_day[dd].append(x)
        per_day = st.mean(st.mean(v) for v in by_day.values())   # 一天一票
        ov = overlap_t(d, dates, H)
        if ov["t"] is None:
            t_ov, crit = ("不可估" if ov["insufficient"] else "退化"), "—"
        else:
            t_ov, crit = f"{ov['t']:+.2f}", f"{ov['crit']:.2f}"
        print(f"| {k} − A | {st.mean(d):+.3f}% | {st.median(d):+.3f}% | "
              f"{cluster_t(d, dates):+.2f} | {t_ov} | {crit} | "
              f"{per_day:+.3f}% | {ov['n_days']}/{ov['df']} |")

    print("\n判读顺序：")
    print("  1) 先看 p5 / p10 / 最差 —— 离场规则的真实价值通常在尾部，不在均值。")
    print("  2) 再比「平均差」与「按日等权差」—— 两者符号相反，说明少数日期在主导，结论不可靠。")
    print("  3) 显著性看「t(重叠校正)」是否过「门槛」列 —— 日期聚类只处理同日相关，"
          "持有 H 日的交易还共享 H-1 天行情，聚类 t 仍偏高。")
    print("     门槛是有效自由度下的 t 临界值（天数/(2H-1) 决定），不是 1.96。")
    first = overlap_t([t["B_ret"] - t["A_ret"] for t in trades], dates, H)
    if first["insufficient"]:
        print(f"  ⚠️ 重叠校正不可估：只有 {first['n_days']} 个信号日，"
              f"持有 {H} 日的校正至少要 {first['need_days']} 天。别把「不可估」读成「不显著」。")
    elif first["degenerate"]:
        print(f"  ⚠️ 重叠校正退化：日度差值方差为 0（同一天所有交易差值相同），t 不可估。")
    print("  4) 这里只是同一批入场的离场规则对比：没有仓位、资金曲线、成交与容量模拟，"
          "任何 L4/L5 判断都要另做，不能拿这张表当上线证据。")


def _q(v, p):
    s = sorted(v)
    return s[min(len(s) - 1, max(0, int(len(s) * p)))]


def _walk(b, ma20, j, e, stop, name, H, entry_at="close"):
    """从入场那一根开始逐日走，返回该变体的收益（%）与持仓天数。

    入场价与 factor_lab.build_rows 对齐：T+1 收盘买入 → T+1+H 收盘卖出。
    跳空开盘在止损线下方时按开盘价离场（保留这笔，不删样本）。
    若触发止损当日为一字跌停或停牌（不可卖出），顺延至首个可成交日按开盘价离场。
    """
    N = len(b)
    exit_idx = j + 1 + H
    start = j + 2 if entry_at == "close" else j + 1
    for t in range(start, min(exit_idx + 1, N)):
        if name in ("B", "D", "E", "F"):
            if b[t].open <= stop or b[t].low <= stop:
                if _is_limit_down_locked(b[t], b[t - 1].close):
                    u = t + 1
                    while u < N - 1 and _is_limit_down_locked(b[u], b[u - 1].close):
                        u += 1
                    px = b[min(u, N - 1)].open
                    return {f"{name}_ret": (px / e - 1) * 100, f"{name}_days": min(u, N - 1) - j,
                            f"{name}_deferred": True}
                px = b[t].open if b[t].open <= stop else stop
                return {f"{name}_ret": (px / e - 1) * 100, f"{name}_days": t - j, f"{name}_deferred": False}
        if name in ("C", "D", "G") and t > j + 1:
            m = ma20[t]
            if m and b[t].close < m * 0.97 and b[t].close < b[t].open:
                if name == "G":
                    m2 = ma20[t - 1] if t >= 1 else None
                    if not (m2 and b[t - 1].close < m2 * 0.97 and b[t - 1].close < b[t - 1].open):
                        continue
                # 次日开盘离场；但不得越过持有窗口，否则等于偷看窗口外的价格
                px = b[t + 1].open if t + 1 <= exit_idx and t + 1 < N else b[t].close
                return {f"{name}_ret": (px / e - 1) * 100, f"{name}_days": t - j}
    t = min(exit_idx, N - 1)
    return {f"{name}_ret": (b[t].close / e - 1) * 100, f"{name}_days": t - j}


# ============================================================ 分年
def cmd_yearly(args):
    """逐年 IC + 分档价差：用来做"分年"这一关，也用来切样本外。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import factor_lab as fl

    bars = load_bars_dir(args.bars)
    close_by_code = {c: {b.date: b.close for b in v} for c, v in bars.items()}
    fb = defaultdict(dict)
    for r in load_csv(args.factor):
        fb[r["date"].strip()][r["code"].strip()] = _num(r["value"], "factor.csv")
    h = args.horizon
    rows_all = fl.build_rows(dict(fb), close_by_code, horizons=(h,))
    untradable = untradable_dates(bars)
    rows = fl.build_rows(dict(fb), close_by_code, horizons=(h,), untradable_by_code=untradable)
    blocked = len(rows_all) - len(rows)
    if not rows:
        if rows_all:
            raise SystemExit(f"没有可用观测：{len(rows_all):,} 条入场全部落在停牌/一字板日（不可成交剔除），"
                             "不是日期/代码对不上")
        raise SystemExit("没有可用观测：检查 factor.csv 的日期/代码是否与 bars 对得上")

    years = sorted({r[0][:4] for r in rows})
    th = _th(args.trials)
    print(f"持有期 {h} 日 · 多重检验门槛 |t| ≥ {th:.2f}（{args.trials} 次尝试）")
    if blocked:
        print(f"- 不可成交剔除：T+1 停牌或一字涨跌停拦截 {blocked:,} 条（与 `ic` 同口径）")
    print()
    print("| 年份 | 交易日 | IC | t朴素 | t_HAC | 低−高 | 判定 |")
    print("|---|---:|---:|---:|---:|---:|:--|")
    sig_years = []
    for y in years:
        sub = [r for r in rows if r[0][:4] == y]
        stt = fl.ic_summary(fl.daily_ic(sub, h), h)
        ss = fl.spread_summary(fl.quantile_spread(sub, h))
        spread = f"{ss['low_minus_high']*100:+.2f}%" if ss else "—"
        if not stt:
            print(f"| {y} | {len({r[0] for r in sub})} | 样本不足 | | | {spread} | — |")
            continue
        mark = "显著" if abs(stt.t_hac) >= th else "不显著"
        if stt.degenerate:
            mark = "退化"
        sig_years.append((y, stt.t_hac))
        print(f"| {y} | {stt.n_days} | {stt.mean:+.4f} | {stt.t:+.2f} | "
              f"{stt.t_hac:+.2f} | {spread} | {mark} |")

    if len(sig_years) >= 3:
        pos = sum(1 for _, t in sig_years if t > 0)
        last_y, last_t = sig_years[-1]
        all_st = fl.ic_summary(fl.daily_ic(rows, h), h)
        same = (all_st.t_hac > 0) == (last_t > 0)
        print(f"\n判读：{pos}/{len(sig_years)} 年为正；最近一年 {last_y} t_HAC={last_t:+.2f}。")
        if same:
            print("  · 最近一年方向与总体一致 → 关 4 通过，但要检查它是否也过了 Bonferroni 门槛")
        else:
            print("  · 最近一年与总体反号 → 近期证据不支持（关 4 不过，不允许拟合 regime 开关）")
        print("  · 只有一种情况算样本外：规则、参数、阈值在看到这一年之前就写死，且一次没改。"
              "先看后调的最后一年只是样本内，叫不出 OOS。")
        print("  · 真样本外要留出没参与任何决策的一段数据：时间在后，或换一个市场/标的池。")


# ============================================================ 集中度
def cmd_conc(args):
    g = defaultdict(list)
    for r in load_csv(args.rows):
        g[r["date"].strip()].append(_num(r["value"], f"rows.csv:{r['date']}"))

    tot = sum(len(v) for v in g.values())
    s = sum(sum(v) for v in g.values())
    print(f"全部：n={tot:,}  均值 {s/tot:+.3f}%  交易日 {len(g)}")
    print(f"按日等权：{st.mean(st.mean(v) for v in g.values()):+.3f}%"
          f"（与上一行差异大 → 少数日期样本量爆炸，池化均值被少数日期绑架）\n")

    order = sorted(g.items(), key=lambda kv: -sum(kv[1]))
    print("| 剔除贡献最大的 N 天 | 剩余 n | 均值 | 剩余按日等权 |")
    print("|---:|---:|---:|---:|")
    for k in (1, 3, 5, 10, 20):
        if k >= len(order):
            break
        drop = {d for d, _ in order[:k]}
        keep = {d: v for d, v in g.items() if d not in drop}
        n = sum(len(v) for v in keep.values())
        if not n:
            break
        s2 = sum(sum(v) for v in keep.values())
        print(f"| {k} | {n:,} | {s2/n:+.3f}% | "
              f"{st.mean(st.mean(v) for v in keep.values()):+.3f}% |")

    print("\n被剔除的日期（按贡献排序）：")
    for d, v in order[:10]:
        print(f"  {d}  n={len(v):>5}  均值 {st.mean(v):+.2f}%")

    print("\n判读（两问都答，缺一不可）：")
    print("  1) 剔掉 5 天后是否为负？")
    print("  2) 被剔掉的那几天，**事前**能不能识别？")
    print("     政策/救市日 = 不可识别 → 判死；财报季、指数调整、期权到期 = 可识别 →")
    print("     集中度本身不构成否决，但必须改写成事件策略并重新计入多重检验。")


# ============================================================ 择时
def cmd_timing(args):
    idx = _load_index(args.index)
    days = sorted(idx)
    pos = {d: i for i, d in enumerate(days)}
    fire = defaultdict(int)
    for r in load_csv(args.signals):
        fire[r["date"].strip()] += 1

    H = args.hold
    print(f"前向 {H} 日 · 指数 {args.index}")
    print(f"注：H 日前向收益天然重叠，已用 HAC 校正；4 个阈值 = 4 次尝试，"
          f"门槛应抬到 |t| ≥ {_th(4):.2f}\n")
    print("| 阈值 | 触发天 | 触发日前向 | 其余日前向 | 差 | t(朴素) | t(HAC) |")
    print("|---:|---:|---:|---:|---:|---:|---:|")
    for th in (1, 3, 5, 10):
        y, g = [], []
        for d in days:
            i = pos[d]
            if i + H >= len(days):
                break
            y.append((idx[days[i + H]] / idx[d] - 1) * 100)
            g.append(1.0 if fire.get(d, 0) >= th else 0.0)
        a = [v for v, x in zip(y, g) if x > 0]
        b = [v for v, x in zip(y, g) if x == 0]
        if len(a) < 20 or len(b) < 20:
            continue
        m = st.mean(a) - st.mean(b)
        se = (st.pstdev(a) ** 2 / len(a) + st.pstdev(b) ** 2 / len(b)) ** 0.5
        _, t_hac = hac_ols_diff(y, g, lag=H)
        print(f"| ≥{th} | {len(a)} | {st.mean(a):+.2f}% | {st.mean(b):+.2f}% | "
              f"{m:+.2f}pp | {(m/se if se else 0):+.2f} | {t_hac:+.2f} |")
    print("\n判读：以 t(HAC) 为准。|t|<2 就是没有择时能力，别用「信号日之后大概率反弹」这类叙事。")


def _th(trials):
    """Bonferroni 双侧 95% 门槛（正态近似）。"""
    from statistics import NormalDist
    return NormalDist().inv_cdf(1 - 0.025 / max(1, trials))


def _load_index(path):
    out = {}
    for r in load_csv(path):
        out[r["date"].strip()] = _num(r["close"], f"index:{r['date']}")
    return out


# ============================================================ 自检
# ---------------------------------------------------------------- 输入校验
def _positive_int(text: str) -> int:
    """正整数参数。负持有期曾是隐形后门：会算出回看收益，还照样打印成一张合格的表。"""
    try:
        v = int(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"需要正整数，收到 {text!r}")
    if v <= 0:
        raise argparse.ArgumentTypeError(f"需要正整数，收到 {text!r}")
    return v


def _nonnegative_float(text: str) -> float:
    try:
        v = float(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"需要非负数，收到 {text!r}")
    if v < 0 or not math.isfinite(v):
        raise argparse.ArgumentTypeError(f"需要非负有限数，收到 {text!r}")
    return v


def _parse_horizons(text: str) -> tuple[int, ...]:
    """--horizons 解析：非空、正整数、不重复；同一口径在 build_rows 里再验一次。"""
    parts = [p.strip() for p in str(text).split(",") if p.strip() != ""]
    bad = "--horizons 必须是非空、互不重复的正整数，如 1,5,10,20"
    if not parts:
        raise SystemExit(bad)
    try:
        vals = tuple(int(p) for p in parts)
    except ValueError:
        raise SystemExit(f"--horizons 只能填整数，收到 {text!r}")
    if any(v <= 0 for v in vals) or len(set(vals)) != len(vals):
        raise SystemExit(bad)
    return vals


def cmd_db(args):
    """大样本 SQLite 审计库直读入口（打通 CSV 与 SQLite 双通道）。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db_lab as dbl
    hs = _parse_horizons(args.horizons)
    eff_trials, trial_note = _track_trials(
        args.db, f"db:{hs}:{args.cat}:{args.lo_score}:{args.hi_score}:{args.walkforward}", args.trials)
    argv = ["--db", args.db, "--table", args.table, "--horizons", args.horizons,
            "--trials", str(eff_trials), "--cat", args.cat, "--q", str(args.q),
            "--slices", str(args.slices), "--first-test-year", str(args.first_test_year)]
    if args.lo_score is not None:
        argv += ["--lo-score", str(args.lo_score)]
    if args.hi_score is not None:
        argv += ["--hi-score", str(args.hi_score)]
    if args.walkforward:
        argv.append("--walkforward")
    if args.out:
        argv += ["--out", args.out]
    if trial_note:
        print(trial_note)
    return dbl.main(argv)


def cmd_selftest(args):
    """合成数据：真信号 / 纯噪声 / 并列 / 跳空 / 假阳性率。"""
    rnd = random.Random(20261102)
    out_dir = args.out
    bars_dir = os.path.join(out_dir, "bars")
    os.makedirs(bars_dir, exist_ok=True)

    codes = [f"S{i:03d}" for i in range(60)]
    days = [f"D{i:04d}" for i in range(320)]
    price = {c: 10.0 + rnd.random() * 5 for c in codes}
    panel = {c: [] for c in codes}
    for d in days:
        for c in codes:
            o = price[c]
            price[c] = max(1.0, price[c] * (1 + rnd.gauss(0, 0.02)))
            cl = price[c]
            panel[c].append(Bar(d, o, max(o, cl) * 1.01, min(o, cl) * 0.99, cl,
                                rnd.uniform(1e6, 5e6)))
    for c in codes:
        _write(os.path.join(bars_dir, f"{c}.csv"),
               ["date", "open", "high", "low", "close", "volume"],
               [(b.date, b.open, b.high, b.low, b.close, b.volume) for b in panel[c]])

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import factor_lab as fl
    close_by_code = {c: {b.date: b.close for b in panel[c]} for c in codes}
    ok = True

    # ---------- 1. IC 分辨力 + 前视报警
    good, null = [], []
    for di in range(60, 300):
        d = days[di]
        for c in codes:
            fwd5 = panel[c][di + 5].close / panel[c][di + 1].close - 1
            good.append((d, c, fwd5 * 100 + rnd.gauss(0, 2)))
            null.append((d, c, rnd.gauss(0, 1)))

    def ic_of(rows):
        fb = defaultdict(dict)
        for d, c, v in rows:
            fb[d][c] = v
        r = fl.build_rows(dict(fb), close_by_code, horizons=(5,))
        return fl.ic_summary(fl.daily_ic(r, 5), 5)

    st_real, st_null = ic_of(good), ic_of(null)
    print("=== 自检 1/18：IC 能否分辨真信号与噪声，并对过高的 IC 报警 ===")
    print(f"  真信号 IC={st_real.mean:+.4f}  t={st_real.t:+.2f}  t_HAC={st_real.t_hac:+.2f}")
    print(f"  纯噪声 IC={st_null.mean:+.4f}  t={st_null.t:+.2f}  t_HAC={st_null.t_hac:+.2f}")
    warned = "前视" in _ic_report(fl, good, close_by_code)
    if abs(st_real.t_hac) < 2.6:
        print("  ✗ 真信号没被识别"); ok = False
    elif abs(st_null.t_hac) > 2.6:
        print("  ✗ 噪声被误判为信号"); ok = False
    elif not warned:
        print("  ✗ IC 明显过高（>0.30）却没有给出前视警告"); ok = False
    else:
        print("  ✓ 通过（真信号识别 / 噪声拒绝 / 高 IC 已报警）")

    # ---------- 2. 并列因子：输入顺序不得改变结果
    tie_rows = []
    for di in range(20):
        for i, c in enumerate(codes[:30]):
            tie_rows.append((days[di], c, float(i // 3), {5: 0.01 * ((i * 7) % 11 - 5) / 5}))
    a = fl.spread_summary(fl.quantile_spread(tie_rows, 5))
    shuffled = tie_rows[:]
    rnd.shuffle(shuffled)
    b = fl.spread_summary(fl.quantile_spread(shuffled, 5))
    print("\n=== 自检 2/18：并列因子分档与输入顺序无关 ===")
    print(f"  原始输入 低−高={a['low_minus_high']*100:+.3f}%  |  "
          f"打乱输入 低−高={b['low_minus_high']*100:+.3f}%")
    if abs(a["low_minus_high"] - b["low_minus_high"]) > 1e-9:
        print("  ✗ 结果随输入顺序变化"); ok = False
    else:
        print("  ✓ 通过")

    # ---------- 3. 跳空入场不得被删除
    gap_dir = os.path.join(out_dir, "gap")
    os.makedirs(gap_dir, exist_ok=True)
    rows = []
    for i in range(45):                                    # 需要 1+hold 根，否则窗口不足会跳过
        px = 10.0 * (1 + 0.001 * i)
        rows.append((f"D{i:04d}", px, px * 1.01, px * 0.99, px, 1e6))
    rows[20] = (rows[20][0], 8.0, 8.1, 7.9, 8.0, 1e6)      # 急跌，次日开盘落在止损线下方
    _write(os.path.join(gap_dir, "G001.csv"),
           ["date", "open", "high", "low", "close", "volume"], rows)
    with open(os.path.join(out_dir, "gap_signals.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows([("date", "code"), ("D0019", "G001")])
    txt = _quiet_sim(gap_dir, os.path.join(out_dir, "gap_signals.csv"))
    print("\n=== 自检 3/18：跳空开盘低于止损位时保留该笔 ===")
    if "跳空开盘已在止损线下仍计入：1" not in txt:
        print(f"  ✗ 该笔被删除或未标记\n{txt}"); ok = False
    else:
        print("  ✓ 通过（保留，并在开盘价离场）")

    # ---------- 4. 假阳性率
    print("\n=== 自检 4/18：纯噪声下的假阳性率（400 次独立抽样）===")
    fp, N = 0, 400
    for k in range(N):
        r = random.Random(1000 + k)
        rows = []
        for di in range(80, 280):
            d = days[di]
            for c in codes:
                rows.append((d, c, r.gauss(0, 1)))
        if abs(ic_of(rows).t_hac) >= 2.6:
            fp += 1
    rate = fp / N
    print(f"  |t_HAC| ≥ 2.6 的比例：{rate*100:.1f}%（{fp}/{N}）")
    if rate > 0.04:
        print("  ✗ 假阳性率过高，t 值未校准（验收线 4%，名义 1%）"); ok = False
    else:
        print("  ✓ 通过（<=4%；|t|≥2.6 的名义水平约 1%）")

    # ---------- 5. HAC-OLS 在独立数据上应退化为普通 t
    print("\n=== 自检 5/18：HAC-OLS 在独立数据上不放大显著性 ===")
    ratios = []
    for k, (n_i, pp) in enumerate(((2000, 0.5), (2000, 0.2), (800, 0.5))):
        r = random.Random(31 + k)
        y = [r.gauss(0, 1) for _ in range(n_i)]
        g = [1.0 if r.random() < pp else 0.0 for _ in range(n_i)]
        aa = [v for v, x in zip(y, g) if x > 0]
        bb = [v for v, x in zip(y, g) if x == 0]
        m = st.mean(aa) - st.mean(bb)
        se = (st.pstdev(aa) ** 2 / len(aa) + st.pstdev(bb) ** 2 / len(bb)) ** 0.5
        _, th_ = hac_ols_diff(y, g, lag=20)
        ratio = th_ / (m / se) if se else 0.0
        ratios.append(ratio)
        print(f"  n={n_i} p={pp}: 朴素 t={m/se:+.2f}  HAC t={th_:+.2f}  比值={ratio:+.2f}")
    if not all(0.7 <= abs(r) <= 1.35 for r in ratios):
        print("  ✗ HAC 尺度偏离普通 t 太多（独立数据上应接近 1.0）"); ok = False
    else:
        print("  ✓ 通过（比值接近 1）")

    # ---------- 6. 全并列：空档必须判为不可估，不能写成 0%
    print("\n=== 自检 6/18：全并列分档返回不可估，而不是 0% ===")
    tie2 = [(f"D{i:04d}", f"S{j:03d}", 1.0, {5: 0.01 * j}) for i in range(20) for j in range(30)]
    qs = fl.quantile_spread(tie2, 5)
    empty = sum(1 for w in qs for x in w["excess"] if x is None)
    summ = fl.spread_summary(qs)
    print(f"  空档位计数={empty}  spread_summary={'None（不可估）' if summ is None else summ['low_minus_high']}")
    if summ is not None:
        print("  ✗ 空档被当成有效结果"); ok = False
    else:
        print("  ✓ 通过")

    # ---------- 7. 常数关系：IC 零方差，t 不可报
    print("\n=== 自检 7/18：IC 序列零方差时不给 t 值 ===")
    rows_c = [(f"D{i:04d}", f"S{j:03d}", float(j), {5: 0.01 * j}) for i in range(6) for j in range(30)]
    sc = fl.ic_summary(fl.daily_ic(rows_c, 5), 5)
    print(f"  IC={sc.mean:+.4f}  degenerate={sc.degenerate}  t={sc.t:+.2f}  t_HAC={sc.t_hac:+.2f}")
    if not sc.degenerate or abs(sc.t) > 1e6:
        print("  ✗ 零方差下仍在报 t 值"); ok = False
    else:
        print("  ✓ 通过")

    # ---------- 8. 成本方向：正向因子不得被判成不可实施
    print("\n=== 自检 8/18：扣成本按数据方向，不预设做多低档 ===")
    pos = [(f"2026-01-{i+1:02d}", f"S{j:03d}", float(j),
            {5: 0.02 if j >= 80 else (-0.02 if j < 20 else 0.0)})
           for i in range(30) for j in range(100)]
    md_pos = fl.report(pos, horizons=(5,), cost_pct=0.20)
    md_neg = fl.report([(d, c, -f, fw) for d, c, f, fw in pos], horizons=(5,), cost_pct=0.20)
    l_pos = [x for x in md_pos.splitlines() if "多高档／空低档" in x]
    l_neg = [x for x in md_neg.splitlines() if "多低档／空高档" in x]
    print("  正向因子：" + (l_pos[0] if l_pos else "?"))
    print("  反向因子：" + (l_neg[0] if l_neg else "?"))
    if "不可实施（L3）" in md_pos or "不可实施（L3）" in md_neg:
        print("  ✗ 有明确价差的因子被判成不可实施（成本扣错方向）"); ok = False
    elif "多高档／空低档" not in md_pos or "多低档／空高档" not in md_neg:
        print("  ✗ 方向标错"); ok = False
    else:
        print("  ✓ 通过（价差 4%，扣 0.20% 后仍为正，方向随数据翻转）")

    # ---------- 9. 持有期重叠：t 必须做重叠校正
    print("\n=== 自检 9/18：持有期重叠时 t 的校准（纯噪声，名义 5%）===")
    H9, R9, D9, N9 = 20, 120, 200, 40
    fp_cluster = fp_overlap = 0
    for k in range(R9):
        r = random.Random(50000 + k)
        shocks = [r.gauss(0, 1.0) for _ in range(D9 + 2 * H9)]
        vals, dts = [], []
        for di in range(D9):
            mkt = sum(shocks[di:di + H9])
            for _ in range(N9):
                vals.append(mkt + r.gauss(0, 1.0))
                dts.append(f"D{di:04d}")
        fp_cluster += abs(cluster_t(vals, dts)) >= 1.96
        ov = overlap_t(vals, dts, H9)
        fp_overlap += abs(ov["t"]) >= ov["crit"]
    rc, ro = fp_cluster / R9, fp_overlap / R9
    print(f"  日期聚类  假阳性 {rc*100:5.1f}%（只处理同日相关，处理不了跨日重叠）")
    print(f"  重叠校正  假阳性 {ro*100:5.1f}%（按日等权 + 平坦权 HAC + t 临界值）")
    if rc < 0.20:
        print("  ✗ 没复现出重叠导致的假阳性，这项自检失去意义"); ok = False
    elif ro > 0.10:
        print("  ✗ 重叠校正后假阳性仍偏高"); ok = False
    else:
        print("  ✓ 通过（问题被复现，且已压到 10% 以内）")

    # ---------- 10. 口径一致：ic 的 h 日收益 == sim 的 A 收益
    print("\n=== 自检 10/18：ic 与 sim 的买卖口径一致 ===")
    cdir = os.path.join(out_dir, "cal")
    os.makedirs(cdir, exist_ok=True)
    px0 = 10.0
    crow = []
    for i in range(60):
        px0 *= 1.004 + 0.002 * math.sin(i)
        o = px0 * 0.998
        crow.append((f"2026-05-{i+1:02d}", o, px0 * 1.01, o * 0.99, px0, 1e6))
    _write(os.path.join(cdir, "K001.csv"),
           ["date", "open", "high", "low", "close", "volume"], crow)
    csig = os.path.join(out_dir, "cal_signals.csv")
    with open(csig, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows([("date", "code"), (crow[10][0], "K001")])
    cbars = load_bars(os.path.join(cdir, "K001.csv"))
    H10 = 20
    r_ic = fl.build_rows({crow[10][0]: {"K001": 1.0}},
                         {"K001": {b.date: b.close for b in cbars}}, horizons=(H10,))[0][3][H10] * 100
    txt10 = _quiet_sim(cdir, csig, hold=H10, entry="close")
    row_a = [l for l in txt10.splitlines() if l.startswith("| A 不动")]
    r_sim = float(row_a[0].split("|")[2].strip().replace("%", "").replace("+", "")) if row_a else float("nan")
    print(f"  ic  口径 {r_ic:+.3f}%   sim 口径 {r_sim:+.3f}%   差 {r_sim-r_ic:+.3f}pp")
    if not (abs(r_sim - r_ic) <= 0.0011):
        print("  ✗ 两个命令的买卖口径不一致，跨关比较没有意义"); ok = False
    elif "收益口径一致" not in txt10:
        print("  ✗ sim 没有打印口径"); ok = False
    else:
        print(f"  ✓ 通过（T+1 收盘买 → T+{1+H10} 收盘卖，两边一致）")

    # ---------- 11. 无效截面不得记成 IC=0
    print("\n=== 自检 11/18：无定义的截面被剔除，而不是记成 IC=0 ===")
    bad11 = [(f"D{i:04d}", f"S{j:03d}", 1.0, {5: 0.01 * j}) for i in range(10) for j in range(30)]
    s11, dg11 = fl.daily_ic_diag(bad11, 5)
    print(f"  10 个全同分截面 → 有效 IC 条数 {len(s11)}，剔除（因子全同分）{dg11['tie_factor']}")
    if s11 or dg11["tie_factor"] != 10:
        print("  ✗ 无效截面被当成 IC=0 计入样本"); ok = False
    else:
        print("  ✓ 通过")

    # ---------- 12. 非法持有期在入口被拒（不是悄悄算回看收益）
    print("\n=== 自检 12/18：负数/零/重复/空持有期在入口被拒 ===")
    def _rejected(fn, *a, **kw):
        try:
            fn(*a, **kw)
        except (SystemExit, ValueError, argparse.ArgumentTypeError):
            return True
        return False

    cases = 0
    for bad_h in ("-5", "0", "5,5", "", "1,x"):
        cases += _rejected(_parse_horizons, bad_h)
    cases += _rejected(fl.build_rows, {"D0000": {"S000": 1.0}}, close_by_code, horizons=(-5,))
    cases += _rejected(fl.build_rows, {"D0000": {"S000": 1.0}}, close_by_code, horizons=(5, 5))
    tiny = [(days[100], "S000", 1.0), (days[101], "S001", 2.0)]
    tiny = [(d, c, f, {5: 0.01 * i}) for i, (d, c, f) in enumerate(tiny)]
    cases += _rejected(fl.report, tiny, horizons=(0,))
    print(f"  8 种非法输入被拒 {cases} 次（应为 8）")
    if cases != 8:
        print("  ✗ 有非法持有期被接受——会静默算出回看收益"); ok = False
    else:
        print("  ✓ 通过")

    # ---------- 13. 坏行情在入口被拒（不是算完再报错）
    print("\n=== 自检 13/18：负成交量 / 空日期 在读数时被拒 ===")
    bad_csv = os.path.join(out_dir, "bad_bars.csv")
    hdr = ["date", "open", "high", "low", "close", "volume"]
    _write(bad_csv, hdr, [("D0000", 10, 10.5, 9.5, 10, 1e6), ("D0001", 10, 10.5, 9.5, 10, -1e6)])
    r_neg = _rejected(load_bars, bad_csv)
    _write(bad_csv, hdr, [("D0000", 10, 10.5, 9.5, 10, 1e6), ("", 10, 10.5, 9.5, 10, 1e6)])
    r_empty = _rejected(load_bars, bad_csv)
    _write(bad_csv, hdr, [("D0000", 10, 10.5, 9.5, 10, 1e6), ("D0001", 10, 10.5, 9.5, 10, 1e6)])
    good_ok = len(load_bars(bad_csv)) == 2
    print(f"  负成交量被拒={r_neg}  空日期被拒={r_empty}  正常两行仍可读={good_ok}")
    if not (r_neg and r_empty and good_ok):
        print("  ✗ 坏行情没在入口被拒，或正常数据被误杀"); ok = False
    else:
        print("  ✓ 通过")

    # ---------- 14. rolling_ic 零方差返回不可估（不爆 1e8 假 t）且 spread_summary 共用 hac_t_overlap
    print("\n=== 自检 14/18：rolling_ic 零方差返回不可估 & 分档 t_HAC 共用重叠校正 ===")
    const_ic = [(f"2026-01-{i:02d}", 0.05, 50) for i in range(1, 25)]
    segs14 = fl.rolling_ic(const_ic, 4)
    deg_ok = bool(segs14) and all(s["t"] is None for s in segs14)
    pos14 = [(f"2026-01-{i+1:02d}", f"S{j:03d}", float(j),
              {5: (0.02 + 0.002 * (i % 5)) if j >= 80 else (-0.02 if j < 20 else 0.0)})
             for i in range(30) for j in range(100)]
    qs14 = fl.quantile_spread(pos14, 5, 5)
    ss14 = fl.spread_summary(qs14, hac_lags=5)
    top14 = [w["excess"][-1] - w["excess"][0] for w in qs14 if all(x is not None for x in w["excess"])]
    ov14 = fl.hac_t_overlap(top14, 4)[0]
    same_hac = ss14 is not None and ss14["t_hac"] is not None and abs(ss14["t_hac"] - ov14) < 1e-12
    print(f"  常数分段 t={segs14[0]['t']}（应为 None）  spread t_HAC 与 hac_t_overlap 一致={same_hac}")
    if not (deg_ok and same_hac):
        print("  ✗ rolling_ic 仍用 1e-9 兜底或 spread_summary 未统一到 hac_t_overlap"); ok = False
    else:
        print("  ✓ 通过")

    # ---------- 15. db_lab / pool_lab 入口校验与参数化防注入
    print("\n=== 自检 15/18：db_lab 与 pool_lab 入口拒绝非法持有期/非法标识符 ===")
    import db_lab as dbl
    import pool_lab as plb
    import sqlite3 as _sq
    tdb = os.path.join(out_dir, "tiny_audit.db")
    if os.path.exists(tdb):
        os.remove(tdb)
    con15 = _sq.connect(tdb)
    con15.execute("CREATE TABLE rows (date TEXT, sym TEXT, score REAL, cat TEXT, fwd5 REAL)")
    con15.executemany("INSERT INTO rows VALUES (?,?,?,?,?)",
                      [(f"2026-01-{d:02d}", f"S{s:02d}", float(s), "A" if s % 2 else "B", 0.001 * s)
                       for d in range(1, 15) for s in range(1, 25)])
    con15.commit()
    con15.close()
    db_rej = sum([
        _rejected(lambda: list(dbl.iter_sections(tdb, (-5,)))),
        _rejected(lambda: list(dbl.iter_sections(tdb, (5, 5)))),
        _rejected(lambda: list(dbl.iter_sections(tdb, (5,), table="rows; DROP TABLE rows"))),
        _rejected(plb.run, tdb, -5, (5,), 0.002, "cat", (), "rows", 10),
        _rejected(plb.run, tdb, 5, (0,), 0.002, "cat", (), "rows", 10),
        _rejected(plb.run, tdb, 5, (5,), -0.01, "cat", (), "rows", 10),
    ])
    # 含单引号的 cat 参数应安全返回空而非抛 SQL 语法错误
    safe_cat = list(dbl.iter_sections(tdb, (5,), cat="A' OR '1'='1"))
    print(f"  6 种非法调用被拒 {db_rej} 次（应为 6）  恶意单引号注入返回条数={len(safe_cat)}（应为 0）")
    if db_rej != 6 or len(safe_cat) != 0:
        print("  ✗ db_lab / pool_lab 入口校验或 SQL 参数化存在缺口"); ok = False
    else:
        print("  ✓ 通过")

    # ---------- 16. A 股可交易性硬过滤：T+1 停牌/一字涨停买不进，一字跌停止损顺延离场
    print("\n=== 自检 16/18：A 股 T+1 停牌/一字板不可买入 & 一字跌停止损顺延离场 ===")
    lim_dir = os.path.join(out_dir, "lim")
    os.makedirs(lim_dir, exist_ok=True)
    lim_rows = []
    for i in range(45):
        px = 10.0
        lim_rows.append([f"D{i:04d}", px, px * 1.01, px * 0.99, px, 1e6])
    # D0005 信号 -> D0006 为 T+1 入场日：设为一字涨停（high==low==11.0），应被拦截不可买
    lim_rows[6] = ["D0006", 11.0, 11.0, 11.0, 11.0, 5e5]
    # D0015 信号 -> D0016 正常 10.0 入场，D0017 一字跌停（9.0, high==low, 低于止损），D0018 开板 8.2 开盘离场
    lim_rows[17] = ["D0017", 9.0, 9.0, 9.0, 9.0, 2e5]
    lim_rows[18] = ["D0018", 8.2, 8.5, 8.0, 8.3, 1e6]
    _write(os.path.join(lim_dir, "L001.csv"), ["date", "open", "high", "low", "close", "volume"], lim_rows)
    lim_sig = os.path.join(out_dir, "lim_signals.csv")
    with open(lim_sig, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows([("date", "code"), ("D0005", "L001"), ("D0015", "L001")])
    txt16 = _quiet_sim(lim_dir, lim_sig, hold=20, entry="close")
    b_line16 = [l for l in txt16.splitlines() if "B 仅止损" in l]
    # D0016 入场价 10.0，D0017 一字跌停卖不出，顺延到 D0018 开盘 8.2 离场 -> -18.000%
    deferred_ok = ("T+1停牌/一字板不可买：1" in txt16 and "一字跌停止损顺延：1" in txt16
                   and b_line16 and "-18.000%" in b_line16[0])
    # yearly 必须与 ic 同口径：历史上这里漏接过滤，同一份数据会算出两个 IC
    lim_factor = os.path.join(out_dir, "lim_factor.csv")
    with open(lim_factor, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows([("date", "code", "value"), ("D0005", "L001", 1.0), ("D0015", "L001", 2.0)])
    txt16y = _quiet_yearly(lim_dir, lim_factor, horizon=20)
    yearly_ok = "不可成交剔除：T+1 停牌或一字涨跌停拦截 1 条" in txt16y
    print(f"  一字板拦截与跌停顺延验证={deferred_ok}  yearly 同口径={yearly_ok}")
    if not (deferred_ok and yearly_ok):
        print(f"  ✗ 一字板未被拦截或 yearly 未接同一口径过滤\n{txt16}\n--- yearly ---\n{txt16y}"); ok = False
    else:
        print("  ✓ 通过（T+1 一字涨停被拒、一字跌停止损顺延至开板日 8.2 离场、yearly 同口径）")

    # ---------- 17. db_lab CLI 与 Walk-Forward 多重检验门槛打通
    print("\n=== 自检 17/18：db_lab CLI 与 Walk-Forward 多重检验门槛生效 ===")
    wf_db = os.path.join(out_dir, "wf_audit.db")
    if os.path.exists(wf_db):
        os.remove(wf_db)
    con17 = _sq.connect(wf_db)
    con17.execute("CREATE TABLE rows (date TEXT, sym TEXT, score REAL, cat TEXT, fwd5 REAL)")
    wf_data = []
    for y in (2021, 2022, 2023):
        for d in range(1, 25):
            for s in range(1, 25):
                wf_data.append((f"{y}-01-{d:02d}", f"S{s:02d}", float(s), "A", 0.002 * s))
    con17.executemany("INSERT INTO rows VALUES (?,?,?,?,?)", wf_data)
    con17.commit()
    con17.close()
    wf1 = dbl.walkforward(wf_db, horizons=(5,), first_test_year=2022, trials=1)
    wf20 = dbl.walkforward(wf_db, horizons=(5,), first_test_year=2022, trials=20)
    wf_ok = bool(wf1 and wf20 and wf20[0]["threshold"] > wf1[0]["threshold"])
    import contextlib, io
    buf17 = io.StringIO()
    with contextlib.redirect_stdout(buf17):
        rc17 = dbl.main(["--db", wf_db, "--horizons", "5", "--trials", "4", "--walkforward"])
    cli_ok = (rc17 == 0 and "Walk-Forward" in buf17.getvalue())
    print(f"  Walk-Forward 门槛随 trials 抬升={wf_ok}  db_lab CLI 退出码={rc17}")
    if not (wf_ok and cli_ok):
        print("  ✗ db_lab walkforward trials 形参未生效或 CLI 异常"); ok = False
    else:
        print("  ✓ 通过")

    # ---------- 18. 多重检验账本（.verdict_trials.json）自动累计并防低报
    print("\n=== 自检 18/18：多重检验账本自动累计尝试次数并纠偏低报 ===")
    ledger_target = os.path.join(out_dir, "dummy_factor.csv")
    open(ledger_target, "w", encoding="utf-8").write("date,code,value\n")
    ledger_json = os.path.join(out_dir, ".verdict_trials.json")
    if os.path.exists(ledger_json):
        os.remove(ledger_json)
    e1, n1 = _track_trials(ledger_target, "param_v1", 1)
    e2, n2 = _track_trials(ledger_target, "param_v2", 1)
    e3, n3 = _track_trials(ledger_target, "param_v3", 1)
    ledger_ok = (e1 == 1 and e2 == 2 and e3 == 3 and "自动校正" in n3)
    print(f"  三次不同参数运行后手填 --trials 1 实得有效次数={e3}（应为 3）  告警触发={bool(n3)}")
    if not ledger_ok:
        print("  ✗ 多重检验账本未自动累计或未纠偏低报"); ok = False
    else:
        print("  ✓ 通过")

    print("\n" + ("全部通过。" if ok else "有失败项，先修工具再用它下结论。"))
    return 0 if ok else 1


def _write(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow([f"{x:.6g}" if isinstance(x, float) else x for x in r])


def _ic_report(fl, rows, close_by_code):
    fb = defaultdict(dict)
    for d, c, v in rows:
        fb[d][c] = v
    r = fl.build_rows(dict(fb), close_by_code, horizons=(5,))
    return fl.report(r, horizons=(5,))


def _quiet_sim(bars_dir, signals, hold=20, entry="close"):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_sim(argparse.Namespace(bars=bars_dir, signals=signals, hold=hold, entry=entry))
    return buf.getvalue()


def _quiet_yearly(bars_dir, factor, horizon=20, trials=1):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_yearly(argparse.Namespace(bars=bars_dir, factor=factor, horizon=horizon, trials=trials))
    return buf.getvalue()


# ============================================================ CLI
def main():
    ap = argparse.ArgumentParser(description="vkit — 否决器工具箱")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ic", help="单因子截面检验")
    p.add_argument("--bars", required=True, help="每只一个 csv 的目录")
    p.add_argument("--factor", required=True, help="date,code,value")
    p.add_argument("--horizons", default="1,5,10,20", help="持有期（交易日），逗号分隔的正整数")
    p.add_argument("--trials", type=_positive_int, default=1, help="一共试了几次（Bonferroni）")
    p.add_argument("--slices", type=_positive_int, default=4)
    p.add_argument("--cost", type=_nonnegative_float, default=0.0,
                   help="双边成本（百分数，如 0.20）；填了会输出扣成本后的首尾差")
    p.add_argument("--out")
    p.set_defaults(func=cmd_ic)

    p = sub.add_parser("sim", help="组合模拟：同一批入场比 7 种离场")
    p.add_argument("--bars", required=True)
    p.add_argument("--signals", required=True, help="date,code")
    p.add_argument("--hold", type=_positive_int, default=20)
    p.add_argument("--entry", choices=("close", "open"), default="close",
                   help="T+1 的成交价：close=收盘（与 ic 同口径，默认），open=开盘（更实际但比 ic 多一段隔夜）")
    p.set_defaults(func=cmd_sim)

    p = sub.add_parser("yearly", help="逐年 IC 与分档（关4 / 样本外切分）")
    p.add_argument("--bars", required=True)
    p.add_argument("--factor", required=True)
    p.add_argument("--horizon", type=_positive_int, default=5)
    p.add_argument("--trials", type=_positive_int, default=4)
    p.set_defaults(func=cmd_yearly)

    p = sub.add_parser("conc", help="集中度检验")
    p.add_argument("--rows", required=True, help="date,value")
    p.set_defaults(func=cmd_conc)

    p = sub.add_parser("timing", help="择时检验")
    p.add_argument("--signals", required=True)
    p.add_argument("--index", required=True, help="date,close")
    p.add_argument("--hold", type=_positive_int, default=20)
    p.set_defaults(func=cmd_timing)

    p = sub.add_parser("db", help="大样本审计库流式检验与 Walk-Forward 外推")
    p.add_argument("--db", required=True, help="SQLite 审计库路径")
    p.add_argument("--table", default="rows")
    p.add_argument("--horizons", default="1,5,10,20")
    p.add_argument("--trials", type=_positive_int, default=1)
    p.add_argument("--cat", default="")
    p.add_argument("--lo-score", type=float, default=None)
    p.add_argument("--hi-score", type=float, default=None)
    p.add_argument("--q", type=_positive_int, default=10)
    p.add_argument("--slices", type=_positive_int, default=4)
    p.add_argument("--walkforward", action="store_true")
    p.add_argument("--first-test-year", type=_positive_int, default=2022)
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_db)

    p = sub.add_parser("selftest", help="用合成数据自检")
    p.add_argument("--out", default=os.path.join(os.getcwd(), "vkit_selftest"))
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()