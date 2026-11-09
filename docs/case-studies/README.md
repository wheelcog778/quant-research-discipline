# Case studies — eight research lines and the numbers that killed them

Each file is a real research record, sanitised: project names and personal paths
are removed, the arithmetic and the decision thresholds are untouched. All eight
lines ended in a kill or a rejection. That is the normal outcome.

| # | File | Line | Verdict |
|---|---|---|---|
| 01 | [01-qv-pool-baseline.md](01-qv-pool-baseline.md) | Q/V pool history baseline | Q pool killed at the pre-registered line: 20-day −0.80pp (t −1.51), 60-day −1.72pp (t −3.24, 95% CI [−3.29, −0.35]) |
| 02 | [02-exit-experiment.md](02-exit-experiment.md) | Exit rules (V5.4 reconciliation) | Nine exit variants on the same entries: none turns negative expectancy positive; the external +1.08% claim is not reproducible |
| 03 | [03-filter-ablation.md](03-filter-ablation.md) | Filter ablation | The filter was losing money, but the loss was beta (small-cap premium), not alpha |
| 04 | [04-c1-pool-composition.md](04-c1-pool-composition.md) | Pool composition with delisted names | Adding 211 delisted stocks leaves the verdict unchanged: no usable alpha |
| 05 | [05-sue.md](05-sue.md) | SUE (standardised unexpected earnings) | Killed: no market-cap data for 2010–2017, so an out-of-sample revival is impossible |
| 06 | [06-st-delisting.md](06-st-delisting.md) | ST de-listing leg | Killed by the pre-registered §7-4 rule: MDE₈₀ = 2.96pp > 2.0pp — the sample cannot detect an effect that small |
| 07 | [07-sector-clustering.md](07-sector-clustering.md) | Sector clustering (limit-up breadth) | Killed at level 1: Q5−Q1 = −0.1137pp, t = −3.029 — statistically measurable, economically one ninth of the bar |
| 08 | [08-announcement-gate.md](08-announcement-gate.md) | Announcement hard-event gate | Rejected: the main test missed its pre-registered line, so no gate was built |

## How to read one

1. Read the pre-registration line at the top. If there is none, the record is not admissible.
2. Find the decision rule and the measured value. The gap between them is the whole story.
3. Check whether the sample handles delisted and suspended names.
4. Check what was *not* concluded. Every record here states its own limits.

## What is deliberately missing

- Raw price panels and factor values (not redistributable).
- Anything that would let you re-run the numbers without your own data source.

If a case study claims a number, it also states where the number came from and
which command produced it. That is the standard this repository is trying to hold.