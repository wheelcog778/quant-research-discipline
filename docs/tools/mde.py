#!/usr/bin/env python3
"""mde.py — minimum detectable effect for a mean (or a difference of means).

Answers one question before a study runs: given this much noise and this many
observations, what is the smallest effect the sample could detect at the chosen
significance and power? If the answer is larger than the effect you care about,
the study cannot succeed — it can only return "uncertain".

Usage:
    python mde.py --sigma 14.71 --n 365
    python mde.py --sigma 14.71 --n 365 --alpha 0.05 --power 0.8
    python mde.py --sigma 20 --n 120 --design two --n2 120

Formula (normal approximation):
    one-sample:  MDE = (z_{1-alpha/2} + z_{power}) * sigma / sqrt(n)
    two-sample:  MDE = (z_{1-alpha/2} + z_{power}) * sigma * sqrt(1/n1 + 1/n2)

Assumptions, printed with every result:
  * independent observations — overlapping windows need an effective n, not the raw n
  * normal approximation; for n < 30 use a t quantile instead
  * sigma is the standard deviation of the quantity being averaged
"""
from __future__ import annotations

import argparse
import math
import sys
from statistics import NormalDist

ND = NormalDist()


def z(p: float) -> float:
    return ND.inv_cdf(p)


def mde(sigma: float, n: float, alpha: float = 0.05, power: float = 0.8,
        design: str = "one", n2: float | None = None, two_sided: bool = True) -> float:
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    if n <= 1:
        raise ValueError("n must be greater than 1")
    za = z(1 - alpha / 2) if two_sided else z(1 - alpha)
    zb = z(power)
    if design == "one":
        se = sigma / math.sqrt(n)
    else:
        if not n2 or n2 <= 1:
            raise ValueError("two-sample design needs --n2 greater than 1")
        se = sigma * math.sqrt(1 / n + 1 / n2)
    return (za + zb) * se


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Minimum detectable effect (80% power by default)")
    ap.add_argument("--sigma", type=float, required=True, help="standard deviation of the measured quantity, in the same unit as the effect")
    ap.add_argument("--n", type=float, required=True, help="number of independent observations")
    ap.add_argument("--n2", type=float, default=None, help="second group size (two-sample design)")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--power", type=float, default=0.8)
    ap.add_argument("--design", choices=["one", "two"], default="one")
    ap.add_argument("--one-sided", action="store_true", help="use a one-sided test (default: two-sided)")
    ap.add_argument("--unit", default="pp", help="unit label for the output (default: pp)")
    args = ap.parse_args(argv)

    try:
        value = mde(args.sigma, args.n, args.alpha, args.power, args.design, args.n2,
                    two_sided=not args.one_sided)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    za = z(1 - args.alpha / 2) if not args.one_sided else z(1 - args.alpha)
    zb = z(args.power)

    print(f"sigma={args.sigma:g}  n={args.n:g}"
          + (f"  n2={args.n2:g}" if args.design == "two" else "")
          + f"  alpha={args.alpha:g}  power={args.power:g}  design={args.design}"
          + ("  one-sided" if args.one_sided else "  two-sided"))
    print(f"z_alpha={za:.4f}  z_power={zb:.4f}")
    print(f"MDE = {value:.4f} {args.unit}")
    print()
    print("Assumptions:")
    print("  - observations are independent; overlapping windows need an effective n")
    print("  - normal approximation; use a t quantile when n < 30")
    print("  - sigma is the SD of the quantity being averaged, not of a single raw observation")
    print("  - note: some finance write-ups use a fixed 3.84 multiplier (2 x 1.96). That is")
    print("    roughly 97.5% power and stricter than the standard 2.80 used here; if a historical")
    print("    record quotes a larger MDE, check which multiplier it used before comparing.")
    if args.n < 30:
        print("  WARNING: n < 30 — the normal approximation is optimistic; use a t quantile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())