# Quantitative Research with Pre-registration and Kill Criteria

> Avoid fooling yourself: pre-register, verify independently, kill when it fails.

A toolkit and a case book for one job: deciding whether a trading signal is real **before** you risk money on it. It is not a strategy library — it is the machinery that kills strategies.

- Pure Python standard library (3.9+), zero dependencies
- Data in CSV; any language or agent can produce it
- 18 self-tests that first prove the tool can tell true from false

---

## Why this exists

Most backtests fail for reasons that are cheap to rule out and expensive to discover late:

- the rule was chosen after seeing the result (no pre-registration)
- the date range was not fixed in advance
- one parameter set was tried, then twenty more
- the sample excludes delisted stocks
- a 2% effect is claimed from a sample whose noise floor is 3%
- the second reader is the same person, or the same prompt

This repository packages the countermeasures: a pre-registration template, a
seven-gate verification toolkit, an MDE calculator, and eight case studies where
the answer turned out to be "no".

## What is inside

```
vkit/          seven gates, six runnable commands, 18 self-tests
  scripts/     factor_lab.py, pool_lab.py, db_lab.py, vkit.py, probe_score_construct.py
  references/  gates, score construction, pool decomposition, data sources, reconciliation
baseline/      parameterised portfolio baseline (baseline_v2.py)
docs/
  case-studies/  eight real research lines, each with its kill decision
  templates/     pre-registration template
  tools/         MDE calculator
```

## Quick start

```bash
python vkit/scripts/vkit.py selftest     # 18/18 required before you trust anything
python vkit/scripts/vkit.py --help       # the six commands
python docs/tools/mde.py --sigma 20 --n 100 --alpha 0.05 --power 0.8
```

The toolkit runs on CSV. Bring your own panel: one row per (date, symbol) with a
forward return column and whatever factors you are testing.

## The seven gates

| # | Gate | The question it answers |
|---|---|---|
| 1 | Data integrity | Are the bars real, complete, and in the right order? |
| 2 | Point-in-time | Would this information have been available on that date? |
| 3 | Pre-registration | Was the rule, sample and threshold frozen before the run? |
| 4 | Multiple testing | How many variants were tried before this one? |
| 5 | Effect size | Is the effect bigger than the noise floor (MDE)? |
| 6 | Robustness | Does it survive sub-periods, alternative definitions, costs? |
| 7 | Independent verification | Did someone else recompute it from the artifacts? |

Full text: [`vkit/references/gates.md`](vkit/references/gates.md).

## Eight case studies

Each one is a real research line that ended in a kill decision, with the numbers
that killed it (07 is the exception: it records an audit that stopped before
producing numbers). Names are sanitised; the arithmetic is not.

| # | Line | Verdict |
|---|---|---|
| 01 | Q/V pool history | Q pool killed: 20-day −0.80pp (t −1.51), 60-day −1.72pp (t −3.24) |
| 02 | Exit rules (V5.4) | Exit rules cannot turn a negative expectancy positive; the claimed +1.08% is not reproducible |
| 03 | Filter ablation | The filter was losing money, but it was losing beta, not alpha |
| 04 | Pool composition with delisted stocks | Adding 211 delisted names does not change the verdict: no usable alpha |
| 05 | SUE | Killed: no market-cap data for 2010–2017, so no out-of-sample revival |
| 06 | ST de-listing leg | Killed: MDE₈₀ 2.96pp against a 2.0pp threshold — noise, not absence of effect |
| 07 | Sector clustering | Earlier level-1 kill stands; this record is the re-audit, which stopped at its input gate (date boundary) before producing new numbers. The earlier figures are not in this repository |
| 08 | Announcement gate | Rejected: the main test failed its pre-registered line; no gate was built |

Index: [`docs/case-studies/README.md`](docs/case-studies/README.md).

## Why "kill" is the product

An honest research process produces mostly negative results. A process that only
reports wins is not a process, it is marketing. Publishing the eight kills is the
point: each one saved money that would otherwise have been spent trading noise.

The pre-registration template requires four things before any number is produced:
hypothesis, sample (including how delisted and suspended names are handled),
decision threshold (with an MDE calculation), and a stop condition.

## Known limitations

- The sample data behind the case studies is not included. Numbers are reported; the raw panel is not redistributable.
- The MDE calculator assumes independent observations. Overlapping windows need an effective sample size; the tool prints its assumptions and refuses to pretend otherwise.
- This is a single-machine, file-based workflow. It has no execution, no portfolio accounting, no live trading.
- Statistical gates do not prove a signal is real; they only make it harder to fool yourself.

## Commercial support

Audits of backtests for look-ahead bias, multiple testing and survivorship bias.
Open an issue with the `audit` label.

## License

MIT. See [LICENSE](LICENSE).

## Contact

Author: Q洗Y烟机 · 753073396@qq.com
