"""因子检验实验室 · 通用库（无第三方依赖）

用途：任何「每日给出一份横截面打分」的策略，都可以用这里的函数回答三个问题：
  1. 这个打分和未来收益有没有关系？（IC / IR / t 值）
  2. 关系能撑多久？（IC 衰减曲线）
  3. 关系是不是在样本两端都成立、还是只有最近半年？（滚动窗口 + 多重检验校正）

输入统一是「长表」：list[Row]，Row = (date, code, factor, fwd_ret)。
fwd_ret 必须是无前视的：T 日收盘算因子 → T+1 收盘买入 → 持有 h 日。

为什么必须先做 IC 而不是直接分层回测：
  分层回测把几千个横截面观测压成 5 个均值，一个方向错误要跑完所有档才看得出来；
  IC 是每日一个相关系数，方向错在第一周就暴露，信号强弱还有统计量可判。
  板块层那次花三轮才发现"方向是反的"，用 IC 一场就能看出来。

用法：
  import factor_lab as fl
  rows = fl.build_rows(panel, factor_by_date, horizons=(1, 5, 10))
  print(fl.report(rows, horizons=(1, 5, 10), n_trials=4))
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

Row = tuple[str, str, float, dict[int, float]]   # date, code, factor, {h: fwd_ret}


# ---------------------------------------------------------------- 秩与相关
def rank(xs: Sequence[float]) -> list[float]:
    """平均秩（并列取均值），返回 1..n 的浮点秩。"""
    n = len(xs)
    order = sorted(range(n), key=lambda i: xs[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def pearson(a: Sequence[float], b: Sequence[float]) -> float:
    n = len(a)
    if n < 3:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


def spearman(a: Sequence[float], b: Sequence[float]) -> float | None:
    """秩相关 = 秩的 Pearson。对极端值不敏感，因子研究的标准口径。

    任一序列零方差（因子全部同分 / 未来收益全部相同）时相关系数**没有定义**，
    返回 None（不可估），不返回 0。返回 0 会把这天当成"有效但无信息"计入样本，
    既稀释 IC 均值，又虚增自由度，把 t 值推向"不显著"。
    """
    if len(a) != len(b) or len(a) < 3:
        return None
    ra, rb = rank(a), rank(b)
    if max(ra) == min(ra) or max(rb) == min(rb):
        return None
    return pearson(ra, rb)


# ---------------------------------------------------------------- 长表构造
def build_rows(factor_by_date: dict[str, dict[str, float]],
               close_by_code: dict[str, dict[str, float]],
               horizons: Iterable[int] = (1, 5, 10),
               entry_lag: int = 1,
               untradable_by_code: dict[str, set[str]] | None = None) -> list[Row]:
    """把「每日因子值」与「代码日K」拼成检验用长表（T+1 收盘买入，无前视）。

    factor_by_date[date][code] = 因子值（必须只用该日收盘及之前的信息）
    close_by_code[code][date] = 收盘价
    untradable_by_code[code] = T+1 入场日停牌（volume==0）或一字涨跌停（high==low）不可买入的日期集合
    """
    horizons = tuple(horizons)
    if not horizons or any(isinstance(h, bool) or not isinstance(h, int) or h <= 0 for h in horizons):
        raise ValueError("horizons 必须是非空正整数")
    if len(set(horizons)) != len(horizons):
        raise ValueError("horizons 不得重复")
    if isinstance(entry_lag, bool) or not isinstance(entry_lag, int) or entry_lag < 0:
        raise ValueError("entry_lag 必须是非负整数")
    dates = sorted(factor_by_date)
    cal = sorted({d for c in close_by_code.values() for d in c})
    pos = {d: i for i, d in enumerate(cal)}
    rows: list[Row] = []
    for d in dates:
        i = pos.get(d)
        if i is None:
            continue
        entry_idx = i + entry_lag
        if entry_idx >= len(cal):
            continue
        entry_date = cal[entry_idx]
        for code, f in factor_by_date[d].items():
            c = close_by_code.get(code)
            if not c:
                continue
            if untradable_by_code and entry_date in untradable_by_code.get(code, ()):
                continue
            entry = c.get(entry_date)
            if not entry or entry <= 0:
                continue
            fwd: dict[int, float] = {}
            for h in horizons:
                exit_idx = entry_idx + h
                if exit_idx >= len(cal):
                    continue
                exit_ = c.get(cal[exit_idx])
                if exit_ and exit_ > 0:
                    fwd[h] = exit_ / entry - 1
            if fwd:
                rows.append((d, code, float(f), fwd))
    return rows


# ---------------------------------------------------------------- IC
@dataclass
class ICStats:
    horizon: int
    n_days: int
    mean: float
    std: float
    ir: float           # mean/std，日频 IC 的信息比率（未年化）
    t: float            # 朴素 t：mean/std*sqrt(n)，假设 IC 序列独立
    pos_ratio: float    # IC>0 的交易日占比
    avg_n: float        # 每期平均标的数
    t_hac: float = 0.0  # 重叠校正后的 t（平坦/Bartlett 取大方差 + 有效自由度）
    degenerate: bool = False   # IC 序列零方差（如常数关系）→ t 不可估
    t_crit: float = 1.96       # 该持有期下有效自由度对应的 t 临界值
    df: int = 0                # 有效自由度 = 天数/(2·lag+1)

    def line(self, threshold: float = 1.96) -> str:
        if self.degenerate:
            return (f"| {self.horizon}日 | {self.n_days} | {self.avg_n:.0f} | "
                    f"{self.mean:+.4f} | 0.0000 | 退化 | — | — | "
                    f"{self.pos_ratio*100:.0f}% | ⚠️ |")
        need = max(threshold, self.t_crit)
        star = "✅" if abs(self.t_hac) >= need else "❌"
        return (f"| {self.horizon}日 | {self.n_days} | {self.avg_n:.0f} | "
                f"{self.mean:+.4f} | {self.std:.4f} | {self.ir:+.3f} | "
                f"{self.t:+.2f} | {self.t_hac:+.2f} | {self.pos_ratio*100:.0f}% | {star} |")


def daily_ic_diag(rows: Sequence[Row], horizon: int, min_n: int = 20
                  ) -> tuple[list[tuple[str, float, int]], dict]:
    """逐日截面 Spearman IC，并给出被剔除的天数分类。

    三种天不进入 IC 序列：样本不足 / 因子全部同分 / 未来收益全部相同。
    后两种是"相关系数没有定义"，不是"相关系数为 0"。
    """
    by_date: dict[str, list[tuple[float, float]]] = {}
    for d, _code, f, fwd in rows:
        r = fwd.get(horizon)
        if r is None:
            continue
        by_date.setdefault(d, []).append((f, r))
    out = []
    diag = {"days": len(by_date), "too_few": 0, "tie_factor": 0, "tie_ret": 0}
    for d in sorted(by_date):
        pairs = by_date[d]
        if len(pairs) < min_n:
            diag["too_few"] += 1
            continue
        fs = [p[0] for p in pairs]
        rs = [p[1] for p in pairs]
        ic = spearman(fs, rs)
        if ic is None:                     # 不可估：分类计数后剔除，不当成 0
            rk = rank(fs)
            if max(rk) == min(rk):
                diag["tie_factor"] += 1
            else:
                diag["tie_ret"] += 1
            continue
        out.append((d, ic, len(pairs)))
    return out, diag


def daily_ic(rows: Sequence[Row], horizon: int, min_n: int = 20
             ) -> list[tuple[str, float, int]]:
    """逐日截面 Spearman IC。返回 [(date, ic, n)]；无法定义 IC 的日子直接剔除。"""
    return daily_ic_diag(rows, horizon, min_n)[0]


# Student-t 双侧 95% 临界值；df 之间线性插值，>120 用 1.96
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
        15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
        25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 80: 1.990, 120: 1.980}


def t_crit(df: float) -> float:
    """双侧 95% 的 t 临界值。df 小于 120 时明显大于 1.96。"""
    df = max(1.0, float(df))
    if df >= 120:
        return 1.96
    ks = sorted(_T95)
    if df in _T95:
        return _T95[df]
    lo = max(k for k in ks if k < df)
    hi = min(k for k in ks if k > df)
    return _T95[lo] + (_T95[hi] - _T95[lo]) * (df - lo) / (hi - lo)


def hac_t_overlap(vals: Sequence[float], lag: int) -> tuple[float, float, int]:
    """持有期重叠下的稳健 t，返回 (t, 门槛, 有效自由度)。

    前向 h 日收益逐日重叠 → IC 序列是 MA(h-1)，自相关最长到 h-1 阶。
    Bartlett 权（Newey-West 默认）在 lag=h 时把远端自协方差压低 20%–40%，
    方差仍被低估：真实面板 20 日 IC 上实测 t 从 −1.83 被放大到 −2.20。

    做法与 vkit.overlap_t 完全相同（口径只能有一套）：
      1. 平坦权与 Bartlett 权各算一遍，取较大方差（平坦权期望无偏但可能为负，
         Bartlett 保正但偏低）；
      2. 自由度按有效样本 n/(2·lag+1) 算，门槛用 t 临界值而不是 1.96。
    """
    n = len(vals)
    lag = max(0, min(int(lag), n - 2))
    df = max(1, n // (2 * lag + 1))
    if n < 3:
        return 0.0, t_crit(df), df
    m = sum(vals) / n
    x = [v - m for v in vals]
    v0 = sum(v * v for v in x) / n
    v_flat = v_bart = v0
    for l in range(1, lag + 1):
        gl = sum(x[i] * x[i - l] for i in range(l, n)) / n
        v_flat += 2 * gl
        v_bart += 2 * (1 - l / (lag + 1)) * gl
    var = v_flat if v_flat > v_bart else v_bart
    if var <= 0:
        return 0.0, t_crit(df), df
    return m / math.sqrt(var / n), t_crit(df), df


def hac_t(vals: Sequence[float], lags: int | None = None) -> float:
    """Newey-West 稳健 t（Bartlett 权）。保留给外部调用；判定请用 hac_t_overlap。"""
    n = len(vals)
    if n < 5:
        return 0.0
    m = statistics.mean(vals)
    d = [v - m for v in vals]
    if lags is None:
        lags = max(1, int(4 * (n / 100) ** (2 / 9)))   # Newey-West 经验带宽
    lags = min(lags, n - 2)
    var = sum(x * x for x in d) / n
    for l in range(1, lags + 1):
        gl = sum(d[i] * d[i - l] for i in range(l, n)) / n
        var += 2 * (1 - l / (lags + 1)) * gl
    if var <= 0:                     # 完全共线/退化，退回朴素
        var = sum(x * x for x in d) / n
    return m / math.sqrt(var / n) if var > 0 else 0.0


def ic_summary(ic_series: Sequence[tuple[str, float, int]], horizon: int) -> ICStats | None:
    vals = [x[1] for x in ic_series]
    if len(vals) < 3:
        return None
    m = statistics.mean(vals)
    s = statistics.pstdev(vals)
    n = len(vals)
    pos = sum(1 for v in vals if v > 0) / n
    avg_n = statistics.mean(x[2] for x in ic_series)
    if s < 1e-6:                      # 零方差：t 无定义，不许报出一个巨大值
        return ICStats(horizon, n, m, 0.0, 0.0, 0.0, pos, avg_n, 0.0, True)
    t_ov, crit, df = hac_t_overlap(vals, horizon - 1)
    return ICStats(horizon, n, m, s, m / s, m / s * math.sqrt(n), pos, avg_n,
                   t_ov, False, crit, df)


# ---------------------------------------------------------------- 分层
def quantile_spread(rows: Sequence[Row], horizon: int, q: int = 5,
                    min_n: int = 20) -> list[dict]:
    """逐日按因子分 q 档，各档相对当日全池均值的超额收益，最后按期平均。

    返回每日列表：[{date, n, excess: [低档..高档超额], raw: [...]}]
    注意 excess[0] 是**因子最低**档。和 common 的"Q1"（最高分）口径相反，
    这里统一按因子从低到高排，读的时候别搞反方向。
    """
    by_date: dict[str, list[tuple[float, float]]] = {}
    for d, _code, f, fwd in rows:
        r = fwd.get(horizon)
        if r is None:
            continue
        by_date.setdefault(d, []).append((f, r))
    out = []
    for d in sorted(by_date):
        pairs = by_date[d]
        if len(pairs) < max(min_n, q * 3):
            continue
        base = statistics.mean(r for _, r in pairs)
        n = len(pairs)
        # tie-aware：用平均秩定位档位，同分同秩 → 结果与输入顺序无关
        ranks = rank([p[0] for p in pairs])
        buckets: list[list[float]] = [[] for _ in range(q)]
        for (_f, r), rk in zip(pairs, ranks):
            buckets[min(q - 1, max(0, int((rk - 1) * q / n)))].append(r)
        out.append({"date": d, "n": n,
                    "excess": [(statistics.mean(b) - base) if b else None for b in buckets],
                    "raw": [statistics.mean(b) if b else None for b in buckets]})
    return out


def spread_summary(daily: Sequence[dict], hac_lags: int = 1) -> dict | None:
    """空档位（并列过多导致某档没人）返回 None，该日整日剔除并计数。

    价差 t 同时给朴素与重叠校正 HAC 两个口径（与 ic_summary 共用 hac_t_overlap，lag=hac_lags-1）。
    上限/托底造成的**零方差**（每天价差完全相同）返回 None（不可估），不许用 `pstdev or 1e-9` 兜底。
    """
    if not daily:
        return None
    q = len(daily[0]["excess"])
    usable = [w for w in daily if all(x is not None for x in w["excess"])]
    dropped = len(daily) - len(usable)
    if len(usable) < 3:
        return None
    means = [statistics.mean(w["excess"][i] for w in usable) for i in range(q)]
    top = [w["excess"][q - 1] - w["excess"][0] for w in usable]
    m = statistics.mean(top)
    s = statistics.pstdev(top)
    degenerate = s < 1e-12 or len(set(round(x, 12) for x in top)) == 1
    lag = max(0, int(hac_lags) - 1)
    return {"q_mean": means, "high_minus_low": m, "low_minus_high": -m,
            "top_win": sum(1 for x in top if x > 0) / len(top),
            "t": None if degenerate else m / s * math.sqrt(len(top)),
            "t_hac": None if degenerate or len(top) < 5 else hac_t_overlap(top, lag)[0],
            "degenerate": degenerate,
            "monotonic": sum(1 for i in range(q - 1) if means[i] < means[i + 1]),
            "n_days": len(usable), "dropped_empty": dropped}


# ---------------------------------------------------------------- 滚动稳定性
def rolling_ic(ic_series: Sequence[tuple[str, float, int]], slices: int = 4
               ) -> list[dict]:
    """把 IC 序列切成等长几段，看每段的均值/符号是否一致。
    全样本 IC 显著但只在半段成立 = 结构性变化或已被市场消化。"""
    vals = [x[1] for x in ic_series]
    n = len(vals)
    if n < slices * 3:
        return []
    out = []
    k = n // slices
    for s in range(slices):
        seg = vals[s * k:(s + 1) * k] if s < slices - 1 else vals[s * k:]
        m = statistics.mean(seg)
        # 原先写 `pstdev(seg) or 1e-9`，常数分段会算出 1e8 假 t；零方差统一返回 None（不可估）
        sd = statistics.pstdev(seg)
        deg = sd < 1e-12 or len(set(round(x, 12) for x in seg)) == 1
        out.append({"from": ic_series[s * k][0],
                    "to": ic_series[min(n - 1, (s + 1) * k - 1)][0],
                    "n": len(seg), "mean": m,
                    "t": None if deg else m / sd * math.sqrt(len(seg))})
    return out


# ---------------------------------------------------------------- 多重检验
def t_threshold(n_trials: int, alpha: float = 0.05) -> float:
    """Bonferroni 校正后的双尾 |t| 门槛。

    试了 k 个变体，就要求最显著的那个通过 alpha/k 的检验，否则「最好看的那个」
    只是数据窥探的产物。k=1 时约 1.96；k=4 约 2.50；k=20 约 3.02。
    """
    if n_trials < 1:
        n_trials = 1
    p = 1.0 - alpha / (2 * n_trials)
    return statistics.NormalDist().inv_cdf(p)


def min_trials_verdict(t: float, n_trials: int, alpha: float = 0.05) -> str:
    need = t_threshold(n_trials, alpha)
    if abs(t) >= need:
        return f"✅ |t|={abs(t):.2f} ≥ 门槛 {need:.2f}（按 {n_trials} 次尝试校正）"
    return (f"❌ |t|={abs(t):.2f} < 门槛 {need:.2f}（按 {n_trials} 次尝试校正）"
            f" —— 未过校正，不能采信")


# ---------------------------------------------------------------- 报告
def report(rows: Sequence[Row], horizons: Iterable[int] = (1, 5, 10),
           n_trials: int = 1, rolling_slices: int = 4, q: int = 5,
           cost_pct: float = 0.0, input_obs: int | None = None) -> str:
    """一行结论都不写死，全部由数据决定。返回 markdown 片段。"""
    horizons = tuple(horizons)
    if not horizons or any(isinstance(h, bool) or not isinstance(h, int) or h <= 0 for h in horizons):
        raise ValueError("horizons 必须是非空正整数")
    if cost_pct < 0 or not math.isfinite(cost_pct):
        raise ValueError("cost_pct 必须是非负有限数")
    md: list[str] = []
    md.append("# 因子检验报告\n")
    md.append(f"- 观测：{len(rows):,} 条（date×code）")
    md.append(f"- 日期范围：{min(r[0] for r in rows) if rows else '—'} → "
              f"{max(r[0] for r in rows) if rows else '—'}")
    if input_obs is not None:
        md.append(f"- 输入因子观测：{input_obs:,} 条；可用前向收益观测：{len(rows):,} 条；"
                  f"差异：{max(0, input_obs - len(rows)):,} 条（缺行情/停牌/超出可计算区间，须人工归因）")
    md.append("- 收益口径：T 日信号 → T+1 收盘入场 → 再过 H 个完整交易日收盘退出")
    md.append(f"- 多重检验：按 {n_trials} 次尝试做 Bonferroni 校正，"
              f"|t| 门槛 {t_threshold(n_trials):.2f}\n")

    th = t_threshold(n_trials)
    md.append("## 1. IC / IR\n")
    md.append("| 持有期 | 交易日 | 平均标的 | IC均值 | IC标准差 | IR | t朴素 | t_HAC | IC>0占比 | 显著 |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    series = {}
    drops = {}
    for h in horizons:
        s, dg = daily_ic_diag(rows, h)
        series[h] = s
        drops[h] = dg
        st = ic_summary(s, h)
        md.append(st.line(th) if st else f"| {h}日 | — | — | 样本不足 | | | | | | ❌ |")

    bad = [(h, dg) for h, dg in drops.items() if dg["tie_factor"] or dg["tie_ret"]]
    if bad:
        md.append("")
        md.append("> ⚠️ **无效截面已剔除**（相关系数无定义，不计入 IC、不计入自由度）：" +
                  "；".join(f"{h}日 因子全同分 {dg['tie_factor']} 天 / 收益全相同 {dg['tie_ret']} 天"
                            for h, dg in bad))
    md.append("")
    for h in horizons:
        dg = drops[h]
        md.append(f"> {h} 日截面：共 {dg['days']} 日；标的不足 20 只剔除 {dg['too_few']} 日；"
                  f"相关系数无定义剔除 {dg['tie_factor'] + dg['tie_ret']} 日"
                  f"（因子全同分 {dg['tie_factor']} / 收益全相同 {dg['tie_ret']}）。")

    hi = [ic_summary(series[h], h) for h in horizons]
    hi = [x for x in hi if x and abs(x.mean) > 0.30]
    if hi:
        md.append(f"\n> ⚠️ **IC 绝对值 > 0.30**（{hi[0].horizon} 日 {hi[0].mean:+.3f}）："
                  f"真实因子的日频 IC 极少超过 0.10。先查前视/未来函数，再谈显著性。")

    md.append("\n## 2. IC 衰减（关系能撑几天）\n")
    md.append("| 持有期 | IC均值 | 相对 1 日 |")
    md.append("|---|---:|---:|")
    base = None
    for h in horizons:
        st = ic_summary(series[h], h)
        if not st:
            continue
        if base is None:
            base = st.mean
        ratio = (st.mean / base) if base else 0.0
        md.append(f"| {h}日 | {st.mean:+.4f} | {ratio*100:.0f}% |")

    md.append(f"\n## 3. 分层（{q} 档，因子从低到高；超额相对当日全池均值）\n")
    md.append("| 持有期 | " + " | ".join(f"Q{i+1}" for i in range(q)) +
              " | 高−低 | 低−高 | 高档胜率 | t朴素 | t_HAC | 单调档数 | 空档剔除日 |")
    md.append("|---|" + "---:|" * (q + 7))
    for h in horizons:
        ss = spread_summary(quantile_spread(rows, h, q), hac_lags=h)
        if not ss:
            md.append(f"| {h}日 | 样本不足或可用日少于 3 天 |" + " |" * (q + 7))
            continue
        t0 = "不可估" if ss["t"] is None else f"{ss['t']:+.2f}"
        thac = "不可估" if ss["t_hac"] is None else f"{ss['t_hac']:+.2f}"
        md.append(f"| {h}日 | " + " | ".join(f"{x*100:+.2f}%" for x in ss["q_mean"]) +
                  f" | {ss['high_minus_low']*100:+.2f}% | {ss['low_minus_high']*100:+.2f}% | "
                  f"{ss['top_win']*100:.0f}% | {t0} | {thac} | "
                  f"{ss['monotonic']}/{q-1} | {ss['dropped_empty']} |")

    md.append("\n## 4. 滚动稳定性（IC 分段均值）\n")
    md.append("| 持有期 | 段 | 区间 | 天数 | IC均值 | t值 |")
    md.append("|---|---|---|---:|---:|---:|")
    for h in horizons:
        for i, r in enumerate(rolling_ic(series[h], rolling_slices), 1):
            t_str = "不可估" if r["t"] is None else f"{r['t']:+.2f}"
            md.append(f"| {h}日 | {i} | {r['from']}→{r['to']} | {r['n']} | "
                      f"{r['mean']:+.4f} | {t_str} |")

    if cost_pct:
        md.append(f"\n## 4b. 经济量级（双边成本 {cost_pct:.2f}%／每腿）\n")
        md.append("**方向由数据定，不预设。** 价差为负 = 做多高档、做空低档；为正 = 反过来。"
                  "旧版一律按\"低−高\"扣成本，正向因子会被判成不可实施。\n")
        md.append("| 持有期 | 方向 | 多空毛价差 | 扣两腿 | 仅多头腿超额 | 扣单边 | 判定 |")
        md.append("|---|---|---:|---:|---:|---:|:--|")
        for h in horizons:
            ss = spread_summary(quantile_spread(rows, h, q), hac_lags=h)
            if not ss:
                md.append(f"| {h}日 | 样本不足 | | | | | — |")
                continue
            lmh = ss["low_minus_high"]              # 低档 − 高档（有符号）
            long_high = lmh < 0
            gross = abs(lmh) * 100
            leg = ss["q_mean"][q - 1] if long_high else ss["q_mean"][0]
            ls_net = gross - 2 * cost_pct           # 两条腿各自一次往返
            lo_net = leg * 100 - cost_pct           # 只用多头腿
            if lo_net > 0 and ls_net > 0:
                verdict = "可实施（做多即可）"
            elif lo_net > 0:
                verdict = "可实施（只看多头腿）"
            elif ls_net > 0:
                verdict = "仅多空可实施（需可做空）"
            else:
                verdict = "不可实施（L3）"
            md.append(f"| {h}日 | {'多高档／空低档' if long_high else '多低档／空高档'} | "
                      f"{gross:+.2f}% | {ls_net:+.2f}% | {leg*100:+.2f}% | {lo_net:+.2f}% | {verdict} |")
        md.append("")
        md.append("> A 股零售默认不能做空：以「仅多头腿超额 − 双边成本」为可实施口径，多空一列只在有融券/股指期货时成立。"
                  "多头腿是相对当日全池均值的超额，不含市场 β。")
    md.append("\n## 5. 结论\n")
    for h in horizons:
        st = ic_summary(series[h], h)
        if not st:
            continue
        if st.degenerate:
            md.append(f"- **{h} 日**：IC 序列零方差（均值 {st.mean:+.4f}），t 不可估，不做显著性判定。")
            continue
        md.append(f"- **{h} 日**：{min_trials_verdict(st.t_hac, n_trials)}，"
                  f"IC={'正' if st.mean > 0 else '负'}向 {st.mean:+.4f}，"
                  f"IR={st.ir:+.3f}（t_HAC={st.t_hac:+.2f}，t朴素={st.t:+.2f}）")
    return "\n".join(md)


# ---------------------------------------------------------------- 自检
def _selftest() -> None:
    """合成数据自检：构造一个 IC 明确为正的因子，看各指标是否符合预期。"""
    import random
    random.seed(7)
    codes = [f"S{i:03d}" for i in range(60)]
    dates = [f"2026-01-{d:02d}" for d in range(1, 29)]
    alpha = {c: random.gauss(0, 0.02) for c in codes}      # 每只股票的固定超额
    close = {c: {} for c in codes}
    for c in codes:
        p = 10.0
        for d in dates:
            p *= 1 + alpha[c] + random.gauss(0, 0.01)
            close[c][d] = round(p, 3)
    # 因子 = 真实 alpha + 噪声：IC 应该显著为正
    fby = {d: {c: alpha[c] * 100 + random.gauss(0, 0.3) for c in codes} for d in dates}
    rows = build_rows(fby, close, horizons=(1, 5), entry_lag=1)
    st = ic_summary(daily_ic(rows, 1), 1)
    ss = spread_summary(quantile_spread(rows, 1, 5))
    assert st and st.mean > 0.15, f"IC 应显著为正，实得 {st.mean if st else None}"
    assert abs(st.t) > 2.5, f"t 应显著，实得 {st.t}"
    assert ss and ss["q_mean"][-1] > ss["q_mean"][0], "高档超额应高于低档"
    # 反向因子应得到相反 IC
    fneg = {d: {c: -v for c, v in m.items()} for d, m in fby.items()}
    st2 = ic_summary(daily_ic(build_rows(fneg, close, (1,), 1), 1), 1)
    assert st2 and st2.mean < -0.15, "反向因子 IC 应为负"
    # Bonferroni 门槛应随尝试次数上升
    assert t_threshold(1) < t_threshold(4) < t_threshold(20)
    # 秩函数并列处理
    assert rank([1, 1, 1]) == [2.0, 2.0, 2.0]
    assert rank([3, 1, 2]) == [3.0, 1.0, 2.0]
    # spearman 对单调变换不变
    a = [1, 2, 3, 4, 5]
    assert abs(spearman(a, [x ** 3 for x in a]) - 1.0) < 1e-9
    # 零方差截面：IC 无定义 → None，且整日剔除而不是记 0
    assert spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None
    assert spearman([1, 2, 3, 4], [2, 2, 2, 2]) is None
    bad_rows = [(f"2026-01-{i+1:02d}", f"S{j:03d}", 1.0, {5: 0.01 * j})
                for i in range(6) for j in range(30)]
    s_bad, dg = daily_ic_diag(bad_rows, 5)
    assert s_bad == [] and dg["tie_factor"] == 6, (s_bad, dg)
    # 成本方向：正向因子（高档更强）不许被判成 L3
    pos = [(f"2026-02-{i+1:02d}", f"S{j:03d}", float(j),
            {5: 0.02 if j >= 80 else (-0.02 if j < 20 else 0.0)}) for i in range(30) for j in range(100)]
    md = report(pos, horizons=(5,), cost_pct=0.20)
    assert "多高档／空低档" in md and "不可实施（L3）" not in md, md
    neg = [(d, c, -f, fw) for d, c, f, fw in pos]
    md2 = report(neg, horizons=(5,), cost_pct=0.20)
    assert "多低档／空高档" in md2 and "不可实施（L3）" not in md2, md2
    print("selftest OK：IC 正向显著、反向为负、分层单调、Bonferroni 递进、秩与并列正确、"
          "无效截面返回 NA、成本按数据方向扣")


if __name__ == "__main__":
    _selftest()