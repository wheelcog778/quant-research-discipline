# 否决器（factor-verdict）v1.8.1

一套可以搬进任何智能体的**策略证伪工具包**：七道关卡 + 六个可跑的命令 + 一次完整的实战案例。

它不负责找策略，只负责杀掉假的。适用于：把时间或钱投进一个"看起来能赚钱"的信号之前。

- 纯 Python 标准库，零依赖（3.9+）
- 数据全部 CSV，任何语言/任何 agent 都能产出
- 自带十八项自检，**先证明工具能分辨真假，再用它下结论**

---

## 30 秒验证

```bash
cd factor-verdict
python scripts/vkit.py selftest
```

十八项全过才可用：

1. 真信号被认出、纯噪声被拒、IC 过高（>0.30）时给出前视警告
2. 并列因子分档与输入顺序无关
3. 跳空开盘低于止损位的入场**保留**、按开盘价离场
4. 400 次独立噪声抽样的假阳性率（实测 1.5% @ |t|≥2.6，验收线 4%）
5. HAC-OLS 在独立数据上退化为普通 t（比值 0.86–1.00）
6. 全并列分档返回"不可估"，不写成 0%
7. IC 零方差时不报 t 值
8. 扣成本按数据方向走：正向因子不再被判成 L3
9. 持有期重叠：先复现出日期聚类 t 的假阳性（69%），再验证重叠校正压到 5%
10. `ic` 与 `sim` 同一信号日的收益口径逐位一致
11. 因子全同分的截面被剔除，而不是记成 IC=0
12. 负数/零/重复/空持有期在入口被拒（不再静默算出回看收益）
13. 负成交量、空日期在读数时被拒，正常数据不被误杀
14. `rolling_ic` 零方差分段返回不可估（不再用 `1e-9` 算出上亿假 t），且分档 `t_HAC` 与 `hac_t_overlap` 逐位一致
15. `db_lab` 与 `pool_lab` 入口拒绝负数/零/重复持有期与非法列名，且 SQL 过滤条件参数化防注入
16. A 股可交易性硬过滤：T+1 停牌（`volume=0`）或一字板（`high=low`）不可买入，一字跌停止损顺延至首个开板日成交
17. `db_lab` CLI 与 `vkit.py db --walkforward` 接通，`walkforward(trials=...)` 门槛随尝试次数抬升
18. 多重检验账本（`.verdict_trials.json`）自动累计同一数据集的不同参数尝试次数，防手填 `--trials 1` 低报

---

## 目录

```
factor-verdict/
├── SKILL.md                  # 主入口：何时用、七关、六档分级、命令
├── checklist.md              # 门禁：判定"可提交"的唯一定义
├── README.md                 # 本文件
├── references/
│   ├── gates.md              # 每关阈值、假阳/假阴、报告模板
│   ├── pool-decomposition.md  # 关 6.5：池子分解与选股消融的口径
│   ├── score-construct.md     # 第 0 关：构造效度的判读与写法
│   ├── verification/          # 边界探针（成本方向/重叠校准/口径一致）
│   ├── reconciling-copies.md  # 收到外来 zip / 另一台机的版本时怎么合并
│   ├── case-study.md      # 实战案例：一个策略被三遍否证的全过程
│   ├── data-sources.md       # A股多源抓取、自建复权、大样本审计
│   └── CHANGELOG.md          # v1.1–v1.8.0 每一条修了什么、对结论有什么影响
├── agents/
│   ├── CLAUDE.md             # 给 Claude Code 的挂载说明
│   └── AGENTS.md             # 给 Codex / 通用 agent 的挂载说明
├── examples/
│   ├── export_from_db.py     # 从审计库导出全部 CSV，使案例每张表可复现
│   ├── rev_rows.csv          # 案例：反转类 20 日收益（集中度/择时可直接跑）
│   ├── rev_signals.csv
│   └── index.csv
└── scripts/
    ├── vkit.py               # 七个子命令 + 十八项自检
    ├── factor_lab.py         # 通用因子检验库（IC/IR/分层/滚动/Bonferroni/HAC）
    ├── db_lab.py             # 审计库直读（百万行级，不走 CSV）
    ├── pool_lab.py           # 关 6.5 池子分解与四种选法消融
    └── probe_score_construct.py  # 第 0 关：封顶/托底/分类二次赋值的 30 秒定性
```

