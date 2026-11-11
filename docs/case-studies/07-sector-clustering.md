# 板块抱团的审计进展与停止记录

记录时间：2026-11-07 13:17:13 +08:00。执行者：Codex (GPT-6)。

**真实研究尚未完成。** 新审计脚本、预注册与合成验证已准备完成；真实输入日期超过登记上界，输入门按约定退出。没有本轮新的收益差、IC 或 HAC t 结果，也没有改写旧判死记录。

## 已落实的工作

我先按全局交接规则读取限定资料并认领该审计任务，随后冻结预注册，新增 `src/hac_audit.py` 和 `src/hac_audit_test.py`。脚本保留旧 A 算法复现用于精确对账，再实现统一的个股-日 A/B/C/D：特征先由当日数据生成，候选剔除涨停，按代码破平分档；HAC 主带宽固定为10，敏感性0/4/20，原1pp/2.9判据保留。

静态审计确认旧 `src/test.py:37` 先过滤未来 ex5 缺失再计算特征，并未排除当日涨停样本；旧 `variants.py` 按行业数分档、日差提前舍入且没有处理循环尾部缓冲。新实现对这些问题已通过合成验证，**尚未在真实样本上验证其数值影响**。

## 日期差异与停止

缓存元信息：4,191,087 行、1,433 个日期，2020-12-01 至 2026-10-30。原预注册及本轮冻结上界为2026-10-29。

最终真实输入检查输出：

```text
STOPPED: DATE_BOUNDARY: actual 2020-12-01..2026-10-30; registered 2020-12-01..2026-10-29; no row-level analysis performed
exit_code=2
protected_inputs_unchanged=10/10
```

停止条件来自本轮预注册第16行和第65行；全局 AGENTS.md §5 要求口径决策写入待批。我没有截断数据、放宽边界或查看越界日的个股收益。`reviewed_guard/` 中没有 summary.json、daily_A.csv 或 legacy_reconciliation.json。原始缓存末日越界不等于已证明有效收益样本越界；本轮已冻结整个缓存的元信息边界，因此仍按约定停止。

待确认文件：`<decision-queue>/HAC-样本日期边界.md`。建议确认以完整现有缓存为同样本审计对象，将实际末日登记为10-30；确认后另写补充预注册并保留本次记录，先对账旧A，再计算修正结果。原判据、固定带宽与不发版边界均不改变。

## 真正执行的验证

| 验证 | 真实结果 | 可说明的范围 |
|---|---|---|
| `python -B -W error::ResourceWarning <workspace>/work/hac_audit_test.py` | 28项通过；退出0 | 数学公式、缺失日期、涨停剔除、特征不看未来目标、分档、末日、常量退化和判据等合成检查 |
| `python -B <workspace>/work/check_synthetic_e2e.py --fixture-root <workspace>/work/synthetic_e2e_reviewed` | 6000行/30日人工数据；旧脚本全字段对账；40组HAC矩阵比对、25组五档CSV复算通过；退出0 | 完整流程只在人工数据库通过，不是投资证据；重跑时必须使用另一个新的fixture目录 |
| `python -B <project>/src/hac_audit.py --out <project>/reports/hac_audit_20261107/reviewed_guard` | 退出2，DATE_BOUNDARY | 真实输入停止机制有效；未执行逐日样本研究 |
| 最终暂存脚本与项目脚本逐字节比对 | 2/2一致 | 已测试代码与项目最终版本一致 |
| 原始输入/旧脚本/旧结果/预注册运行前后SHA256 | 10/10一致 | 受保护输入未改变 |

全部输出保留于 `<project>/reports/hac_audit_20261107/`。历轮检查分别留档；最终来源以 `reviewed_guard/run_manifest.json` 和本表为准。

独立复核提出三项实现问题，我已据此修复：非二进制精确常量产生伪HAC t、修正路径漏过恒定行业收益、输入哈希检查前发布summary。处理采用精确全等检查（不设置近零容差），旧算法回放保留原行为；最终summary只在完整性通过后写出，停止状态另有status.json。独立复验记录见本工作区outputs/T-002-独立复核.md。

## 产物清单

| 绝对路径 | 字节数 | SHA256 |
|---|---:|---|
| `<project>/docs/预注册-HAC与旁证口径-2026-11-07.md` | 7445 | `baa507adbf956a81cc04b18418437a93f944e7be27869cd70fd850d632a20ea5` |
| `<project>/src/hac_audit.py` | 19443 | `5fc9c71c2a9444face40472ff4aafbc6fd3224fa8eeaa76cf296414ec40ef403` |
| `<project>/src/hac_audit_test.py` | 12472 | `b544bf647dc908100db49cf9be0f15641e850b2b25ef2ca21be8167e91d9879a` |
| `<project>/reports/hac_audit_20261107/reviewed_guard/STOP.json` | 209 | `43397251072404fa45bb0549f0e59b74a3f8344b5a59f45ba7297a43b2f23099` |
| `<project>/reports/hac_audit_20261107/reviewed_guard/panel_metadata.json` | 101 | `10c71a71737e1bf09562333ee4683224119d41c4b43330a4f33b49645e6c1023` |
| `<project>/reports/hac_audit_20261107/reviewed_guard/input_integrity.json` | 39 | `94ec2f0d6ea8ab7e4e2e36283224d273459588e2e8465fe61c1ea82662bd5a58` |
| `<project>/reports/hac_audit_20261107/synthetic_tests_reviewed.log` | 3506 | `1030df8409a9fc217a408030c23b917f46e618b67af753224e278e5a6a80a422` |
| `<project>/reports/hac_audit_20261107/synthetic_e2e_reviewed_verification.json` | 285 | `34e612d56492fdfb9d925aff2100b391fd1cf279e970a2f5b4d0fde66820b176` |
| `<decision-queue>/HAC-样本日期边界.md` | 3088 | `c31a1182f3cbdc1e62fc40876b016c9d6ca6247e5ea27e2450302c86cd1f2df8` |

## 边界与后续

产品本体、原始面板、旧源码与结果、交付包、归档和规则均未修改；没有网络调用、交易、发版或真实密钥内容产出。前向影子样本没有增加，不评价命中率或误杀率。

下一步是确认上述日期口径后创建补充预注册，绑定其哈希并使用新的输出目录执行审计。确认前不绕过停止条件。用户已要求我继续自主规划与分工，因此其他可做任务并行推进，此问题以书面留言留存。

## 独立复核关闭回执

协作复核者已对R1/R2/R3独立复验并关闭：100组非二进制常量、多长度/带宽/缺失场景无伪t；5组恒定行业收益在corrected分支被跳过，legacy保留原行为；模拟受保护输入变化时退出2，STOP/status明确，summary与阈值结论均不发布。真实微小非零方差被保留，没有用近零容差改口径。

实跑命令：`python -B -W error::ResourceWarning <workspace>/outputs/独立复验.py` → exit0，passed=true，closed=[R1,R2,R3]。独立报告：`<workspace>/outputs/独立复核.md`（8183 B，SHA256 7becaf9cad6bb324dd82e9ef99ef1ec911461105ea2c7f0dd75fe99c07c64c42）；复验脚本5676 B，SHA256 512d0ef0e93e6a6d4c0c347df1bd86c100e7a4eec1fbe7dabd39527e95584313。该复验只打开自己创建的人工空数据库。

主智能体也回读并核对7份reviewed_guard产物hash、10份受保护输入当前hash及项目报告/INDEX副本，全部一致。T-002以blocked交接，等待日期口径确认；实现验收完成不代表真实研究完成。