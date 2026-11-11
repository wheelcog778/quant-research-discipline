"""pool_v1 历史基线：2025 年 12 个月度截面，Q/V 池 vs 全主板。

数据源（全部只读公开接口）：
  估值/价格：东财 RPT_VALUEANALYSIS_DET（按交易日，含 CLOSE_PRICE / TOTAL_MARKET_CAP / PE_TTM / PB_MRQ）
  财报：东财 RPT_LICO_FN_CPD（按报告期，含 NOTICE_DATE）
  交易日历：腾讯上证指数日线

收益口径：用总市值比值（拆分中性、含增发摊薄、不含现金分红）。
  不用收盘价比：除权日会假跌。分红缺失对高股息的价值池是负偏，属保守方向。

用法：
  python -B baseline.py --start 2025-01-01 --end 2025-12-31
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "baseline" / "_cache"
TZ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
EM = "https://datacenter-web.eastmoney.com/api/data/v1/get"
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
DEFAULT_PERIODS = ("2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30")
PERIODS = DEFAULT_PERIODS  # 可被 --periods 覆盖（见 main）


def is_main(code: str) -> bool:
    return len(code) == 6 and code.startswith(MAIN_PREFIX)


def trading_days(start: str, end: str) -> list[str]:
    """分段抓取（单次跨度过大会被腾讯 501），去重排序后返回。"""
    out: list[str] = []
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    while s <= e:
        seg_end = min(e, s + timedelta(days=360))
        url = ("https://ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param=sh000001,day,{s.isoformat()},{seg_end.isoformat()},640,qfq")
        data = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20).json()
        node = data["data"]["sh000001"]
        rows = node.get("day") or node.get("qfqday") or []
        out.extend(r[0] for r in rows)
        s = seg_end + timedelta(days=1)
        time.sleep(0.3)
    return sorted(set(out))


def fetch_pages(report: str, filt: str, page_size: int = 2000) -> list[dict]:
    out, page = [], 1
    while True:
        params = {"reportName": report, "columns": "ALL", "filter": filt,
                  "pageNumber": page, "pageSize": page_size, "source": "WEB", "client": "WEB"}
        for attempt in range(3):
            try:
                data = requests.get(EM, params=params, headers=UA, timeout=30).json()
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
        result = data.get("result") or {}
        rows = result.get("data") or []
        out.extend(rows)
        if page >= int(result.get("pages") or 1) or not rows:
            return out
        page += 1
        time.sleep(0.05)


def valuation(day: str) -> dict[str, dict]:
    path = CACHE / f"val_{day}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    rows = fetch_pages("RPT_VALUEANALYSIS_DET", f"(TRADE_DATE='{day}')")
    out = {}
    for r in rows:
        code = str(r.get("SECURITY_CODE") or "")
        out[code] = {"name": r.get("SECURITY_NAME_ABBR"), "cap": r.get("TOTAL_MARKET_CAP"),
                     "price": r.get("CLOSE_PRICE"), "pe": r.get("PE_TTM"),
                     "pb": r.get("PB_MRQ"), "board": r.get("BOARD_NAME")}
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def financials() -> dict[str, list[dict]]:
    path = CACHE / "fin.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, list[dict]] = {}
    for period in PERIODS:
        for r in fetch_pages("RPT_LICO_FN_CPD", f"(REPORTDATE='{period}')"):
            code = str(r.get("SECURITY_CODE") or "")
            out.setdefault(code, []).append({
                "period": period, "notice": str(r.get("NOTICE_DATE") or "")[:10],
                "roe": r.get("WEIGHTAVG_ROE"), "gross": r.get("XSMLL"),
                "profit": r.get("PARENT_NETPROFIT"), "cash_ps": r.get("MGJYXJJE")})
        for code in out:
            out[code].sort(key=lambda x: x["period"])
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def pick_fin(rows: list[dict], day: str) -> dict | None:
    usable = [r for r in rows if r["notice"] and r["notice"] <= day]
    return usable[-1] if usable else None


def annualize_roe(roe: float, period: str) -> float:
    """累计 ROE 年化：Q1×4、H1×2、Q3×4/3、年报×1。否则 4-7 月的池子会因季度 ROE 只有 2-4% 而塌成个位数。"""
    factor = {"03": 4.0, "06": 2.0, "09": 4 / 3, "12": 1.0}.get(period[5:7], 1.0)
    return roe * factor


def num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def build_pool(codes, val, fin, day, kind) -> tuple[list[str], dict]:
    hit, skipped = [], {"st": 0, "no_fin": 0, "fields": 0, "cap": 0}
    for code in codes:
        v = val.get(code) or {}
        name = str(v.get("name") or "")
        if not is_main(code) or "ST" in name.upper() or "退" in name:
            skipped["st"] += 1
            continue
        f = pick_fin(fin.get(code, []), day)
        if not f:
            skipped["no_fin"] += 1
            continue
        roe_raw, gross, profit, cash = (num(f["roe"]), num(f["gross"]), num(f["profit"]), num(f["cash_ps"]))
        roe = None if roe_raw is None else annualize_roe(roe_raw, f["period"])
        cap = num(v.get("cap"))
        if None in (roe, profit, cap):
            skipped["fields"] += 1
            continue
        if cap < 50e8:
            skipped["cap"] += 1
            continue
        if kind == "Q":
            if roe >= 10 and (gross or 0) >= 25 and profit > 0 and (cash or 0) > 0:
                hit.append(code)
        else:
            pe, pb = num(v.get("pe")), num(v.get("pb"))
            if pe and 0 < pe <= 15 and pb and 0 < pb <= 1.5 and roe >= 8:
                hit.append(code)
    return hit, skipped


def block_bootstrap(xs: list[float], n: int = 20000, seed: int = 20261108) -> tuple[float | None, float | None]:
    """按建池日重抽样的 block bootstrap 95% 区间（每次重抽整批 12 个差值）。"""
    import random
    if not xs:
        return None, None
    rng = random.Random(seed)
    k = len(xs)
    meds = []
    for _ in range(n):
        meds.append(st.median([xs[rng.randrange(k)] for _ in range(k)]))
    meds.sort()
    return meds[int(0.025 * n)], meds[int(0.975 * n)]


def median(xs):
    return st.median(xs) if xs else None


def main() -> int:
    global PERIODS
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--qcap", type=float, default=50e8)
    ap.add_argument("--periods", default=",".join(DEFAULT_PERIODS),
                    help="财报报告期，逗号分隔；须覆盖 --start 前的最近四个季度")
    ap.add_argument("--cal-start", default="2024-12-01")
    ap.add_argument("--cal-end", default="2026-11-08")
    ap.add_argument("--builds", default="",
                    help="只跑指定建池日（逗号分隔），用于复现比对；空=按月份自动推")
    ap.add_argument("--out", default="", help="输出目录；空=baseline/<时间戳>")
    args = ap.parse_args()
    PERIODS = tuple(p.strip() for p in args.periods.split(",") if p.strip())

    cal = trading_days(args.cal_start, args.cal_end)
    days_in = [d for d in cal if args.start <= d <= args.end]
    if args.builds:
        builds = [d.strip() for d in args.builds.split(",") if d.strip()]
        missing = [d for d in builds if d not in cal]
        if missing:
            raise SystemExit(f"builds 不在交易日历内: {missing}")
    else:
        builds = []
        for m in range(1, 13):
            month = [d for d in days_in if d[:7] == f"{args.start[:4]}-{m:02d}"]
            if month:
                builds.append(month[-1])

    fin = financials()
    print("calendar", len(cal), "build days", len(builds))

    results, detail, big_members = [], [], {}
    for day in builds:
        i = cal.index(day)
        entry, exit20, exit60 = cal[i + 1], cal[i + 21], cal[i + 61]
        val0, val1, val20, val60 = valuation(day), valuation(entry), valuation(exit20), valuation(exit60)
        def eligible(c: str) -> bool:
            name = (val0[c].get("name") or "")
            if not is_main(c) or not name or "ST" in name.upper() or "退" in name:
                return False
            if not num(val0[c].get("cap")):
                return False
            return pick_fin(fin.get(c, []), day) is not None
        universe = [c for c in val0 if eligible(c)]
        big_members[day] = None  # filled after big is defined below
        row = {"date": day, "entry": entry, "exit20": exit20, "exit60": exit60, "universe": len(universe),
               "cleaned_out": sum(1 for c in val0 if is_main(c) and c not in set(universe))}
        for kind in ("Q", "V"):
            pool, skipped = build_pool(universe, val0, fin, day, kind)
            def rets(codes, valx, base_val=None):
                out = {}
                base_val = base_val or val1
                for c in codes:
                    c0, c1 = num((base_val.get(c) or {}).get("cap")), num((valx.get(c) or {}).get("cap"))
                    if c0 and c1 and c0 > 0:
                        out[c] = c1 / c0 - 1
                return out
            mkt20 = rets(universe, val20)
            mkt60 = rets(universe, val60)
            big = [c for c in universe if (num((val0.get(c) or {}).get("cap")) or 0) >= args.qcap]
            big_members[day] = sorted(big)
            big20, big60 = rets(big, val20), rets(big, val60)
            row.update(market_missing20=len(universe) - len(mkt20), market_missing60=len(universe) - len(mkt60),
                       big_missing20=len(big) - len(big20), big_missing60=len(big) - len(big60))
            p20 = {c: mkt20[c] for c in pool if c in mkt20}
            p60 = {c: mkt60[c] for c in pool if c in mkt60}
            row[kind] = {"n": len(pool), "missing20": len(pool) - len(p20), "missing60": len(pool) - len(p60),
                         "med20": median(list(p20.values())), "med60": median(list(p60.values())),
                         "mean20": (st.mean(p20.values()) if p20 else None),
                         "market_med20": median(list(mkt20.values())), "market_med60": median(list(mkt60.values())),
                         "market_mean20": (st.mean(mkt20.values()) if mkt20 else None),
                         "big_n": len(big), "big_med20": median(list(big20.values())), "big_med60": median(list(big60.values())),
                         "skipped": skipped}
            if row[kind]["med20"] is not None and row[kind]["big_med20"] is not None:
                row[kind]["diff_big20"] = row[kind]["med20"] - row[kind]["big_med20"]
            if row[kind]["med60"] is not None and row[kind]["big_med60"] is not None:
                row[kind]["diff_big60"] = row[kind]["med60"] - row[kind]["big_med60"]
            if row[kind]["med20"] is not None and row[kind]["market_med20"] is not None:
                row[kind]["diff20"] = row[kind]["med20"] - row[kind]["market_med20"]
            if row[kind]["med60"] is not None and row[kind]["market_med60"] is not None:
                row[kind]["diff60"] = row[kind]["med60"] - row[kind]["market_med60"]
            if row[kind]["mean20"] is not None and row[kind]["market_mean20"] is not None:
                row[kind]["port_diff20"] = row[kind]["mean20"] - row[kind]["market_mean20"]
            detail.extend({"date": day, "pool": kind, "code": c, "ret20": p20.get(c), "ret60": p60.get(c)} for c in pool)
        results.append(row)
        print(json.dumps({k: v for k, v in row.items() if k in ("date", "Q", "V")}, ensure_ascii=False)[:400])

    out_dir = (Path(args.out) if args.out else
               ROOT / "baseline" / datetime.now(TZ).strftime("%Y%m%dT%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "by_date.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "detail.json").write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "big_members.json").write_text(json.dumps(big_members, ensure_ascii=False), encoding="utf-8")

    summary = {"generated_at": datetime.now(TZ).isoformat(timespec="seconds"), "builds": len(results),
               "max_market_missing_rate": max((r.get("market_missing20", 0) / max(r["universe"], 1)) for r in results),
               "max_big_missing_rate": max((r.get("big_missing20", 0) / max(r.get("Q", {}).get("big_n") or 1, 1)) for r in results)}
    for kind in ("Q", "V"):
        for h in ("20", "60"):
            xs = [r[kind].get(f"diff{h}") for r in results if r[kind].get(f"diff{h}") is not None]
            ps = [r[kind].get("port_diff20") for r in results if r[kind].get("port_diff20") is not None]
            bs = [r[kind].get(f"diff_big{h}") for r in results if r[kind].get(f"diff_big{h}") is not None]
            if bs:
                bm = st.mean(bs); bsd = st.stdev(bs) if len(bs) > 1 else 0.0
                blo, bhi = block_bootstrap(bs)
                summary[f"{kind}_{h}_vs_big"] = {"n": len(bs), "median_diff": median(bs), "mean_diff": bm,
                                                 "t": (bm / (bsd / (len(bs) ** 0.5)) if bsd else None),
                                                 "win": sum(1 for x in bs if x > 0) / len(bs),
                                                 "median_ci95": [blo, bhi], "bootstrap": "block_by_build_day_20000"}
            if xs:
                mean = st.mean(xs)
                sd = st.stdev(xs) if len(xs) > 1 else 0.0
                t = mean / (sd / (len(xs) ** 0.5)) if sd else None
                lo, hi = block_bootstrap(xs)
                summary[f"{kind}_{h}"] = {"n": len(xs), "median_diff": median(xs), "mean_diff": mean,
                                          "t": t, "win": sum(1 for x in xs if x > 0) / len(xs),
                                          "min": min(xs), "max": max(xs),
                                          "median_ci95": [lo, hi], "bootstrap": "block_by_build_day_20000",
                                          "mean_port_diff20": (st.mean(ps) if ps else None)}
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("OUT", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())