---

## 怎么装进不同的智能体

### Hermes Agent

已安装为 skill `factor-verdict`，直接说"用否决器检验这个策略"。

### Claude Code

```bash
cp factor-verdict/agents/CLAUDE.md ./CLAUDE.md
```

### Codex / 其他读 AGENTS.md 的工具

```bash
cp factor-verdict/agents/AGENTS.md ./AGENTS.md
```

### 任何聊天型模型（Kimi / GPT / Claude 网页版）

把 `SKILL.md` 全文作为系统提示，附件带 `references/gates.md`。
模型不需要执行代码也能用七关 + 门禁清单做审查——只是数字要你自己跑。

### 自己跑

```bash
python scripts/vkit.py ic     --bars data/bars --factor data/factor.csv --trials 4
python scripts/vkit.py conc   --rows trades.csv
python scripts/vkit.py sim    --bars data/bars --signals signals.csv --hold 20
python scripts/vkit.py timing --signals signals.csv --index index.csv
python scripts/vkit.py yearly --bars data/bars --factor data/factor.csv --horizon 5
python scripts/vkit.py ic     --bars data/bars --factor data/factor.csv --cost 0.20
```

---

## 数据格式

全部 UTF-8 CSV，第一行表头。

| 文件 | 列 | 说明 |
|---|---|---|
| `bars/<code>.csv` | date,open,high,low,close[,volume] | 每只标的一个文件，文件名即代码 |
| `factor.csv` | date,code,value | 因子值，**只能用该日收盘前可得的信息** |
| `signals.csv` | date,code | 入场信号日 |
| `rows.csv` | date,value | 每笔/每观测的收益（百分数），集中度检验用 |
| `index.csv` | date,close | 基准指数 |

非法数值、重复日期、OHLC 不自洽、重复信号会被直接拦下并报出行号，不会静默变成 0。

---

## 五个命令在做什么

| 命令 | 回答 | 对应关卡 |
|---|---|---|
| `ic` | 有没有信息、能撑几天、是不是每段都有、扣完成本还剩多少 | 关 1–3、5 |
| `conc` | 收益是不是全在几天；那几天事前可不可识别 | 关 6 |
| `sim` | 止损/离场规则：均值、尾部、换手、聚类 t、重叠校正 t | 关 7 |
| `timing` | 信号日之后市场真的更好吗（HAC 校正） | 横向 B |
| `selftest` | 工具自己有没有坏 | — |

关 4（分年）用 `yearly`；关 5（经济量级）用 `ic --cost`。口径细节见 `references/gates.md`。

---

## **v1.8.1** 改了什么（要点）

- **`yearly` 补上可交易性过滤**：v1.8.0 只在 `ic` 接了 `untradable_by_code`，`yearly` 没接——同一份数据、同一个持有期会算出两个 IC（实测 +0.0097 vs +0.0111）。现在两处同口径，`yearly` 表头也会打印「不可成交剔除 N 条」（自检 16 加了这条断言）。
- **两条错误信息改成说人话**：全部入场被停牌/一字板拦掉时，`ic` 不再说「检查日期/代码是否对得上」，改报「N 条入场全部落在停牌/一字板日」；`sim` 不再只报「没有可模拟的入场」，改把不可买/缺行情/窗口不足的计数一起打出来。

## **v1.8.0** 改了什么（要点）

- **补齐 A 股停牌与一字涨跌停过滤**：T+1 入场遇停牌（`volume=0`）或一字板（`high=low`）自动拦截并报数；`sim` 止损遇一字跌停或停牌无法卖出时，顺延至首个开板日按开盘价离场（自检 16）。
- **打通 `vkit.py db` 与 Walk-Forward CLI**：`db_lab.py` 补齐 CLI 入口并修好 `walkforward(trials=...)` 的 Bonferroni 门槛计算，`vkit.py` 新增 `db` 子命令统一 CSV 与 SQLite 双通道（自检 17）。
- **多重检验尝试次数自动记账**：在数据目录自动维护 `.verdict_trials.json` 记录不同参数组合尝试次数；手填 `--trials` 低于累计次数时自动纠偏并告警（自检 18）。

## v1.7.0 改了什么（要点）

- **清掉最后一处零方差假 t**：`factor_lab.rolling_ic` 原先保留了 `pstdev or 1e-9`，常数分段会算出 1.22 亿的假 t，现统一返回 `None`（不可估）。
- **全包重叠 t 口径三处合一**：`spread_summary` 与 `pool_lab.py` 选股消融的 `alpha_t` 统一改调 `factor_lab.hac_t_overlap`（lag=H−1），消除 Bartlett 单核与朴素 t 的残留分叉。
- **大样本 SQL 通道补齐入口闸门与参数化**：`db_lab.py` 与 `pool_lab.py` 在入口拦截非正整数/重复持有期、非正 top-N 与负成本，SQL `WHERE` 全部改为 `?` 参数化绑定并校验表/列标识符。
- **文档与实战案例过检**：版本号统一为 `1.7.0`（过 `version_lint.py`），跨文件重复行清零（过 `doc_dup_scan.py`），`references/case-study.md` 补齐七件套章节标题、复现命令与末节边界（过 `doctrine_check.py`）。自检扩至 **15 项**。

## v1.6 改了什么（要点）

- **非法持有期不再放行**：`--horizons -5` 过去会静默输出「−5日」表并标显著（用的是回看收益），
  现在入口拒绝，`build_rows` / `report` 各再验一次。
- **坏行情在入口被拒**：负成交量、空日期；正常数据不误杀。
- **零方差的分档价差给"不可估"**，不再用 `pstdev or 1e-9` 兜出一个假 t；表格补 `t_HAC` 列。
- 除权判定改为"可能除权，须公告/股本/第二源交叉验证"，不再仅凭 −11.5% 阈值整体缩放。
- 报告补「输入观测 vs 可用观测」「收益口径」两行；自检 13 项。
- 统计口径未动：真实数据（2024 全年、2965 只、5 日）合并前后 IC/t/t_HAC/分档/成本判定逐位一致。

## v1.3 改了什么（要点）

- **扣成本按数据方向**：正向因子不再被判成"不可实施（L3）"，并分出"多空"与"只用多头腿"两种口径。
- **组合模拟加重叠校正 t**：日期聚类处理不了相邻信号日共享 H-1 天行情，新增按日等权 + 平坦权
  HAC + t 临界值，假阳性从 46%–69% 压到 5%–8%。
- **`ic` 与 `sim` 同口径**：都改成 T+1 收盘买、T+1+H 收盘卖（`--entry open` 可选，但会多一段隔夜）。
- **无定义的 IC 不再记 0**：因子全同分 / 未来收益全相同的截面直接剔除，并在报告里报数。
- 自检从 7 项扩到 11 项，每一项都设计成"能失败"；假阳性验收线从 12% 收到 4%。

## v1.1 与 v1.0 的差别（要点）

- 并列因子同分同档；跳空入场不再被删除；非法数值报错。
- IC 的 t 改用 **Newey-West(HAC)**；择时用 HAC-OLS；组合模拟的配对差按**日期聚类**。
  实测同一批数据：配对差 t 从 −21.98（朴素）修正为 **−3.36（仅日期聚类；v1.3 起还要求重叠校正）**。
- 结论从"判死/有效"改为 **L0–L5 六档**；集中度改为"两问"；组合模拟同时报尾部与换手。
- 自检从 3 项扩到 4 项，新增假阳性率与高 IC 前视报警。

完整对照见 `references/CHANGELOG.md`。**修复没有改变案例结论，但改变了它的强度。**

---

## 使用纪律

1. **回测前先写下 Gate，中途不许改。** 改一次就等于多试一次。
2. **试了几次要如实算**：不同参数、不同持有期、不同样本段、不同分类口径，每个都算一次。
3. **任何"组合赚钱"先做集中度，并回答那几天事前能否识别。**
4. **类别标签必须回查代码分支。** 统计上"有效"的东西，可能是另一件事。
5. **判死的理由不是"结果难看"，是"能救它的解释都被排除了"。**
6. **没有重算命令的数字，不进报告。**

---

## 出处

案例数字来自一次真实审计：3018 只 × 1439 天 × 410 万观测，两个独立数据源，两种独立实现交叉验证。
`examples/export_from_db.py` 可以把那份审计库导成 vkit 的输入，让案例的每一张表都可复现。